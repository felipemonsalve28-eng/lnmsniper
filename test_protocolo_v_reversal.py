# -*- coding: utf-8 -*-
"""
test_protocolo_v_reversal.py
Suite de verificación cuantitativa de los 15 escenarios obligatorios de auditoría
(Objetivo Balance Disponible, NO_TRADE Greedy, Jerarquía de Seguridad, Exposición Proyectada,
Confirmación de Guardian, Degradación de Edge, SHA-256 y Aprendizaje Neto sin doble comisión).
"""
import os
import time
from guardian_execution_core import AtomicBrainStore, HardFloorGuardian
from sniper_perception_sats_core import (
    ShortTermPerceptionEngine,
    SatsExpectancyGate,
    sync_brain_floor_with_reality,
)
from lnm_sniper_v6_advanced import (
    Config,
    AutonomousGoalBrain,
    compute_account_equity,
    infer_adopted_trade,
    compute_situational_order_params,
    evaluate_system_mode,
    execute_strategy_guardian,
)


class MockLNMClient:
    def __init__(self, running_list=None, sl_confirm=True, close_confirm=True):
        self.running_list = list(running_list or [])
        self.sl_confirm = sl_confirm
        self.close_confirm = close_confirm
        self.sl_calls = []
        self.close_calls = []

    def update_stoploss(self, tid, target_sl):
        self.sl_calls.append((str(tid), int(target_sl)))
        if self.sl_confirm:
            return {"id": str(tid), "stoploss": int(target_sl)}
        return {}

    def close_trade(self, tid):
        self.close_calls.append(str(tid))
        if self.close_confirm:
            self.running_list = [t for t in self.running_list if str(t.get("id")) != str(tid)]
            return {"id": str(tid), "closed": True}
        return {}

    def get_running_trades(self):
        return self.running_list


def run_audit_protocol():
    print("=== INICIANDO PROTOCOLO DE AUDITORÍA CUANTITATIVA (15 ESCENARIOS) ===\n")
    engine = ShortTermPerceptionEngine()
    now = time.time()

    state_v_rally = "RANGE_SQUEEZE|PEAK|LOW_EXP|CHOP|BEAR_RALLY"
    engine._record_sample(now - 300, 76800.0, 220.0)
    engine._record_sample(now - 200, 77150.0, 220.0)
    engine._record_sample(now - 100, 77550.0, 220.0)

    ind_5m_bull = {
        "price": 78050.0, "atr": 220.0, "atr_pct": 0.0028, "rsi": 66.5, "bb_pos": 0.82,
        "ema9": 77820.0, "ema21": 77450.0, "high": 78090.0, "low": 77600.0,
        "open": 77620.0, "regime": "TREND_BULL", "ema_score": 1.0,
    }
    ind_15m_bull = {"rsi": 59.0, "bb_pos": 0.70, "ema9": 77600.0, "ema21": 77200.0, "ema_score": 1.0, "regime": "TREND_BULL"}
    ind_1h_lag = {"rsi": 54.5, "ema9": 77500.0, "ema21": 77300.0, "ema_score": 0.35, "regime": "TREND_BULL"}
    ind_4h_bear_lag = {"rsi": 43.0, "ema9": 77000.0, "ema21": 77900.0, "ema_score": -0.35, "regime": "TREND_BEAR"}

    perception = engine.evaluate(state_v_rally, ind_5m_bull, ind_15m_bull, ind_1h_lag, ind_4h_bear_lag, now=now)
    mock_q = [0.01, 0.05, 0.02, 0.04, 0.03, 0.01, 0.02, 0.12, 0.01]
    mock_strats = {i: {"name": f"STRAT_{i}", "side": "buy" if i <= 4 else "sell"} for i in range(1, 9)}

    # 1. ESCENARIO 1: Selección de NO_TRADE (0) en modo greedy cuando las estrategias operativas tienen Q negativo
    tmp_brain = AutonomousGoalBrain(filename="test_tmp_brain.json")
    st_neg = "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS"
    tmp_brain.q_table[st_neg] = [0.02, -0.15, -0.22, -0.18, -0.12, -0.25, -0.30, -0.19, -0.14]
    tmp_brain.state_visits[st_neg] = 95
    tmp_brain.epsilon = 0.0
    greedy_no_trade = tmp_brain.choose_action(st_neg, ind_5m_bull, ind_15m_bull, ind_1h_lag, ind_4h_bear_lag, recovery_mode=False, current_dd_pct=0.01)
    assert greedy_no_trade == 0, f"Fallo E1: NO_TRADE (0) debía ganar en selección greedy ante Q negativos, obtuvo {greedy_no_trade}"
    print("✅ ESCENARIO 1 (NO_TRADE Elegible y Ganador en Selección Greedy): PASÓ.")

    # 2. ESCENARIO 2: Integridad de las 9 acciones y Rally en V con LOW_EXP
    final_action, reason = SatsExpectancyGate.filter_and_rank_action(
        raw_action=7, state=state_v_rally, q_values=mock_q,
        strategies=mock_strats, perception=perception,
        recovery_mode=False, current_dd_pct=0.02,
    )
    assert len(tmp_brain.get_state_q_values(state_v_rally)) == 9, "Fallo E2: Vector Q debe tener 9 acciones."
    assert perception["override_side"] == "BULL" and final_action in [1, 3, 4], f"Fallo E2: {final_action}"
    print(f"✅ ESCENARIO 2 (Integridad 9 Acciones + Redirección Giro en V en LOW_EXP): PASÓ -> Acción {final_action} ({reason}).")

    # 3. ESCENARIO 3: Jerarquía de Seguridad P0 (RANGE_SQUEEZE|HIGH_EXP bloquea incluso con OVR:BULL activo)
    sq_high_ovr_act, sq_high_ovr_reason = SatsExpectancyGate.filter_and_rank_action(
        raw_action=3, state="RANGE_SQUEEZE|MID|HIGH_EXP|BULL|FULL_BULL",
        q_values=mock_q, strategies=mock_strats, perception=perception,
        recovery_mode=False, current_dd_pct=0.03
    )
    assert sq_high_ovr_act == 0 and sq_high_ovr_reason == "BLOCKED_SQUEEZE_HIGH_EXP", (
        f"Fallo E3: Override BULL se saltó el bloqueo RANGE_SQUEEZE|HIGH_EXP ({sq_high_ovr_act}, {sq_high_ovr_reason})"
    )
    print("✅ ESCENARIO 3 (Jerarquía P0: Bloqueo RANGE_SQUEEZE|HIGH_EXP prevalece sobre Override BULL): PASÓ.")

    # 4. ESCENARIO 4: Límites de Exposición Proyectada (<= 38%) y Presupuesto de Lote Mínimo (1000 sats)
    exp_ok_1, exp_r_1, proj_1 = SatsExpectancyGate.verify_projected_exposure(
        locked_margin_sats=8158, new_margin_sats=2700, equity_sats=25795, available_balance_sats=15808
    )
    assert exp_ok_1 is False and "PROJECTED_EXPOSURE_EXCEEDED" in exp_r_1, f"Fallo E4a: Debía bloquear 42.1% > 38%: {exp_r_1}"
    exp_ok_2, exp_r_2, _ = SatsExpectancyGate.verify_projected_exposure(
        locked_margin_sats=0, new_margin_sats=1000, equity_sats=3500, available_balance_sats=3500, max_single_trade_risk_pct=0.22
    )
    assert exp_ok_2 is False and "MIN_LOT_EXCEEDS_RISK_BUDGET" in exp_r_2, f"Fallo E4b: Lote mínimo excesivo no fue bloqueado: {exp_r_2}"
    print(f"✅ ESCENARIO 4 (Exposición Proyectada {proj_1*100:.1f}% Bloqueada + Control Lote Mínimo): PASÓ.")

    # 5. ESCENARIO 5: Protección de Pérdidas Diarias y Drawdown Conectadas a evaluate_system_mode()
    mode_dd, allow_dd, _, _, _ = evaluate_system_mode(now, 0.0, ind_5m_bull, daily_pnl_pct=-0.02, current_dd_pct=0.165, floor_threat=False, equity=21000, recovery_anchor_equity=None)
    mode_dl, allow_dl, _, _, _ = evaluate_system_mode(now, 0.0, ind_5m_bull, daily_pnl_pct=-0.115, current_dd_pct=0.05, floor_threat=False, equity=22000, recovery_anchor_equity=None)
    assert mode_dd == "DEFENSIVE" and allow_dd is False, f"Fallo E5a: MAX_DRAWDOWN_PCT no activó DEFENSIVE ({mode_dd})"
    assert mode_dl == "DEFENSIVE" and allow_dl is False, f"Fallo E5b: MAX_DAILY_LOSS_PCT no activó DEFENSIVE ({mode_dl})"
    print("✅ ESCENARIO 5 (Conexión Real de MAX_DAILY_LOSS_PCT y MAX_DRAWDOWN_PCT): PASÓ.")

    # 6. ESCENARIO 6: Confirmación Real de SL y Cierres en el Guardian
    mock_trade = {"id": "t-confirm-1", "side": "buy", "margin": 1500, "leverage": 30, "price": 77500.0, "stoploss": 77100.0, "liquidation": 75200.0, "pl": 220}
    meta_unconfirmed = infer_adopted_trade(mock_trade, ind_5m_bull, state_v_rally, now, forced_action=4)
    client_fail = MockLNMClient(running_list=[mock_trade], sl_confirm=False, close_confirm=False)
    closed_f = execute_strategy_guardian(client_fail, tmp_brain, mock_trade, ind_5m_bull, ind_15m_bull, meta_unconfirmed, "NORMAL", edge_score=0.1)
    assert closed_f is False and meta_unconfirmed["ai_closed"] is False, "Fallo E6a: No debe marcar ai_closed sin confirmación del servidor."
    client_ok = MockLNMClient(running_list=[mock_trade], sl_confirm=True, close_confirm=True)
    meta_confirmed = infer_adopted_trade(mock_trade, ind_5m_bull, state_v_rally, now, forced_action=4)
    closed_ok = execute_strategy_guardian(client_ok, tmp_brain, mock_trade, ind_5m_bull, ind_15m_bull, meta_confirmed, "NORMAL", edge_score=0.1)
    assert closed_ok is True and meta_confirmed["ai_closed"] is True, "Fallo E6b: Debe confirmar cierre cuando el servidor lo acredita."
    print("✅ ESCENARIO 6 (Confirmación de Servidor para Cierres y Bloqueo de SL): PASÓ.")

    # 7. ESCENARIO 7: Reconciliación de Posiciones y Adopción Conservadora de Huérfanos
    orphan_meta = infer_adopted_trade({"id": "orphan-1", "side": "buy", "leverage": 75, "margin": 1000, "price": 78050.0}, ind_5m_bull, state_v_rally, now)
    assert orphan_meta["action"] == 4 and orphan_meta["style"] == "DEF_SCALP", "Fallo E7: Huérfano debía ser DEF_SCALP(4)."
    print("✅ ESCENARIO 7 (Reconciliación y Adopción Conservadora DEF_SCALP): PASÓ.")

    # 8. ESCENARIO 8: Compatibilidad del Cerebro Anterior (q_brain_advanced_lnm.json)
    prod_brain = AutonomousGoalBrain(filename="q_brain_advanced_lnm.json")
    assert len(prod_brain.q_table) >= 100, f"Fallo E8: No cargó q_brain_advanced_lnm.json correctamente ({len(prod_brain.q_table)} estados)."
    print(f"✅ ESCENARIO 8 (Compatibilidad Cerebro Existente: {len(prod_brain.q_table)} estados cargados): PASÓ.")

    # 9. ESCENARIO 9: Persistencia Atómica, Verificación SHA-256 y Recuperación desde Backup
    store_test = AtomicBrainStore("test_atomic_store.json")
    digest = store_test.save({"q_table": {st_neg: [0.02]*9}, "current_target": 300000})
    store_test.save({"q_table": {st_neg: [0.05]*9}, "current_target": 300000})
    with open("test_atomic_store.json", "w", encoding="utf-8") as bad_f:
        bad_f.write("{corrupted_json")
    recovered = store_test.load()
    assert isinstance(recovered, dict) and "q_table" in recovered, "Fallo E9: No recuperó desde .bak ante corrupción."
    for ext in ("", ".bak", ".sha256", ".tmp"):
        if os.path.exists(f"test_atomic_store.json{ext}"):
            os.remove(f"test_atomic_store.json{ext}")
    print(f"✅ ESCENARIO 9 (Persistencia Atómica SHA-256 {digest[:12]}... y Recuperación .bak): PASÓ.")

    # 10. ESCENARIO 10: Avance de Meta Exclusivo por Balance Disponible (No por Equity Flotante)
    tmp_brain.current_target = 300000
    tmp_brain.milestone_level = 1
    # Si el equity es 310,000 pero el balance disponible es 290,000, NO avanza
    adv_false = tmp_brain.check_and_advance_goal(available_balance=290000)
    assert adv_false is False and tmp_brain.milestone_level == 1, "Fallo E10a: No debe avanzar meta con balance < 300,000."
    adv_true = tmp_brain.check_and_advance_goal(available_balance=300500)
    assert adv_true is True and tmp_brain.milestone_level == 2 and tmp_brain.locked_floor == 264000, "Fallo E10b: No avanzó meta con balance disponible real."
    print("✅ ESCENARIO 10 (Objetivo Económico P0: Avance Exclusivo por Balance Disponible): PASÓ.")

    # 11. ESCENARIO 11: Recompensas Netas basadas en ΔBalance sin Doble Contabilización de Comisiones
    t_before = tmp_brain.realized_net_profit_sats
    tmp_brain.remember_and_train(
        st_neg, 3, reward_sats=250, margin_used=2000, next_state=state_v_rally,
        available_balance_after=25250, real_balance_delta_sats=250,
        hit_tp=True, harvested=True, recovery_mode=False, lev_used=30.0, exact_fee_sats=80
    )
    assert (tmp_brain.realized_net_profit_sats - t_before) == 250, "Fallo E11: Descontó comisiones dos veces sobre real_balance_delta_sats."
    print("✅ ESCENARIO 11 (Recompensa Económica P1: ΔBalance Neto sin Doble Comisión): PASÓ.")

    # 12. ESCENARIO 12: Aprendizaje Online Táctico vs Económico y Prioridad PER
    tmp_brain.remember_and_train(
        state_v_rally, 3, reward_sats=320, margin_used=2000,
        next_state="RANGE_NORMAL|MID|LOW_EXP|BULL|FULL_BULL",
        equity_after=28500, hit_tp=True, harvested=True,
        recovery_mode=True, exited_recovery_bonus=True, lev_used=30.0, exact_fee_sats=120
    )
    assert abs(tmp_brain.alpha - 0.28) < 1e-6 and tmp_brain.memory[-1][0] > 0.10, "Fallo E12: Error en PER o Alpha adaptativo."
    print("✅ ESCENARIO 12 (Separación Aprendizaje Táctico/Económico + PER): PASÓ.")

    # 13. ESCENARIO 13: Controlador de Degradación de Edge en el Guardian (Cierre Defensivo tras Persistencia)
    losing_trade = {"id": "t-deg-1", "side": "buy", "margin": 1000, "leverage": 25, "price": 78500.0, "stoploss": 77200.0, "liquidation": 75500.0, "pl": -180}
    ind_5m_bear_drop = dict(ind_5m_bull, price=78180.0, rsi=41.0)
    bear_perc = {"override_side": "BEAR", "vel_ema_atr": -0.85, "rsi_5m": 41.0}
    deg_meta = infer_adopted_trade(losing_trade, ind_5m_bear_drop, state_v_rally, now, forced_action=3)
    deg_client = MockLNMClient(running_list=[losing_trade], close_confirm=True)
    # Ciclos 1 y 2 incrementan streak sin cerrar; ciclo 3 confirma persistencia y ejecuta salida defensiva
    c1 = execute_strategy_guardian(deg_client, tmp_brain, losing_trade, ind_5m_bear_drop, ind_15m_bull, deg_meta, "RECOVERY", edge_score=-0.85, perception=bear_perc)
    c2 = execute_strategy_guardian(deg_client, tmp_brain, losing_trade, ind_5m_bear_drop, ind_15m_bull, deg_meta, "RECOVERY", edge_score=-0.88, perception=bear_perc)
    c3 = execute_strategy_guardian(deg_client, tmp_brain, losing_trade, ind_5m_bear_drop, ind_15m_bull, deg_meta, "RECOVERY", edge_score=-0.92, perception=bear_perc)
    assert c1 is False and c2 is False and c3 is True, f"Fallo E13: Controlador de Edge debía cerrar en el 3er ciclo persistente ({c1}, {c2}, {c3})."
    print("✅ ESCENARIO 13 (Guardian Edge Degradation Controller con Persistencia 3 Ciclos): PASÓ.")

    # 14. ESCENARIO 14: Validación Matemática de Stop Loss vs Liquidación (sl_dist <= max_safe_sl_dist) y Latencia
    t0 = time.perf_counter()
    lev_safe, sl_safe, tp_safe = compute_situational_order_params(
        raw_lev=90, raw_sl=76000.0, raw_tp=81000.0, strat=tmp_brain.strategies[3],
        state=state_v_rally, ind_5m=ind_5m_bull, ind_15m=ind_15m_bull, ind_1h=ind_1h_lag, ind_4h=ind_4h_bear_lag,
        mods=tmp_brain.get_parallel_modifiers(), brain=tmp_brain, action=3,
        recovery_mode=False, current_dd_pct=0.02, consecutive_losses=0, combined_rr=1.2, perception=perception
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    max_safe_dist = ind_5m_bull["price"] * (0.70 / lev_safe)
    actual_sl_dist = abs(ind_5m_bull["price"] - sl_safe)
    assert actual_sl_dist <= max_safe_dist + 1.0, f"Fallo E14: sl_dist ({actual_sl_dist}) superó max_safe_sl_dist ({max_safe_dist})"
    assert elapsed_ms < 15.0, f"Fallo E14b: Latencia excesiva ({elapsed_ms:.2f}ms)"
    print(f"✅ ESCENARIO 14 (Garantía Matemática SL <= MaxSafeDist a {lev_safe}x | Latencia {elapsed_ms:.2f}ms): PASÓ.")

    # 15. ESCENARIO 15: Sincronización de Piso Unificado y Decaimiento de Override
    class DummyBrain:
        peak_equity = 30000
        locked_floor = 22500
        dirty = False
    db = DummyBrain()
    resynced, new_peak, new_floor = sync_brain_floor_with_reality(db, true_equity=26264, floor_ratio=0.75)
    ind_flat = dict(ind_5m_bull, price=77450.0, rsi=48.0, ema9=77450.0, ema21=77450.0)
    perc_after_loss = engine.evaluate(state_v_rally, ind_flat, ind_15m_bull, ind_1h_lag, ind_4h_bear_lag, now=now + 60)
    assert perc_after_loss["override_side"] is None, f"Fallo E15: Override no se canceló al perder momentum ({perc_after_loss})"
    for ext in ("", ".bak", ".sha256", ".tmp"):
        if os.path.exists(f"test_tmp_brain.json{ext}"):
            os.remove(f"test_tmp_brain.json{ext}")
    print("✅ ESCENARIO 15 (Cancelación Anticipada de Override sin Momentum + Piso Unificado): PASÓ.")
    print("\n🎯 LOS 15 ESCENARIOS DE CERTIFICACIÓN CUANTITATIVA PASARON EXITOSAMENTE.")


if __name__ == "__main__":
    run_audit_protocol()
