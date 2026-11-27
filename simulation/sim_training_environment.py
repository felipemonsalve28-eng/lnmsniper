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
from simulation.sim_lnm_exchange import MicrostructureStressConfig, SimLNMExchange
from lnm_sniper_v6_advanced import (
    AutonomousGoalBrain,
    Config,
    THRESHOLDS_MANAGER,
    calculate_strategy_order_params,
    compute_live_edge,
)
from learning_thresholds_manager import get_thresholds_manager
from hedge_state_store import HedgeStateStore
from adaptive_controller import AdaptiveHedgeController
from edge_monitor import EdgeMonitor
from portfolio_hedge import PortfolioHedgeCoordinator
from predictive_hedge import PredictiveHedgeEngine
from guardian_execution_core import HardFloorGuardian, compat_np as np
from sniper_perception_sats_core import SatsExpectancyGate, MarketRegimeClassifier
from liquidity_radar import (
    DeterministicMTFAggregator,
    LiquidityConfig,
    LiquidityRadar,
    compute_liquidity_bias,
    compute_opposite_pool_room,
    compute_structural_td9_trailing_stop,
)
from limit_execution_manager import (
    LimitExecutionConfig,
    LimitExecutionManager,
    should_route_limit,
    compute_limit_entry_plan,
)
from side_streak_brake import SideStreakBrakeStore


class PrecomputedMarketFeed:
    """Alimentador O(1) sobre bloques precomputados (warmup_state + steps) para validación OOS."""

    def __init__(self, block: Dict[str, Any], liquidity_config_override: Optional[LiquidityConfig] = None):
        self._steps = block["steps"]
        self._warmup_bars = int(block["warmup_bars"])
        self._total_len = int(block["total_len_with_warmup"])
        self.candles_1m = [None] * self._total_len
        self.cursor = self._warmup_bars
        self.last_candle = Candle(
            timestamp=float(block["warmup_last_ts"]),
            open=0.0,
            high=0.0,
            low=0.0,
            close=0.0,
            volume=0.0,
        )
        self.warmup_state = block["warmup_state"]
        self._cur_state = self.warmup_state
        self._idx = 0
        self.liquidity_config_override = liquidity_config_override
        self.ram_aggregator = DeterministicMTFAggregator(max_history=300)
        self.liquidity_radar = LiquidityRadar(liquidity_config_override)

    def load_candles(self, candles: Any) -> None:
        pass

    def warmup(self, warmup_bars: int = 80) -> int:
        return self._warmup_bars

    def has_next(self) -> bool:
        return self.cursor < self._total_len

    def next_step(self) -> Optional[Candle]:
        if self.cursor >= self._total_len or self._idx >= len(self._steps):
            return None
        c, ind_5m, ind_15m, ind_1h, ind_4h, perception = self._steps[self._idx]
        self._idx += 1
        self.cursor += 1
        self.last_candle = c
        self.ram_aggregator.push_1m(
            c.timestamp, c.open, c.high, c.low, c.close, getattr(c, "volume", 0.0), is_closed=True
        )
        self._cur_state = {
            "price": float(ind_5m["price"]),
            "ind_5m": ind_5m,
            "ind_15m": ind_15m,
            "ind_1h": ind_1h,
            "ind_4h": ind_4h,
            "perception": perception,
            "timestamp": float(c.timestamp),
        }
        return c

    def get_perception_state(self) -> dict:
        return self._cur_state

    def get_ram_candles(self, tf: str, include_forming: bool = True) -> Tuple[Any, Any, Any]:
        return self.ram_aggregator.get_hlc_arrays(tf, include_forming=include_forming)

    def get_ram_ohlcv(self, tf: str, include_forming: bool = True) -> Tuple[Any, Any, Any, Any, Any, float]:
        return self.ram_aggregator.get_ohlcv(tf, include_forming=include_forming)

    def get_session_vwap(self) -> Dict[str, Any]:
        return self.ram_aggregator.get_session_vwap()

    def get_td9_state(self) -> Dict[str, Dict[str, Any]]:
        return self.ram_aggregator.get_td9_state()

    def get_structural_context(
        self, atr_ref: Optional[float] = None, spot_price: Optional[float] = None
    ) -> Dict[str, Any]:
        liq_cfg = self.liquidity_config_override or LiquidityConfig.from_thresholds(get_thresholds_manager())
        spot = float(spot_price if spot_price is not None else self._cur_state.get("price", 0.0))
        ind_15 = self._cur_state.get("ind_15m") or self._cur_state.get("ind_5m") or {}
        atr_val = float(atr_ref) if atr_ref is not None else max(1.0, float(ind_15.get("atr") or 1.0))
        tf_ohlcv = {}
        for _tf in ("15m", "30m", "45m", "1h", "4h"):
            _o, _h, _l, _c, _v, _ts = self.ram_aggregator.get_ohlcv(_tf, include_forming=True)
            if len(_c) > 0:
                tf_ohlcv[_tf] = (_h, _l, _c, _ts)
        hierarchy = self.liquidity_radar.evaluate_multi_tf_hierarchy(
            tf_ohlcv=tf_ohlcv,
            atr_ref=atr_val,
            spot_price=spot,
            config_override=liq_cfg,
        )
        return {
            "hierarchy": hierarchy,
            "td9": self.ram_aggregator.get_td9_state(),
            "session_vwap": self.ram_aggregator.get_session_vwap(),
        }


class SimTrainingEnvironment:
    """
    Entorno coordinado de alta velocidad para entrenamiento RL offline y certificación OOS.
    Ejecuta el bucle completo de trading (percepción, dimensionamiento Kelly,
    política determinista C1_STREAK3_24H_INV o selección greedy/explorer, y aprendizaje de Bellman).
    """

    def __init__(
        self,
        candidate_brain_file: str = "q_brain_simulated_candidate.json",
        candidate_hedge_file: str = "hedge_state_simulated_candidate.json",
        initial_balance_sats: int = 100000,
        explorer_mode: bool = False,
        learning_enabled: Optional[bool] = None,
        taker_fee_rate: float = 0.0010,
        funding_rate_8h: float = 0.0001,
        default_slippage_bps: float = 1.5,
        inverse_contract_math: bool = True,
        fe_off_trend: Optional[bool] = None,
        trend_sl_floor_pct: float = 0.0065,
        short_exp_phys_hold: bool = True,
        streak3_24h: bool = True,
        deterministic_trend_policy: Optional[bool] = None,
        base_risk_pct_std: float = 0.0075,
        be_stage1_r: float = 0.80,
        structural_td9_trailing_enabled: bool = False,
        stress_config: Optional[MicrostructureStressConfig] = None,
    ):
        self.candidate_brain_file = str(candidate_brain_file)
        self.candidate_hedge_file = str(candidate_hedge_file)
        self.initial_balance_sats = int(initial_balance_sats)
        self.explorer_mode = bool(explorer_mode)
        self.learning_enabled = bool(self.explorer_mode) if learning_enabled is None else bool(learning_enabled)
        self.inverse_contract_math = bool(inverse_contract_math)
        self.deterministic_trend_policy = (
            (not self.explorer_mode and not self.learning_enabled)
            if deterministic_trend_policy is None
            else bool(deterministic_trend_policy)
        )
        self.fe_off_trend = (not self.learning_enabled) if fe_off_trend is None else bool(fe_off_trend)
        self.trend_sl_floor_pct = float(trend_sl_floor_pct)
        self.short_exp_phys_hold = bool(short_exp_phys_hold)
        self.streak3_24h = bool(streak3_24h)
        self.base_risk_pct_std = float(base_risk_pct_std)
        self.be_stage1_r = float(be_stage1_r)
        self.structural_td9_trailing_enabled = bool(structural_td9_trailing_enabled)
        self.stress_config: Optional[MicrostructureStressConfig] = stress_config
        self.side_brake = SideStreakBrakeStore(
            env_mode="sim",
            persist_to_disk=False,
            enabled=self.streak3_24h,
        )

        self.clock = SimClock(start_timestamp=1700000000.0)
        self.feed: Any = SimMarketFeed()
        self.exchange = SimLNMExchange(
            initial_balance_sats=self.initial_balance_sats,
            taker_fee_rate=taker_fee_rate,
            funding_rate_8h=funding_rate_8h,
            default_slippage_bps=default_slippage_bps,
            inverse_contract_math=self.inverse_contract_math,
            stress_config=self.stress_config,
        )

        # Cerebros estrictamente aislados (nunca tocan los archivos de producción)
        self.brain = AutonomousGoalBrain(
            filename=self.candidate_brain_file,
            explorer_mode=self.explorer_mode,
            learning_enabled=self.learning_enabled,
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
        self.last_side_entry_ts: Dict[str, float] = {"b": -1e18, "s": -1e18}
        self.dd_guard_events: int = 0
        self.step_count: int = 0
        self.total_settled_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_net_pnl_sats: int = 0
        self.total_fees_paid_sats: int = 0
        self.peak_equity_sats: int = self.initial_balance_sats
        self.max_seen_drawdown_pct: float = 0.0
        self.limit_config_override: Optional[LimitExecutionConfig] = None
        self.limit_manager = LimitExecutionManager(
            env_mode="testnet",
            base_dir=".",
            max_concurrent_trades=Config.MAX_CONCURRENT_TRADES,
            persist_to_disk=False,
        )

    def _seed_canonical_states_if_needed(self) -> None:
        """Siembra los 175 estados canónicos si el cerebro candidato es nuevo y garantiza semilla limpia en modo determinista."""
        if len(self.brain.q_table) >= 100:
            if self.deterministic_trend_policy:
                for k in list(self.brain.q_table.keys()):
                    self.brain.q_table[k] = self.brain._init_q_vector(k)
                    self.brain.state_visits[k] = 1
            return

        init_v = 1 if self.deterministic_trend_policy else 0
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
                            self.brain.state_visits[norm_k] = init_v
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
                                self.brain.state_visits[norm_s] = init_v
        self.brain.dirty = True

    @staticmethod
    def _norm_side(side: str) -> str:
        return "b" if str(side).lower().strip() in ("b", "buy", "long") else "s"

    def _check_anti_stacking_s2(self, side: str, price: float, atr: float, now: float) -> Tuple[bool, str]:
        cand_side = self._norm_side(side)
        atr_safe = max(1.0, float(atr))
        same_all = []

        for tid_k, tr_v in self.exchange.running_trades.items():
            if tr_v.get("_is_hedge", False) or self.hedge_store.is_hedge_trade(tid_k):
                continue
            if self._norm_side(tr_v.get("side", "")) != cand_side:
                continue
            meta = self.brain.active_trades.get(str(tid_k), {})
            same_all.append({
                "sl_locked": bool(meta.get("sl_locked", False)),
                "entry_price": float(tr_v.get("entry_price") or tr_v.get("price") or price),
                "open_ts": float(meta.get("entry_time") or tr_v.get("open_ts") or 0.0),
            })

        for rec in self.limit_manager.pending.values():
            if rec.get("status") not in ("PENDING_SUBMIT", "OPEN_WAITING"):
                continue
            if self._norm_side(rec.get("side", "")) != cand_side:
                continue
            same_all.append({
                "sl_locked": False,
                "entry_price": float(rec.get("limit_price") or price),
                "open_ts": float(rec.get("created_at") or 0.0),
            })

        at_risk = sum(1 for x in same_all if not x["sl_locked"])
        if at_risk >= 2:
            return False, "atrisk>=2"
        if same_all:
            min_dist = min(abs(float(price) - x["entry_price"]) for x in same_all)
            last_ts = max([self.last_side_entry_ts.get(cand_side, -1e18)] + [x["open_ts"] for x in same_all])
            elapsed = float(now) - float(last_ts)
            if (min_dist < (0.75 * atr_safe)) and (elapsed < 900.0):
                return False, "dist<0.75ATR_and_cd<900s"
        return True, "OK"

    def reset(
        self,
        candles_1m: Any,
        initial_balance_sats: Optional[int] = None,
        warmup_bars: int = 80,
    ) -> dict:
        """
        Reinicia el entorno con un nuevo lote de velas (o bloque precomputado) y balance inicial.
        """
        if initial_balance_sats is not None:
            self.initial_balance_sats = int(initial_balance_sats)

        prev_counter = getattr(self.exchange, "trade_counter", 1000)
        self.exchange = SimLNMExchange(
            initial_balance_sats=self.initial_balance_sats,
            taker_fee_rate=self.exchange.taker_fee_rate,
            funding_rate_8h=self.exchange.funding_rate_8h,
            default_slippage_bps=self.exchange.default_slippage_bps,
            inverse_contract_math=self.inverse_contract_math,
            stress_config=self.stress_config,
        )
        self.exchange.trade_counter = prev_counter
        self.side_brake.reset(persist=False)

        liq_ov = getattr(self.feed, "liquidity_config_override", None)
        if isinstance(candles_1m, dict) and candles_1m.get("_precomputed"):
            self.feed = PrecomputedMarketFeed(candles_1m, liquidity_config_override=liq_ov)
            start_ts = float(candles_1m["warmup_last_ts"])
            self.clock.set_time(start_ts)
            init_state = self.feed.warmup_state
        else:
            if not isinstance(self.feed, SimMarketFeed):
                self.feed = SimMarketFeed()
                self.feed.liquidity_config_override = liq_ov
            self.feed.load_candles(candles_1m)
            self.feed.warmup(warmup_bars)
            start_ts = float(self.feed.last_candle.timestamp if self.feed.last_candle else 1700000000.0)
            self.clock.set_time(start_ts)
            init_state = self.feed.get_perception_state()

        self.last_entry_ts = 0.0
        self.last_side_entry_ts = {"b": -1e18, "s": -1e18}
        self.dd_guard_events = 0
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
        self.limit_manager = LimitExecutionManager(
            env_mode="testnet",
            base_dir=".",
            max_concurrent_trades=Config.MAX_CONCURRENT_TRADES,
            persist_to_disk=False,
        )

        return init_state

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

        if self.streak3_24h and hasattr(self, "side_brake") and self.side_brake is not None:
            self.side_brake.record_trade_close(
                side=str(ev.get("side", "b")),
                net_pnl_sats=net_pnl,
                close_ts=float(ev.get("close_ts", now)),
            )

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
        _stress_active = bool(self.stress_config is not None and self.stress_config.enabled)
        if _stress_active:
            mkt_state = self.feed.get_perception_state()
            ind_5m = mkt_state["ind_5m"]
            cur_atr = max(1.0, float(ind_5m.get("atr", c.close * 0.002)))
            events = self.exchange.step_candle(c, atr_5m=cur_atr)
        else:
            events = self.exchange.step_candle(c)
            mkt_state = self.feed.get_perception_state()
            ind_5m = mkt_state["ind_5m"]
            cur_atr = max(1.0, float(ind_5m.get("atr", c.close * 0.002)))
        ind_15m = mkt_state["ind_15m"]
        ind_1h = mkt_state["ind_1h"]
        ind_4h = mkt_state["ind_4h"]
        perception = mkt_state["perception"]

        # 1b. Reconciliar órdenes límite pasivas (incluye fills cerrados pesimistamente en la misma vela 1m)
        if self.limit_manager.pending:
            _tm_rec = getattr(self, "thresholds", None) or getattr(self.brain, "thresholds", None) or THRESHOLDS_MANAGER
            _lim_cfg_rec = self.limit_config_override or LimitExecutionConfig.from_thresholds(_tm_rec)
            _combined_trades = list(self.exchange.running_trades.values()) + list(events)
            _rec_res = self.limit_manager.reconcile_open_orders(
                client=self.exchange,
                running_trades=_combined_trades,
                spot_price=float(c.close),
                perception=perception,
                cfg=_lim_cfg_rec,
                force_cancel_all=False,
                now=now,
            )
            for _f_ev in _rec_res.get("filled", []):
                _tr_f = _f_ev.get("trade") or {}
                _mt_f = _f_ev.get("meta") or {}
                _fid = str(_tr_f.get("id") or "")
                if _fid and _fid not in self.brain.active_trades:
                    self.brain.active_trades[_fid] = {
                        "action": int(_mt_f.get("action", 1)),
                        "style": str(_mt_f.get("style", "SNIPER")),
                        "margin": int(_mt_f.get("margin_sats", _tr_f.get("margin", 0))),
                        "leverage": float(_mt_f.get("leverage", _tr_f.get("leverage", 1))),
                        "state": str(_mt_f.get("state_key", "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS")),
                        "entry_time": float(_tr_f.get("open_ts", now)),
                        "entry_q": float(_mt_f.get("entry_q", 0.0)),
                        "is_exploratory": bool(_mt_f.get("is_exploratory", False)),
                        "edge_deg_streak": 0,
                        "entry_type": "limit",
                    }

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
                    if self.fe_off_trend and (act_id in (1, 5) or strat_cfg.get("style") == "TREND"):
                        allow_forced_edge_exit = False
                    # Regla 1: Prohibir FORCED EDGE EXIT en los primeros 90s salvo colapso físico severo
                    elif trade_age_sec < 90.0 and pnl_atr > -0.85:
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

                if self.deterministic_trend_policy:
                    if "initial_sl_dist_pct" not in live_meta:
                        _sl_def = float(live_t.get("stoploss", 0.0))
                        live_meta["initial_sl_dist_pct"] = (
                            max(0.0020, abs(entry_p - _sl_def) / entry_p) if _sl_def > 0.0 else 0.0040
                        )
                        live_meta.setdefault("sl_locked", False)
                        live_meta.setdefault("sl_is_trailing_lock", False)
                        live_meta.setdefault("trail_stage2", False)
                    init_sl_pct = float(live_meta["initial_sl_dist_pct"])
                    close_pct = ((float(c.close) - entry_p) / entry_p) if is_long else ((entry_p - float(c.close)) / entry_p)
                    pnl_atr_close = (
                        ((float(c.close) - entry_p) if is_long else (entry_p - float(c.close)))
                        / max(1.0, cur_atr)
                    )
                    prev_pnl_atr = float(live_meta.get("prev_loop_pnl_atr", pnl_atr_close))
                    delta_atr = pnl_atr_close - prev_pnl_atr
                    live_meta["prev_loop_pnl_atr"] = round(pnl_atr_close, 4)
                    eval_count = int(live_meta.get("eval_count", 0)) + 1
                    live_meta["eval_count"] = eval_count

                    fee_offset_px = entry_p * ((2.4 * self.exchange.taker_fee_rate) + (carry_f / max(1.0, notional_f)))
                    cur_sl_now = float(live_t.get("stoploss", 0.0))
                    sl_locked_server = (
                        (is_long and cur_sl_now > (entry_p + fee_offset_px * 0.8))
                        or ((not is_long) and cur_sl_now > 0.0 and cur_sl_now < (entry_p - fee_offset_px * 0.8))
                    )
                    live_meta["sl_locked"] = bool(sl_locked_server)

                    exit_mode = str(strat_cfg.get("exit_mode", "HYBRID"))
                    can_check_harvest = not (exit_mode == "RUNNER" and sl_locked_server)
                    harvest_rate_l1 = max((1.60 * init_sl_pct) + (2.60 * rt_fee_rate), rt_fee_rate * 3.50, atr_pct * harvest_atr)

                    exhaust_sens = str(strat_cfg.get("exhaust_sens", "MED"))
                    rsi = float(ind_5m.get("rsi", 50.0))
                    bb_pos = float(ind_5m.get("bb_pos", 0.5))
                    regime = str(ind_5m.get("regime", "RANGE_NORMAL"))
                    ema9_15 = float(ind_15m.get("ema9", c.close))
                    ema21_15 = float(ind_15m.get("ema21", c.close))

                    if exhaust_sens == "MAX":
                        is_exhausted = (
                            (is_long and (rsi > 60 or bb_pos > 0.74 or delta_atr < -0.06))
                            or ((not is_long) and (rsi < 40 or bb_pos < 0.26 or delta_atr < -0.06))
                            or (regime in ("RANGE_SQUEEZE", "CHOP_HIGHVOL") and eval_count >= 2)
                        )
                    elif exhaust_sens == "HIGH":
                        is_exhausted = (
                            (is_long and (rsi > 64 or bb_pos > 0.80 or delta_atr < -0.10))
                            or ((not is_long) and (rsi < 36 or bb_pos < 0.20 or delta_atr < -0.10))
                            or (regime in ("RANGE_SQUEEZE", "CHOP_HIGHVOL") and eval_count >= 3)
                        )
                    elif exhaust_sens == "MED":
                        ema_flip = (is_long and ema9_15 < ema21_15) or ((not is_long) and ema9_15 > ema21_15)
                        is_exhausted = (
                            ((is_long and rsi > 69) or ((not is_long) and rsi < 31)) and delta_atr < -0.08
                        ) or (ema_flip and delta_atr < -0.10)
                    else:
                        is_exhausted = (
                            ((is_long and rsi > 75) or ((not is_long) and rsi < 25)) and delta_atr < -0.15
                        )

                    if can_check_harvest and close_pct >= harvest_rate_l1 and is_exhausted:
                        ok_h, rec_h = self.exchange.close_trade(
                            live_tid, price=float(c.close), reason="GUARDIAN_HARVEST", now=now
                        )
                        if ok_h and rec_h:
                            events.append(rec_h)
                        continue

                    step_min = max(12.0, 0.15 * cur_atr)
                    spot_buf = max(22.0, 0.25 * cur_atr)
                    liq_p = float(live_t.get("liquidation", 0.0))
                    cur_sl = float(live_t.get("stoploss", 0.0))
                    profit_lock_rate = rt_fee_rate * 1.65

                    s1_fee_mult = 2.50 if self.be_stage1_r >= 1.0 else 2.20
                    trig_stage1 = max(
                        (self.be_stage1_r * init_sl_pct) + ((1.0 + self.be_stage1_r) * rt_fee_rate),
                        rt_fee_rate * s1_fee_mult,
                    )
                    trig_stage2 = max((1.35 * init_sl_pct) + (2.35 * rt_fee_rate), atr_pct * trig_atr * 1.25, rt_fee_rate * 3.00)
                    stage2_floor_rate = (0.55 * init_sl_pct) + (1.55 * rt_fee_rate)
                    slack_px = 1.35 * max(cur_atr * 0.45, float(strat_cfg.get("trail_slack_atr", 0.68)) * cur_atr)

                    if close_pct >= trig_stage2:
                        live_meta["trail_stage2"] = True

                    struct_sl = None
                    if self.structural_td9_trailing_enabled and hasattr(self.feed, "get_structural_context"):
                        _s_ctx = self.feed.get_structural_context(atr_ref=cur_atr, spot_price=float(c.close))
                        struct_sl = compute_structural_td9_trailing_stop(
                            side_is_long=is_long,
                            spot_price=float(c.close),
                            current_sl=cur_sl,
                            entry_price=entry_p,
                            atr=cur_atr,
                            td9_state=_s_ctx.get("td9"),
                            hierarchy_pools=_s_ctx.get("hierarchy"),
                            enabled=True,
                        )

                    if bool(live_meta.get("trail_stage2", False)):
                        if is_long:
                            floor_s2 = float(math.ceil(entry_p * (1.0 + stage2_floor_rate)))
                            raw_sl = max(floor_s2, float(c.close) - slack_px)
                            target_sl = float(round(min(raw_sl, float(c.close) - spot_buf), 2))
                            if struct_sl is not None:
                                target_sl = max(target_sl, float(round(min(struct_sl, float(c.close) - spot_buf), 2)))
                            if target_sl >= floor_s2 and target_sl > (liq_p + 15.0) and target_sl > (cur_sl + step_min):
                                live_t["stoploss"] = target_sl
                                live_meta["sl_is_trailing_lock"] = True
                                live_meta["sl_locked"] = True
                        else:
                            ceil_s2 = float(math.floor(entry_p * (1.0 - stage2_floor_rate)))
                            raw_sl = min(ceil_s2, float(c.close) + slack_px)
                            target_sl = float(round(max(raw_sl, float(c.close) + spot_buf), 2))
                            if struct_sl is not None:
                                target_sl = min(target_sl, float(round(max(struct_sl, float(c.close) + spot_buf), 2)))
                            liq_ok = (liq_p <= 0.0) or (target_sl < (liq_p - 15.0))
                            if target_sl <= ceil_s2 and liq_ok and (cur_sl <= 0.0 or target_sl < (cur_sl - step_min)):
                                live_t["stoploss"] = target_sl
                                live_meta["sl_is_trailing_lock"] = True
                                live_meta["sl_locked"] = True
                    elif close_pct >= trig_stage1 and not bool(live_meta.get("sl_is_trailing_lock", False)):
                        if is_long:
                            be_floor = float(math.ceil(entry_p * (1.0 + profit_lock_rate)))
                            cand_sl = max(be_floor, float(round(min(struct_sl, float(c.close) - spot_buf), 2))) if struct_sl is not None else be_floor
                            if cand_sl <= (float(c.close) - spot_buf) and cand_sl > (liq_p + 15.0) and cand_sl > (cur_sl + step_min):
                                live_t["stoploss"] = cand_sl
                                live_meta["sl_is_trailing_lock"] = True
                                live_meta["sl_locked"] = True
                        else:
                            be_ceil = float(math.floor(entry_p * (1.0 - profit_lock_rate)))
                            cand_sl = min(be_ceil, float(round(max(struct_sl, float(c.close) + spot_buf), 2))) if struct_sl is not None else be_ceil
                            liq_ok = (liq_p <= 0.0) or (cand_sl < (liq_p - 15.0))
                            if cand_sl >= (float(c.close) + spot_buf) and liq_ok and (cur_sl <= 0.0 or cand_sl < (cur_sl - step_min)):
                                live_t["stoploss"] = cand_sl
                                live_meta["sl_is_trailing_lock"] = True
                                live_meta["sl_locked"] = True
                else:
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
                self.dd_guard_events += 1
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

        # 3. Supervisar coberturas mediante PortfolioHedgeCoordinator + AdaptiveHedgeController + HedgeRiskManager (solo si no está desactivado en modo determinista)
        if not self.deterministic_trend_policy:
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

        if not dd_throttle_halt and rem_bars >= min_rem_bars and self.limit_manager.effective_slots_used(running_count) < max_trades and (now - self.last_entry_ts) >= Config.ENTRY_COOLDOWN_SEC:
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
            elif self.deterministic_trend_policy:
                max_q = max(float(q_vec[a]) for a in range(1, 9))
                if max_q <= 0.0:
                    cand_order = []
                else:
                    top_acts = [a for a in range(1, 9) if abs(float(q_vec[a]) - max_q) <= 1e-12]
                    if any(1 <= a <= 4 for a in top_acts) and any(5 <= a <= 8 for a in top_acts):
                        cand_order = []
                    else:
                        cand_order = [top_acts[0]]
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
                base_risk_pct = (
                    0.015
                    if self.explorer_mode
                    else (self.base_risk_pct_std if self.deterministic_trend_policy else 0.012)
                ) * dd_dampener
                auto_adj = self.brain.supervisor.get_autonomous_adjustments() if hasattr(self.brain, "supervisor") and self.brain.supervisor else {"expectancy_boost_applied": 0.0}
                vel_ema = float(perception.get("vel_ema_atr", 0.0)) if isinstance(perception, dict) else 0.0

                parts_curr = str(curr_state_key).split("|")
                seg1 = parts_curr[0] if len(parts_curr) > 0 else "RANGE_NORMAL"
                seg5 = parts_curr[4] if len(parts_curr) > 4 else "EMA_TRANS"

                evaluated_acts = set()
                for cand_a in cand_order:
                    action, _ = SatsExpectancyGate.filter_and_rank_action(
                        cand_a, curr_state_key, q_vec, self.brain.strategies, perception,
                        recovery_mode=(current_dd >= 0.035), current_dd_pct=current_dd,
                        explorer_mode=False,
                        deterministic_trend_policy=self.deterministic_trend_policy,
                    )
                    if action <= 0 or action in evaluated_acts or action not in self.brain.strategies:
                        continue
                    evaluated_acts.add(action)

                    if self.deterministic_trend_policy:
                        if action not in (1, 5):
                            continue
                        if seg1 in ("RANGE_NORMAL", "RANGE_SQUEEZE", "CHOP_HIGHVOL", "EXTREME_VOLATILITY"):
                            continue
                        if action == 1 and (seg1 == "TREND_BEAR" or seg5 in ("FULL_BEAR", "BEAR_RALLY")):
                            continue
                        if action == 5 and (seg1 == "TREND_BULL" or seg5 in ("FULL_BULL", "BULL_DIP")):
                            continue

                    if not self.explorer_mode:
                        q_val = float(q_vec[action]) if action < len(q_vec) else 0.0
                        if q_val <= 0.0:
                            continue

                    strat = self.brain.strategies[action]
                    side = strat["side"]

                    if self.deterministic_trend_policy and self.streak3_24h and hasattr(self, "side_brake") and self.side_brake is not None:
                        paused_s, _ = self.side_brake.is_side_paused(side, now=now)
                        if paused_s:
                            continue

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
                    effective_rr = max(1.5, float(strat["rr"]) * eff_rr_mult)
                    if self.deterministic_trend_policy and self.trend_sl_floor_pct > 0.0 and action in (1, 5):
                        raw_sl_dist = max(atr * float(strat["sl_atr"]), price * 0.0024)
                        floor_dist = price * self.trend_sl_floor_pct
                        if floor_dist > raw_sl_dist:
                            sl_distance = floor_dist
                            lev = max(
                                Config.MIN_LEVERAGE,
                                min(Config.MAX_LEVERAGE, int((0.68 / (sl_distance / price)) * float(strat["lev_f"]))),
                            )
                            tp_distance = (sl_distance * effective_rr) + (price * (2.2 * Config.TAKER_FEE))
                            sl = (price - sl_distance) if side == "buy" else (price + sl_distance)
                            tp = (price + tp_distance) if side == "buy" else (price - tp_distance)

                    sl_dist_pct = abs(price - sl) / max(price, 1.0)
                    if self.deterministic_trend_policy and self.short_exp_phys_hold and action == 5 and sl_dist_pct <= 0.006505:
                        continue

                    q_val_entry = float(q_vec[action]) if action < len(q_vec) else 0.0
                    st_visits_entry = 1 if self.deterministic_trend_policy else int(self.brain.state_visits.get(curr_state_key, 0))
                    eff_style_metrics = None if self.deterministic_trend_policy else self.brain.style_metrics
                    p_hat, p_lcb, _ = SatsExpectancyGate.estimate_bayesian_win_rate(
                        curr_state_key,
                        action,
                        strat_style,
                        mkt_reg_tag,
                        eff_style_metrics,
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
                    if margin_sats > derived_margin and derived_margin > 0:
                        adjusted_lev = max(1, int(round((float(derived_margin) * float(lev)) / float(margin_sats))))
                        if adjusted_lev < lev:
                            lev = adjusted_lev

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
                        style_metrics=eff_style_metrics,
                        q_val=q_val_entry,
                        state_visits=st_visits_entry,
                    )

                    _tm_sim = getattr(self, "thresholds", None) or getattr(self.brain, "thresholds", None) or get_thresholds_manager()
                    liq_cfg = getattr(self.feed, "liquidity_config_override", None) or LiquidityConfig.from_thresholds(_tm_sim)
                    liq_sig = perception.get("liquidity") if isinstance(perception, dict) else None
                    if (
                        viable
                        and liq_cfg.enabled
                        and (not liq_cfg.shadow_only)
                        and liq_cfg.opposite_wall_enabled
                        and (not self.explorer_mode)
                        and (strat_style != "TREND")
                        and (action not in (1, 5))
                    ):
                        liq_room = compute_opposite_pool_room(
                            side_is_long=(1 <= (int(action) % 9) <= 4),
                            entry_price=float(price),
                            margin_sats=int(margin_sats),
                            leverage=float(lev),
                            taker_fee=float(_tm_sim.get_float("fees", "taker_fee_rate", 0.001)),
                            liquidity_signal=liq_sig,
                            min_wall_fee_ratio=liq_cfg.min_opposite_wall_fee_ratio,
                            min_wall_touches=liq_cfg.min_wall_touches,
                        )
                        if liq_room.get("blocked_by_wall", False):
                            viable = False

                    if viable and (not self.explorer_mode):
                        pre_edge = compute_live_edge(
                            brain=self.brain,
                            state=curr_state_key,
                            action=action,
                            ind_5m=ind_5m,
                            perception=perception,
                            margin_sats=int(margin_sats),
                            leverage=float(lev),
                            entry_price=float(price),
                            simulated_entry=True,
                            now=now,
                        )
                        base_edge_no_liq = float(pre_edge["edge_score"]) - float(pre_edge.get("liq_bias", 0.0))
                        eff_liq_bias = (
                            compute_liquidity_bias(
                                action=action,
                                liquidity_signal=liq_sig,
                                cfg=liq_cfg,
                                num_actions=getattr(self.brain, "num_actions", 9),
                            )
                            if (liq_cfg.enabled and (not liq_cfg.shadow_only))
                            else 0.0
                        )
                        min_preflight_edge = 0.15 if (current_dd >= 0.035) else 0.0
                        if (base_edge_no_liq + eff_liq_bias) < min_preflight_edge:
                            viable = False

                    if viable and margin_sats <= self.exchange.available_balance_sats:
                        norm_side = self._norm_side(side)
                        if self.deterministic_trend_policy:
                            ok_stk, _ = self._check_anti_stacking_s2(side, price, atr, now)
                            if not ok_stk:
                                continue

                        lim_cfg = self.limit_config_override or LimitExecutionConfig.from_thresholds(_tm_sim)
                        use_limit = False
                        if lim_cfg.enabled and (not self.explorer_mode):
                            use_limit, _ = should_route_limit(
                                action=action,
                                style=strat_style,
                                market_regime=mkt_reg_tag,
                                liquidity_signal=liq_sig,
                                spot_price=float(price),
                                atr_5m=float(atr),
                                taker_fee=float(_tm_sim.get_float("fees", "taker_fee_rate", 0.001)),
                                recovery_mode=(current_dd >= 0.035),
                                cfg=lim_cfg,
                            )
                        if use_limit:
                            lim_plan = compute_limit_entry_plan(
                                side=side,
                                style=strat_style,
                                spot_price=float(price),
                                atr_5m=float(atr),
                                base_sl=float(sl),
                                base_tp=float(tp),
                                liquidity_signal=liq_sig,
                                cfg=lim_cfg,
                            )
                            if not lim_plan.get("valid", False):
                                continue
                            lim_meta = {
                                "action": action,
                                "style": strat["style"],
                                "state": curr_state_key,
                                "entry_q": float(q_vec[action]) if action < len(q_vec) else 0.0,
                                "is_exploratory": bool(is_exploring or getattr(self.brain, "_last_decision_exploratory", False)),
                                "liq_score": float((liq_sig or {}).get("liquidity_score", 0.0)),
                            }
                            ok_lim, _, _ = self.limit_manager.submit_limit_order(
                                client=self.exchange,
                                side=side,
                                margin_sats=margin_sats,
                                leverage=lev,
                                plan=lim_plan,
                                meta=lim_meta,
                                running_count=running_count,
                                atr_5m=float(atr),
                                max_slots=max_trades,
                                now=now,
                            )
                            if ok_lim:
                                self.last_entry_ts = now
                                self.last_side_entry_ts[norm_side] = now
                                entry_dispatched = True
                                break
                            continue
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
                            _entry_px_m = max(1.0, float(trade.get("entry_price") or trade.get("price") or price))
                            _sl_init_m = float(trade.get("stoploss", sl) or sl)
                            _init_sl_pct_m = (
                                max(0.0020, abs(_entry_px_m - _sl_init_m) / _entry_px_m)
                                if _sl_init_m > 0.0
                                else 0.0040
                            )
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
                                "entry_type": "market",
                                "initial_sl_dist_pct": _init_sl_pct_m,
                                "sl_locked": False,
                                "sl_is_trailing_lock": False,
                                "trail_stage2": False,
                            }
                            self.last_entry_ts = now
                            self.last_side_entry_ts[norm_side] = now
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

        # Cancelar órdenes límite pendientes al finalizar el episodio
        if self.limit_manager.pending:
            self.limit_manager.cancel_all_for_shutdown(
                self.exchange, list(self.exchange.running_trades.values())
            )

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
            "limit_stats": dict(self.limit_manager.stats),
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
