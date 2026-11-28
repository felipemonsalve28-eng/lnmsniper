# -*- coding: utf-8 -*-
"""
simulation/scalp_sim_engine.py
Motor de simulación causal para Carril B (Scalp) sobre contrato inverso BTCUSD.
Módulo puro de simulación: cero I/O a disco, cero dependencias de red, stdlib + numpy + módulos del repo.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from guardian_execution_core import HardFloorGuardian, compat_np as np
from limit_execution_manager import (
    LimitExecutionConfig,
    compute_limit_entry_plan,
    round_to_tick,
)
from liquidity_radar import (
    DeterministicMTFAggregator,
    LiquidityConfig,
    LiquidityRadar,
    compute_opposite_pool_room,
)
from simulation.lane_budget import (
    LaneBudgetConfig,
    LaneBudgetController,
    compute_inverse_loss_at_sl_sats,
    compute_inverse_net_pnl_sats,
)
from simulation.sim_lnm_exchange import MicrostructureStressConfig


VALID_LOCK_MODES: Tuple[str, ...] = ("legacy", "pure_sl_tp", "delayed_75")
VALID_MACRO_FILTERS: Tuple[str, ...] = ("off", "counter_block", "range_only")
SCALP_PROFILE_ORDER: Tuple[str, ...] = (
    "SCALP_SWEEP_TIGHT",
    "SCALP_SWEEP_STD",
    "SCALP_VWAP_TD9",
    "SCALP_SWEEP_WIDE",
    "SCALP_SWEEP_EXT",
)


@dataclass(frozen=True)
class ScalpProfileConfig:
    """Contrato inmutable de geometría, gatillos y riesgo para un perfil de Carril B."""

    name: str
    min_sl_pct: float
    max_sl_pct: float
    atr_sl_mult: float
    min_tp_pct: float
    max_tp_pct: float
    target_rr: float
    retest_atr: float
    fallback_pullback_atr: float
    ttl_sec_5m: float
    ttl_sec_15m: float
    runaway_tp_frac: float = 0.55
    max_entry_offset_atr: float = 0.75
    invalidation_buffer_atr: float = 0.15
    tick_size_usd: float = 0.5
    min_liq_score: float = 0.36
    require_htf_or_super_pool: bool = False
    use_vwap_td9: bool = False
    min_wall_fee_ratio: float = 2.0
    cooldown_same_side_sec: float = 1800.0
    def_scalp_lock_enabled: bool = True
    def_scalp_lock_trigger_r: float = 1.15
    def_scalp_lock_offset_r: float = 0.18
    max_hold_sec: float = 18000.0
    base_risk_pct_b: float = 0.0035
    default_leverage: int = 25

    def __post_init__(self) -> None:
        if self.min_sl_pct < 0.0045 - 1e-12 or self.max_sl_pct > 0.0065 + 1e-12:
            raise ValueError(
                f"SL bounds fuera de rango [0.0045, 0.0065] en {self.name}: "
                f"[{self.min_sl_pct}, {self.max_sl_pct}]"
            )
        if self.min_sl_pct > self.max_sl_pct:
            raise ValueError(f"min_sl_pct > max_sl_pct en {self.name}")
        if self.min_tp_pct < 0.0095 - 1e-12 or self.max_tp_pct > 0.0160 + 1e-12:
            raise ValueError(
                f"TP bounds fuera de rango [0.0095, 0.0160] en {self.name}: "
                f"[{self.min_tp_pct}, {self.max_tp_pct}]"
            )
        if self.min_tp_pct > self.max_tp_pct:
            raise ValueError(f"min_tp_pct > max_tp_pct en {self.name}")
        if not (1.9 - 1e-12 <= self.target_rr <= 2.5 + 1e-12):
            raise ValueError(f"target_rr fuera de rango [1.9, 2.5] en {self.name}: {self.target_rr}")


SCALP_PROFILES: Dict[str, ScalpProfileConfig] = {
    "SCALP_SWEEP_TIGHT": ScalpProfileConfig(
        name="SCALP_SWEEP_TIGHT",
        min_sl_pct=0.0055,
        max_sl_pct=0.0058,
        atr_sl_mult=1.20,
        min_tp_pct=0.0125,
        max_tp_pct=0.0135,
        target_rr=2.25,
        retest_atr=0.08,
        fallback_pullback_atr=0.14,
        ttl_sec_5m=240.0,
        ttl_sec_15m=360.0,
        min_liq_score=0.36,
        require_htf_or_super_pool=False,
        use_vwap_td9=False,
        min_wall_fee_ratio=2.0,
        cooldown_same_side_sec=1800.0,
    ),
    "SCALP_SWEEP_STD": ScalpProfileConfig(
        name="SCALP_SWEEP_STD",
        min_sl_pct=0.0055,
        max_sl_pct=0.0062,
        atr_sl_mult=1.30,
        min_tp_pct=0.0128,
        max_tp_pct=0.0145,
        target_rr=2.30,
        retest_atr=0.10,
        fallback_pullback_atr=0.16,
        ttl_sec_5m=300.0,
        ttl_sec_15m=480.0,
        min_liq_score=0.42,
        require_htf_or_super_pool=False,
        use_vwap_td9=False,
        min_wall_fee_ratio=2.3,
        cooldown_same_side_sec=2700.0,
    ),
    "SCALP_VWAP_TD9": ScalpProfileConfig(
        name="SCALP_VWAP_TD9",
        min_sl_pct=0.0055,
        max_sl_pct=0.0060,
        atr_sl_mult=1.25,
        min_tp_pct=0.0125,
        max_tp_pct=0.0140,
        target_rr=2.20,
        retest_atr=0.12,
        fallback_pullback_atr=0.15,
        ttl_sec_5m=300.0,
        ttl_sec_15m=420.0,
        min_liq_score=0.25,
        require_htf_or_super_pool=False,
        use_vwap_td9=True,
        min_wall_fee_ratio=2.0,
        cooldown_same_side_sec=2400.0,
    ),
    "SCALP_SWEEP_WIDE": ScalpProfileConfig(
        name="SCALP_SWEEP_WIDE",
        min_sl_pct=0.0058,
        max_sl_pct=0.0062,
        atr_sl_mult=1.35,
        min_tp_pct=0.0140,
        max_tp_pct=0.0150,
        target_rr=2.42,
        retest_atr=0.10,
        fallback_pullback_atr=0.16,
        ttl_sec_5m=360.0,
        ttl_sec_15m=540.0,
        min_liq_score=0.40,
        require_htf_or_super_pool=False,
        use_vwap_td9=False,
        min_wall_fee_ratio=2.3,
        cooldown_same_side_sec=2700.0,
    ),
    "SCALP_SWEEP_EXT": ScalpProfileConfig(
        name="SCALP_SWEEP_EXT",
        min_sl_pct=0.0058,
        max_sl_pct=0.0065,
        atr_sl_mult=1.45,
        min_tp_pct=0.0140,
        max_tp_pct=0.0160,
        target_rr=2.45,
        retest_atr=0.08,
        fallback_pullback_atr=0.15,
        ttl_sec_5m=360.0,
        ttl_sec_15m=600.0,
        min_liq_score=0.45,
        require_htf_or_super_pool=True,
        use_vwap_td9=False,
        min_wall_fee_ratio=2.6,
        cooldown_same_side_sec=3600.0,
    ),
}


def get_scalp_profile(name: str) -> ScalpProfileConfig:
    """Devuelve la configuración congelada de un perfil Scalp registrado."""
    key = str(name or "").strip().upper()
    if key not in SCALP_PROFILES:
        raise ValueError(f"Perfil Scalp desconocido: {name!r}. Válidos: {SCALP_PROFILE_ORDER}")
    return SCALP_PROFILES[key]


def _normalize_side(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in ("b", "buy", "long"):
        return "b"
    if s in ("s", "sell", "short"):
        return "s"
    raise ValueError(f"Lado inválido: {side!r}")


def _round_nearest_tick(price: float, tick_size: float = 0.5) -> float:
    tick = max(0.01, float(tick_size))
    p = max(tick, float(price))
    return round(round(p / tick) * tick, 2)


def compute_scalp_inverse_pnl(
    side: str,
    margin_sats: float,
    leverage: float,
    entry_price: float,
    exit_price: float,
    fee_rate_open: float,
    fee_rate_close: float,
    funding_sats: float = 0.0,
) -> Dict[str, float]:
    """Contabilidad exacta inversa BTCUSD delegando a simulation.lane_budget."""
    res = compute_inverse_net_pnl_sats(
        side=side,
        margin_sats=margin_sats,
        leverage=leverage,
        entry_price=entry_price,
        exit_price=exit_price,
        fee_rate_open=fee_rate_open,
        fee_rate_close=fee_rate_close,
        funding_sats=funding_sats,
    )
    fees_sats = float(res["opening_fee_sats"]) + float(res["closing_fee_sats"])
    return {
        "gross_pnl_sats": float(res["gross_pnl_sats"]),
        "fees_sats": fees_sats,
        "opening_fee_sats": float(res["opening_fee_sats"]),
        "closing_fee_sats": float(res["closing_fee_sats"]),
        "funding_sats": float(res["funding_sats"]),
        "c_rt_sats": float(res["total_cost_sats"]),
        "net_pnl_sats": float(res["net_pnl_sats"]),
    }


def compute_scalp_loss_at_sl_sats(
    margin_sats: float,
    leverage: float,
    entry_price: float,
    sl_exec_price: float,
    fee_rate_open: float,
    fee_rate_close: float,
) -> float:
    """Calcula LossAtSL_sats delegando a simulation.lane_budget."""
    return float(
        compute_inverse_loss_at_sl_sats(
            margin_sats=margin_sats,
            leverage=leverage,
            entry_price=entry_price,
            sl_price=sl_exec_price,
            fee_rate_open=fee_rate_open,
            fee_rate_close=fee_rate_close,
        )
    )


def compute_fer(sum_net_pnl_sats: float, sum_c_rt_sats: float) -> float:
    """Fee Efficiency Ratio: Sum(NetPnL_sats) / Sum(C_rt_sats)."""
    denom = float(sum_c_rt_sats)
    if denom <= 0.0:
        return 0.0
    return float(sum_net_pnl_sats) / denom


class CausalScalpContextBuilder:
    """
    Constructor causal O(1) por barra 1m para contexto multi-TF, LiquidityRadar,
    SuperPools, SessionVWAP, TD9 y barridos HTF (45m/1h/SuperPool).
    """

    def __init__(
        self,
        max_history: int = 300,
        liq_config: Optional[LiquidityConfig] = None,
    ) -> None:
        self.agg = DeterministicMTFAggregator(max_history=max_history)
        self.radar = LiquidityRadar(
            liq_config or LiquidityConfig(enabled=True, shadow_only=False, opposite_wall_enabled=True)
        )
        self._atr_cache: Dict[str, Tuple[int, float]] = {}
        self._macro_cache: Dict[str, Tuple[int, float, Dict[str, Any]]] = {}

    def reset(self) -> None:
        max_h = self.agg.max_history
        self.agg = DeterministicMTFAggregator(max_history=max_h)
        self.radar.clear_cache()
        self._atr_cache.clear()
        self._macro_cache.clear()

    def _causal_atr(
        self,
        tf: str,
        spot_price: float,
        fallback_pct: float,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
    ) -> float:
        n = len(closes)
        if n < 2:
            return max(1.0, float(spot_price) * fallback_pct)

        start = max(1, n - 14)
        tr_sum = 0.0
        cnt = 0
        for i in range(start, n):
            h_i = float(highs[i])
            l_i = float(lows[i])
            c_prev = float(closes[i - 1])
            tr = max(h_i - l_i, abs(h_i - c_prev), abs(l_i - c_prev))
            tr_sum += tr
            cnt += 1
        return max(1.0, tr_sum / max(1, cnt))

    def _detect_htf_sweep_reclaim(
        self,
        hierarchy: Dict[str, Any],
        tf_ohlcv: Dict[str, Tuple[Sequence[float], Sequence[float], Sequence[float], Optional[float]]],
        atr_15m: float,
        close_px: float,
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        """Detecta causalmente barrido + reclaim en pools 45m/1h/SuperPool usando barras hasta t."""
        atr_ref = max(1.0, float(atr_15m))
        min_pierce = 0.05 * atr_ref
        max_pierce = 1.50 * atr_ref
        bull_cand: Optional[Dict[str, Any]] = None
        bear_cand: Optional[Dict[str, Any]] = None

        def _recent_extremes(tf_key: str) -> Optional[Tuple[float, float]]:
            entry = tf_ohlcv.get(tf_key)
            if entry is None or len(entry[2]) < 2:
                return None
            hs, ls = entry[0], entry[1]
            # Evaluar únicamente barra actual o recién cerrada (máx 2 barras recientes)
            w_h = max(float(hs[-1]), float(hs[-2]))
            w_l = min(float(ls[-1]), float(ls[-2]))
            return w_h, w_l

        low_pools = hierarchy.get("low_pools") or ()
        for p in low_pools:
            if not (getattr(p, "anchor_timeframe", "") in ("45m", "1h") or getattr(p, "is_super_pool", False)):
                continue
            tf_check = "15m" if (getattr(p, "is_super_pool", False) and getattr(p, "anchor_timeframe", "") not in ("45m", "1h")) else p.anchor_timeframe
            ext = _recent_extremes(tf_check) or _recent_extremes("15m")
            if ext is None:
                continue
            _, w_l = ext
            pierce = float(p.level) - w_l
            if min_pierce <= pierce <= max_pierce and close_px > float(p.level):
                cand = {
                    "level": round(float(p.level), 2),
                    "wick_extreme": round(w_l, 2),
                    "tf": str(p.anchor_timeframe),
                    "is_super_pool": bool(p.is_super_pool),
                    "touches": int(p.touches),
                    "weight": float(getattr(p, "weight", 1.0)),
                }
                if bull_cand is None or cand["weight"] > bull_cand["weight"]:
                    bull_cand = cand

        high_pools = hierarchy.get("high_pools") or ()
        for p in high_pools:
            if not (getattr(p, "anchor_timeframe", "") in ("45m", "1h") or getattr(p, "is_super_pool", False)):
                continue
            tf_check = "15m" if (getattr(p, "is_super_pool", False) and getattr(p, "anchor_timeframe", "") not in ("45m", "1h")) else p.anchor_timeframe
            ext = _recent_extremes(tf_check) or _recent_extremes("15m")
            if ext is None:
                continue
            w_h, _ = ext
            pierce = w_h - float(p.level)
            if min_pierce <= pierce <= max_pierce and close_px < float(p.level):
                cand = {
                    "level": round(float(p.level), 2),
                    "wick_extreme": round(w_h, 2),
                    "tf": str(p.anchor_timeframe),
                    "is_super_pool": bool(p.is_super_pool),
                    "touches": int(p.touches),
                    "weight": float(getattr(p, "weight", 1.0)),
                }
                if bear_cand is None or cand["weight"] > bear_cand["weight"]:
                    bear_cand = cand

        return {"bull": bull_cand, "bear": bear_cand}

    def _causal_macro_ind(
        self,
        tf: str,
        fallback_pct: float,
    ) -> Dict[str, Any]:
        """Calcula indicadores macro causales 1h/4h EXCLUSIVAMENTE sobre barras cerradas."""
        _, h_tf, l_tf, c_tf, _, ts_tf = self.agg.get_ohlcv(tf, include_forming=False)
        n_closed = len(c_tf)
        if n_closed < 3:
            return {"regime": "RANGE", "market_regime": "RANGE", "ema_score": 0.0}

        last_ts = (
            float(ts_tf[-1])
            if isinstance(ts_tf, (list, tuple)) and len(ts_tf) > 0
            else float(ts_tf or 0.0)
        )
        cached = self._macro_cache.get(tf)
        if cached is not None and cached[0] == n_closed and cached[1] == last_ts:
            return dict(cached[2])

        def _ema(vals: Sequence[float], span: int) -> float:
            alpha = 2.0 / (float(span) + 1.0)
            e = float(vals[0])
            for x in vals[1:]:
                e = alpha * float(x) + (1.0 - alpha) * e
            return e

        c_last = max(1.0, float(c_tf[-1]))
        atr_val = self._causal_atr(tf, c_last, fallback_pct, h_tf, l_tf, c_tf)
        atr_pct = atr_val / c_last
        ema9 = _ema(c_tf, 9)
        ema21 = _ema(c_tf, 21)
        ema50 = _ema(c_tf, 50)

        if ema9 > ema21 >= ema50:
            ema_score = 1.0
        elif ema9 < ema21 <= ema50:
            ema_score = -1.0
        else:
            ema_score = 0.0

        if atr_pct >= 0.015:
            regime = "EXTREME_VOLATILITY"
        elif ema9 > ema21 and abs(ema9 - ema21) / c_last >= 0.0015:
            regime = "TREND_BULL"
        elif ema9 < ema21 and abs(ema9 - ema21) / c_last >= 0.0015:
            regime = "TREND_BEAR"
        else:
            regime = "RANGE"

        out: Dict[str, Any] = {
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "atr": atr_val,
            "atr_pct": atr_pct,
            "ema_score": ema_score,
            "regime": regime,
            "market_regime": regime,
        }
        self._macro_cache[tf] = (n_closed, last_ts, out)
        return dict(out)

    def step(
        self,
        ts: float,
        open_px: float,
        high_px: float,
        low_px: float,
        close_px: float,
        volume: float = 0.0,
        ind_5m: Optional[Dict[str, Any]] = None,
        ind_15m: Optional[Dict[str, Any]] = None,
        perception: Optional[Dict[str, Any]] = None,
        ind_1h: Optional[Dict[str, Any]] = None,
        ind_4h: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ts_f = float(ts)
        c_f = max(1.0, float(close_px))
        self.agg.push_1m(ts_f, float(open_px), float(high_px), float(low_px), c_f, float(volume), is_closed=True)

        o5, h5, l5, c5, v5, ts5 = self.agg.get_ohlcv("5m", include_forming=True)
        o15, h15, l15, c15, v15, ts15 = self.agg.get_ohlcv("15m", include_forming=True)

        atr_5m_ext = float((ind_5m or {}).get("atr") or 0.0)
        atr_15m_ext = float((ind_15m or {}).get("atr") or 0.0)
        atr_5m = atr_5m_ext if atr_5m_ext > 0.0 else self._causal_atr("5m", c_f, 0.0025, h5, l5, c5)
        atr_15m = atr_15m_ext if atr_15m_ext > 0.0 else self._causal_atr("15m", c_f, 0.0040, h15, l15, c15)

        liq_sig = self.radar.evaluate(
            candles_5m=(h5, l5, c5) if len(c5) >= 3 else None,
            candles_15m=(h15, l15, c15) if len(c15) >= 3 else None,
            atr_5m=atr_5m,
            atr_15m=atr_15m,
            spot_price=c_f,
            opens_5m=o5 if len(o5) >= 3 else None,
            opens_15m=o15 if len(o15) >= 3 else None,
            volumes_5m=v5 if len(v5) >= 3 else None,
            volumes_15m=v15 if len(v15) >= 3 else None,
            bar_ts_5m=ts5 if len(c5) >= 3 else None,
            bar_ts_15m=ts15 if len(c15) >= 3 else None,
        )

        liq_sig_15m = None
        if liq_sig.get("timeframe") == "5m" and len(c15) >= 3:
            liq_sig_15m = self.radar.evaluate(
                candles_5m=None,
                candles_15m=(h15, l15, c15),
                atr_5m=atr_5m,
                atr_15m=atr_15m,
                spot_price=c_f,
                opens_15m=o15,
                volumes_15m=v15,
                bar_ts_15m=ts15,
            )

        tf_ohlcv: Dict[str, Tuple[Sequence[float], Sequence[float], Sequence[float], Optional[float]]] = {}
        atr_by_tf: Dict[str, float] = {"15m": atr_15m}
        for tf_k, fb_pct in (("15m", 0.0040), ("30m", 0.0050), ("45m", 0.0060), ("1h", 0.0070), ("4h", 0.0120)):
            if tf_k == "15m":
                _h, _l, _c, _ts = h15, l15, c15, ts15
            else:
                _, _h, _l, _c, _, _ts = self.agg.get_ohlcv(tf_k, include_forming=True)
            if len(_c) > 0:
                tf_ohlcv[tf_k] = (_h, _l, _c, _ts)
                if tf_k != "15m":
                    atr_by_tf[tf_k] = self._causal_atr(tf_k, c_f, fb_pct, _h, _l, _c)

        hierarchy = self.radar.evaluate_multi_tf_hierarchy(
            tf_ohlcv=tf_ohlcv,
            atr_ref=atr_15m,
            spot_price=c_f,
            atr_by_tf=atr_by_tf,
        )

        vwap_raw = self.agg.get_session_vwap()
        v_px = float(vwap_raw.get("vwap") or 0.0)
        v_std = float(vwap_raw.get("std") or 0.0)
        vwap_out = dict(vwap_raw)
        vwap_out["upper_1_5s"] = v_px + (1.5 * v_std)
        vwap_out["lower_1_5s"] = v_px - (1.5 * v_std)

        td9_out = self.agg.get_td9_state()
        htf_sweeps = self._detect_htf_sweep_reclaim(hierarchy, tf_ohlcv, atr_15m, c_f)
        eff_ind_1h = ind_1h if isinstance(ind_1h, dict) else self._causal_macro_ind("1h", 0.0070)
        eff_ind_4h = ind_4h if isinstance(ind_4h, dict) else self._causal_macro_ind("4h", 0.0120)

        return {
            "ts": ts_f,
            "spot_price": c_f,
            "atr_5m": atr_5m,
            "atr_15m": atr_15m,
            "liquidity_signal": liq_sig,
            "liquidity_signal_15m": liq_sig_15m or liq_sig,
            "hierarchy": hierarchy,
            "vwap": vwap_out,
            "td9": td9_out,
            "htf_sweeps": htf_sweeps,
            "ind_1h": eff_ind_1h,
            "ind_4h": eff_ind_4h,
            "perception": perception if isinstance(perception, dict) else {},
        }


def _extract_bar_fields(step_item: Any) -> Tuple[float, float, float, float, float, float, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Extrae (ts, open, high, low, close, volume, ind_5m, ind_15m, perception) de Candle o tuple de steps."""
    ind_5m: Optional[Dict[str, Any]] = None
    ind_15m: Optional[Dict[str, Any]] = None
    perception: Optional[Dict[str, Any]] = None
    candle_obj = step_item
    if isinstance(step_item, (tuple, list)):
        candle_obj = step_item[0]
        if len(step_item) > 1 and isinstance(step_item[1], dict):
            ind_5m = step_item[1]
        if len(step_item) > 2 and isinstance(step_item[2], dict):
            ind_15m = step_item[2]
        if len(step_item) > 5 and isinstance(step_item[5], dict):
            perception = step_item[5]

    if isinstance(candle_obj, dict):
        ts = float(candle_obj.get("timestamp", candle_obj.get("time", 0.0)))
        o = float(candle_obj["open"])
        h = float(candle_obj["high"])
        l = float(candle_obj["low"])
        c = float(candle_obj["close"])
        v = float(candle_obj.get("volume", 0.0))
    else:
        ts = float(candle_obj.timestamp)
        o = float(candle_obj.open)
        h = float(candle_obj.high)
        l = float(candle_obj.low)
        c = float(candle_obj.close)
        v = float(getattr(candle_obj, "volume", 0.0))
    return ts, o, h, l, c, v, ind_5m, ind_15m, perception


def _extract_bar_fields_full(
    step_item: Any,
) -> Tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
]:
    """Extrae (ts, open, high, low, close, volume, ind_5m, ind_15m, ind_1h, ind_4h, perception)."""
    ts, o, h, l, c, v, ind_5m, ind_15m, perception = _extract_bar_fields(step_item)
    ind_1h: Optional[Dict[str, Any]] = None
    ind_4h: Optional[Dict[str, Any]] = None
    if isinstance(step_item, (tuple, list)):
        if len(step_item) > 3 and isinstance(step_item[3], dict):
            ind_1h = step_item[3]
        if len(step_item) > 4 and isinstance(step_item[4], dict):
            ind_4h = step_item[4]
    return ts, o, h, l, c, v, ind_5m, ind_15m, ind_1h, ind_4h, perception


class ScalpSimEngine:
    """
    Motor de simulación causal para Carril B con ejecución LIMIT/MARKET,
    contabilidad inversa BTCUSD, DEF_SCALP_LOCK, LaneBudgetController y HardFloorGuardian.
    """

    def __init__(
        self,
        profile: Union[str, ScalpProfileConfig] = "SCALP_SWEEP_STD",
        exec_mode: str = "LIMIT",
        fee_rt_bps: float = 16.0,
        stress_config: Optional[MicrostructureStressConfig] = None,
        initial_balance_sats: float = 100_000,
        lane_config: Optional[LaneBudgetConfig] = None,
        funding_rate_8h: float = 0.0001,
        lock_mode: str = "pure_sl_tp",
        macro_filter: str = "off",
        dynamic_risk_clamp: bool = True,
        rolling_pause_reset: bool = False,
    ) -> None:
        self.profile: ScalpProfileConfig = (
            profile if isinstance(profile, ScalpProfileConfig) else get_scalp_profile(profile)
        )
        mode_up = str(exec_mode or "LIMIT").strip().upper()
        if mode_up not in ("LIMIT", "MARKET"):
            raise ValueError(f"exec_mode inválido: {exec_mode!r} (esperado LIMIT o MARKET)")
        self.exec_mode: str = mode_up
        self.fee_rt_bps: float = float(fee_rt_bps)
        self.fee_rate_open: float = max(0.0, self.fee_rt_bps / 20000.0)
        self.fee_rate_close: float = max(0.0, self.fee_rt_bps / 20000.0)
        self.stress_config: Optional[MicrostructureStressConfig] = stress_config
        self.initial_balance_sats: float = float(initial_balance_sats)

        lk = str(lock_mode or "").strip().lower()
        if lk not in VALID_LOCK_MODES:
            raise ValueError(f"lock_mode inválido: {lock_mode!r}. Válidos: {VALID_LOCK_MODES}")
        self.lock_mode: str = lk

        mf = str(macro_filter or "").strip().lower()
        if mf not in VALID_MACRO_FILTERS:
            raise ValueError(f"macro_filter inválido: {macro_filter!r}. Válidos: {VALID_MACRO_FILTERS}")
        self.macro_filter: str = mf

        self.dynamic_risk_clamp: bool = bool(dynamic_risk_clamp)
        self.rolling_pause_reset: bool = bool(rolling_pause_reset)

        if lane_config is None:
            self.lane_config: LaneBudgetConfig = LaneBudgetConfig(
                enabled=True,
                rolling_pause_reset=bool(rolling_pause_reset),
            )
        elif bool(rolling_pause_reset) and not lane_config.rolling_pause_reset:
            self.lane_config = replace(lane_config, rolling_pause_reset=True)
        else:
            self.lane_config = lane_config

        self.funding_rate_8h: float = float(funding_rate_8h)

        self.guardian = HardFloorGuardian(
            initial_floor_ratio=0.75,
            milestone_floor_ratio=0.88,
            max_dd_pct=0.052,
            max_daily_loss_pct=0.11,
            kill_switch_dd_pct=0.052,
        )
        self._limit_cfg = LimitExecutionConfig(
            enabled=(self.exec_mode == "LIMIT"),
            tick_size_usd=self.profile.tick_size_usd,
            ttl_sec_5m=self.profile.ttl_sec_5m,
            ttl_sec_15m=self.profile.ttl_sec_15m,
            runaway_tp_frac=self.profile.runaway_tp_frac,
            max_entry_offset_atr=self.profile.max_entry_offset_atr,
            retest_atr_sniper=self.profile.retest_atr,
            retest_atr_reversion=self.profile.retest_atr,
            retest_atr_scalp=self.profile.retest_atr,
            fallback_pullback_atr=self.profile.fallback_pullback_atr,
            invalidation_buffer_atr=self.profile.invalidation_buffer_atr,
            min_sweep_align_sniper=self.profile.min_liq_score,
            min_sweep_align_reversion=self.profile.min_liq_score,
        )
        self._ctx_builder: Optional[CausalScalpContextBuilder] = None
        self.reset()

    def reset(self) -> None:
        self.lane_budget = LaneBudgetController(
            initial_equity_sats=self.initial_balance_sats,
            config=self.lane_config,
        )
        self.running_trades: List[Dict[str, Any]] = []
        self.pending_orders: List[Dict[str, Any]] = []
        self.closed_trades: List[Dict[str, Any]] = []
        self.rejected_reasons: Counter = Counter()

        self.current_bar_idx: int = -1
        self.current_ts: float = 0.0
        self.last_close: float = 0.0

        self.signals_total: int = 0
        self.orders_submitted: int = 0
        self.orders_filled: int = 0
        self.limit_submitted: int = 0
        self.limit_filled: int = 0
        self.cancelled_ttl: int = 0
        self.cancelled_runaway: int = 0
        self.cancelled_invalidated: int = 0
        self.clamped_entries: int = 0

        self.last_entry_ts_by_side: Dict[str, float] = {"b": -1e18, "s": -1e18}
        self._edge_latch_side: Optional[str] = None
        self._edge_anchor_ts: Dict[Tuple[str, str], float] = {}
        self._seq_id: int = 0

        self.peak_equity_mtm: float = self.initial_balance_sats
        self.locked_floor: float = 0.75 * self.initial_balance_sats
        self.daily_utc_day: Optional[int] = None
        self.daily_start_equity: float = self.initial_balance_sats
        self.max_seen_dd_global: float = 0.0
        self.max_seen_dd_b: float = 0.0
        self.max_seen_dd_a: float = 0.0
        self.dd_guard_events: int = 0
        self.hard_floor_violated: bool = False
        self.halted: bool = False
        self._last_throttle_active: bool = False
        if self._ctx_builder is not None:
            self._ctx_builder.reset()

    def _reject(self, reason: str) -> None:
        r_str = str(reason)
        if r_str.startswith("LIMIT_OFFSET_TOO_FAR"):
            r_str = "LIMIT_OFFSET_TOO_FAR"
        self.rejected_reasons[r_str] += 1

    def _compute_sl_stress_price(
        self,
        fill_price: float,
        side: str,
        atr_5m: float,
        candle_range_pct: float,
    ) -> float:
        if (
            self.stress_config is None
            or not self.stress_config.enabled
            or not self.stress_config.apply_to_sl
        ):
            return float(fill_price)
        slip = self.stress_config.compute_slip_ratio(
            ref_price=fill_price,
            atr_5m=atr_5m,
            candle_range_pct=candle_range_pct,
        )
        a_side = 1.0 if side == "b" else -1.0
        return max(1.0, float(fill_price) * (1.0 - a_side * slip))

    def _compute_entry_stress_price(
        self,
        fill_price: float,
        side: str,
        atr_5m: float,
        candle_range_pct: float,
    ) -> float:
        if self.stress_config is None or not self.stress_config.enabled:
            return float(fill_price)
        slip = self.stress_config.compute_slip_ratio(
            ref_price=fill_price,
            atr_5m=atr_5m,
            candle_range_pct=candle_range_pct,
        )
        a_side = 1.0 if side == "b" else -1.0
        return max(1.0, float(fill_price) * (1.0 + a_side * slip))

    @staticmethod
    def _calc_liq_price(entry_price: float, side: str, leverage: float) -> float:
        lev = max(1.0, float(leverage))
        if side == "b":
            return round(entry_price / (1.0 + (0.99 / lev)), 2)
        return round(entry_price / max(0.05, 1.0 - (0.99 / lev)), 2)

    def _close_trade_record(
        self,
        trade: Dict[str, Any],
        exit_price: float,
        reason: str,
        close_ts: float,
    ) -> Dict[str, Any]:
        pnl = compute_scalp_inverse_pnl(
            side=trade["side"],
            margin_sats=trade["margin_sats"],
            leverage=trade["leverage"],
            entry_price=trade["entry_price"],
            exit_price=exit_price,
            fee_rate_open=self.fee_rate_open,
            fee_rate_close=self.fee_rate_close,
            funding_sats=trade["funding_sats"],
        )
        self.lane_budget.record_lane_b_trade_close(
            side=trade["side"],
            net_pnl_sats=pnl["net_pnl_sats"],
            now_ts=close_ts,
        )
        self.max_seen_dd_b = max(self.max_seen_dd_b, self.lane_budget.dd_b)
        self.max_seen_dd_a = max(self.max_seen_dd_a, self.lane_budget.dd_a)

        gross_pct = (
            100.0
            * (1.0 - float(trade["entry_price"]) / max(1e-9, float(exit_price)))
            * (1.0 if trade["side"] == "b" else -1.0)
        )
        rec = {
            "id": trade["id"],
            "profile": self.profile.name,
            "exec_mode": trade["exec_mode"],
            "side": trade["side"],
            "margin_sats": trade["margin_sats"],
            "leverage": trade["leverage"],
            "entry_price": trade["entry_price"],
            "exit_price": float(exit_price),
            "sl_price": trade["sl_price"],
            "tp_price": trade["tp_price"],
            "lock_active": bool(trade.get("lock_active", False)),
            "gross_pct": gross_pct,
            "gross_pnl_sats": pnl["gross_pnl_sats"],
            "fees_sats": pnl["fees_sats"],
            "opening_fee_sats": pnl["opening_fee_sats"],
            "closing_fee_sats": pnl["closing_fee_sats"],
            "funding_sats": pnl["funding_sats"],
            "c_rt_sats": pnl["c_rt_sats"],
            "net_pnl_sats": pnl["net_pnl_sats"],
            "close_reason": reason,
            "open_ts": trade["open_ts"],
            "close_ts": float(close_ts),
            "duration_sec": max(0.0, float(close_ts) - trade["open_ts"]),
        }
        self.closed_trades.append(rec)
        return rec

    def _evaluate_trade_intrabar(
        self,
        trade: Dict[str, Any],
        open_px: float,
        high_px: float,
        low_px: float,
        close_px: float,
        ts: float,
        atr_5m: float,
        candle_range_pct: float,
        *,
        is_fill_bar: bool,
    ) -> bool:
        """Evalúa SL/Liquidation/TP sobre la barra t con desempate conservador SL-first. Retorna True si cerró."""
        side = trade["side"]
        sl = float(trade["sl_price"])
        tp = float(trade["tp_price"])
        liq = float(trade["liq_price"])
        ref_open = float(trade["entry_price"]) if is_fill_bar else float(open_px)

        gap_to_liq = (ref_open <= liq) if side == "b" else (ref_open >= liq)
        sl_protects_before_liq = (
            sl > 0.0
            and not gap_to_liq
            and ((side == "b" and sl > liq) or (side == "s" and sl < liq))
        )

        sl_hit = (sl > 0.0) and ((low_px <= sl) if side == "b" else (high_px >= sl))
        tp_hit = (tp > 0.0) and ((high_px >= tp) if side == "b" else (low_px <= tp))
        liq_hit = (liq > 0.0) and ((low_px <= liq) if side == "b" else (high_px >= liq))
        sl_reason = "DEF_SCALP_LOCK" if bool(trade.get("lock_active", False)) else "STOP_LOSS"

        if sl_protects_before_liq and sl_hit:
            fill_px = min(ref_open, sl) if side == "b" else max(ref_open, sl)
            fill_px = self._compute_sl_stress_price(fill_px, side, atr_5m, candle_range_pct)
            self._close_trade_record(trade, fill_px, sl_reason, ts)
            return True

        if liq_hit and not sl_protects_before_liq:
            self._close_trade_record(trade, liq, "LIQUIDATION", ts)
            return True

        if sl_hit:
            fill_px = min(ref_open, sl) if side == "b" else max(ref_open, sl)
            fill_px = self._compute_sl_stress_price(fill_px, side, atr_5m, candle_range_pct)
            self._close_trade_record(trade, fill_px, sl_reason, ts)
            return True

        if tp_hit:
            self._close_trade_record(trade, tp, "TAKE_PROFIT", ts)
            return True

        return False

    def _process_running_trades(
        self,
        bar_idx: int,
        ts: float,
        open_px: float,
        high_px: float,
        low_px: float,
        close_px: float,
        atr_5m: float,
        candle_range_pct: float,
    ) -> None:
        survivors: List[Dict[str, Any]] = []
        for trade in self.running_trades:
            if trade["created_bar_idx"] >= bar_idx:
                survivors.append(trade)
                continue

            # 1. Funding cada 8h (28,800s)
            dt_fund = ts - float(trade["last_funding_ts"])
            if dt_fund >= 28800.0:
                blocks = int(dt_fund // 28800.0)
                notional = float(trade["margin_sats"]) * float(trade["leverage"])
                trade["funding_sats"] += notional * self.funding_rate_8h * blocks
                trade["last_funding_ts"] += blocks * 28800.0

            # 2. SL & TP conservador (SL-first en colisión intrabar)
            closed = self._evaluate_trade_intrabar(
                trade,
                open_px,
                high_px,
                low_px,
                close_px,
                ts,
                atr_5m,
                candle_range_pct,
                is_fill_bar=False,
            )
            if closed:
                continue

            # 3. Time stop intradía (max_hold_sec)
            if (ts - float(trade["open_ts"])) >= self.profile.max_hold_sec:
                self._close_trade_record(trade, close_px, "TIME_STOP", ts)
                continue

            # 4. DEF_SCALP_LOCK al cierre de barra t (ratchet SL para t+1) según lock_mode
            if self.lock_mode == "legacy" and self.profile.def_scalp_lock_enabled:
                r_dist = float(trade["initial_sl_dist"])
                if r_dist > 0.0:
                    entry = float(trade["entry_price"])
                    trig = self.profile.def_scalp_lock_trigger_r * r_dist
                    off = self.profile.def_scalp_lock_offset_r * r_dist
                    if trade["side"] == "b":
                        if close_px >= entry + trig:
                            new_sl = _round_nearest_tick(entry + off, self.profile.tick_size_usd)
                            if new_sl > float(trade["sl_price"]) and new_sl < close_px:
                                trade["sl_price"] = new_sl
                                trade["lock_active"] = True
                    else:
                        if close_px <= entry - trig:
                            new_sl = _round_nearest_tick(entry - off, self.profile.tick_size_usd)
                            if new_sl < float(trade["sl_price"]) and new_sl > close_px:
                                trade["sl_price"] = new_sl
                                trade["lock_active"] = True
            elif self.lock_mode == "delayed_75" and self.profile.def_scalp_lock_enabled:
                entry = float(trade["entry_price"])
                tp_dist = float(trade.get("initial_tp_dist") or abs(float(trade["tp_price"]) - entry))
                if tp_dist > 0.0:
                    trig = 0.75 * tp_dist
                    fee_dist = entry * (self.fee_rate_open + self.fee_rate_close)
                    off = min(0.68 * tp_dist, max(0.50 * tp_dist, 0.42 * tp_dist + fee_dist))
                    if trade["side"] == "b":
                        if close_px >= entry + trig:
                            new_sl = _round_nearest_tick(entry + off, self.profile.tick_size_usd)
                            if new_sl > float(trade["sl_price"]) and new_sl < close_px:
                                trade["sl_price"] = new_sl
                                trade["lock_active"] = True
                    else:
                        if close_px <= entry - trig:
                            new_sl = _round_nearest_tick(entry - off, self.profile.tick_size_usd)
                            if new_sl < float(trade["sl_price"]) and new_sl > close_px:
                                trade["sl_price"] = new_sl
                                trade["lock_active"] = True

            survivors.append(trade)

        self.running_trades = survivors

    def _process_pending_orders(
        self,
        bar_idx: int,
        ts: float,
        open_px: float,
        high_px: float,
        low_px: float,
        close_px: float,
        atr_5m: float,
        candle_range_pct: float,
        ovr_side: Optional[str],
    ) -> None:
        remaining: List[Dict[str, Any]] = []
        tick = self.profile.tick_size_usd

        for order in self.pending_orders:
            if order["created_bar_idx"] >= bar_idx:
                remaining.append(order)
                continue

            if self.halted:
                self._reject("SAFETY_OR_SHUTDOWN")
                continue

            side = order["side"]
            if order["exec_mode"] == "MARKET":
                entry_px = self._compute_entry_stress_price(open_px, side, atr_5m, candle_range_pct)
                sl_dist = abs(float(order["ref_price"]) - float(order["sl_price"]))
                tp_dist = abs(float(order["tp_price"]) - float(order["ref_price"]))
                sl_px = _round_nearest_tick((entry_px - sl_dist) if side == "b" else (entry_px + sl_dist), tick)
                tp_px = _round_nearest_tick((entry_px + tp_dist) if side == "b" else (entry_px - tp_dist), tick)
                loss_sl = compute_scalp_loss_at_sl_sats(
                    order["margin_sats"],
                    order["leverage"],
                    entry_px,
                    sl_px,
                    self.fee_rate_open,
                    self.fee_rate_close,
                )
                trade = {
                    "id": order["id"],
                    "exec_mode": "MARKET",
                    "side": side,
                    "margin_sats": order["margin_sats"],
                    "leverage": order["leverage"],
                    "entry_price": float(entry_px),
                    "sl_price": float(sl_px),
                    "tp_price": float(tp_px),
                    "initial_sl_dist": abs(entry_px - sl_px),
                    "initial_tp_dist": abs(float(tp_px) - float(entry_px)),
                    "loss_at_sl_sats": loss_sl,
                    "liq_price": self._calc_liq_price(entry_px, side, order["leverage"]),
                    "open_ts": ts,
                    "last_funding_ts": ts,
                    "funding_sats": 0.0,
                    "created_bar_idx": bar_idx,
                    "lock_active": False,
                }
                self.orders_filled += 1
                closed = self._evaluate_trade_intrabar(
                    trade,
                    open_px,
                    high_px,
                    low_px,
                    close_px,
                    ts,
                    atr_5m,
                    candle_range_pct,
                    is_fill_bar=True,
                )
                if not closed:
                    self.running_trades.append(trade)
                continue

            # Orden LIMIT
            age = ts - float(order["created_at"])
            if age >= float(order["ttl_sec"]):
                self.cancelled_ttl += 1
                self._reject("TTL_EXPIRED")
                continue

            p_inv = float(order["invalidation_price"])
            if p_inv > 0.0 and ((side == "b" and open_px < p_inv) or (side == "s" and open_px > p_inv)):
                self.cancelled_invalidated += 1
                self._reject("WICK_INVALIDATED")
                continue

            p_lim = float(order["limit_price"])
            filled = (low_px <= (p_lim - tick + 1e-9)) if side == "b" else (high_px >= (p_lim + tick - 1e-9))
            if filled:
                self.orders_filled += 1
                self.limit_filled += 1
                sl_px = float(order["sl_price"])
                tp_px = float(order["tp_price"])
                trade = {
                    "id": order["id"],
                    "exec_mode": "LIMIT",
                    "side": side,
                    "margin_sats": order["margin_sats"],
                    "leverage": order["leverage"],
                    "entry_price": p_lim,
                    "sl_price": sl_px,
                    "tp_price": tp_px,
                    "initial_sl_dist": abs(p_lim - sl_px),
                    "initial_tp_dist": abs(tp_px - p_lim),
                    "loss_at_sl_sats": float(order["loss_at_sl_sats"]),
                    "liq_price": self._calc_liq_price(p_lim, side, order["leverage"]),
                    "open_ts": ts,
                    "last_funding_ts": ts,
                    "funding_sats": 0.0,
                    "created_bar_idx": bar_idx,
                    "lock_active": False,
                }
                closed = self._evaluate_trade_intrabar(
                    trade,
                    open_px,
                    high_px,
                    low_px,
                    close_px,
                    ts,
                    atr_5m,
                    candle_range_pct,
                    is_fill_bar=True,
                )
                if not closed:
                    self.running_trades.append(trade)
                continue

            # No llenada en barra t: revisar invalidación y runaway guard
            wick_inv = p_inv > 0.0 and (
                (low_px < p_inv or close_px < p_inv)
                if side == "b"
                else (high_px > p_inv or close_px > p_inv)
            )
            ovr_flip = (side == "b" and ovr_side == "BEAR") or (side == "s" and ovr_side == "BULL")
            if wick_inv or ovr_flip:
                self.cancelled_invalidated += 1
                self._reject("OVR_FLIP" if ovr_flip else "WICK_INVALIDATED")
                continue

            fav_px = max(high_px, close_px) if side == "b" else min(low_px, close_px)
            tp_span = abs(float(order["tp_price"]) - p_lim)
            if tp_span > 1e-6:
                progress = ((fav_px - p_lim) / tp_span) if side == "b" else ((p_lim - fav_px) / tp_span)
                if progress >= self.profile.runaway_tp_frac:
                    self.cancelled_runaway += 1
                    self._reject("RUNAWAY_GUARD")
                    continue

            remaining.append(order)

        self.pending_orders = remaining

    def _evaluate_guardian(self, ts: float, close_px: float) -> None:
        floating_pnl = 0.0
        locked_margin = 0.0
        for t in self.running_trades:
            locked_margin += float(t["margin_sats"])
            pnl = compute_scalp_inverse_pnl(
                side=t["side"],
                margin_sats=t["margin_sats"],
                leverage=t["leverage"],
                entry_price=t["entry_price"],
                exit_price=close_px,
                fee_rate_open=self.fee_rate_open,
                fee_rate_close=self.fee_rate_close,
                funding_sats=t["funding_sats"],
            )
            floating_pnl += pnl["net_pnl_sats"]

        mtm_equity = float(self.lane_budget.e_global) + floating_pnl
        if mtm_equity > self.peak_equity_mtm:
            self.peak_equity_mtm = mtm_equity
        if self.peak_equity_mtm >= 1.15 * self.initial_balance_sats:
            self.locked_floor = max(self.locked_floor, 0.88 * self.peak_equity_mtm)

        day = int(ts) // 86400
        if self.daily_utc_day is None or day > self.daily_utc_day:
            self.daily_utc_day = day
            self.daily_start_equity = max(1.0, mtm_equity)
        daily_pnl_pct = (mtm_equity - self.daily_start_equity) / max(1.0, self.daily_start_equity)
        avail_bal = max(0.0, mtm_equity - locked_margin)

        prot = self.guardian.evaluate_protection(
            equity=mtm_equity,
            available_balance=avail_bal,
            peak_equity=self.peak_equity_mtm,
            locked_floor=self.locked_floor,
            daily_pnl_pct=daily_pnl_pct,
        )
        cur_dd = float(prot["current_dd_pct"])
        self.max_seen_dd_global = max(self.max_seen_dd_global, cur_dd, self.lane_budget.dd_global)
        self.max_seen_dd_b = max(self.max_seen_dd_b, self.lane_budget.dd_b)
        self.max_seen_dd_a = max(self.max_seen_dd_a, self.lane_budget.dd_a)

        throttle_hit = cur_dd >= 0.045
        if throttle_hit and not self._last_throttle_active:
            self.dd_guard_events += 1
        self._last_throttle_active = throttle_hit

        if prot["should_liquidate"]:
            self.hard_floor_violated = True
            self.halted = True
            for t in list(self.running_trades):
                self._close_trade_record(t, close_px, "HARD_FLOOR_LIQUIDATION", ts)
            self.running_trades.clear()
            for _ in self.pending_orders:
                self._reject("SAFETY_OR_SHUTDOWN")
            self.pending_orders.clear()
        elif prot["should_halt_entries"] or throttle_hit:
            self.halted = True

    def _compute_clamped_bracket(
        self,
        side: str,
        ref_price: float,
        atr_5m: float,
    ) -> Tuple[float, float]:
        cfg = self.profile
        px = max(1.0, float(ref_price))
        raw_sl = float(atr_5m) * cfg.atr_sl_mult
        sl_dist = min(cfg.max_sl_pct * px, max(cfg.min_sl_pct * px, raw_sl))
        raw_tp = sl_dist * cfg.target_rr
        tp_dist = min(cfg.max_tp_pct * px, max(cfg.min_tp_pct * px, raw_tp))
        if side == "b":
            sl_px = _round_nearest_tick(px - sl_dist, cfg.tick_size_usd)
            tp_px = _round_nearest_tick(px + tp_dist, cfg.tick_size_usd)
        else:
            sl_px = _round_nearest_tick(px + sl_dist, cfg.tick_size_usd)
            tp_px = _round_nearest_tick(px - tp_dist, cfg.tick_size_usd)
        return sl_px, tp_px

    def _clamp_bracket_around_entry(
        self,
        side: str,
        entry_px: float,
        cand_sl: float,
        cand_tp: float,
    ) -> Tuple[float, float]:
        cfg = self.profile
        px = max(1.0, float(entry_px))
        sl_dist = abs(px - float(cand_sl))
        tp_dist = abs(float(cand_tp) - px)
        sl_dist = min(cfg.max_sl_pct * px, max(cfg.min_sl_pct * px, sl_dist))
        tp_dist = max(tp_dist, sl_dist * cfg.target_rr)
        tp_dist = min(cfg.max_tp_pct * px, max(cfg.min_tp_pct * px, tp_dist))
        if side == "b":
            return (
                _round_nearest_tick(px - sl_dist, cfg.tick_size_usd),
                _round_nearest_tick(px + tp_dist, cfg.tick_size_usd),
            )
        return (
            _round_nearest_tick(px + sl_dist, cfg.tick_size_usd),
            _round_nearest_tick(px - tp_dist, cfg.tick_size_usd),
        )

    def _detect_profile_signal(
        self,
        close_px: float,
        high_px: float,
        low_px: float,
        context: Dict[str, Any],
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        cfg = self.profile
        liq_sig = context.get("liquidity_signal") or {}
        hierarchy = context.get("hierarchy") or {}
        vwap = context.get("vwap") or {}
        td9 = context.get("td9") or {}
        htf_sweeps = context.get("htf_sweeps") or {}
        atr_15m = max(1.0, float(context.get("atr_15m") or (close_px * 0.0040)))
        score = float(liq_sig.get("liquidity_score") or 0.0)
        tf = str(liq_sig.get("timeframe") or "")
        conf = str(liq_sig.get("confirmation") or "")

        if cfg.name == "SCALP_SWEEP_TIGHT":
            if score >= cfg.min_liq_score:
                return "b", liq_sig
            if score <= -cfg.min_liq_score:
                return "s", liq_sig
            return None

        if cfg.name in ("SCALP_SWEEP_STD", "SCALP_SWEEP_WIDE"):
            if abs(score) < cfg.min_liq_score:
                return None
            cand_side = "b" if score > 0.0 else "s"
            if tf == "15m":
                return cand_side, liq_sig
            if tf == "5m":
                swept_lv = float(liq_sig.get("swept_level") or 0.0)
                wick_lv = float(liq_sig.get("wick_extreme") or 0.0)
                pools = hierarchy.get("low_pools" if cand_side == "b" else "high_pools") or ()
                tol = 0.40 * atr_15m
                confluent = any(
                    (getattr(p, "anchor_timeframe", "") in ("45m", "1h") or getattr(p, "is_super_pool", False))
                    and (
                        abs(float(p.level) - swept_lv) <= tol
                        or (wick_lv > 0.0 and abs(float(p.level) - wick_lv) <= tol)
                    )
                    for p in pools
                )
                if confluent:
                    return cand_side, liq_sig
            sig15 = context.get("liquidity_signal_15m") or {}
            score15 = float(sig15.get("liquidity_score") or 0.0)
            if str(sig15.get("timeframe") or "") == "15m" and abs(score15) >= cfg.min_liq_score:
                return ("b" if score15 > 0.0 else "s"), sig15
            return None

        if cfg.use_vwap_td9:
            bar_cnt = int(vwap.get("bar_count") or 0)
            v_px = float(vwap.get("vwap") or 0.0)
            v_std = float(vwap.get("std") or 0.0)
            if bar_cnt < 20 or v_px <= 0.0 or v_std <= 0.0:
                return None
            upper_1_5s = float(vwap.get("upper_1_5s") or (v_px + 1.5 * v_std))
            lower_1_5s = float(vwap.get("lower_1_5s") or (v_px - 1.5 * v_std))
            upper_2s = float(vwap.get("upper_2s") or (v_px + 2.0 * v_std))
            lower_2s = float(vwap.get("lower_2s") or (v_px - 2.0 * v_std))
            t5 = td9.get("5m") or {}
            t15 = td9.get("15m") or {}

            td9_long = bool(
                t5.get("buy_td9")
                or t5.get("buy_perfected")
                or int(t5.get("buy_count") or 0) >= 8
                or t15.get("buy_td9")
                or t15.get("buy_perfected")
                or int(t15.get("buy_count") or 0) >= 8
            )
            td9_short = bool(
                t5.get("sell_td9")
                or t5.get("sell_perfected")
                or int(t5.get("sell_count") or 0) >= 8
                or t15.get("sell_td9")
                or t15.get("sell_perfected")
                or int(t15.get("sell_count") or 0) >= 8
            )
            pierce_reclaim_long = (low_px <= lower_2s) and (close_px > lower_2s)
            pierce_reclaim_short = (high_px >= upper_2s) and (close_px < upper_2s)

            if close_px <= lower_1_5s and (td9_long or pierce_reclaim_long) and score >= 0.0:
                return "b", liq_sig
            if close_px >= upper_1_5s and (td9_short or pierce_reclaim_short) and score <= 0.0:
                return "s", liq_sig
            return None

        if cfg.name == "SCALP_SWEEP_EXT":
            bull_htf = htf_sweeps.get("bull")
            bear_htf = htf_sweeps.get("bear")
            sig15 = context.get("liquidity_signal_15m") or liq_sig
            score15 = float(sig15.get("liquidity_score") or 0.0)
            tf15 = str(sig15.get("timeframe") or "")
            conf15 = str(sig15.get("confirmation") or "")
            ok_15m_long = (tf15 == "15m" and conf15 == "CONFIRMED_CLOSE" and score15 >= cfg.min_liq_score)
            ok_15m_short = (tf15 == "15m" and conf15 == "CONFIRMED_CLOSE" and score15 <= -cfg.min_liq_score)

            if ok_15m_long or bull_htf is not None:
                eff_sig = sig15 if ok_15m_long else {
                    "liquidity_score": 0.50,
                    "event_type": "BULL_SWEEP_RECLAIM",
                    "confirmation": "CONFIRMED_CLOSE",
                    "timeframe": "15m",
                    "swept_level": float(bull_htf["level"]),
                    "wick_extreme": float(bull_htf["wick_extreme"]),
                    "nearest_high_pool": liq_sig.get("nearest_high_pool", 0.0),
                    "nearest_high_touches": liq_sig.get("nearest_high_touches", 0),
                    "nearest_low_pool": liq_sig.get("nearest_low_pool", 0.0),
                    "nearest_low_touches": liq_sig.get("nearest_low_touches", 0),
                }
                return "b", eff_sig

            if ok_15m_short or bear_htf is not None:
                eff_sig = sig15 if ok_15m_short else {
                    "liquidity_score": -0.50,
                    "event_type": "BEAR_SWEEP_RECLAIM",
                    "confirmation": "CONFIRMED_CLOSE",
                    "timeframe": "15m",
                    "swept_level": float(bear_htf["level"]),
                    "wick_extreme": float(bear_htf["wick_extreme"]),
                    "nearest_high_pool": liq_sig.get("nearest_high_pool", 0.0),
                    "nearest_high_touches": liq_sig.get("nearest_high_touches", 0),
                    "nearest_low_pool": liq_sig.get("nearest_low_pool", 0.0),
                    "nearest_low_touches": liq_sig.get("nearest_low_touches", 0),
                }
                return "s", eff_sig
            return None

        if score >= cfg.min_liq_score:
            return "b", liq_sig
        if score <= -cfg.min_liq_score:
            return "s", liq_sig
        return None

    def _check_macro_filter(
        self,
        side: str,
        context: Optional[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        if self.macro_filter == "off" or not isinstance(context, dict):
            return True, "OK"

        explicit_macro = str(context.get("macro_regime") or "").strip().upper()
        ind_1h = context.get("ind_1h") or {}
        ind_4h = context.get("ind_4h") or {}
        perc = context.get("perception") or {}

        is_extreme_vol = "EXTREME_VOLATILITY" in {
            explicit_macro,
            str(perc.get("market_regime") or "").upper(),
            str(ind_1h.get("market_regime") or "").upper(),
            str(ind_1h.get("regime") or "").upper(),
            str(ind_4h.get("market_regime") or "").upper(),
            str(ind_4h.get("regime") or "").upper(),
        }
        if is_extreme_vol:
            return False, "MACRO_EXTREME_VOL"

        reg_1h = str(ind_1h.get("regime") or ind_1h.get("market_regime") or "").upper()
        mreg_1h = str(ind_1h.get("market_regime") or "").upper()
        ema_1h = float(ind_1h.get("ema_score") or 0.0)
        adx_1h = float(ind_1h.get("adx") or 0.0)
        e9_1h, e21_1h = float(ind_1h.get("ema9") or 0.0), float(ind_1h.get("ema21") or 0.0)

        reg_4h = str(ind_4h.get("regime") or ind_4h.get("market_regime") or "").upper()
        mreg_4h = str(ind_4h.get("market_regime") or "").upper()
        ema_4h = float(ind_4h.get("ema_score") or 0.0)
        e9_4h, e21_4h = float(ind_4h.get("ema9") or 0.0), float(ind_4h.get("ema21") or 0.0)

        range_tags = {
            "RANGE",
            "RANGING",
            "RANGE_NORMAL",
            "RANGE_SQUEEZE",
            "TRANSITION",
            "NEUTRAL",
            "EMA_FLAT",
        }
        spread_1h = abs(e9_1h - e21_1h) / max(1.0, e21_1h) if e21_1h > 0.0 else 0.0
        spread_4h = abs(e9_4h - e21_4h) / max(1.0, e21_4h) if e21_4h > 0.0 else 0.0

        bull_1h = ("BULL" in reg_1h) or (
            reg_1h not in range_tags and ema_1h >= 0.5 and e9_1h > e21_1h and spread_1h >= 0.0015
        )
        bear_1h = ("BEAR" in reg_1h) or (
            reg_1h not in range_tags and ema_1h <= -0.5 and e9_1h < e21_1h and spread_1h >= 0.0015
        )
        bull_4h = ("BULL" in reg_4h) or (
            reg_4h not in range_tags and ema_4h >= 0.5 and e9_4h > e21_4h and spread_4h >= 0.0015
        )
        bear_4h = ("BEAR" in reg_4h) or (
            reg_4h not in range_tags and ema_4h <= -0.5 and e9_4h < e21_4h and spread_4h >= 0.0015
        )

        strong_bull = (
            explicit_macro in ("TREND_BULL", "FULL_BULL", "STRONG_BULL")
            or (bull_1h and bull_4h)
            or (bull_1h and (mreg_1h == "VOLATILITY_EXPANSION" or adx_1h >= 25.0 or ema_4h > 0.0 or not ind_4h))
            or (bull_1h and ema_1h >= 1.0 and spread_1h >= 0.0025 and not bear_4h)
            or (bull_4h and (mreg_4h == "VOLATILITY_EXPANSION" or ema_1h > 0.0))
        )
        strong_bear = (
            explicit_macro in ("TREND_BEAR", "FULL_BEAR", "STRONG_BEAR")
            or (bear_1h and bear_4h)
            or (bear_1h and (mreg_1h == "VOLATILITY_EXPANSION" or adx_1h >= 25.0 or ema_4h < 0.0 or not ind_4h))
            or (bear_1h and ema_1h <= -1.0 and spread_1h >= 0.0025 and not bull_4h)
            or (bear_4h and (mreg_4h == "VOLATILITY_EXPANSION" or ema_1h < 0.0))
        )

        if self.macro_filter == "counter_block":
            if side == "s" and strong_bull:
                return False, "MACRO_COUNTER_BLOCK"
            if side == "b" and strong_bear:
                return False, "MACRO_COUNTER_BLOCK"
            return True, "OK"

        if self.macro_filter == "range_only":
            allowed_tags = {
                "",
                "RANGE",
                "RANGING",
                "RANGE_NORMAL",
                "RANGE_SQUEEZE",
                "TRANSITION",
                "NEUTRAL",
                "EMA_FLAT",
            }
            if explicit_macro:
                if explicit_macro not in allowed_tags:
                    return False, "MACRO_NOT_RANGE"
            if (
                bull_1h
                or bear_1h
                or bull_4h
                or bear_4h
                or mreg_1h == "VOLATILITY_EXPANSION"
                or mreg_4h == "VOLATILITY_EXPANSION"
                or (reg_1h not in allowed_tags)
                or (reg_4h not in allowed_tags)
            ):
                return False, "MACRO_NOT_RANGE"
            return True, "OK"

        return True, "OK"

    def submit_signal(
        self,
        side: str = "b",
        spot_price: Optional[float] = None,
        atr_5m: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        invalidation_price: Optional[float] = None,
        ttl_sec: Optional[float] = None,
        margin_sats: Optional[float] = None,
        leverage: Optional[int] = None,
        liquidity_signal: Optional[Dict[str, Any]] = None,
        hierarchy: Optional[Dict[str, Any]] = None,
        *,
        bar_idx: Optional[int] = None,
        ts: Optional[float] = None,
        enforce_cooldown: bool = False,
        check_walls: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Registra y valida una señal al cierre de la barra actual t (created_bar_idx = t).
        Útil tanto para el motor de estrategia como para inyección determinista en tests unitarios.
        """
        self.signals_total += 1
        norm_side = _normalize_side(side)
        cur_bar = int(self.current_bar_idx if bar_idx is None else bar_idx)
        cur_ts = float(self.current_ts if ts is None else ts)
        spot = float(self.last_close if spot_price is None else spot_price)
        if spot <= 0.0:
            self._reject("INVALID_SPOT")
            return False, "INVALID_SPOT", None

        if self.halted:
            self._reject("HALTED")
            return False, "HALTED", None

        if context is not None:
            macro_ok, macro_reason = self._check_macro_filter(norm_side, context)
            if not macro_ok:
                self._reject(macro_reason)
                return False, macro_reason, None

        if enforce_cooldown:
            last_ts = float(self.last_entry_ts_by_side.get(norm_side, -1e18))
            if (cur_ts - last_ts) < self.profile.cooldown_same_side_sec:
                self._reject("COOLDOWN_SAME_SIDE")
                return False, "COOLDOWN_SAME_SIDE", None

        atr_val = max(1.0, float(atr_5m if atr_5m is not None else (spot * 0.0025)))
        lev = int(leverage if leverage is not None else self.profile.default_leverage)
        auto_sl, auto_tp = self._compute_clamped_bracket(norm_side, spot, atr_val)
        base_sl = float(sl_price) if sl_price is not None else auto_sl
        base_tp = float(tp_price) if tp_price is not None else auto_tp

        if self.exec_mode == "LIMIT":
            if limit_price is not None:
                p_lim = round_to_tick(float(limit_price), norm_side, self.profile.tick_size_usd)
                if sl_price is not None and tp_price is not None:
                    sl_px, tp_px = float(sl_price), float(tp_price)
                else:
                    raw_sl, raw_tp = self._compute_clamped_bracket(norm_side, p_lim, atr_val)
                    sl_px = float(sl_price) if sl_price is not None else raw_sl
                    tp_px = float(tp_price) if tp_price is not None else raw_tp
                p_inv = float(invalidation_price) if invalidation_price is not None else (
                    round(p_lim - (0.65 * atr_val), 2) if norm_side == "b" else round(p_lim + (0.65 * atr_val), 2)
                )
                ttl_val = float(ttl_sec) if ttl_sec is not None else self.profile.ttl_sec_5m
            else:
                plan = compute_limit_entry_plan(
                    side=norm_side,
                    style="SCALP",
                    spot_price=spot,
                    atr_5m=atr_val,
                    base_sl=base_sl,
                    base_tp=base_tp,
                    liquidity_signal=liquidity_signal,
                    cfg=self._limit_cfg,
                )
                if not plan.get("valid"):
                    reason = str(plan.get("reason") or "LIMIT_PLAN_INVALID")
                    if reason.startswith("LIMIT_OFFSET_TOO_FAR"):
                        reason = "LIMIT_OFFSET_TOO_FAR"
                    self._reject(reason)
                    return False, reason, None
                p_lim = float(plan["limit_price"])
                clamped_sl, clamped_tp = self._clamp_bracket_around_entry(
                    norm_side, p_lim, float(plan["stoploss"]), float(plan["takeprofit"])
                )
                sl_px = float(sl_price) if sl_price is not None else clamped_sl
                tp_px = float(tp_price) if tp_price is not None else clamped_tp
                p_inv = float(invalidation_price) if invalidation_price is not None else float(plan["invalidation_price"])
                ttl_val = float(ttl_sec) if ttl_sec is not None else float(plan["ttl_sec"])
            entry_ref = p_lim
        else:
            p_lim = spot
            sl_px, tp_px = base_sl, base_tp
            p_inv = 0.0
            ttl_val = 0.0
            entry_ref = spot

        tp_dist = abs(tp_px - entry_ref)
        if self.profile.require_htf_or_super_pool and isinstance(hierarchy, dict):
            opp_key = "nearest_high_45m_1h" if norm_side == "b" else "nearest_low_45m_1h"
            opp_pool = float(hierarchy.get(opp_key) or 0.0)
            if opp_pool > 0.0:
                room_htf = (opp_pool - entry_ref) if norm_side == "b" else (entry_ref - opp_pool)
                if room_htf < 0.80 * tp_dist:
                    self._reject("REJECT_HTF_POOL_ROOM")
                    return False, "REJECT_HTF_POOL_ROOM", None

        if self.lane_budget.config.enabled and self.lane_budget.config.rolling_pause_reset:
            self.lane_budget.sync_pause_state(cur_ts)

        unit_loss = compute_scalp_loss_at_sl_sats(
            1.0, lev, entry_ref, sl_px, self.fee_rate_open, self.fee_rate_close
        )
        if margin_sats is None:
            base_risk_sats = self.profile.base_risk_pct_b * float(self.lane_budget.e_global)
            raw_margin = max(1.0, round(base_risk_sats / max(1e-9, unit_loss)))
        else:
            raw_margin = max(1.0, float(margin_sats))

        # Aplicar recorte suave 0.5x UNA sola vez antes del gate de LaneBudgetController
        need_soft = self.lane_budget.config.enabled and (
            self.lane_budget.dd_b >= self.lane_budget.config.lane_b_soft_dd_cut_pct
            or (self.lane_budget.config.rolling_pause_reset and self.lane_budget.post_pause_half_risk_b)
        )
        soft_mult = self.lane_budget.config.lane_b_soft_risk_mult if need_soft else 1.0
        cand_margin = max(1, int(round(raw_margin * soft_mult)))

        if check_walls and isinstance(liquidity_signal, dict):
            room_wall = compute_opposite_pool_room(
                side_is_long=(norm_side == "b"),
                entry_price=entry_ref,
                margin_sats=cand_margin,
                leverage=lev,
                taker_fee=self.fee_rate_close,
                liquidity_signal=liquidity_signal,
                min_wall_fee_ratio=self.profile.min_wall_fee_ratio,
                min_wall_touches=2,
            )
            if room_wall.get("blocked_by_wall"):
                self._reject("BLOCKED_BY_OPPOSITE_WALL")
                return False, "BLOCKED_BY_OPPOSITE_WALL", None

        cand_loss_at_sl = compute_scalp_loss_at_sl_sats(
            cand_margin, lev, entry_ref, sl_px, self.fee_rate_open, self.fee_rate_close
        )

        n_open_b_buy = sum(1 for t in self.running_trades if t["side"] == "b") + sum(
            1 for o in self.pending_orders if o["side"] == "b"
        )
        n_open_b_sell = sum(1 for t in self.running_trades if t["side"] == "s") + sum(
            1 for o in self.pending_orders if o["side"] == "s"
        )
        open_margin_b = sum(float(t["margin_sats"]) for t in self.running_trades) + sum(
            float(o["margin_sats"]) for o in self.pending_orders
        )
        open_sl_risk_b = sum(float(t["loss_at_sl_sats"]) for t in self.running_trades) + sum(
            float(o["loss_at_sl_sats"]) for o in self.pending_orders
        )

        was_clamped = False
        lcfg = self.lane_budget.config
        if self.dynamic_risk_clamp and lcfg.enabled:
            ep_capped = lcfg.rolling_pause_reset and (
                float(self.lane_budget.dd_b_episode) >= lcfg.lane_b_episode_hard_cap_pct
            )
            prot_brake = (
                float(self.lane_budget.dd_a) >= lcfg.protective_dd_a_max_pct
                or float(self.lane_budget.dd_global) >= lcfg.protective_dd_global_max_pct
            )
            if (
                not self.lane_budget.is_lane_b_paused(now_ts=cur_ts, side=norm_side)
                and not ep_capped
                and not prot_brake
            ):
                e_glob = float(self.lane_budget.e_global)
                dd_b_win = float(self.lane_budget.dd_b)
                r_avail = max(0.0, lcfg.lane_b_hard_dd_pause_pct - dd_b_win) * e_glob - open_sl_risk_b
                if lcfg.rolling_pause_reset:
                    r_ep_avail = (
                        max(0.0, lcfg.lane_b_episode_hard_cap_pct - float(self.lane_budget.dd_b_episode))
                        * float(self.lane_budget.episode_peak_b)
                        - open_sl_risk_b
                    )
                    r_avail = min(r_avail, r_ep_avail)
                r_min = 0.0005 * e_glob
                if cand_loss_at_sl > r_avail and r_avail >= r_min:
                    clamped_margin = int(math.floor(r_avail / max(1e-9, unit_loss)))
                    if clamped_margin >= 1 and (clamped_margin * lev) >= 250:
                        cand_margin = clamped_margin
                        cand_loss_at_sl = compute_scalp_loss_at_sl_sats(
                            cand_margin, lev, entry_ref, sl_px, self.fee_rate_open, self.fee_rate_close
                        )
                        was_clamped = True

        avail_bal = max(0.0, float(self.lane_budget.e_global) - open_margin_b)

        decision = self.lane_budget.evaluate_lane_b_entry(
            side=norm_side,
            now_ts=cur_ts,
            n_open_a=0,
            n_open_b_buy=n_open_b_buy,
            n_open_b_sell=n_open_b_sell,
            avail_balance_sats=avail_bal,
            open_margin_b_sats=open_margin_b,
            margin_new_sats=cand_margin,
            open_loss_at_sl_b_sats=open_sl_risk_b,
            loss_at_sl_new_sats=cand_loss_at_sl,
        )
        if not decision.allowed:
            self._reject(decision.reason)
            return False, decision.reason, None

        if was_clamped:
            self.clamped_entries += 1
        self._seq_id += 1
        order_rec = {
            "id": f"scalp-{self._seq_id}",
            "exec_mode": self.exec_mode,
            "side": norm_side,
            "margin_sats": cand_margin,
            "leverage": lev,
            "ref_price": spot,
            "limit_price": p_lim,
            "sl_price": sl_px,
            "tp_price": tp_px,
            "invalidation_price": p_inv,
            "ttl_sec": ttl_val,
            "loss_at_sl_sats": cand_loss_at_sl,
            "risk_clamped": was_clamped,
            "created_at": cur_ts,
            "created_bar_idx": cur_bar,
        }
        self.pending_orders.append(order_rec)
        self.orders_submitted += 1
        if self.exec_mode == "LIMIT":
            self.limit_submitted += 1
        self.last_entry_ts_by_side[norm_side] = cur_ts
        return True, "SUBMITTED", dict(order_rec)

    def process_bar(
        self,
        bar_idx: int,
        ts: float,
        open_px: float,
        high_px: float,
        low_px: float,
        close_px: float,
        volume: float = 0.0,
        context: Optional[Dict[str, Any]] = None,
        has_more_bars: bool = True,
    ) -> None:
        """
        Avanza una barra 1m cumpliendo estrictamente el orden causal:
          (1) Posiciones RUNNING existentes (created_bar_idx < t)
          (2) Órdenes pendientes (created_bar_idx < t)
          (3) HardFloorGuardian al cierre de t
          (4) Generación de señal y envío al cierre de t (si has_more_bars y not halted)
        """
        b_idx = int(bar_idx)
        ts_f = float(ts)
        o_f = float(open_px)
        h_f = float(high_px)
        l_f = float(low_px)
        c_f = max(1.0, float(close_px))

        self.current_bar_idx = b_idx
        self.current_ts = ts_f
        self.last_close = c_f

        if context is None:
            if self._ctx_builder is None:
                self._ctx_builder = CausalScalpContextBuilder()
            ctx = self._ctx_builder.step(ts_f, o_f, h_f, l_f, c_f, volume=float(volume))
        else:
            ctx = context

        atr_5m = max(1.0, float(ctx.get("atr_5m") or (c_f * 0.0025)))
        range_pct = max(0.0, (h_f - l_f) / c_f)
        perception = ctx.get("perception") or {}
        ovr_side = perception.get("override_side") if isinstance(perception, dict) else None

        # (1) Posiciones RUNNING
        self._process_running_trades(b_idx, ts_f, o_f, h_f, l_f, c_f, atr_5m, range_pct)

        # (2) Órdenes pendientes (created_bar_idx < t)
        self._process_pending_orders(b_idx, ts_f, o_f, h_f, l_f, c_f, atr_5m, range_pct, ovr_side)

        # (3) HardFloorGuardian sobre equidad MTM global al cierre de t
        self._evaluate_guardian(ts_f, c_f)

        # (4) Señales y envío únicamente al cierre de t con edge-triggering estricto
        if has_more_bars and not self.halted:
            raw_sig = self._detect_profile_signal(c_f, h_f, l_f, ctx)
            if raw_sig is None:
                self._edge_latch_side = None
            else:
                cand_side, eff_liq_sig = raw_sig
                swept_lv = float(eff_liq_sig.get("swept_level") or 0.0) if isinstance(eff_liq_sig, dict) else 0.0
                if self.profile.use_vwap_td9:
                    anchor_key = f"VWAP_TD9:{int(ts_f // 900)}"
                    dedup_window = 900.0
                elif swept_lv > 0.0:
                    tf_s = str(eff_liq_sig.get("timeframe") or "5m")
                    lvl_bucket = round(swept_lv / max(1.0, 0.25 * atr_5m))
                    anchor_key = f"SWEEP:{tf_s}:{lvl_bucket}"
                    dedup_window = 2700.0 if tf_s == "15m" else 900.0
                else:
                    tf_s = (
                        str((eff_liq_sig or {}).get("timeframe") or "5m")
                        if isinstance(eff_liq_sig, dict)
                        else "5m"
                    )
                    anchor_key = f"LIQ:{tf_s}:{int(ts_f // 300)}"
                    dedup_window = 600.0

                if (
                    self._edge_latch_side != cand_side
                    and (ts_f - self._edge_anchor_ts.get((cand_side, anchor_key), -1e18)) >= dedup_window
                ):
                    self._edge_latch_side = cand_side
                    self._edge_anchor_ts[(cand_side, anchor_key)] = ts_f
                    self.submit_signal(
                        side=cand_side,
                        spot_price=c_f,
                        atr_5m=atr_5m,
                        liquidity_signal=eff_liq_sig,
                        hierarchy=ctx.get("hierarchy"),
                        bar_idx=b_idx,
                        ts=ts_f,
                        enforce_cooldown=True,
                        check_walls=True,
                        context=ctx,
                    )

    def finalize_episode(self) -> Dict[str, Any]:
        """Cancela órdenes pendientes remanentes y cierra posiciones abiertas al último cierre."""
        for _ in self.pending_orders:
            self._reject("EPISODE_END_CANCEL")
        self.pending_orders.clear()

        if self.running_trades:
            close_px = max(1.0, float(self.last_close or 1.0))
            close_ts = float(self.current_ts)
            for t in list(self.running_trades):
                self._close_trade_record(t, close_px, "EPISODE_END", close_ts)
            self.running_trades.clear()

        n_trades = len(self.closed_trades)
        win_trades = [t for t in self.closed_trades if float(t["net_pnl_sats"]) > 0.0]
        loss_trades = [t for t in self.closed_trades if float(t["net_pnl_sats"]) <= 0.0]
        wins = len(win_trades)
        losses = n_trades - wins
        gross_pnl_sats = sum(float(t["gross_pnl_sats"]) for t in self.closed_trades)
        net_profit_sats = sum(float(t["net_pnl_sats"]) for t in self.closed_trades)
        gross_win_sats = sum(float(t["net_pnl_sats"]) for t in win_trades)
        gross_loss_sats = sum(abs(float(t["net_pnl_sats"])) for t in self.closed_trades if float(t["net_pnl_sats"]) < 0.0)
        total_fees_sats = sum(float(t["fees_sats"]) for t in self.closed_trades)
        total_funding_sats = sum(float(t["funding_sats"]) for t in self.closed_trades)
        total_cost_sats = sum(float(t["c_rt_sats"]) for t in self.closed_trades)
        fer = compute_fer(net_profit_sats, total_cost_sats)
        fill_pct = (self.orders_filled / self.orders_submitted * 100.0) if self.orders_submitted > 0 else 0.0

        avg_win_net_sats = (gross_win_sats / wins) if wins > 0 else 0.0
        avg_loss_net_sats = (sum(float(t["net_pnl_sats"]) for t in loss_trades) / losses) if losses > 0 else 0.0
        avg_win_gross_pct = (sum(float(t["gross_pct"]) for t in win_trades) / wins) if wins > 0 else 0.0
        avg_loss_gross_pct = (sum(float(t["gross_pct"]) for t in loss_trades) / losses) if losses > 0 else 0.0

        exit_reasons: Dict[str, int] = {"TP": 0, "SL": 0, "DEF_SCALP_LOCK": 0, "EOD": 0}
        for t in self.closed_trades:
            cr = str(t.get("close_reason") or "")
            if cr == "TAKE_PROFIT":
                exit_reasons["TP"] += 1
            elif cr in ("STOP_LOSS", "LIQUIDATION"):
                exit_reasons["SL"] += 1
            elif cr == "DEF_SCALP_LOCK":
                exit_reasons["DEF_SCALP_LOCK"] += 1
            else:
                exit_reasons["EOD"] += 1

        return {
            "n_trades": n_trades,
            "wins": wins,
            "losses": losses,
            "gross_pnl_sats": gross_pnl_sats,
            "net_profit_sats": net_profit_sats,
            "gross_win_sats": gross_win_sats,
            "gross_loss_sats": gross_loss_sats,
            "total_fees_sats": total_fees_sats,
            "total_funding_sats": total_funding_sats,
            "total_cost_sats": total_cost_sats,
            "fer": fer,
            "fill_pct": fill_pct,
            "avg_win_net_sats": avg_win_net_sats,
            "avg_loss_net_sats": avg_loss_net_sats,
            "avg_win_gross_pct": avg_win_gross_pct,
            "avg_loss_gross_pct": avg_loss_gross_pct,
            "exit_reasons": exit_reasons,
            "signals_total": self.signals_total,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "limit_submitted": self.limit_submitted,
            "limit_filled": self.limit_filled,
            "cancelled_ttl": self.cancelled_ttl,
            "cancelled_runaway": self.cancelled_runaway,
            "cancelled_invalidated": self.cancelled_invalidated,
            "clamped_entries": int(self.clamped_entries),
            "pause_reanchors": int(getattr(self.lane_budget, "reanchor_count_b", 0)),
            "max_drawdown_pct": round(self.max_seen_dd_global * 100.0, 4),
            "dd_b_pct": round(self.max_seen_dd_b * 100.0, 4),
            "dd_a_pct": round(self.max_seen_dd_a * 100.0, 4),
            "dd_guard_events": self.dd_guard_events,
            "hard_floor_violated": self.hard_floor_violated,
            "final_equity_sats": float(self.lane_budget.e_global),
            "closed_trades": list(self.closed_trades),
            "rejected_reasons": dict(self.rejected_reasons),
        }

    def run_block(
        self,
        blk: Union[Dict[str, Any], Sequence[Any]],
        ctx_builder: Optional[CausalScalpContextBuilder] = None,
    ) -> Dict[str, Any]:
        """Ejecuta el motor barra por barra sobre un bloque precomputado (blk['steps']) o lista de Candle."""
        self.reset()
        steps = blk["steps"] if isinstance(blk, dict) else blk
        n_steps = len(steps)
        builder = ctx_builder if ctx_builder is not None else CausalScalpContextBuilder()
        if ctx_builder is not None:
            builder.reset()

        for idx, step_item in enumerate(steps):
            ts, o, h, l, c, v, ind_5m, ind_15m, ind_1h, ind_4h, perception = _extract_bar_fields_full(step_item)
            ctx = builder.step(
                ts,
                o,
                h,
                l,
                c,
                volume=v,
                ind_5m=ind_5m,
                ind_15m=ind_15m,
                perception=perception,
                ind_1h=ind_1h,
                ind_4h=ind_4h,
            )
            self.process_bar(
                bar_idx=idx,
                ts=ts,
                open_px=o,
                high_px=h,
                low_px=l,
                close_px=c,
                volume=v,
                context=ctx,
                has_more_bars=(idx < n_steps - 1),
            )

        return self.finalize_episode()


def run_scalp_cells_on_block(
    blk: Union[Dict[str, Any], Sequence[Any]],
    cell_specs: Union[Dict[str, ScalpSimEngine], Iterable[Tuple[str, ScalpSimEngine]]],
    initial_balance: float = 100_000,
) -> Dict[str, Dict[str, Any]]:
    """
    Ejecuta múltiples celdas ScalpSimEngine en paralelo lógico sobre un solo CausalScalpContextBuilder
    barra por barra, evitando recalcular LiquidityRadar y agregación MTF por cada celda.
    """
    items: List[Tuple[str, ScalpSimEngine]] = (
        list(cell_specs.items()) if isinstance(cell_specs, dict) else list(cell_specs)
    )
    for _, eng in items:
        if initial_balance is not None and abs(float(eng.initial_balance_sats) - float(initial_balance)) > 1e-9:
            eng.initial_balance_sats = float(initial_balance)
        eng.reset()

    steps = blk["steps"] if isinstance(blk, dict) else blk
    n_steps = len(steps)
    builder = CausalScalpContextBuilder()

    for idx, step_item in enumerate(steps):
        ts, o, h, l, c, v, ind_5m, ind_15m, ind_1h, ind_4h, perception = _extract_bar_fields_full(step_item)
        ctx = builder.step(
            ts,
            o,
            h,
            l,
            c,
            volume=v,
            ind_5m=ind_5m,
            ind_15m=ind_15m,
            perception=perception,
            ind_1h=ind_1h,
            ind_4h=ind_4h,
        )
        has_more = idx < (n_steps - 1)
        for _, eng in items:
            eng.process_bar(
                bar_idx=idx,
                ts=ts,
                open_px=o,
                high_px=h,
                low_px=l,
                close_px=c,
                volume=v,
                context=ctx,
                has_more_bars=has_more,
            )

    return {cell_id: eng.finalize_episode() for cell_id, eng in items}
