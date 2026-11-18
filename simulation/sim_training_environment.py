# -*- coding: utf-8 -*-
"""
simulation/sim_training_environment.py
Entorno acelerado de simulación paso a paso (Step-Based RL Training Environment)
para entrenar y calibrar el cerebro candidato de LNM Sniper (Schema v2) y el
motor de cobertura (Hybrid Edge Hedging) con rigor matemático y aislamiento absoluto.
"""
from typing import Dict, List, Optional, Tuple, Any
import math
import time
import os
import random

from simulation.sim_clock import SimClock
from simulation.sim_market_feed import SimMarketFeed, Candle
from simulation.sim_lnm_exchange import SimLNMExchange
from lnm_sniper_v6_advanced import (
    AutonomousGoalBrain,
    Config,
    THRESHOLDS_MANAGER,
    calculate_strategy_order_params,
)
from hedge_state_store import HedgeStateStore
from adaptive_controller import AdaptiveHedgeController
from edge_monitor import EdgeMonitor
from portfolio_hedge import PortfolioHedgeCoordinator
from predictive_hedge import PredictiveHedgeEngine
from guardian_execution_core import HardFloorGuardian, compat_np as np
from sniper_perception_sats_core import SatsExpectancyGate, MarketRegimeClassifier


class SimTrainingEnvironment:
    """
    Entorno coordinado de alta velocidad para entrenamiento RL offline.
    Ejecuta el bucle completo de trading (percepción, dimensionamiento Kelly,
    selección greedy/explorer, coberturas adaptativas y aprendizaje de Bellman).
    """

    def __init__(
        self,
        candidate_brain_file: str = "q_brain_simulated_candidate.json",
        candidate_hedge_file: str = "hedge_state_simulated_candidate.json",
        initial_balance_sats: int = 100000,
        explorer_mode: bool = True,
        learning_enabled: bool = True,
        taker_fee_rate: float = 0.0010,
        funding_rate_8h: float = 0.0001,
        default_slippage_bps: float = 1.5,
    ):
        self.candidate_brain_file = str(candidate_brain_file)
        self.candidate_hedge_file = str(candidate_hedge_file)
        self.initial_balance_sats = int(initial_balance_sats)
        self.explorer_mode = bool(explorer_mode)
        self.learning_enabled = bool(learning_enabled)

        self.clock = SimClock(start_timestamp=1700000000.0)
        self.feed = SimMarketFeed()
        self.exchange = SimLNMExchange(
            initial_balance_sats=self.initial_balance_sats,
            taker_fee_rate=taker_fee_rate,
            funding_rate_8h=funding_rate_8h,
            default_slippage_bps=default_slippage_bps,
        )

        # Cerebros estrictamente aislados (nunca tocan los archivos de producción)
        self.brain = AutonomousGoalBrain(
            filename=self.candidate_brain_file,
            explorer_mode=self.explorer_mode,
        )
        self._seed_canonical_states_if_needed()
        self.hedge_store = HedgeStateStore(
            filename=self.candidate_hedge_file,
        )
        self.hedge_controller = AdaptiveHedgeController()
        self.floor_guardian = HardFloorGuardian(
            initial_floor_ratio=Config.INITIAL_FLOOR_RATIO,
            milestone_floor_ratio=Config.MILESTONE_FLOOR_RATIO,
            max_dd_pct=Config.MAX_DRAWDOWN_PCT,
            max_daily_loss_pct=Config.MAX_DAILY_LOSS_PCT,
        )

        self.last_entry_ts: float = 0.0
        self.step_count: int = 0
        self.total_settled_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_net_pnl_sats: int = 0
        self.total_fees_paid_sats: int = 0
        self.peak_equity_sats: int = self.initial_balance_sats
        self.max_seen_drawdown_pct: float = 0.0

    def _seed_canonical_states_if_needed(self) -> None:
        """Siembra los 175 estados canónicos si el cerebro candidato es nuevo."""
        if len(self.brain.q_table) >= 100:
            return

        # Cargar claves de estado canónicas de referencia sin modificar producción
        ref_file = "q_brain_advanced_lnm.json"
        if os.path.exists(ref_file):
            try:
                from guardian_execution_core import AtomicBrainStore
                p_data = AtomicBrainStore(ref_file).load()
                if isinstance(p_data, dict) and "q_table" in p_data:
                    for k in p_data["q_table"].keys():
                        norm_k = AutonomousGoalBrain._normalize_state_key(k)
                        if norm_k not in self.brain.q_table:
                            self.brain.q_table[norm_k] = self.brain._init_q_vector(norm_k)
                            self.brain.state_visits[norm_k] = 0
                    self.brain.dirty = True
                    return
            except Exception:
                pass

        regimes = ["RANGE_NORMAL", "RANGE_SQUEEZE", "TREND_BULL", "TREND_BEAR", "VOLATILITY_EXPANSION", "CHOP_HIGHVOL"]
        oscillators = ["OVERSOLD", "DIP", "MID", "PEAK", "OVERBOUGHT"]
        expansions = ["LOW_EXP", "MID_EXP", "HIGH_EXP"]
        macros = ["BULL", "BEAR", "CHOP"]
        emas = ["FULL_BULL", "BULL_DIP", "FULL_BEAR", "BEAR_RALLY", "EMA_TRANS"]

        for r in regimes:
            for o in oscillators:
                for e in expansions:
                    for m in macros:
                        valid_emas = (
                            ("EMA_TRANS", "FULL_BULL", "BULL_DIP")
                            if m == "BULL"
                            else (("EMA_TRANS", "FULL_BEAR", "BEAR_RALLY") if m == "BEAR" else ("EMA_TRANS",))
                        )
                        for em in valid_emas:
                            norm_s = AutonomousGoalBrain._normalize_state_key(f"{r}|{o}|{e}|{m}|{em}")
                            if norm_s not in self.brain.q_table:
                                self.brain.q_table[norm_s] = self.brain._init_q_vector(norm_s)
                                self.brain.state_visits[norm_s] = 0
        self.brain.dirty = True

    def reset(
        self,
        candles_1m: List[Candle],
        initial_balance_sats: Optional[int] = None,
        warmup_bars: int = 80,
    ) -> dict:
        """
        Reinicia el entorno con un nuevo lote de velas y balance inicial.
        """
        if initial_balance_sats is not None:
            self.initial_balance_sats = int(initial_balance_sats)

        prev_counter = getattr(self.exchange, "trade_counter", 1000)
        self.exchange = SimLNMExchange(
            initial_balance_sats=self.initial_balance_sats,
            taker_fee_rate=self.exchange.taker_fee_rate,
            funding_rate_8h=self.exchange.funding_rate_8h,
            default_slippage_bps=self.exchange.default_slippage_bps,
        )
        self.exchange.trade_counter = prev_counter
        self.feed.load_candles(candles_1m)
        self.feed.warmup(warmup_bars)

        start_ts = float(self.feed.last_candle.timestamp if self.feed.last_candle else 1700000000.0)
        self.clock.set_time(start_ts)

        self.last_entry_ts = 0.0
        self.step_count = 0
        self.total_settled_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_net_pnl_sats = 0
        self.total_fees_paid_sats = 0
        self.peak_equity_sats = self.initial_balance_sats
        self.max_seen_drawdown_pct = 0.0
        self.floor_guardian = HardFloorGuardian(
            initial_floor_ratio=Config.INITIAL_FLOOR_RATIO,
            milestone_floor_ratio=Config.MILESTONE_FLOOR_RATIO,
            max_dd_pct=Config.MAX_DRAWDOWN_PCT,
            max_daily_loss_pct=Config.MAX_DAILY_LOSS_PCT,
        )
        self.brain.active_trades.clear()
        self.hedge_store.pairs.clear()

        return self.feed.get_perception_state()

    def _consolidate_orphan_hedge(
        self, orig_id: str, orig_info: Optional[dict], close_price: float, now: float
    ) -> None:
        """Cierra cobertura huérfana si existe y consolida su PnL/comisiones en la primaria."""
        oid = str(orig_id)
        pair_ctx = self.hedge_store.pairs.get(oid)
        if isinstance(pair_ctx, dict) and pair_ctx.get("hedge_trade_id"):
            h_tid = str(pair_ctx["hedge_trade_id"])
            if h_tid in self.exchange.running_trades:
                ok_oh, rec_oh = self.exchange.close_trade(
                    h_tid, price=float(close_price), reason="ORPHAN_HEDGE_CLEANUP", now=now
                )
                if ok_oh and rec_oh and isinstance(orig_info, dict):
                    orig_info["realized_hedge_net_sats"] = (
                        int(orig_info.get("realized_hedge_net_sats", 0))
                        + int(rec_oh.get("net_pnl_sats", 0))
                    )
                    orig_info["realized_hedge_fee_sats"] = (
                        int(orig_info.get("realized_hedge_fee_sats", 0))
                        + int(rec_oh.get("entry_fee_sats", 0))
                        + int(rec_oh.get("exit_fee_sats", 0))
                        + int(rec_oh.get("carry_fees_sats", 0))
                    )
        if oid in self.hedge_store.pairs:
            self.hedge_store.pairs.pop(oid, None)
            if self.learning_enabled:
                self.hedge_store.dirty = True

    def _settle_primary_event(
        self, ev: dict, curr_state_key: str, now: float, close_price: float
    ) -> int:
        """Consolida cobertura vinculada, actualiza métricas y entrena Q-table para una primaria."""
        tid = str(ev["id"])
        orig_info = self.brain.active_trades.pop(tid, None)
        if orig_info is None:
            return 0

        self._consolidate_orphan_hedge(tid, orig_info, close_price=close_price, now=now)

        orig_st = orig_info.get("state", curr_state_key)
        act_taken = int(orig_info.get("action", 1))
        realized_h_net = int(orig_info.get("realized_hedge_net_sats", 0))
        realized_h_fee = int(orig_info.get("realized_hedge_fee_sats", 0))
        net_pnl = int(ev.get("net_pnl_sats", 0)) + realized_h_net
        margin_used = int(orig_info.get("margin", 1000))
        lev_used = float(orig_info.get("leverage", 10))
        hit_tp = bool(ev.get("close_reason") == "TAKE_PROFIT")
        dur_ticks = max(1.0, float(ev.get("duration_s", 60.0)) / 60.0)
        mae_tracked = float(orig_info.get("mae_sats", max(0, -net_pnl)))
        mfe_tracked = float(orig_info.get("mfe_sats", max(0, net_pnl)))
        total_taker_fee = int(ev.get("entry_fee_sats", 0) + ev.get("exit_fee_sats", 0)) + realized_h_fee

        self.total_settled_trades += 1
        self.total_net_pnl_sats += net_pnl
        if net_pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        if self.learning_enabled:
            self.brain.remember_and_train(
                orig_st,
                act_taken,
                reward_sats=net_pnl,
                margin_used=margin_used,
                next_state=curr_state_key,
                equity_after=self.exchange.equity_sats,
                available_balance_after=self.exchange.available_balance_sats,
                real_balance_delta_sats=net_pnl,
                hit_tp=hit_tp,
                harvested=bool(ev.get("close_reason") in ("FORCED_EDGE_EXIT", "CLOSE_ORIGINAL_POSITION")),
                recovery_mode=False,
                lev_used=lev_used,
                exact_fee_sats=total_taker_fee,
                pl_includes_fees=True,
                funding_fee_sats=int(ev.get("carry_fees_sats", 0)),
                trade_id=tid,
                mae_sats=mae_tracked,
                mfe_sats=mfe_tracked,
                duration_ticks=dur_ticks,
                predicted_q_entry=orig_info.get("entry_q", None),
            )
        return net_pnl

    def step(self) -> Tuple[Optional[dict], int, bool, dict]:
        """
        Avanza un paso de simulación (1 vela de 1 minuto):
        1. Alimenta la vela al feed y al exchange.
        2. Procesa cierres, stop losses, FORCED EDGE EXIT con viabilidad neta y liquidaciones.
        3. Entrena la Q-table del cerebro con las operaciones concluidas (Schema v2).
        4. Supervisa coberturas mediante AdaptiveHedgeController y HedgeRiskManager.
        5. Evalúa nuevas aperturas viables respetando compuertas Bayesianas y HardFloorGuardian.
        """
        c = self.feed.next_step()
        if c is None:
            return None, 0, True, {"reason": "END_OF_DATA"}

        self.step_count += 1
        self.clock.set_time(c.timestamp)
        now = c.timestamp

        # 1. Evaluar posiciones vivas contra extremos de la vela en el exchange
        events = self.exchange.step_candle(c)

        mkt_state = self.feed.get_perception_state()
        ind_5m = mkt_state["ind_5m"]
        ind_15m = mkt_state["ind_15m"]
        ind_1h = mkt_state["ind_1h"]
        ind_4h = mkt_state["ind_4h"]
        perception = mkt_state["perception"]
        cur_atr = max(1.0, float(ind_5m.get("atr", c.close * 0.002)))

        pre_primary_count = sum(
            1 for tid_k, tr_v in self.exchange.running_trades.items()
            if not tr_v.get("_is_hedge", False) and not self.hedge_store.is_hedge_trade(tid_k)
        )
        pre_state_key = self.brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, pre_primary_count)
        pre_q_vec = self.brain.get_state_q_values(pre_state_key)

        # Actualizar excursiones MFE/MAE y ejecutar Guardián (incluyendo FORCED EDGE EXIT con viabilidad neta)
        for live_tid, live_t in list(self.exchange.running_trades.items()):
            if live_t.get("_is_hedge", False) or self.hedge_store.is_hedge_trade(live_tid):
                continue
            live_meta = self.brain.active_trades.get(live_tid)
            notional_f = float(live_t.get("notional_sats", 10000))
            est_rt_fee = notional_f * (2.0 * self.exchange.taker_fee_rate)
            carry_f = float(live_t.get("sum_carry_fees", 0))
            net_unr = float(live_t.get("pl", 0)) - est_rt_fee - carry_f

            if isinstance(live_meta, dict) and hasattr(self.brain, "supervisor") and self.brain.supervisor is not None:
                exc = self.brain.supervisor.update_open_position_excursion(
                    live_tid, net_unr, margin_sats=float(live_t.get("margin", 1000))
                )
                live_meta["mfe_sats"] = exc["mfe_sats"]
                live_meta["mae_sats"] = exc["mae_sats"]

            if isinstance(live_meta, dict):
                act_id = int(live_meta.get("action", 1))
                strat_cfg = self.brain.strategies.get(act_id, {})
                entry_p = max(1.0, float(live_t.get("entry_price", c.close)))
                raw_side = str(live_t.get("side", "b")).lower()
                is_long = raw_side in ("b", "buy", "long")

                # Evaluar EdgeMonitor y Compuerta de Viabilidad Neta en FORCED EDGE EXIT (paridad Etapa 25)
                pair_ctx = self.hedge_store.get_or_create_pair(live_t, orig_meta=live_meta, now=now)
                pair_ctx_ro = dict(pair_ctx)
                pair_ctx_ro["edge_history"] = list(pair_ctx.get("edge_history", []))
                q_val_act = float(pre_q_vec[act_id]) if act_id < len(pre_q_vec) else float(live_meta.get("entry_q", 0.0))
                edge_info = EdgeMonitor.evaluate_position_edge(
                    live_t,
                    pair_ctx_ro,
                    ind_5m,
                    ind_15m,
                    ind_1h,
                    perception=perception,
                    q_val_action=q_val_act,
                    now=now,
                    orig_meta=live_meta,
                )
                edge_score = float(edge_info.get("edge_score", 0.0))
                live_meta["last_edge_score"] = round(edge_score, 4)

                if edge_score <= Config.EDGE_DEGRADATION_EXIT:
                    live_meta["edge_deg_streak"] = int(live_meta.get("edge_deg_streak", 0)) + 1
                elif edge_score <= Config.EDGE_DEGRADATION_WARN:
                    live_meta["edge_deg_streak"] = min(
                        int(live_meta.get("edge_deg_streak", 0)),
                        Config.EDGE_DEGRADATION_PERSISTENCE - 1,
                    )
                else:
                    live_meta["edge_deg_streak"] = 0

                deg_streak = int(live_meta["edge_deg_streak"])
                opened_at_ts = float(live_meta.get("entry_time") or live_t.get("open_ts") or now)
                trade_age_sec = max(0.0, float(now) - opened_at_ts)
                t_margin = max(1000, int(live_t.get("margin") or live_meta.get("margin", 1000)))
                t_lev = max(1.0, float(live_t.get("leverage") or live_meta.get("leverage", 15.0)))
                gross_pl_sats = int(live_t.get("pl") or 0)
                curr_adverse_sats = max(0.0, float(-gross_pl_sats), float(live_meta.get("mae_sats", 0.0)))
                mae_over_margin = curr_adverse_sats / float(max(1, t_margin))
                in_grace_period = (trade_age_sec < 45.0) and (mae_over_margin < 0.045)
                is_hedged_pos = bool(pair_ctx.get("hedge_trade_id")) and (
                    str(pair_ctx.get("hedge_trade_id")) in self.exchange.running_trades
                )

                if (
                    not is_hedged_pos
                    and not in_grace_period
                    and deg_streak >= Config.EDGE_DEGRADATION_PERSISTENCE
                    and edge_score <= Config.EDGE_DEGRADATION_EXIT
                ):
                    pnl_atr = float(edge_info.get("pnl_atr", 0.0))
                    t_notional = t_margin * t_lev
                    rt_fee_sats = max(2, int(round(t_notional * 2.0 * self.exchange.taker_fee_rate)))
                    est_net_sats = gross_pl_sats - rt_fee_sats - int(round(carry_f))
                    min_net_harvest = max(15, int(round(0.25 * rt_fee_sats)))
                    min_physical_loss = max(30, int(round(0.04 * t_margin)))

                    allow_forced_edge_exit = True
                    # Regla 1: Prohibir FORCED EDGE EXIT en los primeros 90s salvo colapso físico severo
                    if trade_age_sec < 90.0 and pnl_atr > -0.85:
                        allow_forced_edge_exit = False
                    # Regla 2: Zona muerta de comisiones (pnl_atr > -0.45)
                    elif pnl_atr > -0.45 and est_net_sats < min_net_harvest:
                        live_meta["edge_deg_streak"] = max(0, int(live_meta.get("edge_deg_streak", 1)) - 1)
                        live_meta["edge_decay_streak"] = live_meta["edge_deg_streak"]
                        allow_forced_edge_exit = False
                    # Regla 3: En zona negativa (pnl_atr <= -0.45), exigir pérdida física real confirmada
                    elif pnl_atr <= -0.45 and gross_pl_sats > -min_physical_loss:
                        allow_forced_edge_exit = False

                    if allow_forced_edge_exit:
                        ok_fe, rec_fe = self.exchange.close_trade(
                            live_tid, price=float(c.close), reason="FORCED_EDGE_EXIT", now=now
                        )
                        if ok_fe and rec_fe:
                            events.append(rec_fe)
                        continue

                rt_fee_rate = (2.0 * self.exchange.taker_fee_rate) + (carry_f / max(1.0, notional_f))

                harvest_atr = float(strat_cfg.get("harvest_atr", 1.15))
                trig_atr = float(strat_cfg.get("trail_trig_atr", 0.75))

                atr_pct = cur_atr / entry_p
                harvest_rate = max(rt_fee_rate * 2.40, atr_pct * harvest_atr)
                trig_rate = max(rt_fee_rate * 1.80, atr_pct * trig_atr * 0.75)
                lock_net_rate = rt_fee_rate * 1.62

                fav_p = float(c.high) if is_long else float(c.low)
                fav_pct = ((fav_p - entry_p) / entry_p) if is_long else ((entry_p - fav_p) / entry_p)

                # A. Cosecha neta de beneficio (Guardian Harvest) cuando la excursión favorable supera objetivo táctico y costos
                if fav_pct >= harvest_rate:
                    harvest_exec_p = (
                        min(float(c.high), round(entry_p * (1.0 + harvest_rate), 1))
                        if is_long
                        else max(float(c.low), round(entry_p * (1.0 - harvest_rate), 1))
                    )
                    ok_h, rec_h = self.exchange.close_trade(
                        live_tid, price=harvest_exec_p, reason="TAKE_PROFIT", now=now
                    )
                    if ok_h and rec_h:
                        events.append(rec_h)
                    continue

                # B. Bloqueo de Stop Loss en ganancia neta (Breakeven + Trailing Lock)
                if fav_pct >= trig_rate:
                    cur_sl = float(live_t.get("stoploss", 0.0))
                    if is_long:
                        trig_p = entry_p * (1.0 + trig_rate)
                        lock_sl = float(math.ceil(entry_p * (1.0 + lock_net_rate) + max(0.0, (fav_p - trig_p) * 0.65)))
                        if float(c.close) <= lock_sl:
                            ok_l, rec_l = self.exchange.close_trade(
                                live_tid, price=lock_sl, reason="TAKE_PROFIT", now=now
                            )
                            if ok_l and rec_l:
                                events.append(rec_l)
                            continue
                        if lock_sl > cur_sl:
                            live_t["stoploss"] = lock_sl
                    else:
                        trig_p = entry_p * (1.0 - trig_rate)
                        lock_sl = float(math.floor(entry_p * (1.0 - lock_net_rate) - max(0.0, (trig_p - fav_p) * 0.65)))
                        if float(c.close) >= lock_sl:
                            ok_l, rec_l = self.exchange.close_trade(
                                live_tid, price=lock_sl, reason="TAKE_PROFIT", now=now
                            )
                            if ok_l and rec_l:
                                events.append(rec_l)
                            continue
                        if cur_sl <= 0.0 or lock_sl < cur_sl:
                            live_t["stoploss"] = lock_sl

        # Cierre defensivo de emergencia si el drawdown flotante supera 5.2% en evaluación
        if not self.explorer_mode and self.exchange.running_trades:
            cur_dd_floating = (
                (self.peak_equity_sats - self.exchange.equity_sats)
                / max(1.0, float(self.peak_equity_sats))
                if self.peak_equity_sats > 0 else 0.0
            )
            if cur_dd_floating >= 0.052:
                for tid in list(self.exchange.running_trades.keys()):
                    ok, rec = self.exchange.close_trade(
                        tid, price=c.close, reason="MAX_DD_GUARD", now=now
                    )
                    if ok and rec:
                        events.append(rec)

        # 2. Despachar liquidaciones a la Q-table (separando patas de cobertura y primarias)
        hedge_to_orig = {}
        for o_id, p_ctx in self.hedge_store.pairs.items():
            if isinstance(p_ctx, dict) and p_ctx.get("hedge_trade_id"):
                hedge_to_orig[str(p_ctx["hedge_trade_id"])] = str(o_id)

        running_count = sum(
            1 for tid_k, tr_v in self.exchange.running_trades.items()
            if not tr_v.get("_is_hedge", False) and not self.hedge_store.is_hedge_trade(tid_k)
        )
        curr_state_key = self.brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, running_count)
        q_vec_curr = self.brain.get_state_q_values(curr_state_key)

        step_net_sats = 0
        # Paso 2A: Consolidar primero eventos de patas de cobertura cerradas (SL/TP/Liquidación)
        for ev in events:
            tid = str(ev["id"])
            if tid in hedge_to_orig:
                orig_id = hedge_to_orig[tid]
                orig_meta = self.brain.active_trades.get(orig_id)
                if isinstance(orig_meta, dict):
                    orig_meta["realized_hedge_net_sats"] = (
                        int(orig_meta.get("realized_hedge_net_sats", 0))
                        + int(ev.get("net_pnl_sats", 0))
                    )
                    orig_meta["realized_hedge_fee_sats"] = (
                        int(orig_meta.get("realized_hedge_fee_sats", 0))
                        + int(ev.get("entry_fee_sats", 0))
                        + int(ev.get("exit_fee_sats", 0))
                        + int(ev.get("carry_fees_sats", 0))
                    )
                if orig_id in self.hedge_store.pairs:
                    self.hedge_store.pairs[orig_id]["hedge_trade_id"] = None

        # Paso 2B: Liquidar operaciones primarias cerradas (consolidando cualquier cobertura huérfana)
        for ev in events:
            tid = str(ev["id"])
            if tid not in hedge_to_orig:
                step_net_sats += self._settle_primary_event(ev, curr_state_key, now, float(c.close))

        # 3. Supervisar coberturas mediante PortfolioHedgeCoordinator + AdaptiveHedgeController + HedgeRiskManager
        pf_snap = PortfolioHedgeCoordinator.reconcile_portfolio(
            list(self.exchange.running_trades.values()),
            self.hedge_store,
            self.exchange.equity_sats,
            float(c.close),
            now=now,
        )
        for t in pf_snap["primary_trades"]:
            if t.get("_is_hedge", False):
                continue
            tid = str(t["id"])
            live_meta = self.brain.active_trades.get(tid, {})
            act_idx = int(live_meta.get("action", 2 if str(t.get("side", "b")).lower() in ("b", "buy", "long") else 6))
            q_val_act = float(q_vec_curr[act_idx]) if act_idx < len(q_vec_curr) else float(live_meta.get("entry_q", 0.0))
            prev_pair = self.hedge_store.pairs.get(tid)
            h_tid = str(prev_pair.get("hedge_trade_id")) if (isinstance(prev_pair, dict) and prev_pair.get("hedge_trade_id")) else None
            hedge_trade_obj = pf_snap["hedge_trades_map"].get(h_tid) if h_tid else None

            hedge_dec = AdaptiveHedgeController.evaluate_and_manage_pair(
                client=self.exchange,
                orig_trade=t,
                hedge_trade=hedge_trade_obj,
                orig_meta=live_meta,
                portfolio_snap=pf_snap,
                hedge_store=self.hedge_store,
                ind_5m=ind_5m,
                ind_15m=ind_15m,
                ind_1h=ind_1h,
                perception=perception,
                q_val_main=q_val_act,
                account_equity_sats=self.exchange.equity_sats,
                available_balance_sats=self.exchange.available_balance_sats,
                locked_floor_sats=getattr(self.brain, "locked_floor", 0),
                latency_degraded=False,
                explorer_mode=self.explorer_mode,
                safe_mode=False,
                learning_enabled=self.learning_enabled,
                now=now,
            )
            exec_info = hedge_dec.get("execution", {})
            sel_act_name = hedge_dec.get("selected_action_name", "NO_HEDGE")
            if exec_info.get("executed"):
                opened_hid = exec_info.get("hedge_trade_id")
                if opened_hid and str(opened_hid) in self.exchange.running_trades:
                    self.exchange.running_trades[str(opened_hid)]["_is_hedge"] = True
                if exec_info.get("action") == "CLOSE_HEDGE" and h_tid:
                    closed_h_rec = next(
                        (r for r in reversed(self.exchange.closed_trades) if str(r.get("id")) == str(h_tid)),
                        None,
                    )
                    if closed_h_rec is not None and isinstance(live_meta, dict):
                        live_meta["realized_hedge_net_sats"] = (
                            int(live_meta.get("realized_hedge_net_sats", 0))
                            + int(closed_h_rec.get("net_pnl_sats", 0))
                        )
                        live_meta["realized_hedge_fee_sats"] = (
                            int(live_meta.get("realized_hedge_fee_sats", 0))
                            + int(closed_h_rec.get("entry_fee_sats", 0))
                            + int(closed_h_rec.get("exit_fee_sats", 0))
                            + int(closed_h_rec.get("carry_fees_sats", 0))
                        )
                if sel_act_name == "CLOSE_ORIGINAL_POSITION":
                    closed_orig_rec = next(
                        (r for r in reversed(self.exchange.closed_trades) if str(r.get("id")) == tid),
                        None,
                    )
                    if closed_orig_rec is not None:
                        step_net_sats += self._settle_primary_event(
                            closed_orig_rec, curr_state_key, now, float(c.close)
                        )

        # 4. Actualizar métrica de Drawdown continuo pico-valle
        cur_eq = self.exchange.equity_sats
        if cur_eq > self.peak_equity_sats:
            self.peak_equity_sats = cur_eq
        elif self.peak_equity_sats > 0:
            cur_dd = (self.peak_equity_sats - cur_eq) / float(self.peak_equity_sats)
            if cur_dd > self.max_seen_drawdown_pct:
                self.max_seen_drawdown_pct = cur_dd

        # 5. Evaluar nuevas aperturas si hay capacidad de concurrencia y cooldown
        max_trades = Config.MAX_CONCURRENT_TRADES
        running_count = sum(
            1 for tid_k, tr_v in self.exchange.running_trades.items()
            if not tr_v.get("_is_hedge", False) and not self.hedge_store.is_hedge_trade(tid_k)
        )
        curr_state_key = self.brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, running_count)
        current_dd = (
            (self.peak_equity_sats - cur_eq) / max(1.0, float(self.peak_equity_sats))
            if self.peak_equity_sats > 0 else 0.0
        )

        # Freno P0 de preservación de capital: si el drawdown del episodio alcanza el 4.5%,
        # bloquear nuevas aperturas (excepto exploración forzada en entrenamiento) para jamás violar el 8% MaxDD.
        dd_throttle_halt = (not self.explorer_mode) and (current_dd >= 0.045)
        rem_bars = len(self.feed.candles_1m) - self.feed.cursor
        min_rem_bars = min(35, max(5, len(self.feed.candles_1m) // 12))

        if not dd_throttle_halt and rem_bars >= min_rem_bars and running_count < max_trades and (now - self.last_entry_ts) >= Config.ENTRY_COOLDOWN_SEC:
            is_exploring = self.learning_enabled and (random.random() < self.brain.epsilon)

            raw_action = self.brain.choose_action(
                curr_state_key, ind_5m, ind_15m, ind_1h, ind_4h,
                recovery_mode=(current_dd >= 0.035), current_dd_pct=current_dd
            )
            q_vec = self.brain.get_state_q_values(curr_state_key)
            entry_dispatched = False

            # Construir orden de evaluación de candidatos alineados con el régimen
            if is_exploring:
                cand_order = list(range(1, 9))
                random.shuffle(cand_order)
            else:
                ranked_by_q = sorted(range(1, 9), key=lambda a: float(q_vec[a]) if a < len(q_vec) else -1.0, reverse=True)
                cand_order = []
                if raw_action > 0:
                    cand_order.append(raw_action)
                for a in ranked_by_q:
                    if a not in cand_order:
                        cand_order.append(a)

            prot = self.floor_guardian.evaluate_protection(
                equity=self.exchange.equity_sats,
                available_balance=self.exchange.available_balance_sats,
                peak_equity=max(self.peak_equity_sats, self.exchange.equity_sats),
                locked_floor=getattr(self.brain, "locked_floor", 0),
                daily_pnl_pct=-current_dd,
            )
            floor_ok = not prot["should_halt_entries"]

            if floor_ok:
                price = float(ind_5m["price"])
                atr = float(ind_5m["atr"])
                mods = self.brain.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)
                mkt_reg_tag = perception.get("market_regime", ind_5m.get("regime", "RANGE_NORMAL"))
                reg_adapt = SatsExpectancyGate.compute_regime_risk_adaptation(
                    mkt_reg_tag, current_dd_pct=current_dd, recovery_mode=(current_dd >= 0.035)
                )
                fee_rt = THRESHOLDS_MANAGER.get_float("fees", "round_trip_fee_rate", 0.0020)
                kelly_scale = THRESHOLDS_MANAGER.get_float("q_calibration", "fractional_kelly_scale", 0.25)
                dd_dampener = max(0.25, 1.0 - (current_dd / 0.06) ** 2) if current_dd > 0 else 1.0
                base_risk_pct = (0.015 if self.explorer_mode else 0.012) * dd_dampener
                auto_adj = self.brain.supervisor.get_autonomous_adjustments() if hasattr(self.brain, "supervisor") and self.brain.supervisor else {"expectancy_boost_applied": 0.0}
                vel_ema = float(perception.get("vel_ema_atr", 0.0)) if isinstance(perception, dict) else 0.0

                evaluated_acts = set()
                for cand_a in cand_order:
                    action, _ = SatsExpectancyGate.filter_and_rank_action(
                        cand_a, curr_state_key, q_vec, self.brain.strategies, perception,
                        recovery_mode=(current_dd >= 0.035), current_dd_pct=current_dd,
                        explorer_mode=False,
                    )
                    if action <= 0 or action in evaluated_acts or action not in self.brain.strategies:
                        continue
                    evaluated_acts.add(action)

                    if not self.explorer_mode:
                        q_val = float(q_vec[action]) if action < len(q_vec) else 0.0
                        if q_val <= 0.0:
                            continue

                    strat = self.brain.strategies[action]
                    side = strat["side"]

                    # Filtro de confluencia microestructural: exigir vela de confirmación y evitar centro de rango sin dirección
                    if side == "buy":
                        if float(c.close) <= float(c.open):
                            continue
                        if "|MID|" in curr_state_key and "|CHOP|" in curr_state_key and float(ind_5m["ema9"]) <= float(ind_5m["ema21"]):
                            continue
                        if vel_ema < -0.15:
                            continue
                    else:
                        if float(c.close) >= float(c.open):
                            continue
                        if "|MID|" in curr_state_key and "|CHOP|" in curr_state_key and float(ind_5m["ema9"]) >= float(ind_5m["ema21"]):
                            continue
                        if vel_ema > 0.15:
                            continue

                    strat_style = strat.get("style", "SNIPER")
                    eff_rr_mult = max(1.12, float(mods.get("rr_mult", 1.0)))
                    lev, sl, tp = calculate_strategy_order_params(
                        strat, price, atr, rr_mult=eff_rr_mult
                    )

                    q_val_entry = float(q_vec[action]) if action < len(q_vec) else 0.0
                    st_visits_entry = int(self.brain.state_visits.get(curr_state_key, 0))
                    p_hat, p_lcb, _ = SatsExpectancyGate.estimate_bayesian_win_rate(
                        curr_state_key,
                        action,
                        strat_style,
                        mkt_reg_tag,
                        self.brain.style_metrics,
                        q_val=q_val_entry,
                        state_visits=st_visits_entry,
                    )
                    sl_dist_pct = abs(price - sl) / max(price, 1.0)
                    tp_dist_pct = abs(tp - price) / max(price, 1.0)
                    b_ratio = max(0.2, (tp_dist_pct - fee_rt) / max(0.0001, sl_dist_pct + fee_rt))
                    raw_kelly = max(0.0, (p_hat * b_ratio - (1.0 - p_hat)) / b_ratio)
                    kelly_mult = float(np.clip(0.70 + (raw_kelly * kelly_scale * 3.0), 0.55, 1.40))

                    eff_risk_pct = base_risk_pct * kelly_mult * reg_adapt["size_mult"]
                    risk_budget_sats = max(120, int(round(self.exchange.equity_sats * eff_risk_pct)))

                    loss_per_margin_sat = float(lev) * (sl_dist_pct + fee_rt)
                    if loss_per_margin_sat > 0.0001:
                        derived_margin = int(round(risk_budget_sats / loss_per_margin_sat))
                    else:
                        derived_margin = int(round(self.exchange.equity_sats * 0.02))

                    max_single_margin = int(round(self.exchange.equity_sats * Config.MAX_SINGLE_TRADE_MARGIN_PCT))
                    margin_sats = max(Config.MIN_MARGIN_SATS, min(max_single_margin, derived_margin))

                    viable, _ = SatsExpectancyGate.verify_net_sats_expectancy(
                        price=price,
                        margin_sats=margin_sats,
                        lev=lev,
                        sl_price=sl,
                        tp_price=tp,
                        explorer_mode=self.explorer_mode,
                        slippage_bps=float(self.exchange.default_slippage_bps),
                        expectancy_boost=auto_adj.get("expectancy_boost_applied", 0.0),
                        state=curr_state_key,
                        action=action,
                        style=strat_style,
                        regime=mkt_reg_tag,
                        style_metrics=self.brain.style_metrics,
                        q_val=q_val_entry,
                        state_visits=st_visits_entry,
                    )

                    if viable and margin_sats <= self.exchange.available_balance_sats:
                        try:
                            trade = self.exchange.open_trade(
                                side=side,
                                margin=margin_sats,
                                leverage=lev,
                                stoploss=sl,
                                takeprofit=tp,
                                price=price,
                                atr=atr,
                                now=now,
                            )
                            tid = trade["id"]
                            self.brain.active_trades[tid] = {
                                "action": action,
                                "style": strat["style"],
                                "margin": margin_sats,
                                "leverage": lev,
                                "state": curr_state_key,
                                "entry_time": now,
                                "entry_q": float(q_vec[action]) if action < len(q_vec) else 0.0,
                                "is_exploratory": bool(is_exploring or getattr(self.brain, "_last_decision_exploratory", False)),
                                "edge_deg_streak": 0,
                            }
                            self.last_entry_ts = now
                            entry_dispatched = True
                            break
                        except Exception:
                            pass

            if raw_action > 0 and not entry_dispatched and hasattr(self.brain, "supervisor") and self.brain.supervisor is not None:
                self.brain.supervisor.discard_last_action_decision()

        done = (self.exchange.balance_sats <= Config.MIN_MARGIN_SATS * 2) or not self.feed.has_next()
        info = {
            "step": self.step_count,
            "balance_sats": self.exchange.balance_sats,
            "equity_sats": self.exchange.equity_sats,
            "running_trades": len(self.exchange.running_trades),
            "total_settled": self.total_settled_trades,
            "win_rate": (self.winning_trades / max(1, self.total_settled_trades)) * 100.0,
            "total_net_pnl_sats": self.total_net_pnl_sats,
        }
        return mkt_state, step_net_sats, done, info

    def run_episode(self, candles_1m: List[Candle], initial_balance: Optional[int] = None) -> dict:
        """
        Ejecuta un episodio completo hasta agotar las velas o alcanzar quiebra.
        Retorna el resumen de desempeño del episodio.
        """
        self.reset(candles_1m, initial_balance_sats=initial_balance)
        done = False
        last_info = {}
        while not done:
            _, _, done, last_info = self.step()

        # Si quedaron posiciones vivas al agotar las velas, cerrarlas a precio de mercado de la última vela
        if self.exchange.running_trades:
            final_price = self.exchange.last_price
            final_ts = self.exchange.last_ts
            hedge_to_orig_end = {}
            for o_id, p_ctx in list(self.hedge_store.pairs.items()):
                if isinstance(p_ctx, dict) and p_ctx.get("hedge_trade_id"):
                    hedge_to_orig_end[str(p_ctx["hedge_trade_id"])] = str(o_id)

            # 1. Cerrar primero patas de cobertura vivas y consolidarlas en sus primarias
            for tid in list(self.exchange.running_trades.keys()):
                tr_obj = self.exchange.running_trades.get(tid)
                if tr_obj and (tr_obj.get("_is_hedge", False) or str(tid) in hedge_to_orig_end):
                    ok_h, rec_h = self.exchange.close_trade(
                        tid, price=final_price, reason="EPISODE_END_HEDGE", now=final_ts
                    )
                    if ok_h and rec_h:
                        orig_id = hedge_to_orig_end.get(str(tid))
                        orig_meta = self.brain.active_trades.get(orig_id) if orig_id else None
                        if isinstance(orig_meta, dict):
                            orig_meta["realized_hedge_net_sats"] = (
                                int(orig_meta.get("realized_hedge_net_sats", 0))
                                + int(rec_h.get("net_pnl_sats", 0))
                            )
                            orig_meta["realized_hedge_fee_sats"] = (
                                int(orig_meta.get("realized_hedge_fee_sats", 0))
                                + int(rec_h.get("entry_fee_sats", 0))
                                + int(rec_h.get("exit_fee_sats", 0))
                                + int(rec_h.get("carry_fees_sats", 0))
                            )
                        if orig_id and orig_id in self.hedge_store.pairs:
                            self.hedge_store.pairs[orig_id]["hedge_trade_id"] = None

            # 2. Cerrar primarias remanentes pasando métricas completas (MFE/MAE, ticks, predicted_q_entry)
            for tid in list(self.exchange.running_trades.keys()):
                ok, rec = self.exchange.close_trade(tid, price=final_price, reason="EPISODE_END", now=final_ts)
                if ok and rec:
                    orig_meta_peek = self.brain.active_trades.get(str(tid))
                    fallback_st = (
                        orig_meta_peek.get("state", "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS")
                        if isinstance(orig_meta_peek, dict)
                        else "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS"
                    )
                    self._settle_primary_event(rec, fallback_st, final_ts, final_price)

        # Guardar cerebro candidato atómicamente con verificación SHA-256 solo si learning_enabled
        if self.learning_enabled:
            self.save_candidate_brains()
        return {
            "total_steps": self.step_count,
            "initial_balance_sats": self.initial_balance_sats,
            "final_balance_sats": self.exchange.balance_sats,
            "final_equity_sats": self.exchange.equity_sats,
            "net_profit_sats": self.exchange.balance_sats - self.initial_balance_sats,
            "total_settled_trades": self.total_settled_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round((self.winning_trades / max(1, self.total_settled_trades)) * 100.0, 2),
            "max_drawdown_pct": round(self.max_seen_drawdown_pct * 100.0, 2),
            "total_fees_paid_sats": self.exchange.total_fees_paid_sats,
            "total_funding_paid_sats": self.exchange.total_funding_paid_sats,
            "epsilon_final": round(self.brain.epsilon, 4),
            "total_trained_transitions": self.brain.total_trained,
            "closed_trades": list(self.exchange.closed_trades),
        }

    def save_candidate_brains(self) -> Tuple[str, str]:
        """Guarda y sella criptográficamente los cerebros candidatos solo si learning_enabled."""
        if not self.learning_enabled:
            return "", ""
        self.brain.save_brain(force=True)
        d_hedge = self.hedge_store.save(force=True)
        import hashlib
        d_main = ""
        if os.path.exists(self.candidate_brain_file):
            with open(self.candidate_brain_file, "rb") as f:
                d_main = hashlib.sha256(f.read()).hexdigest()
        return str(d_main), str(d_hedge)
