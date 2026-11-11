#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_simulation_engine.py
Suite de certificación de rigor matemático y funcional para el motor de simulación
(SimClock, SimMarketFeed, SimLNMExchange).
"""
import sys
import os
import unittest
import math

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from simulation.sim_clock import SimClock
from simulation.sim_market_feed import SimMarketFeed, Candle
from simulation.sim_lnm_exchange import SimLNMExchange


class TestSimClock(unittest.TestCase):

    def test_clock_progression_and_cycles(self):
        clock = SimClock(start_timestamp=1000.0)
        self.assertEqual(clock.time(), 1000.0)

        # Avanzar 120 segundos
        new_ts = clock.advance(120.0)
        self.assertEqual(new_ts, 1120.0)
        self.assertEqual(clock.time(), 1120.0)
        self.assertEqual(clock.elapsed_seconds(), 120.0)

        # Emulación de sleep
        clock.sleep(60.0)
        self.assertEqual(clock.time(), 1180.0)

        # Detección de ciclo periódico por buckets (ej. 3600s para 1h)
        # Bucket a t=1180: int(1180 // 3600) = 0
        self.assertFalse(clock.is_cycle_due(3600.0, last_ts=1000.0))

        # Avanzar hasta cruzar la frontera de 3600s
        clock.advance(2500.0)  # t = 3680 -> bucket = 1
        self.assertTrue(clock.is_cycle_due(3600.0, last_ts=1180.0))
        # Si se vuelve a evaluar con el timestamp actualizado, ya no está pendiente
        self.assertFalse(clock.is_cycle_due(3600.0, last_ts=3680.0))


class TestSimMarketFeed(unittest.TestCase):

    def test_mtf_aggregation_and_indicator_generation(self):
        # Generar 60 velas sintéticas de 1m a precio 70,000 con pequeñas oscilaciones
        candles = []
        base_ts = 1700000000.0
        base_price = 70000.0

        for i in range(250):
            p = base_price + math.sin(i / 10.0) * 150.0
            c = Candle(
                timestamp=base_ts + (i * 60.0),
                open=p - 10.0,
                high=p + 25.0,
                low=p - 25.0,
                close=p + 5.0,
                volume=1.5,
            )
            candles.append(c)

        feed = SimMarketFeed(candles)
        # Calentar con 200 velas
        warmed = feed.warmup(200)
        self.assertEqual(warmed, 200)
        self.assertEqual(feed.cursor, 200)

        # Consultar estado de percepción
        state = feed.get_perception_state()
        self.assertIn("ind_5m", state)
        self.assertIn("ind_15m", state)
        self.assertIn("ind_1h", state)
        self.assertIn("regime_info", state)
        self.assertIn("market_regime", state["ind_5m"])
        self.assertIn("rsi", state["ind_5m"])
        self.assertIn("atr", state["ind_5m"])
        self.assertGreater(state["ind_5m"]["atr"], 0.0)

        # Avanzar vela por vela
        step_c = feed.next_step()
        self.assertIsNotNone(step_c)
        self.assertEqual(feed.cursor, 201)


class TestSimLNMExchange(unittest.TestCase):

    def setUp(self):
        self.exchange = SimLNMExchange(
            initial_balance_sats=100000,
            taker_fee_rate=0.0010,
            funding_rate_8h=0.0001,
            default_slippage_bps=0.0,  # 0 bps para probar matemática exacta
        )

    def test_open_trade_and_fees(self):
        # Abrir posición Long con margen 10,000 y leverage 10x
        # Nocional = 10,000 * 10 = 100,000 sats
        # Fee apertura = 100,000 * 0.0010 = 100 sats
        trade = self.exchange.open_trade(
            side="b", margin=10000, leverage=10, price=70000.0, now=1000.0
        )
        self.assertEqual(trade["side"], "b")
        self.assertEqual(trade["margin"], 10000)
        self.assertEqual(trade["notional_sats"], 100000)
        self.assertEqual(trade["entry_fee_sats"], 100)
        self.assertEqual(self.exchange.balance_sats, 99900)  # 100,000 - 100 fee
        self.assertEqual(self.exchange.total_locked_margin_sats, 10000)
        self.assertEqual(self.exchange.available_balance_sats, 89900)

        # Precio de liquidación a 10x: 70000 * (1 - 0.99 / 10) = 70000 * 0.901 = 63070.0
        self.assertAlmostEqual(trade["liquidation"], 63070.0, places=1)

    def test_close_trade_pnl_math(self):
        trade = self.exchange.open_trade(
            side="b", margin=10000, leverage=10, price=70000.0, now=1000.0
        )
        tid = trade["id"]

        # Cerrar con subida de precio a 71,400 (+2.0%)
        # PnL Bruto = 100,000 * (71,400 - 70,000) / 70,000 = 100,000 * 0.02 = 2,000 sats
        # Fee salida = 100 sats
        # Balance inicial: 99,900 + (2,000 - 100) = 101,800 sats
        # Ganancia neta ciclo: 2,000 - 100 (exit) - 100 (entry) = +1,800 sats
        ok, closed = self.exchange.close_trade(tid, price=71400.0, now=1050.0)
        self.assertTrue(ok)
        self.assertEqual(closed["gross_pnl_sats"], 2000)
        self.assertEqual(closed["exit_fee_sats"], 100)
        self.assertEqual(closed["net_pnl_sats"], 1800)
        self.assertEqual(self.exchange.balance_sats, 101800)
        self.assertEqual(len(self.exchange.running_trades), 0)

    def test_short_trade_pnl_math(self):
        trade = self.exchange.open_trade(
            side="s", margin=5000, leverage=20, price=70000.0, now=1000.0
        )
        tid = trade["id"]
        # Nocional = 5000 * 20 = 100,000 sats. Entry fee = 100 sats.
        self.assertEqual(self.exchange.balance_sats, 99900)

        # Precio cae a 68,600 (-2.0%) -> Ganancia para Short
        # PnL Bruto = 100,000 * (70,000 - 68,600) / 70,000 = 2,000 sats
        # Exit fee = 100 sats
        # Net = +1,800 sats
        ok, closed = self.exchange.close_trade(tid, price=68600.0, now=1050.0)
        self.assertTrue(ok)
        self.assertEqual(closed["gross_pnl_sats"], 2000)
        self.assertEqual(closed["net_pnl_sats"], 1800)
        self.assertEqual(self.exchange.balance_sats, 101800)

    def test_stop_loss_trigger_with_gap_risk(self):
        trade = self.exchange.open_trade(
            side="b", margin=10000, leverage=10, price=70000.0, stoploss=69000.0, now=1000.0
        )
        tid = trade["id"]

        # Vela con gap a la baja: Open en 68,500 (por debajo de SL=69000)
        c = Candle(
            timestamp=1060.0,
            open=68500.0,
            high=68600.0,
            low=68200.0,
            close=68300.0,
        )
        events = self.exchange.step_candle(c)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["close_reason"], "STOP_LOSS")
        # El precio de ejecución debe ser 68,500 debido al Gap Risk (no el ideal 69,000)
        self.assertEqual(events[0]["exit_price"], 68500.0)

    def test_liquidation_trigger(self):
        trade = self.exchange.open_trade(
            side="b", margin=10000, leverage=10, price=70000.0, now=1000.0
        )
        # Liquidación a 63,070
        c = Candle(
            timestamp=1060.0,
            open=65000.0,
            high=65100.0,
            low=62900.0,  # Toca liquidación
            close=63000.0,
        )
        events = self.exchange.step_candle(c)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["close_reason"], "LIQUIDATION")
        # Pérdida de todo el margen (10,000) + entry fee ya pagado (100) -> balance final = 89,900
        self.assertEqual(self.exchange.balance_sats, 89900)

    def test_funding_fee_accrual(self):
        trade = self.exchange.open_trade(
            side="b", margin=10000, leverage=10, price=70000.0, now=1000.0
        )
        # Simular paso de vela 28,801 segundos después (1 bloque de 8h cruzado)
        # Nocional = 100,000 sats. Funding 8h = 0.0001 -> Carry = 10 sats
        c = Candle(
            timestamp=1000.0 + 28801.0,
            open=70000.0,
            high=70100.0,
            low=69900.0,
            close=70000.0,
        )
        self.exchange.step_candle(c)
        self.assertEqual(self.exchange.total_funding_paid_sats, 10)
        # Balance = 100,000 - 100 (entry fee) - 10 (funding) = 99,890 sats
        self.assertEqual(self.exchange.balance_sats, 99890)


if __name__ == "__main__":
    unittest.main()
