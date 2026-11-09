# -*- coding: utf-8 -*-
"""
edge_monitor.py
Capa 2 (Edge & Risk Detection) del Hybrid Edge Hedging Engine.
Evalúa continuamente la salud direccional, microestructural y económica de cada
posición abierta en LN Markets API v3 para detectar degradación temprana del edge
antes de que se dispare el Stop Loss.
"""
import time


class EdgeMonitor:
    """
    Monitor continuo de salud de posición y degradación de ventaja (Edge).
    Calcula métricas normalizadas por ATR y costes reales en satoshis.
    """

    TAKER_FEE = 0.0010

    @classmethod
    def evaluate_position_edge(
        cls,
        trade,
        pair_ctx,
        ind_5m,
        ind_15m,
        ind_1h,
        perception=None,
        q_val_action=0.0,
        now=None,
    ):
        now = float(now if now is not None else time.time())
        side = str(trade.get("side") or pair_ctx.get("orig_side") or "buy").lower()
        is_long = (side == "buy")

        price = max(float(ind_5m.get("price", 1.0)), 1.0)
        atr = max(float(ind_5m.get("atr", price * 0.0015)), 1.0)
        entry = float(trade.get("price") or trade.get("entryPrice") or pair_ctx.get("orig_entry_price") or price)
        sl = float(trade.get("stoploss") or 0.0)
        liq = float(trade.get("liquidation") or 0.0)
        margin_sats = int(trade.get("margin") or pair_ctx.get("orig_margin_sats") or 1000)
        leverage = float(trade.get("leverage") or pair_ctx.get("orig_leverage") or 20.0)
        notional_sats = float(margin_sats) * leverage
        pl_sats = int(trade.get("pl") or 0)
        carry_fees = int(round(float(trade.get("sum_carry_fees") or trade.get("funding_fees") or 0)))

        # 1. Excursión flotante y distancias a SL / Liquidación en unidades de ATR
        signed_move = (price - entry) if is_long else (entry - price)
        pnl_atr = signed_move / atr

        if sl > 0.0:
            dist_to_sl = ((price - sl) if is_long else (sl - price)) / atr
        else:
            dist_to_sl = 2.0

        if liq > 0.0:
            dist_to_liq = ((price - liq) if is_long else (liq - price)) / atr
        else:
            dist_to_liq = 4.0

        # 2. Alineación microestructural y multitemporal
        ema9_5m = float(ind_5m.get("ema9", price))
        ema21_5m = float(ind_5m.get("ema21", price))
        ema50_5m = float(ind_5m.get("ema50", price))
        rsi_5m = float(ind_5m.get("rsi", 50.0))
        rsi_15m = float(ind_15m.get("rsi", 50.0)) if isinstance(ind_15m, dict) else 50.0
        ema_score_5m = float(ind_5m.get("ema_score", 0.0))
        ema_score_15m = float(ind_15m.get("ema_score", 0.0)) if isinstance(ind_15m, dict) else 0.0
        ema_score_1h = float(ind_1h.get("ema_score", 0.0)) if isinstance(ind_1h, dict) else 0.0

        dir_sign = 1.0 if is_long else -1.0
        mtf_trend_score = dir_sign * (0.40 * ema_score_5m + 0.35 * ema_score_15m + 0.25 * ema_score_1h)
        rsi_aligned = dir_sign * ((0.60 * (rsi_5m - 50.0) + 0.40 * (rsi_15m - 50.0)) / 25.0)

        vel_ema_atr = float(perception.get("vel_ema_atr", 0.0)) if isinstance(perception, dict) else 0.0
        clv = float(perception.get("clv", 0.0)) if isinstance(perception, dict) else 0.0
        override_side = perception.get("override_side") if isinstance(perception, dict) else None
        market_regime = (
            perception.get("market_regime") if isinstance(perception, dict) and perception.get("market_regime")
            else ind_5m.get("market_regime", ind_5m.get("regime", "RANGE"))
        )

        ovr_component = 0.0
        if override_side == "BULL":
            ovr_component = 0.30 if is_long else -0.42
        elif override_side == "BEAR":
            ovr_component = -0.42 if is_long else 0.30

        micro_vel_aligned = dir_sign * vel_ema_atr
        clv_aligned = dir_sign * clv

        # 3. Ruptura estructural en contra de la posición
        if is_long:
            structure_broken = (price < ema21_5m and ema9_5m < ema21_5m and rsi_5m < 44.5) or (price < ema50_5m and vel_ema_atr <= -0.65)
        else:
            structure_broken = (price > ema21_5m and ema9_5m > ema21_5m and rsi_5m > 55.5) or (price > ema50_5m and vel_ema_atr >= 0.65)

        struct_penalty = -0.22 if structure_broken else 0.0

        # 4. Score compuesto de Edge [-1.0, +1.0]
        raw_edge = (
            (0.30 * float(q_val_action))
            + (0.22 * max(-1.5, min(1.5, pnl_atr)))
            + (0.20 * mtf_trend_score)
            + (0.14 * max(-1.0, min(1.0, rsi_aligned)))
            + (0.14 * max(-1.2, min(1.2, micro_vel_aligned)))
            + (0.08 * clv_aligned)
            + ovr_component
            + struct_penalty
        )
        edge_score = round(max(-1.0, min(1.0, raw_edge)), 4)

        # 5. Velocidad de deterioro del Edge respecto a muestras recientes
        hist = pair_ctx.setdefault("edge_history", [])
        hist.append((now, edge_score, round(pnl_atr, 4)))
        if len(hist) > 12:
            del hist[:-12]

        if len(hist) >= 2:
            prev_ts, prev_edge, prev_pnl_atr = hist[max(0, len(hist) - 3)]
            dt_min = max(0.10, (now - float(prev_ts)) / 60.0)
            edge_delta = round(edge_score - float(prev_edge), 4)
            adverse_velocity_atr = round(max(0.0, float(prev_pnl_atr) - pnl_atr) / dt_min, 4)
        else:
            edge_delta = 0.0
            adverse_velocity_atr = round(max(0.0, -micro_vel_aligned * 0.35), 4)

        pair_ctx["last_edge_score"] = edge_score

        # 6. Costes económicos en satoshis (mantener, cerrar ahora, o tocar SL completo)
        exit_taker_fee_sats = max(1, int(round(notional_sats * cls.TAKER_FEE)))
        est_slippage_sats = max(1, int(round(notional_sats * (0.00025 if market_regime in ("EXTREME_VOLATILITY", "VOLATILITY_EXPANSION") else 0.00010))))
        close_cost_sats = exit_taker_fee_sats + est_slippage_sats + max(0, carry_fees)
        close_now_loss_sats = max(0, -pl_sats) + close_cost_sats

        if sl > 0.0:
            sl_dist_pct = abs(entry - sl) / max(entry, 1.0)
        else:
            sl_dist_pct = (1.6 * atr) / max(entry, 1.0)
        sl_full_loss_sats = int(round(notional_sats * sl_dist_pct)) + close_cost_sats + exit_taker_fee_sats

        age_hours = max(0.1, (now - float(pair_ctx.get("state_entered_at", now))) / 3600.0)
        hold_cost_sats_per_hour = max(1, int(round((notional_sats * 0.00005) + (max(0, carry_fees) / age_hours))))

        return {
            "trade_id": str(trade.get("id", "")),
            "side": side,
            "price": price,
            "entry_price": entry,
            "atr": atr,
            "market_regime": market_regime,
            "edge_score": edge_score,
            "edge_delta": edge_delta,
            "pnl_atr": round(pnl_atr, 4),
            "pl_sats": pl_sats,
            "adverse_velocity_atr": adverse_velocity_atr,
            "dist_to_sl_atr": round(dist_to_sl, 3),
            "dist_to_liq_atr": round(dist_to_liq, 3),
            "structure_broken": bool(structure_broken),
            "mtf_trend_score": round(mtf_trend_score, 3),
            "micro_vel_aligned": round(micro_vel_aligned, 3),
            "hold_cost_sats_per_hour": hold_cost_sats_per_hour,
            "close_cost_sats": close_cost_sats,
            "close_now_loss_sats": close_now_loss_sats,
            "sl_full_loss_sats": sl_full_loss_sats,
            "notional_sats": int(round(notional_sats)),
            "margin_sats": margin_sats,
            "leverage": leverage,
        }
