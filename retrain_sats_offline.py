# -*- coding: utf-8 -*-
"""
retrain_sats_offline.py
Reentrenamiento y Recalibración Offline orientada a Sats Netos Acumulados.
Corrige el sesgo de estados BEAR_RALLY / BULL_DIP y penaliza el churning en CHOP.
"""
import os
import json
import math
import hashlib
import numpy as np
from guardian_execution_core import AtomicBrainStore


def compute_net_sats_reward(gross_atr_move, atr_pct, lev, is_chop=False, aligned_momentum=False):
    """
    Calcula la recompensa en términos de avance neto de satoshis sobre el margen arriesgado,
    descontando el taker fee round-trip de LN Markets (0.20% nominal) y penalizando CHOP.
    """
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

    # Mapa de acciones:
    # 0: NO_TRADE
    # 1: LONG_SNIPER, 2: LONG_TREND, 3: LONG_REVERSION, 4: LONG_DEF_SCALP
    # 5: SHORT_SNIPER, 6: SHORT_TREND, 7: SHORT_REVERSION, 8: SHORT_DEF_SCALP
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

        new_q = [float(x) for x in q_vec]

        # 1. Penalizar sobre-operación en RANGE_SQUEEZE + CHOP (donde las comisiones devoran sats)
        if "RANGE_SQUEEZE" in regime and "CHOP" in micro_trend:
            new_q[0] = max(new_q[0], 0.025)  # Premiar NO_TRADE en rango comprimido sin dirección
            new_q[1] -= 0.045  # Penalizar LONG_SNIPER (alto lev en chop)
            new_q[5] -= 0.045  # Penalizar SHORT_SNIPER (alto lev en chop)
            for idx in [2, 3, 4, 6, 7, 8]:
                new_q[idx] = round(new_q[idx] - lcb_penalty, 5)

        # 2. Corregir sesgo BEAR_RALLY: cuando el precio está en PEAK/MID o rompiendo al alza,
        # evitar que SHORT_REVERSION (7) o SHORT_SNIPER (5) tengan ventaja artificial
        if "BEAR_RALLY" in ema_struct:
            if "BULL" in micro_trend or "TREND_BULL" in regime:
                # Impulso alcista real (Giro en V): premiar LONG_TREND (2) y LONG_DEF_SCALP (4), penalizar SHORTS
                new_q[2] = max(new_q[2] + 0.055, 0.045)
                new_q[4] = max(new_q[4] + 0.040, 0.030)
                for s_idx in [5, 6, 7, 8]:
                    new_q[s_idx] = min(new_q[s_idx] - 0.060, -0.020)
            elif "CHOP" in micro_trend and ("PEAK" in osc or "MID" in osc):
                # En pleno empuje hacia el techo del rango, reducir el sesgo suicida de SHORT_REVERSION
                new_q[7] = round(new_q[7] - 0.035, 5)
                new_q[5] = round(new_q[5] - 0.045, 5)

        # 3. Corregir sesgo BULL_DIP en caídas fuertes:
        if "BULL_DIP" in ema_struct and ("BEAR" in micro_trend or "TREND_BEAR" in regime):
            new_q[6] = max(new_q[6] + 0.055, 0.045)
            new_q[8] = max(new_q[8] + 0.040, 0.030)
            for l_idx in [1, 2, 3, 4]:
                new_q[l_idx] = min(new_q[l_idx] - 0.060, -0.020)

        # 4. Premiar seguimiento de tendencia limpia con baja exposición (LOW_EXP)
        if "TREND_BULL" in regime and "LOW_EXP" in exp and "BEAR" not in micro_trend:
            new_q[2] = max(new_q[2] + 0.040, 0.050)
        elif "TREND_BEAR" in regime and "LOW_EXP" in exp and "BULL" not in micro_trend:
            new_q[6] = max(new_q[6] + 0.040, 0.050)

        q_table[state_key] = [round(max(-0.95, min(0.95, val)), 5) for val in new_q]
        recalibrated_count += 1

    data["q_table"] = q_table
    data["version"] = "v11.4-SATS-MOMENTUM-AUDITED"
    data["penalty_method"] = "LCB_Sample_Penalty_Plus_NetSats_Fee_Gate"

    digest = store.save(data)
    print("====================================================================")
    print("✅ REENTRENAMIENTO / RECALIBRACIÓN OFFLINE ORIENTADA A SATS COMPLETADA")
    print(f"• Estados recalibrados en Q-Table: {recalibrated_count}")
    print(f"• Versión de Cerebro: {data['version']}")
    print(f"• Integridad Atómica SHA-256: {digest[:24]}...")
    print("====================================================================")
    return True


if __name__ == "__main__":
    recalibrate_brain_for_sats_accumulation()
