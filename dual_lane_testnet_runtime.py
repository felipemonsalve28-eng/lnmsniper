# -*- coding: utf-8 -*-
"""
dual_lane_testnet_runtime.py
Runtime autocontenido de Carril B (SCALP_SWEEP_WIDE + LIMIT + TIME_STOP 16h +
LaneBudgetController + MicroFlowGuard shadow_only=True) exclusivo para Testnet 48h.
"""
import json
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from binance_micro_sidecar import (
    append_binance_micro_trade_event,
    read_binance_micro_snapshot,
)
from guardian_execution_core import FileLockContext
from limit_execution_manager import (
    LimitExecutionConfig,
    compute_limit_entry_plan,
)
from liquidity_radar import LiquidityConfig, LiquidityRadar
from micro_flow_guard import MicroFlowGuardConfig, evaluate_micro_entry_guard
from simulation.lane_budget import LaneBudgetConfig, LaneBudgetController
from simulation.scalp_sim_engine import (
    SCALP_PROFILES,
    _normalize_macro_ind_dict,
    _round_nearest_tick,
    classify_macro_tf_regime,
    compute_scalp_loss_at_sl_sats,
)
from sniper_perception_sats_core import SatsExpectancyGate

_ALLOWED_MACRO_RANGE_TAGS = frozenset({
    "",
    "RANGE",
    "RANGING",
    "RANGE_NORMAL",
    "RANGE_SQUEEZE",
    "TRANSITION",
    "NEUTRAL",
    "EMA_FLAT",
})
_ALLOWED_MACRO_CLEAN_TAGS = frozenset({
    "",
    "RANGE",
    "RANGING",
    "RANGE_NORMAL",
    "RANGE_SQUEEZE",
    "NEUTRAL",
    "EMA_FLAT",
})


def _norm_side(side: Any) -> str:
    s = str(side or "").strip().lower()
    if s in ("b", "buy", "long"):
        return "b"
    if s in ("s", "sell", "short"):
        return "s"
    return ""


def _extract_v3_fees(trade_obj: Optional[Dict[str, Any]]) -> Tuple[int, int, int, bool]:
    """Extrae openingFee, closingFee y sumFundingFees reutilizando la lógica API v3."""
    try:
        from lnm_sniper_v6_advanced import extract_trade_fees_v3

        return extract_trade_fees_v3(trade_obj)
    except Exception:
        if not isinstance(trade_obj, dict):
            return 0, 0, 0, False
        open_val = trade_obj.get("openingFee")
        if open_val is None:
            open_val = trade_obj.get("opening_fee")
        close_val = trade_obj.get("closingFee")
        if close_val is None:
            close_val = trade_obj.get("closing_fee")
        has_explicit = (open_val is not None) or (close_val is not None)
        open_fee = max(0, int(round(float(open_val or 0)))) if open_val is not None else 0
        close_fee = max(0, int(round(float(close_val or 0)))) if close_val is not None else 0
        funding_val = None
        for key in ("sumFundingFees", "sum_carry_fees", "funding_fees"):
            if trade_obj.get(key) is not None:
                funding_val = trade_obj.get(key)
                break
        funding_fee = int(round(float(funding_val or 0))) if funding_val is not None else 0
        return open_fee, close_fee, funding_fee, has_explicit


def _extract_funding_sats(trade_obj: Optional[Dict[str, Any]]) -> int:
    try:
        from lnm_sniper_v6_advanced import extract_trade_funding_sats

        return int(extract_trade_funding_sats(trade_obj))
    except Exception:
        _, _, funding_fee, _ = _extract_v3_fees(trade_obj)
        return int(funding_fee)


class DualLaneTestnetRuntime:
    """Orquestador aislado de Carril B para prueba 48h en Testnet."""

    def __init__(
        self,
        env_mode: str = "testnet",
        *,
        state_path: Optional[str] = None,
        micro_events_log_path: Optional[str] = None,
        micro_snapshot_path: Optional[str] = None,
        initial_equity_sats: Optional[float] = None,
        max_hold_hours: float = 16.0,
        fee_rate_open: float = 0.0008,
        fee_rate_close: float = 0.0008,
        min_margin_sats: int = 1000,
    ) -> None:
        env_norm = str(env_mode or "").strip().lower()
        if env_norm != "testnet":
            raise ValueError(
                f"DualLaneTestnetRuntime solo permitido en env_mode='testnet' (recibido={env_mode!r})."
            )

        self.env_mode = "testnet"
        base_dir = os.path.dirname(os.path.abspath(__file__))
        home_state_dir = os.path.join(os.path.expanduser("~"), ".sniper_state")
        self.state_path = state_path or os.path.join(home_state_dir, "lane_b_state_testnet.json")
        self.micro_events_log_path = micro_events_log_path or os.path.join(
            base_dir, "logs", "binance_micro_events_testnet.jsonl"
        )
        self.micro_snapshot_path = micro_snapshot_path

        self.profile = SCALP_PROFILES["SCALP_SWEEP_WIDE"]
        self.exit_mode = "pure_sl_tp"
        self.max_hold_hours = float(max_hold_hours)
        self.max_hold_sec = max(60.0, float(max_hold_hours) * 3600.0)
        self.fee_rate_open = float(fee_rate_open)
        self.fee_rate_close = float(fee_rate_close)
        self.min_margin_sats = max(1, int(min_margin_sats))

        self.limit_cfg = LimitExecutionConfig(
            enabled=True,
            tick_size_usd=float(self.profile.tick_size_usd),
            ttl_sec_5m=float(self.profile.ttl_sec_5m),
            ttl_sec_15m=float(self.profile.ttl_sec_15m),
            runaway_tp_frac=float(self.profile.runaway_tp_frac),
            max_entry_offset_atr=float(self.profile.max_entry_offset_atr),
            retest_atr_scalp=float(self.profile.retest_atr),
            fallback_pullback_atr=float(self.profile.fallback_pullback_atr),
            invalidation_buffer_atr=float(self.profile.invalidation_buffer_atr),
        )
        self.budget_cfg = LaneBudgetConfig(
            enabled=True,
            max_concurrent_total=5,
            lane_a_reserved_slots=3,
            lane_a_max_slots=5,
            lane_b_max_slots=2,
            lane_b_max_per_side=1,
            lane_b_max_open_total_before_entry=3,
            lane_b_max_margin_share=0.15,
            lane_b_soft_dd_cut_pct=0.0080,
            lane_b_soft_risk_mult=0.50,
            lane_b_hard_dd_pause_pct=0.0150,
            lane_b_pause_sec=86400.0,
            lane_b_side_loss_streak_limit=2,
            protective_dd_a_max_pct=0.0200,
            protective_dd_global_max_pct=0.0220,
            rolling_pause_reset=True,
        )
        self.micro_guard_cfg = MicroFlowGuardConfig(enabled=True, shadow_only=False, max_age_sec=5.0)
        self.liquidity_radar = LiquidityRadar(
            LiquidityConfig(enabled=True, shadow_only=False, opposite_wall_enabled=False)
        )

        self.pending_orders: Dict[str, Dict[str, Any]] = {}
        self.active_trades: Dict[str, Dict[str, Any]] = {}
        self.known_lane_b_ids: List[str] = []
        self.known_client_ids: List[str] = []
        self.last_side_entry_ts: Dict[str, float] = {"b": -1e18, "s": -1e18}
        self.last_eval_1m_ts: Optional[float] = None
        self.budget: Optional[LaneBudgetController] = None

        self.load_state()
        if self.budget is None and initial_equity_sats is not None and float(initial_equity_sats) > 0.0:
            self.budget = LaneBudgetController(float(initial_equity_sats), config=self.budget_cfg)

    def _remember_ids(self, trade_id: Optional[str] = None, client_id: Optional[str] = None) -> None:
        if trade_id:
            tid = str(trade_id).strip()
            if tid and tid not in self.known_lane_b_ids:
                self.known_lane_b_ids.append(tid)
                if len(self.known_lane_b_ids) > 500:
                    self.known_lane_b_ids = self.known_lane_b_ids[-500:]
        if client_id:
            cid = str(client_id).strip()
            if cid and cid not in self.known_client_ids:
                self.known_client_ids.append(cid)
                if len(self.known_client_ids) > 500:
                    self.known_client_ids = self.known_client_ids[-500:]

    def is_lane_b_trade(self, trade_or_id: Any) -> bool:
        """Retorna True si el trade o ID pertenece a Carril B para evitar adopción de Carril A."""
        if isinstance(trade_or_id, dict):
            tid = str(trade_or_id.get("id") or "").strip()
            cid = str(trade_or_id.get("clientId") or trade_or_id.get("client_id") or "").strip()
            if cid.startswith("lb_") or (cid and cid in self.known_client_ids):
                return True
        else:
            tid = str(trade_or_id or "").strip()
        if not tid:
            return False
        return (
            tid in self.pending_orders
            or tid in self.active_trades
            or tid in self.known_lane_b_ids
        )

    def pending_order_count(self) -> int:
        return len(self.pending_orders)

    def active_trade_count(self) -> int:
        return len(self.active_trades)

    def reserved_margin_sats(self) -> int:
        return int(sum(int(rec.get("margin_sats") or 0) for rec in self.pending_orders.values()))

    def _serialize_budget(self) -> Optional[Dict[str, Any]]:
        if self.budget is None:
            return None
        b = self.budget
        return {
            "initial_equity_sats": float(b.initial_equity_sats),
            "e_b": float(b.e_b),
            "peak_b": float(b.peak_b),
            "dd_b": float(b.dd_b),
            "e_global": float(b.e_global),
            "peak_global": float(b.peak_global),
            "dd_global": float(b.dd_global),
            "paused_until_ts_b": float(b.paused_until_ts_b),
            "paused_until_ts_b_side": {k: float(v) for k, v in b.paused_until_ts_b_side.items()},
            "loss_streak_b": {k: int(v) for k, v in b.loss_streak_b.items()},
            "episode_peak_b": float(b.episode_peak_b),
            "dd_b_episode": float(b.dd_b_episode),
            "dd_pause_armed_b": bool(b._dd_pause_armed_b),
            "post_pause_half_risk_b": bool(b.post_pause_half_risk_b),
            "reanchor_count_b": int(b.reanchor_count_b),
        }

    def _restore_budget(self, data: Optional[Dict[str, Any]]) -> None:
        if not isinstance(data, dict):
            return
        init_eq = float(data.get("initial_equity_sats") or 0.0)
        if init_eq <= 0.0:
            return
        b = LaneBudgetController(init_eq, config=self.budget_cfg)
        b.e_b = float(data.get("e_b", init_eq))
        b.peak_b = float(data.get("peak_b", init_eq))
        b.dd_b = float(data.get("dd_b", 0.0))
        b.e_global = float(data.get("e_global", init_eq))
        b.peak_global = float(data.get("peak_global", init_eq))
        b.dd_global = float(data.get("dd_global", 0.0))
        b.paused_until_ts_b = float(data.get("paused_until_ts_b", -1e18))
        raw_side_p = data.get("paused_until_ts_b_side") or {}
        b.paused_until_ts_b_side = {
            "b": float(raw_side_p.get("b", -1e18)),
            "s": float(raw_side_p.get("s", -1e18)),
        }
        raw_streak = data.get("loss_streak_b") or {}
        b.loss_streak_b = {
            "b": int(raw_streak.get("b", 0)),
            "s": int(raw_streak.get("s", 0)),
        }
        b.episode_peak_b = float(data.get("episode_peak_b", init_eq))
        b.dd_b_episode = float(data.get("dd_b_episode", 0.0))
        b._dd_pause_armed_b = bool(data.get("dd_pause_armed_b", False))
        b.post_pause_half_risk_b = bool(data.get("post_pause_half_risk_b", False))
        b.reanchor_count_b = int(data.get("reanchor_count_b", 0))
        self.budget = b

    def load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        lock_path = f"{self.state_path}.lock"
        try:
            with FileLockContext(lock_path, exclusive=False, timeout=2.0, raise_on_timeout=False):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            if not isinstance(raw, dict):
                return
            self.pending_orders = dict(raw.get("pending_orders") or {})
            self.active_trades = dict(raw.get("active_trades") or {})
            self.known_lane_b_ids = [str(x) for x in (raw.get("known_lane_b_ids") or [])][-500:]
            self.known_client_ids = [str(x) for x in (raw.get("known_client_ids") or [])][-500:]
            lse = raw.get("last_side_entry_ts") or {}
            self.last_side_entry_ts = {
                "b": float(lse.get("b", -1e18)),
                "s": float(lse.get("s", -1e18)),
            }
            le1m = raw.get("last_eval_1m_ts")
            self.last_eval_1m_ts = float(le1m) if le1m is not None else None
            self._restore_budget(raw.get("budget"))
        except Exception:
            pass

    def save_state(self) -> bool:
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        payload = {
            "version": 1,
            "env": self.env_mode,
            "updated_at": time.time(),
            "pending_orders": self.pending_orders,
            "active_trades": self.active_trades,
            "known_lane_b_ids": self.known_lane_b_ids[-500:],
            "known_client_ids": self.known_client_ids[-500:],
            "last_side_entry_ts": self.last_side_entry_ts,
            "last_eval_1m_ts": self.last_eval_1m_ts,
            "budget": self._serialize_budget(),
        }
        lock_path = f"{self.state_path}.lock"
        tmp_path = f"{self.state_path}.tmp.{os.getpid()}"
        try:
            with FileLockContext(lock_path, exclusive=True, timeout=2.0, raise_on_timeout=False):
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.state_path)
            return True
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

    def evaluate_micro_guard_shadow(
        self,
        side: Any,
        micro_snapshot: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Evalúa MicroFlowGuard en O(1) con shadow_only=True y fail-open estricto."""
        snap = micro_snapshot
        if snap is None:
            snap = read_binance_micro_snapshot(
                state_path=self.micro_snapshot_path,
                now_ts=now_ts,
                ttl_sec=15.0,
            )
        dec = evaluate_micro_entry_guard(side, snap, cfg=self.micro_guard_cfg, now_ts=now_ts)
        age_val: Optional[float] = None
        if isinstance(snap, dict):
            raw_age = snap.get("age_sec")
            if raw_age is not None:
                try:
                    age_f = float(raw_age)
                    if math.isfinite(age_f):
                        age_val = round(age_f, 3)
                except (TypeError, ValueError):
                    age_val = None
            elif now_ts is not None:
                for ts_k in ("ts", "updated_ts", "updated_at_ts"):
                    if snap.get(ts_k) is not None:
                        try:
                            age_f = float(now_ts) - float(snap[ts_k])
                            if math.isfinite(age_f):
                                age_val = round(age_f, 3)
                                break
                        except (TypeError, ValueError):
                            pass
        return {
            "decision": "VETO" if (bool(dec.would_veto) or (not getattr(dec, "allowed", True))) else "ALLOW",
            "reason": str(dec.reason),
            "age_sec": age_val,
            "shadow_only": bool(self.micro_guard_cfg.shadow_only),
            "active": not bool(self.micro_guard_cfg.shadow_only),
            "blocked": bool((bool(dec.would_veto) or (not getattr(dec, "allowed", True))) and (not self.micro_guard_cfg.shadow_only) and (not dec.fail_open)),
            "fail_open": bool(dec.fail_open),
            "rule_code": dec.rule_code,
        }

    def evaluate_micro_guard(
        self,
        lane: str,
        side: Any,
        price: Optional[float] = None,
        micro_snapshot: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Evalúa MicroFlowGuard en bloqueo activo (fail-open O(1)) y registra VETO_BLOCKED."""
        ts = float(now_ts if now_ts is not None else time.time())
        snap = micro_snapshot
        if snap is None:
            snap = read_binance_micro_snapshot(
                state_path=self.micro_snapshot_path,
                now_ts=ts,
                ttl_sec=15.0,
            )
        mg = self.evaluate_micro_guard_shadow(side, micro_snapshot=snap, now_ts=ts)
        allowed = not bool(mg.get("blocked", False))
        if not allowed:
            self._emit_trade_event({
                "ts": round(ts, 3),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                "env": self.env_mode,
                "event": "VETO_BLOCKED",
                "lane": str(lane).upper(),
                "side": str(side),
                "lnm_price": round(float(price), 2) if price is not None else None,
                "reason": mg.get("reason"),
                "rule_code": mg.get("rule_code"),
                "age_sec": mg.get("age_sec"),
                "micro_guard": mg,
                "binance_micro": snap if isinstance(snap, dict) else None,
            })
        return allowed, mg

    def check_macro_range_only(
        self,
        ind_1h: Optional[Dict[str, Any]],
        ind_4h: Optional[Dict[str, Any]],
        ind_5m: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Filtro macro causal range_only usando velas cerradas 1h/4h."""
        n1h = _normalize_macro_ind_dict(ind_1h, "1h")
        n4h = _normalize_macro_ind_dict(ind_4h, "4h")
        if not n1h and not n4h:
            return False, "MACRO_UNAVAILABLE"

        c1h = classify_macro_tf_regime(n1h, "1h") if n1h else {}
        c4h = classify_macro_tf_regime(n4h, "4h") if n4h else {}

        reg_1h = str(c1h.get("regime") or n1h.get("regime") or n1h.get("market_regime") or "").upper()
        mreg_1h = str(c1h.get("market_regime") or n1h.get("market_regime") or "").upper()
        reg_4h = str(c4h.get("regime") or n4h.get("regime") or n4h.get("market_regime") or "").upper()
        mreg_4h = str(c4h.get("market_regime") or n4h.get("market_regime") or "").upper()
        reg_5m = str((ind_5m or {}).get("market_regime") or "").upper()

        if "EXTREME_VOLATILITY" in {reg_1h, mreg_1h, reg_4h, mreg_4h, reg_5m}:
            return False, "MACRO_EXTREME_VOL"

        def _is_dir(reg: str, ind: Dict[str, Any]) -> bool:
            if "BULL" in reg or "BEAR" in reg:
                return True
            if reg in _ALLOWED_MACRO_CLEAN_TAGS:
                return False
            ema_s = float(ind.get("ema_score") or 0.0)
            e9 = float(ind.get("ema9") or 0.0)
            e21 = float(ind.get("ema21") or 0.0)
            spread = abs(e9 - e21) / max(1.0, e21) if e21 > 0.0 else 0.0
            return (abs(ema_s) >= 0.5 and spread >= 0.0015 and ((ema_s > 0 and e9 > e21) or (ema_s < 0 and e9 < e21)))

        if (
            _is_dir(reg_1h, n1h)
            or _is_dir(reg_4h, n4h)
            or mreg_1h == "VOLATILITY_EXPANSION"
            or mreg_4h == "VOLATILITY_EXPANSION"
            or (reg_1h not in _ALLOWED_MACRO_RANGE_TAGS)
            or (reg_4h not in _ALLOWED_MACRO_RANGE_TAGS)
        ):
            return False, "MACRO_NOT_RANGE"
        return True, "OK"

    def _get_closed_1m_ts(self, mtf_cache: Any) -> Optional[float]:
        if mtf_cache is None:
            return None
        getter = getattr(mtf_cache, "get_closed_1m_ts", None)
        if callable(getter):
            val = getter()
            return float(val) if val is not None else None
        ram_agg = getattr(mtf_cache, "ram_aggregator", None)
        if ram_agg is not None:
            try:
                if ram_agg.closed_count("1m") > 0:
                    ts_seq, _, _, _, _, _ = ram_agg.get_closed_ohlcv("1m")
                    if ts_seq is not None and len(ts_seq) > 0:
                        return float(ts_seq[-1])
            except Exception:
                pass
        cache = getattr(mtf_cache, "cache", None)
        if isinstance(cache, dict):
            c1m = cache.get("1m") or {}
            ts_arr = c1m.get("ts")
            if ts_arr is not None and len(ts_arr) >= 2:
                return float(ts_arr[-2])
        return None

    def _get_spot_and_indicators(self, mtf_cache: Any, running_trades: Sequence[Dict[str, Any]]) -> Tuple[float, Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        spot = 0.0
        for t in running_trades or ():
            for k in ("markPrice", "price", "entryPrice"):
                if t.get(k):
                    try:
                        spot = float(t[k])
                        break
                    except (TypeError, ValueError):
                        pass
            if spot > 0.0:
                break
        ind_5m: Dict[str, Any] = {}
        ind_15m: Dict[str, Any] = {}
        ind_1h: Dict[str, Any] = {}
        ind_4h: Dict[str, Any] = {}
        if mtf_cache is not None:
            try:
                ind_5m = mtf_cache.get_indicators_fast("5m", spot) or {}
                if spot <= 0.0:
                    spot = float(ind_5m.get("price") or 0.0)
                ind_15m = mtf_cache.get_indicators_fast("15m", spot) or {}
                ind_1h = mtf_cache.get_indicators_fast("1h") or {}
                ind_4h = mtf_cache.get_indicators_fast("4h") or {}
            except Exception:
                pass
        return spot, ind_5m, ind_15m, ind_1h, ind_4h

    def _detect_sweep_wide_signal(
        self,
        mtf_cache: Any,
        spot_price: float,
        ind_5m: Dict[str, Any],
        ind_15m: Dict[str, Any],
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        if mtf_cache is None or spot_price <= 0.0:
            return None
        cache = getattr(mtf_cache, "cache", None)
        if not isinstance(cache, dict):
            return None
        c5 = cache.get("5m") or {}
        c15 = cache.get("15m") or {}
        d5 = c5.get("data")
        d15 = c15.get("data")
        if d5 is None and d15 is None:
            return None
        b_ratio = float(c5.get("basis_ratio") or 1.0)
        if not (0.95 <= b_ratio <= 1.05):
            b_ratio = 1.0
        spot_bin = spot_price / b_ratio
        atr_5m_bin = max(1.0, float(ind_5m.get("atr") or (spot_price * 0.0025)) / b_ratio)
        atr_15m_bin = max(1.0, float(ind_15m.get("atr") or (spot_price * 0.0040)) / b_ratio)

        sig = self.liquidity_radar.evaluate(
            candles_5m=d5,
            candles_15m=d15,
            atr_5m=atr_5m_bin,
            atr_15m=atr_15m_bin,
            spot_price=spot_bin,
            volumes_5m=c5.get("vol"),
            volumes_15m=c15.get("vol"),
        )
        if abs(b_ratio - 1.0) > 1e-6:
            for pk in ("swept_level", "wick_extreme", "nearest_high_pool", "nearest_low_pool"):
                if float(sig.get(pk) or 0.0) > 0.0:
                    sig[pk] = round(float(sig[pk]) * b_ratio, 2)

        score = float(sig.get("liquidity_score") or 0.0)
        if abs(score) < float(self.profile.min_liq_score):
            return None
        side = "b" if score > 0.0 else "s"
        tf = str(sig.get("timeframe") or "")
        if tf == "15m":
            return side, sig

        if tf == "5m":
            tf_ohlcv: Dict[str, Any] = {}
            for k in ("5m", "15m", "45m", "1h"):
                item = cache.get(k) or {}
                tup = item.get("data")
                if tup is not None and len(tup) == 3:
                    tf_ohlcv[k] = (tup[0], tup[1], tup[2], item.get("vol"))
            hierarchy: Dict[str, Any] = {}
            if tf_ohlcv:
                hierarchy = self.liquidity_radar.evaluate_multi_tf_hierarchy(
                    tf_ohlcv=tf_ohlcv,
                    atr_ref=atr_15m_bin,
                    spot_price=spot_bin,
                    atr_by_tf={"5m": atr_5m_bin, "15m": atr_15m_bin},
                )
            swept_lv = float(sig.get("swept_level") or 0.0)
            wick_lv = float(sig.get("wick_extreme") or 0.0)
            pools = hierarchy.get("low_pools" if side == "b" else "high_pools") or ()
            tol = 0.40 * max(1.0, float(ind_15m.get("atr") or atr_15m_bin))
            for p in pools:
                lv = float(p.level) * b_ratio
                if (getattr(p, "anchor_timeframe", "") in ("45m", "1h") or getattr(p, "is_super_pool", False)) and (
                    abs(lv - swept_lv) <= tol or (wick_lv > 0.0 and abs(lv - wick_lv) <= tol)
                ):
                    return side, sig
        return None

    def _compute_bracket_and_limit_plan(
        self,
        side: str,
        spot_price: float,
        atr_5m: float,
        liq_sig: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        cfg = self.profile
        px = max(1.0, float(spot_price))
        atr = max(1.0, float(atr_5m))
        sl_dist = min(cfg.max_sl_pct * px, max(cfg.min_sl_pct * px, atr * cfg.atr_sl_mult))
        tp_dist = min(cfg.max_tp_pct * px, max(cfg.min_tp_pct * px, sl_dist * cfg.target_rr))
        if side == "b":
            base_sl = _round_nearest_tick(px - sl_dist, cfg.tick_size_usd)
            base_tp = _round_nearest_tick(px + tp_dist, cfg.tick_size_usd)
        else:
            base_sl = _round_nearest_tick(px + sl_dist, cfg.tick_size_usd)
            base_tp = _round_nearest_tick(px - tp_dist, cfg.tick_size_usd)

        plan = compute_limit_entry_plan(
            side=side,
            style="SCALP_SWEEP_WIDE",
            spot_price=px,
            atr_5m=atr,
            base_sl=base_sl,
            base_tp=base_tp,
            liquidity_signal=liq_sig,
            cfg=self.limit_cfg,
        )
        if not plan.get("valid"):
            return None

        lim_px = float(plan["limit_price"])
        c_sl = abs(lim_px - float(plan["stoploss"]))
        c_sl = min(cfg.max_sl_pct * lim_px, max(cfg.min_sl_pct * lim_px, c_sl))
        c_tp = min(cfg.max_tp_pct * lim_px, max(cfg.min_tp_pct * lim_px, c_sl * cfg.target_rr))
        if side == "b":
            plan["stoploss"] = _round_nearest_tick(lim_px - c_sl, cfg.tick_size_usd)
            plan["takeprofit"] = _round_nearest_tick(lim_px + c_tp, cfg.tick_size_usd)
        else:
            plan["stoploss"] = _round_nearest_tick(lim_px + c_sl, cfg.tick_size_usd)
            plan["takeprofit"] = _round_nearest_tick(lim_px - c_tp, cfg.tick_size_usd)
        return plan

    def _compute_margin_and_risk(
        self,
        equity_sats: float,
        available_margin_sats: float,
        limit_price: float,
        sl_price: float,
        open_margin_b_sats: float,
        open_loss_at_sl_b_sats: float,
    ) -> Tuple[int, float]:
        assert self.budget is not None
        b = self.budget
        soft_mult = (
            float(self.budget_cfg.lane_b_soft_risk_mult)
            if (b.dd_b >= self.budget_cfg.lane_b_soft_dd_cut_pct or b.post_pause_half_risk_b)
            else 1.0
        )
        risk_target = float(self.profile.base_risk_pct_b) * float(equity_sats) * soft_mult
        dd_headroom = max(0.0, (float(self.budget_cfg.lane_b_hard_dd_pause_pct) - b.dd_b) * b.e_global - open_loss_at_sl_b_sats)
        ep_headroom = max(0.0, (float(self.budget_cfg.lane_b_episode_hard_cap_pct) - b.dd_b_episode) * b.episode_peak_b - open_loss_at_sl_b_sats)
        eff_risk = min(risk_target, dd_headroom, ep_headroom)
        if eff_risk <= 0.0:
            return 0, 0.0

        lev = int(self.profile.default_leverage)
        loss_per_sat = compute_scalp_loss_at_sl_sats(
            margin_sats=1.0,
            leverage=float(lev),
            entry_price=float(limit_price),
            sl_exec_price=float(sl_price),
            fee_rate_open=self.fee_rate_open,
            fee_rate_close=self.fee_rate_close,
        )
        if loss_per_sat <= 1e-9:
            return 0, 0.0
        max_margin_cap = max(0.0, float(self.budget_cfg.lane_b_max_margin_share) * float(available_margin_sats) - open_margin_b_sats)
        margin_sats = int(math.floor(min(eff_risk / loss_per_sat, max_margin_cap)))
        if margin_sats < self.min_margin_sats:
            return 0, 0.0
        loss_at_sl = compute_scalp_loss_at_sl_sats(
            margin_sats=float(margin_sats),
            leverage=float(lev),
            entry_price=float(limit_price),
            sl_exec_price=float(sl_price),
            fee_rate_open=self.fee_rate_open,
            fee_rate_close=self.fee_rate_close,
        )
        return margin_sats, float(loss_at_sl)

    def _emit_trade_event(self, payload: Dict[str, Any]) -> None:
        try:
            append_binance_micro_trade_event(self.micro_events_log_path, payload)
        except Exception:
            pass

    def _pending_cancel_reason(
        self,
        rec: Dict[str, Any],
        spot_price: float,
        now_ts: float,
        hard_floor_active: bool,
    ) -> Optional[str]:
        if hard_floor_active:
            return "HARD_FLOOR_OR_SAFE_MODE"
        age = max(0.0, float(now_ts) - float(rec.get("created_at") or now_ts))
        if age >= float(rec.get("ttl_sec") or self.limit_cfg.ttl_sec_5m):
            return f"TTL_EXPIRED({age:.0f}s)"
        spot = float(spot_price or 0.0)
        p_lim = float(rec.get("limit_price") or 0.0)
        p_tp = float(rec.get("tp_price") or 0.0)
        p_inv = float(rec.get("invalidation_price") or 0.0)
        is_long = (rec.get("side") == "b")
        if spot > 0.0 and p_lim > 0.0:
            if is_long and p_inv > 0.0 and spot < p_inv:
                return "WICK_INVALIDATED"
            if (not is_long) and p_inv > 0.0 and spot > p_inv:
                return "WICK_INVALIDATED"
            sign = 1.0 if is_long else -1.0
            tp_span = sign * (p_tp - p_lim)
            if tp_span > 1e-6:
                progress = (sign * (spot - p_lim)) / tp_span
                if progress >= float(self.profile.runaway_tp_frac):
                    return f"RUNAWAY_TP_{int(round(progress * 100))}PCT"
        return None

    def _compute_real_net_pnl(self, ct_obj: Dict[str, Any], rec: Dict[str, Any]) -> Tuple[int, int, int, int]:
        open_fee, close_fee, funding_fee, has_explicit = _extract_v3_fees(ct_obj)
        if ct_obj.get("real_balance_delta_sats") is not None:
            net_pnl = int(round(float(ct_obj["real_balance_delta_sats"])))
            return net_pnl, open_fee, close_fee, funding_fee
        pl = int(round(float(ct_obj.get("pl") or 0)))
        if bool(ct_obj.get("pl_includes_fees")):
            return pl, open_fee, close_fee, funding_fee
        fill_px = float(ct_obj.get("entryPrice") or ct_obj.get("price") or rec.get("fill_price") or rec.get("limit_price") or 0.0)
        econ = SatsExpectancyGate.compute_trade_economic_breakdown(
            gross_pnl_sats=pl,
            margin_sats=int(rec.get("margin_sats") or ct_obj.get("margin") or self.min_margin_sats),
            leverage=float(ct_obj.get("leverage") or rec.get("leverage") or self.profile.default_leverage),
            exact_fee_sats=open_fee + close_fee,
            funding_fee_sats=funding_fee,
            entry_price=fill_px,
            decision_price=float(rec.get("limit_price") or fill_px),
            side="buy" if rec.get("side") == "b" else "sell",
            gross_includes_execution=True,
            exact_fee_is_explicit=has_explicit,
        )
        return int(econ["net_pnl_sats"]), open_fee, close_fee, funding_fee

    def step(
        self,
        now_ts: float,
        mtf_cache: Any,
        client: Any,
        running_trades: Optional[Sequence[Dict[str, Any]]],
        closed_trades: Optional[Sequence[Dict[str, Any]]],
        equity_sats: float,
        available_margin_sats: float,
        dd_a_pct: float,
        dd_global_pct: float,
        hard_floor_active: bool,
        micro_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Ejecuta 1 ciclo O(1) de reconciliación, TIME_STOP 16h y señalización LIMIT."""
        ts = float(now_ts)
        eq = max(1.0, float(equity_sats or 1.0))
        if self.budget is None:
            self.budget = LaneBudgetController(eq, config=self.budget_cfg)
        self.budget.e_global = eq
        self.budget.peak_global = max(self.budget.peak_global, eq)
        self.budget.dd_a = max(0.0, float(dd_a_pct or 0.0))
        self.budget.dd_global = max(0.0, float(dd_global_pct or 0.0))
        self.budget.sync_pause_state(ts)

        run_list = [t for t in (running_trades or ()) if isinstance(t, dict) and t.get("id")]
        run_by_id = {str(t["id"]): t for t in run_list}
        run_by_cid = {
            str(t.get("clientId") or t.get("client_id")): t
            for t in run_list
            if (t.get("clientId") or t.get("client_id"))
        }

        spot_px, ind_5m, ind_15m, ind_1h, ind_4h = self._get_spot_and_indicators(mtf_cache, run_list)
        dirty = False
        summary: Dict[str, Any] = {
            "filled": [],
            "cancelled": [],
            "closed": [],
            "time_stop_requested": [],
            "placed": None,
            "skip_reason": None,
        }

        # 1. Reconciliar órdenes LIMIT pendientes de Carril B
        for oid in list(self.pending_orders.keys()):
            rec = self.pending_orders[oid]
            matched = run_by_id.get(oid) or (run_by_cid.get(str(rec.get("client_id") or "")) if rec.get("client_id") else None)
            if matched is not None:
                tid = str(matched["id"])
                fill_px = float(matched.get("price") or matched.get("entryPrice") or rec.get("limit_price") or spot_px or 0.0)
                open_fee, _, _, _ = _extract_v3_fees(matched)
                self.pending_orders.pop(oid, None)
                active_rec = {
                    "trade_id": tid,
                    "client_id": rec.get("client_id"),
                    "side": rec["side"],
                    "margin_sats": int(matched.get("margin") or rec["margin_sats"]),
                    "leverage": float(matched.get("leverage") or rec["leverage"]),
                    "limit_price": float(rec["limit_price"]),
                    "fill_price": round(fill_px, 2),
                    "sl_price": float(rec["sl_price"]),
                    "tp_price": float(rec["tp_price"]),
                    "loss_at_sl_sats": float(rec["loss_at_sl_sats"]),
                    "entry_time": ts,
                    "micro_guard_shadow": rec.get("micro_guard_shadow"),
                    "opening_fee": int(open_fee),
                    "exit_reason": None,
                }
                self.active_trades[tid] = active_rec
                self._remember_ids(tid, rec.get("client_id"))
                self._emit_trade_event({
                    "ts": round(ts, 3),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "env": self.env_mode,
                    "event": "OPEN",
                    "lane": "B",
                    "exec_type": "LIMIT",
                    "trade_id": tid,
                    "client_id": rec.get("client_id"),
                    "side": "buy" if rec["side"] == "b" else "sell",
                    "limit_price": round(float(rec["limit_price"]), 2),
                    "fill_price": round(fill_px, 2),
                    "lnm_price": round(float(spot_px or fill_px), 2),
                    "margin_sats": active_rec["margin_sats"],
                    "leverage": active_rec["leverage"],
                    "micro_guard_shadow": rec.get("micro_guard_shadow"),
                    "opening_fee": int(open_fee),
                    "closing_fee": 0,
                    "funding": 0,
                    "net_pnl_sats": 0,
                    "exit_reason": None,
                })
                summary["filled"].append(tid)
                dirty = True
                continue

            cancel_reason = self._pending_cancel_reason(rec, spot_px, ts, bool(hard_floor_active))
            if cancel_reason is not None:
                try:
                    client.cancel_open_order(oid)
                    self.pending_orders.pop(oid, None)
                    summary["cancelled"].append({"order_id": oid, "reason": cancel_reason})
                    dirty = True
                except Exception:
                    pass

        # 2. Reconciliar cierres de Carril B
        disappeared = [tid for tid in list(self.active_trades.keys()) if tid not in run_by_id]
        if disappeared:
            c_rows = list(closed_trades) if closed_trades is not None else []
            if closed_trades is None:
                try:
                    c_rows = list(client.get_closed_trades(limit=50) or [])
                except Exception:
                    c_rows = []
            closed_map = {str(ct["id"]): ct for ct in c_rows if isinstance(ct, dict) and ct.get("id")}
            for tid in disappeared:
                rec = self.active_trades.get(tid) or {}
                ct_obj = closed_map.get(tid)
                if ct_obj is None:
                    wait_c = int(rec.get("settlement_wait_cycles") or 0) + 1
                    rec["settlement_wait_cycles"] = wait_c
                    dirty = True
                    if wait_c >= 8:
                        self.active_trades.pop(tid, None)
                    continue
                self.active_trades.pop(tid, None)
                net_pnl, open_fee, close_fee, funding_fee = self._compute_real_net_pnl(ct_obj, rec)
                self.budget.record_lane_b_trade_close(rec.get("side", "b"), float(net_pnl), ts)
                exit_reason = str(rec.get("exit_reason") or "").strip()
                if not exit_reason:
                    if bool(ct_obj.get("takeprofit_wi") or ct_obj.get("takeprofitWi")):
                        exit_reason = "TAKE_PROFIT"
                    elif bool(ct_obj.get("stoploss_wi") or ct_obj.get("stoplossWi")):
                        exit_reason = "STOP_LOSS"
                    elif bool(ct_obj.get("liquidated") or ct_obj.get("liquidationWi")):
                        exit_reason = "LIQUIDATION"
                    else:
                        exit_reason = "EXCHANGE_CLOSED"
                exit_px = float(spot_px or ct_obj.get("exitPrice") or ct_obj.get("exit_price") or rec.get("fill_price") or 0.0)
                self._emit_trade_event({
                    "ts": round(ts, 3),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "env": self.env_mode,
                    "event": "CLOSE",
                    "lane": "B",
                    "exec_type": "LIMIT",
                    "trade_id": tid,
                    "client_id": rec.get("client_id"),
                    "side": "buy" if rec.get("side") == "b" else "sell",
                    "limit_price": rec.get("limit_price"),
                    "fill_price": rec.get("fill_price"),
                    "lnm_price": round(exit_px, 2),
                    "micro_guard_shadow": rec.get("micro_guard_shadow"),
                    "opening_fee": int(open_fee),
                    "closing_fee": int(close_fee),
                    "funding": int(funding_fee),
                    "net_pnl_sats": int(net_pnl),
                    "exit_reason": exit_reason,
                })
                summary["closed"].append({"trade_id": tid, "net_pnl_sats": int(net_pnl), "exit_reason": exit_reason})
                dirty = True

        # 3. Revisar TIME_STOP (>= 16.0h) sin DEF_SCALP_LOCK ni BE 0%
        for tid, rec in list(self.active_trades.items()):
            if tid in run_by_id and not rec.get("close_requested"):
                held_sec = ts - float(rec.get("entry_time") or ts)
                if held_sec >= self.max_hold_sec:
                    try:
                        rec["exit_reason"] = "TIME_STOP"
                        rec["close_requested"] = True
                        client.close_trade(tid)
                        summary["time_stop_requested"].append(tid)
                        dirty = True
                    except Exception:
                        rec["close_requested"] = False

        # 4. Evaluar nueva señal (edge-triggered al cierre de vela 1m)
        closed_1m_ts = self._get_closed_1m_ts(mtf_cache)
        if hard_floor_active:
            summary["skip_reason"] = "HARD_FLOOR_ACTIVE"
        elif closed_1m_ts is None:
            summary["skip_reason"] = "NO_1M_CLOSED_BAR"
        elif self.last_eval_1m_ts is not None and abs(float(closed_1m_ts) - float(self.last_eval_1m_ts)) < 1e-6:
            summary["skip_reason"] = "SAME_1M_BAR"
        else:
            self.last_eval_1m_ts = float(closed_1m_ts)
            dirty = True
            macro_ok, macro_reason = self.check_macro_range_only(ind_1h, ind_4h, ind_5m=ind_5m)
            if not macro_ok:
                summary["skip_reason"] = macro_reason
            else:
                sig_res = self._detect_sweep_wide_signal(mtf_cache, spot_px, ind_5m, ind_15m)
                if sig_res is None:
                    summary["skip_reason"] = "NO_SIGNAL"
                else:
                    side, liq_sig = sig_res
                    last_side_ts = float(self.last_side_entry_ts.get(side, -1e18))
                    if (ts - last_side_ts) < float(self.profile.cooldown_same_side_sec):
                        summary["skip_reason"] = "SIDE_COOLDOWN"
                    else:
                        atr_5m = max(1.0, float(ind_5m.get("atr") or (spot_px * 0.0025)))
                        plan = self._compute_bracket_and_limit_plan(side, spot_px, atr_5m, liq_sig)
                        if plan is None:
                            summary["skip_reason"] = "INVALID_LIMIT_PLAN"
                        else:
                            open_b_all = list(self.active_trades.values()) + list(self.pending_orders.values())
                            n_open_a = sum(1 for t in run_list if not self.is_lane_b_trade(t))
                            n_b_buy = sum(1 for r in open_b_all if r.get("side") == "b")
                            n_b_sell = sum(1 for r in open_b_all if r.get("side") == "s")
                            open_margin_b = float(sum(int(r.get("margin_sats") or 0) for r in open_b_all))
                            open_loss_b = float(sum(float(r.get("loss_at_sl_sats") or 0.0) for r in open_b_all))

                            margin_sats, loss_at_sl = self._compute_margin_and_risk(
                                equity_sats=eq,
                                available_margin_sats=float(available_margin_sats),
                                limit_price=float(plan["limit_price"]),
                                sl_price=float(plan["stoploss"]),
                                open_margin_b_sats=open_margin_b,
                                open_loss_at_sl_b_sats=open_loss_b,
                            )
                            if margin_sats < self.min_margin_sats:
                                summary["skip_reason"] = "RISK_OR_MARGIN_TOO_SMALL"
                            else:
                                dec = self.budget.evaluate_lane_b_entry(
                                    side=side,
                                    now_ts=ts,
                                    n_open_a=n_open_a,
                                    n_open_b_buy=n_b_buy,
                                    n_open_b_sell=n_b_sell,
                                    avail_balance_sats=float(available_margin_sats),
                                    open_margin_b_sats=open_margin_b,
                                    margin_new_sats=float(margin_sats),
                                    open_loss_at_sl_b_sats=open_loss_b,
                                    loss_at_sl_new_sats=loss_at_sl,
                                )
                                if not dec.allowed:
                                    summary["skip_reason"] = dec.reason
                                else:
                                    mg_allowed, mg_shadow = self.evaluate_micro_guard(
                                        lane="B",
                                        side=side,
                                        price=spot_px,
                                        micro_snapshot=micro_snapshot,
                                        now_ts=ts,
                                    )
                                    if not mg_allowed:
                                        summary["skip_reason"] = f"MICRO_VETO:{mg_shadow.get('rule_code') or mg_shadow.get('reason')}"
                                    else:
                                        client_id = f"lb_t_{int(ts * 1000)}_{uuid.uuid4().hex[:8]}"
                                        res = client.open_limit_trade(
                                            side=side,
                                            margin=int(margin_sats),
                                            leverage=int(self.profile.default_leverage),
                                            price=float(plan["limit_price"]),
                                            stoploss=float(plan["stoploss"]),
                                            takeprofit=float(plan["takeprofit"]),
                                            client_id=client_id,
                                        )
                                        oid = str((res or {}).get("id") or client_id)
                                        self.pending_orders[oid] = {
                                            "order_id": oid,
                                            "client_id": client_id,
                                            "side": side,
                                            "margin_sats": int(margin_sats),
                                            "leverage": int(self.profile.default_leverage),
                                            "limit_price": float(plan["limit_price"]),
                                            "sl_price": float(plan["stoploss"]),
                                            "tp_price": float(plan["takeprofit"]),
                                            "invalidation_price": float(plan.get("invalidation_price") or 0.0),
                                            "ttl_sec": float(plan.get("ttl_sec") or self.limit_cfg.ttl_sec_5m),
                                            "loss_at_sl_sats": float(loss_at_sl),
                                            "created_at": ts,
                                            "micro_guard_shadow": mg_shadow,
                                        }
                                        self._remember_ids(oid, client_id)
                                        self.last_side_entry_ts[side] = ts
                                        summary["placed"] = {
                                            "order_id": oid,
                                            "client_id": client_id,
                                            "side": side,
                                            "limit_price": float(plan["limit_price"]),
                                            "stoploss": float(plan["stoploss"]),
                                            "takeprofit": float(plan["takeprofit"]),
                                            "margin_sats": int(margin_sats),
                                            "micro_guard_shadow": mg_shadow,
                                        }
                                        dirty = True
        if dirty:
            self.save_state()
        return summary

    def cancel_pending_for_shutdown(self, client: Any) -> int:
        cancelled = 0
        for oid in list(self.pending_orders.keys()):
            try:
                client.cancel_open_order(oid)
                self.pending_orders.pop(oid, None)
                cancelled += 1
            except Exception:
                pass
        if cancelled > 0:
            self.save_state()
        return cancelled
