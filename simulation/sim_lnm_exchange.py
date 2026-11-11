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
    ):
        self.initial_balance_sats = int(initial_balance_sats)
        self.balance_sats = int(initial_balance_sats)
        self.taker_fee_rate = float(taker_fee_rate)
        self.funding_rate_8h = float(funding_rate_8h)
        self.default_slippage_bps = float(default_slippage_bps)

        self.running_trades: Dict[str, dict] = {}
        self.closed_trades: List[dict] = []
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
        return sum(int(t.get("margin", 0)) for t in self.running_trades.values())

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

        # 4. Precio de liquidación exacto con margen de mantenimiento (0.99 para 1% de buffer)
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
        exit_price = float(price if price is not None else self.last_price)
        cur_ts = float(now if now is not None else self.last_ts)

        notional = trade["notional_sats"]
        entry_price = trade["entry_price"]
        side = trade["side"]

        # PnL Bruto
        if side == "b":
            gross_pnl = int(round(notional * ((exit_price - entry_price) / entry_price)))
        else:
            gross_pnl = int(round(notional * ((entry_price - exit_price) / entry_price)))

        # Comisión Taker de Salida
        exit_fee = max(1, int(round(notional * self.taker_fee_rate)))
        self.total_fees_paid_sats += exit_fee

        # PnL Neto de la operación cerrada
        net_trade_pnl = gross_pnl - exit_fee
        self.balance_sats += net_trade_pnl
        self.total_realized_pnl_sats += (net_trade_pnl - trade["entry_fee_sats"] - trade["sum_carry_fees"])

        closed_rec = {
            "id": tid,
            "side": side,
            "margin": trade["margin"],
            "leverage": trade["leverage"],
            "entry_price": entry_price,
            "exit_price": round(exit_price, 2),
            "gross_pnl_sats": gross_pnl,
            "entry_fee_sats": trade["entry_fee_sats"],
            "exit_fee_sats": exit_fee,
            "carry_fees_sats": trade["sum_carry_fees"],
            "net_pnl_sats": net_trade_pnl - trade["entry_fee_sats"] - trade["sum_carry_fees"],
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
        if t["side"] == "b":
            t["liquidation"] = round(entry * (1.0 - (0.99 / new_lev)), 2)
        else:
            t["liquidation"] = round(entry * (1.0 + (0.99 / new_lev)), 2)

        return True, {"id": tid, "new_margin": t["margin"], "new_liquidation": t["liquidation"]}

    def step_candle(self, candle: Any) -> List[dict]:
        """
        Avanza un paso de simulación evaluando las órdenes abiertas contra los extremos
        de la vela (High, Low, Open, Close). Verifica Stop Loss, Take Profit y Liquidaciones.
        """
        c_open = float(candle.open)
        c_high = float(candle.high)
        c_low = float(candle.low)
        c_close = float(candle.close)
        c_ts = float(candle.timestamp)

        self.last_price = c_close
        self.last_ts = c_ts

        events_triggered = []

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

        # 2. Evaluar Liquidaciones, Stop Losses y Take Profits
        for tid in list(self.running_trades.keys()):
            if tid not in self.running_trades:
                continue
            t = self.running_trades[tid]
            side = t["side"]
            sl = float(t.get("stoploss", 0.0))
            tp = float(t.get("takeprofit", 0.0))
            liq = float(t["liquidation"])

            # A. Comprobar Liquidación (Prioridad P0 absoluta)
            is_liquidated = (c_low <= liq) if side == "b" else (c_high >= liq)
            if is_liquidated:
                # Pérdida total del margen
                trade_obj = self.running_trades.pop(tid)
                net_loss = trade_obj["margin"]
                self.balance_sats -= net_loss
                self.total_realized_pnl_sats -= (net_loss + trade_obj["entry_fee_sats"])
                rec = {
                    "id": tid,
                    "close_reason": "LIQUIDATION",
                    "exit_price": liq,
                    "net_pnl_sats": -net_loss - trade_obj["entry_fee_sats"],
                }
                self.closed_trades.append(rec)
                events_triggered.append(rec)
                continue

            # B. Comprobar Stop Loss (con modelado de Gap Risk)
            if sl > 0:
                is_sl_hit = (c_low <= sl) if side == "b" else (c_high >= sl)
                if is_sl_hit:
                    # En caso de gap de apertura que salte el SL, se ejecuta al precio de apertura real
                    if side == "b":
                        fill_price = min(c_open, sl)
                    else:
                        fill_price = max(c_open, sl)
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

            # D. Actualizar PnL flotante sobre velas vivas
            notional = t["notional_sats"]
            entry = t["entry_price"]
            if side == "b":
                t["pl"] = int(round(notional * ((c_close - entry) / entry)))
            else:
                t["pl"] = int(round(notional * ((entry - c_close) / entry)))

        return events_triggered
