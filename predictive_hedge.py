# -*- coding: utf-8 -*-
"""
predictive_hedge.py
Capa 2 (Edge & Risk Detection) del Hybrid Edge Hedging Engine.
Motor de Cobertura Predictiva que estima la probabilidad de continuación adversa,
la probabilidad de recuperación estructural y el riesgo de whipsaw ANTES de que
el precio alcance el Stop Loss, comparando matemáticamente en satoshis netos:
1. Mantener sin cobertura
2. Cubrir parcial o totalmente (h*)
3. Cerrar directamente la posición primaria
"""


class PredictiveHedgeEngine:
    """
    Calcula la expectativa matemática en sats netos de activar o escalar una
    cobertura frente a asumir la pérdida esperada sin cobertura o cerrar de inmediato.
    """

    ROUND_TRIP_TAKER_FEE = 0.0020

    @classmethod
    def forecast_adverse_risk(cls, edge_assessment, ind_5m, ind_15m, ind_1h, perception=None):
        edge_score = float(edge_assessment.get("edge_score", 0.0))
        edge_delta = float(edge_assessment.get("edge_delta", 0.0))
        pnl_atr = float(edge_assessment.get("pnl_atr", 0.0))
        adverse_vel = float(edge_assessment.get("adverse_velocity_atr", 0.0))
        dist_to_sl = float(edge_assessment.get("dist_to_sl_atr", 2.0))
        structure_broken = bool(edge_assessment.get("structure_broken", False))
        mtf_trend = float(edge_assessment.get("mtf_trend_score", 0.0))
        market_regime = str(edge_assessment.get("market_regime", "RANGE"))
        notional_sats = max(1000, int(edge_assessment.get("notional_sats", 20000)))
        close_now_loss = int(edge_assessment.get("close_now_loss_sats", 50))
        sl_full_loss = max(close_now_loss + 10, int(edge_assessment.get("sl_full_loss_sats", 250)))
        hold_cost = int(edge_assessment.get("hold_cost_sats_per_hour", 5))

        # 1. Probabilidad de continuación adversa hacia el SL
        base_adverse = 0.25
        if edge_score < 0.0:
            base_adverse += min(0.45, abs(edge_score) * 0.55)
        if edge_delta < -0.06:
            base_adverse += min(0.15, abs(edge_delta) * 0.45)
        if adverse_vel > 0.15:
            base_adverse += min(0.18, adverse_vel * 0.22)
        if structure_broken:
            base_adverse += 0.14
        if dist_to_sl < 0.85:
            base_adverse += 0.10
        if market_regime in ("VOLATILITY_EXPANSION", "TREND_BULL", "TREND_BEAR") and mtf_trend < -0.15:
            base_adverse += 0.12

        adverse_continuation_prob = round(max(0.05, min(0.95, base_adverse)), 4)

        # 2. Riesgo de látigo (whipsaw) según régimen y compresión
        if market_regime in ("RANGE_SQUEEZE", "EXTREME_VOLATILITY", "CHOP_HIGHVOL"):
            whipsaw_base = 0.46
        elif market_regime == "RANGE":
            whipsaw_base = 0.34
        else:
            whipsaw_base = 0.20

        if abs(pnl_atr) < 0.25:
            whipsaw_base += 0.12
        whipsaw_risk_prob = round(max(0.10, min(0.85, whipsaw_base - (0.15 * max(0.0, adverse_continuation_prob - 0.55)))), 4)

        # 3. Probabilidad de que la posición primaria aún tenga soporte macro para recuperar más adelante
        ema_score_1h = float(ind_1h.get("ema_score", 0.0)) if isinstance(ind_1h, dict) else 0.0
        is_long = str(edge_assessment.get("side", "buy")).lower() == "buy"
        macro_support = (ema_score_1h >= 0.20) if is_long else (ema_score_1h <= -0.20)
        reversion_prob = round(
            max(0.08, min(0.85, (1.0 - adverse_continuation_prob * 0.65) + (0.18 if macro_support else -0.08))),
            4,
        )

        # 4. Ratio óptimo de cobertura h* in [0.35, 1.00]
        if adverse_continuation_prob >= 0.72 or (structure_broken and pnl_atr <= -0.55):
            optimal_h = min(1.00, max(0.75, round(adverse_continuation_prob * 1.05, 2)))
            recommended_mode = "INCREASE_HEDGE"
        elif adverse_continuation_prob >= 0.52 and edge_score <= -0.24:
            optimal_h = min(0.65, max(0.38, round(adverse_continuation_prob * 0.85, 2)))
            recommended_mode = "PARTIAL_HEDGE"
        else:
            optimal_h = 0.0
            recommended_mode = "NO_HEDGE"

        # 5. Comparación económica estricta en satoshis netos
        remaining_loss_to_sl = max(20, sl_full_loss - max(0, -int(edge_assessment.get("pl_sats", 0))))
        expected_unhedged_loss_sats = int(round(adverse_continuation_prob * remaining_loss_to_sl + hold_cost))

        hedge_notional_sats = float(notional_sats) * max(0.35, optimal_h if optimal_h > 0 else 0.50)
        hedge_fees_sats = max(2, int(round(hedge_notional_sats * cls.ROUND_TRIP_TAKER_FEE)))
        hedge_slippage_sats = max(1, int(round(hedge_notional_sats * (0.00025 if market_regime == "EXTREME_VOLATILITY" else 0.00010))))
        whipsaw_drag_sats = int(round(whipsaw_risk_prob * hedge_notional_sats * 0.0022))

        saved_move_sats = int(round(adverse_continuation_prob * (optimal_h if optimal_h > 0 else 0.50) * remaining_loss_to_sl))
        expected_hedge_net_cost_sats = (hedge_fees_sats + hedge_slippage_sats + whipsaw_drag_sats) - saved_move_sats
        net_expected_saving_sats = expected_unhedged_loss_sats - max(0, hedge_fees_sats + hedge_slippage_sats + whipsaw_drag_sats - saved_move_sats // 2)

        # Criterio: ¿Es mejor cerrar la original directamente en lugar de abrir cobertura?
        direct_close_preferred = (
            adverse_continuation_prob >= 0.80
            and reversion_prob <= 0.22
            and not macro_support
            and close_now_loss < (hedge_fees_sats * 2 + int(0.35 * remaining_loss_to_sl))
        )

        # Solo recomendar cobertura si el ahorro esperado supera el coste de cubrir + margen de seguridad
        min_fee_efficiency = saved_move_sats >= int(round((hedge_fees_sats + hedge_slippage_sats) * 1.45))
        recommend_hedge = (
            optimal_h >= 0.35
            and min_fee_efficiency
            and expected_hedge_net_cost_sats < expected_unhedged_loss_sats
            and whipsaw_risk_prob <= 0.62
            and not direct_close_preferred
        )

        if direct_close_preferred and edge_score <= -0.52 and pnl_atr <= -0.60:
            recommended_mode = "CLOSE_ORIGINAL_POSITION"
        elif not recommend_hedge:
            recommended_mode = "NO_HEDGE"

        return {
            "adverse_continuation_prob": adverse_continuation_prob,
            "whipsaw_risk_prob": whipsaw_risk_prob,
            "reversion_prob": reversion_prob,
            "macro_support": bool(macro_support),
            "optimal_hedge_ratio": round(optimal_h if recommend_hedge else 0.0, 3),
            "expected_unhedged_loss_sats": expected_unhedged_loss_sats,
            "expected_hedge_fee_sats": hedge_fees_sats + hedge_slippage_sats,
            "expected_saved_sats": saved_move_sats,
            "net_expected_saving_sats": net_expected_saving_sats,
            "recommend_hedge": bool(recommend_hedge),
            "direct_close_preferred": bool(direct_close_preferred),
            "recommended_action_name": recommended_mode,
        }
