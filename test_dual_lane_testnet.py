# -*- coding: utf-8 -*-
"""
test_dual_lane_testnet.py
Suite rápida en RAM (< 1s) para el cableado Testnet 48h Máquina Dual (Carril A + Carril B).
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import lnm_sniper_v6_advanced as main_bot
from dual_lane_testnet_runtime import DualLaneTestnetRuntime


def _make_sweep_candles_15m(spot_price=70050.0):
    """Genera velas 15m cerradas con swing low en 70000 y sweep/reclaim alcista limpio."""
    n = 40
    h = np.full(n, 70120.0, dtype=np.float64)
    l = np.full(n, 70040.0, dtype=np.float64)
    c = np.full(n, 70080.0, dtype=np.float64)
    # Dos swing lows limpios en índices 15 y 25 (k=2) alrededor de 70000
    for idx in (15, 25):
        l[idx - 2 : idx + 3] = [70030.0, 70015.0, 70000.0, 70015.0, 70035.0]
        h[idx - 2 : idx + 3] = [70090.0, 70085.0, 70080.0, 70090.0, 70100.0]
        c[idx - 2 : idx + 3] = [70060.0, 70040.0, 70030.0, 70050.0, 70070.0]
    # Vela cerrada más reciente barre 70000 hasta 69900 y recupera arriba de 70000
    h[-1] = 70060.0
    l[-1] = 69900.0
    c[-1] = spot_price
    v = np.full(n, 100.0, dtype=np.float64)
    v[-1] = 320.0
    return (h, l, c), v


class _MockMTFCache:
    def __init__(self, *, spot_price=70050.0, macro_regime="RANGE", closed_1m_ts=1_700_000_060.0):
        self.spot_price = float(spot_price)
        self.macro_regime = str(macro_regime)
        self.closed_1m_ts = closed_1m_ts
        d15, v15 = _make_sweep_candles_15m(self.spot_price)
        self.cache = {
            "1m": {"ts": np.array([closed_1m_ts, closed_1m_ts + 60.0], dtype=np.float64)},
            "5m": {"data": d15, "vol": v15, "basis_ratio": 1.0},
            "15m": {"data": d15, "vol": v15, "basis_ratio": 1.0},
        }

    def get_closed_1m_ts(self):
        return self.closed_1m_ts

    def get_indicators_fast(self, tf, current_lnm_price=0.0):
        if tf in ("1h", "4h"):
            is_bull = "BULL" in self.macro_regime
            return {
                "price": self.spot_price,
                "atr": 400.0 if tf == "1h" else 800.0,
                "regime": "TREND_BULL" if is_bull else "RANGE",
                "market_regime": "TREND_BULL" if is_bull else "RANGE_NORMAL",
                "ema_score": 1.5 if is_bull else 0.0,
                "ema9": 71200.0 if is_bull else 70020.0,
                "ema21": 70000.0,
                "adx": 32.0 if is_bull else 14.0,
            }
        return {
            "price": self.spot_price,
            "atr": 180.0 if tf == "5m" else 280.0,
            "market_regime": "RANGE_NORMAL",
        }


class _MockClient:
    def __init__(self):
        self.placed_limits = []
        self.cancelled_orders = []
        self.closed_requests = []
        self.closed_trades_feed = []
        self._seq = 0

    def open_limit_trade(self, side, margin, leverage, price, stoploss, takeprofit, client_id=""):
        self._seq += 1
        oid = f"ord_b_{self._seq}"
        rec = {
            "id": oid,
            "side": "buy" if side == "b" else "sell",
            "margin": int(margin),
            "leverage": int(leverage),
            "price": float(price),
            "stoploss": float(stoploss),
            "takeprofit": float(takeprofit),
            "clientId": client_id,
        }
        self.placed_limits.append(rec)
        return rec

    def cancel_open_order(self, trade_id):
        self.cancelled_orders.append(str(trade_id))
        return {"id": str(trade_id), "cancelled": True}

    def close_trade(self, trade_id):
        self.closed_requests.append(str(trade_id))
        return {"id": str(trade_id), "closed": True}

    def get_closed_trades(self, limit=50):
        return list(self.closed_trades_feed)

    def get_user(self, force_refresh=False):
        return {"balance": 100_000}


class _DummyBrain:
    def __init__(self):
        self.active_trades = {}
        self.dirty = False


class _DummyBrake:
    def __init__(self):
        self.calls = []
        self.max_consecutive_losses = 3

    def record_trade_close(self, side, net_pnl, close_ts=None):
        self.calls.append((side, net_pnl))
        return {"triggered_pause": False}


class TestDualLaneTestnet(unittest.TestCase):
    def _make_runtime(self, tmpdir, **kwargs):
        return DualLaneTestnetRuntime(
            env_mode="testnet",
            state_path=os.path.join(tmpdir, "lane_b_state_testnet.json"),
            micro_events_log_path=os.path.join(tmpdir, "binance_micro_events_testnet.jsonl"),
            initial_equity_sats=100_000.0,
            **kwargs,
        )

    def test_1_cli_guard_blocks_mainnet_and_preserves_default(self):
        self.assertFalse(main_bot._parse_cli_enable_lane_b_scalp(["lnm_sniper_v6_advanced.py"]))
        self.assertTrue(
            main_bot._parse_cli_enable_lane_b_scalp(
                ["lnm_sniper_v6_advanced.py", "--env", "testnet", "--enable-lane-b-scalp"]
            )
        )
        with self.assertRaises(SystemExit):
            main_bot._validate_lane_b_cli_guard("mainnet", True)
        main_bot._validate_lane_b_cli_guard("mainnet", False)
        main_bot._validate_lane_b_cli_guard("testnet", True)
        with self.assertRaises(ValueError):
            DualLaneTestnetRuntime(env_mode="mainnet")

    def test_2_strict_trade_isolation_and_global_concurrency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = self._make_runtime(tmpdir)
            rt.active_trades["trade_b_1"] = {"trade_id": "trade_b_1", "side": "b", "margin_sats": 3000}
            rt._remember_ids("trade_b_1", "lb_t_123")

            self.assertTrue(rt.is_lane_b_trade("trade_b_1"))
            self.assertTrue(rt.is_lane_b_trade({"id": "unknown_id", "clientId": "lb_t_999"}))
            self.assertFalse(rt.is_lane_b_trade({"id": "trade_a_1", "clientId": "m_123"}))

            # manage_open_positions_and_hedges filtra B y solo entrega A al guardián
            seen_by_guardian = []

            def _fake_guardian(_c, _b, _hs, _ps, primary, *_args, **_kw):
                seen_by_guardian.extend([str(x["id"]) for x in primary])
                return primary

            with patch.object(main_bot, "run_integrated_guardian_and_hedge_cycle", side_effect=_fake_guardian), \
                 patch.object(main_bot, "evaluate_open_trades_online"):
                still = main_bot.manage_open_positions_and_hedges(
                    client=_MockClient(),
                    brain=_DummyBrain(),
                    hedge_store=None,
                    portfolio_state={},
                    primary_running=[
                        {"id": "trade_a_1", "side": "buy"},
                        {"id": "trade_b_1", "side": "buy", "clientId": "lb_t_123"},
                    ],
                    ind_5m={}, ind_15m={}, ind_1h={}, state="RANGE",
                    current_mode="NORMAL", edge_scores_by_tid={},
                    perception={}, q_vec=[0.0] * 9, account_state={},
                    now=1_700_000_100.0, safe_mode=False, lane_b_runtime=rt,
                )
            self.assertEqual(seen_by_guardian, ["trade_a_1"])
            self.assertEqual([x["id"] for x in still], ["trade_a_1"])

            # process_settlement_queue ignora cierres de B (sin tocar brain ni SideStreakBrakeStore)
            brain = _DummyBrain()
            brain.active_trades["trade_b_1"] = {"side": "buy", "margin": 3000}
            brake = _DummyBrake()
            client = _MockClient()
            client.closed_trades_feed = [{"id": "trade_b_1", "pl": -500}]
            settled, _, _, _ = main_bot.process_settlement_queue(
                client, brain, ["trade_b_1"], "RANGE", 1_700_000_200.0,
                None, "NORMAL", 0, 1.0, 0.0, side_brake=brake, lane_b_runtime=rt,
            )
            self.assertEqual(settled, 0)
            self.assertEqual(brake.calls, [])
            self.assertIn("trade_b_1", brain.active_trades)

            # Conteo global MAX_CONCURRENT_TRADES=5 / Lane B cap N_A+N_B <= 3 bloquea entrada B
            mtf = _MockMTFCache()
            running_4_a = [{"id": f"a_{i}", "side": "buy", "markPrice": 70050.0} for i in range(4)]
            res = rt.step(
                now_ts=1_700_000_100.0, mtf_cache=mtf, client=client,
                running_trades=running_4_a, closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=90_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertIsNone(res["placed"])
            self.assertEqual(res["skip_reason"], "LANE_B_SLOT_CAP")

    def test_3_atomic_state_persistence_and_restart_recovery(self):
        default_rt = DualLaneTestnetRuntime(env_mode="testnet", state_path=None)
        self.assertNotIn("/sdcard", default_rt.state_path)
        self.assertTrue(default_rt.state_path.endswith(os.path.join(".sniper_state", "lane_b_state_testnet.json")))

        with tempfile.TemporaryDirectory() as tmpdir:
            sp = os.path.join(tmpdir, "lane_b_state_testnet.json")
            rt1 = DualLaneTestnetRuntime(env_mode="testnet", state_path=sp, initial_equity_sats=100_000.0)
            rt1.pending_orders["ord_b_1"] = {
                "order_id": "ord_b_1", "client_id": "lb_1", "side": "b",
                "margin_sats": 3500, "leverage": 25, "limit_price": 70010.0,
                "sl_price": 69590.0, "tp_price": 71026.0, "loss_at_sl_sats": 550.0,
                "created_at": 1_700_000_000.0,
            }
            rt1.budget.record_lane_b_trade_close("b", -600.0, 1_700_000_010.0)
            rt1.last_eval_1m_ts = 1_700_000_060.0
            self.assertTrue(rt1.save_state())

            rt2 = DualLaneTestnetRuntime(env_mode="testnet", state_path=sp)
            self.assertIn("ord_b_1", rt2.pending_orders)
            self.assertTrue(rt2.is_lane_b_trade("ord_b_1"))
            self.assertEqual(rt2.last_eval_1m_ts, 1_700_000_060.0)
            self.assertAlmostEqual(rt2.budget.e_b, 99_400.0)
            self.assertEqual(rt2.budget.loss_streak_b["b"], 1)

    def test_4_micro_flow_guard_shadow_only_and_fail_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = self._make_runtime(tmpdir)
            now = 1_700_000_100.0

            ok_snap = {
                "available": True, "connected": True, "stale": False, "age_sec": 0.8,
                "trades_1m": 120, "buy_vol_1m_usd": 450_000.0, "sell_vol_1m_usd": 400_000.0,
                "cvd_5m_usd": 1_000_000.0, "liq_buy_1m_usd": 5_000.0, "liq_sell_1m_usd": 5_000.0,
            }
            d_allow = rt.evaluate_micro_guard_shadow("b", ok_snap, now_ts=now)
            self.assertEqual(d_allow["decision"], "ALLOW")
            self.assertFalse(d_allow["shadow_only"])

            stale_snap = dict(ok_snap, stale=True, age_sec=9.5)
            d_stale = rt.evaluate_micro_guard_shadow("b", stale_snap, now_ts=now)
            self.assertEqual(d_stale["decision"], "ALLOW")
            self.assertTrue(d_stale["fail_open"])
            self.assertTrue(d_stale["reason"].startswith("FAIL_OPEN_"))

            d_none = rt.evaluate_micro_guard_shadow("b", None, now_ts=now)
            self.assertEqual(d_none["decision"], "ALLOW")
            self.assertTrue(d_none["fail_open"])

            veto_snap = {
                "available": True, "connected": True, "stale": False, "age_sec": 1.0,
                "trades_1m": 250, "buy_vol_1m_usd": 100_000.0, "sell_vol_1m_usd": 900_000.0,
                "cvd_5m_usd": -35_000_000.0, "liq_buy_1m_usd": 10_000.0, "liq_sell_1m_usd": 10_000.0,
            }
            d_veto = rt.evaluate_micro_guard_shadow("b", veto_snap, now_ts=now)
            self.assertEqual(d_veto["decision"], "VETO")
            self.assertFalse(d_veto["shadow_only"])

            # Con bloqueo activo (shadow_only=False), VETO bloquea LIMIT en Carril B y registra VETO_BLOCKED
            client = _MockClient()
            res = rt.step(
                now_ts=now, mtf_cache=_MockMTFCache(), client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot=veto_snap,
            )
            self.assertIsNone(res["placed"])
            self.assertEqual(len(client.placed_limits), 0)
            self.assertTrue(str(res.get("skip_reason") or "").startswith("MICRO_VETO:"))
            import json as _json
            with open(rt.micro_events_log_path, encoding="utf-8") as _fh:
                _evs = [_json.loads(ln) for ln in _fh if ln.strip()]
            self.assertTrue(any(e.get("event") == "VETO_BLOCKED" and e.get("lane") == "B" for e in _evs))

    def test_5_full_step_lifecycle_limit_cancel_fill_timestop_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rt = self._make_runtime(tmpdir)
            client = _MockClient()
            t0 = 1_700_000_100.0

            # a) Bloqueado en tendencia bajo range_only
            res_trend = rt.step(
                now_ts=t0, mtf_cache=_MockMTFCache(macro_regime="TREND_BULL"), client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertEqual(res_trend["skip_reason"], "MACRO_NOT_RANGE")
            self.assertEqual(len(client.placed_limits), 0)

            # b) Nuevo minuto en RANGE -> coloca LIMIT + edge-trigger impide duplicar mismo 1m
            mtf = _MockMTFCache(macro_regime="RANGE", closed_1m_ts=1_700_000_120.0)
            res_ok = rt.step(
                now_ts=t0 + 60.0, mtf_cache=mtf, client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertIsNotNone(res_ok["placed"])
            self.assertTrue(res_ok["placed"]["client_id"].startswith("lb_"))
            oid1 = res_ok["placed"]["order_id"]

            res_same = rt.step(
                now_ts=t0 + 65.0, mtf_cache=mtf, client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertEqual(res_same["skip_reason"], "SAME_1M_BAR")

            # c) Cancelación por TTL y por runaway >= 55% TP
            res_ttl = rt.step(
                now_ts=t0 + 700.0, mtf_cache=mtf, client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertIn(oid1, [c["order_id"] for c in res_ttl["cancelled"]])

            # Runaway cancel en nuevo minuto (después de cooldown 2700s)
            mtf2 = _MockMTFCache(macro_regime="RANGE", closed_1m_ts=1_700_000_180.0)
            res_ok2 = rt.step(
                now_ts=t0 + 2800.0, mtf_cache=mtf2, client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            placed2 = res_ok2["placed"]
            self.assertIsNotNone(placed2)
            runaway_px = placed2["limit_price"] + 0.65 * (placed2["takeprofit"] - placed2["limit_price"])
            mtf2.spot_price = runaway_px
            res_runaway = rt.step(
                now_ts=t0 + 2810.0, mtf_cache=mtf2, client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertTrue(any("RUNAWAY_TP" in c["reason"] for c in res_runaway["cancelled"]))

            # d) Nueva orden -> Fill detectado -> TIME_STOP a 16h -> Close con fees v3 -> JSONL
            mtf3 = _MockMTFCache(macro_regime="RANGE", closed_1m_ts=1_700_000_240.0)
            res_ok3 = rt.step(
                now_ts=t0 + 5600.0, mtf_cache=mtf3, client=client,
                running_trades=[], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=100_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            p3 = res_ok3["placed"]
            self.assertIsNotNone(p3)
            running_trade = {
                "id": "trade_b_live_3",
                "clientId": p3["client_id"],
                "side": "buy",
                "margin": p3["margin_sats"],
                "leverage": 25,
                "price": p3["limit_price"],
                "openingFee": 30,
            }
            fill_ts = t0 + 5610.0
            res_fill = rt.step(
                now_ts=fill_ts, mtf_cache=mtf3, client=client,
                running_trades=[running_trade], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=95_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertEqual(res_fill["filled"], ["trade_b_live_3"])

            # TIME_STOP a las 16.0h
            ts_16h = fill_ts + (16.0 * 3600.0) + 5.0
            res_ts = rt.step(
                now_ts=ts_16h, mtf_cache=mtf3, client=client,
                running_trades=[running_trade], closed_trades=[],
                equity_sats=100_000.0, available_margin_sats=95_000.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertEqual(res_ts["time_stop_requested"], ["trade_b_live_3"])
            self.assertIn("trade_b_live_3", client.closed_requests)

            # Cierre reportado en API v3
            client.closed_trades_feed = [{
                "id": "trade_b_live_3",
                "side": "buy",
                "margin": p3["margin_sats"],
                "leverage": 25,
                "entryPrice": p3["limit_price"],
                "pl": 500,
                "openingFee": 30,
                "closingFee": 32,
                "sumFundingFees": 8,
            }]
            res_close = rt.step(
                now_ts=ts_16h + 5.0, mtf_cache=mtf3, client=client,
                running_trades=[], closed_trades=client.closed_trades_feed,
                equity_sats=100_430.0, available_margin_sats=100_430.0,
                dd_a_pct=0.0, dd_global_pct=0.0, hard_floor_active=False,
                micro_snapshot={"available": False},
            )
            self.assertEqual(len(res_close["closed"]), 1)
            self.assertEqual(res_close["closed"][0]["net_pnl_sats"], 430)
            self.assertEqual(res_close["closed"][0]["exit_reason"], "TIME_STOP")
            self.assertAlmostEqual(rt.budget.e_b, 100_430.0)

            # Verificar eventos JSONL auditables
            with open(rt.micro_events_log_path, "r", encoding="utf-8") as fh:
                events = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual([e["event"] for e in events], ["OPEN", "CLOSE"])
            self.assertEqual(events[0]["lane"], "B")
            self.assertEqual(events[0]["exec_type"], "LIMIT")
            self.assertEqual(events[1]["opening_fee"], 30)
            self.assertEqual(events[1]["closing_fee"], 32)
            self.assertEqual(events[1]["funding"], 8)
            self.assertEqual(events[1]["net_pnl_sats"], 430)
            self.assertEqual(events[1]["exit_reason"], "TIME_STOP")


    def test_6_active_micro_guard_blocks_and_logs_veto(self):
        import json, os, tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from dual_lane_testnet_runtime import DualLaneTestnetRuntime

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"HOME": td}):
                rt = DualLaneTestnetRuntime(env_mode="testnet")
                rt.micro_events_log_path = os.path.join(td, "events.jsonl")
                self.assertFalse(rt.micro_guard_cfg.shadow_only)

                veto_dec = SimpleNamespace(
                    allowed=False, would_veto=True, fail_open=False,
                    reason="TEST_TOXIC_FLOW", rule_code="TOXIC_FLOW",
                )
                with patch("dual_lane_testnet_runtime.evaluate_micro_entry_guard", return_value=veto_dec):
                    ok_a, meta_a = rt.evaluate_micro_guard(
                        "A", "b", price=90000.0, micro_snapshot={"age_sec": 0.4}, now_ts=1700000000.0
                    )
                    self.assertFalse(ok_a)
                    self.assertTrue(meta_a["active"])
                    self.assertTrue(meta_a["blocked"])
                    self.assertEqual(meta_a["decision"], "VETO")

                fo_dec = SimpleNamespace(
                    allowed=True, would_veto=False, fail_open=True,
                    reason="SNAPSHOT_STALE", rule_code=None,
                )
                with patch("dual_lane_testnet_runtime.evaluate_micro_entry_guard", return_value=fo_dec):
                    ok_fo, meta_fo = rt.evaluate_micro_guard(
                        "B", "s", price=90000.0, micro_snapshot={"age_sec": 9.9}, now_ts=1700000005.0
                    )
                    self.assertTrue(ok_fo)
                    self.assertFalse(meta_fo["blocked"])
                    self.assertTrue(meta_fo["fail_open"])

                with open(rt.micro_events_log_path, encoding="utf-8") as fh:
                    lines = [ln for ln in fh.read().splitlines() if ln.strip()]
                self.assertEqual(len(lines), 1)
                ev = json.loads(lines[0])
                self.assertEqual(ev["event"], "VETO_BLOCKED")
                self.assertEqual(ev["lane"], "A")
                self.assertEqual(ev["rule_code"], "TOXIC_FLOW")

if __name__ == "__main__":
    unittest.main()
