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
from simulation.sim_lnm_exchange import MicrostructureStressConfig, SimLNMExchange


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

    def test_conservative_limit_fill_and_same_bar_sl(self):
        """Orden limite solo llena al cruzar >= $0.5 y aplica SL pesimista si la misma vela rompe SL."""
        o = self.exchange.open_limit_trade(
            side="b", margin=10000, leverage=10, price=69500.0, stoploss=69000.0, takeprofit=70500.0, now=1000.0
        )
        self.assertEqual(self.exchange.available_balance_sats, 90000)

        # Toque exacto a 69500.0 sin atravesar $0.5 -> NO llena
        self.exchange.step_candle(Candle(timestamp=1060.0, open=69600.0, high=69650.0, low=69500.0, close=69580.0))
        self.assertEqual(len(self.exchange.running_trades), 0)
        self.assertEqual(len(self.exchange.open_orders), 1)

        # Cruza a 69499.5 -> llena exactamente a 69500.0 sin slippage
        self.exchange.step_candle(Candle(timestamp=1120.0, open=69580.0, high=69620.0, low=69499.5, close=69590.0))
        self.assertEqual(len(self.exchange.open_orders), 0)
        self.assertIn(o["id"], self.exchange.running_trades)
        self.assertEqual(self.exchange.running_trades[o["id"]]["entry_price"], 69500.0)

        # Caso pesimista: vela que llena SHORT a 69700 y rompe SL 69850 en la misma barra -> cierra en STOP_LOSS
        o2 = self.exchange.open_limit_trade(
            side="s", margin=5000, leverage=10, price=69700.0, stoploss=69850.0, takeprofit=69100.0, now=1120.0
        )
        evs = self.exchange.step_candle(Candle(timestamp=1180.0, open=69600.0, high=69900.0, low=69590.0, close=69880.0))
        self.assertTrue(any(e["id"] == o2["id"] and e["close_reason"] == "STOP_LOSS" for e in evs))




class TestPhase5MicrostructureStress(unittest.TestCase):
    def test_phase5_stress_formula_and_presets(self):
        s0 = MicrostructureStressConfig.from_preset("S0")
        s1 = MicrostructureStressConfig.from_preset("S1")
        s2 = MicrostructureStressConfig.from_preset("S2")
        self.assertFalse(s0.enabled)
        self.assertEqual(s0.compute_slip_ratio(70000.0, 500.0, 0.01), 0.0)

        # Con ATR/P = 0.006 (> theta=0.004) y rango 1m = 0.01:
        px = 100000.0
        atr = 600.0
        rng = 0.01
        exp_s1 = 0.0002 + 0.15 * 0.002 + 0.25 * (1.5 / 60.0) ** 0.5 * 0.01
        exp_s2 = 0.0005 + 0.25 * 0.002 + 0.25 * (3.0 / 60.0) ** 0.5 * 0.01
        got_s1 = s1.compute_slip_ratio(px, atr, rng)
        got_s2 = s2.compute_slip_ratio(px, atr, rng)
        self.assertAlmostEqual(got_s1, exp_s1, places=10)
        self.assertAlmostEqual(got_s2, exp_s2, places=10)
        self.assertLess(got_s1, got_s2)

        # Debajo de theta_vol no agrega componente vol_term
        self.assertAlmostEqual(s1.compute_slip_ratio(px, 200.0, 0.0), 0.0002, places=10)

    def test_phase5_adverse_entry_and_sl_execution_long_and_short(self):
        from simulation.sim_market_feed import Candle

        s1 = MicrostructureStressConfig.s1_termux_realistic()
        ex_s0 = SimLNMExchange(initial_balance_sats=100000, inverse_contract_math=True)
        ex_s1 = SimLNMExchange(initial_balance_sats=100000, inverse_contract_math=True, stress_config=s1)

        warm = Candle(timestamp=1700000000.0, open=70000.0, high=70700.0, low=70000.0, close=70000.0, volume=10.0)
        ex_s0.step_candle(warm)
        ex_s1.step_candle(warm, atr_5m=420.0)

        t0_l = ex_s0.open_trade("b", margin=5000, leverage=10, stoploss=69300.0, price=70000.0, atr=420.0, now=1700000000.0)
        t1_l = ex_s1.open_trade("b", margin=5000, leverage=10, stoploss=69300.0, price=70000.0, atr=420.0, now=1700000000.0)
        self.assertGreater(t1_l["entry_price"], t0_l["entry_price"])
        self.assertEqual(ex_s1.stress_entry_events, 1)

        sl_bar = Candle(timestamp=1700000060.0, open=69900.0, high=70000.0, low=69000.0, close=69200.0, volume=20.0)
        ev0 = ex_s0.step_candle(sl_bar)
        ev1 = ex_s1.step_candle(sl_bar, atr_5m=420.0)
        self.assertEqual(len(ev0), 1)
        self.assertEqual(len(ev1), 1)
        self.assertEqual(ev0[0]["exit_price"], 69300.0)
        self.assertLess(ev1[0]["exit_price"], ev0[0]["exit_price"])
        self.assertLess(ev1[0]["net_pnl_sats"], ev0[0]["net_pnl_sats"])
        self.assertEqual(ex_s1.stress_sl_events, 1)

        # SHORT: entrada más baja y SL más alto (ambos adversos)
        t0_s = ex_s0.open_trade("s", margin=5000, leverage=10, stoploss=70000.0, price=69200.0, atr=420.0, now=1700000060.0)
        t1_s = ex_s1.open_trade("s", margin=5000, leverage=10, stoploss=70000.0, price=69200.0, atr=420.0, now=1700000060.0)
        self.assertLess(t1_s["entry_price"], t0_s["entry_price"])
        sl_bar_s = Candle(timestamp=1700000120.0, open=69300.0, high=70300.0, low=69200.0, close=70100.0, volume=20.0)
        ev0_s = ex_s0.step_candle(sl_bar_s)
        ev1_s = ex_s1.step_candle(sl_bar_s, atr_5m=420.0)
        self.assertGreater(ev1_s[0]["exit_price"], ev0_s[0]["exit_price"])
        self.assertLess(ev1_s[0]["net_pnl_sats"], ev0_s[0]["net_pnl_sats"])

    def test_phase5_stress_config_persists_across_env_reset(self):
        import tempfile
        from simulation.sim_market_feed import Candle
        from simulation.sim_training_environment import SimTrainingEnvironment

        s2 = MicrostructureStressConfig.s2_vol_spike()
        with tempfile.TemporaryDirectory() as td:
            env = SimTrainingEnvironment(
                candidate_brain_file=f"{td}/b.json",
                candidate_hedge_file=f"{td}/h.json",
                initial_balance_sats=100000,
                stress_config=s2,
            )
            self.assertIs(env.exchange.stress_config, s2)
            self.assertTrue(env.exchange.inverse_contract_math)
            candles = [
                Candle(
                    timestamp=1700000000.0 + i * 60.0,
                    open=70000.0,
                    high=70050.0,
                    low=69950.0,
                    close=70000.0,
                    volume=5.0,
                )
                for i in range(90)
            ]
            env.reset(candles, initial_balance_sats=100000, warmup_bars=80)
            self.assertIs(env.exchange.stress_config, s2)
            self.assertTrue(env.exchange.inverse_contract_math)


class TestPhase5Bv2LaneIsolation(unittest.TestCase):
    def test_no_contagion_exact_lane_a_vs_lane_b_and_global_dd(self):
        from simulation.lane_budget import LaneBudgetConfig, LaneBudgetController

        ctrl = LaneBudgetController(
            initial_equity_sats=100_000,
            config=LaneBudgetConfig(enabled=True),
        )
        # Inyectar -1.50% en B y -2.10% en A => DD_global = 3.60% > 3.50%
        ctrl.record_lane_b_pnl(-1500.0, now_ts=1000.0, side="b")
        ctrl.record_lane_a_pnl(-2100.0)

        self.assertAlmostEqual(ctrl.dd_b, 0.0150, places=6)
        self.assertAlmostEqual(ctrl.dd_a, 0.0210, places=6)
        self.assertAlmostEqual(ctrl.dd_global, 0.0360, places=6)
        self.assertFalse(ctrl.lane_a_recovery_mode)

        dec_b = ctrl.evaluate_lane_b_entry(
            side="s",
            now_ts=1005.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=96_400.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_b.allowed)
        self.assertTrue(dec_b.lane_b_paused)
        self.assertEqual(dec_b.reason, "LANE_B_PAUSED_24H")
        self.assertFalse(dec_b.lane_a_recovery_mode)

        # Carril A solo entra en recovery_mode cuando dd_a >= 3.50% y sale cuando dd_a <= 2.50%
        ctrl.record_lane_a_pnl(-1400.0)
        self.assertAlmostEqual(ctrl.dd_a, 0.0350, places=6)
        self.assertTrue(ctrl.lane_a_recovery_mode)
        ctrl.record_lane_a_pnl(+1000.0)
        self.assertAlmostEqual(ctrl.dd_a, 0.0250, places=6)
        self.assertFalse(ctrl.lane_a_recovery_mode)

    def test_lane_b_kill_switch_soft_cut_streak_protective_brake_margin_and_sl_risk(self):
        from simulation.lane_budget import (
            LaneBudgetConfig,
            LaneBudgetController,
            compute_inverse_loss_at_sl_sats,
            compute_inverse_net_pnl_sats,
        )

        # Validar matemática inversa exacta
        pnl = compute_inverse_net_pnl_sats(
            side="b",
            margin_sats=1000.0,
            leverage=10.0,
            entry_price=50000.0,
            exit_price=50000.0,
            fee_rate_open=0.0008,
            fee_rate_close=0.0008,
            funding_sats=0.0,
        )
        self.assertAlmostEqual(pnl["gross_pnl_sats"], 0.0, places=9)
        self.assertAlmostEqual(pnl["opening_fee_sats"], 8.0, places=9)
        self.assertAlmostEqual(pnl["closing_fee_sats"], 8.0, places=9)
        self.assertAlmostEqual(pnl["net_pnl_sats"], -16.0, places=9)

        loss_sl = compute_inverse_loss_at_sl_sats(
            margin_sats=1000.0,
            leverage=10.0,
            entry_price=50000.0,
            sl_price=49250.0,
            fee_rate_open=0.0008,
            fee_rate_close=0.0008,
        )
        exp_l_sl = abs(50000.0 - 49250.0) / 49250.0 + 0.0008 + (50000.0 / 49250.0) * 0.0008
        self.assertAlmostEqual(loss_sl, 10000.0 * exp_l_sl, places=6)

        # 1. Recorte 0.5x en DD_B >= 0.80%
        ctrl = LaneBudgetController(100_000, LaneBudgetConfig(enabled=True))
        ctrl.record_lane_b_pnl(-800.0, now_ts=1000.0, side="b")
        dec_soft = ctrl.evaluate_lane_b_entry(
            side="s",
            now_ts=1005.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=99_200.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertTrue(dec_soft.allowed)
        self.assertAlmostEqual(dec_soft.risk_multiplier, 0.50, places=6)

        # 2. Pausa 24h tras 2 pérdidas seguidas por lado en B
        ctrl_streak = LaneBudgetController(100_000, LaneBudgetConfig(enabled=True))
        ctrl_streak.record_lane_b_trade_close("b", -300.0, now_ts=1000.0)
        ctrl_streak.record_lane_b_trade_close("b", -300.0, now_ts=1100.0)
        dec_b_paused = ctrl_streak.evaluate_lane_b_entry(
            side="b",
            now_ts=1200.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=99_400.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_b_paused.allowed)
        self.assertTrue(dec_b_paused.lane_b_paused)
        self.assertEqual(dec_b_paused.reason, "LANE_B_PAUSED_24H")

        # Lado contrario ("s") sigue permitido y "b" se libera tras 24h
        dec_s_ok = ctrl_streak.evaluate_lane_b_entry(
            side="s",
            now_ts=1200.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=99_400.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertTrue(dec_s_ok.allowed)

        dec_b_unpaused = ctrl_streak.evaluate_lane_b_entry(
            side="b",
            now_ts=1100.0 + 86_400.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=99_400.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertTrue(dec_b_unpaused.allowed)

        # 3. Freno protector si DD_A >= 2.00% o DD_global >= 2.20%
        ctrl_prot_a = LaneBudgetController(100_000, LaneBudgetConfig(enabled=True))
        ctrl_prot_a.record_lane_a_pnl(-2000.0)
        dec_pa = ctrl_prot_a.evaluate_lane_b_entry(
            side="b",
            now_ts=1000.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=98_000.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_pa.allowed)
        self.assertEqual(dec_pa.reason, "LANE_B_PROTECTIVE_DD_BRAKE")

        ctrl_prot_g = LaneBudgetController(100_000, LaneBudgetConfig(enabled=True))
        ctrl_prot_g.record_lane_a_pnl(-1500.0)
        ctrl_prot_g.record_lane_b_pnl(-700.0, now_ts=1000.0)
        dec_pg = ctrl_prot_g.evaluate_lane_b_entry(
            side="b",
            now_ts=1005.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=97_800.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_pg.allowed)
        self.assertEqual(dec_pg.reason, "LANE_B_PROTECTIVE_DD_BRAKE")

        # 4. Tope de 15% margen y cota sum(LossAtSL_open_B) + LossAtSL_new <= (0.0150 - DD_B) * E_global
        ctrl_caps = LaneBudgetController(100_000, LaneBudgetConfig(enabled=True))
        ctrl_caps.record_lane_b_pnl(-500.0, now_ts=1000.0)  # dd_b=0.005, e_global=99_500 => cap SL=995.0
        dec_mcap = ctrl_caps.evaluate_lane_b_entry(
            side="b",
            now_ts=1005.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=100_000.0,
            open_margin_b_sats=10_000.0,
            margin_new_sats=5_001.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_mcap.allowed)
        self.assertEqual(dec_mcap.reason, "LANE_B_MARGIN_CAP_15PCT")

        dec_slcap = ctrl_caps.evaluate_lane_b_entry(
            side="b",
            now_ts=1005.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=100_000.0,
            open_margin_b_sats=1_000.0,
            margin_new_sats=1_000.0,
            open_loss_at_sl_b_sats=600.0,
            loss_at_sl_new_sats=400.0,
        )
        self.assertFalse(dec_slcap.allowed)
        self.assertEqual(dec_slcap.reason, "LANE_B_OPEN_SL_RISK_CAP")

        dec_ok = ctrl_caps.evaluate_lane_b_entry(
            side="b",
            now_ts=1005.0,
            n_open_a=0,
            n_open_b_buy=0,
            n_open_b_sell=0,
            avail_balance_sats=100_000.0,
            open_margin_b_sats=1_000.0,
            margin_new_sats=1_000.0,
            open_loss_at_sl_b_sats=600.0,
            loss_at_sl_new_sats=395.0,
        )
        self.assertTrue(dec_ok.allowed)

    def test_asymmetric_slots_lane_a_priority_and_total_cap(self):
        from simulation.lane_budget import LaneBudgetConfig, LaneBudgetController

        ctrl = LaneBudgetController(100_000, LaneBudgetConfig(enabled=True))
        common = dict(
            now_ts=1000.0,
            avail_balance_sats=100_000.0,
            open_margin_b_sats=0.0,
            margin_new_sats=1000.0,
            open_loss_at_sl_b_sats=0.0,
            loss_at_sl_new_sats=100.0,
        )

        # N_A=3, N_B=0 => B permitido (3 <= 3, y quedan 4 >= 3 slots para A)
        self.assertTrue(ctrl.evaluate_lane_b_entry(side="b", n_open_a=3, n_open_b_buy=0, n_open_b_sell=0, **common).allowed)

        # N_A=4, N_B=0 => B rechazado (N_A + N_B = 4 > 3)
        dec_na4 = ctrl.evaluate_lane_b_entry(side="b", n_open_a=4, n_open_b_buy=0, n_open_b_sell=0, **common)
        self.assertFalse(dec_na4.allowed)
        self.assertEqual(dec_na4.reason, "LANE_B_SLOT_CAP")

        # Máximo 1 por lado en B, máximo 2 total en B
        dec_same_side = ctrl.evaluate_lane_b_entry(side="b", n_open_a=1, n_open_b_buy=1, n_open_b_sell=0, **common)
        self.assertFalse(dec_same_side.allowed)
        self.assertEqual(dec_same_side.reason, "LANE_B_SLOT_CAP")

        dec_other_side = ctrl.evaluate_lane_b_entry(side="s", n_open_a=2, n_open_b_buy=1, n_open_b_sell=0, **common)
        self.assertTrue(dec_other_side.allowed)

        dec_b_full = ctrl.evaluate_lane_b_entry(side="b", n_open_a=0, n_open_b_buy=1, n_open_b_sell=1, **common)
        self.assertFalse(dec_b_full.allowed)
        self.assertEqual(dec_b_full.reason, "LANE_B_SLOT_CAP")

        # Carril A puede llenar los 3 slots reservados aunque B tenga 2 abiertos (total 5)
        self.assertTrue(ctrl.evaluate_lane_a_entry(n_open_a=2, n_open_b=2).allowed)
        self.assertFalse(ctrl.evaluate_lane_a_entry(n_open_a=3, n_open_b=2).allowed)

    def test_phase5b_v2_lab_cli_help_offline(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "simulation/run_phase5b_v2_lab.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        for flag in (
            "--scope",
            "--scenarios",
            "--fee-rt-bps",
            "--mode",
            "--sl-floors",
            "--cooldown-hours",
            "--short-exp-anchor",
            "--json-out",
            "--cache-dir",
        ):
            self.assertIn(flag, proc.stdout)


if __name__ == "__main__":
    unittest.main()
