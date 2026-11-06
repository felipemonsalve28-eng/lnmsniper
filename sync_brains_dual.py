# -*- coding: utf-8 -*-
"""
sync_brains_dual.py
Motor de Fusión / Nutrición Segura entre el Cerebro Explorador (Testnet)
y el Cerebro de Producción (Mainnet).
Usa AtomicBrainStore con bloqueo POSIX fcntl, por lo que no bloquea ni corrompe
el loop principal de ninguno de los dos bots.
"""
import os
import sys
import math
import shutil
from datetime import datetime
from guardian_execution_core import AtomicBrainStore


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

    for state_key, t_vec in q_test.items():
        if not isinstance(t_vec, list) or len(t_vec) != 9:
            continue
        t_visits = int(v_test.get(state_key, 0))
        if t_visits < min_testnet_visits:
            continue

        m_visits = int(v_main.get(state_key, 0))
        max_t_q = max(float(x) for x in t_vec[1:])

        # Caso 1: Estado inexplorado en Mainnet con evidencia sólida y positiva en Testnet
        if state_key not in q_main or m_visits < 3:
            if max_t_q >= 0.04:
                q_main[state_key] = [
                    round(0.02 if i == 0 else float(t_vec[i]) * 0.65, 4)
                    for i in range(9)
                ]
                v_main[state_key] = max(m_visits, int(t_visits * 0.35))
                new_states_adopted += 1
                states_transferred += 1
            continue

        # Caso 2: Estado existente en ambos -> Transferir acciones con alta confianza estadística
        m_vec = list(q_main[state_key])
        confidence_ratio = min(1.0, t_visits / max(float(m_visits + t_visits), 1.0))
        eff_alpha = round(blend_alpha * confidence_ratio, 4)
        state_modified = False

        for act_idx in range(1, 9):
            tq = float(t_vec[act_idx])
            mq = float(m_vec[act_idx])
            # Nutrir si Testnet descubrió un edge positivo claro o un peligro severo verificado
            if (tq >= 0.06 and tq > mq + 0.03) or (tq <= -0.35 and t_visits >= 25 and mq > -0.15):
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
        print("ℹ️ [NUTRICIÓN DUAL] Sin estados elegibles para transferir en este ciclo.")
        return True

    if dry_run:
        print(
            f"🔍 [SIMULACIÓN NUTRICIÓN] Estados a nutrir: {states_transferred} "
            f"(Nuevos: {new_states_adopted} | Acciones enriquecidas: {actions_enriched})"
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
        f"Estados nutridos: {states_transferred} (Nuevos: {new_states_adopted} | Acciones: {actions_enriched}) | "
        f"SHA-256: {digest[:16]}..."
    )
    return True


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sync_dual_brains(dry_run=dry)
