#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_c1_streak3_24h_production.py
Suite de certificación unitaria, persistencia POSIX fcntl + SHA-256 y paridad Fase 3
para la arquitectura de producción C1_STREAK3_24H_INV.
"""
import copy
import io
import os
import pickle
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from guardian_execution_core import AtomicBrainStore
from lnm_sniper_v6_advanced import (
    AutonomousGoalBrain,
    Config,
    evaluate_and_dispatch_entries,
    execute_strategy_guardian,
    process_settlement_queue,
)
from side_streak_brake import SideStreakBrakeStore
from simulation.sim_market_feed import Candle
from simulation.sim_training_environment import SimTrainingEnvironment
from sniper_perception_sats_core import SatsExpectancyGate


class _DummyTelemetry:
    def record(self, metric_name: str, val: float) -> None:
        pass

    def evaluate_execution_health(self):
        return False, "OK"


class _DummyOrderGuard:
    def can_send_order(self, side, margin, lev, running_trades=None, now=None):
        return True, "OK"

    def register_intent(self, side, margin, lev, state_key="", now=None):
        return "fp-1"

    def clear_intent(self, fp):
        pass


class _MockProdClient:
    def __init__(self, balance: int = 100000, closed_trades=None):
        self.balance = int(balance)
        self.closed_trades = list(closed_trades or [])
        self.running_trades = []
        self.opened_orders = []
        self.closed_ids = []
        self.telemetry = _DummyTelemetry()
        self.order_guard = _DummyOrderGuard()

    def get_user(self, force_refresh: bool = False):
        return {"balance": self.balance}

    def get_closed_trades(self, limit: int = 50):
        return list(self.closed_trades)

    def get_running_trades(self, force_refresh: bool = False):
        return list(self.running_trades)

    def open_trade(self, side, margin, lev, sl, tp):
        tid = f"mock-open-{len(self.opened_orders) + 1}"
        rec = {
            "id": tid,
            "side": side,
            "margin": int(margin),
            "leverage": float(lev),
            "stoploss": float(sl),
            "takeprofit": float(tp),
            "price": 70000.0,
            "pl": 0,
        }
        self.opened_orders.append(rec)
        self.running_trades.append(rec)
        return rec

    def close_trade(self, tid):
        self.closed_ids.append(str(tid))
        return {"id": str(tid), "closed": True}

    def update_stoploss(self, tid, new_sl):
        return {"id": str(tid), "stoploss": new_sl}


class TestC1Streak324HProduction(unittest.TestCase):
    def setUp(self) -> None:
        Config.configure_environment("mainnet")
        self.tmp_dir = tempfile.mkdtemp(prefix="c1_streak3_24h_test_")

    def tearDown(self) -> None:
        Config.configure_environment("mainnet")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_lcb_penalty_bypass_and_symmetric_seed_selection(self) -> None:
        """Con DETERMINISTIC_TREND_POLICY=True y visits=0, choose_action omite lcb_penalty y es simétrico."""
        self.assertTrue(Config.DETERMINISTIC_TREND_POLICY)
        b_path = os.path.join(self.tmp_dir, "sym_brain.json")
        brain = AutonomousGoalBrain(filename=b_path, explorer_mode=False, learning_enabled=False)

        ind_dummy = {
            "price": 70000.0,
            "atr": 250.0,
            "rsi": 50.0,
            "bb_pos": 0.5,
            "ema9": 70000.0,
            "ema21": 70000.0,
            "ema_score": 0.0,
            "regime": "RANGE_NORMAL",
        }
        st_bull = "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL"
        st_bear = "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR"
        st_mix = "TREND_BEAR|DIP|HIGH_EXP|BULL|FULL_BEAR"
        st_rng = "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS"

        q_bull = brain.get_state_q_values(st_bull)
        q_bear = brain.get_state_q_values(st_bear)
        q_mix = brain.get_state_q_values(st_mix)

        # Espejo exacto entre estados puros BULL y BEAR
        self.assertEqual(q_bull[1:5], q_bear[5:9])
        self.assertEqual(q_bull[5:9], q_bear[1:5])
        # Estado mixto conflictivo no tiene sesgo BULL > BEAR
        self.assertEqual(q_mix[1:5], q_mix[5:9])
        self.assertEqual(brain.state_visits.get(st_bull, 0), 0)
        self.assertEqual(brain.state_visits.get(st_bear, 0), 0)

        act_bull = brain.choose_action(st_bull, ind_dummy, ind_dummy, ind_dummy, ind_dummy)
        act_bear = brain.choose_action(st_bear, ind_dummy, ind_dummy, ind_dummy, ind_dummy)
        act_mix = brain.choose_action(st_mix, ind_dummy, ind_dummy, ind_dummy, ind_dummy)
        act_rng = brain.choose_action(st_rng, ind_dummy, ind_dummy, ind_dummy, ind_dummy)

        self.assertEqual(act_bull, 1, "Estado TREND_BULL con visits=0 debe elegir L_TREND (1) sin bloqueo LCB")
        self.assertEqual(act_bear, 5, "Estado TREND_BEAR con visits=0 debe elegir S_TREND (5) sin bloqueo LCB")
        self.assertEqual(act_mix, 0, "Empate inter-lado en estado mixto debe resolver a HOLD (0)")
        self.assertEqual(act_rng, 0, "Empate inter-lado en estado neutral debe resolver a HOLD (0)")

    def test_02_q_table_immutability_in_live_deterministic_settlement(self) -> None:
        """Bajo DETERMINISTIC_TREND_POLICY=True, la liquidación de trades nunca muta q_table ni state_visits."""
        b_path = os.path.join(self.tmp_dir, "frozen_brain.json")
        brain = AutonomousGoalBrain(filename=b_path, explorer_mode=False, learning_enabled=False)
        st_key = "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL"
        _ = brain.get_state_q_values(st_key)
        q_snapshot_before = copy.deepcopy(brain.q_table)
        visits_before = copy.deepcopy(brain.state_visits)

        now_ts = 1710000000.0
        brain.active_trades["t-loss-1"] = {
            "action": 5,
            "style": "TREND",
            "side": "sell",
            "margin": 2000,
            "leverage": 20.0,
            "state": st_key,
            "entry_time": now_ts - 300.0,
            "entry_q": 0.065,
        }
        cli = _MockProdClient(
            balance=99500,
            closed_trades=[
                {
                    "id": "t-loss-1",
                    "side": "sell",
                    "pl": -420,
                    "margin": 2000,
                    "leverage": 20.0,
                    "opening_fee": 40,
                    "closing_fee": 40,
                    "sum_carry_fees": 0,
                    "price": 70000.0,
                }
            ],
        )
        sb = SideStreakBrakeStore(
            env_mode="mainnet",
            filepath=os.path.join(self.tmp_dir, "sb_settle.json"),
            persist_to_disk=True,
        )
        with redirect_stdout(io.StringIO()):
            settled, _, _, _ = process_settlement_queue(
                cli,
                brain,
                ["t-loss-1"],
                st_key,
                now_ts,
                None,
                "NORMAL",
                0,
                1.0,
                0.0,
                side_brake=sb,
            )
        self.assertEqual(settled, 1)
        self.assertEqual(brain.q_table, q_snapshot_before, "q_table debe permanecer 100% inmutable en modo determinista")
        self.assertEqual(brain.state_visits, visits_before, "state_visits no debe mutar en modo determinista")
        self.assertEqual(sb.side_loss_streak["s"], 1)
        self.assertEqual(sb.side_loss_streak["b"], 0)
        self.assertLess(brain.realized_net_profit_sats, 0)

    def test_03_c1_short_exp_phys_gate_and_recovery_deterministic_hold(self) -> None:
        """Verifica OTR=0 en recovery_mode y bloqueo de S_TREND cuando d_SL <= 0.6505% (C1_SHORT_EXP_PHYS)."""
        st_bull = "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL"
        st_bear = "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR"
        b_path = os.path.join(self.tmp_dir, "c1_gate_brain.json")
        brain = AutonomousGoalBrain(filename=b_path, explorer_mode=False, learning_enabled=False)

        # 3a. En recovery_mode con OVR activo, filter_and_rank_action jamás remapea 1->3 ni 5->7
        q_bull = brain.get_state_q_values(st_bull)
        act_rec, reason_rec = SatsExpectancyGate.filter_and_rank_action(
            1,
            st_bull,
            q_bull,
            brain.strategies,
            {"override_side": "BULL", "market_regime": "TREND_BULL"},
            recovery_mode=True,
            current_dd_pct=0.038,
            explorer_mode=False,
            deterministic_trend_policy=True,
        )
        self.assertEqual(act_rec, 0)
        self.assertEqual(reason_rec, "RECOVERY_DETERMINISTIC_HOLD")

        # 3b. S_TREND con volatilidad baja (2*ATR <= 0.65% -> d_SL pegado al piso 0.65%) se bloquea a HOLD (0)
        cli = _MockProdClient(balance=100000)
        sb = SideStreakBrakeStore(env_mode="mainnet", persist_to_disk=False)
        ind_calm = {
            "price": 70000.0,
            "atr": 140.0,  # 2*ATR / price = 280 / 70000 = 0.0040 <= 0.006505 (floored at 0.0065)
            "atr_pct": 0.0020,
            "rsi": 42.0,
            "bb_pos": 0.35,
            "ema9": 69800.0,
            "ema21": 70100.0,
            "ema_score": -0.8,
            "regime": "TREND_BEAR",
            "market_regime": "TREND_BEAR",
        }
        mods = brain.get_parallel_modifiers(ind_calm, ind_calm, ind_calm, ind_calm)
        perception_bear = {
            "override_side": None,
            "market_regime": "TREND_BEAR",
            "vel_ema_atr": -0.25,
            "rsi_5m": 42.0,
        }
        buf_calm = io.StringIO()
        with redirect_stdout(buf_calm):
            act_calm, _ = evaluate_and_dispatch_entries(
                client=cli,
                brain=brain,
                state=st_bear,
                ind_5m=ind_calm,
                ind_15m=ind_calm,
                ind_1h=ind_calm,
                ind_4h=ind_calm,
                mods=mods,
                recovery_mode=False,
                current_dd_pct=0.0,
                dynamic_max_pos=5,
                active_count=0,
                locked_margin=0,
                equity=100000,
                balance=100000,
                last_entry_time=0.0,
                now=1710001000.0,
                consecutive_losses=0,
                warmup=False,
                risk_multiplier=1.0,
                sprint_mode=False,
                mkt_reg_tag="TREND_BEAR",
                still_running=[],
                safe_mode=True,
                hedge_store=None,
                perception=perception_bear,
                side_brake=sb,
            )
        self.assertEqual(act_calm, 0)
        self.assertIn("BLOCKED_SHORT_NO_EXPANSION", buf_calm.getvalue())

        # 3c. S_TREND con expansión orgánica (2*ATR > 0.6505%, ej. ATR=260 -> d_SL=0.7429%) pasa el filtro C1
        ind_exp = dict(ind_calm, atr=260.0, atr_pct=0.00371)
        buf_exp = io.StringIO()
        with redirect_stdout(buf_exp):
            evaluate_and_dispatch_entries(
                client=cli,
                brain=brain,
                state=st_bear,
                ind_5m=ind_exp,
                ind_15m=ind_exp,
                ind_1h=ind_exp,
                ind_4h=ind_exp,
                mods=mods,
                recovery_mode=False,
                current_dd_pct=0.0,
                dynamic_max_pos=5,
                active_count=0,
                locked_margin=0,
                equity=100000,
                balance=100000,
                last_entry_time=0.0,
                now=1710002000.0,
                consecutive_losses=0,
                warmup=False,
                risk_multiplier=1.0,
                sprint_mode=False,
                mkt_reg_tag="TREND_BEAR",
                still_running=[],
                safe_mode=True,
                hedge_store=None,
                perception=perception_bear,
                side_brake=sb,
            )
        self.assertNotIn("BLOCKED_SHORT_NO_EXPANSION", buf_exp.getvalue())
        self.assertIn("SHORT_TREND", buf_exp.getvalue())

    def test_04_side_streak_brake_24h_pause_independence_and_restart_survival(self) -> None:
        """Verifica freno C1_STREAK3_24H por lado, independencia L/S, expiración 24h y persistencia fcntl + SHA-256."""
        sb_file = os.path.join(self.tmp_dir, "side_brake_state_mainnet.json")
        sb1 = SideStreakBrakeStore(env_mode="mainnet", filepath=sb_file, persist_to_disk=True)
        t0 = 1715000000.0

        # 2 pérdidas en SHORT aún no pausan
        r1 = sb1.record_trade_close("sell", -150, close_ts=t0)
        r2 = sb1.record_trade_close("sell", -180, close_ts=t0 + 60.0)
        self.assertFalse(r1["is_paused"])
        self.assertFalse(r2["is_paused"])
        self.assertEqual(sb1.side_loss_streak["s"], 2)

        # 3ra pérdida consecutiva en SHORT dispara pausa de 24h (86400s) solo en SHORT
        r3 = sb1.record_trade_close("sell", -210, close_ts=t0 + 120.0)
        self.assertTrue(r3["triggered_pause"])
        self.assertTrue(r3["is_paused"])
        self.assertEqual(sb1.side_loss_streak["s"], 0)
        self.assertEqual(r3["pause_until_ts"], t0 + 120.0 + 86400.0)

        # LONG permanece 100% libre
        paused_b, rem_b = sb1.is_side_paused("buy", now=t0 + 200.0)
        paused_s, rem_s = sb1.is_side_paused("sell", now=t0 + 200.0)
        self.assertFalse(paused_b)
        self.assertEqual(rem_b, 0.0)
        self.assertTrue(paused_s)
        self.assertAlmostEqual(rem_s, 86320.0, places=1)

        # Simular reinicio del demonio recargando desde disco y verificando SHA-256
        ok_sha, digest = sb1.verify_disk_integrity()
        self.assertTrue(ok_sha, f"Fallo integridad SHA-256: {digest}")
        self.assertTrue(os.path.exists(f"{sb_file}.sha256"))

        sb2 = SideStreakBrakeStore(env_mode="mainnet", filepath=sb_file, persist_to_disk=True)
        self.assertTrue(sb2.sha256_valid)
        paused_s_reload, rem_s_reload = sb2.is_side_paused("sell", now=t0 + 3720.0)
        self.assertTrue(paused_s_reload)
        self.assertAlmostEqual(rem_s_reload, 82800.0, places=1)
        self.assertEqual(sb2.total_pauses_triggered["s"], 1)

        # Al cumplirse las 24h (+86400s), la pausa expira automáticamente en O(1)
        paused_after_24h, rem_after_24h = sb2.is_side_paused("sell", now=t0 + 120.0 + 86400.0)
        self.assertFalse(paused_after_24h)
        self.assertEqual(rem_after_24h, 0.0)

        # Un trade ganador reinicia racha activa en LONG
        sb2.record_trade_close("buy", -100, close_ts=t0 + 90000.0)
        sb2.record_trade_close("buy", -120, close_ts=t0 + 90100.0)
        self.assertEqual(sb2.side_loss_streak["b"], 2)
        sb2.record_trade_close("buy", +350, close_ts=t0 + 90200.0)
        self.assertEqual(sb2.side_loss_streak["b"], 0)

        # Prueba de corrupción en disco: detecta discrepancia SHA-256 y recupera desde .bak sin caída
        with open(sb_file, "w", encoding="utf-8") as bad_fh:
            bad_fh.write("{corrupted_json")
        ok_corrupt, _ = sb2.verify_disk_integrity()
        self.assertFalse(ok_corrupt)
        with redirect_stdout(io.StringIO()):
            sb3 = SideStreakBrakeStore(env_mode="mainnet", filepath=sb_file, persist_to_disk=True)
        self.assertTrue(sb3.sha256_corrupted_detected)
        self.assertTrue(sb3.sha256_valid)
        self.assertEqual(sb3.total_pauses_triggered["s"], 1)

    def test_05_e2_fe_off_and_p0_invariants(self) -> None:
        """Verifica invariantes P0 y desactivación de FORCED_EDGE_EXIT en TREND (E2_FE_OFF)."""
        Config.configure_environment("mainnet")
        self.assertFalse(Config.ENABLE_HYBRID_HEDGE)
        self.assertTrue(Config.DETERMINISTIC_TREND_POLICY)
        self.assertTrue(Config.FE_OFF_TREND)
        self.assertEqual(Config.MAX_CONCURRENT_TRADES, 5)
        self.assertAlmostEqual(Config.BASE_RISK_PCT_STD, 0.0075)
        self.assertAlmostEqual(Config.TREND_SL_FLOOR_PCT, 0.0065)
        self.assertAlmostEqual(Config.SHORT_EXP_MIN_DSL, 0.006505)
        self.assertAlmostEqual(Config.RECOVERY_ENTER_DD, 0.035)
        self.assertAlmostEqual(Config.DD_THROTTLE_HALT_PCT, 0.045)
        self.assertAlmostEqual(Config.MAX_DD_GUARD_PCT, 0.052)

        b_path = os.path.join(self.tmp_dir, "fe_off_brain.json")
        brain = AutonomousGoalBrain(filename=b_path, explorer_mode=False, learning_enabled=False)
        cli = _MockProdClient(balance=100000)
        now_ts = time.time()
        trend_trade = {
            "id": "trend-fe-test",
            "side": "buy",
            "margin": 3000,
            "leverage": 20.0,
            "price": 78000.0,
            "stoploss": 77493.0,
            "liquidation": 74500.0,
            "pl": -350,
        }
        trend_meta = {
            "action": 1,
            "style": "TREND",
            "state": "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL",
            "entry_time": now_ts - 300.0,
            "edge_deg_streak": 8,
        }
        ind_5m = {
            "price": 77650.0,
            "atr": 200.0,
            "atr_pct": 0.0025,
            "regime": "TREND_BULL",
            "rsi": 45.0,
            "bb_pos": 0.40,
            "_now_ts": now_ts,
        }
        ind_15m = {"ema9": 77700.0, "ema21": 77800.0, "rsi": 46.0}
        closed = execute_strategy_guardian(
            cli,
            brain,
            trend_trade,
            ind_5m,
            ind_15m,
            trend_meta,
            "NORMAL",
            edge_score=-0.95,
            perception={"vel_ema_atr": -0.5},
            current_dd_pct=0.01,
            is_hedged=False,
        )
        self.assertFalse(closed, "E2_FE_OFF debe impedir FORCED_EDGE_EXIT en posiciones TREND (actions 1, 5)")
        self.assertEqual(len(cli.closed_ids), 0)

    def test_06_m16_bull_inverse_contract_math_parity(self) -> None:
        """Verifica paridad de SimTrainingEnvironment en inverse_contract_math=True y M16 BULL (+31,746 sats)."""
        env = SimTrainingEnvironment(
            candidate_brain_file=os.path.join(self.tmp_dir, "sim_inv_brain.json"),
            candidate_hedge_file=os.path.join(self.tmp_dir, "sim_inv_hedge.json"),
            initial_balance_sats=100000,
            explorer_mode=False,
            learning_enabled=False,
        )
        self.assertTrue(env.exchange.inverse_contract_math)
        self.assertTrue(env.deterministic_trend_policy)
        self.assertTrue(env.fe_off_trend)
        self.assertTrue(env.short_exp_phys_hold)
        self.assertTrue(env.streak3_24h)

        # Verificar que reset() preserve inverse_contract_math=True
        c_list = [
            Candle(timestamp=1700000000.0 + i * 60.0, open=70000.0, high=70050.0, low=69950.0, close=70010.0)
            for i in range(100)
        ]
        env.reset(c_list, warmup_bars=80)
        self.assertTrue(env.exchange.inverse_contract_math)

        # Si el caché durable v1 está disponible en WSL, certificar paridad exacta de M16 BULL (oos_m15.pkl = +31,746 sats)
        cache_dir = "/home/akapana/.cache/sniper_wf_s2_cache"
        m16_pkl = os.path.join(cache_dir, "oos_m15.pkl")
        seed_brain = os.path.join(cache_dir, "q_brain_s2_clean_seed.json")
        scratch_dir = "/mnt/c/Users/mberm/.gemini/antigravity/brain/70db3e2b-886d-4e76-8424-78984ef03027/scratch"
        if os.path.exists(m16_pkl) and os.path.exists(seed_brain) and os.path.isdir(scratch_dir):
            import sys
            if scratch_dir not in sys.path:
                sys.path.insert(1, scratch_dir)
            import ablation_fase3_certification_v1 as f3
            from lnm_sniper_v6_advanced import THRESHOLDS_MANAGER
            from liquidity_radar import LiquidityConfig
            from limit_execution_manager import LimitExecutionConfig

            if f3.aod.BASE_LIQ is None:
                f3.aod.BASE_LIQ = LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
            if f3.aod.BASE_LIM is None:
                f3.aod.BASE_LIM = LimitExecutionConfig.from_thresholds(THRESHOLDS_MANAGER)

            blk_meta = {
                "split": "OOS",
                "key": "oos_m15",
                "parent_month": "M16",
                "label": "M16",
                "regime": "BULL",
                "ret_pct": 20.20,
                "ts0": 0.0,
                "ts1": 0.0,
                "seed_off": 15,
                "pkl_path": m16_pkl,
                "purge_min_first_ts": None,
            }
            res_m16 = f3._run_block_fase3(
                blk_meta=blk_meta,
                brain_src=seed_brain,
                inverse_contract_math=True,
                side_brake_mode="streak3_24h",
            )
            self.assertTrue(res_m16.get("ok"), f"Fallo en _run_block_fase3: {res_m16.get('error')}")
            self.assertEqual(res_m16["net_pnl"], 31746, f"Paridad M16 BULL INV esperada +31,746, obtuvo {res_m16['net_pnl']}")
            self.assertEqual(res_m16["trades"], 52)
            self.assertTrue(all(int(d["action"]) in (1, 5) for d in res_m16["details"]))
            self.assertEqual(res_m16["fe_n"], 0)
            self.assertEqual(res_m16["ddg_n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
