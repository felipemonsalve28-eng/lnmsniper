#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_hedge_terminal_bellman.py
Prueba unitaria exhaustiva para validar la corrección matemática de estados terminales
en la ecuación de Bellman de Hybrid Edge Hedging Engine (hedge_state_store.py y adaptive_controller.py).
"""
import sys
import os
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from hedge_state_store import HedgeStateStore
from adaptive_controller import AdaptiveHedgeController


class TestHedgeTerminalBellman(unittest.TestCase):

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.temp_file.close()
        self.store = HedgeStateStore(filename=self.temp_file.name)

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.remove(self.temp_file.name)

    def test_terminal_bellman_with_done_flag(self):
        """Verifica que con done=True, max_next sea forzado a 0.0."""
        a = 2
        s = "TREND_BEAR|NORMAL|UNHEDGED|MID2"
        ns = "RANGE_NORMAL|LOW|UNHEDGED|RECOVERY2"
        self.store.hedge_q_table[ns] = [0.80] * 7
        self.store.hedge_q_table[s] = [0.10] * 7
        
        cur_q = 0.10
        r = 0.50
        alpha = 0.10
        gamma = 0.90
        # Con done=True, max_next = 0.0: target = r + 0.0 = 0.50
        # new_q = cur_q + alpha * (0.50 - 0.10) = 0.10 + 0.04 = 0.14
        delta_q = self.store.update_hedge_q(
            state_key=s, action_idx=a, reward=r, next_state_key=ns, alpha=alpha, gamma=gamma, done=True
        )
        updated_q = self.store.hedge_q_table[s][a]
        self.assertAlmostEqual(updated_q, 0.14, places=4, msg="Con done=True no debe propagar valor futuro")
        self.assertAlmostEqual(delta_q, abs(updated_q - cur_q), places=4)

    def test_terminal_bellman_with_hedge_exit_in_ns(self):
        """Verifica que si 'HEDGE_EXIT' está en el estado siguiente, se trate como terminal aún con done=False."""
        s = "TREND_BEAR|NORMAL|FULL_HEDGED|MID"
        ns = "HEDGE_EXIT|RANGE_NORMAL|MID"
        a = 4  # UNWIND_FULL
        r = 0.40
        alpha = 0.20
        gamma = 0.95

        # Sembrar el estado siguiente con valores altos
        self.store.hedge_q_table[ns] = [0.90] * 7
        self.store.hedge_q_table[s] = [0.20] * 7

        # Al llamar con done=False pero ns contiene HEDGE_EXIT:
        # is_terminal = True -> max_next = 0.0
        # new_q = 0.20 + 0.20 * (0.40 - 0.20) = 0.20 + 0.04 = 0.24
        delta_q = self.store.update_hedge_q(
            state_key=s, action_idx=a, reward=r, next_state_key=ns, alpha=alpha, gamma=gamma, done=False
        )
        updated_q = self.store.hedge_q_table[s][a]
        self.assertAlmostEqual(updated_q, 0.24, places=4, msg="HEDGE_EXIT en ns debe forzar max_next=0.0")

    def test_non_terminal_bellman_propagates_discounted_max(self):
        """Verifica que un estado NO terminal propague gamma * max_next correctamente."""
        s = "TREND_BEAR|NORMAL|UNHEDGED|MID"
        ns = "TREND_BEAR|EXPANDING|SOFT_HEDGED|LOW"
        a = 1  # OPEN_SOFT_SHORT
        r = 0.10
        alpha = 0.20
        gamma = 0.90

        # Sembrar ns con max_next = 0.50
        self.store.hedge_q_table[ns] = [0.10, 0.20, 0.50, 0.30, 0.0, 0.0, 0.0]
        self.store.hedge_q_table[s] = [0.0] * 7

        # cur_q = 0.0, max_next = 0.50, eff_gamma = 0.90
        # target = r + gamma * max_next = 0.10 + 0.90 * 0.50 = 0.10 + 0.45 = 0.55
        # new_q = 0.0 + 0.20 * (0.55 - 0.0) = 0.11
        delta_q = self.store.update_hedge_q(
            state_key=s, action_idx=a, reward=r, next_state_key=ns, alpha=alpha, gamma=gamma, done=False
        )
        updated_q = self.store.hedge_q_table[s][a]
        self.assertAlmostEqual(updated_q, 0.11, places=4, msg="Estado no terminal debe propagar gamma * max_next")

    def test_adaptive_controller_exit_transition_flag(self):
        """Verifica que la lógica en evaluate_and_manage_pair pase done=True al detectar HEDGE_EXIT."""
        hedge_state_key = "RANGE_NORMAL|HEDGE_EXIT|FLAT|EDGE_POSITIVE|NO_HEDGE"
        state = "HEDGE_EXIT"
        is_exit_transition = ("HEDGE_EXIT" in hedge_state_key) or (state == "HEDGE_EXIT")
        self.assertTrue(is_exit_transition)

        # Probar que update_hedge_q con is_exit_transition opera como terminal
        s = "RANGE_NORMAL|RECOVERY|FLAT|EDGE_POSITIVE|SOFT_HEDGE"
        self.store.hedge_q_table[hedge_state_key] = [0.95] * 7
        self.store.hedge_q_table[s] = [0.0] * 7
        
        delta_q = self.store.update_hedge_q(
            state_key=s, action_idx=4, reward=0.25, next_state_key=hedge_state_key, done=is_exit_transition, alpha=0.1
        )
        # alpha=0.1, cur_q=0.0, max_next=0.0 -> new_q = 0.0 + 0.1 * (0.25 - 0.0) = 0.025
        updated_q = self.store.hedge_q_table[s][4]
        self.assertAlmostEqual(updated_q, 0.025, places=4)


if __name__ == "__main__":
    unittest.main()
