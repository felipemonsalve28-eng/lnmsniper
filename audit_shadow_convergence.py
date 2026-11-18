#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_shadow_convergence.py
Auditoría cuantitativa externa de solo lectura para evaluar la convergencia de
Bellman en Modo Sombra (Testnet) sobre la Ventana Móvil de Certificación (últimos
20 cierres virtuales) y emitir veredicto de volcamiento a Mainnet.
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_PATH = os.path.join(BASE_DIR, "telemetry_live_testnet.json")
SHADOW_LOG_PATH = os.path.join(BASE_DIR, "logs", "shadow_signals.jsonl")
TRANSITIONS_TESTNET_PATH = os.path.join(BASE_DIR, "logs", "transitions_audit_testnet.jsonl")
TRANSITIONS_MAIN_PATH = os.path.join(BASE_DIR, "logs", "transitions_audit.jsonl")
MAINNET_BRAIN_PATH = os.path.join(BASE_DIR, "q_brain_advanced_lnm.json")
TESTNET_BRAIN_PATH = os.path.join(BASE_DIR, "q_brain_testnet_lnm.json")

CLOSED_SHADOW_STATUSES = ("CLOSED_TP", "CLOSED_TRAIL_PROFIT", "CLOSED_SL", "CLOSED_TIMEOUT")
ROLLING_CERT_WINDOW = 20
CANONICAL_REF_MARGIN_SATS = 1000.0


def _safe_float(value, default=0.0):
    try:
        if value is None or isinstance(value, bool):
            return float(default)
        num = float(value)
        if num != num or num in (float("inf"), float("-inf")):
            return float(default)
        return num
    except (TypeError, ValueError):
        return float(default)


def load_testnet_telemetry(path):
    """Extrae signos vitales RL desde telemetry_live_testnet.json de forma segura."""
    defaults = {
        "telemetry_found": False,
        "timestamp": "N/A",
        "td_avg50": 999.0,
        "q_bias": 9999.0,
        "h_norm": 0.0,
        "guard_status": "UNKNOWN",
        "shadow_active": False,
    }
    if not os.path.exists(path):
        return defaults

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return defaults

    if not isinstance(data, dict):
        return defaults

    rl = data.get("rl_metrics")
    if not isinstance(rl, dict):
        return defaults

    adj = rl.get("autonomous_adjustments")
    if not isinstance(adj, dict):
        adj = {}

    return {
        "telemetry_found": True,
        "timestamp": str(data.get("timestamp", "N/A")),
        "td_avg50": _safe_float(rl.get("rolling_td_error_avg"), 999.0),
        "q_bias": _safe_float(rl.get("q_bias_sats"), 9999.0),
        "h_norm": _safe_float(rl.get("normalized_policy_entropy"), 0.0),
        "guard_status": str(rl.get("learning_guard_status", "UNKNOWN")),
        "shadow_active": bool(adj.get("runtime_shadow_active", False)),
    }


def _summarize_records(records):
    total = len(records)
    wins = 0
    net_sum = 0.0
    norm_1k_sum = 0.0
    cost_sum = 0.0
    norm_cost_sum = 0.0
    tp_count = 0
    trail_profit_count = 0
    sl_count = 0
    timeout_count = 0
    shadow_virtual_count = 0
    live_signet_count = 0

    for item in records:
        net_sats = item["net_sats"]
        norm_1k = item["norm_1k"]
        cost_sats = item["cost_sats"]
        norm_cost_1k = item["norm_cost_1k"]
        status = item["status"]
        source = item.get("source", "SHADOW_VIRTUAL")

        net_sum += net_sats
        norm_1k_sum += norm_1k
        cost_sum += cost_sats
        norm_cost_sum += norm_cost_1k
        if net_sats > 0.0:
            wins += 1
        if status == "CLOSED_TP":
            tp_count += 1
        elif status == "CLOSED_TRAIL_PROFIT":
            trail_profit_count += 1
        elif status == "CLOSED_SL":
            sl_count += 1
        elif status == "CLOSED_TIMEOUT":
            timeout_count += 1
        if source == "LIVE_SIGNET":
            live_signet_count += 1
        else:
            shadow_virtual_count += 1

    win_rate = (float(wins) / float(total) * 100.0) if total > 0 else 0.0
    virtual_fer = (net_sum / cost_sum) if cost_sum > 0.0 else (1.0 if net_sum > 0.0 else 0.0)
    virtual_fer_1k = (norm_1k_sum / norm_cost_sum) if norm_cost_sum > 0.0 else virtual_fer
    return {
        "count": total,
        "wins": wins,
        "win_rate": win_rate,
        "net_pnl_sats": net_sum,
        "norm_net_pnl_1k": norm_1k_sum,
        "total_cost_sats": cost_sum,
        "virtual_fer": virtual_fer,
        "virtual_fer_1k": virtual_fer_1k,
        "tp_count": tp_count,
        "trail_profit_count": trail_profit_count,
        "sl_count": sl_count,
        "timeout_count": timeout_count,
        "shadow_virtual_count": shadow_virtual_count,
        "live_signet_count": live_signet_count,
    }


def _is_synthetic_tid(trade_id):
    tid_str = str(trade_id or "").strip()
    if not tid_str or tid_str.lower() in ("none", "null"):
        return True
    if tid_str.startswith(("sim-", "test-", "trade-", "mock-", "shadow-")):
        return True
    return False


def _load_live_testnet_transitions():
    """
    Lee cierres reales de Testnet desde logs/transitions_audit_testnet.jsonl y
    logs/transitions_audit.jsonl excluyendo registros sintéticos y de Mainnet.
    """
    mainnet_ids = set()
    testnet_ids = set()
    if os.path.exists(MAINNET_BRAIN_PATH):
        try:
            with open(MAINNET_BRAIN_PATH, "r", encoding="utf-8") as fh:
                mb = json.load(fh)
                if isinstance(mb, dict):
                    mainnet_ids = set(str(x) for x in (mb.get("processed_closed_trade_ids") or []))
        except Exception:
            pass
    if os.path.exists(TESTNET_BRAIN_PATH):
        try:
            with open(TESTNET_BRAIN_PATH, "r", encoding="utf-8") as fh:
                tb = json.load(fh)
                if isinstance(tb, dict):
                    testnet_ids = set(str(x) for x in (tb.get("processed_closed_trade_ids") or []))
        except Exception:
            pass

    seen_ids = set()
    live_records = []
    for t_path in (TRANSITIONS_TESTNET_PATH, TRANSITIONS_MAIN_PATH):
        if not os.path.exists(t_path):
            continue
        try:
            with open(t_path, "r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    tid = str(rec.get("trade_id") or "").strip()
                    if _is_synthetic_tid(tid) or tid in seen_ids:
                        continue
                    env_tag = str(rec.get("env") or "").strip().lower()
                    if env_tag == "mainnet":
                        continue
                    if not env_tag and tid in mainnet_ids and tid not in testnet_ids:
                        continue

                    seen_ids.add(tid)
                    net_sats = _safe_float(rec.get("net_realized_sats"), 0.0)
                    cost_sats = max(0.0, _safe_float(rec.get("fee_sats"), 0.0))
                    raw_margin = _safe_float(rec.get("margin_sats"), 0.0)
                    if raw_margin < CANONICAL_REF_MARGIN_SATS:
                        econ_r = abs(_safe_float(rec.get("econ_reward_sats"), 0.0))
                        shaped_r = abs(_safe_float(rec.get("shaped_reward"), 0.0))
                        if 0.01 < shaped_r < 1.99 and econ_r > 0.0:
                            raw_margin = (econ_r / (shaped_r * 100.0)) * CANONICAL_REF_MARGIN_SATS
                        elif cost_sats > 0.0:
                            raw_margin = max(CANONICAL_REF_MARGIN_SATS, cost_sats * 25.0)
                    margin_sats = max(CANONICAL_REF_MARGIN_SATS, raw_margin)
                    scale_eta = CANONICAL_REF_MARGIN_SATS / margin_sats
                    norm_1k = net_sats * scale_eta
                    norm_cost_1k = cost_sats * scale_eta
                    status = "CLOSED_TP" if net_sats > 0.0 else "CLOSED_SL"

                    live_records.append({
                        "id": tid,
                        "status": status,
                        "source": "LIVE_SIGNET",
                        "timestamp": int(_safe_float(rec.get("timestamp"), 0.0)),
                        "net_sats": net_sats,
                        "norm_1k": norm_1k,
                        "cost_sats": cost_sats,
                        "norm_cost_1k": norm_cost_1k,
                        "margin_sats": margin_sats,
                    })
        except OSError:
            continue
    return live_records


def load_shadow_accounting(path, window_size=ROLLING_CERT_WINDOW):
    """
    Calcula tanto el acumulado histórico global como la Ventana Móvil de Certificación
    (últimos 20 cierres virtuales) desde logs/shadow_signals.jsonl.
    """
    empty_summary = _summarize_records([])
    stats = {
        "shadow_log_found": False,
        "total_shadow_trades": 0,
        "virtual_net_pnl_sats": 0.0,
        "virtual_norm_net_pnl_1k": 0.0,
        "virtual_win_rate": 0.0,
        "virtual_fer_global": 0.0,
        "wins": 0,
        "tp_count": 0,
        "trail_profit_count": 0,
        "sl_count": 0,
        "timeout_count": 0,
        "rolling_20": [],
        "rolling_window_size": 0,
        "rolling_net_pnl_sats": 0.0,
        "rolling_norm_net_pnl_1k": 0.0,
        "rolling_win_rate": 0.0,
        "rolling_virtual_fer": 0.0,
        "rolling_virtual_fer_1k": 0.0,
        "rolling_wins": 0,
        "rolling_tp_count": 0,
        "rolling_trail_profit_count": 0,
        "rolling_sl_count": 0,
        "rolling_timeout_count": 0,
        "rolling_shadow_virtual_count": 0,
        "rolling_live_signet_count": 0,
        "global_summary": empty_summary,
        "rolling_summary": empty_summary,
    }
    closed_records = []
    if os.path.exists(path):
        stats["shadow_log_found"] = True
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    status = str(rec.get("status", "")).strip()
                    if status not in CLOSED_SHADOW_STATUSES:
                        continue

                    net_sats = _safe_float(rec.get("net_realized_sats"), 0.0)
                    margin_sats = max(CANONICAL_REF_MARGIN_SATS, _safe_float(rec.get("margin_sats"), CANONICAL_REF_MARGIN_SATS))
                    scale_eta = CANONICAL_REF_MARGIN_SATS / margin_sats

                    if rec.get("norm_net_sats_1k") is not None:
                        norm_1k = _safe_float(rec.get("norm_net_sats_1k"), net_sats * scale_eta)
                    else:
                        norm_1k = net_sats * scale_eta

                    cost_sats = max(0.0, _safe_float(rec.get("total_cost_sats"), 0.0))
                    if cost_sats <= 0.0:
                        lev = max(1.0, _safe_float(rec.get("leverage"), 1.0))
                        cost_sats = margin_sats * lev * 0.002
                    norm_cost_1k = cost_sats * scale_eta

                    closed_records.append({
                        "id": str(rec.get("id", "")),
                        "status": status,
                        "source": "SHADOW_VIRTUAL",
                        "timestamp": int(_safe_float(rec.get("closed_at") or rec.get("timestamp"), 0.0)),
                        "net_sats": net_sats,
                        "norm_1k": norm_1k,
                        "cost_sats": cost_sats,
                        "norm_cost_1k": norm_cost_1k,
                        "margin_sats": margin_sats,
                    })
        except OSError:
            return stats

    global_sum = _summarize_records(closed_records)
    win_n = max(1, int(window_size))
    combined_for_rolling = list(closed_records)
    if len(combined_for_rolling) < win_n:
        live_signet_records = _load_live_testnet_transitions()
        if live_signet_records:
            needed = win_n - len(combined_for_rolling)
            supplement = live_signet_records[-needed:]
            combined_for_rolling = supplement + combined_for_rolling
            combined_for_rolling.sort(key=lambda r: int(r.get("timestamp", 0)))

    rolling_20 = combined_for_rolling[-win_n:]
    rolling_sum = _summarize_records(rolling_20)

    stats.update({
        "total_shadow_trades": global_sum["count"],
        "virtual_net_pnl_sats": global_sum["net_pnl_sats"],
        "virtual_norm_net_pnl_1k": global_sum["norm_net_pnl_1k"],
        "virtual_win_rate": global_sum["win_rate"],
        "virtual_fer_global": global_sum["virtual_fer"],
        "wins": global_sum["wins"],
        "tp_count": global_sum["tp_count"],
        "trail_profit_count": global_sum["trail_profit_count"],
        "sl_count": global_sum["sl_count"],
        "timeout_count": global_sum["timeout_count"],
        "rolling_20": rolling_20,
        "rolling_window_size": rolling_sum["count"],
        "rolling_net_pnl_sats": rolling_sum["net_pnl_sats"],
        "rolling_norm_net_pnl_1k": rolling_sum["norm_net_pnl_1k"],
        "rolling_win_rate": rolling_sum["win_rate"],
        "rolling_virtual_fer": rolling_sum["virtual_fer"],
        "rolling_virtual_fer_1k": rolling_sum["virtual_fer_1k"],
        "rolling_wins": rolling_sum["wins"],
        "rolling_tp_count": rolling_sum["tp_count"],
        "rolling_trail_profit_count": rolling_sum["trail_profit_count"],
        "rolling_sl_count": rolling_sum["sl_count"],
        "rolling_timeout_count": rolling_sum["timeout_count"],
        "rolling_shadow_virtual_count": rolling_sum["shadow_virtual_count"],
        "rolling_live_signet_count": rolling_sum["live_signet_count"],
        "global_summary": global_sum,
        "rolling_summary": rolling_sum,
    })
    return stats


def evaluate_sync_readiness(tel, shadow):
    """Evalúa las condiciones cuantitativas de convergencia sobre la Ventana Móvil (últimos 20)."""
    rolling_20 = shadow.get("rolling_20", [])
    rolling_net_pnl = _safe_float(shadow.get("rolling_net_pnl_sats"), 0.0)
    rolling_norm_pnl = _safe_float(shadow.get("rolling_norm_net_pnl_1k"), 0.0)
    rolling_fer = _safe_float(shadow.get("rolling_virtual_fer_1k"), 0.0)

    cond_a = tel["telemetry_found"] and (tel["td_avg50"] < 0.45)
    cond_b = tel["telemetry_found"] and (abs(tel["q_bias"]) <= 25.0)
    cond_c = tel["telemetry_found"] and (tel["h_norm"] >= 0.250)
    cond_d = (len(rolling_20) >= 15) and (rolling_net_pnl > 0.0) and (rolling_norm_pnl > 0.0)
    cond_fer = (len(rolling_20) >= 15) and (rolling_fer >= 1.20)
    return {
        "A": cond_a,
        "B": cond_b,
        "C": cond_c,
        "D": cond_d,
        "FER_OK": cond_fer,
        "approved": bool(cond_a and cond_b and cond_c and cond_d),
    }


def print_report(tel, shadow, checks):
    sep = "=" * 82
    line = "-" * 82
    badge = lambda ok: "[PASS]" if ok else "[FAIL]"

    print(sep)
    print("  LNM SNIPER v7.3+ — AUDITORÍA CUANTITATIVA DE CONVERGENCIA EN MODO SOMBRA")
    print(sep)
    tel_src = "OK" if tel["telemetry_found"] else "NO DISPONIBLE (valores conservadores)"
    sh_src = "OK" if shadow["shadow_log_found"] else "AUSENTE (valores en 0)"
    print(f"  Fuente Telemetría : telemetry_live_testnet.json [{tel_src}] | ts={tel['timestamp']}")
    print(f"  Fuente Shadow Log : logs/shadow_signals.jsonl [{sh_src}]")

    print(line)
    print("  1. TELEMETRÍA IPC (TESTNET) VS. UMBRALES EXIGIDOS")
    print(line)
    print(f"  {'Métrica':<34} | {'Valor Actual':<22} | {'Umbral Exigido':<20}")
    print(f"  {'-' * 34}-+-{'-' * 22}-+-{'-' * 20}")
    print(f"  {'td_avg50 (TD-Error Avg50)':<34} | {tel['td_avg50']:<22.4f} | {'< 0.4500':<20}")
    print(f"  {'q_bias (Q-Bias Sats / 1k Margen)':<34} | {tel['q_bias']:<+22.2f} | {'|Q-Bias| <= 25.00':<20}")
    print(f"  {'h_norm (Entropía Normalizada)':<34} | {tel['h_norm']:<22.4f} | {'>= 0.2500':<20}")
    print(f"  {'guard_status':<34} | {tel['guard_status']:<22} | {'Informativo':<20}")
    print(f"  {'shadow_active':<34} | {str(tel['shadow_active']):<22} | {'Informativo':<20}")

    print(line)
    print("  2A. VENTANA MÓVIL DE CERTIFICACIÓN (ÚLTIMOS 20 CIERRES VIRTUALES)")
    print(line)
    print(f"  {'Indicador (Rolling 20)':<34} | {'Valor Medido':<22} | {'Detalle / Criterio':<20}")
    print(f"  {'-' * 34}-+-{'-' * 22}-+-{'-' * 20}")
    print(
        f"  {'Rolling_20_Trades':<34} | {shadow['rolling_window_size']:<22d} | "
        f"LIVE_SIGNET:{shadow.get('rolling_live_signet_count', 0)} "
        f"SHADOW_VIRTUAL:{shadow.get('rolling_shadow_virtual_count', 0)} "
        f"(TP:{shadow['rolling_tp_count']} TR:{shadow['rolling_trail_profit_count']} "
        f"SL:{shadow['rolling_sl_count']} TO:{shadow['rolling_timeout_count']})"
    )
    print(
        f"  {'Rolling_20_Net_PnL_Sats':<34} | {shadow['rolling_net_pnl_sats']:<+22.1f} | "
        f"{'> 0.0 sats (bruto)':<20}"
    )
    print(
        f"  {'Rolling_20_Norm_PnL_1k':<34} | {shadow['rolling_norm_net_pnl_1k']:<+22.2f} | "
        f"{'> 0.0 sats / 1k mrg':<20}"
    )
    r_wr_str = f"{shadow['rolling_win_rate']:.2f}%"
    print(
        f"  {'Rolling_20_Win_Rate':<34} | {r_wr_str:<22} | "
        f"{shadow['rolling_wins']}/{shadow['rolling_window_size']} ganadoras"
    )
    print(
        f"  {'Rolling_20_Virtual_FER':<34} | {shadow['rolling_virtual_fer_1k']:<+22.2f} | "
        f"{'>= 1.20 (objetivo)':<20}"
    )

    print(line)
    print("  2B. ACUMULADO HISTÓRICO GLOBAL (SOLO REFERENCIA APPEND-ONLY)")
    print(line)
    print(
        f"  {'Global_Shadow_Trades':<34} | {shadow['total_shadow_trades']:<22d} | "
        f"TP:{shadow['tp_count']} TR:{shadow['trail_profit_count']} "
        f"SL:{shadow['sl_count']} TO:{shadow['timeout_count']}"
    )
    print(
        f"  {'Global_Virtual_Net_PnL_Sats':<34} | {shadow['virtual_net_pnl_sats']:<+22.1f} | "
        f"Norm1k: {shadow['virtual_norm_net_pnl_1k']:+.2f}s"
    )
    g_wr_str = f"{shadow['virtual_win_rate']:.2f}%"
    print(
        f"  {'Global_Virtual_Win_Rate':<34} | {g_wr_str:<22} | "
        f"{shadow['wins']}/{shadow['total_shadow_trades']} ganadoras | FER:{shadow['virtual_fer_global']:+.2f}"
    )

    print(line)
    print("  3. MATRIZ DE DECISIÓN DE VOLCAMIENTO (SYNC TO MAINNET)")
    print(line)
    print(f"  Condición A (Estabilidad de Bellman: td_avg50 < 0.45)                   : {badge(checks['A'])}")
    print(f"  Condición B (Calibración Q Canónica: |q_bias| <= 25.0 sats)             : {badge(checks['B'])}")
    print(f"  Condición C (Diversidad de Política: h_norm >= 0.250)                   : {badge(checks['C'])}")
    print(f"  Condición D (Ventana Móvil 20: N >= 15 & Rolling PnL > 0)               : {badge(checks['D'])}")
    print(f"  Referencia FER Virtual (Ventana Móvil 20: FER_1k >= 1.20)               : {badge(checks['FER_OK'])}")

    print(sep)
    if checks["approved"]:
        print("\033[92m[🟢 VEREDICTO: VOLCAMIENTO A MAINNET APROBADO] - Ejecutar: python3 sniper_cli.py brain sync\033[0m")
    else:
        print("\033[91m[🔴 VEREDICTO: RECHAZADO] - La política aún requiere convergencia en la sombra.\033[0m")
    print(sep)


def main():
    tel = load_testnet_telemetry(TELEMETRY_PATH)
    shadow = load_shadow_accounting(SHADOW_LOG_PATH)
    checks = evaluate_sync_readiness(tel, shadow)
    print_report(tel, shadow, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
