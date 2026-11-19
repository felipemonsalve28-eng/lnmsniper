# -*- coding: utf-8 -*-
"""
simulation/sim_lnm_exchange.py
Simulador virtual de alta precisión del exchange LN Markets (API v3).
Implementa con rigor matemático:
1. Margen aislado en Satoshis y cálculo de Nocional.
2. Comisiones Taker exactas (0.10% a la apertura y al cierre).
3. Financiamiento de Carry (Funding fees cada 8 horas).
4. Slippage dinámico basado en ATR/volatilidad.
5. Precio de quiebra y liquidación estricta al tocar precio de liquidación.
6. Riesgo de salto de liquidez (Gap Risk) en la ejecución de Stop Loss.
7. Compatibilidad de interfaz con LNMarketsClient y WalkForwardSimClient.
"""
from typing import Dict, List, Optional, Tuple, Any
import math
import time


class SimLNMExchange:
    """
    Exchange virtual determinista que emula la API v3 de LN Markets.
    """

    def __init__(
        self,
        initial_balance_sats: int = 100000,
        taker_fee_rate: float = 0.0010,
        funding_rate_8h: float = 0.0001,
        default_slippage_bps: float = 1.5,
        inverse_contract_math: bool = False,
    ):
        self.initial_balance_sats = int(initial_balance_sats)
        self.balance_sats = int(initial_balance_sats)
        self.taker_fee_rate = float(taker_fee_rate)
        self.funding_rate_8h = float(funding_rate_8h)
        self.default_slippage_bps = float(default_slippage_bps)
        self.inverse_contract_math = bool(inverse_contract_math)

        self.running_trades: Dict[str, dict] = {}
        self.open_orders: Dict[str, dict] = {}
        self.closed_trades: List[dict] = []
        self.limit_fills_count: int = 0
        self.limit_cancelled_count: int = 0
        self.run_id: str = f"{int(time.time() * 1000) % 10000000:07d}"
        self.trade_counter: int = 1000

        self.total_fees_paid_sats: int = 0
        self.total_funding_paid_sats: int = 0
        self.total_realized_pnl_sats: int = 0
        self.cashed_in_total_sats: int = 0

        self.last_price: float = 70000.0
        self.last_ts: float = 0.0

    @property
    def total_locked_margin_sats(self) -> int:
        run_m = sum(int(t.get("margin", 0)) for t in self.running_trades.values())
        open_m = sum(int(o.get("margin", 0)) for o in self.open_orders.values())
        return run_m + open_m

    @property
    def available_balance_sats(self) -> int:
        return max(0, self.balance_sats - self.total_locked_margin_sats)

    @property
    def equity_sats(self) -> int:
        unrealized_pl = sum(int(t.get("pl", 0)) for t in self.running_trades.values())
        return self.balance_sats + unrealized_pl

    def get_user(self, force_refresh: bool = False) -> dict:
        """Emula client.get_user() devolviendo balance, margen y balance disponible."""
        return {
            "balance": self.balance_sats,
            "margin": self.total_locked_margin_sats,
            "available_balance": self.available_balance_sats,
            "equity": self.equity_sats,
        }

    def get_running_trades(self, force_refresh: bool = False) -> List[dict]:
        """Emula client.get_running_trades()."""
        return list(self.running_trades.values())

    def get_open_orders(self, force_refresh: bool = False) -> List[dict]:
        """Emula client.get_open_orders()."""
        return list(self.open_orders.values())

    def open_limit_trade(
        self,
        side: str,
        margin: int,
        leverage: int,
        price: float,
        stoploss: Optional[float] = None,
        takeprofit: Optional[float] = None,
        client_id: str = "",
        now: Optional[float] = None,
    ) -> dict:
        """Registra una orden limite pasiva reservando margen aislado sin slippage."""
        side_norm = "b" if str(side).lower() in ("b", "buy", "long") else "s"
        m = int(margin)
        lev = int(leverage)
        lim_px = round(round(float(price) * 2.0) / 2.0, 2)
        cur_ts = float(now if now is not None else self.last_ts)
        if m <= 0 or lev <= 0 or lim_px <= 0.0:
            raise ValueError("Parametros invalidos en open_limit_trade.")
        if m > self.available_balance_sats:
            raise RuntimeError(f"Saldo disponible insuficiente ({self.available_balance_sats} sats) para limite de {m} sats.")
        self.trade_counter += 1
        oid = f"simlim-{self.run_id}-{self.trade_counter}"
        rec = {
            "id": oid,
            "client_id": str(client_id),
            "side": side_norm,
            "type": "limit",
            "margin": m,
            "leverage": lev,
            "price": lim_px,
            "entry_price": lim_px,
            "stoploss": round(float(stoploss), 2) if stoploss else 0.0,
            "takeprofit": round(float(takeprofit), 2) if takeprofit else 0.0,
            "open_ts": cur_ts,
            "status": "open",
        }
        self.open_orders[oid] = rec
        return dict(rec)

    def cancel_open_order(self, order_id: str) -> dict:
        oid = str(order_id)
        if oid in self.open_orders:
            rec = self.open_orders.pop(oid)
            rec["status"] = "cancelled"
            self.limit_cancelled_count += 1
            return dict(rec)
        raise RuntimeError(f"HTTP 404 (/futures/isolated/trade/cancel): order {oid} not found")

    def open_trade(
        self,
        side: str,
        margin: int,
        leverage: int,
        stoploss: Optional[float] = None,
        takeprofit: Optional[float] = None,
        price: Optional[float] = None,
        atr: Optional[float] = None,
        now: Optional[float] = None,
    ) -> dict:
        """
        Abre una posición con margen aislado en Satoshis.
        Aplica comisiones de entrada y calcula precio de liquidación exacto.
        """
        side_norm = "b" if str(side).lower() in ("b", "buy", "long") else "s"
        m = int(margin)
        lev = int(leverage)
        ref_price = float(price if price is not None else self.last_price)
        cur_ts = float(now if now is not None else self.last_ts)

        if m <= 0 or lev <= 0:
            raise ValueError(f"Margen ({m}) y apalancamiento ({lev}) deben ser estrictamente positivos.")

        if m > self.available_balance_sats:
            raise RuntimeError(
                f"Saldo disponible insuficiente ({self.available_balance_sats} sats) para margen de {m} sats."
            )

        # 1. Nocional en Satoshis
        notional_sats = m * lev

        # 2. Slippage dinámico proporcional a la volatilidad
        atr_val = float(atr if atr is not None else (ref_price * 0.0015))
        slip_ratio = (self.default_slippage_bps / 10000.0) * (1.0 + (atr_val / max(ref_price, 1.0)) * 10.0)
        if side_norm == "b":
            entry_price = ref_price * (1.0 + slip_ratio)
        else:
            entry_price = ref_price * (1.0 - slip_ratio)

        # 3. Comisión Taker de Entrada
        entry_fee = max(1, int(round(notional_sats * self.taker_fee_rate)))
        self.balance_sats -= entry_fee
        self.total_fees_paid_sats += entry_fee

        # 4. Precio de liquidación con margen de mantenimiento (0.99 para 1% de buffer)
        if self.inverse_contract_math:
            if side_norm == "b":
                liq_price = entry_price / (1.0 + (0.99 / lev))
            else:
                liq_price = entry_price / max(0.05, 1.0 - (0.99 / lev))
        else:
            if side_norm == "b":
                liq_price = entry_price * (1.0 - (0.99 / lev))
            else:
                liq_price = entry_price * (1.0 + (0.99 / lev))

        self.trade_counter += 1
        trade_id = f"sim-{self.run_id}-{self.trade_counter}"

        trade = {
            "id": trade_id,
            "side": side_norm,
            "margin": m,
            "leverage": lev,
            "price": round(entry_price, 2),
            "entry_price": round(entry_price, 2),
            "stoploss": round(float(stoploss), 2) if stoploss else 0.0,
            "takeprofit": round(float(takeprofit), 2) if takeprofit else 0.0,
            "liquidation": round(liq_price, 2),
            "pl": 0,
            "cashed_in_sats": 0,
            "sum_carry_fees": 0,
            "notional_sats": notional_sats,
            "entry_fee_sats": entry_fee,
            "open_ts": cur_ts,
            "last_funding_ts": cur_ts,
        }
        self.running_trades[trade_id] = trade
        return dict(trade)

    def close_trade(
        self,
        trade_id: str,
        price: Optional[float] = None,
        reason: str = "MANUAL_CLOSE",
        now: Optional[float] = None,
    ) -> Tuple[bool, Optional[dict]]:
        """
        Cierra una posición abierta, deduciendo comisiones de salida y liquidando PnL neto.
        """
        tid = str(trade_id)
        if tid not in self.running_trades:
            return False, None

        trade = self.running_trades.pop(tid)
        exit_price = max(1.0, float(price if price is not None else self.last_price))
        cur_ts = float(now if now is not None else self.last_ts)

        notional = trade["notional_sats"]
        entry_price = trade["entry_price"]
        side = trade["side"]
        cashed_in_sats = int(trade.get("cashed_in_sats", 0))

        # PnL Bruto total respecto al precio de entrada
        denom_price = exit_price if self.inverse_contract_math else entry_price
        if side == "b":
            gross_pnl = int(round(notional * ((exit_price - entry_price) / denom_price)))
        else:
            gross_pnl = int(round(notional * ((entry_price - exit_price) / denom_price)))

        remaining_gross_pnl = gross_pnl - cashed_in_sats

        # Comisión Taker de Salida
        exit_fee = max(1, int(round(notional * self.taker_fee_rate)))
        self.total_fees_paid_sats += exit_fee

        # PnL Neto remanente acreditado al balance al cerrar (sin duplicar lo ya cobrado en cash_in)
        net_remaining_pnl = remaining_gross_pnl - exit_fee
        self.balance_sats += net_remaining_pnl
        total_net_pnl = gross_pnl - exit_fee - trade["entry_fee_sats"] - trade["sum_carry_fees"]
        self.total_realized_pnl_sats += total_net_pnl

        closed_rec = {
            "id": tid,
            "side": side,
            "margin": trade["margin"],
            "leverage": trade["leverage"],
            "entry_price": entry_price,
            "exit_price": round(exit_price, 2),
            "gross_pnl_sats": gross_pnl,
            "cashed_in_sats": cashed_in_sats,
            "entry_fee_sats": trade["entry_fee_sats"],
            "exit_fee_sats": exit_fee,
            "carry_fees_sats": trade["sum_carry_fees"],
            "net_pnl_sats": total_net_pnl,
            "close_reason": reason,
            "open_ts": trade["open_ts"],
            "close_ts": cur_ts,
            "duration_s": max(0.0, cur_ts - trade["open_ts"]),
        }
        self.closed_trades.append(closed_rec)
        return True, closed_rec

    def cash_in(self, trade_id: str, amount_sats: int) -> Tuple[bool, Optional[dict]]:
        """Realiza una cosecha parcial de beneficios en satoshis sobre una posición activa."""
        tid = str(trade_id)
        amt = int(amount_sats)
        if tid not in self.running_trades or amt <= 0:
            return False, None

        t = self.running_trades[tid]
        cur_pl = int(t.get("pl", 0))
        if cur_pl < amt:
            return False, None

        t["pl"] = cur_pl - amt
        t["cashed_in_sats"] = int(t.get("cashed_in_sats", 0)) + amt
        self.balance_sats += amt
        self.cashed_in_total_sats += amt
        return True, {"id": tid, "cashed_in": amt, "remaining_pl": t["pl"]}

    def add_margin(self, trade_id: str, amount_sats: int) -> Tuple[bool, Optional[dict]]:
        """Añade margen a una posición aislada, alejando el precio de liquidación."""
        tid = str(trade_id)
        amt = int(amount_sats)
        if tid not in self.running_trades or amt <= 0 or amt > self.available_balance_sats:
            return False, None

        t = self.running_trades[tid]
        t["margin"] += amt
        # Recalcular nuevo apalancamiento efectivo y precio de liquidación
        new_lev = max(1.0, float(t["notional_sats"]) / float(t["margin"]))
        entry = t["entry_price"]
        if self.inverse_contract_math:
            if t["side"] == "b":
                t["liquidation"] = round(entry / (1.0 + (0.99 / new_lev)), 2)
            else:
                t["liquidation"] = round(entry / max(0.05, 1.0 - (0.99 / new_lev)), 2)
        else:
            if t["side"] == "b":
                t["liquidation"] = round(entry * (1.0 - (0.99 / new_lev)), 2)
            else:
                t["liquidation"] = round(entry * (1.0 + (0.99 / new_lev)), 2)

        return True, {"id": tid, "new_margin": t["margin"], "new_liquidation": t["liquidation"]}

    def update_stoploss(self, trade_id: str, stoploss_price: float) -> dict:
        """Actualiza el nivel de Stop Loss de una posición simulada activa."""
        tid = str(trade_id)
        if tid not in self.running_trades:
            return {}
        t = self.running_trades[tid]
        t["stoploss"] = round(float(stoploss_price), 2)
        return {"id": tid, "stoploss": t["stoploss"]}

    def update_takeprofit(self, trade_id: str, takeprofit_price: float) -> dict:
        """Actualiza el nivel de Take Profit de una posición simulada activa."""
        tid = str(trade_id)
        if tid not in self.running_trades:
            return {}
        t = self.running_trades[tid]
        t["takeprofit"] = round(float(takeprofit_price), 2)
        return {"id": tid, "takeprofit": t["takeprofit"]}

    def step_candle(self, candle: Any) -> List[dict]:
        """
        Avanza un paso de simulación evaluando las órdenes abiertas contra los extremos
        de la vela (High, Low, Open, Close). Verifica Stop Loss, Take Profit y Liquidaciones.
        """
        c_open = float(candle.open)
        c_high = float(candle.high)
        c_low = float(candle.low)
        c_close = max(1.0, float(candle.close))
        c_ts = float(candle.timestamp)

        self.last_price = c_close
        self.last_ts = c_ts

        events_triggered = []

        # 0. Llenado conservador estricto de ordenes limite (requiere cruzar >= 1 tick = $0.5).
        #    Evaluado ANTES de running_trades: si la misma barra 1m toca SL, cierra pesimistamente en SL.
        for oid in list(self.open_orders.keys()):
            ord_rec = self.open_orders.get(oid)
            if not ord_rec:
                continue
            p_lim = float(ord_rec["price"])
            o_side = ord_rec["side"]
            filled = (c_low <= (p_lim - 0.5)) if o_side == "b" else (c_high >= (p_lim + 0.5))
            if filled:
                self.open_orders.pop(oid, None)
                m = int(ord_rec["margin"])
                lev = int(ord_rec["leverage"])
                notional_sats = m * lev
                entry_fee = max(1, int(round(notional_sats * self.taker_fee_rate)))
                self.balance_sats -= entry_fee
                self.total_fees_paid_sats += entry_fee
                if self.inverse_contract_math:
                    liq_px = (p_lim / (1.0 + (0.99 / lev))) if o_side == "b" else (p_lim / max(0.05, 1.0 - (0.99 / lev)))
                else:
                    liq_px = (p_lim * (1.0 - (0.99 / lev))) if o_side == "b" else (p_lim * (1.0 + (0.99 / lev)))
                trade = {
                    "id": oid,
                    "client_id": ord_rec.get("client_id", ""),
                    "side": o_side,
                    "type": "limit",
                    "margin": m,
                    "leverage": lev,
                    "notional_sats": notional_sats,
                    "price": round(p_lim, 2),
                    "entry_price": round(p_lim, 2),
                    "liquidation": round(liq_px, 2),
                    "stoploss": float(ord_rec.get("stoploss", 0.0)),
                    "takeprofit": float(ord_rec.get("takeprofit", 0.0)),
                    "pl": 0,
                    "open_ts": c_ts,
                    "last_funding_ts": c_ts,
                    "entry_fee_sats": entry_fee,
                    "sum_carry_fees": 0,
                    "cashed_in_sats": 0,
                    "running": True,
                }
                self.running_trades[oid] = trade
                self.limit_fills_count += 1

        # 1. Evaluar Funding Rate periódico (cada 8h = 28,800 segundos)
        for tid, t in list(self.running_trades.items()):
            dt = c_ts - float(t.get("last_funding_ts", t["open_ts"]))
            if dt >= 28800.0:
                funding_blocks = int(dt // 28800.0)
                carry_sats = int(round(t["notional_sats"] * self.funding_rate_8h * funding_blocks))
                t["sum_carry_fees"] += carry_sats
                t["last_funding_ts"] = float(t.get("last_funding_ts", t["open_ts"])) + (funding_blocks * 28800.0)
                self.balance_sats -= carry_sats
                self.total_funding_paid_sats += carry_sats

        # 2. Evaluar Stop Losses, Liquidaciones y Take Profits respetando la trayectoria intra-vela
        for tid in list(self.running_trades.keys()):
            if tid not in self.running_trades:
                continue
            t = self.running_trades[tid]
            side = t["side"]
            sl = float(t.get("stoploss", 0.0))
            tp = float(t.get("takeprofit", 0.0))
            liq = float(t["liquidation"])

            gap_to_liq = (c_open <= liq) if side == "b" else (c_open >= liq)
            sl_protects_before_liq = (
                sl > 0.0
                and not gap_to_liq
                and ((side == "b" and sl > liq) or (side == "s" and sl < liq))
            )

            # Si existe SL válido por encima/debajo de liquidación y no hubo gap directo a liquidación,
            # el precio cruza primero el Stop Loss antes de alcanzar el nivel de liquidación.
            if sl_protects_before_liq:
                is_sl_hit = (c_low <= sl) if side == "b" else (c_high >= sl)
                if is_sl_hit:
                    fill_price = min(c_open, sl) if side == "b" else max(c_open, sl)
                    ok, rec = self.close_trade(tid, price=fill_price, reason="STOP_LOSS", now=c_ts)
                    if ok:
                        events_triggered.append(rec)
                    continue

            # A. Comprobar Liquidación
            is_liquidated = (c_low <= liq) if side == "b" else (c_high >= liq)
            if is_liquidated:
                trade_obj = self.running_trades.pop(tid)
                net_loss = trade_obj["margin"]
                cashed_in_sats = int(trade_obj.get("cashed_in_sats", 0))
                self.balance_sats -= net_loss
                total_liq_net = cashed_in_sats - net_loss - trade_obj["entry_fee_sats"] - trade_obj["sum_carry_fees"]
                self.total_realized_pnl_sats += total_liq_net
                rec = {
                    "id": tid,
                    "side": side,
                    "margin": trade_obj["margin"],
                    "leverage": trade_obj["leverage"],
                    "entry_price": trade_obj["entry_price"],
                    "exit_price": liq,
                    "gross_pnl_sats": cashed_in_sats - net_loss,
                    "cashed_in_sats": cashed_in_sats,
                    "entry_fee_sats": trade_obj["entry_fee_sats"],
                    "exit_fee_sats": 0,
                    "carry_fees_sats": trade_obj["sum_carry_fees"],
                    "net_pnl_sats": total_liq_net,
                    "close_reason": "LIQUIDATION",
                    "open_ts": trade_obj["open_ts"],
                    "close_ts": c_ts,
                    "duration_s": max(0.0, c_ts - trade_obj["open_ts"]),
                }
                self.closed_trades.append(rec)
                events_triggered.append(rec)
                continue

            # B. Comprobar Stop Loss (con modelado de Gap Risk)
            if sl > 0:
                is_sl_hit = (c_low <= sl) if side == "b" else (c_high >= sl)
                if is_sl_hit:
                    fill_price = min(c_open, sl) if side == "b" else max(c_open, sl)
                    ok, rec = self.close_trade(tid, price=fill_price, reason="STOP_LOSS", now=c_ts)
                    if ok:
                        events_triggered.append(rec)
                    continue

            # C. Comprobar Take Profit
            if tp > 0:
                is_tp_hit = (c_high >= tp) if side == "b" else (c_low <= tp)
                if is_tp_hit:
                    ok, rec = self.close_trade(tid, price=tp, reason="TAKE_PROFIT", now=c_ts)
                    if ok:
                        events_triggered.append(rec)
                    continue

            # D. Actualizar PnL flotante sobre velas vivas (descontando cosechas parciales previas)
            notional = t["notional_sats"]
            entry = t["entry_price"]
            denom_price = c_close if self.inverse_contract_math else entry
            if side == "b":
                raw_pl = int(round(notional * ((c_close - entry) / denom_price)))
            else:
                raw_pl = int(round(notional * ((entry - c_close) / denom_price)))
            t["pl"] = raw_pl - int(t.get("cashed_in_sats", 0))

        return events_triggered
