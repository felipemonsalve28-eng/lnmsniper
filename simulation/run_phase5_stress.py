# -*- coding: utf-8 -*-
"""
simulation/run_phase5_stress.py
Matriz de estrés de microestructura Fase 5 (S0, S1, S2) en WSL sobre Fold 1 OOS o 18M.
Solo stdlib + módulos del repositorio; jamás muta cachés WSL ni cerebros del repo.
"""
import argparse
import json
import os
import pickle
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from simulation.sim_lnm_exchange import MicrostructureStressConfig
from simulation.sim_training_environment import SimTrainingEnvironment

DEFAULT_CACHE_DIR = "/home/akapana/.cache/sniper_wf_s2_cache"
DEFAULT_DATASET_CACHE = os.path.join(REPO_ROOT, "simulation", "data", "binance_btc_1m.json")
DEFAULT_INITIAL_BALANCE = 100000


def _parse_scenarios(raw: str) -> List[Tuple[str, MicrostructureStressConfig]]:
    items = [part.strip().upper() for part in str(raw).split(",") if part.strip()]
    if not items:
        raise ValueError("Debe especificar al menos un escenario en --scenarios.")
    seen = set()
    parsed: List[Tuple[str, MicrostructureStressConfig]] = []
    for name in items:
        if name in seen:
            continue
        seen.add(name)
        parsed.append((name, MicrostructureStressConfig.from_preset(name)))
    return parsed


def _parse_fee_rt_bps(raw: str) -> List[float]:
    items = [part.strip() for part in str(raw).split(",") if part.strip()]
    if not items:
        raise ValueError("Debe especificar al menos una tarifa en --fee-rt-bps.")
    fees: List[float] = []
    for part in items:
        val = float(part)
        if val <= 0.0:
            raise ValueError(f"La tarifa round-trip debe ser > 0 bps: {val}")
        fees.append(val)
    return fees


def _atomic_write_json(path: str, payload: dict) -> None:
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".phase5_stress_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, abs_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _load_months_meta(cache_dir: str) -> Dict[int, dict]:
    meta_path = os.path.join(cache_dir, "months_meta.json")
    if not os.path.exists(meta_path):
        return {}
    with open(meta_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("months_meta", []) if isinstance(data, dict) else []
    return {int(item["idx"]): item for item in entries if isinstance(item, dict) and "idx" in item}


def _required_pkl_paths(scope: str, cache_dir: str) -> List[str]:
    paths: List[str] = []
    if scope == "18m":
        for w in range(52):
            paths.append(os.path.join(cache_dir, f"train_w{w:02d}.pkl"))
    for idx in range(12, 18):
        paths.append(os.path.join(cache_dir, f"oos_m{idx}.pkl"))
    return paths


def _run_dry_check(
    scope: str,
    scenarios: List[Tuple[str, MicrostructureStressConfig]],
    fee_rt_list: List[float],
    cache_dir: str,
    dataset_cache: str,
) -> int:
    missing: List[str] = []
    seed_brain = os.path.join(cache_dir, "q_brain_s2_clean_seed.json")
    if not os.path.exists(seed_brain):
        missing.append(seed_brain)

    req_pkls = _required_pkl_paths(scope, cache_dir)
    for pkl_path in req_pkls:
        if not os.path.exists(pkl_path):
            missing.append(pkl_path)

    print(f"[DRY-CHECK] scope={scope} scenarios={[s[0] for s in scenarios]} fee_rt_bps={fee_rt_list}")
    print(
        f"[DRY-CHECK] cache_dir={cache_dir} "
        f"(seed={'OK' if os.path.exists(seed_brain) else 'MISSING'}, pkls={len(req_pkls) - sum(1 for p in req_pkls if p in missing)}/{len(req_pkls)})"
    )
    if scope == "18m":
        print(f"[DRY-CHECK] dataset_cache={dataset_cache} ({'OK' if os.path.exists(dataset_cache) else 'OPTIONAL/MISSING'})")

    if missing:
        print("[ERROR] Faltan artefactos requeridos:")
        for item in missing[:12]:
            print(f"  - {item}")
        if len(missing) > 12:
            print(f"  ... y {len(missing) - 12} más")
        return 1

    print("[OK] Dry-check superado: imports, presets, tarifas y cachés válidos.")
    return 0


def _load_scope_blocks(scope: str, cache_dir: str) -> List[Tuple[str, str, Any, bool, bool]]:
    """Retorna tuplas (block_key, label, block_obj, is_oos_12m, is_m16) en paridad 1:1 con el benchmark."""
    meta_by_idx = _load_months_meta(cache_dir)
    blocks: List[Tuple[str, str, Any, bool, bool]] = []

    if scope == "18m":
        for w in range(52):
            pkl_path = os.path.join(cache_dir, f"train_w{w:02d}.pkl")
            if not os.path.exists(pkl_path):
                raise FileNotFoundError(f"Falta bloque semanal 18M requerido: {pkl_path}")
            with open(pkl_path, "rb") as fh:
                block = pickle.load(fh)
            is_oos_f2 = w < 26
            tag = "F2" if is_oos_f2 else "IS"
            blocks.append((f"w{w:02d}", f"W{w:02d}({tag})", block, is_oos_f2, False))

    for idx in range(12, 18):
        meta = meta_by_idx.get(idx, {})
        label = str(meta.get("label") or f"M{idx + 1:02d}")
        pkl_path = os.path.join(cache_dir, f"oos_m{idx}.pkl")
        if not os.path.exists(pkl_path):
            raise FileNotFoundError(f"Falta bloque Fold 1 OOS requerido: {pkl_path}")
        with open(pkl_path, "rb") as fh:
            block = pickle.load(fh)
        blocks.append((f"m{idx:02d}", label, block, True, idx == 15))

    return blocks


def _evaluate_cell(
    scope: str,
    scenario_name: str,
    stress_cfg: MicrostructureStressConfig,
    fee_rt_bps: float,
    blocks: List[Tuple[str, str, Any, bool, bool]],
    seed_brain: str,
    initial_balance: int,
) -> Dict[str, Any]:
    taker_fee_rate = (float(fee_rt_bps) / 2.0) / 10000.0
    total_trades = 0
    total_net = 0
    oos_12m_trades = 0
    oos_12m_net = 0
    oos_12m_gross_win = 0
    oos_12m_gross_loss = 0
    gross_win = 0
    gross_loss = 0
    max_dd_pct = 0.0
    dd_45 = 0
    ddg = 0
    hard_floor_violations = 0
    entry_events = 0
    sl_events = 0
    entry_slip_bps_sum = 0.0
    sl_slip_bps_sum = 0.0
    m16_info: Optional[Dict[str, Any]] = None
    block_rows: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as td:
        for b_i, (block_key, label, block, is_oos_12m, is_m16) in enumerate(blocks):
            b_dst = os.path.join(td, f"brain_{b_i:02d}_{block_key}.json")
            h_dst = os.path.join(td, f"hedge_{b_i:02d}_{block_key}.json")
            shutil.copy2(seed_brain, b_dst)
            if os.path.exists(seed_brain + ".sha256"):
                shutil.copy2(seed_brain + ".sha256", b_dst + ".sha256")

            env = SimTrainingEnvironment(
                candidate_brain_file=b_dst,
                candidate_hedge_file=h_dst,
                initial_balance_sats=initial_balance,
                taker_fee_rate=taker_fee_rate,
                stress_config=stress_cfg,
            )
            res = env.run_episode(block, initial_balance=initial_balance)
            closed = res.get("closed_trades", [])
            n = int(res.get("total_settled_trades", len(closed)))
            net = int(res.get("net_profit_sats", 0))
            dd_pct = float(res.get("max_drawdown_pct", 0.0))
            blk_ddg = int(getattr(env, "dd_guard_events", 0))

            b_win = sum(int(r.get("net_pnl_sats", 0)) for r in closed if int(r.get("net_pnl_sats", 0)) > 0)
            b_loss = sum(abs(int(r.get("net_pnl_sats", 0))) for r in closed if int(r.get("net_pnl_sats", 0)) < 0)
            total_trades += n
            total_net += net
            gross_win += b_win
            gross_loss += b_loss
            if is_oos_12m:
                oos_12m_trades += n
                oos_12m_net += net
                oos_12m_gross_win += b_win
                oos_12m_gross_loss += b_loss
            max_dd_pct = max(max_dd_pct, dd_pct)
            if dd_pct >= 4.5:
                dd_45 += 1
            ddg += blk_ddg

            ex = env.exchange
            entry_events += int(getattr(ex, "stress_entry_events", 0))
            sl_events += int(getattr(ex, "stress_sl_events", 0))
            entry_slip_bps_sum += float(getattr(ex, "stress_entry_slip_bps_sum", 0.0))
            sl_slip_bps_sum += float(getattr(ex, "stress_sl_slip_bps_sum", 0.0))

            final_eq = int(res.get("final_equity_sats", initial_balance))
            prot = env.floor_guardian.evaluate_protection(
                equity=final_eq,
                available_balance=int(ex.available_balance_sats),
                peak_equity=max(int(getattr(env, "peak_equity_sats", initial_balance)), final_eq),
                locked_floor=int(getattr(env.brain, "locked_floor", 0)),
                daily_pnl_pct=-float(getattr(env, "max_seen_drawdown_pct", 0.0)),
            )
            floor_sats = int(
                prot.get("effective_floor")
                or prot.get("hard_floor")
                or prot.get("floor_sats")
                or prot.get("floor")
                or round(initial_balance * float(getattr(env.floor_guardian, "initial_floor_ratio", 0.90)))
            )
            max_dd_limit_pct = float(getattr(env.floor_guardian, "max_dd_pct", 0.052)) * 100.0
            floor_violated = bool(
                prot.get("hard_floor_breached", False)
                or prot.get("floor_breached", False)
                or (final_eq < floor_sats)
                or (blk_ddg > 0)
                or (dd_pct >= max_dd_limit_pct)
            )
            if floor_violated:
                hard_floor_violations += 1

            if is_m16:
                m16_info = {"net_sats": net, "trades": n, "max_dd_pct": round(dd_pct, 2)}

            block_rows.append(
                {
                    "block_key": block_key,
                    "label": label,
                    "is_oos_12m": is_oos_12m,
                    "trades": n,
                    "net_sats": net,
                    "max_dd_pct": round(dd_pct, 2),
                    "ddg": blk_ddg,
                    "hard_floor_violated": floor_violated,
                }
            )

    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    oos_12m_pf = (
        (oos_12m_gross_win / oos_12m_gross_loss) if oos_12m_gross_loss > 0 else float("inf")
    ) if scope == "18m" else None
    avg_entry_slip_bps = (entry_slip_bps_sum / entry_events) if entry_events > 0 else 0.0
    avg_sl_slip_bps = (sl_slip_bps_sum / sl_events) if sl_events > 0 else 0.0

    if scope == "18m":
        pf_oos_txt = "inf" if oos_12m_pf == float("inf") else f"{oos_12m_pf:.3f}"
        print(
            f"[CELL DONE] {scenario_name}@{fee_rt_bps:g}bps | N={total_trades} Net18M={total_net:+d} "
            f"OOS12M={oos_12m_net:+d}({oos_12m_trades}) PF_OOS12M={pf_oos_txt} "
            f"MaxDD={max_dd_pct:.2f}% DDG={ddg} HFViol={hard_floor_violations}",
            flush=True,
        )

    return {
        "scope": scope,
        "scenario": scenario_name,
        "fee_rt_bps": float(fee_rt_bps),
        "taker_fee_rate": taker_fee_rate,
        "blocks": len(blocks),
        "months": 6 if scope == "fold1" else 18,
        "n_trades": total_trades,
        "net_sats": total_net,
        "oos_12m_trades": oos_12m_trades if scope == "18m" else None,
        "oos_12m_sats": oos_12m_net if scope == "18m" else None,
        "pf": None if pf == float("inf") else round(pf, 3),
        "pf_raw": pf,
        "oos_12m_pf": None if (oos_12m_pf is None or oos_12m_pf == float("inf")) else round(oos_12m_pf, 3),
        "oos_12m_pf_raw": oos_12m_pf,
        "max_dd_pct": round(max_dd_pct, 2),
        "dd_ge_45": dd_45,
        "ddg": ddg,
        "hard_floor_violations": hard_floor_violations,
        "m16": m16_info,
        "avg_entry_slip_bps": round(avg_entry_slip_bps, 3),
        "avg_sl_slip_bps": round(avg_sl_slip_bps, 3),
        "entry_stress_events": entry_events,
        "sl_stress_events": sl_events,
        "monthly": block_rows,
    }


def _evaluate_verdict(scope: str, fee_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_sc = {r["scenario"]: r for r in fee_rows}
    reasons: List[str] = []
    passed = True

    s0 = by_sc.get("S0")
    s0_parity_ok: Optional[bool] = None
    if s0 is not None and abs(float(s0["fee_rt_bps"]) - 20.0) < 1e-9:
        m16 = s0.get("m16") or {}
        if scope == "fold1":
            s0_parity_ok = (
                s0["n_trades"] == 130
                and s0["net_sats"] == 26933
                and round(float(s0["pf_raw"]), 2) == 1.73
                and round(float(s0["max_dd_pct"]), 2) == 4.10
                and s0["dd_ge_45"] == 0
                and s0["ddg"] == 0
                and m16.get("net_sats") == 31746
                and m16.get("trades") == 52
            )
            if not s0_parity_ok:
                passed = False
                reasons.append("S0@20bps perdió paridad bit a bit con referencia Fold 1 OOS.")
        elif scope == "18m":
            s0_parity_ok = (
                s0["n_trades"] == 672
                and s0["net_sats"] == -8196
                and s0["oos_12m_trades"] == 377
                and s0["oos_12m_sats"] == 18092
                and round(float(s0["pf_raw"]), 3) == 0.964
                and round(float(s0["oos_12m_pf_raw"]), 3) == 1.145
                and round(float(s0["max_dd_pct"]), 2) == 4.66
                and s0["ddg"] == 0
                and m16.get("net_sats") == 31746
                and m16.get("trades") == 52
            )
            if not s0_parity_ok:
                passed = False
                reasons.append("S0@20bps perdió paridad bit a bit con referencia 18M/OOS12M (+18,092 / -8,196).")

    s1 = by_sc.get("S1")
    if s1 is not None:
        ref_net = int(s1["oos_12m_sats"]) if scope == "18m" else int(s1["net_sats"])
        net_label = "OOS 12M" if scope == "18m" else "Fold 1 Net"
        if ref_net <= 0:
            passed = False
            reasons.append(f"S1 {net_label} <= 0 ({ref_net:+d} sats)")
        if float(s1["max_dd_pct"]) >= 4.8:
            passed = False
            reasons.append(f"S1 MaxDD >= 4.8% ({s1['max_dd_pct']:.2f}%)")
        if int(s1["ddg"]) != 0:
            passed = False
            reasons.append(f"S1 DDG != 0 ({s1['ddg']})")
        ref_pf = float(s1["oos_12m_pf_raw"]) if scope == "18m" else float(s1["pf_raw"])
        pf_label = "PF_OOS12M" if scope == "18m" else "PF"
        if ref_pf < 1.10:
            passed = False
            reasons.append(f"S1 {pf_label} < 1.10 ({ref_pf:.3f})")
    else:
        reasons.append("S1 no evaluado en --scenarios")

    s2 = by_sc.get("S2")
    if s2 is not None:
        if int(s2["hard_floor_violations"]) != 0 or int(s2["ddg"]) != 0:
            passed = False
            reasons.append(
                f"S2 rompió HardFloorGuardian (HFViol={s2['hard_floor_violations']}, DDG={s2['ddg']})"
            )
    else:
        reasons.append("S2 no evaluado en --scenarios")

    return {
        "verdict": "GO" if passed else "NO-GO",
        "s0_parity_ok": s0_parity_ok,
        "s0_fold1_parity_ok": s0_parity_ok if scope == "fold1" else None,
        "reasons": reasons,
    }


def _format_table(rows: List[Dict[str, Any]]) -> str:
    headers = [
        "Scenario",
        "FeeRT",
        "N",
        "Net (sats)",
        "OOS 12M (sats)",
        "PF",
        "PF_OOS12M",
        "MaxDD%",
        "DD>=4.5%",
        "DDG",
        "M16",
        "avg_entry_slip_bps",
        "avg_sl_slip_bps",
        "HFViol",
    ]
    str_rows: List[List[str]] = []
    for r in rows:
        m16 = r.get("m16")
        m16_str = f"{m16['net_sats']:+d}({m16['trades']})" if isinstance(m16, dict) else "-"
        oos_str = f"{r['oos_12m_sats']:+d}" if r["oos_12m_sats"] is not None else "-"
        pf_val = r["pf_raw"]
        pf_str = "inf" if pf_val == float("inf") else f"{pf_val:.3f}"
        oos_pf_val = r.get("oos_12m_pf_raw")
        oos_pf_str = "-" if oos_pf_val is None else ("inf" if oos_pf_val == float("inf") else f"{oos_pf_val:.3f}")
        str_rows.append(
            [
                r["scenario"],
                f"{r['fee_rt_bps']:g}bps",
                str(r["n_trades"]),
                f"{r['net_sats']:+d}",
                oos_str,
                pf_str,
                oos_pf_str,
                f"{r['max_dd_pct']:.2f}%",
                f"{r['dd_ge_45']}/{r['blocks']}",
                str(r["ddg"]),
                m16_str,
                f"{r['avg_entry_slip_bps']:.3f}",
                f"{r['avg_sl_slip_bps']:.3f}",
                str(r["hard_floor_violations"]),
            ]
        )

    widths = [max(len(headers[i]), *(len(row[i]) for row in str_rows)) for i in range(len(headers))]
    sep = " | "
    lines = [
        sep.join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        "-+-".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in str_rows:
        lines.append(
            sep.join(
                row[i].ljust(widths[i]) if i in (0, 10) else row[i].rjust(widths[i])
                for i in range(len(headers))
            )
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ejecutor de matriz de estrés de microestructura Fase 5 (S0, S1, S2)."
    )
    parser.add_argument("--scope", choices=("fold1", "18m"), default="fold1", help="Alcance de replay: fold1 o 18m")
    parser.add_argument("--scenarios", default="S0,S1,S2", help="Lista separada por comas de escenarios (S0,S1,S2)")
    parser.add_argument("--fee-rt-bps", default="20,16", help="Lista separada por comas de comisiones round-trip en bps")
    parser.add_argument("--json-out", default=None, help="Ruta opcional para guardar reporte JSON atómicamente")
    parser.add_argument("--dry-check", action="store_true", help="Valida imports, presets y cachés sin correr episodios")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="Directorio de caché WSL")
    parser.add_argument("--dataset-cache", default=DEFAULT_DATASET_CACHE, help="Ruta opcional de binance_btc_1m.json")
    parser.add_argument("--initial-balance", type=int, default=DEFAULT_INITIAL_BALANCE, help="Balance inicial en sats")
    args = parser.parse_args(argv)

    scenarios = _parse_scenarios(args.scenarios)
    fee_rt_list = _parse_fee_rt_bps(args.fee_rt_bps)

    if args.dry_check:
        return _run_dry_check(args.scope, scenarios, fee_rt_list, args.cache_dir, args.dataset_cache)

    seed_brain = os.path.join(args.cache_dir, "q_brain_s2_clean_seed.json")
    if not os.path.exists(seed_brain):
        raise FileNotFoundError(f"Falta semilla OOS: {seed_brain}")

    blocks = _load_scope_blocks(args.scope, args.cache_dir)
    all_rows: List[Dict[str, Any]] = []
    verdicts_by_fee: Dict[str, Dict[str, Any]] = {}

    for fee_rt in fee_rt_list:
        fee_rows: List[Dict[str, Any]] = []
        for sc_name, stress_cfg in scenarios:
            row = _evaluate_cell(
                scope=args.scope,
                scenario_name=sc_name,
                stress_cfg=stress_cfg,
                fee_rt_bps=fee_rt,
                blocks=blocks,
                seed_brain=seed_brain,
                initial_balance=args.initial_balance,
            )
            fee_rows.append(row)
            all_rows.append(row)
        verdicts_by_fee[f"{fee_rt:g}bps"] = _evaluate_verdict(args.scope, fee_rows)

    print(f"\n=== FASE 5 STRESS MATRIX (scope={args.scope}, blocks={len(blocks)}) ===")
    print(_format_table(all_rows))
    print("\n=== VEREDICTO GO / NO-GO (ROADMAP_LNM_SNIPER.md) ===")
    overall_go = True
    for fee_key, v in verdicts_by_fee.items():
        if v["verdict"] != "GO":
            overall_go = False
        parity_note = ""
        if v["s0_parity_ok"] is not None:
            parity_note = f" | S0_{args.scope}_Parity={'PASS' if v['s0_parity_ok'] else 'FAIL'}"
        reasons_str = "; ".join(v["reasons"]) if v["reasons"] else "S1 y S2 cumplen criterios P0"
        print(f"  [{fee_key}] {v['verdict']}{parity_note} -> {reasons_str}")

    if args.json_out:
        clean_rows = []
        for r in all_rows:
            copy_r = dict(r)
            copy_r.pop("pf_raw", None)
            copy_r.pop("oos_12m_pf_raw", None)
            clean_rows.append(copy_r)
        report = {
            "scope": args.scope,
            "initial_balance_sats": args.initial_balance,
            "scenarios": [s[0] for s in scenarios],
            "fee_rt_bps": fee_rt_list,
            "overall_verdict": "GO" if overall_go else "NO-GO",
            "verdicts_by_fee": verdicts_by_fee,
            "results": clean_rows,
        }
        _atomic_write_json(args.json_out, report)
        print(f"\n[OK] Reporte JSON guardado en: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
