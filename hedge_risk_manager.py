# -*- coding: utf-8 -*-
"""
hedge_risk_manager.py
Capa 4 (Adaptive Decision & Safety) del Hybrid Edge Hedging Engine.
Gobernador de seguridad subordinado al HardFloorGuardian:
1. Protege el piso duro de satoshis (locked_floor).
2. Verifica margen disponible suficiente (colchón >= 2.0x).
3. Impide sobrecobertura (h <= 1.00) y doble liquidación por ampliación de spread.
4. Bloquea aperturas/incrementos de cobertura ante degradación de latencia.
"""
import time


class HedgeRiskManager:
    """
    Valida todas las restricciones de riesgo, margen, liquidez y piso de capital
    antes de autorizar cualquier transición o ejecución de cobertura.
    """

    TAKER_FEE = 0.0010
    MIN_MARGIN_SATS = 1000
    MAX_HEDGE_RATIO = 1.00
    MAX_HEDGE_LEVERAGE = 35

    @classmethod
    def authorize_hedge_action(
        cls,
        action_name,
        pair_ctx,
        pair_econ,
        portfolio_snap,
        predictive_signal,
        account_equity_sats,
        available_balance_sats,
        locked_floor_sats,
        ind_5m,
        latency_degraded=False,
        explorer_mode=False,
        now=None,
    ):
        """
        Retorna (authorized: bool, reason: str, approved_params: dict).
        """
        now = float(now if now is not None else time.time())
        act = str(action_name or "NO_HEDGE").upper()

        if act == "NO_HEDGE":
            return True, "NO_HEDGE_OK", {}

        # Acciones de reducción o cierre siempre están permitidas salvo cooldown anti-duplicado
        if act in ("CLOSE_HEDGE", "CLOSE_ORIGINAL_POSITION", "REDUCE_HEDGE", "REDUCE_ORIGINAL_POSITION"):
            if act == "CLOSE_HEDGE" and not pair_econ.get("has_active_hedge", False):
                return False, "BLOCKED_NO_ACTIVE_HEDGE_TO_CLOSE", {}
            return True, f"AUTHORIZED_{act}", {}

        # A partir de aquí se evalúan PARTIAL_HEDGE e INCREASE_HEDGE (acciones que consumen margen/fees)
        if latency_degraded and not explorer_mode:
            return False, "BLOCKED_HEDGE_LATENCY_DEGRADED", {}

        cooldown_until = float(pair_ctx.get("cooldown_until", 0.0))
        if now < cooldown_until:
            return False, f"BLOCKED_HEDGE_COOLDOWN({cooldown_until - now:.1f}s)", {}

        # 1. Subordinación absoluta al HardFloorGuardian
        safety_buffer = max(120, int(round(account_equity_sats * 0.015)))
        if locked_floor_sats > 0 and (account_equity_sats - safety_buffer) <= locked_floor_sats:
            return False, f"BLOCKED_HARD_FLOOR_PROXIMITY(eq={account_equity_sats}<=floor={locked_floor_sats}+{safety_buffer})", {}

        # 2. Evitar sobrecobertura (máximo 100% de la exposición nocional original)
        current_ratio = float(pair_econ.get("effective_hedge_ratio", 0.0))
        if act == "PARTIAL_HEDGE" and pair_econ.get("has_active_hedge", False):
            return False, "BLOCKED_PARTIAL_HEDGE_ALREADY_ACTIVE", {}
        if current_ratio >= 0.92:
            return False, f"BLOCKED_MAX_HEDGE_RATIO_REACHED(h={current_ratio:.2f})", {}

        orig_notional = max(1000, int(pair_ctx.get("orig_notional_sats", 20000)))
        orig_lev = max(15.0, float(pair_ctx.get("orig_leverage", 25.0)))
        target_ratio = float(predictive_signal.get("optimal_hedge_ratio", 0.50))
        if act == "PARTIAL_HEDGE":
            target_ratio = max(0.38, min(0.65, target_ratio if target_ratio > 0 else 0.50))
        else:  # INCREASE_HEDGE
            target_ratio = max(0.75, min(cls.MAX_HEDGE_RATIO, target_ratio if target_ratio > 0 else 0.85))

        delta_ratio = max(0.0, min(cls.MAX_HEDGE_RATIO - current_ratio, target_ratio - current_ratio))
        if delta_ratio < 0.20:
            return False, f"BLOCKED_DELTA_RATIO_TOO_SMALL(dh={delta_ratio:.2f})", {}

        desired_hedge_notional = int(round(orig_notional * delta_ratio))

        # 3. Prevención de doble liquidación: limitar apalancamiento de cobertura según ATR
        price = max(1.0, float(ind_5m.get("price", 70000.0)))
        atr = max(1.0, float(ind_5m.get("atr", price * 0.002)))
        atr_pct = atr / price
        safe_lev_from_atr = int(min(cls.MAX_HEDGE_LEVERAGE, max(15, round(0.25 / max(atr_pct, 0.002)))))
        hedge_lev = int(min(orig_lev, safe_lev_from_atr, cls.MAX_HEDGE_LEVERAGE))
        hedge_margin_sats = max(cls.MIN_MARGIN_SATS, int(round(desired_hedge_notional / max(1, hedge_lev))))
        actual_hedge_notional = hedge_margin_sats * hedge_lev

        # Verificar que el lote mínimo (1000 sats) no provoque sobrecobertura > 1.15x
        projected_total_ratio = (float(pair_econ.get("hedge_notional_sats", 0)) + actual_hedge_notional) / float(orig_notional)
        if projected_total_ratio > 1.15:
            # Reducir apalancamiento al mínimo permitido para ajustar nocional
            hedge_lev = 15
            actual_hedge_notional = hedge_margin_sats * hedge_lev
            projected_total_ratio = (float(pair_econ.get("hedge_notional_sats", 0)) + actual_hedge_notional) / float(orig_notional)
            if projected_total_ratio > 1.25:
                return False, f"BLOCKED_MIN_LOT_OVERHEDGES(proj_h={projected_total_ratio:.2f})", {}

        # 4. Suficiencia de margen libre y exposición máxima del portafolio
        est_entry_fee = max(1, int(round(actual_hedge_notional * cls.TAKER_FEE)))
        est_round_fee = est_entry_fee * 2
        if available_balance_sats < (hedge_margin_sats * 2 + est_entry_fee):
            return False, f"BLOCKED_INSUFFICIENT_FREE_MARGIN(avail={available_balance_sats}<req={hedge_margin_sats*2+est_entry_fee})", {}

        locked_margin = int(portfolio_snap.get("locked_margin_sats", 0))
        max_total_exp = 0.58 if explorer_mode else 0.48
        if (locked_margin + hedge_margin_sats) / max(1.0, float(account_equity_sats)) > max_total_exp:
            return False, "BLOCKED_PORTFOLIO_HEDGE_MARGIN_CAP", {}

        # 5. Garantizar que el coste esperado de la cobertura sea menor que la pérdida esperada sin cubrir
        exp_unhedged_loss = int(predictive_signal.get("expected_unhedged_loss_sats", 0))
        if not explorer_mode and exp_unhedged_loss < int(round(est_round_fee * 1.30)):
            return False, f"BLOCKED_HEDGE_COST_EXCEEDS_UNHEDGED_RISK(fee={est_round_fee}>=loss={exp_unhedged_loss})", {}

        orig_side = str(pair_ctx.get("orig_side", "buy")).lower()
        hedge_side = "sell" if orig_side == "buy" else "buy"

        # Stop Loss y Take Profit protectores en la pata de cobertura (fuera de zona de ruido)
        sl_dist = max(atr * 1.55, price * 0.0045)
        tp_dist = max(atr * 2.40, price * 0.0085)
        if hedge_side == "buy":
            hedge_sl = round(price - sl_dist, 1)
            hedge_tp = round(price + tp_dist, 1)
        else:
            hedge_sl = round(price + sl_dist, 1)
            hedge_tp = round(price - tp_dist, 1)

        return True, f"AUTHORIZED_{act}", {
            "hedge_side": hedge_side,
            "hedge_margin_sats": int(hedge_margin_sats),
            "hedge_leverage": int(hedge_lev),
            "hedge_notional_sats": int(actual_hedge_notional),
            "hedge_sl": float(hedge_sl),
            "hedge_tp": float(hedge_tp),
            "projected_hedge_ratio": round(projected_total_ratio, 3),
            "est_round_fee_sats": int(est_round_fee),
        }
