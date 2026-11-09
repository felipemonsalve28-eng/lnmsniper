# -*- coding: utf-8 -*-
"""
sync_brains_dual.py
Motor de Fusión / Nutrición Controlada entre el Cerebro Explorador (Testnet)
y el Cerebro de Producción (Mainnet).
Evalúa multicriterio:
1. Bloqueo estricto si Mainnet tiene operaciones abiertas (active_trades > 0).
2. Calidad de datos y número de observaciones.
3. Estabilidad del resultado y consistencia fuera de muestra (memoria de replay).
4. Régimen de mercado y rentabilidad neta / eficiencia de comisiones por estilo.
Usa AtomicBrainStore con bloqueo POSIX fcntl para garantizar integridad total.
"""
import os
import sys
import math
import shutil
from datetime import datetime
from guardian_execution_core import AtomicBrainStore


ACTION_TO_STYLE = {
    1: "TREND",
    2: "REVERSION",
    3: "SNIPER",
    4: "DEF_SCALP",
    5: "TREND",
    6: "REVERSION",
    7: "SNIPER",
    8: "DEF_SCALP",
}


def evaluate_transfer_quality(state_key, act_idx, tq, t_visits, test_data):
    """
    Pondera la calidad y relevancia de una experiencia en Testnet antes de
    transferirla a Mainnet. Retorna (allowed: bool, quality_weight: float [0.0..1.0]).
    """
    if math.isnan(tq) or math.isinf(tq):
        return False, 0.0

    parts = str(state_key).split("|")
    regime = parts[0] if parts else "RANGE_NORMAL"

    # 1. Ponderación por régimen de mercado
    if regime in ("CHOP_HIGHVOL", "EXTREME_VOLATILITY"):
        regime_w = 0.55
    elif regime == "RANGE_SQUEEZE":
        regime_w = 0.70
    elif regime in ("TREND_BULL", "TREND_BEAR"):
        regime_w = 1.00
    else:
        regime_w = 0.85

    # 2. Rentabilidad neta y riesgo por familia de estrategia (si existen estadísticas)
    style_name = ACTION_TO_STYLE.get(int(act_idx), "REVERSION")
    style_metrics = test_data.get("style_metrics") or test_data.get("strategy_stats") or {}
    st_info = style_metrics.get(style_name, {}) if isinstance(style_metrics, dict) else {}
    trades_cnt = int(st_info.get("trades", int(st_info.get("wins", 0)) + int(st_info.get("losses", 0))) or 0)
    net_sats = float(st_info.get("net_pnl_sats", st_info.get("net_sats", 0.0)) or 0.0)
    fees_sats = float(st_info.get("fees_sats", 0.0) or 0.0)

    econ_w = 1.0
    if trades_cnt >= 10:
        if net_sats < 0 and tq > 0:
            # Si el estilo pierde sats netos en Testnet, exigir mayor evidencia y descontar peso
            loss_to_fee = abs(net_sats) / max(fees_sats, 1.0)
            econ_w = max(0.30, 0.75 - min(0.45, loss_to_fee * 0.25))
            if tq < 0.11:
                return False, 0.0
        elif fees_sats > 0 and (net_sats / fees_sats) >= 0.25:
            econ_w = 1.10

    # 3. Estabilidad y consistencia fuera de muestra en la memoria de experiencias
    memory = test_data.get("memory", [])
    oos_w = 1.0
    if isinstance(memory, list) and len(memory) >= 8:
        matching_rewards = [
            float(m[3]) for m in memory
            if isinstance(m, (list, tuple)) and len(m) >= 5 and str(m[1]) == str(state_key) and int(m[2]) == int(act_idx)
        ]
        if len(matching_rewards) >= 4:
            mid = len(matching_rewards) // 2
            in_sample_mean = sum(matching_rewards[:mid]) / max(1, mid)
            oos_mean = sum(matching_rewards[mid:]) / max(1, len(matching_rewards) - mid)
            # Si la mitad fuera de muestra contradice una señal positiva, bloquear contaminación
            if tq > 0 and oos_mean < -0.05:
                return False, 0.0
            if (in_sample_mean * oos_mean) < 0 and abs(oos_mean - in_sample_mean) > 0.25:
                oos_w = 0.50
            else:
                oos_w = 1.05

    obs_w = min(1.15, math.sqrt(max(1.0, float(t_visits)) / 20.0))
    combined_quality = max(0.20, min(1.0, regime_w * econ_w * oos_w * obs_w))
    return True, round(combined_quality, 4)


def sync_dual_brains(
    mainnet_path="q_brain_advanced_lnm.json",
    testnet_path="q_brain_testnet_lnm.json",
    min_testnet_visits=15,
    blend_alpha=0.25,
    seed_testnet_if_missing=True,
    dry_run=False,
):
    main_store = AtomicBrainStore(mainnet_path)
    test_store = AtomicBrainStore(testnet_path)

    main_data = main_store.load()
    if not isinstance(main_data, dict) or "q_table" not in main_data:
        print(f"[ERROR] No se pudo cargar el cerebro Mainnet ({mainnet_path}).")
        return False

    # Bloqueo estricto (Fase 3.3): No modificar el cerebro de Mainnet si existen operaciones abiertas
    main_active_trades = main_data.get("active_trades", {})
    if isinstance(main_active_trades, dict) and len(main_active_trades) > 0 and not dry_run:
        print(
            f"⏸️ [NUTRICIÓN DIFERIDA] Mainnet tiene {len(main_active_trades)} operación(es) abierta(s). "
            f"Se pospone la sincronización para preservar la consistencia del estado."
        )
        return False

    # Si no existe aún el cerebro de Testnet, inicializarlo clonando las tablas Q de Mainnet
    if not os.path.exists(testnet_path) and seed_testnet_if_missing:
        seed_payload = dict(main_data)
        seed_payload["active_trades"] = {}
        seed_payload["epsilon"] = 0.22
        seed_payload["env"] = "testnet"
        test_store.save(seed_payload)
        print(f"✅ Cerebro Testnet inicializado desde Mainnet en: {testnet_path}")
        return True

    test_data = test_store.load()
    if not isinstance(test_data, dict) or "q_table" not in test_data:
        print(f"[AVISO] Cerebro Testnet ({testnet_path}) aún no disponible o vacío.")
        return False

    q_main = main_data.get("q_table", {})
    v_main = main_data.get("state_visits", {})
    q_test = test_data.get("q_table", {})
    v_test = test_data.get("state_visits", {})

    states_transferred = 0
    actions_enriched = 0
    new_states_adopted = 0
    rejected_low_quality = 0

    for state_key, t_vec in q_test.items():
        if not isinstance(t_vec, list) or len(t_vec) != 9:
            continue
        t_visits = int(v_test.get(state_key, 0))
        if t_visits < min_testnet_visits:
            continue

        m_visits = int(v_main.get(state_key, 0))
        best_t_act = int(max(range(1, 9), key=lambda i: float(t_vec[i])))
        max_t_q = float(t_vec[best_t_act])

        # Caso 1: Estado inexplorado en Mainnet con evidencia sólida, positiva y de calidad en Testnet
        if state_key not in q_main or m_visits < 3:
            ok_q, q_weight = evaluate_transfer_quality(state_key, best_t_act, max_t_q, t_visits, test_data)
            if max_t_q >= 0.04 and ok_q:
                scale = round(0.65 * q_weight, 4)
                q_main[state_key] = [
                    round(0.02 if i == 0 else float(t_vec[i]) * scale, 4)
                    for i in range(9)
                ]
                v_main[state_key] = max(m_visits, int(t_visits * 0.35 * q_weight))
                new_states_adopted += 1
                states_transferred += 1
            elif max_t_q >= 0.04 and not ok_q:
                rejected_low_quality += 1
            continue

        # Caso 2: Estado existente en ambos -> Transferir acciones que superen filtro multicriterio
        m_vec = list(q_main[state_key])
        confidence_ratio = min(1.0, t_visits / max(float(m_visits + t_visits), 1.0))
        base_eff_alpha = blend_alpha * confidence_ratio
        state_modified = False

        for act_idx in range(1, 9):
            tq = float(t_vec[act_idx])
            mq = float(m_vec[act_idx])
            is_candidate = (tq >= 0.06 and tq > mq + 0.03) or (tq <= -0.35 and t_visits >= 25 and mq > -0.15)
            if not is_candidate:
                continue

            ok_q, q_weight = evaluate_transfer_quality(state_key, act_idx, tq, t_visits, test_data)
            if not ok_q:
                rejected_low_quality += 1
                continue

            eff_alpha = round(base_eff_alpha * q_weight, 4)
            blended = (1.0 - eff_alpha) * mq + (eff_alpha * tq)
            m_vec[act_idx] = round(max(-0.95, min(0.95, blended)), 4)
            actions_enriched += 1
            state_modified = True

        if state_modified:
            m_vec[0] = max(0.01, min(0.025, float(m_vec[0])))
            q_main[state_key] = m_vec
            v_main[state_key] = m_visits + max(1, int(t_visits * 0.15))
            states_transferred += 1

    if states_transferred == 0:
        print(
            f"ℹ️ [NUTRICIÓN DUAL] Sin estados elegibles para transferir en este ciclo "
            f"(Filtrados por baja calidad neta: {rejected_low_quality})."
        )
        return True

    if dry_run:
        print(
            f"🔍 [SIMULACIÓN NUTRICIÓN] Estados a nutrir: {states_transferred} "
            f"(Nuevos: {new_states_adopted} | Acciones enriquecidas: {actions_enriched} | "
            f"Rechazados por calidad: {rejected_low_quality})"
        )
        return True

    ts_bak = f"{mainnet_path}.pre_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
    if os.path.exists(mainnet_path):
        shutil.copy2(mainnet_path, ts_bak)

    main_data["q_table"] = q_main
    main_data["state_visits"] = v_main
    digest = main_store.save(main_data)
    print(
        f"✅ [NUTRICIÓN DUAL COMPLETADA] Testnet ➔ Mainnet | "
        f"Estados nutridos: {states_transferred} (Nuevos: {new_states_adopted} | Acciones: {actions_enriched} | "
        f"Filtrados: {rejected_low_quality}) | SHA-256: {digest[:16]}..."
    )
    return True


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sync_dual_brains(dry_run=dry)
