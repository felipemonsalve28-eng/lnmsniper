#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_candidate_certification.py
Suite unitaria de certificación para CandidateBrainCertifier.
Valida el rechazo de esquemas corruptos, métricas Monte Carlo y la seguridad de la promoción atómica.
"""
import sys
import os
import json
import unittest
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from simulation.certify_and_promote_candidate import CandidateBrainCertifier
from guardian_execution_core import AtomicBrainStore


class TestCandidateCertification(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.valid_brain_file = os.path.join(self.temp_dir, "q_brain_candidate_valid.json")
        self.invalid_brain_file = os.path.join(self.temp_dir, "q_brain_candidate_invalid.json")

        # Construir un cerebro candidato válido Schema v2
        regimes = ["RANGE_NORMAL", "RANGE_SQUEEZE", "TREND_BULL", "TREND_BEAR", "VOLATILITY_EXPANSION", "CHOP_HIGHVOL"]
        oscillators = ["OVERSOLD", "DIP", "MID", "PEAK", "OVERBOUGHT"]
        expansions = ["LOW_EXP", "MID_EXP", "HIGH_EXP"]

        q_table = {}
        for r in regimes:
            for o in oscillators:
                for e in expansions:
                    for m in ("CHOP", "BULL"):
                        k = f"{r}|{o}|{e}|{m}|EMA_TRANS"
                        q_table[k] = [0.0, 0.05, -0.02, 0.08, 0.01, -0.05, 0.02, -0.08, -0.01]

        valid_payload = {
            "env": "candidate",
            "reward_schema_version": 2,
            "epsilon": 0.20,
            "q_table": q_table,
            "state_visits": {k: 5 for k in q_table.keys()},
            "style_metrics": {
                "TREND": {"trades": 50, "wins": 35, "net_pnl_sats": 50000, "fees_sats": 5000},
                "REVERSION": {"trades": 20, "wins": 12, "net_pnl_sats": 10000, "fees_sats": 2000},
                "SNIPER": {"trades": 10, "wins": 6, "net_pnl_sats": 5000, "fees_sats": 1000},
                "DEF_SCALP": {"trades": 10, "wins": 6, "net_pnl_sats": 5000, "fees_sats": 1000},
            },
        }
        with open(self.valid_brain_file, "w", encoding="utf-8") as f:
            json.dump(valid_payload, f)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_validate_schema_valid_candidate(self):
        ok, issues = CandidateBrainCertifier.validate_schema(self.valid_brain_file)
        self.assertTrue(ok, f"Debe aceptar candidato válido pero reportó: {issues}")
        self.assertEqual(len(issues), 0)

    def test_validate_schema_rejects_corrupted_actions(self):
        # Crear un cerebro con una acción corrupta (NaN o Q(s,0) != 0.0)
        with open(self.valid_brain_file, "r") as f:
            bad_data = json.load(f)
        first_key = list(bad_data["q_table"].keys())[0]
        bad_data["q_table"][first_key][0] = 0.50  # Violación del axioma Q(s,0)=0.0

        with open(self.invalid_brain_file, "w") as f:
            json.dump(bad_data, f)

        ok, issues = CandidateBrainCertifier.validate_schema(self.invalid_brain_file)
        self.assertFalse(ok)
        self.assertTrue(any("Axioma violado" in msg for msg in issues))

    def test_validate_schema_rejects_legacy_schema(self):
        with open(self.valid_brain_file, "r") as f:
            bad_data = json.load(f)
        bad_data["reward_schema_version"] = 1  # Schema v1 obsoleto

        with open(self.invalid_brain_file, "w") as f:
            json.dump(bad_data, f)

        ok, issues = CandidateBrainCertifier.validate_schema(self.invalid_brain_file)
        self.assertFalse(ok)
        self.assertTrue(any("reward_schema_version" in msg for msg in issues))

    def test_quantitative_scenarios_check(self):
        ok, details = CandidateBrainCertifier.run_quantitative_scenarios_check(self.valid_brain_file)
        self.assertTrue(ok)
        self.assertEqual(details["status"], "PASS")
        self.assertTrue(details["action_0_neutral"])
        self.assertGreaterEqual(details["states_loaded"], 100)

    def test_dry_run_promotion(self):
        ok, msg, data = CandidateBrainCertifier.promote(
            candidate_brain_path=self.valid_brain_file,
            target_env="testnet",
            num_monte_carlo_paths=5,
            dry_run=True,
        )
        self.assertTrue(ok)
        self.assertIn("DRY-RUN", msg)

    def test_monte_carlo_zero_mutation_guarantee(self):
        import hashlib
        with open(self.valid_brain_file, "rb") as f:
            sha_before = hashlib.sha256(f.read()).hexdigest()

        ok, metrics = CandidateBrainCertifier.run_monte_carlo_stress_test(
            brain_path=self.valid_brain_file,
            num_paths=3,
            bars_per_path=100,
        )
        with open(self.valid_brain_file, "rb") as f:
            sha_after = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(sha_before, sha_after, "El estrés Monte Carlo NUNCA debe modificar el archivo evaluado.")
        self.assertEqual(metrics.get("mutations_detected", 0), 0)


if __name__ == "__main__":
    unittest.main()
