# -*- coding: utf-8 -*-
"""
adaptive_controller.py
Capa 4 (Adaptive Decision & Safety) del Hybrid Edge Hedging Engine.
Controlador adaptativo con máquina de 7 estados e histéresis explícita:
NORMAL -> WARNING -> HEDGE_CANDIDATE -> HEDGE_ACTIVE -> HEDGE_ADJUST -> RECOVERY -> HEDGE_EXIT
Selecciona entre las 7 acciones independientes del controlador de cobertura y
actualiza una tabla Q tabular independiente sin alterar las 9 acciones del cerebro principal.
"""
import time
from hedge_state_store import HEDGE_ACTIONS, NUM_HEDGE_ACTIONS
from edge_monitor import EdgeMonitor
from predictive_hedge import PredictiveHedgeEngine
from portfolio_hedge import PortfolioHedgeCoordinator
from hedge_risk_manager import HedgeRiskManager
from dynamic_hedge import DynamicHedgeExecutor


ACTION_NAME_TO_ID = {v: k for k, v in HEDGE_ACTIONS.items()}


class AdaptiveHedgeController:
    """
    Orquesta el ciclo completo de inteligencia, monitoreo de edge, predicción,
    máquina de estados con histéresis, autorización de riesgo, ejecución API v3
    y aprendizaje tabular independiente para el motor de cobertura.
    """

    # Parámetros de histéresis para evitar sobreoperación (whipsaw)
    WARN_EDGE_THRESHOLD = -0.18
    WARN_PNL_ATR_THRESHOLD = -0.35
    CANDIDATE_EDGE_THRESHOLD = -0.30
    CANDIDATE_MIN_ADVERSE_PROB = 0.55
    RECOVERY_EDGE_THRESHOLD = 0.06
    MIN_CANDIDATE_STREAK = 2
    MIN_RECOVERY_STREAK = 2

    @staticmethod
    def discretize_hedge_state(market_regime, hedge_state, pnl_atr, edge_score, eff_hedge_ratio):
        reg = str(market_regime or "RANGE")
        st = str(hedge_state or "NORMAL")
        if pnl_atr <= -0.85:
            pnl_b = "SEVERE_LOSS"
        elif pnl_atr <= -0.35:
            pnl_b = "MOD_LOSS"
        elif pnl_atr < 0.25:
            pnl_b = "FLAT"
        else:
            pnl_b = "PROFIT"

        if edge_score <= -0.45:
            edge_b = "EDGE_BROKEN"
        elif edge_score <= -0.18:
            edge_b = "EDGE_WEAK"
        elif edge_score < 0.15:
            edge_b = "EDGE_NEUTRAL"
        else:
            edge_b = "EDGE_POSITIVE"

        if eff_hedge_ratio <= 0.05:
            hr_b = "UNHEDGED"
        elif eff_hedge_ratio < 0.68:
            hr_b = "PARTIAL"
        else:
            hr_b = "FULL"

        return f"{reg}|{st}|{pnl_b}|{edge_b}|{hr_b}"

    @staticmethod
    def compute_hedge_reward(prev_net_pl_sats, curr_net_pl_sats, action_idx, fee_paid_sats, margin_sats, avoided_sl_sats=0):
        """
        Recompensa multiobjetivo del controlador de cobertura:
        Premia la reducción de pérdida neta y el ahorro frente al SL;
        penaliza comisiones, sobreoperación y coberturas prematuras.
        """
        scale = max(100.0, float(margin_sats) * 0.35)
        delta_net = float(curr_net_pl_sats - prev_net_pl_sats)
        saved_bonus = float(avoided_sl_sats) * 0.50
        fee_Drag = 1.45 * float(fee_paid_sats)
        churn_pen = 8.0 if int(action_idx) in (1, 2, 3, 4) and abs(delta_net) < fee_paid_sats else 0.0

        raw = (delta_net + saved_bonus - fee_Drag - churn_pen) / scale
        return round(max(-1.0, min(1.0, raw)), 4)

    @classmethod
    def _transition_state_with_hysteresis(cls, pair_ctx, pair_econ, edge_info, pred_sig, now):
        cur_state = str(pair_ctx.get("state", "NORMAL"))
        has_hedge = bool(pair_econ.get("has_active_hedge", False))
        edge_score = float(edge_info.get("edge_score", 0.0))
        pnl_atr = float(edge_info.get("pnl_atr", 0.0))
        adv_prob = float(pred_sig.get("adverse_continuation_prob", 0.0))
        rec_prob = float(pred_sig.get("reversion_prob", 0.5))

        pair_ctx["state_cycles"] = int(pair_ctx.get("state_cycles", 0)) + 1
        new_state = cur_state

        if not has_hedge:
            if cur_state in ("HEDGE_ACTIVE", "HEDGE_ADJUST", "RECOVERY", "HEDGE_EXIT"):
                new_state = "NORMAL"
            elif cur_state == "NORMAL":
                if edge_score <= cls.WARN_EDGE_THRESHOLD or pnl_atr <= cls.WARN_PNL_ATR_THRESHOLD:
                    pair_ctx["warning_streak"] = int(pair_ctx.get("warning_streak", 0)) + 1
                    if pair_ctx["warning_streak"] >= 1:
                        new_state = "WARNING"
                else:
                    pair_ctx["warning_streak"] = 0
            elif cur_state == "WARNING":
                if edge_score >= 0.04 and pnl_atr > -0.25:
                    pair_ctx["recovery_streak"] = int(pair_ctx.get("recovery_streak", 0)) + 1
                    if pair_ctx["recovery_streak"] >= 2:
                        new_state = "NORMAL"
                        pair_ctx["warning_streak"] = 0
                        pair_ctx["candidate_streak"] = 0
                elif edge_score <= cls.CANDIDATE_EDGE_THRESHOLD and adv_prob >= cls.CANDIDATE_MIN_ADVERSE_PROB:
                    pair_ctx["candidate_streak"] = int(pair_ctx.get("candidate_streak", 0)) + 1
                    if pair_ctx["candidate_streak"] >= cls.MIN_CANDIDATE_STREAK:
                        new_state = "HEDGE_CANDIDATE"
            elif cur_state == "HEDGE_CANDIDATE":
                if edge_score > -0.15 and adv_prob < 0.45:
                    new_state = "WARNING"
                    pair_ctx["candidate_streak"] = 0
        else:
            # Con cobertura activa
            hedge_pl = int(pair_econ.get("hedge_pl_sats", 0))
            comb_net = int(pair_econ.get("combined_net_pl_sats", 0))
            if cur_state in ("NORMAL", "WARNING", "HEDGE_CANDIDATE"):
                new_state = "HEDGE_ACTIVE"
            elif cur_state == "HEDGE_ACTIVE":
                if edge_score >= cls.RECOVERY_EDGE_THRESHOLD and rec_prob >= 0.48:
                    pair_ctx["recovery_streak"] = int(pair_ctx.get("recovery_streak", 0)) + 1
                    if pair_ctx["recovery_streak"] >= cls.MIN_RECOVERY_STREAK:
                        new_state = "RECOVERY"
                elif adv_prob >= 0.72 and float(pair_econ.get("effective_hedge_ratio", 0.0)) < 0.72:
                    new_state = "HEDGE_ADJUST"
                elif comb_net >= max(35, int(pair_ctx.get("orig_margin_sats", 1000) * 0.06)):
                    new_state = "HEDGE_EXIT"
            elif cur_state == "HEDGE_ADJUST":
                if edge_score >= cls.RECOVERY_EDGE_THRESHOLD:
                    new_state = "RECOVERY"
                elif pair_ctx["state_cycles"] >= 2:
                    new_state = "HEDGE_ACTIVE"
            elif cur_state == "RECOVERY":
                if edge_score <= -0.32:
                    new_state = "HEDGE_ACTIVE"
                    pair_ctx["recovery_streak"] = 0
                elif hedge_pl >= 15 or pair_ctx["state_cycles"] >= 2:
                    new_state = "HEDGE_EXIT"

        if new_state != cur_state:
            pair_ctx["state"] = new_state
            pair_ctx["state_entered_at"] = now
            pair_ctx["state_cycles"] = 0
            pair_ctx["last_transition_ts"] = now

        return new_state

    @classmethod
    def select_hedge_action(cls, state, hedge_state_key, pair_ctx, pair_econ, edge_info, pred_sig, hedge_store):
        q_vals = hedge_store.get_q_values(hedge_state_key)
        has_hedge = bool(pair_econ.get("has_active_hedge", False))
        eff_ratio = float(pair_econ.get("effective_hedge_ratio", 0.0))
        hedge_pl = int(pair_econ.get("hedge_pl_sats", 0))
        orig_pl = int(pair_econ.get("orig_pl_sats", 0))
        comb_net = int(pair_econ.get("combined_net_pl_sats", 0))
        rec_act_name = str(pred_sig.get("recommended_action_name", "NO_HEDGE"))

        # 1. Estados tranquilos sin cobertura
        if state in ("NORMAL", "WARNING") and not has_hedge:
            if orig_pl >= 60 and float(edge_info.get("edge_delta", 0.0)) < -0.12:
                return 5, "REDUCE_ORIGINAL_POSITION"
            return 0, "NO_HEDGE"

        # 2. Candidato confirmado a cobertura
        if state == "HEDGE_CANDIDATE" and not has_hedge:
            if pred_sig.get("direct_close_preferred", False):
                return 6, "CLOSE_ORIGINAL_POSITION"
            if pred_sig.get("recommend_hedge", False):
                if rec_act_name == "INCREASE_HEDGE":
                    return 2, "INCREASE_HEDGE"
                return 1, "PARTIAL_HEDGE"
            return 0, "NO_HEDGE"

        # 3. Cobertura activa / ajuste
        if state in ("HEDGE_ACTIVE", "HEDGE_ADJUST") and has_hedge:
            if pred_sig.get("direct_close_preferred", False) and float(edge_info.get("pnl_atr", 0.0)) <= -0.95:
                return 6, "CLOSE_ORIGINAL_POSITION"
            if float(pred_sig.get("adverse_continuation_prob", 0.0)) >= 0.72 and (eff_ratio < 0.72 or hedge_pl > 30):
                return 2, "INCREASE_HEDGE"
            if hedge_pl >= 50 and float(edge_info.get("edge_delta", 0.0)) > 0.05:
                return 3, "REDUCE_HEDGE"
            return 0, "NO_HEDGE"

        # 4. Recuperación / Salida de cobertura
        if state in ("RECOVERY", "HEDGE_EXIT") and has_hedge:
            if comb_net > 25 or hedge_pl >= 15 or float(edge_info.get("edge_score", 0.0)) >= 0.12:
                return 4, "CLOSE_HEDGE"
            if hedge_pl >= 40:
                return 3, "REDUCE_HEDGE"
            return 4, "CLOSE_HEDGE"

        # Selección asistida por Q-table independiente entre acciones válidas
        valid_ids = [0, 5, 6] if not has_hedge else [0, 2, 3, 4, 6]
        best_id = max(valid_ids, key=lambda idx: float(q_vals[idx]))
        return int(best_id), HEDGE_ACTIONS[int(best_id)]

    @classmethod
    def evaluate_and_manage_pair(
        cls,
        client,
        orig_trade,
        hedge_trade,
        orig_meta,
        portfolio_snap,
        hedge_store,
        ind_5m,
        ind_15m,
        ind_1h,
        perception,
        q_val_main,
        account_equity_sats,
        available_balance_sats,
        locked_floor_sats,
        latency_degraded=False,
        explorer_mode=False,
        safe_mode=False,
        now=None,
    ):
        now = float(now if now is not None else time.time())
        pair_ctx = hedge_store.get_or_create_pair(orig_trade, orig_meta=orig_meta, now=now)
        btc_price = float(ind_5m.get("price", 70000.0))

        pair_econ = PortfolioHedgeCoordinator.evaluate_pair_economics(
            orig_trade, hedge_trade, pair_ctx, btc_price
        )
        edge_info = EdgeMonitor.evaluate_position_edge(
            orig_trade, pair_ctx, ind_5m, ind_15m, ind_1h,
            perception=perception, q_val_action=q_val_main, now=now
        )
        pred_sig = PredictiveHedgeEngine.forecast_adverse_risk(
            edge_info, ind_5m, ind_15m, ind_1h, perception=perception
        )

        prev_net_pl = int(pair_ctx.get("last_combined_net_sats", pair_econ["combined_net_pl_sats"]))
        curr_net_pl = int(pair_econ["combined_net_pl_sats"])

        state = cls._transition_state_with_hysteresis(pair_ctx, pair_econ, edge_info, pred_sig, now)
        hedge_state_key = cls.discretize_hedge_state(
            edge_info["market_regime"],
            state,
            edge_info["pnl_atr"],
            edge_info["edge_score"],
            pair_econ["effective_hedge_ratio"],
        )

        # Actualizar aprendizaje Q independiente de la transición anterior
        prev_h_key = pair_ctx.get("last_hedge_state_key")
        prev_h_act = int(pair_ctx.get("last_hedge_action", 0))
        if prev_h_key is not None:
            r_h = cls.compute_hedge_reward(
                prev_net_pl,
                curr_net_pl,
                prev_h_act,
                fee_paid_sats=0,
                margin_sats=pair_ctx.get("orig_margin_sats", 1000),
            )
            hedge_store.update_hedge_q(prev_h_key, prev_h_act, r_h, hedge_state_key)

        act_id, act_name = cls.select_hedge_action(
            state, hedge_state_key, pair_ctx, pair_econ, edge_info, pred_sig, hedge_store
        )

        auth_ok, auth_reason, approved_params = HedgeRiskManager.authorize_hedge_action(
            act_name,
            pair_ctx,
            pair_econ,
            portfolio_snap,
            pred_sig,
            account_equity_sats=account_equity_sats,
            available_balance_sats=available_balance_sats,
            locked_floor_sats=locked_floor_sats,
            ind_5m=ind_5m,
            latency_degraded=latency_degraded,
            explorer_mode=explorer_mode,
            now=now,
        )

        if not auth_ok:
            act_id, act_name = 0, "NO_HEDGE"
            exec_res = {"executed": False, "action": "NO_HEDGE", "reason": auth_reason}
        else:
            exec_res = DynamicHedgeExecutor.execute_hedge_action(
                client=client,
                action_name=act_name,
                approved_params=approved_params,
                orig_trade=orig_trade,
                hedge_trade=hedge_trade,
                pair_ctx=pair_ctx,
                pair_econ=pair_econ,
                ind_5m=ind_5m,
                hedge_store=hedge_store,
                safe_mode=safe_mode,
                now=now,
            )

        pair_ctx["last_hedge_state_key"] = hedge_state_key
        pair_ctx["last_hedge_action"] = int(act_id)
        pair_ctx["last_combined_net_sats"] = curr_net_pl
        hedge_store.dirty = True

        return {
            "orig_trade_id": str(orig_trade["id"]),
            "state": pair_ctx["state"],
            "hedge_state_key": hedge_state_key,
            "selected_action_id": act_id,
            "selected_action_name": act_name,
            "authorized": auth_ok,
            "auth_reason": auth_reason,
            "execution": exec_res,
            "edge_info": edge_info,
            "predictive_signal": pred_sig,
            "pair_economics": pair_econ,
        }
