# -*- coding: utf-8 -*-
"""
dynamic_hedge.py
Capa 3 (Hedge Execution & Portfolio Control) del Hybrid Edge Hedging Engine.
Ejecuta las 7 acciones independientes del controlador de cobertura utilizando
los endpoints nativos de LN Markets API v3 (/v3/futures/isolated/trade/*):
- open_trade (POST /futures/isolated/trade)
- close_trade (POST /futures/isolated/trade/close)
- cash_in (POST /futures/isolated/trade/cash-in)
- add_margin (POST /futures/isolated/trade/add-margin)
- update_stoploss / update_takeprofit (PUT /futures/isolated/trade/stoploss|takeprofit)
"""
import time


class DynamicHedgeExecutor:
    """
    Traduce decisiones autorizadas de cobertura en operaciones reales o simuladas
    (safe_mode) sobre LN Markets API v3, manteniendo la trazabilidad en satoshis.
    """

    TAKER_FEE = 0.0010

    @classmethod
    def execute_hedge_action(
        cls,
        client,
        action_name,
        approved_params,
        orig_trade,
        hedge_trade,
        pair_ctx,
        pair_econ,
        ind_5m,
        hedge_store,
        safe_mode=False,
        now=None,
    ):
        now = float(now if now is not None else time.time())
        act = str(action_name or "NO_HEDGE").upper()
        orig_tid = str(orig_trade.get("id", pair_ctx.get("orig_trade_id", "")))
        hedge_tid = str(hedge_trade["id"]) if isinstance(hedge_trade, dict) and "id" in hedge_trade else pair_ctx.get("hedge_trade_id")
        market_regime = str(ind_5m.get("market_regime", ind_5m.get("regime", "RANGE")))

        if act == "NO_HEDGE":
            return {"executed": False, "action": "NO_HEDGE", "reason": "HOLD_STATE"}

        # Modo seguro (--safe-mode): registrar simulación sin enviar órdenes HTTP reales
        if safe_mode:
            return {
                "executed": False,
                "simulated": True,
                "action": act,
                "reason": f"SAFE_MODE_SIMULATED({act})",
                "params": approved_params,
            }

        # ------------------------------------------------------------------
        # 1. PARTIAL_HEDGE / INCREASE_HEDGE
        # ------------------------------------------------------------------
        if act in ("PARTIAL_HEDGE", "INCREASE_HEDGE"):
            # Si aún no existe pata de cobertura abierta, abrir posición aislada opuesta en API v3
            if not hedge_tid:
                h_side = approved_params["hedge_side"]
                h_margin = int(approved_params["hedge_margin_sats"])
                h_lev = int(approved_params["hedge_leverage"])
                h_sl = float(approved_params["hedge_sl"])
                h_tp = float(approved_params["hedge_tp"])
                est_fee = max(1, int(round(h_margin * h_lev * cls.TAKER_FEE)))

                resp = client.open_trade(h_side, h_margin, h_lev, h_sl, h_tp)
                if isinstance(resp, dict) and "id" in resp:
                    new_hid = str(resp["id"])
                    fill_price = float(resp.get("price") or resp.get("entryPrice") or ind_5m.get("price", 0.0))
                    pair_ctx["hedge_trade_id"] = new_hid
                    pair_ctx["hedge_side"] = h_side
                    pair_ctx["hedge_margin_sats"] = h_margin
                    pair_ctx["hedge_leverage"] = float(h_lev)
                    pair_ctx["hedge_notional_sats"] = h_margin * h_lev
                    pair_ctx["hedge_entry_price"] = fill_price
                    pair_ctx["hedge_ratio"] = float(approved_params.get("projected_hedge_ratio", 0.5))
                    pair_ctx["entry_unhedged_pl_sats"] = int(orig_trade.get("pl") or 0)
                    pair_ctx["cumulative_hedge_fees_sats"] = int(pair_ctx.get("cumulative_hedge_fees_sats", 0)) + est_fee
                    pair_ctx["cooldown_until"] = now + 20.0

                    hedge_store.hedge_metrics["total_hedges_opened"] = int(hedge_store.hedge_metrics.get("total_hedges_opened", 0)) + 1
                    hedge_store.hedge_metrics["total_hedge_fees_sats"] = int(hedge_store.hedge_metrics.get("total_hedge_fees_sats", 0)) + est_fee
                    hedge_store.record_regime_metric(market_regime, "hedges", 1)
                    hedge_store.record_regime_metric(market_regime, "fees_sats", est_fee)
                    hedge_store.dirty = True
                    return {
                        "executed": True,
                        "action": act,
                        "hedge_trade_id": new_hid,
                        "hedge_margin_sats": h_margin,
                        "hedge_leverage": h_lev,
                        "hedge_ratio": pair_ctx["hedge_ratio"],
                        "reason": f"OPENED_{act}(id={new_hid[:6]}|h={pair_ctx['hedge_ratio']:.2f})",
                    }
                return {"executed": False, "action": act, "reason": "OPEN_HEDGE_EMPTY_RESPONSE"}

            # Si ya existe una pata de cobertura activa y se solicita INCREASE_HEDGE:
            # Proteger su ganancia mediante SL dinámico o reforzar margen (add_margin) si la primaria está cerca de liquidación
            if isinstance(hedge_trade, dict):
                price = float(ind_5m.get("price", 0.0))
                atr = max(1.0, float(ind_5m.get("atr", price * 0.002)))
                h_side = str(hedge_trade.get("side", "sell")).lower()
                h_entry = float(hedge_trade.get("price") or hedge_trade.get("entryPrice") or price)
                h_pl = int(hedge_trade.get("pl") or 0)
                fee_off = h_entry * (2.4 * cls.TAKER_FEE)

                if h_pl > 25:
                    target_sl = int(round((h_entry + fee_off) if h_side == "buy" else (h_entry - fee_off)))
                    try:
                        client.update_stoploss(hedge_tid, target_sl)
                        pair_ctx["cooldown_until"] = now + 18.0
                        hedge_store.hedge_metrics["successful_neutralizations"] = int(hedge_store.hedge_metrics.get("successful_neutralizations", 0)) + 1
                        hedge_store.dirty = True
                        return {
                            "executed": True,
                            "action": "INCREASE_HEDGE",
                            "reason": f"LOCKED_HEDGE_SL_BREAKEVEN(id={str(hedge_tid)[:6]}|sl={target_sl})",
                        }
                    except Exception:
                        pass
            return {"executed": False, "action": act, "reason": "INCREASE_HEDGE_NO_OP"}

        # ------------------------------------------------------------------
        # 2. REDUCE_HEDGE (vía cash_in v3 o cierre si lote es mínimo)
        # ------------------------------------------------------------------
        if act == "REDUCE_HEDGE" and hedge_tid:
            h_pl = int((hedge_trade or {}).get("pl") or 0)
            if h_pl >= 40 and hasattr(client, "cash_in"):
                cash_amt = max(20, h_pl // 2)
                try:
                    client.cash_in(hedge_tid, cash_amt)
                    pair_ctx["cumulative_cashed_in_sats"] = int(pair_ctx.get("cumulative_cashed_in_sats", 0)) + cash_amt
                    pair_ctx["cooldown_until"] = now + 20.0
                    hedge_store.hedge_metrics["total_cashed_in_sats"] = int(hedge_store.hedge_metrics.get("total_cashed_in_sats", 0)) + cash_amt
                    hedge_store.dirty = True
                    return {
                        "executed": True,
                        "action": "REDUCE_HEDGE",
                        "cashed_in_sats": cash_amt,
                        "reason": f"CASHED_IN_HEDGE_PROFIT(id={str(hedge_tid)[:6]}|+{cash_amt}sats)",
                    }
                except Exception:
                    pass
            # Fallback: si no aplica cash_in, cerrar la cobertura parcial en beneficio
            if h_pl > 15:
                act = "CLOSE_HEDGE"
            else:
                return {"executed": False, "action": "REDUCE_HEDGE", "reason": "HEDGE_PL_INSUFFICIENT_FOR_REDUCE"}

        # ------------------------------------------------------------------
        # 3. CLOSE_HEDGE (desarme de cobertura cuando el movimiento adverso se agota)
        # ------------------------------------------------------------------
        if act == "CLOSE_HEDGE" and hedge_tid:
            h_pl = int((hedge_trade or {}).get("pl") or 0)
            h_notional = int(pair_ctx.get("hedge_notional_sats", 15000))
            exit_fee = max(1, int(round(h_notional * cls.TAKER_FEE)))
            resp = client.close_trade(hedge_tid)
            confirmed = isinstance(resp, dict) and (resp.get("closed") is True or str(resp.get("id", "")) == str(hedge_tid))
            if confirmed:
                net_hedge_gain = h_pl - exit_fee
                pair_ctx["cumulative_hedge_fees_sats"] = int(pair_ctx.get("cumulative_hedge_fees_sats", 0)) + exit_fee
                pair_ctx["cumulative_saved_sats"] = int(pair_ctx.get("cumulative_saved_sats", 0)) + net_hedge_gain
                pair_ctx["hedge_trade_id"] = None
                pair_ctx["hedge_side"] = None
                pair_ctx["hedge_margin_sats"] = 0
                pair_ctx["hedge_leverage"] = 0.0
                pair_ctx["hedge_notional_sats"] = 0
                pair_ctx["hedge_entry_price"] = 0.0
                pair_ctx["hedge_ratio"] = 0.0
                pair_ctx["cooldown_until"] = now + 25.0

                hedge_store.hedge_metrics["total_hedge_fees_sats"] = int(hedge_store.hedge_metrics.get("total_hedge_fees_sats", 0)) + exit_fee
                hedge_store.hedge_metrics["net_sats_saved_vs_sl"] = int(hedge_store.hedge_metrics.get("net_sats_saved_vs_sl", 0)) + net_hedge_gain
                if net_hedge_gain > 0:
                    hedge_store.hedge_metrics["successful_unwinds"] = int(hedge_store.hedge_metrics.get("successful_unwinds", 0)) + 1
                    hedge_store.record_regime_metric(market_regime, "unwinds", 1)
                else:
                    hedge_store.hedge_metrics["premature_hedges"] = int(hedge_store.hedge_metrics.get("premature_hedges", 0)) + 1
                hedge_store.record_regime_metric(market_regime, "saved_sats", net_hedge_gain)
                hedge_store.record_regime_metric(market_regime, "fees_sats", exit_fee)
                hedge_store.dirty = True
                return {
                    "executed": True,
                    "action": "CLOSE_HEDGE",
                    "net_hedge_gain_sats": net_hedge_gain,
                    "reason": f"CLOSED_HEDGE_LEG(id={str(hedge_tid)[:6]}|net={net_hedge_gain:+d}sats)",
                }
            return {"executed": False, "action": "CLOSE_HEDGE", "reason": "CLOSE_HEDGE_UNCONFIRMED"}

        # ------------------------------------------------------------------
        # 4. REDUCE_ORIGINAL_POSITION (cosecha parcial cash_in o ajuste de TP/SL)
        # ------------------------------------------------------------------
        if act == "REDUCE_ORIGINAL_POSITION" and orig_tid:
            orig_pl = int(orig_trade.get("pl") or 0)
            if orig_pl >= 40 and hasattr(client, "cash_in"):
                cash_amt = max(20, orig_pl // 2)
                try:
                    client.cash_in(orig_tid, cash_amt)
                    pair_ctx["cumulative_cashed_in_sats"] = int(pair_ctx.get("cumulative_cashed_in_sats", 0)) + cash_amt
                    pair_ctx["cooldown_until"] = now + 20.0
                    hedge_store.dirty = True
                    return {
                        "executed": True,
                        "action": "REDUCE_ORIGINAL_POSITION",
                        "cashed_in_sats": cash_amt,
                        "reason": f"CASHED_IN_ORIG_PROFIT(id={orig_tid[:6]}|+{cash_amt}sats)",
                    }
                except Exception:
                    pass
            # Si está en pérdida moderada, acercar Take Profit a breakeven para salida rápida en rebote
            if hasattr(client, "update_takeprofit"):
                orig_entry = float(orig_trade.get("price") or orig_trade.get("entryPrice") or ind_5m.get("price", 0.0))
                orig_side = str(orig_trade.get("side", "buy")).lower()
                be_tp = int(round(orig_entry * (1.0022 if orig_side == "buy" else 0.9978)))
                try:
                    client.update_takeprofit(orig_tid, be_tp)
                    pair_ctx["cooldown_until"] = now + 20.0
                    return {
                        "executed": True,
                        "action": "REDUCE_ORIGINAL_POSITION",
                        "reason": f"TIGHTENED_ORIG_TP_TO_BE(id={orig_tid[:6]}|tp={be_tp})",
                    }
                except Exception:
                    pass
            return {"executed": False, "action": "REDUCE_ORIGINAL_POSITION", "reason": "REDUCE_ORIG_NO_OP"}

        # ------------------------------------------------------------------
        # 5. CLOSE_ORIGINAL_POSITION (tesis primaria invalidada)
        # ------------------------------------------------------------------
        if act == "CLOSE_ORIGINAL_POSITION" and orig_tid:
            resp = client.close_trade(orig_tid)
            confirmed = isinstance(resp, dict) and (resp.get("closed") is True or str(resp.get("id", "")) == str(orig_tid))
            if confirmed:
                hedge_store.hedge_metrics["avoided_stopouts"] = int(hedge_store.hedge_metrics.get("avoided_stopouts", 0)) + 1
                hedge_store.dirty = True
                return {
                    "executed": True,
                    "action": "CLOSE_ORIGINAL_POSITION",
                    "orig_closed_id": orig_tid,
                    "reason": f"CLOSED_INVALIDATED_ORIG(id={orig_tid[:6]})",
                }
            return {"executed": False, "action": "CLOSE_ORIGINAL_POSITION", "reason": "CLOSE_ORIG_UNCONFIRMED"}

        return {"executed": False, "action": act, "reason": "UNHANDLED_HEDGE_ACTION"}
