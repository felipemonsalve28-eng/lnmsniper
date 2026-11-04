# -*- coding: utf-8 -*-
"""
sniper_perception_sats_core.py
Motor de Percepción en Tiempo Real (Proxies OHLCV 5m/15m + Spread API opcional),
Detector de Giro en V / Micro-Breakouts, Compuerta de Acumulación de Sats Netos
y Controlador Anti-Parálisis para LNM Sniper.
"""
import time
import math


class ShortTermPerceptionEngine:
    """
    Detecta aceleraciones de corto plazo (5m/15m), giros en V y micro-breakouts
    usando datos realmente disponibles en MultiTimeframeCache y ticker REST.
    """

    def __init__(self):
        self.price_history = []
        self.override_side = None
        self.override_until = 0.0
        self.override_strength = 0.0
        self.override_reason = "OFF"

    def _record_sample(self, now, price, atr):
        if not self.price_history or (now - self.price_history[-1][0]) >= 45.0:
            self.price_history.append((now, float(price), float(atr)))
            if len(self.price_history) > 40:
                self.price_history = self.price_history[-40:]

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
        if len(self.price_history) >= 3:
            recent_prices = [p[1] for p in self.price_history[-20:]]
            ref_idx = max(0, len(self.price_history) - 15)
            ref_price = self.price_history[ref_idx][1]
            vel_window_atr = (price - ref_price) / atr
            if len(recent_prices) > 1:
                micro_high = max(recent_prices[:-1])
                micro_low = min(recent_prices[:-1])

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
        comp_v = px["composite_vel_atr"]
        vel_ema = px["vel_ema_atr"]
        rsi_5m = px["rsi_5m"]
        rsi_15m = px["rsi_15m"]
        rsi_1h = float(ind_1h.get("rsi", 50.0))

        # 1. Evaluar activación de Giro en V Alcista (BULL OVERRIDE) con sensibilidad calibrada
        v_bull_trigger = (
            (vel_ema >= 1.35 and rsi_5m >= 55.5 and px["clv"] >= 0.20)
            or (px["breakout_bull"] and rsi_15m >= 52.0 and comp_v >= 1.05)
            or (rsi_5m >= 60.0 and rsi_15m >= 54.5 and rsi_1h >= 51.0 and vel_ema >= 1.00)
        )

        # 2. Evaluar activación de Desplome Bajista (BEAR OVERRIDE) con sensibilidad calibrada
        v_bear_trigger = (
            (vel_ema <= -1.35 and rsi_5m <= 44.5 and px["clv"] <= -0.20)
            or (px["breakout_bear"] and rsi_15m <= 48.0 and comp_v <= -1.05)
            or (rsi_5m <= 40.0 and rsi_15m <= 45.5 and rsi_1h <= 49.0 and vel_ema <= -1.00)
        )

        if v_bull_trigger and not v_bear_trigger:
            self.override_side = "BULL"
            self.override_until = now + 900.0
            self.override_strength = min(2.5, max(1.0, abs(vel_ema)))
            self.override_reason = f"V_BULL(v={vel_ema:+.2f}ATR|rsi5={rsi_5m:.0f}|brk={int(px['breakout_bull'])})"
        elif v_bear_trigger and not v_bull_trigger:
            self.override_side = "BEAR"
            self.override_until = now + 900.0
            self.override_strength = min(2.5, max(1.0, abs(vel_ema)))
            self.override_reason = f"V_BEAR(v={vel_ema:+.2f}ATR|rsi5={rsi_5m:.0f}|brk={int(px['breakout_bear'])})"
        else:
            if now >= self.override_until:
                self.override_side = None
                self.override_strength = 0.0
                self.override_reason = "OFF"
            elif self.override_side == "BULL" and (vel_ema < -0.40 or rsi_5m < 46.0):
                self.override_side = None
                self.override_strength = 0.0
                self.override_reason = "OFF_INV"
            elif self.override_side == "BEAR" and (vel_ema > 0.40 or rsi_5m > 54.0):
                self.override_side = None
                self.override_strength = 0.0
                self.override_reason = "OFF_INV"

        px["override_side"] = self.override_side
        px["override_strength"] = round(self.override_strength, 2)
        px["override_reason"] = self.override_reason
        return px


class SatsExpectancyGate:
    """
    Mapa oficial de estrategias en lnm_sniper_v6_advanced.py:
    1: LONG_TREND (RUNNER)   | 5: SHORT_TREND (RUNNER)
    2: LONG_REVERSION (LOCK) | 6: SHORT_REVERSION (LOCK)
    3: LONG_SNIPER (HYBRID)  | 7: SHORT_SNIPER (HYBRID)
    4: LONG_DEF_SCALP (LOCK) | 8: SHORT_DEF_SCALP (LOCK)
    """

    ROUND_TRIP_FEE_RATE = 0.0020
    MIN_NET_REWARD_TO_FEE_RATIO = 3.20

    @staticmethod
    def filter_and_rank_action(
        raw_action, state, q_values, strategies, perception,
        recovery_mode=False, current_dd_pct=0.0
    ):
        override = perception.get("override_side")
        norm_s = str(state)

        # En recovery_mode se excluyen los runners puros (1 y 5)
        long_actions = [2, 3, 4] if recovery_mode else [1, 2, 3, 4]
        short_actions = [6, 7, 8] if recovery_mode else [5, 6, 7, 8]

        # 1. Compuerta direccional dura anti-lag (Corrección giro en V alineada con índices reales)
        if override == "BULL":
            if raw_action in [5, 6, 7, 8] or raw_action == 0:
                best_long = (4 if recovery_mode else 1) if perception.get("breakout_bull") else (4 if recovery_mode else 3)
                best_score = -999.0
                for a in long_actions:
                    if a < len(q_values):
                        # Premiar LONG_TREND (1) y LONG_SNIPER (3) en normal; LONG_DEF_SCALP (4) y LONG_SNIPER (3) en recovery
                        bonus = 0.12 if a == 1 else (0.09 if a == 3 else (0.08 if a == 4 else 0.0))
                        score = float(q_values[a]) + bonus
                        if score > best_score:
                            best_score = score
                            best_long = a
                return best_long, f"OVR_BULL_REDIRECT({raw_action}->{best_long})"
            return raw_action, "OVR_BULL_ALIGNED"

        if override == "BEAR":
            if raw_action in [1, 2, 3, 4] or raw_action == 0:
                best_short = (8 if recovery_mode else 5) if perception.get("breakout_bear") else (8 if recovery_mode else 7)
                best_score = -999.0
                for a in short_actions:
                    if a < len(q_values):
                        # Premiar SHORT_TREND (5) y SHORT_SNIPER (7) en normal; SHORT_DEF_SCALP (8) y SHORT_SNIPER (7) en recovery
                        bonus = 0.12 if a == 5 else (0.09 if a == 7 else (0.08 if a == 8 else 0.0))
                        score = float(q_values[a]) + bonus
                        if score > best_score:
                            best_score = score
                            best_short = a
                return best_short, f"OVR_BEAR_REDIRECT({raw_action}->{best_short})"
            return raw_action, "OVR_BEAR_ALIGNED"

        # 2. Protección contra sobre-exposición en RANGE_SQUEEZE sin override
        if "RANGE_SQUEEZE" in norm_s and "HIGH_EXP" in norm_s:
            return 0, "BLOCKED_SQUEEZE_HIGH_EXP"

        # 3. Sin override activo: corregir sesgo BEAR_RALLY / BULL_DIP cuando RSI 5m/15m contradice
        rsi_5m = perception.get("rsi_5m", 50.0)
        vel_ema = perception.get("vel_ema_atr", 0.0)
        if raw_action in [5, 6, 7, 8] and ("BEAR_RALLY" in norm_s or "CHOP" in norm_s):
            if vel_ema > 0.55 and rsi_5m > 54.0:
                return 0, "BLOCKED_SHORT_AGAINST_MICRO_UPTREND"

        if raw_action in [1, 2, 3, 4] and ("BULL_DIP" in norm_s or "CHOP" in norm_s):
            if vel_ema < -0.55 and rsi_5m < 46.0:
                return 0, "BLOCKED_LONG_AGAINST_MICRO_DOWNTREND"

        # 4. En CHOP o RECOVERY sin override, exigir Q-value estrictamente positivo
        if raw_action > 0 and (recovery_mode or "CHOP" in norm_s):
            q_act = float(q_values[raw_action]) if raw_action < len(q_values) else -1.0
            q_zero = float(q_values[0]) if len(q_values) > 0 else 0.0
            if q_act <= max(0.005, q_zero):
                return 0, "BLOCKED_LOW_Q_IN_CHOP_RECOVERY"

        return raw_action, "STANDARD_Q"

    @classmethod
    def verify_net_sats_expectancy(cls, price, margin_sats, lev, sl_price, tp_price):
        price = max(float(price), 1.0)
        notional_sats = float(margin_sats) * float(lev)
        fee_sats = max(2, int(round(notional_sats * cls.ROUND_TRIP_FEE_RATE)))

        tp_dist_pct = abs(float(tp_price) - price) / price
        sl_dist_pct = abs(price - float(sl_price)) / price

        expected_gross_tp_sats = int(round(notional_sats * tp_dist_pct))
        expected_net_tp_sats = expected_gross_tp_sats - fee_sats
        expected_loss_sl_sats = int(round(notional_sats * sl_dist_pct)) + fee_sats

        fee_ratio = expected_gross_tp_sats / max(float(fee_sats), 1.0)
        net_rr = expected_net_tp_sats / max(float(expected_loss_sl_sats), 1.0)

        is_viable = (
            expected_net_tp_sats >= max(25, int(fee_sats * 2.2))
            and fee_ratio >= cls.MIN_NET_REWARD_TO_FEE_RATIO
            and net_rr >= 0.85
        )
        return is_viable, {
            "fee_sats": fee_sats,
            "net_tp_sats": expected_net_tp_sats,
            "net_sl_sats": expected_loss_sl_sats,
            "fee_ratio": round(fee_ratio, 2),
            "net_rr": round(net_rr, 2),
        }


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
        brain.locked_floor = int(round(true_equity * floor_ratio))
        brain.dirty = True

    return False, brain.peak_equity, brain.locked_floor
