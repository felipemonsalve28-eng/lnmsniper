# -*- coding: utf-8 -*-
"""
portfolio_hedge.py
Capa 3 (Hedge Execution & Portfolio Control) del Hybrid Edge Hedging Engine.
Coordina la exposición global del portafolio en satoshis, vincula cada posición
primaria con su pata de cobertura en LN Markets API v3 y calcula el punto de
equilibrio combinado (breakeven neto en sats).
"""


class PortfolioHedgeCoordinator:
    """
    Separa las posiciones primarias de las coberturas, consolida el delta neto
    del portafolio y calcula la contabilidad conjunta de cada par (Primaria + Hedge).
    """

    TAKER_FEE = 0.0010

    @classmethod
    def reconcile_portfolio(cls, running_trades, hedge_store, equity_sats, btc_price, now=None):
        hedge_store.reconcile_with_live_trades(running_trades, now=now)
        hedge_ids = hedge_store.get_all_hedge_trade_ids()

        live_map = {}
        primary_trades = []
        hedge_trades_map = {}

        gross_long_notional = 0
        gross_short_notional = 0
        locked_margin_sats = 0
        floating_gross_sats = 0
        est_exit_fees_sats = 0
        carry_fees_sats = 0

        for t in (running_trades or []):
            if not isinstance(t, dict) or "id" not in t:
                continue
            tid = str(t["id"])
            live_map[tid] = t

            side = str(t.get("side", "buy")).lower()
            margin = int(t.get("margin") or 0)
            lev = float(t.get("leverage") or 20.0)
            notional = int(round(margin * lev))
            pl = int(t.get("pl") or 0)
            exit_fee = max(1, int(round(notional * cls.TAKER_FEE)))
            carry = int(round(float(t.get("sum_carry_fees") or t.get("funding_fees") or 0)))

            if side == "buy":
                gross_long_notional += notional
            else:
                gross_short_notional += notional

            locked_margin_sats += margin
            floating_gross_sats += pl
            est_exit_fees_sats += exit_fee
            carry_fees_sats += max(0, carry)

            if tid in hedge_ids:
                hedge_trades_map[tid] = t
            else:
                primary_trades.append(t)

        net_delta_notional = gross_long_notional - gross_short_notional
        gross_total_notional = gross_long_notional + gross_short_notional
        net_exposure_ratio = locked_margin_sats / max(float(equity_sats), 1.0)
        combined_floating_net_sats = floating_gross_sats - est_exit_fees_sats - carry_fees_sats

        return {
            "primary_trades": primary_trades,
            "hedge_trades_map": hedge_trades_map,
            "live_map": live_map,
            "gross_long_notional_sats": gross_long_notional,
            "gross_short_notional_sats": gross_short_notional,
            "gross_total_notional_sats": gross_total_notional,
            "net_delta_notional_sats": net_delta_notional,
            "locked_margin_sats": locked_margin_sats,
            "net_exposure_ratio": round(net_exposure_ratio, 4),
            "combined_floating_gross_sats": floating_gross_sats,
            "combined_est_exit_fees_sats": est_exit_fees_sats,
            "combined_carry_fees_sats": carry_fees_sats,
            "combined_floating_net_sats": combined_floating_net_sats,
            "btc_price": float(btc_price),
        }

    @classmethod
    def evaluate_pair_economics(cls, orig_trade, hedge_trade, pair_ctx, btc_price):
        price = max(1.0, float(btc_price))
        orig_side = str(orig_trade.get("side", "buy")).lower()
        orig_margin = int(orig_trade.get("margin") or 1000)
        orig_lev = float(orig_trade.get("leverage") or 20.0)
        orig_notional = int(round(orig_margin * orig_lev))
        orig_entry = float(orig_trade.get("price") or orig_trade.get("entryPrice") or price)
        orig_pl = int(orig_trade.get("pl") or 0)
        orig_exit_fee = max(1, int(round(orig_notional * cls.TAKER_FEE)))
        orig_carry = int(round(float(orig_trade.get("sum_carry_fees") or orig_trade.get("funding_fees") or 0)))

        if hedge_trade is None:
            net_pl = orig_pl - orig_exit_fee - max(0, orig_carry) + int(pair_ctx.get("cumulative_cashed_in_sats", 0))
            fee_offset_pct = 2.2 * cls.TAKER_FEE
            be_price = orig_entry * (1.0 + fee_offset_pct) if orig_side == "buy" else orig_entry * (1.0 - fee_offset_pct)
            return {
                "has_active_hedge": False,
                "orig_pl_sats": orig_pl,
                "hedge_pl_sats": 0,
                "combined_gross_pl_sats": orig_pl,
                "combined_exit_fees_sats": orig_exit_fee,
                "combined_net_pl_sats": net_pl,
                "effective_hedge_ratio": 0.0,
                "net_pair_notional_sats": orig_notional if orig_side == "buy" else -orig_notional,
                "combined_breakeven_price": round(be_price, 1),
            }

        hedge_side = str(hedge_trade.get("side", "sell" if orig_side == "buy" else "buy")).lower()
        hedge_margin = int(hedge_trade.get("margin") or 1000)
        hedge_lev = float(hedge_trade.get("leverage") or 20.0)
        hedge_notional = int(round(hedge_margin * hedge_lev))
        hedge_entry = float(hedge_trade.get("price") or hedge_trade.get("entryPrice") or price)
        hedge_pl = int(hedge_trade.get("pl") or 0)
        hedge_exit_fee = max(1, int(round(hedge_notional * cls.TAKER_FEE)))
        hedge_carry = int(round(float(hedge_trade.get("sum_carry_fees") or hedge_trade.get("funding_fees") or 0)))

        cashed_in = int(pair_ctx.get("cumulative_cashed_in_sats", 0))
        combined_gross = orig_pl + hedge_pl + cashed_in
        combined_fees = orig_exit_fee + hedge_exit_fee + max(0, orig_carry) + max(0, hedge_carry)
        combined_net = combined_gross - combined_fees

        eff_ratio = hedge_notional / max(float(orig_notional), 1.0)
        pair_ctx["hedge_ratio"] = round(eff_ratio, 3)
        if combined_net > float(pair_ctx.get("peak_combined_net_sats", -999999)):
            pair_ctx["peak_combined_net_sats"] = combined_net

        signed_orig = orig_notional if orig_side == "buy" else -orig_notional
        signed_hedge = hedge_notional if hedge_side == "buy" else -hedge_notional
        net_pair_notional = signed_orig + signed_hedge

        # Precio de equilibrio combinado aproximado
        if abs(net_pair_notional) < max(500, int(orig_notional * 0.08)):
            be_price = price
        else:
            missing_sats = -combined_net
            req_pct_move = missing_sats / float(net_pair_notional)
            be_price = max(100.0, price * (1.0 + max(-0.15, min(0.15, req_pct_move))))

        return {
            "has_active_hedge": True,
            "orig_pl_sats": orig_pl,
            "hedge_pl_sats": hedge_pl,
            "hedge_notional_sats": hedge_notional,
            "hedge_margin_sats": hedge_margin,
            "hedge_leverage": hedge_lev,
            "hedge_entry_price": hedge_entry,
            "combined_gross_pl_sats": combined_gross,
            "combined_exit_fees_sats": combined_fees,
            "combined_net_pl_sats": combined_net,
            "effective_hedge_ratio": round(eff_ratio, 3),
            "net_pair_notional_sats": net_pair_notional,
            "combined_breakeven_price": round(be_price, 1),
        }
