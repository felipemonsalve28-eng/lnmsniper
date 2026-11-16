#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_sniper_cli.py
Suite de Certificación Automatizada para el CLI de Control LNM Sniper v7.3+.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from guardian_execution_core import AtomicBrainStore
from learning_thresholds_manager import DEFAULT_CONFIG_PATH, LearningThresholdsManager
from sniper_cli import (
    SniperControlCenter,
    _parse_sats_input,
    _safe_input,
    build_full_parameter_catalog,
)
from sniper_cli_catalog import PARAM_OPERATIONAL_GUIDE, RUNTIME_PARAMETERS_SPEC


class TestSniperCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="sniper_cli_test_")
        self.cfg_dir = os.path.join(self.tmp_dir, "config")
        os.makedirs(self.cfg_dir, exist_ok=True)
        self.thr_path = os.path.join(self.cfg_dir, "learning_thresholds.json")
        self.rt_path = os.path.join(self.cfg_dir, "cli_runtime_overrides.json")
        shutil.copy2(DEFAULT_CONFIG_PATH, self.thr_path)

        self.brain_path = os.path.join(self.tmp_dir, "q_brain_advanced_lnm.json")
        self.store = AtomicBrainStore(self.brain_path)
        self.store.save({
            "env": "mainnet",
            "reward_schema_version": 2,
            "epsilon": 0.0,
            "total_trained": 10,
            "current_target": 300000,
            "locked_floor": 75000,
            "peak_equity": 100000,
            "q_table": {"RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS": [0.0, 0.05, 0.04, 0.06, 0.03, 0.02, 0.01, 0.02, 0.01]},
            "state_visits": {"RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS": 12},
            "active_trades": {},
        })
        self.ctrl = SniperControlCenter(
            thresholds_path=self.thr_path,
            runtime_path=self.rt_path,
            base_dir=self.tmp_dir,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_full_catalog_explanations_and_operational_impact(self) -> None:
        catalog = build_full_parameter_catalog(self.ctrl)
        with open(self.thr_path, "r", encoding="utf-8") as fh:
            raw_thr = json.load(fh)
        total_json_params = sum(len(v) for v in raw_thr.values())
        self.assertEqual(total_json_params, 78)
        self.assertEqual(len(catalog), total_json_params + len(RUNTIME_PARAMETERS_SPEC))
        for key, item in catalog.items():
            self.assertTrue(len(str(item["description"]).strip()) > 10)
            self.assertTrue(len(str(item["math_role"]).strip()) > 10)
            self.assertTrue(len(str(item["impact_up"]).strip()) > 10)
            self.assertTrue(len(str(item["impact_down"]).strip()) > 10)
            if item["scope"] == "HOT_RELOAD":
                self.assertIn(key, PARAM_OPERATIONAL_GUIDE)

    def test_02_atomic_threshold_set_and_range_validation(self) -> None:
        ok, msg, meta = self.ctrl.set_threshold_parameter("expectancy.min_expectancy_ratio", 4.25)
        self.assertTrue(ok, msg)
        self.assertAlmostEqual(meta["new_value"], 4.25)
        mgr = LearningThresholdsManager(config_path=self.thr_path)
        self.assertAlmostEqual(mgr.get_float("expectancy", "min_expectancy_ratio"), 4.25)

        ok_bad, _, _ = self.ctrl.set_threshold_parameter("expectancy.min_expectancy_ratio", 25.0)
        self.assertFalse(ok_bad)
        mgr.check_and_reload(force=True)
        self.assertAlmostEqual(mgr.get_float("expectancy", "min_expectancy_ratio"), 4.25)

    def test_03_structural_lock_protection_when_trades_open(self) -> None:
        tel_path = os.path.join(self.tmp_dir, "telemetry_live.json")
        with open(tel_path, "w", encoding="utf-8") as fh:
            json.dump({"active_trades": 2, "balance_sats": 100000, "equity_sats": 100000, "rl_metrics": {}}, fh)

        ok_locked, msg_locked, _ = self.ctrl.set_threshold_parameter("exploration.gamma_discount", 0.90)
        self.assertFalse(ok_locked)
        self.assertIn("BLOQUEO ESTRUCTURAL ACTIVO", msg_locked)

        ok_dyn, _, _ = self.ctrl.set_threshold_parameter("mfe_mae.trailing_tighten_factor", 0.80)
        self.assertTrue(ok_dyn)

        with open(tel_path, "w", encoding="utf-8") as fh:
            json.dump({"active_trades": 0, "balance_sats": 100000, "equity_sats": 100000, "rl_metrics": {}}, fh)
        ok_unlocked, _, _ = self.ctrl.set_threshold_parameter("exploration.gamma_discount", 0.90)
        self.assertTrue(ok_unlocked)

    def test_04_all_five_presets_validation_and_mainnet_guard(self) -> None:
        for p_name in ("baseline_v73", "capital_fortress", "fee_sniper_anti_chop", "trend_expansion_kelly"):
            ok, msg, _ = self.ctrl.apply_preset(p_name, env_mode="mainnet")
            self.assertTrue(ok, msg)
            mgr = LearningThresholdsManager(config_path=self.thr_path)
            self.assertEqual(mgr.reload_rejected_count, 0)

        ok_test_main, msg_block, _ = self.ctrl.apply_preset("testnet_curriculum_lab", env_mode="mainnet")
        self.assertFalse(ok_test_main)
        self.assertIn("BLOQUEO DE SEGURIDAD P0", msg_block)

        ok_test_env, _, _ = self.ctrl.apply_preset("testnet_curriculum_lab", env_mode="testnet")
        self.assertTrue(ok_test_env)

    def test_05_runtime_p0_guards_and_brain_sha256_integrity(self) -> None:
        ok_cap, _, _ = self.ctrl.set_runtime_parameter("MAX_CONCURRENT_TRADES", 8)
        self.assertFalse(ok_cap)

        ok_p0, msg_p0, _ = self.ctrl.set_runtime_parameter("INITIAL_FLOOR_RATIO", 0.50)
        self.assertFalse(ok_p0)
        self.assertIn("PROTECCIÓN P0 INVIOLABLE", msg_p0)

        ok_t, msg_t = self.ctrl.update_brain_target_sats(450000, env_mode="mainnet")
        self.assertTrue(ok_t, msg_t)
        sha_ok, loaded = self.store.verify_file(self.brain_path, strict_sha=True)
        self.assertTrue(sha_ok)
        self.assertEqual(loaded["current_target"], 450000)

    def test_06_usability_short_key_resolution_and_error_handling(self) -> None:
        ok_short, msg_short, meta_short = self.ctrl.set_threshold_parameter("trailing_tighten_factor", 0.82)
        self.assertTrue(ok_short, msg_short)
        self.assertIsNotNone(meta_short)
        self.assertEqual(meta_short["key"], "mfe_mae.trailing_tighten_factor")
        self.assertAlmostEqual(meta_short["new_value"], 0.82)

        ok_typo, msg_typo, _ = self.ctrl.set_threshold_parameter("trailing_tighten_factr", 0.82)
        self.assertFalse(ok_typo)
        self.assertIn("mfe_mae.trailing_tighten_factor", msg_typo)

        ok_bool, msg_bool, _ = self.ctrl.set_threshold_parameter("mfe_mae.trailing_tighten_factor", True)
        self.assertFalse(ok_bool)
        self.assertIn("booleano", msg_bool.lower())

        ok_rt, msg_rt, meta_rt = self.ctrl.set_runtime_parameter("max_leverage", 55)
        self.assertTrue(ok_rt, msg_rt)
        self.assertIsNotNone(meta_rt)
        self.assertEqual(meta_rt["key"], "MAX_LEVERAGE")
        self.assertEqual(meta_rt["new_value"], 55)

        self.assertEqual(_parse_sats_input("350,000"), 350000)
        self.assertEqual(_parse_sats_input("350k"), 350000)
        self.assertIsNone(_parse_sats_input(True))

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertIsNone(_safe_input("Prompt: "))
        with patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(_safe_input("Prompt: "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
