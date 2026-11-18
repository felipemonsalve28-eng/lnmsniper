#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_hedge_anti_whipsaw.py
Suite de Certificación Unitaria para el Blindaje Integral del Hedge Engine y
Conciliación de Equity ante Coberturas Huérfanas (Parches 1–6).
"""
import os
import tempfile
import time
import unittest

from adaptive_controller import AdaptiveHedgeController
from dynamic_hedge import DynamicHedgeExecutor
from hedge_risk_manager import HedgeRiskManager
from hedge_state_store import HedgeStateStore
from lnm_sniper_v6_advanced import (
    AutonomousGoalBrain,
    compute_account_equity,
    reconcile_portfolio_with_hedges,
)
from predictive_hedge import PredictiveHedgeEngine


class _MockHedgeClient:
    def __init__(self, balance=10000, running=None):
        self.balance = balance
        self.running = list(running or [])
        self.get_user_calls = 0
        self.open_calls = []
        self.close_calls = []

    def open_trade(self, side, margin, leverage, stoploss=None, takeprofit=None):
        tid = f"h-mock-{len(self.open_calls) + 1}"
        t = {
            "id": tid,
            "side": side,
            "margin": int(margin),
            "leverage": float(leverage),
            "price": 70000.0,
            "stoploss": stoploss,
            "takeprofit": takeprofit,
            "pl": 0,
        }
        self.open_calls.append(t)
        self.running.append(t)
        return t

    def close_trade(self, trade_id):
        self.close_calls.append(str(trade_id))
        closed_obj = None
        for t in list(self.running):
            if str(t.get("id")) == str(trade_id):
                closed_obj = t
                self.running.remove(t)
                break
        if closed_obj is not None:
            # Al liquidarse el hedge, su margen + PnL - exit_fee vuelven al balance disponible en el servidor
            exit_fee = int(round(int(closed_obj.get("margin", 0)) * float(closed_obj.get("leverage", 1.0)) * 0.0010))
            self.balance += int(closed_obj.get("margin", 0)) + int(closed_obj.get("pl", 0)) - exit_fee
        return {"id": str(trade_id), "closed": True}

    def get_user(self, force_refresh=False):
        self.get_user_calls += 1
        return {"balance": self.balance}


class TestHedgeAntiWhipsaw(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="hedge_guard_test_")
        self.now = time.time()
        self.store = HedgeStateStore(
            env_mode="mainnet",
            filename=os.path.join(self.tmp_dir, "hedge_test.json"),
        )
        self.ind_5m = {"price": 77600.0, "atr": 250.0, "market_regime": "VOLATILITY_EXPANSION"}
        self.base_ctx = {
            "orig_trade_id": "orig-1",
            "orig_notional_sats": 60000,
            "orig_margin_sats": 3000,
            "orig_leverage": 20.0,
            "orig_side": "buy",
            "orig_opened_at": self.now - 120.0,
            "hedges_opened_count": 0,
            "cooldown_until": 0.0,
        }
        self.base_econ = {
            "has_active_hedge": False,
            "effective_hedge_ratio": 0.0,
            "hedge_notional_sats": 0,
            "orig_pl_sats": -240,
            "hedge_pl_sats": 0,
            "combined_net_pl_sats": -240,
        }
        self.base_pred = {
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.82,
            "expected_saved_sats": 500,
            "net_expected_saving_sats": 400,
            "recommend_hedge": True,
            "recommended_action_name": "INCREASE_HEDGE",
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_block_increase_without_partial_first(self):
        ok, reason, _ = HedgeRiskManager.authorize_hedge_action(
            "INCREASE_HEDGE",
            pair_ctx=self.base_ctx,
            pair_econ=self.base_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "BLOCKED_INCREASE_REQUIRES_PARTIAL_FIRST")

        # AdaptiveHedgeController en HEDGE_CANDIDATE sin cobertura solo permite PARTIAL_HEDGE (1) o NO_HEDGE (0)
        act_id, act_name = AdaptiveHedgeController.select_hedge_action(
            "HEDGE_CANDIDATE",
            "VOLATILITY_EXPANSION|HEDGE_CANDIDATE|SEVERE_LOSS|EDGE_BROKEN|UNHEDGED",
            self.base_ctx,
            self.base_econ,
            {"edge_score": -0.60, "pnl_atr": -1.0},
            self.base_pred,
            self.store,
        )
        self.assertEqual(act_id, 1)
        self.assertEqual(act_name, "PARTIAL_HEDGE")

    def test_02_block_grace_period_60s_and_physical_loss_4pct(self):
        # 2a. Edad < 60s -> BLOCKED_HEDGE_PRIMARY_GRACE_PERIOD
        young_ctx = dict(self.base_ctx, orig_opened_at=self.now - 25.0)
        ok_age, reason_age, _ = HedgeRiskManager.authorize_hedge_action(
            "PARTIAL_HEDGE",
            pair_ctx=young_ctx,
            pair_econ=self.base_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertFalse(ok_age)
        self.assertIn("BLOCKED_HEDGE_PRIMARY_GRACE_PERIOD", reason_age)

        # 2b. Pérdida física < 4% del margen (-50 sats sobre 3000 margen -> min_loss_req = 120)
        shallow_econ = dict(self.base_econ, orig_pl_sats=-50)
        ok_loss, reason_loss, _ = HedgeRiskManager.authorize_hedge_action(
            "PARTIAL_HEDGE",
            pair_ctx=self.base_ctx,
            pair_econ=shallow_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertFalse(ok_loss)
        self.assertIn("BLOCKED_HEDGE_NO_PHYSICAL_LOSS", reason_loss)

        # 2c. PredictiveHedgeEngine exige pnl_atr <= -0.50 para recomendar cobertura
        pred_no_loss = PredictiveHedgeEngine.forecast_adverse_risk(
            {
                "edge_score": -0.60,
                "edge_delta": -0.15,
                "pnl_atr": -0.10,
                "adverse_velocity_atr": 0.80,
                "dist_to_sl_atr": 0.70,
                "structure_broken": True,
                "mtf_trend_score": -0.50,
                "market_regime": "VOLATILITY_EXPANSION",
                "notional_sats": 60000,
                "close_now_loss_sats": 40,
                "sl_full_loss_sats": 900,
                "hold_cost_sats_per_hour": 5,
                "pl_sats": -40,
                "side": "buy",
            },
            self.ind_5m,
            {"ema_score": -0.5},
            {"ema_score": 0.3},
        )
        self.assertFalse(pred_no_loss["recommend_hedge"])
        self.assertEqual(pred_no_loss["recommended_action_name"], "NO_HEDGE")

    def test_03_block_second_hedge_on_same_pair(self):
        used_ctx = dict(self.base_ctx, hedges_opened_count=1)
        ok, reason, _ = HedgeRiskManager.authorize_hedge_action(
            "PARTIAL_HEDGE",
            pair_ctx=used_ctx,
            pair_econ=self.base_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertFalse(ok)
        self.assertIn("BLOCKED_MAX_HEDGES_PER_PAIR", reason)

        act_id, act_name = AdaptiveHedgeController.select_hedge_action(
            "HEDGE_CANDIDATE",
            "VOLATILITY_EXPANSION|HEDGE_CANDIDATE|SEVERE_LOSS|EDGE_BROKEN|UNHEDGED",
            used_ctx,
            self.base_econ,
            {"edge_score": -0.60, "pnl_atr": -1.0},
            self.base_pred,
            self.store,
        )
        self.assertEqual(act_id, 0)
        self.assertEqual(act_name, "NO_HEDGE")

    def test_04_block_premature_loss_unwind_in_recovery(self):
        active_ctx = dict(self.base_ctx, hedge_notional_sats=30000)
        losing_hedge_econ = {
            "has_active_hedge": True,
            "effective_hedge_ratio": 0.50,
            "hedge_notional_sats": 30000,
            "orig_pl_sats": -180,
            "hedge_pl_sats": -220,  # fuertemente negativo (< hedge_rt_fee = 60)
            "combined_net_pl_sats": -400,
        }
        act_id, act_name = AdaptiveHedgeController.select_hedge_action(
            "RECOVERY",
            "TREND_BEAR|RECOVERY|MOD_LOSS|EDGE_NEUTRAL|PARTIAL",
            active_ctx,
            losing_hedge_econ,
            {"edge_score": 0.14, "pnl_atr": -0.4},
            self.base_pred,
            self.store,
        )
        self.assertEqual(act_id, 0)
        self.assertEqual(act_name, "NO_HEDGE")

    def test_05_exact_round_trip_accounting_on_close_hedge(self):
        client = _MockHedgeClient(balance=20000)
        orig_t = {"id": "orig-rt-1", "side": "buy", "margin": 3000, "leverage": 20, "price": 78000.0, "pl": -240}
        pair_ctx = self.store.get_or_create_pair(orig_t, orig_meta={"style": "REVERSION", "entry_time": self.now - 120.0}, now=self.now)
        approved = {
            "hedge_side": "sell",
            "hedge_margin_sats": 1500,
            "hedge_leverage": 20,
            "hedge_notional_sats": 30000,
            "hedge_sl": 78400.0,
            "hedge_tp": 76500.0,
            "projected_hedge_ratio": 0.50,
            "est_round_fee_sats": 60,
        }
        open_res = DynamicHedgeExecutor.execute_hedge_action(
            client=client,
            action_name="PARTIAL_HEDGE",
            approved_params=approved,
            orig_trade=orig_t,
            hedge_trade=None,
            pair_ctx=pair_ctx,
            pair_econ=self.base_econ,
            ind_5m=self.ind_5m,
            hedge_store=self.store,
            safe_mode=False,
            now=self.now,
        )
        self.assertTrue(open_res["executed"])
        self.assertEqual(pair_ctx["hedges_opened_count"], 1)
        self.assertEqual(pair_ctx["hedge_entry_fee_sats"], 30)
        self.assertEqual(pair_ctx["hedge_opened_at"], self.now)

        hedge_t = client.open_calls[0]
        hedge_t["pl"] = -100  # cierre a pérdida simulado
        close_res = DynamicHedgeExecutor.execute_hedge_action(
            client=client,
            action_name="CLOSE_HEDGE",
            approved_params={},
            orig_trade=orig_t,
            hedge_trade=hedge_t,
            pair_ctx=pair_ctx,
            pair_econ={"has_active_hedge": True},
            ind_5m=self.ind_5m,
            hedge_store=self.store,
            safe_mode=False,
            now=self.now + 60.0,
        )
        self.assertTrue(close_res["executed"])
        # net_hedge_gain = h_pl (-100) - exit_fee (30) - entry_fee (30) = -160 exactos
        self.assertEqual(close_res["net_hedge_gain_sats"], -160)
        self.assertEqual(self.store.hedge_metrics["net_sats_saved_vs_sl"], -160)
        self.assertEqual(pair_ctx["cumulative_saved_sats"], -160)
        self.assertEqual(pair_ctx["hedge_entry_fee_sats"], 0)
        self.assertEqual(pair_ctx["hedges_opened_count"], 1)

    def test_06_orphan_hedge_accounting_and_no_phantom_dd(self):
        # Escenario real Mainnet: primaria cerró en servidor; queda cobertura huérfana de 1,636 sats de margen y +40 pl
        orphan_hedge = {"id": "h-orphan-1", "side": "buy", "margin": 1636, "leverage": 20.0, "price": 70000.0, "pl": 40}
        client = _MockHedgeClient(balance=8711, running=[orphan_hedge])
        brain = AutonomousGoalBrain(filename=os.path.join(self.tmp_dir, "brain_orphan.json"), explorer_mode=False)
        brain.active_trades["orig-closed-1"] = {"action": 6, "style": "REVERSION", "state": "S_TEST"}
        self.store.pairs["orig-closed-1"] = {
            "orig_trade_id": "orig-closed-1",
            "orig_side": "sell",
            "orig_margin_sats": 1636,
            "orig_leverage": 20.0,
            "orig_notional_sats": 32720,
            "hedge_trade_id": "h-orphan-1",
            "hedge_side": "buy",
            "hedge_margin_sats": 1636,
            "hedge_leverage": 20.0,
            "hedge_notional_sats": 32720,
            "state": "HEDGE_ACTIVE",
        }

        pre_running = list(client.running)
        stale_balance = client.balance
        pre_equity, _, _ = compute_account_equity(stale_balance, pre_running)
        self.assertGreater(pre_equity, 10300)

        pre_count = len(pre_running)
        running_after, _ = reconcile_portfolio_with_hedges(
            client, brain, self.store, pre_running,
            equity_sats=pre_equity, btc_price=70000.0, now=self.now, safe_mode=False,
        )
        self.assertEqual(len(running_after), 0)
        self.assertLess(len(running_after), pre_count)
        self.assertIn("h-orphan-1", client.close_calls)

        # Verificar contabilización de PnL/fee del hedge huérfano en brain.active_trades
        expected_rt_fee = max(2, int(round(1636 * 20.0 * 0.0020)))
        self.assertEqual(brain.active_trades["orig-closed-1"]["realized_hedge_fee_sats"], expected_rt_fee)
        self.assertEqual(brain.active_trades["orig-closed-1"]["realized_hedge_net_sats"], 40 - expected_rt_fee)

        # Sin refrescar balance habría caída fantasma a 8,711; con el refresco de main() el equity real se preserva exacto
        phantom_equity, _, _ = compute_account_equity(stale_balance, running_after)
        self.assertEqual(phantom_equity, 8711)
        balance = client.get_user(force_refresh=True)["balance"]
        post_equity, _, _ = compute_account_equity(balance, running_after)
        self.assertEqual(client.get_user_calls, 1)
        self.assertEqual(post_equity, pre_equity)

    def test_07_anti_whipsaw_extended_to_increase_hedge(self):
        active_partial_econ = {
            "has_active_hedge": True,
            "effective_hedge_ratio": 0.45,
            "hedge_notional_sats": 27000,
            "orig_pl_sats": -300,
        }
        ok, reason, _ = HedgeRiskManager.authorize_hedge_action(
            "INCREASE_HEDGE",
            pair_ctx=self.base_ctx,
            pair_econ=active_partial_econ,
            portfolio_snap={"locked_margin_sats": 4350},
            predictive_signal={
                "optimal_hedge_ratio": 0.85,
                "adverse_continuation_prob": 0.72,  # < 0.78 en RANGE_SQUEEZE
                "expected_saved_sats": 500,
                "net_expected_saving_sats": 400,
            },
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "RANGE_SQUEEZE"},
            now=self.now,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "BLOCKED_HEDGE_SQUEEZE_WHIPSAW_RISK")

    def test_08_block_reduce_original_and_hedge_below_fee_threshold_and_max_1_reduction(self):
        # orig_notional = 60,000 -> orig_rt_fee = 120 -> min_orig_pl_for_cashin = max(60, 120 * 1.50) = 180
        low_profit_econ = dict(self.base_econ, orig_pl_sats=33)
        ok_low, reason_low, _ = HedgeRiskManager.authorize_hedge_action(
            "REDUCE_ORIGINAL_POSITION",
            pair_ctx=self.base_ctx,
            pair_econ=low_profit_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertFalse(ok_low)
        self.assertIn("BLOCKED_ORIG_CASHIN_BELOW_FEE_THRESHOLD", reason_low)

        # AdaptiveHedgeController en estado NORMAL también bloquea acción 5 si orig_pl < 1.50 * orig_rt_fee
        act_id, act_name = AdaptiveHedgeController.select_hedge_action(
            "NORMAL",
            "VOLATILITY_EXPANSION|NORMAL|FLAT|EDGE_WEAK|UNHEDGED",
            self.base_ctx,
            low_profit_econ,
            {"edge_score": -0.10, "edge_delta": -0.20, "pnl_atr": 0.15},
            self.base_pred,
            self.store,
        )
        self.assertEqual(act_id, 0)
        self.assertEqual(act_name, "NO_HEDGE")

        # Cuando orig_pl >= 180, se autoriza la primera reducción parcial y se bloquea la segunda
        high_profit_econ = dict(self.base_econ, orig_pl_sats=240)
        ok_hi, reason_hi, _ = HedgeRiskManager.authorize_hedge_action(
            "REDUCE_ORIGINAL_POSITION",
            pair_ctx=self.base_ctx,
            pair_econ=high_profit_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertTrue(ok_hi)
        self.assertEqual(reason_hi, "AUTHORIZED_REDUCE_ORIGINAL_POSITION")

        used_red_ctx = dict(self.base_ctx, orig_reductions_count=1)
        ok_max, reason_max, _ = HedgeRiskManager.authorize_hedge_action(
            "REDUCE_ORIGINAL_POSITION",
            pair_ctx=used_red_ctx,
            pair_econ=high_profit_econ,
            portfolio_snap={"locked_margin_sats": 3000},
            predictive_signal=self.base_pred,
            account_equity_sats=30000,
            available_balance_sats=20000,
            locked_floor_sats=21000,
            ind_5m=self.ind_5m,
            now=self.now,
        )
        self.assertFalse(ok_max)
        self.assertEqual(reason_max, "BLOCKED_MAX_ORIG_REDUCTIONS_REACHED")

    def test_09_edge_monitor_exploratory_negative_q_neutralization(self):
        from edge_monitor import EdgeMonitor
        trade_exp = {
            "id": "eb046e42-test",
            "side": "sell",
            "margin": 42300,
            "leverage": 20.0,
            "price": 78000.0,
            "stoploss": 78800.0,
            "liquidation": 81500.0,
            "pl": 221,
        }
        orig_meta = {"is_exploratory": True, "entry_time": self.now - 55.0}
        pair_ctx = self.store.get_or_create_pair(trade_exp, orig_meta=orig_meta, now=self.now)
        res = EdgeMonitor.evaluate_position_edge(
            trade_exp,
            pair_ctx,
            {"price": 77960.0, "atr": 200.0, "rsi": 49.0, "ema_score": 0.0, "_now_ts": self.now},
            {"rsi": 50.0, "ema_score": 0.0},
            {"ema_score": 0.0},
            perception={"vel_ema_atr": 0.0, "clv": 0.0, "override_side": None},
            q_val_action=-1.1122,
            now=self.now,
            orig_meta=orig_meta,
        )
        self.assertEqual(res["q_cql"], -1.1122)
        self.assertEqual(res["effective_q_cql"], 0.0)
        self.assertGreater(res["edge_score"], -0.65)


if __name__ == "__main__":
    unittest.main(verbosity=2)
