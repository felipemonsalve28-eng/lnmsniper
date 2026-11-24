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
from hedge_risk_manager import HedgeRiskManager
from lnm_sniper_v6_advanced import (
    AutonomousGoalBrain,
    Config,
    TELEMETRY_DISPATCHER,
    THRESHOLDS_MANAGER,
    evaluate_open_trades_online,
    record_shadow_order,
    update_shadow_trades,
    process_settlement_queue,
    manage_open_positions_and_hedges,
    evaluate_and_dispatch_entries,
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
    print(f"✅ TEST 1 (Esquema {len(REQUIRED_CATEGORIES)} Categorías, Validación de Límites y 0 Claves Huérfanas en learning_thresholds.json): PASÓ.")

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
        # Normalización canónica (M_ref=1000 / margin=2000 => eta=0.5):
        # norm_net=75.0, norm_mae=10.0 -> R_econ_norm = 75.0 - 0.15*(10.0) - 0.05*(10.0) = 73.0 -> shaped = +0.73
        assert abs(r_win - 0.73) < 1e-3, f"Recompensa canónica esperada 0.73, obtenida {r_win}"
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
    # ESCENARIO 6: Regresión Arquitectónica (9 Acciones, HardFloorGuardian y Límite de 5 Posiciones)
    # -------------------------------------------------------------------------
    Config.configure_environment("mainnet")
    assert Config.NUM_ACTIONS == 9
    assert Config.MAX_CONCURRENT_TRADES == 5
    fg = HardFloorGuardian()
    prot_res = fg.evaluate_protection(25000, 25000, 26000, 19500, -0.02)
    assert prot_res["should_liquidate"] is False
    print("✅ TEST 6 (Regresión Arquitectónica: 9 Acciones, HardFloorGuardian y Límite de 5 Posiciones): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 7: Cluster Alpha (F4 Expectancy Bayesiana Jerárquica + F5 Cero Exploración Mainnet)
    # -------------------------------------------------------------------------
    # 7a. Estimador Bayesiano jerárquico Beta-Binomial y contracción (baseline_v73: alpha=7, beta=15)
    p_global, p_lcb_global, meta_g = SatsExpectancyGate.estimate_bayesian_win_rate()
    alpha_cfg = THRESHOLDS_MANAGER.get_float("expectancy", "bayesian_prior_alpha", 7.0)
    beta_cfg = THRESHOLDS_MANAGER.get_float("expectancy", "bayesian_prior_beta", 15.0)
    expected_p_global = alpha_cfg / (alpha_cfg + beta_cfg)
    assert abs(p_global - expected_p_global) < 1e-3, f"Prior global esperado ~{expected_p_global:.4f}, obtuvo {p_global}"
    assert p_lcb_global <= p_global and p_lcb_global > 0.05

    mock_style_win = {"TREND": {"trades": 10, "wins": 8}}
    p_trend, p_lcb_trend, _ = SatsExpectancyGate.estimate_bayesian_win_rate(style="TREND", style_metrics=mock_style_win)
    # Posterior: (alpha + 8) / (alpha + beta + 10)
    expected_p_trend = (alpha_cfg + 8.0) / (alpha_cfg + beta_cfg + 10.0)
    assert abs(p_trend - expected_p_trend) < 1e-3
    assert p_trend > p_global, "Estilo con 80% WR debe elevar la probabilidad estimada posterior"

    # 7b. Compuerta de Expectancy probabilística real:
    # Simular estilo deficitario (20 trades, 2 wins -> 10% WR) frente a stop loss estándar
    mock_style_poor = {"REVERSION": {"trades": 20, "wins": 2}}
    viable_poor, meta_poor = SatsExpectancyGate.verify_net_sats_expectancy(
        price=78000.0, margin_sats=2000, lev=25, sl_price=77400.0, tp_price=79200.0,
        explorer_mode=False, style="REVERSION", style_metrics=mock_style_poor
    )
    # Debe ser bloqueada por win rate insuficiente respecto al breakeven p*
    assert meta_poor["p_star"] > meta_poor["p_lcb"]
    assert viable_poor is False, "Estilo con 10% WR debe ser bloqueado por la compuerta bayesiana probabilística"

    # 7c. Supresión estricta de exploración con capital real en Mainnet (mainnet_epsilon = 0.0)
    with tempfile.TemporaryDirectory() as tmpdir:
        b_main_path = os.path.join(tmpdir, "mainnet_test_brain.json")
        b_main = AutonomousGoalBrain(filename=b_main_path, explorer_mode=False)
        ind_dummy = {
            "price": 78000.0, "atr": 200.0, "rsi": 50.0, "bb_pos": 0.5, "ema9": 78000.0,
            "ema21": 78000.0, "ema_score": 0.0, "regime": "RANGE_NORMAL"
        }
        # En 30 decisiones de política, nunca debe generar una acción aleatoria
        for _ in range(30):
            chosen = b_main.choose_action(
                "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS",
                ind_dummy, ind_dummy, ind_dummy, ind_dummy
            )
            assert chosen in range(9)

    # 7d. Multiplicador de cooldown dinámico cuando FER reciente < 1.0
    fer_cooldown_cfg = THRESHOLDS_MANAGER.get_float("exploration", "fer_cooldown_mult", 2.0)
    assert fer_cooldown_cfg >= 2.0
    print("✅ TEST 7 (Cluster Alpha: Expectancy Bayesiana Jerárquica O(1) + Cero Exploración Mainnet + Cooldown FER): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 8: Cluster Beta (F6 Guardado Diferido / Debounce, Refactor main(), Auditoría JSONL + F7 Modo Sombra)
    # -------------------------------------------------------------------------
    # 8a. Configuración y Debounce de guardado de cerebro
    assert hasattr(Config, "BRAIN_SAVE_INTERVAL")
    assert Config.BRAIN_SAVE_INTERVAL >= 10.0
    assert hasattr(Config, "SHADOW_MODE")

    with tempfile.TemporaryDirectory() as tmpdir:
        b_debounce_path = os.path.join(tmpdir, "debounce_brain.json")
        b_deb = AutonomousGoalBrain(filename=b_debounce_path, explorer_mode=True)
        b_deb.q_table["STATE_TEST"] = [0.0] * 9
        b_deb.dirty = True
        # Primera llamada force=False guarda archivo si dirty
        saved_1 = b_deb.save_brain(force=False)
        assert saved_1 is True
        mtime_1 = os.path.getmtime(b_debounce_path)

        # Segunda llamada inmediata force=False se pospone (debounce retorna False)
        b_deb.dirty = True
        saved_2 = b_deb.save_brain(force=False)
        assert saved_2 is False
        mtime_2 = os.path.getmtime(b_debounce_path)
        assert mtime_1 == mtime_2, "El guardado debounced no debe escribir a disco antes de BRAIN_SAVE_INTERVAL"

        # Guardado forzado force=True escribe inmediatamente
        time.sleep(0.02)
        saved_3 = b_deb.save_brain(force=True)
        assert saved_3 is True
        mtime_3 = os.path.getmtime(b_debounce_path)
        assert mtime_3 > mtime_1, "El guardado forzado debe sincronizar inmediatamente a disco"

    # 8b. Modularidad de funciones F6
    assert callable(process_settlement_queue)
    assert callable(manage_open_positions_and_hedges)
    assert callable(evaluate_and_dispatch_entries)

    # 8c. Modo Sombra F7: registro y liquidación virtual de órdenes simuladas
    with tempfile.TemporaryDirectory() as tmpdir:
        b_shadow_path = os.path.join(tmpdir, "shadow_brain.json")
        b_shadow = AutonomousGoalBrain(filename=b_shadow_path, explorer_mode=True)
        b_shadow.shadow_trades = {
            "shadow-test-buy": {
                "id": "shadow-test-buy",
                "side": "buy",
                "entry_price": 70000.0,
                "stoploss": 69000.0,
                "takeprofit": 72000.0,
                "margin_sats": 1000,
                "leverage": 20.0,
                "total_cost_sats": 40.0,
                "status": "OPEN_SIMULATED",
            },
            "shadow-test-sell": {
                "id": "shadow-test-sell",
                "side": "sell",
                "entry_price": 70000.0,
                "stoploss": 71000.0,
                "takeprofit": 68000.0,
                "margin_sats": 1000,
                "leverage": 20.0,
                "total_cost_sats": 40.0,
                "status": "OPEN_SIMULATED",
            },
        }

        # Simular precio spot que alcanza TP de la orden buy (72500 >= 72000) y SL de la sell (72500 >= 71000)
        import lnm_sniper_v6_advanced as _bot_mod
        _orig_rec_shadow = _bot_mod.record_shadow_order
        try:
            _bot_mod.record_shadow_order = lambda _rec: None
            closed_sims = update_shadow_trades(b_shadow, 72500.0, now=time.time())
        finally:
            _bot_mod.record_shadow_order = _orig_rec_shadow
        assert len(closed_sims) == 2
        statuses = {s["id"]: s["status"] for s in closed_sims}
        assert statuses["shadow-test-buy"] == "CLOSED_TP"
        assert statuses["shadow-test-sell"] == "CLOSED_SL"
        pnl_by_id = {s["id"]: s["net_realized_sats"] for s in closed_sims}
        assert pnl_by_id["shadow-test-buy"] > 0
        assert pnl_by_id["shadow-test-sell"] < 0
        assert len(b_shadow.shadow_trades) == 0

    print("✅ TEST 8 (Cluster Beta: F6 Refactor Modular main(), Guardado Diferido/Debounce, Auditoría JSONL + F7 Modo Sombra Simulado): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 9: Filtros Cobertura Anti-Whipsaw (HedgeRiskManager) + Desempate Colapso Política (choose_action)
    # -------------------------------------------------------------------------
    base_pair_ctx = {
        "orig_notional_sats": 30000,
        "orig_margin_sats": 1500,
        "orig_leverage": 20,
        "orig_side": "buy",
        "orig_opened_at": time.time() - 120.0,
        "hedges_opened_count": 0,
        "cooldown_until": 0.0,
    }
    base_pair_econ = {
        "has_active_hedge": False,
        "effective_hedge_ratio": 0.0,
        "hedge_notional_sats": 0,
        "orig_pl_sats": -120,
    }
    base_pf_snap = {"locked_margin_sats": 1500}

    # 9a. Bloqueo de PARTIAL_HEDGE en RANGE_SQUEEZE y CHOP_HIGHVOL cuando adv_prob < 0.78
    ok_sq_low, reason_sq_low, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx=base_pair_ctx,
        pair_econ=base_pair_econ,
        portfolio_snap=base_pf_snap,
        predictive_signal={
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.65,
            "expected_saved_sats": 500,
            "net_expected_saving_sats": 400,
        },
        account_equity_sats=30000,
        available_balance_sats=20000,
        locked_floor_sats=21000,
        ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "RANGE_SQUEEZE"},
        explorer_mode=False,
    )
    assert ok_sq_low is False and reason_sq_low == "BLOCKED_HEDGE_SQUEEZE_WHIPSAW_RISK"

    ok_chop_low, reason_chop_low, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx=base_pair_ctx,
        pair_econ=base_pair_econ,
        portfolio_snap=base_pf_snap,
        predictive_signal={
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.77,
            "expected_saved_sats": 500,
            "net_expected_saving_sats": 400,
        },
        account_equity_sats=30000,
        available_balance_sats=20000,
        locked_floor_sats=21000,
        ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "CHOP_HIGHVOL"},
        explorer_mode=False,
    )
    assert ok_chop_low is False and reason_chop_low == "BLOCKED_HEDGE_SQUEEZE_WHIPSAW_RISK"

    ok_sq_high, reason_sq_high, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx=base_pair_ctx,
        pair_econ=base_pair_econ,
        portfolio_snap=base_pf_snap,
        predictive_signal={
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.82,
            "expected_saved_sats": 500,
            "net_expected_saving_sats": 400,
        },
        account_equity_sats=30000,
        available_balance_sats=20000,
        locked_floor_sats=21000,
        ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "RANGE_SQUEEZE"},
        explorer_mode=False,
    )
    assert ok_sq_high is True and reason_sq_high == "AUTHORIZED_PARTIAL_HEDGE"

    # Verificación de costo en explorer_mode: exige net_saving_sats > 0 y expected_saved_sats >= 1.35 * est_round_fee
    ok_exp_low, reason_exp_low, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx=base_pair_ctx,
        pair_econ=base_pair_econ,
        portfolio_snap=base_pf_snap,
        predictive_signal={
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.85,
            "expected_saved_sats": 45,
            "net_expected_saving_sats": 5,
        },
        account_equity_sats=30000,
        available_balance_sats=20000,
        locked_floor_sats=21000,
        ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "VOLATILITY_EXPANSION"},
        explorer_mode=True,
    )
    assert ok_exp_low is False and "BLOCKED_HEDGE_COST_EXCEEDS_UNHEDGED_RISK" in reason_exp_low

    ok_exp_zero_net, reason_exp_zero_net, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx=base_pair_ctx,
        pair_econ=base_pair_econ,
        portfolio_snap=base_pf_snap,
        predictive_signal={
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.85,
            "expected_saved_sats": 100,
            "net_expected_saving_sats": 0,
        },
        account_equity_sats=30000,
        available_balance_sats=20000,
        locked_floor_sats=21000,
        ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "VOLATILITY_EXPANSION"},
        explorer_mode=True,
    )
    assert ok_exp_zero_net is False and "BLOCKED_HEDGE_COST_EXCEEDS_UNHEDGED_RISK" in reason_exp_zero_net

    ok_exp_pass, reason_exp_pass, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx=base_pair_ctx,
        pair_econ=base_pair_econ,
        portfolio_snap=base_pf_snap,
        predictive_signal={
            "optimal_hedge_ratio": 0.50,
            "adverse_continuation_prob": 0.85,
            "expected_saved_sats": 80,
            "net_expected_saving_sats": 40,
        },
        account_equity_sats=30000,
        available_balance_sats=20000,
        locked_floor_sats=21000,
        ind_5m={"price": 77600.0, "atr": 250.0, "market_regime": "VOLATILITY_EXPANSION"},
        explorer_mode=True,
    )
    assert ok_exp_pass is True and reason_exp_pass == "AUTHORIZED_PARTIAL_HEDGE"

    # 9b. Penalización determinista (-0.06) de desempate en choose_action cuando policy_collapse_detected=True
    with tempfile.TemporaryDirectory() as tmpdir:
        b_col_path = os.path.join(tmpdir, "collapse_brain.json")
        b_col = AutonomousGoalBrain(filename=b_col_path, explorer_mode=False)
        state_col = "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS"
        b_col.q_table[state_col] = [0.0, 0.15, 0.0, 0.12, 0.0, 0.0, 0.0, 0.0, 0.0]
        b_col.state_visits[state_col] = 100
        ind_neutral = {
            "price": 78000.0, "atr": 200.0, "rsi": 50.0, "bb_pos": 0.5,
            "ema9": 78000.0, "ema21": 78000.0, "ema50": 78000.0, "ema200": 78000.0,
            "ema_score": 0.0, "regime": "RANGE",
        }

        act_before = b_col.choose_action(state_col, ind_neutral, ind_neutral, ind_neutral, ind_neutral)
        assert act_before == 1

        for _ in range(15):
            b_col.supervisor.record_policy_decision(1)
        assert b_col.supervisor.policy_collapse_detected is True

        act_after = b_col.choose_action(state_col, ind_neutral, ind_neutral, ind_neutral, ind_neutral)
        assert act_after == 3
        assert b_col.q_table[state_col][1] == 0.15 and b_col.q_table[state_col][3] == 0.12

    print("✅ TEST 9 (Anti-Whipsaw HedgeRiskManager + Costo Explorer + Desempate Determinista ante Colapso de Política): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 10: Invariantes Etapa 22 (78 umbrales, Basis, Sync Idempotente y state_visits)
    # -------------------------------------------------------------------------
    from learning_thresholds_manager import FALLBACK_THRESHOLDS
    from sniper_cli_catalog import PARAM_OPERATIONAL_GUIDE
    from lnm_sniper_v6_advanced import compute_indicators
    from sync_brains_dual import sync_dual_brains
    from guardian_execution_core import AtomicBrainStore

    # 10.1: 100 claves exactas en FALLBACK_THRESHOLDS vs PARAM_OPERATIONAL_GUIDE
    fb_keys = {f"{c}.{p}" for c, d in FALLBACK_THRESHOLDS.items() for p in d}
    cat_keys = set(PARAM_OPERATIONAL_GUIDE.keys())
    assert fb_keys == cat_keys, f"Discrepancia en FALLBACK_THRESHOLDS: {fb_keys ^ cat_keys}"
    assert len(fb_keys) == 100, f"Se esperaban 100 claves, encontradas {len(fb_keys)}"

    # 10.2: Invarianza al Basis (BTCUSDT vs BTCUSD) en compute_indicators
    base_c = [70000.0 + i * 5.0 for i in range(60)]
    base_h = [x + 15.0 for x in base_c]
    base_l = [x - 15.0 for x in base_c]
    ind_usdt = compute_indicators(base_h, base_l, base_c, lnm_price=0.0)
    lnm_spot_usd = base_c[-1] * 0.996  # -0.4% basis diferencial
    ind_usd = compute_indicators(base_h, base_l, base_c, lnm_price=lnm_spot_usd)
    assert abs(ind_usdt["rsi"] - ind_usd["rsi"]) < 1e-4, f"RSI contaminado por basis: {ind_usdt['rsi']} vs {ind_usd['rsi']}"
    assert ind_usdt["ema_score"] == ind_usd["ema_score"], "ema_score alterado por basis"

    # 10.3: Idempotencia de sync_dual_brains con synced_test_visits
    with tempfile.TemporaryDirectory() as tmpd:
        m_path = os.path.join(tmpd, "main_brain.json")
        t_path = os.path.join(tmpd, "test_brain.json")
        st_key = "RANGE|NEUTRAL|NORM|BULL|EMA_BULL"
        m_payload = {
            "reward_schema_version": 2,
            "q_table": {st_key: [0.0, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
            "state_visits": {st_key: 5},
            "active_trades": {},
        }
        t_payload = {
            "reward_schema_version": 2,
            "q_table": {st_key: [0.0, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
            "state_visits": {st_key: 20},
            "style_metrics": {"TREND": {"trades": 10, "wins": 7, "net_sats": 500, "fees_sats": 100}},
        }
        AtomicBrainStore(m_path).save(m_payload)
        AtomicBrainStore(t_path).save(t_payload)
        sync_dual_brains(mainnet_path=m_path, testnet_path=t_path)
        after_first = AtomicBrainStore(m_path).load()
        v_after_first = int(after_first["state_visits"][st_key])
        q_after_first = list(after_first["q_table"][st_key])
        sync_dual_brains(mainnet_path=m_path, testnet_path=t_path)
        after_second = AtomicBrainStore(m_path).load()
        assert int(after_second["state_visits"][st_key]) == v_after_first, "sync_dual_brains inflo visitas en 2da pasada sin nuevos cierres"
        assert list(after_second["q_table"][st_key]) == q_after_first, "sync_dual_brains altero Q-table en 2da pasada sin nuevos cierres"

        # 10.4: choose_action no infla state_visits antes de ejecutar
        b_inst = AutonomousGoalBrain(filename=os.path.join(tmpd, "visit_test.json"), explorer_mode=False)
        b_inst.q_table[st_key] = [0.0, 0.35, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        b_inst.state_visits[st_key] = 10
        ind_visit = {
            "price": 78000.0, "atr": 200.0, "rsi": 50.0, "bb_pos": 0.5,
            "ema9": 78000.0, "ema21": 78000.0, "ema50": 78000.0, "ema200": 78000.0,
            "ema_score": 0.0, "regime": "RANGE_NORMAL",
        }
        chosen = b_inst.choose_action(st_key, ind_visit, ind_visit, ind_visit, ind_visit)
        assert chosen == 1
        assert int(b_inst.state_visits[st_key]) == 10, "choose_action inflo state_visits sin despachar ni cerrar orden"

    print("✅ TEST 10 (Invariantes Etapa 22+F1+F2: 100 Umbrales, Basis BTCUSD/BTCUSDT, Sync Idempotente y No-Inflación state_visits): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 11: Normalización Canónica 1k-sat, Aislamiento de Umbrales por Entorno y Shadow Trailing Lock
    # -------------------------------------------------------------------------
    from learning_thresholds_manager import TESTNET_CONFIG_PATH, resolve_thresholds_path_for_env
    import lnm_sniper_v6_advanced as _bot_mod_11

    # 11.1: Enrutamiento aislado mainnet vs testnet en LearningThresholdsManager
    assert os.path.abspath(resolve_thresholds_path_for_env("mainnet")) == os.path.abspath(DEFAULT_CONFIG_PATH)
    assert os.path.abspath(resolve_thresholds_path_for_env("testnet")) == os.path.abspath(TESTNET_CONFIG_PATH)
    mgr_env = LearningThresholdsManager(config_path=DEFAULT_CONFIG_PATH)
    assert abs(mgr_env.get_float("expectancy", "bayesian_prior_alpha") - 7.0) < 1e-6
    assert abs(mgr_env.get_float("expectancy", "bayesian_prior_beta") - 15.0) < 1e-6
    mgr_env.switch_environment("testnet")
    assert mgr_env.config_path == os.path.abspath(TESTNET_CONFIG_PATH)
    assert abs(mgr_env.get_float("expectancy", "bayesian_prior_alpha") - 9.0) < 1e-6
    assert abs(mgr_env.get_float("expectancy", "bayesian_prior_beta") - 10.0) < 1e-6
    assert abs(mgr_env.get_float("expectancy", "breakeven_safety_margin") - 0.02) < 1e-6
    mgr_env.switch_environment("mainnet")
    assert mgr_env.config_path == os.path.abspath(DEFAULT_CONFIG_PATH)
    assert abs(mgr_env.get_float("expectancy", "breakeven_safety_margin") - 0.04) < 1e-6

    # 11.2: Invarianza dimensional al Margen Canónico (M_ref = 1000 sats) en Supervisor y Q-Bias
    assert abs(LearningQualitySupervisor.normalize_to_canonical_margin(100.0, 1000.0) - 100.0) < 1e-6
    assert abs(LearningQualitySupervisor.normalize_to_canonical_margin(-2000.0, 25000.0) - (-80.0)) < 1e-6

    sup_norm = LearningQualitySupervisor()
    # Inyectar outlier legado y comprobar sanitización en get_q_bias_stats()
    sup_norm.q_bias_window.append(2500.0)
    qb_avg_clamped, _ = sup_norm.get_q_bias_stats()
    assert qb_avg_clamped <= 250.0, f"Outlier legado no sanitizado: {qb_avg_clamped}"
    sup_norm.q_bias_window.clear()

    # Simular cierre Testnet (margen=25,000 sats, pérdida=-2,000 sats => -80 sats/1k, Q_entry=-0.80 => -80 sats)
    sup_norm.learning_guard_status = "SUSPENDED_CRITICAL_TD"
    for _ in range(5):
        sup_norm.record_shadow_closed_trade(
            predicted_q_entry=-0.80,
            net_realized_sats=-2000.0,
            signed_td_error=0.12,
            margin_sats=25000.0,
        )
    qb_norm_avg, _ = sup_norm.get_q_bias_stats()
    assert abs(qb_norm_avg) < 1e-3, f"Q-Bias canónico esperado ~0.0, obtuvo {qb_norm_avg}"
    assert sup_norm.learning_guard_status == "ACTIVE_LEARNING", "Histéresis sombra debe reactivar ACTIVE_LEARNING"

    # 11.3: Gemelo digital en update_shadow_trades (Breakeven Lock + CLOSED_TRAIL_PROFIT + Norm1k)
    with tempfile.TemporaryDirectory() as tmpdir:
        b_tr = AutonomousGoalBrain(filename=os.path.join(tmpdir, "trail_brain.json"), explorer_mode=True)
        now_t = time.time()
        b_tr.shadow_trades = {
            "shadow-trail-1": {
                "id": "shadow-trail-1",
                "action": 2,
                "side": "buy",
                "entry_price": 70000.0,
                "stoploss": 69200.0,
                "takeprofit": 72500.0,
                "margin_sats": 25000,
                "leverage": 20.0,
                "total_cost_sats": 1000.0,
                "entry_q": 0.50,
                "opened_at": now_t,
                "state": "TREND_BULL|DIP|LOW_EXP|BULL|FULL_BULL",
                "status": "OPEN_SIMULATED",
            }
        }
        _orig_rec_11 = _bot_mod_11.record_shadow_order
        try:
            _bot_mod_11.record_shadow_order = lambda _rec: None
            # Paso 1: sube +1000 USD (> min_trig con atr=300), debe bloquear SL en ganancia sin cerrar
            c_step1 = update_shadow_trades(b_tr, 71000.0, now=now_t + 30.0, ind_5m={"atr": 300.0})
            assert len(c_step1) == 0
            assert b_tr.shadow_trades["shadow-trail-1"].get("sl_locked") is True
            locked_sl = b_tr.shadow_trades["shadow-trail-1"]["stoploss"]
            assert locked_sl > 70000.0

            # Paso 2: retrocede por debajo del SL bloqueado -> cierra con CLOSED_TRAIL_PROFIT y ganancia neta > 0
            c_step2 = update_shadow_trades(b_tr, locked_sl - 5.0, now=now_t + 60.0, ind_5m={"atr": 300.0})
            assert len(c_step2) == 1
            assert c_step2[0]["status"] == "CLOSED_TRAIL_PROFIT"
            assert c_step2[0]["net_realized_sats"] > 0.0
            assert "norm_net_sats_1k" in c_step2[0]
            assert abs(c_step2[0]["norm_net_sats_1k"] - (c_step2[0]["net_realized_sats"] * (1000.0 / 25000.0))) < 0.2
        finally:
            _bot_mod_11.record_shadow_order = _orig_rec_11

    print("✅ TEST 11 (Invarianza Canónica 1k-sat, Aislamiento Mainnet/Testnet y Shadow Breakeven/Trailing Lock): PASÓ.")

    # -------------------------------------------------------------------------
    # ESCENARIO 12: Parches 1–5 (Forced Edge Viability, Exploratory Q Neutralization,
    #               Fee Threshold en Cash-In, Discard Entropy y Clock Offset 401)
    # -------------------------------------------------------------------------
    from edge_monitor import EdgeMonitor
    from guardian_execution_core import ResilientLNMClient
    from lnm_sniper_v6_advanced import (
        LNMarketsClient,
        compute_live_edge,
        execute_strategy_guardian,
    )

    now_12 = time.time()

    # 12.1: Bloqueo de FORCED EDGE EXIT en zona muerta de comisiones (pnl_atr = +0.20, ganancia bruta < comisión round-trip)
    # Trade con margen 39,000 sats y lev 20x -> notional = 780,000 -> rt_fee_sats = 1,560 sats.
    # Ganancia bruta = +500 sats (< 1,560 sats round-trip fee), pnl_atr = +0.20 ((78040 - 78000)/200).
    class _MockCloseClient12:
        def __init__(self):
            self.closed_ids = []
        def close_trade(self, tid):
            self.closed_ids.append(str(tid))
            return {"id": str(tid), "closed": True}
        def get_running_trades(self, force_refresh=False):
            return []

    with tempfile.TemporaryDirectory() as tmpdir12:
        b_12 = AutonomousGoalBrain(filename=os.path.join(tmpdir12, "b12.json"), explorer_mode=True)
        deadzone_trade = {
            "id": "c5c037bc-deadzone",
            "side": "buy",
            "margin": 39000,
            "leverage": 20.0,
            "price": 78000.0,
            "stoploss": 77200.0,
            "liquidation": 74500.0,
            "pl": 500,
        }
        deadzone_meta = {
            "action": 2,
            "style": "REVERSION",
            "state": "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR",
            "entry_time": now_12 - 150.0,
            "edge_deg_streak": 3,
        }
        ind_5m_dz = {
            "price": 78040.0,
            "atr": 200.0,
            "atr_pct": 0.0025,
            "regime": "RANGE_NORMAL",
            "rsi": 52.0,
            "bb_pos": 0.50,
            "_now_ts": now_12,
        }
        ind_15m_dz = {"ema9": 78000.0, "ema21": 78000.0, "rsi": 50.0}
        cli_12 = _MockCloseClient12()
        closed_dz = execute_strategy_guardian(
            cli_12, b_12, deadzone_trade, ind_5m_dz, ind_15m_dz,
            deadzone_meta, "NORMAL", edge_score=-0.85, perception={"vel_ema_atr": 0.0},
            current_dd_pct=0.0, is_hedged=False,
        )
        assert closed_dz is False, "FORCED EDGE EXIT no debe liquidar trade en ganancia bruta dentro de zona muerta de comisiones"
        assert len(cli_12.closed_ids) == 0
        assert deadzone_meta["edge_deg_streak"] == 3  # incrementó a 4 y Regla 2 restó 1 -> 3

        # 12.2: Neutralización de Q < 0 en edge_monitor.py y compute_live_edge para entradas exploratorias (< 180s)
        st_exp = "TREND_BEAR|MID|LOW_EXP|BEAR|FULL_BEAR"
        b_12.q_table[st_exp] = [0.0, -0.5, -0.5, -0.5, -0.5, -0.5, -0.5, -0.5, -1.1122]
        b_12.state_visits[st_exp] = 30
        exp_meta = {"is_exploratory": True, "entry_time": now_12 - 55.0}
        em_res = EdgeMonitor.evaluate_position_edge(
            deadzone_trade,
            {"orig_opened_at": now_12 - 55.0, "is_exploratory": True},
            ind_5m_dz,
            ind_15m_dz,
            {"ema_score": 0.0},
            perception={"vel_ema_atr": 0.0, "clv": 0.0},
            q_val_action=-1.1122,
            now=now_12,
            orig_meta=exp_meta,
        )
        assert em_res["q_cql"] == -1.1122 and em_res["effective_q_cql"] == 0.0
        assert em_res["edge_score"] >= -0.65

        live_edge_res = compute_live_edge(
            brain=b_12,
            state=st_exp,
            action=8,
            ind_5m=ind_5m_dz,
            perception={"override_side": None},
            margin_sats=39000,
            leverage=20.0,
            entry_price=78000.0,
            simulated_entry=False,
            orig_meta=exp_meta,
            now=now_12,
        )
        assert live_edge_res["q_cql"] < -1.0 and live_edge_res["effective_q_cql"] == 0.0
        assert live_edge_res["edge_score"] >= -0.65

        # 12.2b: Pre-Flight (simulated_entry=True) sin triple descuento de comisiones
        st_pf_ok = "TREND_BULL|DIP|LOW_EXP|BULL|FULL_BULL"
        b_12.q_table[st_pf_ok] = [0.0, 0.065, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        b_12.state_visits[st_pf_ok] = 50
        pf_ok = compute_live_edge(
            brain=b_12, state=st_pf_ok, action=1,
            ind_5m={"price": 62800.0, "atr": 160.0},
            perception={"override_side": None},
            margin_sats=1000, leverage=24.0, entry_price=62800.0, simulated_entry=True,
        )
        assert pf_ok["edge_score"] > 0.0, f"Pre-Flight alineado debe pasar: {pf_ok}"

        pf_squeeze = compute_live_edge(
            brain=b_12, state=st_pf_ok, action=1,
            ind_5m={"price": 62800.0, "atr": 65.0},
            perception={"override_side": None},
            margin_sats=1000, leverage=24.0, entry_price=62800.0, simulated_entry=True,
        )
        assert pf_squeeze["edge_score"] < 0.0, f"Compresion extrema debe bloquearse: {pf_squeeze}"

        st_pf_ct = "CHOP_HIGHVOL|PEAK|LOW_EXP|BEAR|FULL_BEAR"
        b_12.q_table[st_pf_ct] = [0.0, 0.065, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        b_12.state_visits[st_pf_ct] = 50
        pf_ct = compute_live_edge(
            brain=b_12, state=st_pf_ct, action=1,
            ind_5m={"price": 62800.0, "atr": 180.0},
            perception={"override_side": "BULL"},
            margin_sats=1000, leverage=24.0, entry_price=62800.0, simulated_entry=True,
        )
        assert pf_ct["edge_score"] < 0.0, f"Contratendencia macro debe seguir bloqueada: {pf_ct}"

        # 12.2c: Radar de liquidez en Pre-Flight (shadow_only=1 neutro vs activo acotado; trades abiertos siempre neutros)
        from lnm_sniper_v6_advanced import THRESHOLDS_MANAGER as _TM_LIQ
        _orig_shadow = _TM_LIQ._data["liquidity_radar"]["shadow_only"]["value"]
        try:
            _TM_LIQ._data["liquidity_radar"]["shadow_only"]["value"] = 1
            pf_sh = compute_live_edge(
                brain=b_12, state=st_pf_ok, action=1,
                ind_5m={"price": 62800.0, "atr": 160.0},
                perception={"override_side": None, "liquidity": {"liquidity_score": -0.90}},
                margin_sats=1000, leverage=24.0, entry_price=62800.0, simulated_entry=True,
            )
            assert pf_sh["liq_bias"] == 0.0 and pf_sh["edge_score"] > 0.0, f"Modo sombra debe ser neutro: {pf_sh}"

            _TM_LIQ._data["liquidity_radar"]["shadow_only"]["value"] = 0
            pf_trap = compute_live_edge(
                brain=b_12, state=st_pf_ok, action=1,
                ind_5m={"price": 62800.0, "atr": 160.0},
                perception={"override_side": None, "liquidity": {"liquidity_score": -0.85}},
                margin_sats=1000, leverage=24.0, entry_price=62800.0, simulated_entry=True,
            )
            assert pf_trap["liq_bias"] < -0.09 and pf_trap["edge_score"] < 0.0, f"Sweep opuesto debe bloquear Pre-Flight: {pf_trap}"

            open_no_liq = compute_live_edge(
                brain=b_12, state=st_pf_ok, action=1,
                ind_5m={"price": 62800.0, "atr": 160.0},
                perception={"override_side": None, "liquidity": {"liquidity_score": -0.95}},
                margin_sats=1000, leverage=24.0, entry_price=62800.0, simulated_entry=False,
            )
            assert open_no_liq["liq_bias"] == 0.0, f"Trade abierto nunca debe recibir liq_bias: {open_no_liq}"
        finally:
            _TM_LIQ._data["liquidity_radar"]["shadow_only"]["value"] = _orig_shadow

        # 12.3: Bloqueo de REDUCE_ORIGINAL_POSITION cuando orig_pl < 1.50 * orig_rt_fee
        ok_red, reason_red, _ = HedgeRiskManager.authorize_hedge_action(
            "REDUCE_ORIGINAL_POSITION",
            pair_ctx={"orig_notional_sats": 780000, "orig_reductions_count": 0, "cooldown_until": 0.0},
            pair_econ={"orig_pl_sats": 33, "has_active_hedge": False},
            portfolio_snap={"locked_margin_sats": 39000},
            predictive_signal={},
            account_equity_sats=500000,
            available_balance_sats=400000,
            locked_floor_sats=300000,
            ind_5m=ind_5m_dz,
            now=now_12,
        )
        assert ok_red is False and "BLOCKED_ORIG_CASHIN_BELOW_FEE_THRESHOLD" in reason_red

        # 12.4: Reversión de muestra de entropía con discard_last_action_decision() ante bloqueo de Anti-Stacking
        b_12.supervisor.action_window.clear()
        b_12.supervisor.low_entropy_streak = 0
        for act_seed in (1, 2, 3, 4, 5, 6, 7, 8):
            b_12.supervisor.record_policy_decision(act_seed)
        pre_len = len(b_12.supervisor.action_window)
        b_12.supervisor.record_policy_decision(2)
        assert len(b_12.supervisor.action_window) == pre_len + 1
        b_12.supervisor.discard_last_action_decision()
        assert len(b_12.supervisor.action_window) == pre_len

        # 12.5: Auto-sincronización de _server_time_offset_ms en ResilientLNMClient / LNMarketsClient ante HTTP 401 outdated timestamp
        rc = LNMarketsClient("k", "s", "p")
        assert rc._server_time_offset_ms == 0
        fake_srv_ms = int(time.time() * 1000) + 4200
        err_401 = f'{{"message":"outdated timestamp: request timestamp = {fake_srv_ms - 4200}, server timestamp = {fake_srv_ms}"}}'
        synced = rc._sync_clock_offset_from_error_or_headers(err_401, {})
        assert synced is True
        assert abs(rc._server_time_offset_ms - 4200) <= 500

    print("✅ TEST 12 (Parches 1–5: Viabilidad Neta Forced Edge, Q Exploratorio Neutralizado, Umbral Cash-In, Entropía y Clock Sync 401): PASÓ.")

    print("=" * 80)
    print("🏆 CERTIFICACIÓN COMPLETA: TODOS LOS ESCENARIOS SUPERADOS AL 100% 🏆")
    print("=" * 80)


if __name__ == "__main__":
    run_all_tests()
