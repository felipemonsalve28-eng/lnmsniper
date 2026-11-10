# -*- coding: utf-8 -*-
"""
retrain_sats_offline.py
Recalibración Offline segura orientada a Sats Netos Acumulados, Comisiones, Funding,
Slippage, Riesgo Asumido, Drawdown y Penalización de REVERSION / DEF_SCALP en
Expansiones Direccionales.
Opera por defecto sobre una copia independiente (q_brain_advanced_lnm.recalibrated.json),
usa FileLockContext + AtomicBrainStore y bloquea cualquier reemplazo en caliente si
existen operaciones abiertas (active_trades) o procesos críticos activos.
"""
import os
import sys
import json
import math
import shutil
from datetime import datetime
from guardian_execution_core import AtomicBrainStore, FileLockContext


def compute_net_sats_reward(
    gross_atr_move,
    atr_pct,
    lev,
    is_chop=False,
    aligned_momentum=False,
    funding_rate_pct=0.0,
    slippage_bps=1.5,
    drawdown_pct=0.0,
    state_quality=1.0,
):
    """
    Evalúa la recompensa económica offline considerando:
    - Sats netos realizados (movimiento bruto menos comisiones taker 0.0020)
    - Funding (carry fee)
    - Slippage estimado
    - Riesgo asumido (apalancamiento) y Drawdown
    - Calidad del estado observado
    """
    lev_f = max(1.0, float(lev))
    fee_pct = 0.0020 * lev_f
    funding_pct = abs(float(funding_rate_pct)) * lev_f
    slip_pct = (max(0.0, float(slippage_bps)) / 10000.0) * lev_f
    gross_roi = float(gross_atr_move) * float(atr_pct) * lev_f
    net_roi = gross_roi - fee_pct - funding_pct - slip_pct

    risk_penalty = max(0.0, (lev_f - 35.0) * 0.0012) + max(0.0, float(drawdown_pct) * 0.25)
    churn_penalty = 0.035 if is_chop else 0.0
    quality_mult = max(0.50, min(1.25, float(state_quality)))
    momentum_bonus = 0.045 if (aligned_momentum and net_roi > 0) else 0.0

    raw_reward = (net_roi * quality_mult) - churn_penalty - risk_penalty + momentum_bonus
    return round(max(-1.0, min(1.0, raw_reward)), 5)


def recalibrate_brain_for_sats_accumulation(
    brain_path="q_brain_advanced_lnm.json",
    output_path=None,
    apply_in_place=False,
):
    if not os.path.exists(brain_path):
        print(f"❌ No existe {brain_path}.")
        return False

    store_in = AtomicBrainStore(brain_path)
    data = store_in.load()
    if not isinstance(data, dict) or "q_table" not in data:
        print(f"❌ Archivo inválido o corrupto: {brain_path}")
        return False

    active_trades = data.get("active_trades", {})
    if apply_in_place and len(active_trades) > 0:
        print(
            f"🛑 [BLOQUEO DE SEGURIDAD] Hay {len(active_trades)} operaciones activas en el cerebro. "
            f"No se permite recalibrar en caliente mientras existan posiciones abiertas."
        )
        apply_in_place = False

    if output_path is None:
        output_path = brain_path if apply_in_place else f"{brain_path}.recalibrated.json"

    ts_bak = f"{brain_path}.pre_recal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
    with FileLockContext(brain_path, exclusive=False, timeout=4.0):
        shutil.copy2(brain_path, ts_bak)

    q_table = data.get("q_table", {})
    state_visits = data.get("state_visits", {})
    style_metrics = data.get("style_metrics", {})

    # Evaluar eficiencia histórica real de REVERSION y DEF_SCALP (sats netos vs comisiones)
    rev_stats = style_metrics.get("REVERSION", {})
    def_stats = style_metrics.get("DEF_SCALP", {})
    rev_net = float(rev_stats.get("net_pnl_sats", 0))
    rev_fees = max(1.0, float(rev_stats.get("fees_sats", 1)))
    def_net = float(def_stats.get("net_pnl_sats", 0))
    def_fees = max(1.0, float(def_stats.get("fees_sats", 1)))

    rev_fee_drag = 0.035 if (rev_net / rev_fees) < 0.15 else 0.0
    def_fee_drag = 0.035 if (def_net / def_fees) < 0.15 else 0.0

    recalibrated_count = 0
    policy_shifts = 0
    no_trade_promoted = 0

    for state_key, q_vec in list(q_table.items()):
        if not isinstance(q_vec, list) or len(q_vec) < 9:
            continue

        old_best = int(max(range(9), key=lambda i: float(q_vec[i])))
        parts = str(state_key).split("|")
        regime = parts[0] if len(parts) > 0 else ""
        osc = parts[1] if len(parts) > 1 else ""
        exp = parts[2] if len(parts) > 2 else ""
        macro_trend = parts[3] if len(parts) > 3 else ""
        ema_struct = parts[4] if len(parts) > 4 else ""

        visits = int(state_visits.get(state_key, 1))
        lcb_penalty = round(0.015 / math.sqrt(max(1, visits)), 5)

        new_q = [max(-0.85, min(0.85, float(x))) for x in q_vec]
        new_q[0] = 0.0

        # 1. Penalizar REVERSION (2, 6) y DEF_SCALP (4, 8) cuando operan contra expansiones direccionales
        is_bull_expansion = (
            regime in ("TREND_BULL", "VOLATILITY_EXPANSION")
            or (macro_trend == "BULL" and ema_struct == "FULL_BULL")
        )
        is_bear_expansion = (
            regime in ("TREND_BEAR", "VOLATILITY_EXPANSION")
            or (macro_trend == "BEAR" and ema_struct == "FULL_BEAR")
        )

        if is_bull_expansion and ema_struct in ("FULL_BULL", "BULL_STRONG"):
            # En expansión alcista, penalizar SHORT_REVERSION (6) y SHORT_DEF_SCALP (8), y moderar LONG_REVERSION/DEF_SCALP si hay arrastre de fees
            for bad_short in (6, 8):
                if new_q[bad_short] > -0.25:
                    new_q[bad_short] = round(new_q[bad_short] * 0.45 - 0.065 - rev_fee_drag, 5)
            new_q[2] = round(new_q[2] - (0.5 * rev_fee_drag), 5)
            new_q[4] = round(new_q[4] - (0.5 * def_fee_drag), 5)

        if is_bear_expansion and ema_struct in ("FULL_BEAR", "BEAR_STRONG"):
            # En expansión bajista, penalizar LONG_REVERSION (2) y LONG_DEF_SCALP (4)
            for bad_long in (2, 4):
                if new_q[bad_long] > -0.25:
                    new_q[bad_long] = round(new_q[bad_long] * 0.45 - 0.065 - rev_fee_drag, 5)
            new_q[6] = round(new_q[6] - (0.5 * rev_fee_drag), 5)
            new_q[8] = round(new_q[8] - (0.5 * def_fee_drag), 5)

        # 2. Reglas de protección en RANGE_SQUEEZE y CHOP_HIGHVOL / EXTREME_VOLATILITY
        if "RANGE_SQUEEZE" in regime and "BEAR_RALLY" in ema_struct:
            if new_q[2] > 0.0:
                c2 = new_q[2] * 0.55
                new_q[2] = round(new_q[2] - c2, 5)
                new_q[1] = round(new_q[1] + c2 * 0.25, 5)
                new_q[3] = round(new_q[3] + c2 * 0.30, 5)
                new_q[4] = round(new_q[4] + c2 * 0.45, 5)
            else:
                new_q[2] = round(new_q[2] - 0.065, 5)
            if new_q[6] > 0.0:
                c6 = new_q[6] * 0.55
                new_q[6] = round(new_q[6] - c6, 5)
                new_q[5] = round(new_q[5] + c6 * 0.25, 5)
                new_q[7] = round(new_q[7] + c6 * 0.30, 5)
                new_q[8] = round(new_q[8] + c6 * 0.45, 5)
            else:
                new_q[6] = round(new_q[6] - 0.065, 5)

        if "RANGE_SQUEEZE" in regime and "BULL" in macro_trend and "BULL_DIP" in ema_struct:
            new_q[0] = 0.0
            for s_idx in (5, 6, 7, 8):
                if new_q[s_idx] > 0.0:
                    new_q[s_idx] = round(new_q[s_idx] * 0.25 - 0.12, 5)

        if ("RANGE_SQUEEZE" in regime or "CHOP_HIGHVOL" in regime) and ("CHOP" in macro_trend or "HIGH_EXP" in exp):
            new_q[0] = 0.0
            new_q[3] -= 0.045
            new_q[7] -= 0.045
            if "HIGH_EXP" in exp:
                new_q[2] = min(new_q[2], 0.0)
                new_q[6] = min(new_q[6], 0.0)
                new_q[4] = min(new_q[4], 0.0)
                new_q[8] = min(new_q[8], 0.0)
            for idx in [1, 2, 4, 5, 6, 8]:
                new_q[idx] = round(new_q[idx] - lcb_penalty, 5)

        # 3. Favorecer decisiones conservadoras (NO_TRADE = 0, base 0.0 sats) cuando ninguna acción supere expectativa neta mínima
        max_dir_q = max(new_q[1:])
        if max_dir_q < 0.010:
            for idx in range(1, 9):
                if new_q[idx] > 0.0:
                    new_q[idx] = round(new_q[idx] - 0.010, 5)
        new_q[0] = 0.0

        q_table[state_key] = [round(max(-0.95, min(0.95, val)), 4) for val in new_q]
        new_best = int(max(range(9), key=lambda i: float(q_table[state_key][i])))
        if new_best != old_best:
            policy_shifts += 1
            if new_best == 0:
                no_trade_promoted += 1
        recalibrated_count += 1

    data["q_table"] = q_table
    data["version"] = "v7.3-SATS-NET-BALANCE-VERIFIED"
    store_out = AtomicBrainStore(output_path)
    digest = store_out.save(data)

    print("====================================================================")
    print("✅ RECALIBRACIÓN OFFLINE AISLADA COMPLETADA")
    print(f"• Archivo de salida: {output_path}")
    print(f"• Backup previo: {ts_bak}")
    print(f"• Estados evaluados: {recalibrated_count} | Cambios de política: {policy_shifts} (NO_TRADE promovido: {no_trade_promoted})")
    print(f"• Integridad SHA-256: {digest[:24]}...")
    print("====================================================================")
    return True


if __name__ == "__main__":
    apply_flag = "--apply" in sys.argv
    recalibrate_brain_for_sats_accumulation(apply_in_place=apply_flag)
