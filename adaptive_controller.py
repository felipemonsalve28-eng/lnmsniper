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

    # Parámetros de histéresis física y temporal para erradicar whipsaw
    WARN_EDGE_THRESHOLD = -0.25
    WARN_PNL_ATR_THRESHOLD = -0.45
    CANDIDATE_EDGE_THRESHOLD = -0.38
    CANDIDATE_MIN_ADVERSE_PROB = 0.65
    CANDIDATE_MIN_ADVERSE_PNL_ATR = -0.60
    CANDIDATE_MIN_LOSS_MARGIN_RATIO = 0.04
    MIN_PRIMARY_AGE_SEC = 60.0
    MIN_HEDGE_HOLD_SEC = 45.0
    MAX_HEDGES_PER_PAIR = 1
    RECOVERY_EDGE_THRESHOLD = 0.12
    MIN_CANDIDATE_STREAK = 3
    MIN_RECOVERY_STREAK = 4

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

        orig_pl = int(pair_econ.get("orig_pl_sats", 0))
        orig_margin = max(1000, int(pair_ctx.get("orig_margin_sats", 1000)))
        orig_opened_at = float(pair_ctx.get("orig_opened_at", pair_ctx.get("state_entered_at", now)))
        primary_age_sec = max(0.0, float(now) - orig_opened_at)
        min_loss_sats = max(30, int(round(orig_margin * cls.CANDIDATE_MIN_LOSS_MARGIN_RATIO)))
        has_physical_loss = (pnl_atr <= cls.CANDIDATE_MIN_ADVERSE_PNL_ATR) and (orig_pl <= -min_loss_sats)
        can_open_new_hedge = (
            primary_age_sec >= cls.MIN_PRIMARY_AGE_SEC
            and int(pair_ctx.get("hedges_opened_count", 0)) < cls.MAX_HEDGES_PER_PAIR
        )

        pair_ctx["state_cycles"] = int(pair_ctx.get("state_cycles", 0)) + 1
        new_state = cur_state

        if not has_hedge:
            if cur_state in ("HEDGE_ACTIVE", "HEDGE_ADJUST", "RECOVERY", "HEDGE_EXIT"):
                new_state = "NORMAL"
            elif cur_state == "NORMAL":
                if (edge_score <= cls.WARN_EDGE_THRESHOLD and pnl_atr <= -0.25) or pnl_atr <= cls.WARN_PNL_ATR_THRESHOLD:
                    pair_ctx["warning_streak"] = int(pair_ctx.get("warning_streak", 0)) + 1
                    if pair_ctx["warning_streak"] >= 2:
                        new_state = "WARNING"
                else:
                    pair_ctx["warning_streak"] = 0
            elif cur_state == "WARNING":
                hist = pair_ctx.get("edge_history", [])
                rebounding_step = len(hist) >= 2 and (pnl_atr - float(hist[-2][2])) >= 0.10
                if (edge_score >= 0.04 and pnl_atr > -0.25) or rebounding_step:
                    pair_ctx["recovery_streak"] = int(pair_ctx.get("recovery_streak", 0)) + 1
                    pair_ctx["candidate_streak"] = 0
                    if pair_ctx["recovery_streak"] >= 2:
                        new_state = "NORMAL"
                        pair_ctx["warning_streak"] = 0
                        pair_ctx["candidate_streak"] = 0
                elif (
                    can_open_new_hedge
                    and has_physical_loss
                    and edge_score <= cls.CANDIDATE_EDGE_THRESHOLD
                    and adv_prob >= cls.CANDIDATE_MIN_ADVERSE_PROB
                ):
                    pair_ctx["candidate_streak"] = int(pair_ctx.get("candidate_streak", 0)) + 1
                    if pair_ctx["candidate_streak"] >= cls.MIN_CANDIDATE_STREAK:
                        new_state = "HEDGE_CANDIDATE"
                else:
                    pair_ctx["candidate_streak"] = 0
            elif cur_state == "HEDGE_CANDIDATE":
                if (not has_physical_loss) or (edge_score > -0.20 and adv_prob < 0.50):
                    new_state = "WARNING"
                    pair_ctx["candidate_streak"] = 0
        else:
            # Con cobertura activa
            hedge_pl = int(pair_econ.get("hedge_pl_sats", 0))
            comb_net = int(pair_econ.get("combined_net_pl_sats", 0))
            hedge_opened_at = float(pair_ctx.get("hedge_opened_at", pair_ctx.get("state_entered_at", now)))
            hedge_age_sec = max(0.0, float(now) - hedge_opened_at)
            hedge_notional = max(1000, int(pair_ctx.get("hedge_notional_sats", 1000)))
            hedge_rt_fee = max(2, int(round(hedge_notional * 0.0020)))

            if cur_state in ("NORMAL", "WARNING", "HEDGE_CANDIDATE"):
                new_state = "HEDGE_ACTIVE"
            elif cur_state == "HEDGE_ACTIVE":
                if hedge_age_sec >= cls.MIN_HEDGE_HOLD_SEC and edge_score >= cls.RECOVERY_EDGE_THRESHOLD and rec_prob >= 0.52:
                    pair_ctx["recovery_streak"] = int(pair_ctx.get("recovery_streak", 0)) + 1
                    if pair_ctx["recovery_streak"] >= cls.MIN_RECOVERY_STREAK:
                        new_state = "RECOVERY"
                elif adv_prob >= 0.78 and pnl_atr <= -0.95 and float(pair_econ.get("effective_hedge_ratio", 0.0)) < 0.68:
                    new_state = "HEDGE_ADJUST"
                elif comb_net >= max(35, int(orig_margin * 0.06)):
                    new_state = "HEDGE_EXIT"
                else:
                    pair_ctx["recovery_streak"] = 0
            elif cur_state == "HEDGE_ADJUST":
                if hedge_age_sec >= cls.MIN_HEDGE_HOLD_SEC and edge_score >= cls.RECOVERY_EDGE_THRESHOLD:
                    new_state = "RECOVERY"
                elif pair_ctx["state_cycles"] >= 2:
                    new_state = "HEDGE_ACTIVE"
            elif cur_state == "RECOVERY":
                if edge_score <= -0.32:
                    new_state = "HEDGE_ACTIVE"
                    pair_ctx["recovery_streak"] = 0
                elif (
                    hedge_pl >= hedge_rt_fee
                    or comb_net >= 25
                    or (hedge_age_sec >= cls.MIN_HEDGE_HOLD_SEC and pair_ctx["state_cycles"] >= 4 and edge_score >= 0.18 and rec_prob >= 0.58)
                ):
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
                valid_ids = [0, 5]
                heuristic_pref = 5
            else:
                return 0, "NO_HEDGE"

        # 2. Candidato confirmado a cobertura (escalonamiento obligatorio: solo PARTIAL_HEDGE en primera pata)
        elif state == "HEDGE_CANDIDATE" and not has_hedge:
            if pred_sig.get("direct_close_preferred", False):
                return 6, "CLOSE_ORIGINAL_POSITION"
            if int(pair_ctx.get("hedges_opened_count", 0)) >= cls.MAX_HEDGES_PER_PAIR:
                return 0, "NO_HEDGE"
            if pred_sig.get("recommend_hedge", False):
                valid_ids = [0, 1]
                heuristic_pref = 1
            else:
                return 0, "NO_HEDGE"

        # 3. Cobertura activa / ajuste
        elif state in ("HEDGE_ACTIVE", "HEDGE_ADJUST") and has_hedge:
            if pred_sig.get("direct_close_preferred", False) and float(edge_info.get("pnl_atr", 0.0)) <= -0.95:
                return 6, "CLOSE_ORIGINAL_POSITION"
            if float(pred_sig.get("adverse_continuation_prob", 0.0)) >= 0.72 and (eff_ratio < 0.72 or hedge_pl > 30):
                valid_ids = [0, 2]
                heuristic_pref = 2
            elif hedge_pl >= 50 and float(edge_info.get("edge_delta", 0.0)) > 0.05:
                valid_ids = [0, 3]
                heuristic_pref = 3
            else:
                valid_ids = [0, 2]
                heuristic_pref = 0

        # 4. Recuperación / Salida de cobertura (prohibir desarme prematuro a pérdida)
        elif state in ("RECOVERY", "HEDGE_EXIT") and has_hedge:
            hedge_notional = max(1000, int(pair_ctx.get("hedge_notional_sats", 1000)))
            hedge_rt_fee = max(2, int(round(hedge_notional * 0.0020)))
            if state == "RECOVERY":
                if hedge_pl < hedge_rt_fee and comb_net < 25 and float(edge_info.get("edge_score", 0.0)) < 0.18:
                    return 0, "NO_HEDGE"
                valid_ids = [0, 3, 4]
                heuristic_pref = 4 if (comb_net > 25 or hedge_pl >= hedge_rt_fee) else 0
            else:
                valid_ids = [3, 4]
                heuristic_pref = 3 if (hedge_pl >= max(40, hedge_rt_fee * 2) and comb_net < 25) else 4
        else:
            valid_ids = [0, 5, 6] if not has_hedge else [0, 2, 3, 4, 6]
            heuristic_pref = 0

        # Selección asistida por Q-table independiente entre acciones válidas con desempate heurístico
        best_id = max(
            valid_ids,
            key=lambda idx: float(q_vals[idx]) + (0.05 if idx == heuristic_pref else 0.0),
        )
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
        is_exit_transition = ("HEDGE_EXIT" in hedge_state_key) or (state == "HEDGE_EXIT")
        if prev_h_key is not None:
            last_fee_paid = int(pair_ctx.get("last_action_fee_sats", 0))
            avoided_sl = (
                max(0, int(pred_sig.get("expected_saved_sats", 0)))
                if pair_econ.get("has_active_hedge", False)
                and (
                    int(pair_econ.get("hedge_pl_sats", 0)) > 0
                    or (prev_h_act in (1, 2) and bool(pred_sig.get("recommend_hedge", False)))
                )
                else 0
            )
            r_h = cls.compute_hedge_reward(
                prev_net_pl,
                curr_net_pl,
                prev_h_act,
                fee_paid_sats=last_fee_paid,
                avoided_sl_sats=avoided_sl,
                margin_sats=pair_ctx.get("orig_margin_sats", 1000),
            )
            hedge_store.update_hedge_q(
                prev_h_key, prev_h_act, r_h, hedge_state_key, done=is_exit_transition
            )

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
            pair_ctx["last_action_fee_sats"] = 0
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
            if exec_res.get("executed"):
                reason_str = str(exec_res.get("reason", ""))
                if act_name in ("PARTIAL_HEDGE", "INCREASE_HEDGE") and "OPENED_" in reason_str:
                    est_rf = int(approved_params.get("est_round_fee_sats", 0))
                    pair_ctx["last_action_fee_sats"] = max(1, est_rf // 2)
                    curr_net_pl -= est_rf
                elif act_name in ("CLOSE_HEDGE", "CLOSE_ORIGINAL_POSITION") or "CLOSED_" in reason_str:
                    pair_ctx["last_action_fee_sats"] = max(1, int(pair_econ.get("combined_exit_fees_sats", 2)) // 4)
                else:
                    pair_ctx["last_action_fee_sats"] = 0
            else:
                pair_ctx["last_action_fee_sats"] = 0

        if is_exit_transition and not pair_econ.get("has_active_hedge", False):
            pair_ctx["last_hedge_state_key"] = None
            pair_ctx["last_hedge_action"] = 0
        else:
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
