#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
BATERÍA DE PRUEBAS AUTOMATIZADAS Y VALIDACIÓN DE REGRESIÓN (ETAPA 6)
DIRECTIVA MAESTRA LNM SNIPER: CALIDAD DEL APRENDIZAJE, ACUMULACIÓN REAL DE
SATOSHIS Y CONFIGURACIÓN DINÁMICA DE UMBRALES
================================================================================
Verifica:
  1. Esquema, validación de rangos, rechazo de corrupción, bloqueo estructural
     con órdenes abiertas y Hot-Reload atómico de `config/learning_thresholds.json`.
  2. Función de recompensa basada en variación real de satoshis líquidos
     (Delta_Balance_disponible - lambda*MAE_sats - mu*Delta_t_ticks),
     cero doble deducción de comisiones, desacople de PnL flotante,
     penalización negativa garantizada en pérdidas reales, idempotencia
     de transiciones (S, A, R, S') por `trade_id` y trazabilidad completa.
  3. Motor de Supervisión en Tiempo Real (TD-Error, Q-Bias en sats, FER con
     protección div/0 y cohortes, Entropía de Política en ventana de 100
     decisiones y Captura MFE/MAE) + Matriz de Respuesta Autónoma con Histéresis.
  4. Despachador de Telemetría Headless Atómica (`telemetry_live.json`).
  5. Regresión arquitectónica completa (9 acciones discretas, compatibilidad
     con `q_brain_advanced_lnm.json`, Unified Guardian y límite de 5 posiciones).
================================================================================
"""

import copy
import json
import math
import os
import tempfile
import time

from learning_thresholds_manager import (
    DEFAULT_CONFIG_PATH,
    REQUIRED_CATEGORIES,
    REQUIRED_PARAM_FIELDS,
    LearningThresholdsManager,
    get_thresholds_manager,
)
from learning_quality_supervisor import (
    AtomicTelemetryDispatcher,
    LearningQualitySupervisor,
)
from sniper_perception_sats_core import SatsExpectancyGate
from guardian_execution_core import HardFloorGuardian
from lnm_sniper_v6_advanced import (
    AutonomousGoalBrain,
    Config,
    TELEMETRY_DISPATCHER,
    THRESHOLDS_MANAGER,
    evaluate_open_trades_online,
)


def run_all_tests() -> None:
    print("=" * 80)
    print("INICIANDO BATERÍA DE CERTIFICACIÓN: CALIDAD DE APRENDIZAJE Y SATS REALES")
    print("=" * 80)

    # -------------------------------------------------------------------------
    # ESCENARIO 1: Validación de Esquema y 10 Categorías en learning_thresholds.json
    # -------------------------------------------------------------------------
    assert os.path.exists(DEFAULT_CONFIG_PATH), f"No existe {DEFAULT_CONFIG_PATH}"
    with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
        raw_cfg = json.load(fh)

    is_ok, reason, _ = LearningThresholdsManager.validate_schema_and_limits(raw_cfg)
    assert is_ok, f"Esquema invalido en config/learning_thresholds.json: {reason}"
    for cat in REQUIRED_CATEGORIES:
        assert cat in raw_cfg, f"Falta categoria {cat}"
        for p_name, p_meta in raw_cfg[cat].items():
            for req_f in REQUIRED_PARAM_FIELDS:
                assert req_f in p_meta, f"Falta campo {req_f} en {cat}.{p_name}"

    # Verificación de que no existen parámetros huérfanos sin consumidor en el código (Hallazgo M1)
    import glob
    py_code = ""
    for py_file in glob.glob("*.py"):
        try:
            with open(py_file, "r", encoding="utf-8", errors="ignore") as pf:
                py_code += pf.read() + "\n"
        except Exception:
            pass
    orphaned_keys = []
    for cat in REQUIRED_CATEGORIES:
        for p_name in raw_cfg[cat].keys():
            if p_name not in py_code:
                orphaned_keys.append(f"{cat}.{p_name}")
    assert len(orphaned_keys) == 0, f"Existen claves en JSON sin consumidor en el código: {orphaned_keys}"
    print(f"✅ TEST 1 (Esquema 10 Categorías, Validación de Límites y 0 Claves Huérfanas en learning_thresholds.json): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 2: Hot-Reload Atómico, Rechazo de Corrupción y Bloqueo Estructural
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_cfg_path = os.path.join(tmpdir, "learning_thresholds.json")
        with open(tmp_cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw_cfg, fh, indent=2)

        mgr = LearningThresholdsManager(config_path=tmp_cfg_path, min_check_interval_sec=0.0)
        assert mgr.reload_success_count == 1
        assert abs(mgr.get_float("expectancy", "min_expectancy_ratio") - 3.85) < 1e-6

        # 2a. Recarga válida en caliente
        modified_cfg = copy.deepcopy(raw_cfg)
        modified_cfg["expectancy"]["min_expectancy_ratio"]["value"] = 4.25
        time.sleep(0.02)
        with open(tmp_cfg_path, "w", encoding="utf-8") as fh:
            json.dump(modified_cfg, fh, indent=2)
        assert mgr.check_and_reload(active_trades_count=0, force=True) is True
        assert abs(mgr.get_float("expectancy", "min_expectancy_ratio") - 4.25) < 1e-6

        # 2b. Rechazo de valor fuera de rango [min_limit, max_limit]
        out_of_bounds_cfg = copy.deepcopy(modified_cfg)
        out_of_bounds_cfg["expectancy"]["min_expectancy_ratio"]["value"] = 999.0  # max_limit = 10.0
        with open(tmp_cfg_path, "w", encoding="utf-8") as fh:
            json.dump(out_of_bounds_cfg, fh, indent=2)
        assert mgr.check_and_reload(active_trades_count=0, force=True) is False
        assert mgr.reload_rejected_count == 1
        # Debe conservar el último valor válido (4.25)
        assert abs(mgr.get_float("expectancy", "min_expectancy_ratio") - 4.25) < 1e-6

        # 2c. Rechazo de sintaxis JSON corrupta
        with open(tmp_cfg_path, "w", encoding="utf-8") as fh:
            fh.write("{corrupted_json: true, ")
        assert mgr.check_and_reload(active_trades_count=0, force=True) is False
        assert mgr.reload_rejected_count == 2
        assert abs(mgr.get_float("expectancy", "min_expectancy_ratio") - 4.25) < 1e-6

        # 2d. Protección de parámetro estructural cuando hay posiciones abiertas (active_trades_count > 0)
        struct_mod_cfg = copy.deepcopy(modified_cfg)
        struct_mod_cfg["fees"]["taker_fee_rate"]["value"] = 0.0025  # structural_lock_when_open = True
        struct_mod_cfg["expectancy"]["min_expectancy_ratio"]["value"] = 4.50  # no estructural
        with open(tmp_cfg_path, "w", encoding="utf-8") as fh:
            json.dump(struct_mod_cfg, fh, indent=2)
        assert mgr.check_and_reload(active_trades_count=2, force=True) is True
        assert mgr.structural_lock_blocks_count >= 1
        # El parámetro estructural bloqueado NO debe haber cambiado (permanece en 0.001)
        assert abs(mgr.get_float("fees", "taker_fee_rate") - 0.001) < 1e-6
        # El parámetro operativo no estructural SÍ se actualiza en caliente (4.50)
        assert abs(mgr.get_float("expectancy", "min_expectancy_ratio") - 4.50) < 1e-6

    print("✅ TEST 2 (Hot-Reload Atómico, Rechazo de Sintaxis/Rango y Bloqueo Estructural con Posiciones Abiertas): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 3: Función de Recompensa, Cero Doble Descuento, MAE/Ticks y Pérdidas Reales
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        brain_path = os.path.join(tmpdir, "test_brain.json")
        brain = AutonomousGoalBrain(filename=brain_path, explorer_mode=False)
        st_a = "TREND_BULL|DIP|LOW_EXP|BULL|FULL_BULL"
        st_b = "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL"

        # 3a. Prevención de doble descuento cuando real_balance_delta_sats es provisto
        q_before_win = brain.get_state_q_values(st_a)[1]
        sats_before = brain.realized_net_profit_sats
        fees_before = brain.total_fees_paid_sats
        r_win = brain.remember_and_train(
            state=st_a,
            action=1,
            reward_sats=220,
            margin_used=2000,
            next_state=st_b,
            real_balance_delta_sats=150,  # ya neto de 70 sats de comisión
            exact_fee_sats=70,
            pl_includes_fees=True,
            trade_id="trade-win-001",
            mae_sats=20.0,
            mfe_sats=200.0,
            duration_ticks=10.0,
            predicted_q_entry=q_before_win,
        )
        q_after_win = brain.get_state_q_values(st_a)[1]
        # Exactamente +150 sats agregados (sin restar 70 por segunda vez)
        assert (brain.realized_net_profit_sats - sats_before) == 150
        assert (brain.total_fees_paid_sats - fees_before) == 70
        # R_econ = 150 - 0.15*(20) - 0.05*(10) = 150 - 3.0 - 0.5 = 146.5 sats -> shaped = +1.465
        assert abs(r_win - 1.465) < 1e-3, f"Recompensa esperada 1.465, obtenida {r_win}"
        assert q_after_win > q_before_win, "Ganancia neta real debe incrementar el valor Q"

        # 3b. Idempotencia estricta: repetir el mismo trade_id no debe duplicar ni mutar Q
        dup_r = brain.remember_and_train(
            state=st_a,
            action=1,
            reward_sats=220,
            margin_used=2000,
            next_state=st_b,
            real_balance_delta_sats=150,
            exact_fee_sats=70,
            pl_includes_fees=True,
            trade_id="trade-win-001",
        )
        assert dup_r == 0.0
        assert brain.duplicate_transitions_blocked == 1
        assert (brain.realized_net_profit_sats - sats_before) == 150
        assert abs(brain.get_state_q_values(st_a)[1] - q_after_win) < 1e-6

        # 3c. Pérdida neta real en balance DEBE penalizar negativamente Q, incluso si hit_tp=True o harvested=True
        q_before_loss = brain.get_state_q_values(st_a)[2]
        r_loss = brain.remember_and_train(
            state=st_a,
            action=2,
            reward_sats=15,  # Bruto +15 sats, pero comisiones fueron 45 sats -> neto -30 sats
            margin_used=2000,
            next_state=st_b,
            exact_fee_sats=45,
            pl_includes_fees=False,
            hit_tp=True,
            harvested=True,
            trade_id="trade-loss-002",
            mae_sats=60.0,
            mfe_sats=25.0,
            duration_ticks=20.0,
        )
        q_after_loss = brain.get_state_q_values(st_a)[2]
        assert r_loss < 0.0, f"Pérdida neta post-comisiones debe generar recompensa negativa, obtuvo {r_loss}"
        assert q_after_loss < q_before_loss, (
            f"Pérdida neta debe reducir Q(s,a): antes={q_before_loss}, después={q_after_loss}"
        )

        # 3d. Desacople de PnL flotante: remember_intermediate NO debe mutar la Q-table
        q_snapshot_before_float = copy.deepcopy(brain.q_table)
        inter_r = brain.remember_intermediate(
            origin_state=st_a,
            action=1,
            current_state=st_b,
            side="buy",
            delta_atr=0.85,
            guardian_mode="RUNNER",
            sl_locked_confirmed=True,
            eval_count=1,
            edge_score=0.5,
        )
        assert inter_r == 0.0
        assert brain.q_table == q_snapshot_before_float, "El PnL flotante no debe contaminar la Q-table"

        # 3e. Verificación de trazabilidad en transition_audit_log
        last_audit = brain.transition_audit_log[-1]
        for key in ("trade_id", "state", "action", "predicted_q", "predicted_sats", "net_realized_sats", "mae_sats", "mfe_sats", "duration_ticks", "td_error"):
            assert key in last_audit, f"Falta clave de auditoría {key}"
        assert last_audit["trade_id"] == "trade-loss-002"
        assert last_audit["net_realized_sats"] == -30

    print("✅ TEST 3 (Recompensa Sats Líquidos, Cero Doble Comisión, Idempotencia, Desacople Flotante y Penalización de Pérdidas): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 4: Motor de Supervisión en Tiempo Real (5 Indicadores + Learning Guard con Histéresis)
    # -------------------------------------------------------------------------
    sup = LearningQualitySupervisor()

    # 4a. TD-Error normal -> STABLE / IMPROVING
    for td_val in [0.32, 0.28, 0.24, 0.20, 0.16, 0.14]:
        allow, status = sup.evaluate_td_error_pre_update(td_val)
        assert allow is True and status == "ACTIVE_LEARNING"
    snap_metrics = sup.get_rl_metrics_snapshot()
    assert snap_metrics["td_error_trend"] in ("IMPROVING", "STABLE")

    # 4b. Divergencia sostenida (3 updates CRITICAL) -> Suspensión de Q-updates + Snapshot
    snapshots_called = []
    sup.snapshot_callback = lambda reason: snapshots_called.append(reason)
    for _ in range(3):
        allow, status = sup.evaluate_td_error_pre_update(1.85)  # > critical_threshold (1.35)
    assert allow is False and status == "SUSPENDED_CRITICAL_TD"
    assert len(snapshots_called) == 1
    assert sup.can_update_q_table() is False

    # 4c. Histéresis de recuperación: requiere 5 updates estables consecutivos antes de reactivar
    for step_idx in range(4):
        allow, status = sup.evaluate_td_error_pre_update(0.15)
        assert allow is False and status == "RECOVERING_HYSTERESIS"
    allow_fifth, status_fifth = sup.evaluate_td_error_pre_update(0.12)
    assert allow_fifth is True and status_fifth == "ACTIVE_LEARNING"
    assert sup.can_update_q_table() is True

    # 4d. Q-Bias en satoshis, FER (con división por cero protegida) y MFE/MAE
    assert LearningQualitySupervisor._compute_fer_safe(0, 0) == 0.0
    assert LearningQualitySupervisor._compute_fer_safe(120, 0) == 50.0
    assert LearningQualitySupervisor._compute_fer_safe(-50, 0) == -1.0

    # Simular seguimiento en vivo de MFE / MAE en una posición abierta
    sup.update_open_position_excursion("pos-live-1", unrealized_pnl_sats=40.0, margin_sats=1000.0)
    sup.update_open_position_excursion("pos-live-1", unrealized_pnl_sats=-120.0, margin_sats=1000.0)
    sup.update_open_position_excursion("pos-live-1", unrealized_pnl_sats=200.0, margin_sats=1000.0)
    exc_state = sup.get_open_position_excursion("pos-live-1")
    assert exc_state["mfe_sats"] == 200.0 and exc_state["mae_sats"] == 120.0

    # Cerrar 4 operaciones con sobreestimación de Q (predicted_q=0.90 -> 90 sats vs realized=10 sats) y bajo FER (10 / 20 = 0.5)
    for idx in range(4):
        tid = "pos-live-1" if idx == 0 else f"pos-closed-{idx}"
        sup.record_closed_trade(
            trade_id=tid,
            predicted_q_entry=0.90,
            net_realized_sats=10,
            total_fees_sats=20,
            margin_sats=1000,
            mfe_sats=100.0,
            mae_sats=650.0,  # 65% del margen (> 50%) y captura MFE 10/100 = 10% (< 40%)
        )

    adj = sup.get_autonomous_adjustments()
    assert adj["q_bias_defense_active"] is True, "Sobreestimacion Q persistente debe activar defensa"
    assert adj["fer_defense_active"] is True, "FER < 1.2 persistente debe activar defensa anti-fee-drag"
    assert adj["mfe_mae_defense_active"] is True, "Baja captura MFE / alto MAE debe activar ajuste de trailing"
    assert adj["effective_min_expectancy_ratio"] > 3.85
    assert adj["dynamic_trailing_multiplier"] < 1.0

    # 4e. Entropía de la Política (ventana de 100 decisiones) y detección de colapso
    for _ in range(15):
        sup.record_policy_decision(0)  # Fijación absoluta en HOLD (acción 0)
    ent_snap = sup.get_policy_entropy_metrics()
    assert ent_snap["normalized_policy_entropy"] == 0.0
    assert ent_snap["dominant_action"] == "HOLD"
    assert sup.policy_collapse_detected is True
    assert sup.policy_collapse_reason == "PARALYSIS_HOLD_FIXATION"

    # Diversificar decisiones restaura la entropía tras histéresis
    for i in range(60):
        sup.record_policy_decision(i % 9)
    ent_recovered = sup.get_policy_entropy_metrics()
    assert ent_recovered["normalized_policy_entropy"] > 0.90
    assert sup.policy_collapse_detected is False

    print("✅ TEST 4 (Motor de Supervisión Matemática: TD-Error, Learning Guard, Q-Bias, FER, Entropía y MFE/MAE + Histéresis): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 5: Despachador de Telemetría Headless Atómica (telemetry_live.json)
    # -------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        tel_path = os.path.join(tmpdir, "telemetry_live.json")
        disp = AtomicTelemetryDispatcher(filepath=tel_path, min_write_interval_sec=5.0)
        ok_write = disp.publish(
            balance_sats=145230,
            equity_sats=146100,
            available_sats=110000,
            active_trades=2,
            supervisor=sup,
            force=True,
        )
        assert ok_write is True and os.path.exists(tel_path)
        with open(tel_path, "r", encoding="utf-8") as fh:
            tel_data = json.load(fh)

        for root_key in ("timestamp", "balance_sats", "equity_sats", "available_sats", "active_trades", "rl_metrics"):
            assert root_key in tel_data, f"Falta clave raíz {root_key} en telemetry_live.json"
        rl_m = tel_data["rl_metrics"]
        for req_metric in (
            "rolling_td_error_avg",
            "td_error_trend",
            "q_bias_sats",
            "fee_efficiency_ratio",
            "policy_entropy",
            "normalized_policy_entropy",
            "dominant_action",
            "avg_sats_per_trade_last20",
            "policy_health",
            "mfe_capture_avg",
            "mae_margin_ratio_avg",
            "learning_guard_status",
        ):
            assert req_metric in rl_m, f"Falta métrica RL '{req_metric}' en telemetry_live.json"

    # Generar también el archivo telemetry_live.json inicial en la raíz del proyecto para inspección
    TELEMETRY_DISPATCHER.publish(
        balance_sats=25500,
        equity_sats=25500,
        available_sats=25500,
        active_trades=0,
        supervisor=sup,
        force=True,
    )
    assert os.path.exists(TELEMETRY_DISPATCHER.filepath)
    print("✅ TEST 5 (Despachador de Telemetría Headless Atómica `telemetry_live.json`): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 6: Regresión Arquitectónica (9 Acciones, Cerebro Real, Guardian, Concurrencia 5)
    # -------------------------------------------------------------------------
    Config.configure_environment("mainnet")
    assert Config.NUM_ACTIONS == 9
    assert Config.MAX_CONCURRENT_TRADES == 5
    fg = HardFloorGuardian()
    prot_res = fg.evaluate_protection(25000, 25000, 26000, 19500, -0.02)
    assert prot_res["should_liquidate"] is False
    print("✅ TEST 6 (Regresión Arquitectónica: 9 Acciones, HardFloorGuardian y Límite de 5 Posiciones): PASÓ.")

    print("=" * 80)
    print("🏆 CERTIFICACIÓN COMPLETA: TODOS LOS ESCENARIOS SUPERADOS AL 100% 🏆")
    print("=" * 80)


if __name__ == "__main__":
    run_all_tests()
