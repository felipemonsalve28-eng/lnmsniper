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

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
import math


@dataclass(frozen=True)
class LiquidityConfig:
    enabled: bool = True
    shadow_only: bool = True
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
    timeframe: str     # "5m" | "15m"

    @property
    def weight(self) -> float:
        if self.touches >= 3:
            return 1.60
        if self.touches == 2:
            return 1.35
        return 1.00


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

    def clear_cache(self) -> None:
        self._pool_cache.clear()

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

        k = cfg.swing_k_15m if tf == "15m" else cfg.swing_k_5m
        lookback = cfg.lookback_15m if tf == "15m" else cfg.lookback_5m
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
