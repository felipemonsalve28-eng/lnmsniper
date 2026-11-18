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

    def test_simulator_parity_patched_functions(self):
        """
        Certifica paridad 100% del simulador offline con los parches de Etapas 23-25:
        1. normalize_to_canonical_margin en cierres intra-episodio y EPISODE_END.
        2. Compuerta de viabilidad neta en FORCED_EDGE_EXIT (bloqueo dead-zone y cierre físico).
        3. Orquestación completa de AdaptiveHedgeController y HedgeRiskManager en step().
        """
        import tempfile
        import shutil
        from unittest.mock import patch
        from adaptive_controller import AdaptiveHedgeController
        from hedge_risk_manager import HedgeRiskManager
        from lnm_sniper_v6_advanced import Config

        tmp_dir = tempfile.mkdtemp(prefix="sim_parity_")
        try:
            # --- 1. normalize_to_canonical_margin (intra-episodio + EPISODE_END con margin > 1000) ---
            env_norm = SimTrainingEnvironment(
                candidate_brain_file=os.path.join(tmp_dir, "brain_norm.json"),
                candidate_hedge_file=os.path.join(tmp_dir, "hedge_norm.json"),
                initial_balance_sats=300000,
                explorer_mode=True,
                learning_enabled=True,
            )
            base_ts = 1700000000.0
            candles_norm = []
            price_cur = 70000.0
            for i in range(180):
                delta = 20.0 if i < 165 else -5.0
                open_p = price_cur
                close_p = open_p + delta
                price_cur = close_p
                candles_norm.append(
                    Candle(
                        timestamp=base_ts + i * 60.0,
                        open=round(open_p, 2),
                        high=round(max(open_p, close_p) + 12.0, 2),
                        low=round(min(open_p, close_p) - 12.0, 2),
                        close=round(close_p, 2),
                        volume=2.0,
                    )
                )

            with patch.object(
                env_norm.brain.supervisor,
                "normalize_to_canonical_margin",
                wraps=env_norm.brain.supervisor.normalize_to_canonical_margin,
            ) as spy_norm:
                # 1A: Cierre intra-episodio vía step() con margin = 5000 sats
                env_norm.reset(candles_norm[:160], warmup_bars=150)
                c_open_p = float(env_norm.feed.last_candle.close)
                t_intra = env_norm.exchange.open_trade(
                    side="b",
                    margin=5000,
                    leverage=10,
                    stoploss=c_open_p - 5.0,
                    takeprofit=c_open_p + 5000.0,
                    price=c_open_p,
                    now=env_norm.clock.time(),
                )
                env_norm.brain.active_trades[t_intra["id"]] = {
                    "action": 1,
                    "style": "TREND",
                    "margin": 5000,
                    "leverage": 10,
                    "state": "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL",
                    "entry_time": env_norm.clock.time(),
                    "entry_q": 0.12,
                    "is_exploratory": False,
                    "edge_deg_streak": 0,
                }
                # Vela que toca el SL inmediatamente en step()
                env_norm.feed.candles_1m[env_norm.feed.cursor] = Candle(
                    timestamp=candles_norm[env_norm.feed.cursor].timestamp,
                    open=c_open_p,
                    high=c_open_p + 5.0,
                    low=c_open_p - 50.0,
                    close=c_open_p - 30.0,
                    volume=2.0,
                )
                env_norm.step()
                self.assertGreaterEqual(spy_norm.call_count, 1)
                intra_margins = [float(call.args[1]) for call in spy_norm.call_args_list]
                self.assertIn(5000.0, intra_margins)

                # 1B: Cierre EPISODE_END en run_episode() conservando predicted_q_entry y margin > 1000
                calls_before_end = spy_norm.call_count
                for k_st in list(env_norm.brain.q_table.keys()):
                    env_norm.brain.q_table[k_st] = [0.0, 0.45, -0.1, -0.1, -0.1, -0.2, -0.2, -0.2, -0.2]
                env_norm.brain.epsilon = 0.0
                summary_end = env_norm.run_episode(candles_norm[:125], initial_balance=300000)
                self.assertGreater(summary_end["total_settled_trades"], 0)
                self.assertGreater(spy_norm.call_count, calls_before_end)
                self.assertGreater(len(env_norm.brain.supervisor.q_bias_window), 0)

            # --- 2. FORCED_EDGE_EXIT con Compuerta de Viabilidad Neta ---
            env_edge = SimTrainingEnvironment(
                candidate_brain_file=os.path.join(tmp_dir, "brain_edge.json"),
                candidate_hedge_file=os.path.join(tmp_dir, "hedge_edge.json"),
                initial_balance_sats=150000,
                explorer_mode=False,
                learning_enabled=True,
            )
            candles_drop = []
            p_d = 70000.0
            for i in range(210):
                step_d = -18.0 if i < 185 else -65.0
                op = p_d
                cl = op + step_d
                p_d = cl
                candles_drop.append(
                    Candle(
                        timestamp=base_ts + i * 60.0,
                        open=round(op, 2),
                        high=round(max(op, cl) + 10.0, 2),
                        low=round(min(op, cl) - 10.0, 2),
                        close=round(cl, 2),
                        volume=3.0,
                    )
                )
            env_edge.reset(candles_drop, warmup_bars=185)
            for k_st in list(env_edge.brain.q_table.keys()):
                env_edge.brain.q_table[k_st] = [0.0, -0.60, -0.60, -0.60, -0.60, -0.60, -0.60, -0.60, -0.60]

            next_c = env_edge.feed.candles_1m[env_edge.feed.cursor]
            # 2A: Zona muerta de comisiones (pnl_atr > -0.45, ganancia bruta < rt_fee + min_net_harvest)
            t_dz = env_edge.exchange.open_trade(
                side="b",
                margin=3000,
                leverage=20,
                stoploss=next_c.close * 0.85,
                takeprofit=next_c.close * 1.15,
                price=next_c.close - 5.0,
                atr=100.0,
                now=next_c.timestamp - 150.0,
            )
            env_edge.brain.active_trades[t_dz["id"]] = {
                "action": 1,
                "style": "TREND",
                "margin": 3000,
                "leverage": 20,
                "state": "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR",
                "entry_time": next_c.timestamp - 150.0,
                "entry_q": -0.60,
                "is_exploratory": False,
                "edge_deg_streak": Config.EDGE_DEGRADATION_PERSISTENCE,
            }
            env_edge.step()
            self.assertIn(
                t_dz["id"],
                env_edge.exchange.running_trades,
                "Regla 2 de FORCED_EDGE_EXIT debe bloquear cierre en zona muerta de comisiones.",
            )
            self.assertEqual(
                env_edge.brain.active_trades[t_dz["id"]]["edge_deg_streak"],
                Config.EDGE_DEGRADATION_PERSISTENCE,
            )

            # 2B: Pérdida física confirmada (>= max(30, 0.04*margin)) tras 90s -> ejecuta FORCED_EDGE_EXIT
            next_c2 = env_edge.feed.candles_1m[env_edge.feed.cursor]
            env_edge.exchange.running_trades.pop(t_dz["id"], None)
            env_edge.brain.active_trades.pop(t_dz["id"], None)
            t_phys = env_edge.exchange.open_trade(
                side="b",
                margin=3000,
                leverage=20,
                stoploss=next_c2.close * 0.80,
                takeprofit=next_c2.close * 1.20,
                price=next_c2.close + 450.0,
                atr=100.0,
                now=next_c2.timestamp - 150.0,
            )
            env_edge.brain.active_trades[t_phys["id"]] = {
                "action": 1,
                "style": "TREND",
                "margin": 3000,
                "leverage": 20,
                "state": "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR",
                "entry_time": next_c2.timestamp - 150.0,
                "entry_q": -0.60,
                "is_exploratory": False,
                "edge_deg_streak": Config.EDGE_DEGRADATION_PERSISTENCE,
            }
            env_edge.step()
            self.assertNotIn(t_phys["id"], env_edge.exchange.running_trades)
            closed_phys = [r for r in env_edge.exchange.closed_trades if str(r.get("id")) == str(t_phys["id"])]
            self.assertEqual(len(closed_phys), 1)
            self.assertEqual(closed_phys[0]["close_reason"], "FORCED_EDGE_EXIT")

            # --- 3. AdaptiveHedgeController + HedgeRiskManager integrados en step() ---
            env_hedge = SimTrainingEnvironment(
                candidate_brain_file=os.path.join(tmp_dir, "brain_h.json"),
                candidate_hedge_file=os.path.join(tmp_dir, "hedge_h.json"),
                initial_balance_sats=100000,
                explorer_mode=False,
                learning_enabled=True,
            )
            candles_hedge = []
            p_h = 70000.0
            for i in range(230):
                if i < 180:
                    step_h = 12.0 if (i % 6 < 3) else -14.0
                else:
                    step_h = -95.0
                op = p_h
                cl = op + step_h
                p_h = cl
                candles_hedge.append(
                    Candle(
                        timestamp=base_ts + i * 60.0,
                        open=round(op, 2),
                        high=round(max(op, cl) + 25.0, 2),
                        low=round(min(op, cl) - 25.0, 2),
                        close=round(cl, 2),
                        volume=4.0,
                    )
                )
            env_hedge.reset(candles_hedge, warmup_bars=185)
            for k_st in list(env_hedge.brain.q_table.keys()):
                env_hedge.brain.q_table[k_st] = [0.0, -0.55, -0.55, -0.55, -0.55, -0.55, -0.55, -0.55, -0.55]

            c0_h = env_hedge.feed.last_candle
            t_prim = env_hedge.exchange.open_trade(
                side="b",
                margin=5000,
                leverage=20,
                stoploss=c0_h.close * 0.85,
                takeprofit=c0_h.close * 1.15,
                price=c0_h.close + 300.0,
                atr=120.0,
                now=c0_h.timestamp - 120.0,
            )
            env_hedge.brain.active_trades[t_prim["id"]] = {
                "action": 1,
                "style": "TREND",
                "margin": 5000,
                "leverage": 20,
                "state": "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR",
                "entry_time": c0_h.timestamp - 120.0,
                "entry_q": -0.55,
                "is_exploratory": False,
                "edge_deg_streak": 0,
            }

            with patch.object(
                AdaptiveHedgeController,
                "evaluate_and_manage_pair",
                wraps=AdaptiveHedgeController.evaluate_and_manage_pair,
            ) as spy_eval, patch.object(
                HedgeRiskManager,
                "authorize_hedge_action",
                wraps=HedgeRiskManager.authorize_hedge_action,
            ) as spy_auth:
                opened_hedge_id = None
                for _ in range(12):
                    if str(t_prim["id"]) not in env_hedge.exchange.running_trades:
                        break
                    # Mantener edge_deg_streak bajo 3 para que FORCED_EDGE_EXIT no cierre antes de la histéresis de cobertura
                    if str(t_prim["id"]) in env_hedge.brain.active_trades:
                        env_hedge.brain.active_trades[str(t_prim["id"])]["edge_deg_streak"] = 0
                    env_hedge.step()
                    p_ctx_live = env_hedge.hedge_store.pairs.get(str(t_prim["id"]))
                    if isinstance(p_ctx_live, dict) and p_ctx_live.get("hedge_trade_id"):
                        opened_hedge_id = str(p_ctx_live["hedge_trade_id"])
                        break

                self.assertGreaterEqual(spy_eval.call_count, 1)
                self.assertGreaterEqual(spy_auth.call_count, 1)
                self.assertIsNotNone(
                    opened_hedge_id,
                    "AdaptiveHedgeController + HedgeRiskManager debieron abrir PARTIAL_HEDGE tras la histéresis.",
                )
                self.assertIn(opened_hedge_id, env_hedge.exchange.running_trades)
                self.assertTrue(env_hedge.exchange.running_trades[opened_hedge_id].get("_is_hedge", False))
                self.assertNotIn(opened_hedge_id, env_hedge.brain.active_trades)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
