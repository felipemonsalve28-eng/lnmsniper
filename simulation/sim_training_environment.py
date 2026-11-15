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

        return self.feed.get_perception_state()

    def step(self) -> Tuple[Optional[dict], int, bool, dict]:
        """
        Avanza un paso de simulación (1 vela de 1 minuto):
        1. Alimenta la vela al feed y al exchange.
        2. Procesa cierres, stop losses y liquidaciones.
        3. Entrena la Q-table del cerebro con las operaciones concluidas (Schema v2).
        4. Supervisa coberturas mediante AdaptiveHedgeController.
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

        # Actualizar excursiones MFE/MAE y ejecutar Guardián de bloqueo de ganancias / cosecha (homólogo a producción)
        for live_tid, live_t in list(self.exchange.running_trades.items()):
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
                is_long = str(live_t.get("side", "b")).startswith("b")
                rt_fee_rate = (2.0 * self.exchange.taker_fee_rate) + (carry_f / max(1.0, notional_f))

                harvest_atr = float(strat_cfg.get("harvest_atr", 1.15))
                trail_trig_atr = float(strat_cfg.get("trail_trig_atr", 0.75))

                atr_pct = cur_atr / entry_p
                harvest_rate = max(rt_fee_rate * 2.40, atr_pct * harvest_atr)
                trig_rate = max(rt_fee_rate * 1.80, atr_pct * trail_trig_atr * 0.75)
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

        # 2. Despachar liquidaciones a la Q-table (remember_and_train con Schema v2 solo si learning_enabled)
        running_count = len(self.exchange.running_trades)
        curr_state_key = self.brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, running_count)

        step_net_sats = 0
        for ev in events:
            tid = ev["id"]
            orig_info = self.brain.active_trades.pop(tid, None)
            if orig_info is not None:
                orig_st = orig_info.get("state", curr_state_key)
                act_taken = int(orig_info.get("action", 1))
                net_pnl = int(ev.get("net_pnl_sats", 0))
                margin_used = int(orig_info.get("margin", 1000))
                lev_used = float(orig_info.get("leverage", 10))
                hit_tp = bool(ev.get("close_reason") == "TAKE_PROFIT")
                dur_ticks = max(1.0, float(ev.get("duration_s", 60.0)) / 60.0)
                mae_tracked = float(orig_info.get("mae_sats", max(0, -net_pnl)))
                mfe_tracked = float(orig_info.get("mfe_sats", max(0, net_pnl)))

                self.total_settled_trades += 1
                step_net_sats += net_pnl
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
                        harvested=False,
                        recovery_mode=False,
                        lev_used=lev_used,
                        exact_fee_sats=int(ev.get("entry_fee_sats", 0) + ev.get("exit_fee_sats", 0)),
                        pl_includes_fees=True,
                        funding_fee_sats=int(ev.get("carry_fees_sats", 0)),
                        trade_id=tid,
                        mae_sats=mae_tracked,
                        mfe_sats=mfe_tracked,
                        duration_ticks=dur_ticks,
                        predicted_q_entry=orig_info.get("entry_q", None),
                    )

        # 3. Supervisar coberturas mediante AdaptiveHedgeController
        for tid, t in list(self.exchange.running_trades.items()):
            if t.get("_is_hedge", False):
                continue
            pair_ctx = self.hedge_store.get_or_create_pair(t, now=now)
            prev_net_pl = int(pair_ctx.get("last_combined_net_sats", t.get("pl", 0)))
            pair_econ = {
                "combined_net_pl_sats": int(t.get("pl", 0)),
                "has_active_hedge": bool(pair_ctx.get("hedge_trade_id")),
                "effective_hedge_ratio": float(pair_ctx.get("hedge_ratio", 0.0)),
                "hedge_pl_sats": 0,
                "orig_pl_sats": int(t.get("pl", 0)),
            }
            edge_info = EdgeMonitor.evaluate_position_edge(
                t, pair_ctx, ind_5m, ind_15m, ind_1h, perception=perception, q_val_action=0.0, now=now
            )
            pred_sig = PredictiveHedgeEngine.forecast_adverse_risk(
                edge_info, ind_5m, ind_15m, ind_1h, perception=perception
            )
            state_fsm = self.hedge_controller._transition_state_with_hysteresis(
                pair_ctx, pair_econ, edge_info, pred_sig, now
            )
            h_key = self.hedge_controller.discretize_hedge_state(
                edge_info["market_regime"],
                state_fsm,
                edge_info["pnl_atr"],
                edge_info["edge_score"],
                pair_econ["effective_hedge_ratio"],
            )
            prev_h_key = pair_ctx.get("last_hedge_state_key")
            prev_h_act = int(pair_ctx.get("last_hedge_action", 0))
            is_exit_transition = ("HEDGE_EXIT" in h_key) or (state_fsm == "HEDGE_EXIT")
            if self.learning_enabled and prev_h_key is not None:
                r_h = self.hedge_controller.compute_hedge_reward(
                    prev_net_pl,
                    pair_econ["combined_net_pl_sats"],
                    prev_h_act,
                    fee_paid_sats=0,
                    margin_sats=int(t.get("margin", 1000)),
                )
                self.hedge_store.update_hedge_q(
                    prev_h_key, prev_h_act, r_h, h_key, done=is_exit_transition
                )
            pair_ctx["last_hedge_state_key"] = None if (is_exit_transition and not pair_econ["has_active_hedge"]) else h_key
            pair_ctx["last_combined_net_sats"] = pair_econ["combined_net_pl_sats"]

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

                    p_hat, p_lcb, _ = SatsExpectancyGate.estimate_bayesian_win_rate(
                        curr_state_key, action, strat_style, mkt_reg_tag, self.brain.style_metrics
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
                            }
                            self.last_entry_ts = now
                            break
                        except Exception:
                            pass

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
            for tid in list(self.exchange.running_trades.keys()):
                ok, rec = self.exchange.close_trade(tid, price=final_price, reason="EPISODE_END", now=final_ts)
                if ok and rec:
                    self.total_settled_trades += 1
                    net_pnl = rec["net_pnl_sats"]
                    self.total_net_pnl_sats += net_pnl
                    if net_pnl > 0:
                        self.winning_trades += 1
                    else:
                        self.losing_trades += 1
                    if self.learning_enabled:
                        orig_info = self.brain.active_trades.pop(tid, None)
                        if orig_info:
                            orig_st = orig_info.get("state", "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS")
                            act_taken = int(orig_info.get("action", 1))
                            margin_used = int(orig_info.get("margin", 1000))
                            lev_used = float(orig_info.get("leverage", 10))
                            self.brain.remember_and_train(
                                orig_st,
                                act_taken,
                                reward_sats=net_pnl,
                                margin_used=margin_used,
                                next_state=orig_st,
                                equity_after=self.exchange.equity_sats,
                                available_balance_after=self.exchange.available_balance_sats,
                                real_balance_delta_sats=net_pnl,
                                hit_tp=(net_pnl > 0),
                                harvested=False,
                                recovery_mode=False,
                                lev_used=lev_used,
                                exact_fee_sats=int(rec.get("entry_fee_sats", 0) + rec.get("exit_fee_sats", 0)),
                                pl_includes_fees=True,
                                funding_fee_sats=int(rec.get("carry_fees_sats", 0)),
                                trade_id=tid,
                            )

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
