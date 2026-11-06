# -*- coding: utf-8 -*-
"""
sniper_perception_sats_core.py
Motor de Percepción en Tiempo Real (Proxies OHLCV 5m/15m + Confirmación Continua),
Detector de Giro en V con Decaimiento e Histéresis, Compuerta de Expectativa de Sats Netos
con Filtro Anti-Churning de Comisiones (P0) y Soporte Dual (Mainnet Estricto / Testnet Explorador).
"""
import time
import math


class ShortTermPerceptionEngine:
    """
    Detecta aceleraciones de corto plazo (5m/15m), giros en V y micro-breakouts
    con validación continua de momentum, decaimiento gradual e histéresis.
    """

    def __init__(self):
        self.price_history = []
        self.override_side = None
        self.override_since = 0.0
        self.override_until = 0.0
        self.override_strength = 0.0
        self.override_reason = "OFF"
        self.last_flip_time = 0.0

    def _record_sample(self, now, price, atr):
        if not self.price_history or (now - self.price_history[-1][0]) >= 15.0:
            self.price_history.append((float(now), float(price), float(atr)))
            if len(self.price_history) > 60:
                self.price_history = self.price_history[-60:]

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
            recent_prices = [p[1] for p in self.price_history[-25:]]
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
        return px


class SatsExpectancyGate:
    """
    Compuerta económica y de seguridad P0:
    1. Bloqueos críticos de exposición y volatilidad (NINGÚN override puede saltárselos en Mainnet).
    2. Prohibición de abrir contratendencia con Q negativo empujado por Override en RANGE_SQUEEZE.
    3. Expectativa neta de satoshis reforzada contra erosión por fees (Ratio >= 3.85x en Mainnet).
    """

    ROUND_TRIP_FEE_RATE = 0.0020
    MIN_NET_REWARD_TO_FEE_RATIO = 3.85

    @staticmethod
    def filter_and_rank_action(
        raw_action, state, q_values, strategies, perception,
        recovery_mode=False, current_dd_pct=0.0, explorer_mode=False
    ):
        norm_s = str(state)
        override = perception.get("override_side") if isinstance(perception, dict) else None

        # =================================================================
        # PASO 1 (P0): PROTECCIONES CRÍTICAS DE CAPITAL Y EXPOSICIÓN
        # =================================================================
        if not explorer_mode:
            if "RANGE_SQUEEZE" in norm_s and "HIGH_EXP" in norm_s:
                return 0, "BLOCKED_SQUEEZE_HIGH_EXP"
            if "CHOP_HIGHVOL" in norm_s and "HIGH_EXP" in norm_s:
                return 0, "BLOCKED_CHOP_HIGHVOL_HIGH_EXP"

        if current_dd_pct >= (0.28 if explorer_mode else 0.16):
            return 0, "BLOCKED_MAX_DRAWDOWN_REACHED"

        # Helper para verificar si una acción es contratendencia respecto a la estructura macro/EMA
        def is_counter_trend(act_idx):
            return (
                (act_idx >= 5 and ("|BULL|" in norm_s or "BULL_DIP" in norm_s or "FULL_BULL" in norm_s))
                or (1 <= act_idx <= 4 and ("|BEAR|" in norm_s or "BEAR_RALLY" in norm_s or "FULL_BEAR" in norm_s))
            )

        # En Mainnet: bloquear entradas directas con Q negativo en contratendencia o HIGH_EXP
        if not explorer_mode and raw_action in (1, 2, 3, 4, 5, 6, 7, 8) and len(q_values) > int(raw_action):
            rq = float(q_values[int(raw_action)])
            if "HIGH_EXP" in norm_s and rq < 0.04:
                return 0, f"BLOCKED_HIGH_EXP_LOW_Q(Q={rq:+.3f})"
            if is_counter_trend(int(raw_action)) and "RANGE_SQUEEZE" in norm_s and rq < 0.01:
                return 0, f"BLOCKED_NEGATIVE_Q_COUNTERTREND(Q={rq:+.3f})"
            if rq <= -0.15 and override is None:
                return 0, f"BLOCKED_SEVERE_NEGATIVE_Q(Q={rq:+.3f})"

        long_actions = [2, 3, 4] if (recovery_mode and not explorer_mode) else [1, 2, 3, 4]
        short_actions = [6, 7, 8] if (recovery_mode and not explorer_mode) else [5, 6, 7, 8]

        # Umbral mínimo de Q para permitir que un Override seleccione o mantenga una acción en Mainnet
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
    def verify_net_sats_expectancy(cls, price, margin_sats, lev, sl_price, tp_price, explorer_mode=False):
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

        min_ratio = 2.40 if explorer_mode else cls.MIN_NET_REWARD_TO_FEE_RATIO
        min_net_sats = max(16, int(fee_sats * 1.5)) if explorer_mode else max(38, int(round(fee_sats * 2.8)))
        min_rr = 0.72 if explorer_mode else 1.05

        is_viable = (
            expected_net_tp_sats >= min_net_sats
            and fee_ratio >= min_ratio
            and net_rr >= min_rr
        )
        return is_viable, {
            "fee_sats": fee_sats,
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
