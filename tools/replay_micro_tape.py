#!/usr/bin/env python3
"""
Herramienta CLI offline (100% Python stdlib, lectura streaming línea a línea)
para auditar eventos OPEN/CLOSE (`--events`) o el tape 1m (`--tape`) contra
las 4 reglas simétricas de `micro_flow_guard.py`.
"""

import argparse
from collections import Counter
import glob
import json
import math
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from micro_flow_guard import (  # noqa: E402
    MICRO_VETO_ADV_SQUEEZE,
    MICRO_VETO_COUNTER_FLOW,
    MICRO_VETO_POST_CLIMAX_LIQ,
    MICRO_VETO_PURE_CLIMAX,
    MicroFlowGuardConfig,
    evaluate_micro_entry_guard,
)

ALL_RULE_CODES = (
    MICRO_VETO_ADV_SQUEEZE,
    MICRO_VETO_COUNTER_FLOW,
    MICRO_VETO_POST_CLIMAX_LIQ,
    MICRO_VETO_PURE_CLIMAX,
)


def _safe_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _expand_paths(pattern: str) -> List[str]:
    matches = sorted(glob.glob(pattern))
    if not matches and os.path.exists(pattern):
        matches = [pattern]
    return [os.path.abspath(p) for p in matches if os.path.isfile(p)]


def _percentile(sorted_vals: List[float], pct: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    idx = (float(pct) / 100.0) * (n - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = idx - lo
    return float(sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo]))


def _percentiles_summary(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "p50": 0.0, "p75": 0.0, "p90": 0.0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": round(_percentile(ordered, 50.0), 4),
        "p75": round(_percentile(ordered, 75.0), 4),
        "p90": round(_percentile(ordered, 90.0), 4),
    }


def _collect_micro_distribution_point(
    snap: Optional[Mapping[str, Any]],
    liq_tot_vals: List[float],
    cvd_5m_signed_vals: List[float],
    cvd_5m_abs_vals: List[float],
) -> None:
    if not isinstance(snap, Mapping):
        return
    liq_b = _safe_float(snap.get("liq_buy_1m_usd"))
    liq_s = _safe_float(snap.get("liq_sell_1m_usd"))
    if liq_b is not None and liq_s is not None and liq_b >= 0.0 and liq_s >= 0.0:
        liq_tot_vals.append(liq_b + liq_s)
    c5 = _safe_float(snap.get("cvd_5m_usd"))
    if c5 is not None:
        cvd_5m_signed_vals.append(c5)
        cvd_5m_abs_vals.append(abs(c5))


def replay_events_files(
    paths: List[str],
    cfg: MicroFlowGuardConfig,
) -> Dict[str, Any]:
    """Empareja eventos OPEN/CLOSE en streaming y evalúa el impacto del guardia."""
    opens: Dict[str, Dict[str, Any]] = {}
    pending_closes: Dict[str, Dict[str, Any]] = {}
    paired: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    malformed_lines = 0
    missing_trade_id = 0
    total_lines = 0

    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                total_lines += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    malformed_lines += 1
                    continue
                if not isinstance(obj, dict):
                    malformed_lines += 1
                    continue
                ev = str(obj.get("event", "")).strip().upper()
                tid_raw = obj.get("trade_id")
                if tid_raw is None:
                    tid_raw = obj.get("id")
                if tid_raw is None or not str(tid_raw).strip():
                    missing_trade_id += 1
                    continue
                tid = str(tid_raw).strip()

                if ev == "OPEN":
                    if tid in pending_closes:
                        paired.append((obj, pending_closes.pop(tid)))
                    else:
                        opens[tid] = obj
                elif ev == "CLOSE":
                    if tid in opens:
                        paired.append((opens.pop(tid), obj))
                    else:
                        pending_closes[tid] = obj

    approved_count = 0
    vetoed_count = 0
    fail_open_count = 0
    rule_counts: Counter = Counter()
    fail_open_counts: Counter = Counter()

    net_pnl_base_sats = 0.0
    net_pnl_approved_sats = 0.0
    vetoed_net_pnl_sats = 0.0
    total_fees_base_sats = 0.0
    total_fees_approved_sats = 0.0

    liq_tot_vals: List[float] = []
    cvd_5m_signed_vals: List[float] = []
    cvd_5m_abs_vals: List[float] = []

    for open_rec, close_rec in paired:
        side = open_rec.get("side")
        if side is None:
            side = open_rec.get("action_id")
        if side is None:
            side = close_rec.get("side")
        if side is None:
            side = close_rec.get("action_id")

        open_snap = open_rec.get("binance_micro")
        if not isinstance(open_snap, Mapping):
            open_snap = open_rec.get("snapshot")
        if not isinstance(open_snap, Mapping):
            open_snap = None

        _collect_micro_distribution_point(
            open_snap, liq_tot_vals, cvd_5m_signed_vals, cvd_5m_abs_vals
        )

        now_ts = _safe_float(open_rec.get("ts"))
        decision = evaluate_micro_entry_guard(side, open_snap, cfg, now_ts=now_ts)

        net_pnl = _safe_float(close_rec.get("net_pnl_sats"))
        if net_pnl is None:
            net_pnl = _safe_float(close_rec.get("net_realized_sats"))
        if net_pnl is None:
            net_pnl = 0.0

        fee_sats = _safe_float(close_rec.get("fee_sats"))
        if fee_sats is None:
            fee_sats = (
                abs(_safe_float(close_rec.get("opening_fee")) or 0.0)
                + abs(_safe_float(close_rec.get("closing_fee")) or 0.0)
                + (_safe_float(close_rec.get("funding")) or 0.0)
            )
        else:
            fee_sats = abs(fee_sats)

        net_pnl_base_sats += net_pnl
        total_fees_base_sats += fee_sats

        if decision.vetoed:
            vetoed_count += 1
            rule_counts[decision.rule_code or decision.reason] += 1
            vetoed_net_pnl_sats += net_pnl
        else:
            approved_count += 1
            net_pnl_approved_sats += net_pnl
            total_fees_approved_sats += fee_sats
            if decision.fail_open:
                fail_open_count += 1
                fail_open_counts[decision.reason] += 1

    sats_avoided = -vetoed_net_pnl_sats
    fer_base = (
        (net_pnl_base_sats / total_fees_base_sats)
        if total_fees_base_sats > 0.0
        else 0.0
    )
    fer_approved = (
        (net_pnl_approved_sats / total_fees_approved_sats)
        if total_fees_approved_sats > 0.0
        else 0.0
    )
    delta_fer = fer_approved - fer_base

    return {
        "mode": "events",
        "files": paths,
        "total_lines": total_lines,
        "malformed_lines": malformed_lines,
        "missing_trade_id": missing_trade_id,
        "paired_trades": len(paired),
        "unpaired_opens": len(opens),
        "unpaired_closes": len(pending_closes),
        "approved_count": approved_count,
        "vetoed_count": vetoed_count,
        "fail_open_count": fail_open_count,
        "veto_by_rule": {code: int(rule_counts.get(code, 0)) for code in ALL_RULE_CODES},
        "fail_open_by_reason": dict(sorted(fail_open_counts.items())),
        "net_pnl_base_sats": round(net_pnl_base_sats, 4),
        "net_pnl_approved_sats": round(net_pnl_approved_sats, 4),
        "sats_avoided": round(sats_avoided, 4),
        "total_fees_base_sats": round(total_fees_base_sats, 4),
        "total_fees_approved_sats": round(total_fees_approved_sats, 4),
        "fer_base": round(fer_base, 6),
        "fer_approved": round(fer_approved, 6),
        "delta_fer": round(delta_fer, 6),
        "distributions": {
            "liq_tot_1m_usd": _percentiles_summary(liq_tot_vals),
            "cvd_5m_usd_signed": _percentiles_summary(cvd_5m_signed_vals),
            "cvd_5m_usd_abs": _percentiles_summary(cvd_5m_abs_vals),
        },
    }


def replay_tape_files(
    paths: List[str],
    cfg: MicroFlowGuardConfig,
) -> Dict[str, Any]:
    """Lee en streaming un tape 1m JSONL y resume calidad, percentiles y disparos LONG/SHORT."""
    total_minutes = 0
    malformed_lines = 0
    gaps_gt_90s = 0
    connected_count = 0
    stale_count = 0
    fail_open_count = 0
    low_volume_count = 0
    fail_open_counts: Counter = Counter()

    long_veto_counts: Counter = Counter()
    short_veto_counts: Counter = Counter()
    long_vetoed_total = 0
    short_vetoed_total = 0

    liq_tot_vals: List[float] = []
    cvd_5m_signed_vals: List[float] = []
    cvd_5m_abs_vals: List[float] = []

    for path in paths:
        prev_ts: Optional[float] = None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    malformed_lines += 1
                    continue
                if not isinstance(row, dict):
                    malformed_lines += 1
                    continue

                total_minutes += 1
                ts_val = _safe_float(row.get("ts"))
                if ts_val is not None:
                    if prev_ts is not None and (ts_val - prev_ts) > 90.0:
                        gaps_gt_90s += 1
                    prev_ts = ts_val

                if row.get("connected") is True:
                    connected_count += 1
                if bool(row.get("stale", False)):
                    stale_count += 1

                _collect_micro_distribution_point(
                    row, liq_tot_vals, cvd_5m_signed_vals, cvd_5m_abs_vals
                )

                dec_long = evaluate_micro_entry_guard("buy", row, cfg)
                dec_short = evaluate_micro_entry_guard("sell", row, cfg)

                if dec_long.fail_open:
                    fail_open_count += 1
                    fail_open_counts[dec_long.reason] += 1
                    if dec_long.reason == "FAIL_OPEN_LOW_VOLUME":
                        low_volume_count += 1

                if dec_long.vetoed:
                    long_vetoed_total += 1
                    long_veto_counts[dec_long.rule_code or dec_long.reason] += 1
                if dec_short.vetoed:
                    short_vetoed_total += 1
                    short_veto_counts[dec_short.rule_code or dec_short.reason] += 1

    denom = float(max(1, total_minutes))
    return {
        "mode": "tape",
        "files": paths,
        "total_minutes": total_minutes,
        "malformed_lines": malformed_lines,
        "gaps_gt_90s": gaps_gt_90s,
        "pct_connected": round(100.0 * connected_count / denom, 4) if total_minutes else 0.0,
        "pct_stale": round(100.0 * stale_count / denom, 4) if total_minutes else 0.0,
        "pct_fail_open": round(100.0 * fail_open_count / denom, 4) if total_minutes else 0.0,
        "pct_low_volume": round(100.0 * low_volume_count / denom, 4) if total_minutes else 0.0,
        "fail_open_count": fail_open_count,
        "low_volume_count": low_volume_count,
        "fail_open_by_reason": dict(sorted(fail_open_counts.items())),
        "long_vetoed_total": long_vetoed_total,
        "short_vetoed_total": short_vetoed_total,
        "long_veto_by_rule": {
            code: int(long_veto_counts.get(code, 0)) for code in ALL_RULE_CODES
        },
        "short_veto_by_rule": {
            code: int(short_veto_counts.get(code, 0)) for code in ALL_RULE_CODES
        },
        "distributions": {
            "liq_tot_1m_usd": _percentiles_summary(liq_tot_vals),
            "cvd_5m_usd_signed": _percentiles_summary(cvd_5m_signed_vals),
            "cvd_5m_usd_abs": _percentiles_summary(cvd_5m_abs_vals),
        },
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    defaults = MicroFlowGuardConfig(enabled=True, shadow_only=False)
    parser = argparse.ArgumentParser(
        prog="tools/replay_micro_tape.py",
        description=(
            "Replay offline streaming (100% stdlib) de eventos OPEN/CLOSE o tape 1m "
            "para auditar `micro_flow_guard.py`."
        ),
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--events",
        default=None,
        help="Ruta o glob de archivo(s) JSONL de eventos OPEN/CLOSE (binance_micro_events*.jsonl).",
    )
    mode_group.add_argument(
        "--tape",
        default=None,
        help="Ruta o glob de archivo(s) JSONL de tape 1m (binance_micro_tape.jsonl*).",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Ruta opcional para guardar el reporte completo en JSON.",
    )
    parser.add_argument("--max-age-sec", type=float, default=defaults.max_age_sec)
    parser.add_argument("--min-trades-1m", type=int, default=defaults.min_trades_1m)
    parser.add_argument("--min-vol-1m-usd", type=float, default=defaults.min_vol_1m_usd)
    parser.add_argument("--min-liq-1m-usd", type=float, default=defaults.min_liq_1m_usd)
    parser.add_argument(
        "--adv-squeeze-cvd-1m-max",
        type=float,
        default=defaults.adv_squeeze_cvd_1m_max,
    )
    parser.add_argument(
        "--adv-squeeze-liq-dir-max",
        type=float,
        default=defaults.adv_squeeze_liq_dir_max,
    )
    parser.add_argument(
        "--counter-flow-cvd-1m-max",
        type=float,
        default=defaults.counter_flow_cvd_1m_max,
    )
    parser.add_argument(
        "--counter-flow-cvd-5m-usd-max",
        type=float,
        default=defaults.counter_flow_cvd_5m_usd_max,
    )
    parser.add_argument(
        "--post-climax-liq-dir-min",
        type=float,
        default=defaults.post_climax_liq_dir_min,
    )
    parser.add_argument(
        "--post-climax-cvd-1m-min",
        type=float,
        default=defaults.post_climax_cvd_1m_min,
    )
    parser.add_argument(
        "--post-climax-cvd-5m-usd-min",
        type=float,
        default=defaults.post_climax_cvd_5m_usd_min,
    )
    parser.add_argument(
        "--pure-climax-cvd-1m-min",
        type=float,
        default=defaults.pure_climax_cvd_1m_min,
    )
    parser.add_argument(
        "--pure-climax-cvd-5m-usd-min",
        type=float,
        default=defaults.pure_climax_cvd_5m_usd_min,
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> MicroFlowGuardConfig:
    return MicroFlowGuardConfig(
        enabled=True,
        shadow_only=False,
        max_age_sec=args.max_age_sec,
        min_trades_1m=args.min_trades_1m,
        min_vol_1m_usd=args.min_vol_1m_usd,
        min_liq_1m_usd=args.min_liq_1m_usd,
        adv_squeeze_cvd_1m_max=args.adv_squeeze_cvd_1m_max,
        adv_squeeze_liq_dir_max=args.adv_squeeze_liq_dir_max,
        counter_flow_cvd_1m_max=args.counter_flow_cvd_1m_max,
        counter_flow_cvd_5m_usd_max=args.counter_flow_cvd_5m_usd_max,
        post_climax_liq_dir_min=args.post_climax_liq_dir_min,
        post_climax_cvd_1m_min=args.post_climax_cvd_1m_min,
        post_climax_cvd_5m_usd_min=args.post_climax_cvd_5m_usd_min,
        pure_climax_cvd_1m_min=args.pure_climax_cvd_1m_min,
        pure_climax_cvd_5m_usd_min=args.pure_climax_cvd_5m_usd_min,
    )


def _format_report_summary(report: Dict[str, Any]) -> str:
    dist = report["distributions"]
    liq_d = dist["liq_tot_1m_usd"]
    c5s = dist["cvd_5m_usd_signed"]
    c5a = dist["cvd_5m_usd_abs"]
    lines: List[str] = []
    if report["mode"] == "events":
        lines.extend(
            [
                f"[REPLAY EVENTS] files={len(report['files'])} paired={report['paired_trades']} "
                f"approved={report['approved_count']} vetoed={report['vetoed_count']} "
                f"fail_open={report['fail_open_count']} malformed={report['malformed_lines']}",
                f"  net_pnl_base_sats={report['net_pnl_base_sats']:.2f} | "
                f"net_pnl_approved_sats={report['net_pnl_approved_sats']:.2f} | "
                f"sats_avoided={report['sats_avoided']:.2f}",
                f"  fer_base={report['fer_base']:.4f} | fer_approved={report['fer_approved']:.4f} | "
                f"delta_fer={report['delta_fer']:+.4f}",
                f"  veto_by_rule={json.dumps(report['veto_by_rule'], sort_keys=True)}",
                f"  fail_open_by_reason={json.dumps(report['fail_open_by_reason'], sort_keys=True)}",
            ]
        )
    else:
        lines.extend(
            [
                f"[REPLAY TAPE] files={len(report['files'])} minutes={report['total_minutes']} "
                f"gaps_gt_90s={report['gaps_gt_90s']} malformed={report['malformed_lines']}",
                f"  connected={report['pct_connected']:.2f}% | stale={report['pct_stale']:.2f}% | "
                f"fail_open={report['pct_fail_open']:.2f}% | low_volume={report['pct_low_volume']:.2f}%",
                f"  LONG vetoed={report['long_vetoed_total']} {json.dumps(report['long_veto_by_rule'], sort_keys=True)}",
                f"  SHORT vetoed={report['short_vetoed_total']} {json.dumps(report['short_veto_by_rule'], sort_keys=True)}",
            ]
        )
    lines.append(
        f"  liq_tot_1m_usd P50/P75/P90 = {liq_d['p50']:.2f} / {liq_d['p75']:.2f} / {liq_d['p90']:.2f}"
    )
    lines.append(
        f"  cvd_5m_usd_signed P50/P75/P90 = {c5s['p50']:.2f} / {c5s['p75']:.2f} / {c5s['p90']:.2f}"
    )
    lines.append(
        f"  cvd_5m_usd_abs P50/P75/P90 = {c5a['p50']:.2f} / {c5a['p75']:.2f} / {c5a['p90']:.2f}"
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    cfg = _config_from_args(args)

    pattern = args.events if args.events is not None else args.tape
    paths = _expand_paths(pattern)
    if not paths:
        sys.stderr.write(f"Error: no se encontraron archivos para '{pattern}'\n")
        return 2

    if args.events is not None:
        report = replay_events_files(paths, cfg)
    else:
        report = replay_tape_files(paths, cfg)

    if args.json_out:
        out_abs = os.path.abspath(args.json_out)
        parent = os.path.dirname(out_abs)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_abs, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    print(_format_report_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
