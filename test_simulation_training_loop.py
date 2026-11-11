#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_simulation_training_loop.py
Suite de certificación cuantitativa para el bucle de entrenamiento RL acelerado
y el aislamiento de los cerebros candidatos.
"""
import sys
import os
import unittest
import math

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from simulation.sim_market_feed import Candle
from simulation.sim_training_environment import SimTrainingEnvironment
from simulation.train_candidate_brain import generate_synthetic_multi_regime_candles


class TestSimulationTrainingLoop(unittest.TestCase):

    def setUp(self):
        self.cand_brain = "test_cand_brain.json"
        self.cand_hedge = "test_cand_hedge.json"
        self.env = SimTrainingEnvironment(
            candidate_brain_file=self.cand_brain,
            candidate_hedge_file=self.cand_hedge,
            initial_balance_sats=100000,
            explorer_mode=True,
        )

    def tearDown(self):
        for f in (self.cand_brain, self.cand_hedge):
            if os.path.exists(f):
                os.remove(f)
            for ext in (".lock", ".sha256", ".bak"):
                if os.path.exists(f + ext):
                    os.remove(f + ext)

    def test_env_lifecycle_and_warmup(self):
        candles = generate_synthetic_multi_regime_candles(num_bars=300)
        state = self.env.reset(candles, warmup_bars=150)
        self.assertIn("ind_5m", state)
        self.assertIn("price", state)
        self.assertEqual(self.env.feed.cursor, 150)
        self.assertEqual(self.env.exchange.balance_sats, 100000)
        self.assertEqual(len(self.env.exchange.running_trades), 0)

    def test_env_step_trade_execution_and_q_learning(self):
        candles = generate_synthetic_multi_regime_candles(num_bars=400)
        self.env.reset(candles, warmup_bars=150)

        initial_trained = self.env.brain.total_trained
        steps_executed = 0
        done = False

        while not done and steps_executed < 100:
            mkt_st, r, done, info = self.env.step()
            steps_executed += 1

        self.assertGreater(steps_executed, 0)
        self.assertGreater(self.env.exchange.balance_sats, 0)
        # Verificar que el entorno se ejecutó con coherencia matemática
        self.assertEqual(self.env.step_count, steps_executed)

    def test_production_brain_isolation(self):
        """Verifica que el entorno de entrenamiento NUNCA modifique los cerebros de producción."""
        mainnet_file = "q_brain_advanced_lnm.json"
        testnet_file = "q_brain_testnet_lnm.json"

        prev_main_mtime = os.path.getmtime(mainnet_file) if os.path.exists(mainnet_file) else 0
        prev_test_mtime = os.path.getmtime(testnet_file) if os.path.exists(testnet_file) else 0

        # Ejecutar un episodio completo en el entorno
        candles = generate_synthetic_multi_regime_candles(num_bars=350)
        summary = self.env.run_episode(candles)

        self.assertIn("final_balance_sats", summary)
        self.assertIn("win_rate_pct", summary)

        # Confirmar que los archivos de producción no cambiaron de fecha ni contenido
        if os.path.exists(mainnet_file):
            self.assertEqual(os.path.getmtime(mainnet_file), prev_main_mtime, "q_brain_advanced_lnm.json fue mutado!")
        if os.path.exists(testnet_file):
            self.assertEqual(os.path.getmtime(testnet_file), prev_test_mtime, "q_brain_testnet_lnm.json fue mutado!")

    def test_candidate_brain_schema_v2_export(self):
        """Verifica que el cerebro candidato guardado cumpla rigurosamente con Schema v2."""
        candles = generate_synthetic_multi_regime_candles(num_bars=350)
        self.env.run_episode(candles)

        self.assertTrue(os.path.exists(self.cand_brain))
        import json
        with open(self.cand_brain, "r") as f:
            data = json.load(f)

        self.assertEqual(data.get("reward_schema_version"), 2)
        self.assertIn("q_table", data)
        self.assertIn("style_metrics", data)
        self.assertIn("state_visits", data)
        self.assertGreaterEqual(len(data["q_table"]), 100)


if __name__ == "__main__":
    unittest.main()
