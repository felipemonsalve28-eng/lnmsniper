# -*- coding: utf-8 -*-
"""
retrain_sats_offline.py
Reentrenamiento y Recalibración Offline orientada a Sats Netos Acumulados.
Alineado con el mapa oficial de estrategias de lnm_sniper_v6_advanced.py:
0: NO_TRADE
1: LONG_TREND, 2: LONG_REVERSION, 3: LONG_SNIPER, 4: LONG_DEF_SCALP
5: SHORT_TREND, 6: SHORT_REVERSION, 7: SHORT_SNIPER, 8: SHORT_DEF_SCALP
"""
import os
import json
import math
from guardian_execution_core import AtomicBrainStore


def compute_net_sats_reward(gross_atr_move, atr_pct, lev, is_chop=False, aligned_momentum=False):
    fee_pct = 0.0020 * float(lev)
    gross_roi = float(gross_atr_move) * float(atr_pct) * float(lev)
    net_roi = gross_roi - fee_pct
    churn_penalty = 0.035 if is_chop else 0.0
    momentum_bonus = 0.045 if (aligned_momentum and net_roi > 0) else 0.0
    reward = max(-1.0, min(1.0, net_roi - churn_penalty + momentum_bonus))
    return round(reward, 5)


def recalibrate_brain_for_sats_accumulation(brain_path="q_brain_advanced_lnm.json"):
    if not os.path.exists(brain_path):
        print(f"❌ No existe {brain_path}.")
        return False

    store = AtomicBrainStore(brain_path)
    with open(brain_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    q_table = data.get("q_table", {})
    state_visits = data.get("state_visits", {})
    recalibrated_count = 0

    for state_key, q_vec in list(q_table.items()):
        if not isinstance(q_vec, list) or len(q_vec) < 9:
            continue

        parts = str(state_key).split("|")
        regime = parts[0] if len(parts) > 0 else ""
        osc = parts[1] if len(parts) > 1 else ""
        exp = parts[2] if len(parts) > 2 else ""
        micro_trend = parts[3] if len(parts) > 3 else ""
        ema_struct = parts[4] if len(parts) > 4 else ""

        visits = int(state_visits.get(state_key, 1))
        lcb_penalty = round(0.015 / math.sqrt(max(1, visits)), 5)

        # Normalizar primero cualquier outlier histórico fuera de [-0.85, 0.85]
        new_q = [max(-0.85, min(0.85, float(x))) for x in q_vec]

        # 1. Penalizar sobre-operación en RANGE_SQUEEZE + CHOP o HIGH_EXP
        if "RANGE_SQUEEZE" in regime and ("CHOP" in micro_trend or "HIGH_EXP" in exp):
            new_q[0] = max(new_q[0], 0.025)
            new_q[3] -= 0.045  # Penalizar LONG_SNIPER (3) en chop/squeeze
            new_q[7] -= 0.045  # Penalizar SHORT_SNIPER (7) en chop/squeeze
            if "HIGH_EXP" in exp:
                new_q[2] = min(new_q[2], 0.040)  # Evitar acumulación excesiva de LONG_REVERSION (2)
                new_q[6] = min(new_q[6], 0.040)  # Evitar acumulación excesiva de SHORT_REVERSION (6)
            for idx in [1, 2, 4, 5, 6, 8]:
                new_q[idx] = round(new_q[idx] - lcb_penalty, 5)

        # 2. Corregir sesgo BEAR_RALLY con índices exactos (1=LONG_TREND, 3=LONG_SNIPER, 4=LONG_DEF_SCALP)
        if "BEAR_RALLY" in ema_struct:
            if "BULL" in micro_trend or "TREND_BULL" in regime:
                # Si el índice 2 (LONG_REVERSION) tenía inflación histórica por el desfase previo, transferir a 1 y 3
                if new_q[2] > 0.25:
                    new_q[1] = max(new_q[1], round(new_q[2] * 0.55, 4))
                    new_q[3] = max(new_q[3], round(new_q[2] * 0.50, 4))
                    new_q[2] = round(new_q[2] * 0.35, 4)
                new_q[1] = max(new_q[1] + 0.055, 0.050)  # LONG_TREND (1)
                new_q[3] = max(new_q[3] + 0.045, 0.040)  # LONG_SNIPER (3)
                new_q[4] = max(new_q[4] + 0.040, 0.035)  # LONG_DEF_SCALP (4)
                for s_idx in [5, 6, 7, 8]:
                    new_q[s_idx] = min(new_q[s_idx] - 0.060, -0.020)
            elif "CHOP" in micro_trend and ("PEAK" in osc or "MID" in osc):
                new_q[6] = round(new_q[6] - 0.035, 5)  # Reducir sesgo de SHORT_REVERSION (6)
                new_q[7] = round(new_q[7] - 0.045, 5)  # Reducir sesgo de SHORT_SNIPER (7)

        # 3. Corregir sesgo BULL_DIP en caídas fuertes (5=SHORT_TREND, 7=SHORT_SNIPER, 8=SHORT_DEF_SCALP)
        if "BULL_DIP" in ema_struct and ("BEAR" in micro_trend or "TREND_BEAR" in regime):
            if new_q[6] > 0.25:
                new_q[5] = max(new_q[5], round(new_q[6] * 0.55, 4))
                new_q[7] = max(new_q[7], round(new_q[6] * 0.50, 4))
                new_q[6] = round(new_q[6] * 0.35, 4)
            new_q[5] = max(new_q[5] + 0.055, 0.050)  # SHORT_TREND (5)
            new_q[7] = max(new_q[7] + 0.045, 0.040)  # SHORT_SNIPER (7)
            new_q[8] = max(new_q[8] + 0.040, 0.035)  # SHORT_DEF_SCALP (8)
            for l_idx in [1, 2, 3, 4]:
                new_q[l_idx] = min(new_q[l_idx] - 0.060, -0.020)

        # 4. Premiar seguimiento de tendencia limpia con baja exposición (LOW_EXP)
        if "TREND_BULL" in regime and "LOW_EXP" in exp and "BEAR" not in micro_trend:
            new_q[1] = max(new_q[1] + 0.045, 0.055)  # LONG_TREND (1)
            new_q[3] = max(new_q[3] + 0.035, 0.045)  # LONG_SNIPER (3)
        elif "TREND_BEAR" in regime and "LOW_EXP" in exp and "BULL" not in micro_trend:
            new_q[5] = max(new_q[5] + 0.045, 0.055)  # SHORT_TREND (5)
            new_q[7] = max(new_q[7] + 0.035, 0.045)  # SHORT_SNIPER (7)

        q_table[state_key] = [round(max(-0.95, min(0.95, val)), 4) for val in new_q]
        recalibrated_count += 1

    data["q_table"] = q_table
    data["version"] = "v7.3-SATS-INDEX-ALIGNED"
    data["penalty_method"] = "LCB_Sample_Penalty_Plus_NetSats_Fee_Gate"

    digest = store.save(data)
    print("====================================================================")
    print("✅ RECALIBRACIÓN OFFLINE CON ÍNDICES ALINEADOS COMPLETADA")
    print(f"• Estados recalibrados en Q-Table: {recalibrated_count}")
    print(f"• Trades activos preservados: {len(data.get('active_trades', {}))}")
    print(f"• Piso Duro preservado: {data.get('locked_floor', 0):,} sats")
    print(f"• Integridad Atómica SHA-256: {digest[:24]}...")
    print("====================================================================")
    return True


if __name__ == "__main__":
    recalibrate_brain_for_sats_accumulation()
