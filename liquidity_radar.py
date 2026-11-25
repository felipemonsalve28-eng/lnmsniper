#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
LNM SNIPER v7.3+ — RADAR DETERMINISTA DE LIQUIDEZ (FASE 1: SWEEP + RECLAIM)
================================================================================
Módulo puro de análisis de microestructura OHLCV (5m y 15m):
  - Cero I/O, cero llamadas HTTP, cero dependencias pesadas (compatible Termux).
  - Detección estrictamente causal de Swings (j <= t - 1 - k; cero lookahead).
  - Clustering adaptativo EQH/EQL (tolerancia = cluster_atr_mult * ATR).
  - Mitigación estructural de liquidez gastada y detección Sweep + Reclaim
    tanto en cierre confirmado (t-1..t-3) como en rechazo intrabar (vela t).
  - Cálculo exacto de espacio neto hasta pool opuesto en contratos inversos
    BTCUSD (LN Markets) y sesgo acotado para compute_live_edge(simulated_entry=True).
================================================================================
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
import math

try:
    import numpy as np
except ImportError:
    from guardian_execution_core import compat_np as np


TF_POOL_WEIGHTS: Dict[str, float] = {
    "15m": 1.00,
    "30m": 1.20,
    "45m": 1.35,
    "1h": 1.60,
    "4h": 2.00,
}


@dataclass(frozen=True)
class LiquidityConfig:
    enabled: bool = True
    shadow_only: bool = True
    opposite_wall_enabled: bool = False
    swing_k_5m: int = 3
    swing_k_15m: int = 2
    lookback_5m: int = 96
    lookback_15m: int = 64
    cluster_atr_mult: float = 0.25
    mitigation_atr_mult: float = 0.35
    min_pierce_atr: float = 0.10
    max_pierce_atr: float = 1.20
    min_wick_ratio_close: float = 0.45
    min_wick_ratio_intrabar: float = 0.55
    intrabar_reclaim_buf_atr: float = 0.08
    vol_confirm_ratio: float = 1.25
    vol_bonus: float = 0.15
    max_active_pools_per_side: int = 10
    max_liq_bonus: float = 0.10
    max_liq_penalty: float = 0.12
    min_opposite_wall_fee_ratio: float = 1.80
    min_wall_touches: int = 2
    super_pool_atr_mult: float = 0.25
    swing_k_htf: int = 2
    lookback_htf: int = 64
    structural_td9_trailing_enabled: bool = False

    @classmethod
    def from_thresholds(cls, thresholds_mgr: Any = None) -> "LiquidityConfig":
        if thresholds_mgr is None:
            return cls()
        get_b = getattr(thresholds_mgr, "get_bool", None)
        get_i = getattr(thresholds_mgr, "get_int", None)
        get_f = getattr(thresholds_mgr, "get_float", None)
        if not (callable(get_b) and callable(get_i) and callable(get_f)):
            return cls()
        cat = "liquidity_radar"
        return cls(
            enabled=bool(get_b(cat, "enabled", cls.enabled)),
            shadow_only=bool(get_b(cat, "shadow_only", cls.shadow_only)),
            opposite_wall_enabled=bool(get_b(cat, "opposite_wall_enabled", cls.opposite_wall_enabled)),
            swing_k_5m=int(get_i(cat, "swing_k_5m", cls.swing_k_5m)),
            swing_k_15m=int(get_i(cat, "swing_k_15m", cls.swing_k_15m)),
            cluster_atr_mult=float(get_f(cat, "cluster_atr_mult", cls.cluster_atr_mult)),
            mitigation_atr_mult=float(get_f(cat, "mitigation_atr_mult", cls.mitigation_atr_mult)),
            min_pierce_atr=float(get_f(cat, "min_pierce_atr", cls.min_pierce_atr)),
            max_pierce_atr=float(get_f(cat, "max_pierce_atr", cls.max_pierce_atr)),
            min_wick_ratio_close=float(get_f(cat, "min_wick_ratio_close", cls.min_wick_ratio_close)),
            min_wick_ratio_intrabar=float(get_f(cat, "min_wick_ratio_intrabar", cls.min_wick_ratio_intrabar)),
            max_liq_bonus=float(get_f(cat, "max_liq_bonus", cls.max_liq_bonus)),
            max_liq_penalty=float(get_f(cat, "max_liq_penalty", cls.max_liq_penalty)),
            min_opposite_wall_fee_ratio=float(
                get_f(cat, "min_opposite_wall_fee_ratio", cls.min_opposite_wall_fee_ratio)
            ),
        )


@dataclass(frozen=True)
class LiquidityPool:
    side: str          # "HIGH" (buy-side stops) | "LOW" (sell-side stops)
    level: float       # Nivel canónico: max para HIGH, min para LOW
    touches: int       # 1 = swing simple, >=2 = cluster EQH/EQL
    last_index: int    # Índice de la vela más reciente perteneciente al cluster
    timeframe: str     # "5m" | "15m" | "30m" | "45m" | "1h" | "4h"

    @property
    def weight(self) -> float:
        if self.touches >= 3:
            return 1.60
        if self.touches == 2:
            return 1.35
        return 1.00

    @property
    def tf_weight(self) -> float:
        return float(TF_POOL_WEIGHTS.get(self.timeframe, 1.00))

    @property
    def effective_weight(self) -> float:
        return round(self.weight * self.tf_weight, 4)


@dataclass(frozen=True)
class SuperPool:
    side: str
    level: float
    anchor_timeframe: str
    timeframes: Tuple[str, ...]
    touches: int
    tf_weight: float
    effective_weight: float
    is_super_pool: bool
    merged_count: int
    last_index: int


def _touch_multiplier(touches: int) -> float:
    if touches >= 3:
        return 1.60
    if touches == 2:
        return 1.35
    return 1.00


def fuse_super_pools(
    pools_by_tf: Dict[str, Sequence[LiquidityPool]],
    atr: float,
    fusion_atr_mult: float = 0.25,
) -> Tuple[List[SuperPool], List[SuperPool]]:
    """
    Fusiona pools 15m/30m cercanos (<= fusion_atr_mult * ATR) dentro de anclas 1h/4h
    del mismo lado en Super-Pools deterministas, preservando 45m y pools sin fusión.
    """
    tol = max(0.0, float(fusion_atr_mult)) * max(1.0, float(atr or 1.0))
    anchor_tfs = ("4h", "1h")
    sub_tfs = ("15m", "30m")
    results: Dict[str, List[SuperPool]] = {"HIGH": [], "LOW": []}

    for side in ("HIGH", "LOW"):
        anchors: List[LiquidityPool] = []
        for tf in anchor_tfs:
            for p in pools_by_tf.get(tf, ()):
                if p.side == side:
                    anchors.append(p)

        subs: List[LiquidityPool] = []
        for tf in sub_tfs:
            for p in pools_by_tf.get(tf, ()):
                if p.side == side:
                    subs.append(p)

        others: List[LiquidityPool] = []
        for tf, plist in pools_by_tf.items():
            if tf in anchor_tfs or tf in sub_tfs:
                continue
            for p in plist:
                if p.side == side:
                    others.append(p)

        merged_map: Dict[int, List[LiquidityPool]] = {i: [] for i in range(len(anchors))}
        unmerged_subs: List[LiquidityPool] = []

        for sub in subs:
            best_idx = -1
            best_key: Optional[Tuple[float, float, float]] = None
            for idx, anc in enumerate(anchors):
                dist = abs(float(sub.level) - float(anc.level))
                if dist <= tol:
                    cand_key = (
                        dist,
                        -float(TF_POOL_WEIGHTS.get(anc.timeframe, 1.00)),
                        float(anc.level) if side == "LOW" else -float(anc.level),
                    )
                    if best_key is None or cand_key < best_key:
                        best_key = cand_key
                        best_idx = idx
            if best_idx >= 0:
                merged_map[best_idx].append(sub)
            else:
                unmerged_subs.append(sub)

        side_out: List[SuperPool] = []
        for idx, anc in enumerate(anchors):
            attached = merged_map[idx]
            if attached:
                all_members = [anc] + attached
                levels = [float(m.level) for m in all_members]
                level = max(levels) if side == "HIGH" else min(levels)
                touches = sum(int(m.touches) for m in all_members)
                unique_subs = sorted(
                    {m.timeframe for m in attached},
                    key=lambda t: (-float(TF_POOL_WEIGHTS.get(t, 1.00)), t),
                )
                tf_tuple = (anc.timeframe, *unique_subs)
                tf_w = round(
                    float(TF_POOL_WEIGHTS.get(anc.timeframe, 1.00))
                    + 0.15 * sum(float(TF_POOL_WEIGHTS.get(t, 1.00)) for t in unique_subs),
                    4,
                )
                eff_w = round(_touch_multiplier(touches) * tf_w, 4)
                side_out.append(
                    SuperPool(
                        side=side,
                        level=float(level),
                        anchor_timeframe=anc.timeframe,
                        timeframes=tf_tuple,
                        touches=touches,
                        tf_weight=tf_w,
                        effective_weight=eff_w,
                        is_super_pool=True,
                        merged_count=len(all_members),
                        last_index=max(int(m.last_index) for m in all_members),
                    )
                )
            else:
                tf_w = round(float(TF_POOL_WEIGHTS.get(anc.timeframe, 1.00)), 4)
                eff_w = round(_touch_multiplier(int(anc.touches)) * tf_w, 4)
                side_out.append(
                    SuperPool(
                        side=side,
                        level=float(anc.level),
                        anchor_timeframe=anc.timeframe,
                        timeframes=(anc.timeframe,),
                        touches=int(anc.touches),
                        tf_weight=tf_w,
                        effective_weight=eff_w,
                        is_super_pool=False,
                        merged_count=1,
                        last_index=int(anc.last_index),
                    )
                )

        for solo in others + unmerged_subs:
            tf_w = round(float(TF_POOL_WEIGHTS.get(solo.timeframe, 1.00)), 4)
            eff_w = round(_touch_multiplier(int(solo.touches)) * tf_w, 4)
            side_out.append(
                SuperPool(
                    side=side,
                    level=float(solo.level),
                    anchor_timeframe=solo.timeframe,
                    timeframes=(solo.timeframe,),
                    touches=int(solo.touches),
                    tf_weight=tf_w,
                    effective_weight=eff_w,
                    is_super_pool=False,
                    merged_count=1,
                    last_index=int(solo.last_index),
                )
            )

        side_out.sort(key=lambda sp: (-sp.tf_weight, -sp.effective_weight, sp.level))
        results[side] = side_out

    return results["HIGH"], results["LOW"]


class TD9PerfectionTracker:
    """Contador causal O(1) de DeMark TD9 Perfection al cierre de velas 1m, 5m y 15m."""

    SUPPORTED_TFS: Tuple[str, ...] = ("1m", "5m", "15m")

    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, Any]] = {
            tf: {
                "closes": deque(maxlen=4),
                "highs": deque(maxlen=4),
                "lows": deque(maxlen=4),
                "buy_count": 0,
                "sell_count": 0,
                "buy_td9": False,
                "sell_td9": False,
                "buy_perfected": False,
                "sell_perfected": False,
                "signal": "NONE",
                "last_bar_ts": -1.0,
            }
            for tf in self.SUPPORTED_TFS
        }

    @staticmethod
    def _neutral(tf: str) -> Dict[str, Any]:
        return {
            "timeframe": tf,
            "buy_count": 0,
            "sell_count": 0,
            "buy_td9": False,
            "sell_td9": False,
            "buy_perfected": False,
            "sell_perfected": False,
            "signal": "NONE",
            "last_bar_ts": -1.0,
        }

    def update_bar(self, tf: str, bar_ts: float, high: float, low: float, close: float) -> Dict[str, Any]:
        st = self._states.get(tf)
        if st is None:
            return self._neutral(tf)
        ts_f = float(bar_ts)
        if ts_f <= st["last_bar_ts"]:
            return self.get_state(tf)

        st["last_bar_ts"] = ts_f
        c_f = float(close)
        h_f = float(high)
        l_f = float(low)

        if len(st["closes"]) == 4:
            c_prev4 = st["closes"][0]
            if c_f < c_prev4:
                st["buy_count"] += 1
                st["sell_count"] = 0
            elif c_f > c_prev4:
                st["sell_count"] += 1
                st["buy_count"] = 0
            else:
                st["buy_count"] = 0
                st["sell_count"] = 0

        st["closes"].append(c_f)
        st["highs"].append(h_f)
        st["lows"].append(l_f)

        buy_td9 = st["buy_count"] == 9
        sell_td9 = st["sell_count"] == 9
        buy_perf = bool(
            buy_td9
            and len(st["lows"]) == 4
            and min(st["lows"][2], st["lows"][3]) <= min(st["lows"][0], st["lows"][1])
        )
        sell_perf = bool(
            sell_td9
            and len(st["highs"]) == 4
            and max(st["highs"][2], st["highs"][3]) >= max(st["highs"][0], st["highs"][1])
        )

        if buy_perf:
            sig = "BUY_TD9_PERFECTED"
        elif buy_td9:
            sig = "BUY_TD9"
        elif sell_perf:
            sig = "SELL_TD9_PERFECTED"
        elif sell_td9:
            sig = "SELL_TD9"
        else:
            sig = "NONE"

        st["buy_td9"] = buy_td9
        st["sell_td9"] = sell_td9
        st["buy_perfected"] = buy_perf
        st["sell_perfected"] = sell_perf
        st["signal"] = sig
        return self.get_state(tf)

    def get_state(self, tf: str) -> Dict[str, Any]:
        st = self._states.get(tf)
        if st is None:
            return self._neutral(tf)
        return {
            "timeframe": tf,
            "buy_count": st["buy_count"],
            "sell_count": st["sell_count"],
            "buy_td9": st["buy_td9"],
            "sell_td9": st["sell_td9"],
            "buy_perfected": st["buy_perfected"],
            "sell_perfected": st["sell_perfected"],
            "signal": st["signal"],
            "last_bar_ts": st["last_bar_ts"],
        }

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {tf: self.get_state(tf) for tf in self.SUPPORTED_TFS}


class SessionVWAPAccumulator:
    """Acumulador VWAP O(1) por sesión UTC (reinicio a las 00:00 UTC) con bandas ±1σ y ±2σ."""

    def __init__(self) -> None:
        self.session_day: Optional[int] = None
        self._last_minute_bucket: int = -1
        self.sum_w: float = 0.0
        self.vwap: float = 0.0
        self.m2: float = 0.0
        self.bar_count: int = 0

    def update(self, ts: float, high: float, low: float, close: float, volume: float) -> Dict[str, Any]:
        ts_i = int(ts)
        min_bucket = ts_i // 60
        if min_bucket <= self._last_minute_bucket:
            return self.snapshot()

        day = ts_i // 86400
        if self.session_day is None or day > self.session_day:
            self.session_day = day
            self.sum_w = 0.0
            self.vwap = 0.0
            self.m2 = 0.0
            self.bar_count = 0

        self._last_minute_bucket = min_bucket
        tp = (float(high) + float(low) + float(close)) / 3.0
        w = max(0.0, float(volume))

        if self.sum_w == 0.0 and w > 0.0:
            self.vwap = tp
            self.m2 = 0.0
            self.sum_w = w
            self.bar_count = 1
        elif w > 0.0:
            new_sum_w = self.sum_w + w
            delta = tp - self.vwap
            new_vwap = self.vwap + (w / new_sum_w) * delta
            self.m2 += w * delta * (tp - new_vwap)
            self.sum_w = new_sum_w
            self.vwap = new_vwap
            self.bar_count += 1
        elif self.sum_w == 0.0:
            self.vwap = tp

        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        std = math.sqrt(max(0.0, self.m2 / self.sum_w)) if self.sum_w > 0.0 else 0.0
        s_day = int(self.session_day) if self.session_day is not None else -1
        s_start = float(s_day * 86400) if s_day >= 0 else 0.0
        return {
            "session_day": s_day,
            "session_start_ts": s_start,
            "vwap": self.vwap,
            "std": std,
            "upper_1s": self.vwap + std,
            "lower_1s": self.vwap - std,
            "upper_2s": self.vwap + (2.0 * std),
            "lower_2s": self.vwap - (2.0 * std),
            "total_volume": self.sum_w,
            "bar_count": self.bar_count,
        }


class DeterministicMTFAggregator:
    """
    Agregador determinista en RAM desde fuente única 1m hacia 5m, 15m, 30m, 45m, 1h y 4h
    usando cubetas UTC fijas ts // (tf_min * 60), SessionVWAPAccumulator y TD9PerfectionTracker.
    """

    TIMEFRAME_MINUTES: Dict[str, int] = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "45m": 45,
        "1h": 60,
        "4h": 240,
    }
    DERIVED_TIMEFRAMES: Tuple[str, ...] = ("5m", "15m", "30m", "45m", "1h", "4h")

    def __init__(self, max_history: int = 300) -> None:
        self.max_history = max(16, int(max_history))
        self._tf_seconds: Dict[str, int] = {tf: mins * 60 for tf, mins in self.TIMEFRAME_MINUTES.items()}
        self._hist: Dict[str, Dict[str, deque]] = {
            tf: {
                "ts": deque(maxlen=self.max_history),
                "o": deque(maxlen=self.max_history),
                "h": deque(maxlen=self.max_history),
                "l": deque(maxlen=self.max_history),
                "c": deque(maxlen=self.max_history),
                "v": deque(maxlen=self.max_history),
            }
            for tf in self.TIMEFRAME_MINUTES
        }
        self._cur: Dict[str, Optional[List[Any]]] = {tf: None for tf in self.DERIVED_TIMEFRAMES}
        self._live_1m: Optional[Tuple[int, float, float, float, float, float, float]] = None
        self._closed_version: Dict[str, int] = {tf: 0 for tf in self.TIMEFRAME_MINUTES}
        self._last_closed_1m_bucket: int = -1
        self._hlc_cache: Dict[str, Tuple[Tuple[Any, ...], Tuple[Any, Any, Any]]] = {}
        self._ohlcv_cache: Dict[str, Tuple[Tuple[Any, ...], Tuple[Any, Any, Any, Any, Any, float]]] = {}
        self._closed_ohlcv_cache: Dict[str, Tuple[int, Tuple[Any, Any, Any, Any, Any, float]]] = {}
        self.session_vwap = SessionVWAPAccumulator()
        self.td9 = TD9PerfectionTracker()

    def _archive_bar(
        self, tf: str, bar_ts: float, o: float, h: float, l: float, c: float, v: float
    ) -> None:
        buf = self._hist[tf]
        buf["ts"].append(bar_ts)
        buf["o"].append(o)
        buf["h"].append(h)
        buf["l"].append(l)
        buf["c"].append(c)
        buf["v"].append(v)
        self._closed_version[tf] += 1

    def push_1m(
        self,
        ts: float,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float = 0.0,
        is_closed: bool = True,
    ) -> None:
        ts_f = float(ts)
        o_f = float(o)
        h_f = float(h)
        l_f = float(l)
        c_f = float(c)
        v_f = max(0.0, float(v))
        b1 = int(ts_f) // 60
        b1_start = float(b1 * 60)

        if not is_closed:
            if b1 > self._last_closed_1m_bucket:
                self._live_1m = (b1, b1_start, o_f, h_f, l_f, c_f, v_f)
            return

        if b1 <= self._last_closed_1m_bucket:
            return
        self._last_closed_1m_bucket = b1
        if self._live_1m is not None and self._live_1m[0] <= b1:
            self._live_1m = None

        self._archive_bar("1m", b1_start, o_f, h_f, l_f, c_f, v_f)
        self.session_vwap.update(b1_start, h_f, l_f, c_f, v_f)
        self.td9.update_bar("1m", b1_start, h_f, l_f, c_f)

        for tf in self.DERIVED_TIMEFRAMES:
            sec = self._tf_seconds[tf]
            b_tf = int(ts_f) // sec
            cur = self._cur[tf]
            if cur is not None and b_tf > cur[0]:
                if not cur[7]:
                    self._archive_bar(tf, cur[1], cur[2], cur[3], cur[4], cur[5], cur[6])
                    if tf in ("5m", "15m"):
                        self.td9.update_bar(tf, cur[1], cur[3], cur[4], cur[5])
                cur = None

            if cur is None:
                b_start = float(b_tf * sec)
                cur = [b_tf, b_start, o_f, h_f, l_f, c_f, v_f, False]
                self._cur[tf] = cur
            else:
                if h_f > cur[3]:
                    cur[3] = h_f
                if l_f < cur[4]:
                    cur[4] = l_f
                cur[5] = c_f
                cur[6] += v_f

            if (((b1 + 1) * 60) // sec) > b_tf and not cur[7]:
                cur[7] = True
                self._archive_bar(tf, cur[1], cur[2], cur[3], cur[4], cur[5], cur[6])
                if tf in ("5m", "15m"):
                    self.td9.update_bar(tf, cur[1], cur[3], cur[4], cur[5])

    def ingest_1m_series(
        self,
        ts_seq: Sequence[float],
        o_seq: Sequence[float],
        h_seq: Sequence[float],
        l_seq: Sequence[float],
        c_seq: Sequence[float],
        v_seq: Optional[Sequence[float]] = None,
        last_is_live: bool = False,
    ) -> None:
        n = min(len(ts_seq), len(o_seq), len(h_seq), len(l_seq), len(c_seq))
        if n <= 0:
            return
        for idx in range(n):
            vol = float(v_seq[idx]) if v_seq is not None and idx < len(v_seq) else 0.0
            closed = (not last_is_live) or (idx < n - 1)
            self.push_1m(
                ts_seq[idx],
                o_seq[idx],
                h_seq[idx],
                l_seq[idx],
                c_seq[idx],
                v=vol,
                is_closed=closed,
            )

    def closed_count(self, tf: str) -> int:
        buf = self._hist.get(tf)
        return len(buf["c"]) if buf is not None else 0

    def has_enough_bars(self, tf: str, min_bars: int = 1) -> bool:
        return self.closed_count(tf) >= int(min_bars)

    def get_closed_ohlcv(self, tf: str) -> Tuple[Any, Any, Any, Any, Any, float]:
        ver = self._closed_version.get(tf, -1)
        cached = self._closed_ohlcv_cache.get(tf)
        if cached is not None and cached[0] == ver:
            return cached[1]
        buf = self._hist.get(tf)
        if buf is None or not buf["c"]:
            z = np.zeros(0, dtype=np.float64)
            res = (z, z, z, z, z, 0.0)
            self._closed_ohlcv_cache[tf] = (ver, res)
            return res
        res = (
            np.array(buf["o"], dtype=np.float64),
            np.array(buf["h"], dtype=np.float64),
            np.array(buf["l"], dtype=np.float64),
            np.array(buf["c"], dtype=np.float64),
            np.array(buf["v"], dtype=np.float64),
            float(buf["ts"][-1]),
        )
        self._closed_ohlcv_cache[tf] = (ver, res)
        return res

    def _forming_bar(self, tf: str) -> Optional[Tuple[float, float, float, float, float, float]]:
        live = self._live_1m
        if tf == "1m":
            if live is None:
                return None
            return (live[1], live[2], live[3], live[4], live[5], live[6])

        cur = self._cur.get(tf)
        forming = None if (cur is None or cur[7]) else (cur[1], cur[2], cur[3], cur[4], cur[5], cur[6])
        if live is None:
            return forming

        sec = self._tf_seconds[tf]
        b_live = int(live[1]) // sec
        if cur is not None and (not cur[7]) and b_live == cur[0]:
            return (
                cur[1],
                cur[2],
                max(cur[3], live[3]),
                min(cur[4], live[4]),
                live[5],
                cur[6] + live[6],
            )
        if cur is None or b_live > cur[0]:
            return (float(b_live * sec), live[2], live[3], live[4], live[5], live[6])
        return forming

    def get_ohlcv(self, tf: str, include_forming: bool = True) -> Tuple[Any, Any, Any, Any, Any, float]:
        if not include_forming:
            return self.get_closed_ohlcv(tf)
        ver = self._closed_version.get(tf, -1)
        forming = self._forming_bar(tf)
        cache_key = (ver, forming)
        cached = self._ohlcv_cache.get(tf)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        buf = self._hist.get(tf)
        if forming is None:
            return self.get_closed_ohlcv(tf)
        if buf is None or not buf["c"]:
            res = (
                np.array([forming[1]], dtype=np.float64),
                np.array([forming[2]], dtype=np.float64),
                np.array([forming[3]], dtype=np.float64),
                np.array([forming[4]], dtype=np.float64),
                np.array([forming[5]], dtype=np.float64),
                float(forming[0]),
            )
        else:
            res = (
                np.array(list(buf["o"]) + [forming[1]], dtype=np.float64),
                np.array(list(buf["h"]) + [forming[2]], dtype=np.float64),
                np.array(list(buf["l"]) + [forming[3]], dtype=np.float64),
                np.array(list(buf["c"]) + [forming[4]], dtype=np.float64),
                np.array(list(buf["v"]) + [forming[5]], dtype=np.float64),
                float(forming[0]),
            )
        self._ohlcv_cache[tf] = (cache_key, res)
        return res

    def get_hlc_arrays(self, tf: str, include_forming: bool = True) -> Tuple[Any, Any, Any]:
        ver = self._closed_version.get(tf, -1)
        forming = self._forming_bar(tf) if include_forming else None
        cache_key = (include_forming, ver, forming)
        cached = self._hlc_cache.get(tf)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        _, h_arr, l_arr, c_arr, _, _ = self.get_ohlcv(tf, include_forming=include_forming)
        res = (h_arr, l_arr, c_arr)
        self._hlc_cache[tf] = (cache_key, res)
        return res

    def get_session_vwap(self) -> Dict[str, Any]:
        return self.session_vwap.snapshot()

    def get_td9_state(self) -> Dict[str, Dict[str, Any]]:
        return self.td9.snapshot()


def _neutral_signal() -> Dict[str, Any]:
    return {
        "liquidity_score": 0.0,
        "event_type": "NONE",
        "confirmation": "NONE",
        "timeframe": "NONE",
        "swept_level": 0.0,
        "wick_extreme": 0.0,
        "nearest_high_pool": 0.0,
        "nearest_high_touches": 0,
        "nearest_low_pool": 0.0,
        "nearest_low_touches": 0,
        "pool_touches": 0,
        "pierce_atr": 0.0,
        "wick_ratio": 0.0,
        "vol_ratio": 0.0,
        "bars_since_sweep": -1,
        "active_high_pools": 0,
        "active_low_pools": 0,
    }


def _safe_open(
    idx: int,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    opens: Optional[Sequence[float]],
) -> float:
    if opens is not None and idx < len(opens):
        o = float(opens[idx])
    elif idx > 0:
        o = float(closes[idx - 1])
    else:
        o = float(closes[idx])
    lo = float(lows[idx])
    hi = float(highs[idx])
    if o < lo:
        return lo
    if o > hi:
        return hi
    return o


def _compute_vol_ratio(volumes: Optional[Sequence[float]], idx: int) -> float:
    if volumes is None or idx <= 0 or idx >= len(volumes):
        return 0.0
    v_cur = float(volumes[idx])
    if v_cur <= 0.0:
        return 0.0
    start = max(0, idx - 20)
    win = [float(volumes[i]) for i in range(start, idx) if float(volumes[i]) > 0.0]
    if not win:
        return 0.0
    sma = sum(win) / float(len(win))
    if sma <= 1e-12:
        return 0.0
    return v_cur / sma


def detect_causal_swings(
    highs: Sequence[float],
    lows: Sequence[float],
    k: int,
    lookback: int,
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """
    Detecta pivotes Swing High y Swing Low estrictamente causales.
    El índice t = len(highs) - 1 es la vela viva; la última cerrada es t - 1.
    Un pivote en j requiere k velas cerradas a ambos lados -> j <= (t - 1) - k.
    """
    n = len(highs)
    if n < (2 * k + 3) or k < 1:
        return [], []

    t = n - 1
    j_max = (t - 1) - k
    j_min = max(k, t - max(lookback, 2 * k + 2))
    if j_min > j_max:
        return [], []

    swing_highs: List[Tuple[int, float]] = []
    swing_lows: List[Tuple[int, float]] = []

    for j in range(j_min, j_max + 1):
        hj = float(highs[j])
        lj = float(lows[j])

        is_sh = True
        for m in range(j - k, j):
            if float(highs[m]) > hj:
                is_sh = False
                break
        if is_sh:
            for m in range(j + 1, j + k + 1):
                if float(highs[m]) >= hj:
                    is_sh = False
                    break
        if is_sh:
            swing_highs.append((j, hj))

        is_sl = True
        for m in range(j - k, j):
            if float(lows[m]) < lj:
                is_sl = False
                break
        if is_sl:
            for m in range(j + 1, j + k + 1):
                if float(lows[m]) <= lj:
                    is_sl = False
                    break
        if is_sl:
            swing_lows.append((j, lj))

    return swing_highs, swing_lows


def cluster_swings_to_pools(
    swings: Sequence[Tuple[int, float]],
    side: str,
    timeframe: str,
    tolerance: float,
) -> List[LiquidityPool]:
    """Agrupa pivotes cercanos (<= tolerance) en clusters EQH/EQL."""
    if not swings:
        return []

    tol = max(0.0, float(tolerance))
    ordered = sorted(swings, key=lambda x: x[1])
    clusters: List[List[Tuple[int, float]]] = [[ordered[0]]]

    for item in ordered[1:]:
        prev_price = clusters[-1][-1][1]
        if abs(item[1] - prev_price) <= tol:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    pools: List[LiquidityPool] = []
    for grp in clusters:
        prices = [p for _, p in grp]
        indices = [idx for idx, _ in grp]
        level = max(prices) if side == "HIGH" else min(prices)
        pools.append(
            LiquidityPool(
                side=side,
                level=float(level),
                touches=len(grp),
                last_index=max(indices),
                timeframe=timeframe,
            )
        )
    return pools


def filter_unmitigated_pools(
    pools: Sequence[LiquidityPool],
    closes: Sequence[float],
    up_to_closed_idx: int,
    atr: float,
    mitigation_atr_mult: float,
    spot_ref: float,
    max_keep: int,
) -> List[LiquidityPool]:
    """Descarta pools cuya liquidez fue barrida por cierre estructural (> mitigation_atr_mult * ATR)."""
    if not pools:
        return []

    mit_dist = max(0.0, float(mitigation_atr_mult)) * max(1.0, float(atr))
    n_c = len(closes)
    end_idx = min(up_to_closed_idx, n_c - 1)
    active: List[LiquidityPool] = []

    for pool in pools:
        mitigated = False
        start_m = pool.last_index + 1
        if start_m <= end_idx:
            if pool.side == "LOW":
                thresh = pool.level - mit_dist
                for m in range(start_m, end_idx + 1):
                    if float(closes[m]) < thresh:
                        mitigated = True
                        break
            else:
                thresh = pool.level + mit_dist
                for m in range(start_m, end_idx + 1):
                    if float(closes[m]) > thresh:
                        mitigated = True
                        break
        if not mitigated:
            active.append(pool)

    active.sort(key=lambda p: abs(p.level - spot_ref))
    return active[: max(1, int(max_keep))]


def compute_opposite_pool_room(
    side_is_long: bool,
    entry_price: float,
    margin_sats: int,
    leverage: float,
    taker_fee: float,
    liquidity_signal: Optional[Dict[str, Any]],
    min_wall_fee_ratio: float = 1.80,
    min_wall_touches: int = 2,
) -> Dict[str, Any]:
    """
    Calcula el espacio bruto en sats (contrato inverso BTCUSD) hasta el pool
    opuesto más cercano y determina si existe un muro EQH/EQL demasiado cerca.
    """
    p0 = max(1.0, float(entry_price or 0.0))
    m_sats = max(1, int(margin_sats or 1000))
    lev = max(1.0, float(leverage or 1.0))
    c_rt = max(1e-6, 2.0 * float(m_sats) * lev * max(0.0, float(taker_fee)))

    if not isinstance(liquidity_signal, dict):
        return {
            "opposite_pool": 0.0,
            "opposite_touches": 0,
            "room_gross_sats": 0.0,
            "room_fee_ratio": 999.0,
            "blocked_by_wall": False,
        }

    if side_is_long:
        p_opp = float(liquidity_signal.get("nearest_high_pool") or 0.0)
        opp_touches = int(liquidity_signal.get("nearest_high_touches") or 0)
        valid_dir = p_opp > p0
        room_sats = (float(m_sats) * lev * ((p_opp - p0) / p_opp)) if valid_dir else 0.0
    else:
        p_opp = float(liquidity_signal.get("nearest_low_pool") or 0.0)
        opp_touches = int(liquidity_signal.get("nearest_low_touches") or 0)
        valid_dir = 0.0 < p_opp < p0
        room_sats = (float(m_sats) * lev * ((p0 - p_opp) / p_opp)) if valid_dir else 0.0

    if not valid_dir:
        return {
            "opposite_pool": 0.0,
            "opposite_touches": 0,
            "room_gross_sats": 0.0,
            "room_fee_ratio": 999.0,
            "blocked_by_wall": False,
        }

    ratio = room_sats / c_rt
    blocked = bool(opp_touches >= int(min_wall_touches) and ratio < float(min_wall_fee_ratio))
    return {
        "opposite_pool": round(p_opp, 2),
        "opposite_touches": opp_touches,
        "room_gross_sats": round(room_sats, 2),
        "room_fee_ratio": round(ratio, 3),
        "blocked_by_wall": blocked,
    }


def compute_liquidity_bias(
    action: int,
    liquidity_signal: Optional[Dict[str, Any]],
    cfg: Optional[LiquidityConfig] = None,
    num_actions: int = 9,
) -> float:
    """
    Calcula el término liq_bias acotado para compute_live_edge(simulated_entry=True).
    Respeta cfg.enabled y cfg.shadow_only (en modo sombra devuelve estrictamente 0.0).
    """
    conf = cfg or LiquidityConfig()
    if (not conf.enabled) or conf.shadow_only or not isinstance(liquidity_signal, dict):
        return 0.0

    act_idx = int(action) % max(1, int(num_actions))
    if act_idx == 0:
        return 0.0

    score = float(liquidity_signal.get("liquidity_score") or 0.0)
    if abs(score) < 1e-9:
        return 0.0

    is_long = (1 <= act_idx <= 4)
    align = score if is_long else -score
    if align > 0.0:
        bias = conf.max_liq_bonus * min(1.0, align)
    else:
        bias = conf.max_liq_penalty * max(-1.0, align)
    return round(float(bias), 4)


class LiquidityRadar:
    """
    Radar stateful con caché O(1) de pools por cierre de vela.
    Cada tick de 5s solo ejecuta la evaluación ligera contra <= 10 pools por lado.
    """

    def __init__(self, config: Optional[LiquidityConfig] = None):
        self.config = config or LiquidityConfig()
        self._pool_cache: Dict[str, Tuple[Tuple[Any, ...], List[LiquidityPool], List[LiquidityPool]]] = {}
        self._super_pool_cache: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    def clear_cache(self) -> None:
        self._pool_cache.clear()
        self._super_pool_cache.clear()

    @staticmethod
    def _closed_fingerprint(
        tf: str,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        atr: float,
        bar_ts: Optional[float],
    ) -> Tuple[Any, ...]:
        n = len(closes)
        if bar_ts is not None and bar_ts > 0:
            return (tf, n, float(bar_ts), round(float(atr), 4))
        c0 = round(float(closes[0]), 4)
        c_m2 = round(float(closes[-2]), 4)
        h_m2 = round(float(highs[-2]), 4)
        l_m2 = round(float(lows[-2]), 4)
        c_m3 = round(float(closes[-3]), 4) if n >= 3 else 0.0
        return (tf, n, c0, c_m3, h_m2, l_m2, c_m2, round(float(atr), 4))

    def _get_or_build_pools(
        self,
        tf: str,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        atr: float,
        cfg: LiquidityConfig,
        bar_ts: Optional[float] = None,
    ) -> Tuple[List[LiquidityPool], List[LiquidityPool]]:
        fp = self._closed_fingerprint(tf, highs, lows, closes, atr, bar_ts)
        cached = self._pool_cache.get(tf)
        if cached is not None and cached[0] == fp:
            return cached[1], cached[2]

        if tf == "5m":
            k = cfg.swing_k_5m
            lookback = cfg.lookback_5m
        elif tf == "15m":
            k = cfg.swing_k_15m
            lookback = cfg.lookback_15m
        else:
            k = cfg.swing_k_htf
            lookback = cfg.lookback_htf
        sh, sl = detect_causal_swings(highs, lows, k=k, lookback=lookback)
        tol = cfg.cluster_atr_mult * max(1.0, float(atr))
        raw_highs = cluster_swings_to_pools(sh, "HIGH", tf, tol)
        raw_lows = cluster_swings_to_pools(sl, "LOW", tf, tol)

        t = len(closes) - 1
        spot_ref = float(closes[-2]) if t >= 1 else float(closes[-1])
        active_highs = filter_unmitigated_pools(
            raw_highs,
            closes,
            up_to_closed_idx=t - 1,
            atr=atr,
            mitigation_atr_mult=cfg.mitigation_atr_mult,
            spot_ref=spot_ref,
            max_keep=cfg.max_active_pools_per_side,
        )
        active_lows = filter_unmitigated_pools(
            raw_lows,
            closes,
            up_to_closed_idx=t - 1,
            atr=atr,
            mitigation_atr_mult=cfg.mitigation_atr_mult,
            spot_ref=spot_ref,
            max_keep=cfg.max_active_pools_per_side,
        )
        self._pool_cache[tf] = (fp, active_highs, active_lows)
        return active_highs, active_lows

    def _evaluate_timeframe(
        self,
        tf: str,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        atr: float,
        spot_price: float,
        cfg: LiquidityConfig,
        opens: Optional[Sequence[float]] = None,
        volumes: Optional[Sequence[float]] = None,
        bar_ts: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], List[LiquidityPool], List[LiquidityPool]]:
        n = len(closes)
        k = cfg.swing_k_15m if tf == "15m" else cfg.swing_k_5m
        if n < (2 * k + 4):
            return None, [], []

        atr_f = max(1.0, float(atr))
        spot = float(spot_price) if float(spot_price or 0.0) > 0.0 else float(closes[-1])
        t = n - 1

        active_highs, active_lows = self._get_or_build_pools(
            tf, highs, lows, closes, atr_f, cfg, bar_ts=bar_ts
        )

        tf_mult = 1.25 if tf == "15m" else 1.00
        ttl_map = (1.00, 0.50) if tf == "15m" else (1.00, 0.65, 0.35)
        max_d = len(ttl_map) - 1

        best: Optional[Dict[str, Any]] = None

        def _consider(cand: Dict[str, Any]) -> None:
            nonlocal best
            if best is None:
                best = cand
                return
            a_new = abs(cand["liquidity_score"])
            a_old = abs(best["liquidity_score"])
            if a_new > a_old + 1e-9:
                best = cand
            elif abs(a_new - a_old) <= 1e-9 and cand["bars_since_sweep"] < best["bars_since_sweep"]:
                best = cand

        # Evaluar barras candidatas: intrabar (c=t, d=0) y cerradas recientes (c=t-1-d)
        eval_bars: List[Tuple[int, int, bool]] = [(t, 0, True)]
        for d in range(0, max_d + 1):
            c_idx = (t - 1) - d
            if c_idx >= 1:
                eval_bars.append((c_idx, d, False))

        for c_idx, d, is_intrabar in eval_bars:
            ttl_decay = ttl_map[d]
            confirm_mult = 0.70 if is_intrabar else 1.00
            min_wick = cfg.min_wick_ratio_intrabar if is_intrabar else cfg.min_wick_ratio_close

            h_c = max(float(highs[c_idx]), spot) if is_intrabar else float(highs[c_idx])
            l_c = min(float(lows[c_idx]), spot) if is_intrabar else float(lows[c_idx])
            c_c = spot if is_intrabar else float(closes[c_idx])
            o_c = _safe_open(c_idx, highs, lows, closes, opens)
            bar_range = max(h_c - l_c, 1e-8)

            vol_ratio = _compute_vol_ratio(volumes, c_idx) if not is_intrabar else 0.0
            vol_bonus = cfg.vol_bonus if vol_ratio >= cfg.vol_confirm_ratio else 0.0

            # 1. BULL_SWEEP_RECLAIM sobre pools LOW
            for pool in active_lows:
                if pool.last_index >= c_idx:
                    continue
                pierce = (pool.level - l_c) / atr_f
                if not (cfg.min_pierce_atr <= pierce <= cfg.max_pierce_atr):
                    continue

                if is_intrabar:
                    if spot <= pool.level + (cfg.intrabar_reclaim_buf_atr * atr_f):
                        continue
                else:
                    if c_c <= pool.level:
                        continue
                    # Invariante de vigencia post-barrida: el extremo de mecha no debe haberse roto después
                    invalidated = (spot < l_c)
                    if not invalidated:
                        for m in range(c_idx + 1, t):
                            if float(lows[m]) < l_c:
                                invalidated = True
                                break
                    if invalidated:
                        continue

                body_low = max(l_c, min(o_c, c_c))
                wick_ratio = (body_low - l_c) / bar_range
                if wick_ratio < min_wick:
                    continue

                mag = min(1.0, (0.50 * pool.weight * tf_mult * confirm_mult * ttl_decay) + (vol_bonus * ttl_decay))
                _consider(
                    {
                        "liquidity_score": round(mag, 4),
                        "event_type": "BULL_SWEEP_RECLAIM",
                        "confirmation": "INTRABAR_RECLAIM" if is_intrabar else "CONFIRMED_CLOSE",
                        "timeframe": tf,
                        "swept_level": round(pool.level, 2),
                        "wick_extreme": round(l_c, 2),
                        "pool_touches": pool.touches,
                        "pierce_atr": round(pierce, 3),
                        "wick_ratio": round(wick_ratio, 3),
                        "vol_ratio": round(vol_ratio, 3),
                        "bars_since_sweep": d,
                    }
                )

            # 2. BEAR_SWEEP_RECLAIM sobre pools HIGH
            for pool in active_highs:
                if pool.last_index >= c_idx:
                    continue
                pierce = (h_c - pool.level) / atr_f
                if not (cfg.min_pierce_atr <= pierce <= cfg.max_pierce_atr):
                    continue

                if is_intrabar:
                    if spot >= pool.level - (cfg.intrabar_reclaim_buf_atr * atr_f):
                        continue
                else:
                    if c_c >= pool.level:
                        continue
                    invalidated = (spot > h_c)
                    if not invalidated:
                        for m in range(c_idx + 1, t):
                            if float(highs[m]) > h_c:
                                invalidated = True
                                break
                    if invalidated:
                        continue

                body_high = min(h_c, max(o_c, c_c))
                wick_ratio = (h_c - body_high) / bar_range
                if wick_ratio < min_wick:
                    continue

                mag = min(1.0, (0.50 * pool.weight * tf_mult * confirm_mult * ttl_decay) + (vol_bonus * ttl_decay))
                _consider(
                    {
                        "liquidity_score": round(-mag, 4),
                        "event_type": "BEAR_SWEEP_RECLAIM",
                        "confirmation": "INTRABAR_RECLAIM" if is_intrabar else "CONFIRMED_CLOSE",
                        "timeframe": tf,
                        "swept_level": round(pool.level, 2),
                        "wick_extreme": round(h_c, 2),
                        "pool_touches": pool.touches,
                        "pierce_atr": round(pierce, 3),
                        "wick_ratio": round(wick_ratio, 3),
                        "vol_ratio": round(vol_ratio, 3),
                        "bars_since_sweep": d,
                    }
                )

        return best, active_highs, active_lows

    def evaluate(
        self,
        candles_5m: Optional[Tuple[Sequence[float], Sequence[float], Sequence[float]]],
        candles_15m: Optional[Tuple[Sequence[float], Sequence[float], Sequence[float]]],
        atr_5m: float,
        atr_15m: float,
        spot_price: float,
        opens_5m: Optional[Sequence[float]] = None,
        opens_15m: Optional[Sequence[float]] = None,
        volumes_5m: Optional[Sequence[float]] = None,
        volumes_15m: Optional[Sequence[float]] = None,
        bar_ts_5m: Optional[float] = None,
        bar_ts_15m: Optional[float] = None,
        config_override: Optional[LiquidityConfig] = None,
    ) -> Dict[str, Any]:
        cfg = config_override or self.config
        out = _neutral_signal()
        if not cfg.enabled:
            return out

        spot = float(spot_price or 0.0)
        all_highs: List[LiquidityPool] = []
        all_lows: List[LiquidityPool] = []
        cands: List[Dict[str, Any]] = []

        if candles_15m is not None and len(candles_15m) == 3:
            c15, h15, l15 = self._evaluate_timeframe(
                "15m",
                candles_15m[0],
                candles_15m[1],
                candles_15m[2],
                atr_15m,
                spot,
                cfg,
                opens=opens_15m,
                volumes=volumes_15m,
                bar_ts=bar_ts_15m,
            )
            all_highs.extend(h15)
            all_lows.extend(l15)
            if c15 is not None:
                cands.append(c15)

        if candles_5m is not None and len(candles_5m) == 3:
            c5, h5, l5 = self._evaluate_timeframe(
                "5m",
                candles_5m[0],
                candles_5m[1],
                candles_5m[2],
                atr_5m,
                spot,
                cfg,
                opens=opens_5m,
                volumes=volumes_5m,
                bar_ts=bar_ts_5m,
            )
            all_highs.extend(h5)
            all_lows.extend(l5)
            if c5 is not None:
                cands.append(c5)

        if cands:
            # Mayor |score|; desempate a favor de 15m, luego menor edad
            cands.sort(
                key=lambda x: (
                    abs(x["liquidity_score"]),
                    1 if x["timeframe"] == "15m" else 0,
                    -x["bars_since_sweep"],
                ),
                reverse=True,
            )
            out.update(cands[0])

        # Pools opuestos más cercanos respecto al spot (priorizando clusters EQH/EQL si existen a distancia válida)
        above = [p for p in all_highs if p.level > spot]
        below = [p for p in all_lows if 0.0 < p.level < spot]
        if above:
            walls_above = [p for p in above if p.touches >= cfg.min_wall_touches]
            nearest_hi = min(walls_above or above, key=lambda p: p.level - spot)
            out["nearest_high_pool"] = round(nearest_hi.level, 2)
            out["nearest_high_touches"] = nearest_hi.touches
        if below:
            walls_below = [p for p in below if p.touches >= cfg.min_wall_touches]
            nearest_lo = min(walls_below or below, key=lambda p: spot - p.level)
            out["nearest_low_pool"] = round(nearest_lo.level, 2)
            out["nearest_low_touches"] = nearest_lo.touches

        out["active_high_pools"] = len(all_highs)
        out["active_low_pools"] = len(all_lows)
        return out

    def evaluate_multi_tf_hierarchy(
        self,
        tf_ohlcv: Dict[str, Tuple[Sequence[float], Sequence[float], Sequence[float], Optional[float]]],
        atr_ref: float,
        spot_price: float,
        atr_by_tf: Optional[Dict[str, float]] = None,
        config_override: Optional[LiquidityConfig] = None,
    ) -> Dict[str, Any]:
        """
        Construye en O(1) amortizado la jerarquía multi-TF de pools (15m..4h) y Super-Pools
        fusionados (15m/30m dentro de 0.25*ATR de 1h/4h).
        """
        cfg = config_override or self.config
        atr_base = max(1.0, float(atr_ref or 1.0))
        spot = float(spot_price or 0.0)
        ordered_tfs = ("15m", "30m", "45m", "1h", "4h")

        fps: List[Any] = []
        valid_entries: List[Tuple[str, Sequence[float], Sequence[float], Sequence[float], Optional[float], float]] = []
        for tf in ordered_tfs:
            item = tf_ohlcv.get(tf)
            if item is None or len(item) < 3:
                continue
            highs, lows, closes = item[0], item[1], item[2]
            bar_ts = item[3] if len(item) >= 4 else None
            k_tf = cfg.swing_k_15m if tf == "15m" else cfg.swing_k_htf
            if len(closes) < (2 * k_tf + 3):
                continue
            tf_atr = max(1.0, float((atr_by_tf or {}).get(tf) or atr_base))
            fps.append(self._closed_fingerprint(tf, highs, lows, closes, tf_atr, bar_ts))
            valid_entries.append((tf, highs, lows, closes, bar_ts, tf_atr))

        sp_key = (tuple(fps), round(atr_base, 4), round(cfg.super_pool_atr_mult, 4))
        cached = self._super_pool_cache.get(sp_key)
        if cached is not None:
            high_pools = cached["high_pools"]
            low_pools = cached["low_pools"]
            super_high = cached["super_pools_high"]
            super_low = cached["super_pools_low"]
            super_count = cached["super_pool_count"]
            total_pools = cached["total_pools"]
        else:
            pools_by_tf: Dict[str, List[LiquidityPool]] = {}
            for tf, highs, lows, closes, bar_ts, tf_atr in valid_entries:
                ah, al = self._get_or_build_pools(tf, highs, lows, closes, tf_atr, cfg, bar_ts=bar_ts)
                pools_by_tf[tf] = ah + al
            high_pools, low_pools = fuse_super_pools(
                pools_by_tf, atr=atr_base, fusion_atr_mult=cfg.super_pool_atr_mult
            )
            super_high = [p for p in high_pools if p.is_super_pool]
            super_low = [p for p in low_pools if p.is_super_pool]
            super_count = len(super_high) + len(super_low)
            total_pools = len(high_pools) + len(low_pools)
            if len(self._super_pool_cache) > 16:
                self._super_pool_cache.clear()
            self._super_pool_cache[sp_key] = {
                "high_pools": high_pools,
                "low_pools": low_pools,
                "super_pools_high": super_high,
                "super_pools_low": super_low,
                "super_pool_count": super_count,
                "total_pools": total_pools,
            }

        nearest_hi = 0.0
        nearest_lo = 0.0
        if spot > 0.0:
            hi_cands = [
                p.level for p in high_pools if p.anchor_timeframe in ("45m", "1h") and p.level > spot
            ]
            lo_cands = [
                p.level for p in low_pools if p.anchor_timeframe in ("45m", "1h") and 0.0 < p.level < spot
            ]
            if hi_cands:
                nearest_hi = round(min(hi_cands), 2)
            if lo_cands:
                nearest_lo = round(max(lo_cands), 2)

        return {
            "high_pools": high_pools,
            "low_pools": low_pools,
            "super_pools_high": super_high,
            "super_pools_low": super_low,
            "nearest_high_45m_1h": nearest_hi,
            "nearest_low_45m_1h": nearest_lo,
            "super_pool_count": super_count,
            "total_pools": total_pools,
        }


def compute_structural_td9_trailing_stop(
    side_is_long: bool,
    spot_price: float,
    current_sl: float,
    entry_price: float,
    atr: float,
    td9_state: Optional[Dict[str, Any]],
    hierarchy_pools: Optional[Dict[str, Any]],
    enabled: bool = False,
    buffer_atr: float = 0.10,
    td9_timeframes: Tuple[str, ...] = ("15m", "5m", "1m"),
) -> Optional[float]:
    """
    Hook de trailing estructural TD9 Perfection + pool 45m/1h.
    Inactivo por defecto (enabled=False -> None) para aislar Pata A hasta Fase 2.
    """
    if (not enabled) or (not isinstance(td9_state, dict)) or (not isinstance(hierarchy_pools, dict)):
        return None

    spot = float(spot_price or 0.0)
    entry = float(entry_price or 0.0)
    cur_sl = float(current_sl or 0.0)
    atr_f = max(1.0, float(atr or 1.0))
    if spot <= 0.0 or entry <= 0.0:
        return None

    if side_is_long:
        has_td9 = any(bool((td9_state.get(tf) or {}).get("sell_td9_perfected", False)) for tf in td9_timeframes)
        if not has_td9:
            return None
        max_allowed = spot - (float(buffer_atr) * atr_f)
        cands = [
            float(p.level)
            for p in (hierarchy_pools.get("low_pools") or ())
            if getattr(p, "anchor_timeframe", "") in ("45m", "1h")
            and entry < float(p.level) < max_allowed
            and float(p.level) > cur_sl
        ]
        return round(max(cands), 2) if cands else None

    has_td9 = any(bool((td9_state.get(tf) or {}).get("buy_td9_perfected", False)) for tf in td9_timeframes)
    if not has_td9:
        return None
    min_allowed = spot + (float(buffer_atr) * atr_f)
    cands = [
        float(p.level)
        for p in (hierarchy_pools.get("high_pools") or ())
        if getattr(p, "anchor_timeframe", "") in ("45m", "1h")
        and min_allowed < float(p.level) < entry
        and (cur_sl <= 0.0 or float(p.level) < cur_sl)
    ]
    return round(min(cands), 2) if cands else None
