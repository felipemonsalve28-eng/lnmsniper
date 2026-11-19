#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

from limit_execution_manager import (
    LimitExecutionConfig,
    LimitExecutionManager,
    compute_limit_entry_plan,
    round_to_tick,
    should_route_limit,
)


class MockLimitClient:
    def __init__(self):
        self.open_orders = {}
        self.running_trades = []
        self.cancelled_ids = []
        self.next_id = 100
        self.simulate_fill_on_cancel_404 = False

    def open_limit_trade(self, side, margin, leverage, price, stoploss, takeprofit, client_id=""):
        self.next_id += 1
        oid = f"ord-{self.next_id}"
        rec = {
            "id": oid,
            "client_id": client_id,
            "side": side,
            "margin": margin,
            "leverage": leverage,
            "price": price,
            "stoploss": stoploss,
            "takeprofit": takeprofit,
        }
        self.open_orders[oid] = rec
        return rec

    def get_open_orders(self):
        return list(self.open_orders.values())

    def get_running_trades(self, force_refresh=False):
        return list(self.running_trades)

    def cancel_open_order(self, order_id):
        if self.simulate_fill_on_cancel_404 and order_id in self.open_orders:
            filled = self.open_orders.pop(order_id)
            self.running_trades.append(filled)
            raise RuntimeError("HTTP 404 (/futures/isolated/trade/cancel): trade already running")
        self.open_orders.pop(order_id, None)
        self.cancelled_ids.append(order_id)
        return {"cancelled": order_id}


class TestLimitExecutionManager(unittest.TestCase):
    def test_01_tick_rounding_and_non_crossing_guarantee(self):
        """Valida redondeo $0.5 pasivo y garantía estricta non-crossing."""
        self.assertEqual(round_to_tick(62445.79, "b", 0.5), 62445.5)
        self.assertEqual(round_to_tick(62445.21, "s", 0.5), 62445.5)

        cfg = LimitExecutionConfig(enabled=True)
        sig_bull = {"liquidity_score": 0.80, "swept_level": 62480.0, "wick_extreme": 62420.0, "timeframe": "15m"}
        plan_b = compute_limit_entry_plan("b", "LONG_SNIPER", 62500.0, 120.0, 62300.0, 62900.0, sig_bull, cfg)
        self.assertTrue(plan_b["valid"])
        self.assertLess(plan_b["limit_price"], 62500.0)
        self.assertEqual((plan_b["limit_price"] * 2) % 1, 0)
        self.assertEqual(plan_b["ttl_sec"], 150.0)

        sig_bear = {"liquidity_score": -0.80, "swept_level": 62520.0, "wick_extreme": 62580.0, "timeframe": "5m"}
        plan_s = compute_limit_entry_plan("s", "SHORT_REVERSION", 62500.0, 120.0, 62700.0, 62100.0, sig_bear, cfg)
        self.assertTrue(plan_s["valid"])
        self.assertGreater(plan_s["limit_price"], 62500.0)
        self.assertEqual((plan_s["limit_price"] * 2) % 1, 0)
        self.assertEqual(plan_s["ttl_sec"], 90.0)

    def test_02_routing_matrix_trend_vs_sniper_reversion(self):
        """TREND siempre va a MARKET; SNIPER/REVERSION con sweep alineado van a LIMIT."""
        cfg = LimitExecutionConfig(enabled=True)
        sig = {"liquidity_score": 0.75}
        use_tr, _ = should_route_limit(4, "LONG_TREND", "TREND_BULL", sig, 62500.0, 150.0, cfg=cfg)
        self.assertFalse(use_tr)

        use_sn, _ = should_route_limit(3, "LONG_SNIPER", "RANGE_NORMAL", sig, 62500.0, 150.0, cfg=cfg)
        self.assertTrue(use_sn)

        use_rev, _ = should_route_limit(2, "LONG_REVERSION", "RANGE_NORMAL", {"liquidity_score": 0.0}, 62500.0, 150.0, cfg=cfg)
        self.assertTrue(use_rev)

        use_off, _ = should_route_limit(3, "LONG_SNIPER", "RANGE_NORMAL", sig, 62500.0, 150.0, cfg=LimitExecutionConfig(enabled=False))
        self.assertFalse(use_off)

    def test_03_max_offset_guard_rejects_far_sweep(self):
        """Si spot ya se alejó > 0.75 ATR del nivel barrido, rechaza la orden límite muerta."""
        cfg = LimitExecutionConfig(enabled=True, max_entry_offset_atr=0.75)
        sig_far = {"liquidity_score": 0.85, "swept_level": 62300.0, "wick_extreme": 62240.0, "timeframe": "5m"}
        plan = compute_limit_entry_plan("b", "LONG_SNIPER", 62500.0, 100.0, 62200.0, 62900.0, sig_far, cfg)
        self.assertFalse(plan["valid"])
        self.assertIn("LIMIT_OFFSET_TOO_FAR", plan["reason"])

    def test_04_p0_concurrency_slot_cap_and_same_side_dedup(self):
        """Invariante P0: running + open <= 5 y máximo 1 orden pendiente por dirección."""
        with tempfile.TemporaryDirectory() as tmpd:
            mgr = LimitExecutionManager("mainnet", base_dir=tmpd, max_concurrent_trades=5)
            cli = MockLimitClient()
            cfg = LimitExecutionConfig(enabled=True)
            plan = compute_limit_entry_plan("b", "LONG_REVERSION", 62500.0, 100.0, 62300.0, 62900.0, None, cfg)

            # Con 4 running -> puede colocar 1 open (total 5)
            ok1, _, rec1 = mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_REVERSION", "action": 2}, running_count=4, atr_5m=100.0, now=1000.0)
            self.assertTrue(ok1)
            self.assertEqual(mgr.effective_slots_used(4), 5)
            self.assertEqual(mgr.reserved_margin_sats(), 1000)

            # Con 4 running + 1 open = 5 -> bloqueado por SLOTS_FULL incluso en dirección opuesta
            plan_s = compute_limit_entry_plan("s", "SHORT_REVERSION", 62500.0, 100.0, 62700.0, 62100.0, None, cfg)
            ok2, r2, _ = mgr.submit_limit_order(cli, "s", 1000, 20.0, plan_s, {"style": "SHORT_REVERSION", "action": 6}, running_count=4, atr_5m=100.0, now=1005.0)
            self.assertFalse(ok2)
            self.assertIn("SLOTS_FULL", r2)

            # Mismo lado bloqueado incluso con slots libres
            ok3, r3, _ = mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_REVERSION", "action": 2}, running_count=1, atr_5m=100.0, now=1006.0)
            self.assertFalse(ok3)
            self.assertIn("OPEN_LIMIT_SAME_SIDE_EXISTS", r3)

    def test_05_ttl_runaway_and_wick_invalidation_cancellation(self):
        """Valida cancelación temprana por TTL, Runaway (>= 55% TP) e invalidación de mecha."""
        cfg = LimitExecutionConfig(enabled=True, ttl_sec_5m=90.0, runaway_tp_frac=0.55)
        with tempfile.TemporaryDirectory() as tmpd:
            # A. TTL
            mgr = LimitExecutionManager("testnet", base_dir=tmpd)
            cli = MockLimitClient()
            plan = {"limit_price": 62480.0, "stoploss": 62300.0, "takeprofit": 62880.0, "invalidation_price": 62400.0, "ttl_sec": 90.0}
            mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_SNIPER", "action": 3}, 0, 100.0, now=1000.0)
            rep_ttl = mgr.reconcile_open_orders(cli, [], spot_price=62510.0, cfg=cfg, now=1095.0)
            self.assertEqual(len(rep_ttl["cancelled"]), 1)
            self.assertIn("TTL_EXPIRED", rep_ttl["cancelled"][0]["reason"])

            # B. Runaway (TP span = 400 -> 55% = 220 -> spot 62710 dispara runaway)
            mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_SNIPER", "action": 3}, 0, 100.0, now=2000.0)
            rep_run = mgr.reconcile_open_orders(cli, [], spot_price=62710.0, cfg=cfg, now=2020.0)
            self.assertEqual(len(rep_run["cancelled"]), 1)
            self.assertIn("RUNAWAY_GUARD", rep_run["cancelled"][0]["reason"])

            # C. Invalidación de mecha (spot < 62400)
            mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_SNIPER", "action": 3}, 0, 100.0, now=3000.0)
            rep_inv = mgr.reconcile_open_orders(cli, [], spot_price=62390.0, cfg=cfg, now=3010.0)
            self.assertEqual(len(rep_inv["cancelled"]), 1)
            self.assertIn("WICK_INVALIDATED", rep_inv["cancelled"][0]["reason"])

    def test_06_cancel_vs_fill_race_404_recovers_running_trade(self):
        """Si la orden se llena milisegundos antes del cancel (404), la reconciliación la registra como FILLED."""
        cfg = LimitExecutionConfig(enabled=True)
        with tempfile.TemporaryDirectory() as tmpd:
            mgr = LimitExecutionManager("mainnet", base_dir=tmpd)
            cli = MockLimitClient()
            cli.simulate_fill_on_cancel_404 = True
            plan = {"limit_price": 62480.0, "stoploss": 62300.0, "takeprofit": 62880.0, "invalidation_price": 62400.0, "ttl_sec": 90.0}
            mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_SNIPER", "action": 3}, 0, 100.0, now=1000.0)

            rep = mgr.reconcile_open_orders(cli, [], spot_price=62500.0, cfg=cfg, now=1100.0)
            self.assertEqual(len(rep["filled"]), 1)
            self.assertEqual(len(rep["cancelled"]), 0)
            self.assertEqual(mgr.active_open_count(), 0)
            self.assertEqual(mgr.stats["filled"], 1)

    def test_07_atomic_fcntl_persistence_across_restart_and_shutdown_cleanup(self):
        """Tras reinicio del demonio, la orden pendiente se recarga intacta desde disco y SIGTERM la cancela limpia."""
        with tempfile.TemporaryDirectory() as tmpd:
            cli = MockLimitClient()
            plan = {"limit_price": 62480.0, "stoploss": 62300.0, "takeprofit": 62880.0, "invalidation_price": 62400.0, "ttl_sec": 120.0}
            mgr1 = LimitExecutionManager("mainnet", base_dir=tmpd)
            ok, _, rec = mgr1.submit_limit_order(cli, "b", 1200, 18.0, plan, {"style": "LONG_SNIPER", "action": 3}, 0, 100.0, now=5000.0)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(mgr1.filepath))

            # Simular reinicio de proceso con nueva instancia
            mgr2 = LimitExecutionManager("mainnet", base_dir=tmpd)
            self.assertEqual(mgr2.active_open_count(), 1)
            self.assertEqual(mgr2.reserved_margin_sats(), 1200)

            # Simular SIGTERM -> cancelación total
            rep_sd = mgr2.cancel_all_for_shutdown(cli)
            self.assertEqual(len(rep_sd["cancelled"]), 1)
            self.assertEqual(mgr2.active_open_count(), 0)
            self.assertEqual(len(cli.open_orders), 0)

    def test_08_ovr_flip_cancels_resting_limit(self):
        """Un giro violento de OVR en dirección contraria cancela inmediatamente la orden límite en reposo."""
        cfg = LimitExecutionConfig(enabled=True)
        with tempfile.TemporaryDirectory() as tmpd:
            mgr = LimitExecutionManager("testnet", base_dir=tmpd)
            cli = MockLimitClient()
            plan = {"limit_price": 62480.0, "stoploss": 62300.0, "takeprofit": 62880.0, "invalidation_price": 62400.0, "ttl_sec": 120.0}
            mgr.submit_limit_order(cli, "b", 1000, 20.0, plan, {"style": "LONG_SNIPER", "action": 3}, 0, 100.0, now=1000.0)
            rep = mgr.reconcile_open_orders(cli, [], spot_price=62500.0, perception={"override_side": "BEAR"}, cfg=cfg, now=1015.0)
            self.assertEqual(len(rep["cancelled"]), 1)
            self.assertIn("OVR_FLIP", rep["cancelled"][0]["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
