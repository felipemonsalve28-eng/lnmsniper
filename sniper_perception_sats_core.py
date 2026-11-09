# -*- coding: utf-8 -*-
"""
sniper_perception_sats_core.py
Motor de Percepción en Tiempo Real (Proxies OHLCV 5m/15m + Confirmación Continua),
Detector de Regímenes de Mercado (RANGE_SQUEEZE, TREND_BEAR, TREND_BULL, RANGE,
VOLATILITY_EXPANSION, EXTREME_VOLATILITY), Detector de Giro en V con Histéresis,
Adaptación Dinámica de Riesgo y Compuerta de Expectativa de Sats Netos con
Contabilidad Económica Completa (Taker Fee, Funding, Spread y Slippage).
"""
import time
import math
from collections import deque


class MarketRegimeClassifier:
    """
    Clasificador sin duplicación de indicadores que evalúa:
    - RANGE_SQUEEZE
    - TREND_BEAR
    - TREND_BULL
    - RANGE (compatible con RANGE_NORMAL)
    - VOLATILITY_EXPANSION
    - EXTREME_VOLATILITY
    """
    VALID_REGIMES = (
        "RANGE_SQUEEZE",
        "TREND_BEAR",
        "TREND_BULL",
        "RANGE",
        "RANGE_NORMAL",
        "VOLATILITY_EXPANSION",
        "EXTREME_VOLATILITY",
        "CHOP_HIGHVOL",
    )

    @classmethod
    def classify(cls, ind_5m, ind_15m=None, ind_1h=None):
        price = max(float(ind_5m.get("price", 1.0)), 1.0)
        atr = float(ind_5m.get("atr", price * 0.0015))
        atr_pct = float(ind_5m.get("atr_pct", atr / price))
        atr_rel = float(ind_5m.get("atr_rel_ratio", 1.0))
        bb_width_pct = float(ind_5m.get("bb_width_pct", 0.012))
        bb_expansion_rate = float(ind_5m.get("bb_expansion_rate", 1.0))
        ema9 = float(ind_5m.get("ema9", price))
        ema21 = float(ind_5m.get("ema21", price))
        trend_spread_pct = abs(ema9 - ema21) / price
        ema_score_5m = float(ind_5m.get("ema_score", 0.0))
        ema_score_15m = float(ind_15m.get("ema_score", ema_score_5m)) if isinstance(ind_15m, dict) else ema_score_5m
        ema_score_1h = float(ind_1h.get("ema_score", 0.0)) if isinstance(ind_1h, dict) else 0.0
        mtf_alignment = (0.45 * ema_score_5m) + (0.35 * ema_score_15m) + (0.20 * ema_score_1h)

        # 1. EXTREME_VOLATILITY: volatilidad desbordada o expansión violenta sin estructura limpia
        if atr_pct >= 0.0085 or atr_rel >= 2.15 or (atr_pct > 0.0072 and trend_spread_pct < 0.0016):
            return {
                "market_regime": "EXTREME_VOLATILITY",
                "canonical_regime": "CHOP_HIGHVOL",
                "atr_rel_ratio": round(atr_rel, 3),
                "bb_width_pct": round(bb_width_pct, 5),
                "mtf_alignment": round(mtf_alignment, 3),
                "volatility_state": "EXTREME",
            }

        # 2. VOLATILITY_EXPANSION: bandas abriéndose rápido o ATR expandiéndose sobre su media
        is_expanding = (
            (atr_rel >= 1.32 and bb_expansion_rate >= 1.18 and bb_width_pct >= 0.0080)
            or (atr_pct >= 0.0045 and trend_spread_pct >= 0.0022)
        )
        if is_expanding:
            if ema9 > ema21 and mtf_alignment >= 0.15:
                canon = "TREND_BULL"
            elif ema9 < ema21 and mtf_alignment <= -0.15:
                canon = "TREND_BEAR"
            else:
                canon = "CHOP_HIGHVOL"
            return {
                "market_regime": "VOLATILITY_EXPANSION",
                "canonical_regime": canon,
                "atr_rel_ratio": round(atr_rel, 3),
                "bb_width_pct": round(bb_width_pct, 5),
                "mtf_alignment": round(mtf_alignment, 3),
                "volatility_state": "EXPANDING",
            }

        # 3. RANGE_SQUEEZE: compresión real (bandas estrechas y ATR contraído sin ruptura activa)
        if (bb_width_pct < 0.0075 and atr_rel <= 1.08) or (atr_pct < 0.0019 and trend_spread_pct < 0.0012):
            return {
                "market_regime": "RANGE_SQUEEZE",
                "canonical_regime": "RANGE_SQUEEZE",
                "atr_rel_ratio": round(atr_rel, 3),
                "bb_width_pct": round(bb_width_pct, 5),
                "mtf_alignment": round(mtf_alignment, 3),
                "volatility_state": "COMPRESSED",
            }

        # 4. TREND_BULL / TREND_BEAR confirmados por dirección, fuerza y confluencia MTF
        if ema9 > ema21 and trend_spread_pct >= 0.0010 and mtf_alignment >= -0.10:
            return {
                "market_regime": "TREND_BULL",
                "canonical_regime": "TREND_BULL",
                "atr_rel_ratio": round(atr_rel, 3),
                "bb_width_pct": round(bb_width_pct, 5),
                "mtf_alignment": round(mtf_alignment, 3),
                "volatility_state": "NORMAL",
            }
        if ema9 < ema21 and trend_spread_pct >= 0.0010 and mtf_alignment <= 0.10:
            return {
                "market_regime": "TREND_BEAR",
                "canonical_regime": "TREND_BEAR",
                "atr_rel_ratio": round(atr_rel, 3),
                "bb_width_pct": round(bb_width_pct, 5),
                "mtf_alignment": round(mtf_alignment, 3),
                "volatility_state": "NORMAL",
            }

        # 5. RANGE (mapeado a RANGE_NORMAL en claves Q existentes para compatibilidad 100%)
        return {
            "market_regime": "RANGE",
            "canonical_regime": "RANGE_NORMAL",
            "atr_rel_ratio": round(atr_rel, 3),
            "bb_width_pct": round(bb_width_pct, 5),
            "mtf_alignment": round(mtf_alignment, 3),
            "volatility_state": "NORMAL",
        }


class ShortTermPerceptionEngine:
    """
    Detecta aceleraciones de corto plazo (5m/15m), giros en V, micro-breakouts y
    regímenes de mercado con validación continua de momentum, decaimiento gradual e histéresis.
    """

    def __init__(self):
        self.price_history = deque(maxlen=60)
        self.override_side = None
        self.override_since = 0.0
        self.override_until = 0.0
        self.override_strength = 0.0
        self.override_reason = "OFF"
        self.last_flip_time = 0.0
        self.last_regime_info = {}

    def _record_sample(self, now, price, atr):
        if not self.price_history or (now - self.price_history[-1][0]) >= 15.0:
            self.price_history.append((float(now), float(price), float(atr)))

    def _extract_candle_proxies(self, ind_5m, ind_15m):
        price = float(ind_5m.get("price", 0.0))
        atr = max(float(ind_5m.get("atr", price * 0.0015)), price * 0.0008, 1.0)
        rsi_5m = float(ind_5m.get("rsi", 50.0))
        rsi_15m = float(ind_15m.get("rsi", 50.0))

        ema9_5m = float(ind_5m.get("ema9", ind_5m.get("ema_fast", price)))
        ema21_5m = float(ind_5m.get("ema21", ind_5m.get("ema_slow", price)))
        ema9_15m = float(ind_15m.get("ema9", ind_15m.get("ema_fast", price)))
        ema21_15m = float(ind_15m.get("ema21", ind_15m.get("ema_slow", price)))

        vel_ema_atr = (price - ema21_5m) / atr
        ema_spread_5m = (ema9_5m - ema21_5m) / atr
        ema_spread_15m = (ema9_15m - ema21_15m) / atr

        vel_window_atr = 0.0
        micro_high = price
        micro_low = price
        hist_len = len(self.price_history)
        if hist_len >= 3:
            start_idx = max(0, hist_len - 25)
            ref_idx = max(0, hist_len - 15)
            ref_price = self.price_history[ref_idx][1]
            vel_window_atr = (price - ref_price) / atr
            if (hist_len - 1) > start_idx:
                m_high = self.price_history[start_idx][1]
                m_low = m_high
                for idx in range(start_idx + 1, hist_len - 1):
                    p_val = self.price_history[idx][1]
                    if p_val > m_high:
                        m_high = p_val
                    if p_val < m_low:
                        m_low = p_val
                micro_high = m_high
                micro_low = m_low

        c_high = float(ind_5m.get("high", max(price, ema9_5m)))
        c_low = float(ind_5m.get("low", min(price, ema9_5m)))
        c_open = float(ind_5m.get("open", ema9_5m))
        c_range = max(c_high - c_low, 0.0)
        if c_range > 0.0:
            clv = ((price - c_low) - (c_high - price)) / c_range
            body_ratio = abs(price - c_open) / c_range
            range_exp = c_range / atr
        else:
            clv = max(-1.0, min(1.0, (rsi_5m - 50.0) / 22.0))
            body_ratio = min(1.0, abs(vel_ema_atr) / 1.5)
            range_exp = max(0.5, abs(vel_ema_atr))

        composite_vel_atr = (0.60 * vel_ema_atr) + (0.40 * vel_window_atr)

        breakout_bull = (
            (price > micro_high and composite_vel_atr >= 1.00 and rsi_5m >= 55.0)
            or (vel_ema_atr >= 1.35 and ema_spread_5m >= 0.28 and rsi_5m >= 56.5)
        )
        breakout_bear = (
            (price < micro_low and composite_vel_atr <= -1.00 and rsi_5m <= 45.0)
            or (vel_ema_atr <= -1.35 and ema_spread_5m <= -0.28 and rsi_5m <= 43.5)
        )

        return {
            "price": price,
            "atr": atr,
            "rsi_5m": rsi_5m,
            "rsi_15m": rsi_15m,
            "vel_ema_atr": round(vel_ema_atr, 3),
            "vel_window_atr": round(vel_window_atr, 3),
            "composite_vel_atr": round(composite_vel_atr, 3),
            "ema_spread_5m": round(ema_spread_5m, 3),
            "ema_spread_15m": round(ema_spread_15m, 3),
            "clv": round(clv, 3),
            "body_ratio": round(body_ratio, 3),
            "range_exp": round(range_exp, 3),
            "breakout_bull": bool(breakout_bull),
            "breakout_bear": bool(breakout_bear),
        }

    def evaluate(self, state, ind_5m, ind_15m, ind_1h, ind_4h, now=None):
        if now is None:
            now = time.time()
        price = float(ind_5m.get("price", 0.0))
        atr = max(float(ind_5m.get("atr", price * 0.0015)), 1.0)
        self._record_sample(now, price, atr)

        px = self._extract_candle_proxies(ind_5m, ind_15m)
        reg_info = MarketRegimeClassifier.classify(ind_5m, ind_15m, ind_1h)
        self.last_regime_info = reg_info

        comp_v = px["composite_vel_atr"]
        vel_ema = px["vel_ema_atr"]
        rsi_5m = px["rsi_5m"]
        rsi_15m = px["rsi_15m"]
        rsi_1h = float(ind_1h.get("rsi", 50.0))

        v_bull_trigger = (
            (vel_ema >= 1.35 and rsi_5m >= 55.5 and px["clv"] >= 0.20)
            or (px["breakout_bull"] and rsi_15m >= 52.0 and comp_v >= 1.05)
            or (rsi_5m >= 60.0 and rsi_15m >= 54.5 and rsi_1h >= 51.0 and vel_ema >= 1.00)
        )

        v_bear_trigger = (
            (vel_ema <= -1.35 and rsi_5m <= 44.5 and px["clv"] <= -0.20)
            or (px["breakout_bear"] and rsi_15m <= 48.0 and comp_v <= -1.05)
            or (rsi_5m <= 40.0 and rsi_15m <= 45.5 and rsi_1h <= 49.0 and vel_ema <= -1.00)
        )

        can_flip_side = (now - self.last_flip_time) >= 90.0 or abs(vel_ema) >= 1.85

        if v_bull_trigger and not v_bear_trigger:
            if self.override_side != "BULL":
                if self.override_side is None or can_flip_side:
                    self.override_side = "BULL"
                    self.override_since = now
                    self.last_flip_time = now
            if self.override_side == "BULL":
                self.override_until = now + 480.0
                self.override_strength = min(2.5, max(1.0, abs(vel_ema)))
                self.override_reason = f"V_BULL(v={vel_ema:+.2f}ATR|rsi5={rsi_5m:.0f}|brk={int(px['breakout_bull'])})"
        elif v_bear_trigger and not v_bull_trigger:
            if self.override_side != "BEAR":
                if self.override_side is None or can_flip_side:
                    self.override_side = "BEAR"
                    self.override_since = now
                    self.last_flip_time = now
            if self.override_side == "BEAR":
                self.override_until = now + 480.0
                self.override_strength = min(2.5, max(1.0, abs(vel_ema)))
                self.override_reason = f"V_BEAR(v={vel_ema:+.2f}ATR|rsi5={rsi_5m:.0f}|brk={int(px['breakout_bear'])})"
        else:
            if self.override_side is not None:
                elapsed = max(0.0, now - self.override_since)
                decay_factor = max(0.0, 1.0 - (elapsed / 480.0))
                self.override_strength = round(self.override_strength * decay_factor, 3)

                if now >= self.override_until or self.override_strength < 0.35:
                    self.override_side = None
                    self.override_strength = 0.0
                    self.override_reason = "OFF_DECAY"
                elif self.override_side == "BULL" and (vel_ema < 0.15 or comp_v < 0.05 or rsi_5m < 49.0):
                    self.override_side = None
                    self.override_strength = 0.0
                    self.override_reason = "OFF_MOMENTUM_LOST"
                elif self.override_side == "BEAR" and (vel_ema > -0.15 or comp_v > -0.05 or rsi_5m > 51.0):
                    self.override_side = None
                    self.override_strength = 0.0
                    self.override_reason = "OFF_MOMENTUM_LOST"

        px["override_side"] = self.override_side
        px["override_strength"] = round(self.override_strength, 2)
        px["override_reason"] = self.override_reason
        px["market_regime"] = reg_info["market_regime"]
        px["canonical_regime"] = reg_info["canonical_regime"]
        px["atr_rel_ratio"] = reg_info["atr_rel_ratio"]
        px["bb_width_pct"] = reg_info["bb_width_pct"]
        px["mtf_alignment"] = reg_info["mtf_alignment"]
        px["volatility_state"] = reg_info["volatility_state"]
        return px


class SatsExpectancyGate:
    """
    Compuerta económica y de seguridad P0:
    1. Bloqueos críticos de exposición, latencia y volatilidad extrema.
    2. Adaptación dinámica al régimen (EXTREME_VOLATILITY, VOLATILITY_EXPANSION, RANGE_SQUEEZE).
    3. Prohibición de abrir contratendencia con Q negativo o en expansiones direccionales adversas.
    4. Contabilidad completa de satoshis: Taker Fee (0.001 por lado), Funding, Spread y Slippage.
    """

    TAKER_FEE_RATE = 0.0010
    ROUND_TRIP_FEE_RATE = 0.0020
    MIN_NET_REWARD_TO_FEE_RATIO = 3.85

    @staticmethod
    def compute_regime_risk_adaptation(market_regime, current_dd_pct=0.0, recovery_mode=False):
        reg = str(market_regime or "RANGE_NORMAL").upper()
        if reg in ("EXTREME_VOLATILITY", "CHOP_HIGHVOL"):
            lev_mult, size_mult, min_q_boost, allow_ct = 0.45, 0.50, 0.040, False
        elif reg == "VOLATILITY_EXPANSION":
            lev_mult, size_mult, min_q_boost, allow_ct = 0.72, 0.78, 0.020, False
        elif reg == "RANGE_SQUEEZE":
            lev_mult, size_mult, min_q_boost, allow_ct = 0.60, 0.70, 0.015, False
        elif reg in ("TREND_BULL", "TREND_BEAR"):
            lev_mult, size_mult, min_q_boost, allow_ct = 0.92, 1.00, 0.005, False
        else:
            lev_mult, size_mult, min_q_boost, allow_ct = 0.80, 0.90, 0.010, True

        if recovery_mode or current_dd_pct >= 0.10:
            lev_mult *= 0.75
            size_mult *= 0.70
            min_q_boost += 0.010
            allow_ct = False

        return {
            "leverage_mult": round(max(0.30, lev_mult), 3),
            "size_mult": round(max(0.35, size_mult), 3),
            "min_q_boost": round(min_q_boost, 4),
            "allow_counter_trend": bool(allow_ct),
        }

    @staticmethod
    def filter_and_rank_action(
        raw_action, state, q_values, strategies, perception,
        recovery_mode=False, current_dd_pct=0.0, explorer_mode=False
    ):
        norm_s = str(state)
        override = perception.get("override_side") if isinstance(perception, dict) else None
        mkt_regime = perception.get("market_regime", "") if isinstance(perception, dict) else ""

        # =================================================================
        # PASO 1 (P0): PROTECCIONES CRÍTICAS DE CAPITAL, RÉGIMEN Y EXPOSICIÓN
        # =================================================================
        if not explorer_mode:
            if "RANGE_SQUEEZE" in norm_s and "HIGH_EXP" in norm_s:
                return 0, "BLOCKED_SQUEEZE_HIGH_EXP"
            if ("CHOP_HIGHVOL" in norm_s or mkt_regime == "EXTREME_VOLATILITY") and "HIGH_EXP" in norm_s:
                return 0, "BLOCKED_CHOP_HIGHVOL_HIGH_EXP"
            if mkt_regime == "EXTREME_VOLATILITY" and override is None:
                return 0, "BLOCKED_EXTREME_VOLATILITY_NO_CONFIRMATION"

        if current_dd_pct >= (0.28 if explorer_mode else 0.16):
            return 0, "BLOCKED_MAX_DRAWDOWN_REACHED"

        def is_counter_trend(act_idx):
            return (
                (act_idx >= 5 and ("|BULL|" in norm_s or "BULL_DIP" in norm_s or "FULL_BULL" in norm_s))
                or (1 <= act_idx <= 4 and ("|BEAR|" in norm_s or "BEAR_RALLY" in norm_s or "FULL_BEAR" in norm_s))
            )

        # Bloquear REVERSION (2,6) y DEF_SCALP (4,8) en contratendencia durante expansiones direccionales
        if not explorer_mode and raw_action in (2, 4, 6, 8):
            if mkt_regime in ("VOLATILITY_EXPANSION", "EXTREME_VOLATILITY") and is_counter_trend(int(raw_action)):
                return 0, f"BLOCKED_REVERSION_DEF_IN_{mkt_regime}"

        if not explorer_mode and raw_action in (1, 2, 3, 4, 5, 6, 7, 8) and len(q_values) > int(raw_action):
            rq = float(q_values[int(raw_action)])
            if "HIGH_EXP" in norm_s and rq < 0.04:
                return 0, f"BLOCKED_HIGH_EXP_LOW_Q(Q={rq:+.3f})"
            if is_counter_trend(int(raw_action)) and ("RANGE_SQUEEZE" in norm_s or mkt_regime == "VOLATILITY_EXPANSION") and rq < 0.01:
                return 0, f"BLOCKED_NEGATIVE_Q_COUNTERTREND(Q={rq:+.3f})"
            if is_counter_trend(int(raw_action)) and rq < 0.025 and override is None:
                return 0, f"BLOCKED_WEAK_COUNTERTREND_CONFIRMATION(Q={rq:+.3f})"
            if rq <= -0.15 and override is None:
                return 0, f"BLOCKED_SEVERE_NEGATIVE_Q(Q={rq:+.3f})"

        long_actions = [2, 3, 4] if (recovery_mode and not explorer_mode) else [1, 2, 3, 4]
        short_actions = [6, 7, 8] if (recovery_mode and not explorer_mode) else [5, 6, 7, 8]

        min_ovr_q = -0.35 if explorer_mode else (0.01 if "RANGE_SQUEEZE" in norm_s else -0.05)

        # =================================================================
        # PASO 2: REDIRECCIÓN POR GIRO EN V (CON FILTRO DE CALIDAD Q)
        # =================================================================
        if override == "BULL":
            valid_longs = [a for a in long_actions if a < len(q_values) and float(q_values[a]) >= min_ovr_q]
            if not valid_longs:
                return 0, "BLOCKED_OVR_BULL_ALL_LONG_Q_NEGATIVE"

            if raw_action in [5, 6, 7, 8] or raw_action == 0 or raw_action not in valid_longs:
                best_long = valid_longs[0]
                best_score = -999.0
                for a in valid_longs:
                    bonus = 0.10 if a == 1 else (0.08 if a == 3 else (0.07 if a == 4 else 0.0))
                    score = float(q_values[a]) + bonus
                    if score > best_score:
                        best_score = score
                        best_long = a
                return best_long, f"OVR_BULL_REDIRECT({raw_action}->{best_long})"
            if recovery_mode and not explorer_mode and raw_action == 1:
                return (3 if 3 in valid_longs else valid_longs[0]), "OVR_BULL_RECOVERY_CAP_RUNNER"
            return raw_action, "OVR_BULL_ALIGNED"

        if override == "BEAR":
            valid_shorts = [a for a in short_actions if a < len(q_values) and float(q_values[a]) >= min_ovr_q]
            if not valid_shorts:
                return 0, "BLOCKED_OVR_BEAR_ALL_SHORT_Q_NEGATIVE"

            if raw_action in [1, 2, 3, 4] or raw_action == 0 or raw_action not in valid_shorts:
                best_short = valid_shorts[0]
                best_score = -999.0
                for a in valid_shorts:
                    bonus = 0.10 if a == 5 else (0.08 if a == 7 else (0.07 if a == 8 else 0.0))
                    score = float(q_values[a]) + bonus
                    if score > best_score:
                        best_score = score
                        best_short = a
                return best_short, f"OVR_BEAR_REDIRECT({raw_action}->{best_short})"
            if recovery_mode and not explorer_mode and raw_action == 5:
                return (7 if 7 in valid_shorts else valid_shorts[0]), "OVR_BEAR_RECOVERY_CAP_RUNNER"
            return raw_action, "OVR_BEAR_ALIGNED"

        # =================================================================
        # PASO 3: FILTROS SIN OVERRIDE ACTIVO
        # =================================================================
        if recovery_mode and not explorer_mode and "RANGE_SQUEEZE" in norm_s and "BEAR_RALLY" in norm_s and raw_action in (2, 6):
            def_act = 4 if raw_action == 2 else 8
            q_def = float(q_values[def_act]) if def_act < len(q_values) else 0.0
            q_zero = float(q_values[0]) if len(q_values) > 0 else 0.0
            if q_def > max(0.01, q_zero):
                return def_act, f"RECOVERY_SQUEEZE_DEF_REDIRECT({raw_action}->{def_act})"
            return 0, "BLOCKED_REVERSION_IN_RECOVERY_SQUEEZE"

        rsi_5m = perception.get("rsi_5m", 50.0) if isinstance(perception, dict) else 50.0
        vel_ema = perception.get("vel_ema_atr", 0.0) if isinstance(perception, dict) else 0.0
        if not explorer_mode:
            if raw_action in [5, 6, 7, 8] and ("BEAR_RALLY" in norm_s or "CHOP" in norm_s):
                if vel_ema > 0.50 and rsi_5m > 53.5:
                    return 0, "BLOCKED_SHORT_AGAINST_MICRO_UPTREND"

            if raw_action in [1, 2, 3, 4] and ("BULL_DIP" in norm_s or "CHOP" in norm_s):
                if vel_ema < -0.50 and rsi_5m < 46.5:
                    return 0, "BLOCKED_LONG_AGAINST_MICRO_DOWNTREND"

        if raw_action > 0 and not explorer_mode:
            q_act = float(q_values[raw_action]) if raw_action < len(q_values) else -1.0
            q_zero = float(q_values[0]) if len(q_values) > 0 else 0.0
            min_req_q = max(0.015, q_zero) if (recovery_mode or "CHOP" in norm_s or "RANGE_SQUEEZE" in norm_s) else q_zero
            if q_act <= min_req_q:
                return 0, "BLOCKED_LOW_Q_VS_NO_TRADE"

        return raw_action, ("EXPLORER_Q" if explorer_mode else "STANDARD_Q")

    @classmethod
    def compute_trade_economic_breakdown(
        cls,
        gross_pnl_sats,
        margin_sats,
        leverage,
        exact_fee_sats=0,
        funding_fee_sats=0,
        entry_price=0.0,
        decision_price=0.0,
        spread_bps=0.0,
        side="buy",
    ):
        """
        Distingue rigurosamente entre beneficio bruto y resultado neto en sats:
        Desglosa Taker Fee (entrada + salida), Funding (carry fees), Spread y Slippage.
        """
        notional_sats = float(margin_sats) * float(leverage)
        est_entry_fee = max(1, int(round(notional_sats * cls.TAKER_FEE_RATE)))
        est_exit_fee = max(1, int(round(notional_sats * cls.TAKER_FEE_RATE)))
        taker_fees = int(exact_fee_sats) if int(exact_fee_sats) > 0 else (est_entry_fee + est_exit_fee)
        funding_sats = int(round(float(funding_fee_sats or 0)))

        slippage_sats = 0
        if entry_price > 0.0 and decision_price > 0.0:
            if str(side).lower() == "buy":
                adverse_diff_pct = max(0.0, (float(entry_price) - float(decision_price)) / float(decision_price))
            else:
                adverse_diff_pct = max(0.0, (float(decision_price) - float(entry_price)) / float(decision_price))
            slippage_sats = int(round(notional_sats * adverse_diff_pct))

        spread_sats = int(round(notional_sats * (max(0.0, float(spread_bps)) / 10000.0))) if spread_bps > 0 else 0
        total_friction_sats = taker_fees + funding_sats + slippage_sats + spread_sats
        gross_sats = int(round(float(gross_pnl_sats)))
        net_sats = gross_sats - total_friction_sats

        return {
            "gross_pnl_sats": gross_sats,
            "net_pnl_sats": net_sats,
            "entry_fee_sats": est_entry_fee,
            "exit_fee_sats": max(1, taker_fees - est_entry_fee),
            "taker_fee_sats": taker_fees,
            "funding_fee_sats": funding_sats,
            "slippage_sats": slippage_sats,
            "spread_sats": spread_sats,
            "total_friction_sats": total_friction_sats,
        }

    @classmethod
    def verify_net_sats_expectancy(
        cls,
        price,
        margin_sats,
        lev,
        sl_price,
        tp_price,
        explorer_mode=False,
        funding_rate_est=0.0,
        spread_bps=0.0,
        slippage_bps=0.0,
    ):
        price = max(float(price), 1.0)
        notional_sats = float(margin_sats) * float(lev)
        entry_fee_sats = max(1, int(round(notional_sats * cls.TAKER_FEE_RATE)))
        exit_fee_sats = max(1, int(round(notional_sats * cls.TAKER_FEE_RATE)))
        fee_sats = max(2, int(round(notional_sats * cls.ROUND_TRIP_FEE_RATE)))
        funding_sats = max(0, int(round(notional_sats * float(funding_rate_est))))
        spread_sats = max(0, int(round(notional_sats * (float(spread_bps) / 10000.0))))
        slippage_sats = max(0, int(round(notional_sats * (float(slippage_bps) / 10000.0))))
        total_cost_sats = fee_sats + funding_sats + spread_sats + slippage_sats

        tp_dist_pct = abs(float(tp_price) - price) / price
        sl_dist_pct = abs(price - float(sl_price)) / price

        expected_gross_tp_sats = int(round(notional_sats * tp_dist_pct))
        expected_net_tp_sats = expected_gross_tp_sats - total_cost_sats
        expected_loss_sl_sats = int(round(notional_sats * sl_dist_pct)) + total_cost_sats

        fee_ratio = expected_gross_tp_sats / max(float(total_cost_sats), 1.0)
        net_rr = expected_net_tp_sats / max(float(expected_loss_sl_sats), 1.0)

        min_ratio = 2.40 if explorer_mode else cls.MIN_NET_REWARD_TO_FEE_RATIO
        min_net_sats = max(16, int(total_cost_sats * 1.5)) if explorer_mode else max(38, int(round(total_cost_sats * 2.8)))
        min_rr = 0.72 if explorer_mode else 1.05

        is_viable = (
            expected_net_tp_sats >= min_net_sats
            and fee_ratio >= min_ratio
            and net_rr >= min_rr
        )
        return is_viable, {
            "gross_tp_sats": expected_gross_tp_sats,
            "fee_sats": fee_sats,
            "entry_fee_sats": entry_fee_sats,
            "exit_fee_sats": exit_fee_sats,
            "funding_sats": funding_sats,
            "spread_sats": spread_sats,
            "slippage_sats": slippage_sats,
            "total_cost_sats": total_cost_sats,
            "net_tp_sats": expected_net_tp_sats,
            "net_sl_sats": expected_loss_sl_sats,
            "fee_ratio": round(fee_ratio, 2),
            "net_rr": round(net_rr, 2),
        }

    @staticmethod
    def verify_projected_exposure(
        locked_margin_sats, new_margin_sats, equity_sats, available_balance_sats,
        max_exposure_ratio=0.38, est_entry_fee_sats=0, max_single_trade_risk_pct=0.18
    ):
        equity_sats = max(1, int(equity_sats))
        projected_margin = int(locked_margin_sats) + int(new_margin_sats)
        projected_exp_ratio = projected_margin / float(equity_sats)
        single_trade_ratio = int(new_margin_sats) / float(equity_sats)
        required_cash = int(new_margin_sats) + int(est_entry_fee_sats)

        if available_balance_sats < required_cash:
            return False, "INSUFFICIENT_AVAILABLE_BALANCE", round(projected_exp_ratio, 4)
        if projected_exp_ratio > float(max_exposure_ratio):
            return False, f"PROJECTED_EXPOSURE_EXCEEDED({projected_exp_ratio*100:.1f}%>{max_exposure_ratio*100:.1f}%)", round(projected_exp_ratio, 4)
        if single_trade_ratio > float(max_single_trade_risk_pct):
            return False, f"MIN_LOT_EXCEEDS_RISK_BUDGET({single_trade_ratio*100:.1f}%>{max_single_trade_risk_pct*100:.1f}%)", round(projected_exp_ratio, 4)
        return True, "EXPOSURE_OK", round(projected_exp_ratio, 4)


def sync_brain_floor_with_reality(brain, true_equity, floor_ratio=0.75):
    true_equity = max(1, int(true_equity))
    persisted_peak = int(getattr(brain, "peak_equity", 0) or 0)
    persisted_floor = int(getattr(brain, "locked_floor", 0) or 0)

    if persisted_floor > int(true_equity * 1.05) or persisted_peak > int(true_equity * 1.35) or persisted_peak <= 0:
        brain.peak_equity = true_equity
        brain.locked_floor = int(round(true_equity * floor_ratio))
        brain.dirty = True
        return True, brain.peak_equity, brain.locked_floor

    if true_equity > persisted_peak:
        brain.peak_equity = true_equity
        brain.locked_floor = max(persisted_floor, int(round(true_equity * floor_ratio)))
        brain.dirty = True

    return False, brain.peak_equity, brain.locked_floor
