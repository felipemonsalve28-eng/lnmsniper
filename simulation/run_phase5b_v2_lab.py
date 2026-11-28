# -*- coding: utf-8 -*-
"""
simulation/run_phase5b_v2_lab.py
Laboratorio streaming Fase 5B v2 & Aislamiento de Carriles (geometry | regimes | lane_b | exit_asym).
Carga estrictamente 1 archivo .pkl a la vez seguido de del blk; gc.collect().
Solo stdlib + módulos del repositorio; jamás muta cachés WSL ni cerebros del repo.
"""
import argparse
from collections import Counter
import contextlib
from dataclasses import dataclass, field
import gc
import io
import json
import logging
import os
import pickle
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.dont_write_bytecode = True

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from simulation.lane_budget import (
    LaneBudgetConfig,
    LaneBudgetController,
    compute_inverse_loss_at_sl_sats,
    compute_inverse_net_pnl_sats,
)
from simulation.sim_lnm_exchange import MicrostructureStressConfig
from simulation.sim_training_environment import SimTrainingEnvironment

DEFAULT_CACHE_DIR = "/home/akapana/.cache/sniper_wf_s2_cache"
DEFAULT_INITIAL_BALANCE = 100000


@dataclass(frozen=True)
class BlockDesc:
    index: int
    block_key: str
    label: str
    pkl_path: str
    is_oos_12m: bool
    is_m16: bool
    oos_month_bucket: Optional[str]


@dataclass
class CellAccumulator:
    cell_id: str
    mode: str
    scope: str
    scenario: str
    fee_rt_bps: float
    taker_fee_rate: float
    initial_balance: int
    params: Dict[str, Any] = field(default_factory=dict)

    n_blocks: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_net: int = 0
    gross_win: int = 0
    gross_loss: int = 0

    oos_12m_trades: int = 0
    oos_12m_wins: int = 0
    oos_12m_net: int = 0
    oos_12m_gross_win: int = 0
    oos_12m_gross_loss: int = 0
    m16_net: Optional[int] = None
    m16_trades: Optional[int] = None

    max_dd_pct: float = 0.0
    dd_ge_45: int = 0
    ddg: int = 0
    hard_floor_violations: int = 0

    oos_bucket_net: Dict[str, int] = field(default_factory=dict)
    monthly_rows: List[Dict[str, Any]] = field(default_factory=list)
    extra_counters: Counter = field(default_factory=Counter)
    extra_meta: Dict[str, Any] = field(default_factory=dict)

    def record_block(
        self,
        *,
        desc: BlockDesc,
        n_trades: int,
        wins: int,
        net_sats: int,
        b_win: int,
        b_loss: int,
        dd_pct: float,
        blk_ddg: int,
        floor_violated: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.n_blocks += 1
        self.total_trades += n_trades
        self.total_wins += wins
        self.total_net += net_sats
        self.gross_win += b_win
        self.gross_loss += b_loss

        if desc.is_oos_12m:
            self.oos_12m_trades += n_trades
            self.oos_12m_wins += wins
            self.oos_12m_net += net_sats
            self.oos_12m_gross_win += b_win
            self.oos_12m_gross_loss += b_loss
            if desc.oos_month_bucket is not None:
                self.oos_bucket_net[desc.oos_month_bucket] = (
                    self.oos_bucket_net.get(desc.oos_month_bucket, 0) + net_sats
                )

        if desc.is_m16:
            self.m16_net = net_sats
            self.m16_trades = n_trades

        self.max_dd_pct = max(self.max_dd_pct, dd_pct)
        if dd_pct >= 4.5:
            self.dd_ge_45 += 1
        self.ddg += blk_ddg
        if floor_violated:
            self.hard_floor_violations += 1

        row = {
            "block_key": desc.block_key,
            "label": desc.label,
            "is_oos_12m": desc.is_oos_12m,
            "trades": n_trades,
            "wins": wins,
            "net_sats": net_sats,
            "max_dd_pct": round(dd_pct, 2),
            "ddg": blk_ddg,
            "hard_floor_violated": floor_violated,
        }
        if extra:
            row.update(extra)
        self.monthly_rows.append(row)

    def finalize(self) -> Dict[str, Any]:
        months = 6 if self.scope == "fold1" else 18
        n_per_month = round(self.total_trades / months, 2) if months > 0 else 0.0
        wr_pct = round(100.0 * self.total_wins / self.total_trades, 2) if self.total_trades > 0 else 0.0
        pf_raw = (self.gross_win / self.gross_loss) if self.gross_loss > 0 else float("inf")
        pf_oos_raw = (
            (self.oos_12m_gross_win / self.oos_12m_gross_loss)
            if self.oos_12m_gross_loss > 0
            else float("inf")
        )
        oos_sin_m16 = (
            self.oos_12m_net - self.m16_net
            if self.m16_net is not None
            else self.oos_12m_net
        )
        total_oos_months = len(self.oos_bucket_net)
        pos_months_oos = sum(1 for v in self.oos_bucket_net.values() if v > 0)

        out: Dict[str, Any] = {
            "cell_id": self.cell_id,
            "mode": self.mode,
            "scope": self.scope,
            "scenario": self.scenario,
            "fee_rt_bps": self.fee_rt_bps,
            "taker_fee_rate": self.taker_fee_rate,
            "params": dict(self.params),
            "blocks": self.n_blocks,
            "months": months,
            "N": self.total_trades,
            "N_mes": n_per_month,
            "WR_pct": wr_pct,
            "Net_18M": self.total_net,
            "OOS_12M": self.oos_12m_net,
            "OOS_sin_M16": oos_sin_m16,
            "PF": None if pf_raw == float("inf") else round(pf_raw, 3),
            "PF_raw": pf_raw,
            "PF_OOS12M": None if pf_oos_raw == float("inf") else round(pf_oos_raw, 3),
            "PF_OOS12M_raw": pf_oos_raw,
            "MaxDD_pct": round(self.max_dd_pct, 2),
            "DD_ge_45": self.dd_ge_45,
            "DDG": self.ddg,
            "HFViol": self.hard_floor_violations,
            "pos_months_oos": pos_months_oos,
            "total_oos_months": total_oos_months,
            "PlusMeses_OOS": f"{pos_months_oos}/{total_oos_months}" if total_oos_months > 0 else "0/0",
            "m16": (
                {"net_sats": self.m16_net, "trades": self.m16_trades}
                if self.m16_net is not None
                else None
            ),
            "monthly": self.monthly_rows,
        }
        if self.extra_counters:
            out["counters"] = dict(self.extra_counters)
        if self.extra_meta:
            out["meta"] = dict(self.extra_meta)
        return out


def _parse_csv_strs(raw: str, flag_name: str, upper: bool = False) -> List[str]:
    vals: List[str] = []
    seen = set()
    for part in str(raw).split(","):
        item = part.strip()
        if upper:
            item = item.upper()
        if not item or item in seen:
            continue
        seen.add(item)
        vals.append(item)
    if not vals:
        raise ValueError(f"Debe especificar al menos un valor en {flag_name}.")
    return vals


def _parse_csv_floats(raw: str, flag_name: str, min_val: float = 0.0) -> List[float]:
    vals: List[float] = []
    seen = set()
    for part in str(raw).split(","):
        s = part.strip()
        if not s:
            continue
        val = float(s)
        if val <= min_val:
            raise ValueError(f"Valor inválido en {flag_name}: {val} (debe ser > {min_val}).")
        if val not in seen:
            seen.add(val)
            vals.append(val)
    if not vals:
        raise ValueError(f"Debe especificar al menos un valor en {flag_name}.")
    return vals


def _atomic_write_json(path: str, payload: dict) -> None:
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".phase5b_v2_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(_json_sanitize(payload), fh, indent=2, ensure_ascii=False, allow_nan=False)
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


def _json_sanitize(obj: Any) -> Any:
    if isinstance(obj, float):
        if obj == float("inf") or obj == float("-inf") or obj != obj:
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return obj


def _load_months_meta(cache_dir: str) -> Dict[int, dict]:
    meta_path = os.path.join(cache_dir, "months_meta.json")
    if not os.path.exists(meta_path):
        return {}
    with open(meta_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("months_meta", []) if isinstance(data, dict) else []
    return {int(item["idx"]): item for item in entries if isinstance(item, dict) and "idx" in item}


def _discover_block_descs(scope: str, cache_dir: str) -> List[BlockDesc]:
    meta_by_idx = _load_months_meta(cache_dir)
    descs: List[BlockDesc] = []
    idx_counter = 0

    if scope == "18m":
        for w in range(52):
            pkl_path = os.path.join(cache_dir, f"train_w{w:02d}.pkl")
            is_oos_f2 = w < 26
            tag = "F2" if is_oos_f2 else "IS"
            bucket = f"f2_m{min(5, (w * 6) // 26):02d}" if is_oos_f2 else None
            descs.append(
                BlockDesc(
                    index=idx_counter,
                    block_key=f"w{w:02d}",
                    label=f"W{w:02d}({tag})",
                    pkl_path=pkl_path,
                    is_oos_12m=is_oos_f2,
                    is_m16=False,
                    oos_month_bucket=bucket,
                )
            )
            idx_counter += 1

    for idx in range(12, 18):
        meta = meta_by_idx.get(idx, {})
        label = str(meta.get("label") or f"M{idx + 1:02d}")
        pkl_path = os.path.join(cache_dir, f"oos_m{idx}.pkl")
        descs.append(
            BlockDesc(
                index=idx_counter,
                block_key=f"m{idx:02d}",
                label=label,
                pkl_path=pkl_path,
                is_oos_12m=True,
                is_m16=(idx == 15),
                oos_month_bucket=f"f1_m{idx:02d}",
            )
        )
        idx_counter += 1

    return descs


_NOISE_SUBSTRINGS = ("[CEREBRO CARGADO", "PARALYSIS_HOLD_FIXATION")


class _EpisodeNoiseLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        return not any(tok in msg for tok in _NOISE_SUBSTRINGS)


class _FilteredStdout(io.TextIOBase):
    def __init__(self, wrapped: Any):
        self._wrapped = wrapped
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if not any(tok in line for tok in _NOISE_SUBSTRINGS):
                self._wrapped.write(line + "\n")
        return len(s)

    def flush(self) -> None:
        if self._buf:
            if not any(tok in self._buf for tok in _NOISE_SUBSTRINGS):
                self._wrapped.write(self._buf)
            self._buf = ""
        if hasattr(self._wrapped, "flush"):
            self._wrapped.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._wrapped, "isatty", lambda: False)())

    @property
    def encoding(self) -> str:
        return getattr(self._wrapped, "encoding", "utf-8")


@contextlib.contextmanager
def _suppress_episode_noise(quiet: bool) -> Iterable[None]:
    """Suprime warnings/prints repetitivos por episodio cuando quiet=True."""
    if not quiet:
        yield
        return

    noise_filter = _EpisodeNoiseLogFilter()
    loggers: List[logging.Logger] = [
        logging.getLogger(),
        logging.getLogger("LearningQualitySupervisor"),
    ]
    lqs_mod = sys.modules.get("learning_quality_supervisor")
    if lqs_mod is not None and isinstance(getattr(lqs_mod, "logger", None), logging.Logger):
        if lqs_mod.logger not in loggers:
            loggers.append(lqs_mod.logger)

    for lg in loggers:
        lg.addFilter(noise_filter)

    old_stdout = sys.stdout
    filtered_out = _FilteredStdout(old_stdout)
    sys.stdout = filtered_out  # type: ignore[assignment]
    try:
        yield
    finally:
        try:
            filtered_out.flush()
        finally:
            sys.stdout = old_stdout
            for lg in loggers:
                lg.removeFilter(noise_filter)


def _iter_streamed_blocks(descs: Sequence[BlockDesc], quiet: bool = True) -> Iterable[Tuple[BlockDesc, Any]]:
    """Generador estrictamente streaming: mantiene 1 solo .pkl en RAM a la vez."""
    descs_list = list(descs)
    total = len(descs_list)
    t0 = time.perf_counter()
    for idx, desc in enumerate(descs_list, start=1):
        if not os.path.exists(desc.pkl_path):
            raise FileNotFoundError(f"Falta bloque requerido en caché: {desc.pkl_path}")
        with open(desc.pkl_path, "rb") as fh:
            blk = pickle.load(fh)
        try:
            yield desc, blk
        finally:
            del blk
            gc.collect()
        if quiet and total > 0 and (idx % 10 == 0 or idx == total):
            print(f"[{idx}/{total}] {desc.block_key} completado ({time.perf_counter() - t0:.1f}s)", flush=True)


def _prepare_temp_brain(td: str, tag: str, seed_brain: str) -> Tuple[str, str]:
    b_dst = os.path.join(td, f"brain_{tag}.json")
    h_dst = os.path.join(td, f"hedge_{tag}.json")
    shutil.copy2(seed_brain, b_dst)
    if os.path.exists(seed_brain + ".sha256"):
        shutil.copy2(seed_brain + ".sha256", b_dst + ".sha256")
    return b_dst, h_dst


def _check_floor_violation(
    env: SimTrainingEnvironment,
    final_eq: int,
    initial_balance: int,
    dd_pct: float,
    blk_ddg: int,
) -> bool:
    ex = env.exchange
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
    return bool(
        prot.get("hard_floor_breached", False)
        or prot.get("floor_breached", False)
        or (final_eq < floor_sats)
        or (blk_ddg > 0)
        or (dd_pct >= max_dd_limit_pct)
    )


def _apply_geometry_knobs(
    env: SimTrainingEnvironment,
    sl_floor: float,
    cooldown_sec: float,
    short_exp_anchor: str,
) -> None:
    """Ajusta en memoria los parámetros de geometría/cooldown sin tocar disco."""
    env.trend_sl_floor_pct = float(sl_floor)
    env.short_exp_phys_hold = True
    env.short_exp_anchor_raw_atr = (short_exp_anchor == "raw_atr")

    if hasattr(env, "side_brake") and env.side_brake is not None:
        for attr in ("pause_duration_sec", "cooldown_sec", "brake_duration_sec"):
            if hasattr(env.side_brake, attr):
                setattr(env.side_brake, attr, float(cooldown_sec))


def _run_env_block(
    *,
    blk: Any,
    seed_brain: str,
    td: str,
    tag: str,
    initial_balance: int,
    taker_fee_rate: float,
    stress_cfg: MicrostructureStressConfig,
    sl_floor: float = 0.0065,
    cooldown_sec: float = 86400.0,
    short_exp_anchor: str = "sl_dist",
    fe_off_trend: bool = True,
    regime_unlock_policy: Optional[str] = None,
    regime_stats: Optional[Counter] = None,
    forced_edge_loss_only: bool = False,
    forced_edge_min_loss_atr: float = 0.35,
    quiet: bool = True,
) -> Tuple[Dict[str, Any], SimTrainingEnvironment]:
    b_dst, h_dst = _prepare_temp_brain(td, tag, seed_brain)
    with _suppress_episode_noise(quiet):
        env = SimTrainingEnvironment(
            candidate_brain_file=b_dst,
            candidate_hedge_file=h_dst,
            initial_balance_sats=initial_balance,
            taker_fee_rate=taker_fee_rate,
            trend_sl_floor_pct=sl_floor,
            fe_off_trend=fe_off_trend,
            stress_config=stress_cfg,
            short_exp_anchor_raw_atr=(short_exp_anchor == "raw_atr"),
            unlock_transition_regime=(regime_unlock_policy == "unlock_transition"),
            unlock_range_regime=(regime_unlock_policy == "unlock_range"),
            unlock_neutral_ema=(regime_unlock_policy == "unlock_neutral_ema"),
            forced_edge_loss_only=forced_edge_loss_only,
            forced_edge_min_loss_atr=forced_edge_min_loss_atr,
        )

        if regime_stats is not None:
            _attach_brain_regime_hook(env, regime_unlock_policy, regime_stats)

        _apply_geometry_knobs(env, sl_floor, cooldown_sec, short_exp_anchor)
        res = env.run_episode(blk, initial_balance=initial_balance)
    return res, env


def _attach_brain_regime_hook(
    env: SimTrainingEnvironment,
    unlock_policy: Optional[str],
    regime_stats: Optional[Counter],
) -> None:
    """
    Hook pasivo en memoria sobre env.brain.discretize_state para medir la
    distribución streaming de seg1 (REGIME) y seg5 (EMA) sin alterar claves ni Q-table.
    """
    del unlock_policy
    brain = getattr(env, "brain", None)
    if brain is None or regime_stats is None or not callable(getattr(brain, "discretize_state", None)):
        return

    orig_discretize = brain.discretize_state

    def wrapped_discretize(*args: Any, **kwargs: Any) -> str:
        state_key = str(orig_discretize(*args, **kwargs))
        parts = state_key.split("|")
        seg1 = parts[0] if len(parts) >= 1 else "UNK"
        seg5 = parts[4] if len(parts) >= 5 else "UNK"
        regime_stats[f"seg1:{seg1}"] += 1
        regime_stats[f"seg5:{seg5}"] += 1
        regime_stats[f"pair:{seg1}|{seg5}"] += 1
        return state_key

    brain.discretize_state = wrapped_discretize  # type: ignore[assignment]


def _summarize_closed(closed: List[dict]) -> Tuple[int, int, int, int]:
    wins = 0
    b_win = 0
    b_loss = 0
    for r in closed:
        pnl = int(r.get("net_pnl_sats", 0))
        if pnl > 0:
            wins += 1
            b_win += pnl
        elif pnl < 0:
            b_loss += abs(pnl)
    return len(closed), wins, b_win, b_loss


def _run_geometry_mode(
    *,
    descs: List[BlockDesc],
    seed_brain: str,
    scope: str,
    scenarios: List[Tuple[str, MicrostructureStressConfig]],
    fee_rt_list: List[float],
    sl_floors: List[float],
    cooldown_hours: List[float],
    anchors: List[str],
    initial_balance: int,
    quiet: bool = True,
) -> List[Dict[str, Any]]:
    cells: List[Tuple[CellAccumulator, MicrostructureStressConfig, float, float, str]] = []
    for sc_name, sc_cfg in scenarios:
        for fee_bps in fee_rt_list:
            taker_fee = (fee_bps / 2.0) / 10000.0
            for sl_floor in sl_floors:
                for cd_h in cooldown_hours:
                    for anchor in anchors:
                        cid = f"geom|{sc_name}|{fee_bps:g}bps|sl={sl_floor:.4f}|cd={cd_h:g}h|{anchor}"
                        acc = CellAccumulator(
                            cell_id=cid,
                            mode="geometry",
                            scope=scope,
                            scenario=sc_name,
                            fee_rt_bps=fee_bps,
                            taker_fee_rate=taker_fee,
                            initial_balance=initial_balance,
                            params={
                                "trend_sl_floor_pct": sl_floor,
                                "cooldown_hours": cd_h,
                                "pause_duration_sec": cd_h * 3600.0,
                                "short_exp_anchor": anchor,
                            },
                        )
                        cells.append((acc, sc_cfg, sl_floor, cd_h * 3600.0, anchor))

    with tempfile.TemporaryDirectory() as td:
        for desc, blk in _iter_streamed_blocks(descs, quiet=quiet):
            for c_idx, (acc, sc_cfg, sl_floor, cd_sec, anchor) in enumerate(cells):
                res, env = _run_env_block(
                    blk=blk,
                    seed_brain=seed_brain,
                    td=td,
                    tag=f"g{c_idx}_{desc.block_key}",
                    initial_balance=initial_balance,
                    taker_fee_rate=acc.taker_fee_rate,
                    stress_cfg=sc_cfg,
                    sl_floor=sl_floor,
                    cooldown_sec=cd_sec,
                    short_exp_anchor=anchor,
                    quiet=quiet,
                )
                closed = res.get("closed_trades", [])
                n_settled = int(res.get("total_settled_trades", len(closed)))
                _, wins, b_win, b_loss = _summarize_closed(closed)
                net = int(res.get("net_profit_sats", 0))
                dd_pct = float(res.get("max_drawdown_pct", 0.0))
                blk_ddg = int(getattr(env, "dd_guard_events", 0))
                final_eq = int(res.get("final_equity_sats", initial_balance))
                viol = _check_floor_violation(env, final_eq, initial_balance, dd_pct, blk_ddg)
                acc.record_block(
                    desc=desc,
                    n_trades=n_settled,
                    wins=wins,
                    net_sats=net,
                    b_win=b_win,
                    b_loss=b_loss,
                    dd_pct=dd_pct,
                    blk_ddg=blk_ddg,
                    floor_violated=viol,
                )

    return [acc.finalize() for acc, _, _, _, _ in cells]


def _run_regimes_mode(
    *,
    descs: List[BlockDesc],
    seed_brain: str,
    scope: str,
    scenarios: List[Tuple[str, MicrostructureStressConfig]],
    fee_rt_list: List[float],
    initial_balance: int,
    quiet: bool = True,
) -> List[Dict[str, Any]]:
    variants = [
        ("baseline", True, None),
        ("fe_off_disabled", False, None),
        ("unlock_transition", True, "unlock_transition"),
        ("unlock_range", True, "unlock_range"),
        ("unlock_neutral_ema", True, "unlock_neutral_ema"),
    ]
    cells: List[Tuple[CellAccumulator, MicrostructureStressConfig, bool, Optional[str]]] = []
    for sc_name, sc_cfg in scenarios:
        for fee_bps in fee_rt_list:
            taker_fee = (fee_bps / 2.0) / 10000.0
            for v_name, fe_off, unlock_pol in variants:
                cid = f"regimes|{sc_name}|{fee_bps:g}bps|{v_name}"
                acc = CellAccumulator(
                    cell_id=cid,
                    mode="regimes",
                    scope=scope,
                    scenario=sc_name,
                    fee_rt_bps=fee_bps,
                    taker_fee_rate=taker_fee,
                    initial_balance=initial_balance,
                    params={
                        "variant": v_name,
                        "fe_off_trend": fe_off,
                        "unlock_policy": unlock_pol or "none",
                    },
                )
                cells.append((acc, sc_cfg, fe_off, unlock_pol))

    with tempfile.TemporaryDirectory() as td:
        for desc, blk in _iter_streamed_blocks(descs, quiet=quiet):
            for c_idx, (acc, sc_cfg, fe_off, unlock_pol) in enumerate(cells):
                step_regimes: Counter = Counter()
                res, env = _run_env_block(
                    blk=blk,
                    seed_brain=seed_brain,
                    td=td,
                    tag=f"r{c_idx}_{desc.block_key}",
                    initial_balance=initial_balance,
                    taker_fee_rate=acc.taker_fee_rate,
                    stress_cfg=sc_cfg,
                    fe_off_trend=fe_off,
                    regime_unlock_policy=unlock_pol,
                    regime_stats=step_regimes,
                    quiet=quiet,
                )
                closed = res.get("closed_trades", [])
                for row in closed:
                    s_key = str(row.get("state_key") or "")
                    if s_key:
                        parts = s_key.split("|")
                        if len(parts) >= 5:
                            acc.extra_counters[f"trade_seg1:{parts[0]}"] += 1
                            acc.extra_counters[f"trade_seg5:{parts[4]}"] += 1
                acc.extra_counters.update(step_regimes)

                n_settled = int(res.get("total_settled_trades", len(closed)))
                _, wins, b_win, b_loss = _summarize_closed(closed)
                net = int(res.get("net_profit_sats", 0))
                dd_pct = float(res.get("max_drawdown_pct", 0.0))
                blk_ddg = int(getattr(env, "dd_guard_events", 0))
                final_eq = int(res.get("final_equity_sats", initial_balance))
                viol = _check_floor_violation(env, final_eq, initial_balance, dd_pct, blk_ddg)
                acc.record_block(
                    desc=desc,
                    n_trades=n_settled,
                    wins=wins,
                    net_sats=net,
                    b_win=b_win,
                    b_loss=b_loss,
                    dd_pct=dd_pct,
                    blk_ddg=blk_ddg,
                    floor_violated=viol,
                )

    return [acc.finalize() for acc, _, _, _ in cells]


def _extract_trade_fields(trade: dict, fallback_sl_pct: float) -> Dict[str, Any]:
    entry_ts = float(trade.get("entry_ts") or trade.get("open_ts") or trade.get("opened_at") or 0.0)
    exit_ts = float(trade.get("exit_ts") or trade.get("close_ts") or trade.get("closed_at") or entry_ts)
    if exit_ts < entry_ts:
        exit_ts = entry_ts
    side_raw = str(trade.get("side") or "b").strip().lower()
    side = "b" if side_raw in ("b", "buy", "long") else "s"
    entry_px = max(1.0, float(trade.get("entry_price") or trade.get("price") or 70000.0))
    exit_px = max(1.0, float(trade.get("exit_price") or trade.get("close_price") or entry_px))
    sl_px = float(trade.get("stoploss") or trade.get("sl_price") or trade.get("sl") or 0.0)
    if sl_px <= 0.0:
        sl_px = entry_px * (1.0 - fallback_sl_pct) if side == "b" else entry_px * (1.0 + fallback_sl_pct)
    margin = max(1.0, float(trade.get("margin") or trade.get("margin_sats") or 1000.0))
    leverage = max(1.0, float(trade.get("leverage") or 10.0))
    funding = float(trade.get("funding_sats") or trade.get("sum_funding_fees") or trade.get("sumFundingFees") or 0.0)
    net_pnl = float(trade.get("net_pnl_sats", 0.0))
    return {
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "side": side,
        "entry_price": entry_px,
        "exit_price": exit_px,
        "sl_price": sl_px,
        "margin_sats": margin,
        "leverage": leverage,
        "funding_sats": funding,
        "net_pnl_sats": net_pnl,
    }


def _simulate_isolated_lane_b_block(
    *,
    closed_a_raw: List[dict],
    closed_b_raw: List[dict],
    initial_balance: int,
    taker_fee_rate: float,
    sl_floor_b: float,
    lane_cfg: LaneBudgetConfig,
) -> Dict[str, Any]:
    """
    Entrelaza cronológicamente Carril A y candidatos de Carril B v2 sobre LaneBudgetController
    con matemática exacta de contrato inverso, verificando cero contagio a Carril A.
    """
    ctrl = LaneBudgetController(initial_equity_sats=initial_balance, config=lane_cfg)
    ctrl_a_only = LaneBudgetController(initial_equity_sats=initial_balance, config=lane_cfg)

    events: List[Tuple[float, int, str, int, Dict[str, Any]]] = []
    trades_a = [_extract_trade_fields(t, 0.0065) for t in closed_a_raw]
    trades_b = [_extract_trade_fields(t, sl_floor_b) for t in closed_b_raw]

    for idx, ta in enumerate(trades_a):
        events.append((ta["entry_ts"], 0, "A_OPEN", idx, ta))
        events.append((ta["exit_ts"], 2, "A_CLOSE", idx, ta))
    for idx, tb in enumerate(trades_b):
        events.append((tb["entry_ts"], 1, "B_OPEN", idx, tb))
        events.append((tb["exit_ts"], 3, "B_CLOSE", idx, tb))

    events.sort(key=lambda item: (item[0], item[1], item[3]))

    open_a_margin: Dict[int, float] = {}
    open_b: Dict[int, Dict[str, Any]] = {}
    accepted_b_pnls: List[int] = []
    rejected_b_reasons: Counter = Counter()

    for ts, _, kind, idx, tr in events:
        if kind == "A_OPEN":
            open_a_margin[idx] = tr["margin_sats"]
        elif kind == "A_CLOSE":
            open_a_margin.pop(idx, None)
            ctrl.record_lane_a_pnl(tr["net_pnl_sats"])
            ctrl_a_only.record_lane_a_pnl(tr["net_pnl_sats"])
        elif kind == "B_OPEN":
            n_open_a = len(open_a_margin)
            n_open_b_buy = sum(1 for v in open_b.values() if v["side"] == "b")
            n_open_b_sell = sum(1 for v in open_b.values() if v["side"] == "s")
            open_margin_b = sum(float(v["margin_eff"]) for v in open_b.values())
            open_sl_risk_b = sum(float(v["loss_at_sl"]) for v in open_b.values())
            avail_bal = max(0.0, ctrl.e_global - sum(open_a_margin.values()) - open_margin_b)

            loss_at_sl_new = compute_inverse_loss_at_sl_sats(
                margin_sats=tr["margin_sats"],
                leverage=tr["leverage"],
                entry_price=tr["entry_price"],
                sl_price=tr["sl_price"],
                fee_rate_open=taker_fee_rate,
                fee_rate_close=taker_fee_rate,
            )
            dec = ctrl.evaluate_lane_b_entry(
                side=tr["side"],
                now_ts=ts,
                n_open_a=n_open_a,
                n_open_b_buy=n_open_b_buy,
                n_open_b_sell=n_open_b_sell,
                avail_balance_sats=avail_bal,
                open_margin_b_sats=open_margin_b,
                margin_new_sats=tr["margin_sats"],
                open_loss_at_sl_b_sats=open_sl_risk_b,
                loss_at_sl_new_sats=loss_at_sl_new,
            )
            if not dec.allowed:
                rejected_b_reasons[dec.reason] += 1
                continue

            margin_eff = tr["margin_sats"] * dec.risk_multiplier
            pnl_info = compute_inverse_net_pnl_sats(
                side=tr["side"],
                margin_sats=margin_eff,
                leverage=tr["leverage"],
                entry_price=tr["entry_price"],
                exit_price=tr["exit_price"],
                fee_rate_open=taker_fee_rate,
                fee_rate_close=taker_fee_rate,
                funding_sats=tr["funding_sats"] * dec.risk_multiplier,
            )
            net_b = int(round(pnl_info["net_pnl_sats"]))
            open_b[idx] = {
                "side": tr["side"],
                "margin_eff": margin_eff,
                "loss_at_sl": loss_at_sl_new * dec.risk_multiplier,
                "net_pnl_sats": net_b,
            }
        elif kind == "B_CLOSE":
            active = open_b.pop(idx, None)
            if active is not None:
                net_b = int(active["net_pnl_sats"])
                accepted_b_pnls.append(net_b)
                ctrl.record_lane_b_trade_close(active["side"], net_b, now_ts=ts)

    # Cualquier trade B abierto al cierre del bloque se liquida al exit registrado
    for active in list(open_b.values()):
        net_b = int(active["net_pnl_sats"])
        accepted_b_pnls.append(net_b)
        ctrl.record_lane_b_trade_close(active["side"], net_b, now_ts=0.0)
    open_b.clear()

    no_contagion_ok = (
        ctrl.lane_a_recovery_mode == ctrl_a_only.lane_a_recovery_mode
        and abs(ctrl.dd_a - ctrl_a_only.dd_a) < 1e-12
    )

    all_pnls = [int(round(t["net_pnl_sats"])) for t in trades_a] + accepted_b_pnls
    wins = sum(1 for p in all_pnls if p > 0)
    b_win = sum(p for p in all_pnls if p > 0)
    b_loss = sum(abs(p) for p in all_pnls if p < 0)

    return {
        "n_trades": len(all_pnls),
        "wins": wins,
        "net_sats": int(round(ctrl.e_global - initial_balance)),
        "b_win": b_win,
        "b_loss": b_loss,
        "max_dd_pct": ctrl.dd_global * 100.0,
        "final_eq": int(round(ctrl.e_global)),
        "lane_b_trades": len(accepted_b_pnls),
        "lane_b_net_sats": sum(accepted_b_pnls),
        "dd_a_pct": round(ctrl.dd_a * 100.0, 3),
        "dd_b_pct": round(ctrl.dd_b * 100.0, 3),
        "no_contagion_ok": no_contagion_ok,
        "rejected_b_reasons": dict(rejected_b_reasons),
    }


def _run_lane_b_mode(
    *,
    descs: List[BlockDesc],
    seed_brain: str,
    scope: str,
    scenarios: List[Tuple[str, MicrostructureStressConfig]],
    fee_rt_list: List[float],
    sl_floors: List[float],
    initial_balance: int,
    quiet: bool = True,
) -> List[Dict[str, Any]]:
    breathable_sls = [s for s in sl_floors if 0.0110 <= s <= 0.0200]
    if not breathable_sls:
        breathable_sls = [0.0115, 0.0135, 0.0150]

    lane_cfg = LaneBudgetConfig(enabled=True)
    cells: List[Tuple[CellAccumulator, MicrostructureStressConfig, float]] = []
    for sc_name, sc_cfg in scenarios:
        for fee_bps in fee_rt_list:
            taker_fee = (fee_bps / 2.0) / 10000.0
            for sl_b in breathable_sls:
                cid = f"lane_b|{sc_name}|{fee_bps:g}bps|sl_b={sl_b:.4f}"
                acc = CellAccumulator(
                    cell_id=cid,
                    mode="lane_b",
                    scope=scope,
                    scenario=sc_name,
                    fee_rt_bps=fee_bps,
                    taker_fee_rate=taker_fee,
                    initial_balance=initial_balance,
                    params={
                        "lane_a_sl_floor": 0.0065,
                        "lane_b_sl_floor": sl_b,
                        "lane_b_max_margin_share": lane_cfg.lane_b_max_margin_share,
                        "lane_b_hard_dd_pause_pct": lane_cfg.lane_b_hard_dd_pause_pct,
                    },
                )
                acc.extra_meta["no_contagion_all_blocks"] = True
                cells.append((acc, sc_cfg, sl_b))

    with tempfile.TemporaryDirectory() as td:
        for desc, blk in _iter_streamed_blocks(descs, quiet=quiet):
            # Cache por (scenario, fee_bps) para Carril A en este bloque
            lane_a_cache: Dict[Tuple[str, float], Tuple[List[dict], SimTrainingEnvironment, int]] = {}
            for c_idx, (acc, sc_cfg, sl_b) in enumerate(cells):
                cache_key = (acc.scenario, acc.fee_rt_bps)
                if cache_key not in lane_a_cache:
                    res_a, env_a = _run_env_block(
                        blk=blk,
                        seed_brain=seed_brain,
                        td=td,
                        tag=f"la_{acc.scenario}_{acc.fee_rt_bps:g}_{desc.block_key}",
                        initial_balance=initial_balance,
                        taker_fee_rate=acc.taker_fee_rate,
                        stress_cfg=sc_cfg,
                        sl_floor=0.0065,
                        quiet=quiet,
                    )
                    lane_a_cache[cache_key] = (
                        res_a.get("closed_trades", []),
                        env_a,
                        int(getattr(env_a, "dd_guard_events", 0)),
                    )

                closed_a, env_a, blk_ddg_a = lane_a_cache[cache_key]
                res_b, env_b = _run_env_block(
                    blk=blk,
                    seed_brain=seed_brain,
                    td=td,
                    tag=f"lb{c_idx}_{desc.block_key}",
                    initial_balance=initial_balance,
                    taker_fee_rate=acc.taker_fee_rate,
                    stress_cfg=sc_cfg,
                    sl_floor=sl_b,
                    quiet=quiet,
                )
                closed_b = res_b.get("closed_trades", [])

                sim_blk = _simulate_isolated_lane_b_block(
                    closed_a_raw=closed_a,
                    closed_b_raw=closed_b,
                    initial_balance=initial_balance,
                    taker_fee_rate=acc.taker_fee_rate,
                    sl_floor_b=sl_b,
                    lane_cfg=lane_cfg,
                )
                blk_ddg = blk_ddg_a + int(getattr(env_b, "dd_guard_events", 0))
                viol = _check_floor_violation(
                    env_a,
                    sim_blk["final_eq"],
                    initial_balance,
                    sim_blk["max_dd_pct"],
                    blk_ddg,
                )
                if not sim_blk["no_contagion_ok"]:
                    acc.extra_meta["no_contagion_all_blocks"] = False
                acc.extra_counters["lane_b_trades"] += sim_blk["lane_b_trades"]
                acc.extra_counters["lane_b_net_sats"] += sim_blk["lane_b_net_sats"]
                for r_key, r_cnt in sim_blk["rejected_b_reasons"].items():
                    acc.extra_counters[f"rejected:{r_key}"] += r_cnt

                acc.record_block(
                    desc=desc,
                    n_trades=sim_blk["n_trades"],
                    wins=sim_blk["wins"],
                    net_sats=sim_blk["net_sats"],
                    b_win=sim_blk["b_win"],
                    b_loss=sim_blk["b_loss"],
                    dd_pct=sim_blk["max_dd_pct"],
                    blk_ddg=blk_ddg,
                    floor_violated=viol,
                    extra={
                        "lane_b_trades": sim_blk["lane_b_trades"],
                        "lane_b_net_sats": sim_blk["lane_b_net_sats"],
                        "no_contagion_ok": sim_blk["no_contagion_ok"],
                    },
                )

    return [acc.finalize() for acc, _, _ in cells]


def _run_exit_asym_mode(
    *,
    descs: List[BlockDesc],
    seed_brain: str,
    scope: str,
    scenarios: List[Tuple[str, MicrostructureStressConfig]],
    fee_rt_list: List[float],
    initial_balance: int,
    quiet: bool = True,
) -> List[Dict[str, Any]]:
    variants = [
        ("baseline", True, False, 0.35),
        ("fe_sym", False, False, 0.35),
        ("fe_loss_only_0.35atr", True, True, 0.35),
        ("fe_loss_only_0.60atr", True, True, 0.60),
    ]
    cells: List[Tuple[CellAccumulator, MicrostructureStressConfig, bool, bool, float]] = []
    for sc_name, sc_cfg in scenarios:
        for fee_bps in fee_rt_list:
            taker_fee = (fee_bps / 2.0) / 10000.0
            for v_name, e2_fe_off, fe_loss_only, min_loss_atr in variants:
                cid = f"exit_asym|{sc_name}|{fee_bps:g}bps|{v_name}"
                acc = CellAccumulator(
                    cell_id=cid,
                    mode="exit_asym",
                    scope=scope,
                    scenario=sc_name,
                    fee_rt_bps=fee_bps,
                    taker_fee_rate=taker_fee,
                    initial_balance=initial_balance,
                    params={
                        "variant": v_name,
                        "e2_fe_off": e2_fe_off,
                        "forced_edge_loss_only": fe_loss_only,
                        "forced_edge_min_loss_atr": min_loss_atr,
                    },
                )
                cells.append((acc, sc_cfg, e2_fe_off, fe_loss_only, min_loss_atr))

    with tempfile.TemporaryDirectory() as td:
        for desc, blk in _iter_streamed_blocks(descs, quiet=quiet):
            for c_idx, (acc, sc_cfg, e2_fe_off, fe_loss_only, min_loss_atr) in enumerate(cells):
                res, env = _run_env_block(
                    blk=blk,
                    seed_brain=seed_brain,
                    td=td,
                    tag=f"ea{c_idx}_{desc.block_key}",
                    initial_balance=initial_balance,
                    taker_fee_rate=acc.taker_fee_rate,
                    stress_cfg=sc_cfg,
                    fe_off_trend=e2_fe_off,
                    forced_edge_loss_only=fe_loss_only,
                    forced_edge_min_loss_atr=min_loss_atr,
                    quiet=quiet,
                )
                closed = res.get("closed_trades", [])
                fe_exits = sum(
                    1
                    for r in closed
                    if r.get("close_reason", r.get("reason")) == "FORCED_EDGE_EXIT"
                )
                acc.extra_counters["forced_edge_exits"] += fe_exits

                n_settled = int(res.get("total_settled_trades", len(closed)))
                _, wins, b_win, b_loss = _summarize_closed(closed)
                net = int(res.get("net_profit_sats", 0))
                dd_pct = float(res.get("max_drawdown_pct", 0.0))
                blk_ddg = int(getattr(env, "dd_guard_events", 0))
                final_eq = int(res.get("final_equity_sats", initial_balance))
                viol = _check_floor_violation(env, final_eq, initial_balance, dd_pct, blk_ddg)
                acc.record_block(
                    desc=desc,
                    n_trades=n_settled,
                    wins=wins,
                    net_sats=net,
                    b_win=b_win,
                    b_loss=b_loss,
                    dd_pct=dd_pct,
                    blk_ddg=blk_ddg,
                    floor_violated=viol,
                    extra={"forced_edge_exits": fe_exits},
                )

    return [acc.finalize() for acc, _, _, _, _ in cells]


def _format_table(rows: List[Dict[str, Any]]) -> str:
    headers = [
        "Cell",
        "N",
        "N/mes",
        "WR%",
        "Net_18M",
        "OOS_12M",
        "OOS_sin_M16",
        "PF_OOS12M",
        "MaxDD%",
        "DD>=4.5%",
        "DDG",
        "HFViol",
        "+Meses_OOS",
    ]
    str_rows: List[List[str]] = []
    for r in rows:
        pf_val = r["PF_OOS12M_raw"]
        pf_str = "inf" if pf_val == float("inf") else f"{pf_val:.3f}"
        str_rows.append(
            [
                str(r["cell_id"]),
                str(r["N"]),
                f"{r['N_mes']:.2f}",
                f"{r['WR_pct']:.1f}%",
                f"{r['Net_18M']:+d}",
                f"{r['OOS_12M']:+d}",
                f"{r['OOS_sin_M16']:+d}",
                pf_str,
                f"{r['MaxDD_pct']:.2f}%",
                f"{r['DD_ge_45']}/{r['blocks']}",
                str(r["DDG"]),
                str(r["HFViol"]),
                str(r["PlusMeses_OOS"]),
            ]
        )

    if not str_rows:
        return "(sin resultados)"

    widths = [max(len(headers[i]), *(len(row[i]) for row in str_rows)) for i in range(len(headers))]
    sep = " | "
    lines = [
        sep.join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        "-+-".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in str_rows:
        lines.append(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def _run_dry_check(descs: List[BlockDesc], cache_dir: str, seed_brain: str) -> int:
    missing: List[str] = []
    if not os.path.exists(seed_brain):
        missing.append(seed_brain)
    for d in descs:
        if not os.path.exists(d.pkl_path):
            missing.append(d.pkl_path)

    print(
        f"[DRY-CHECK] cache_dir={cache_dir} seed={'OK' if os.path.exists(seed_brain) else 'MISSING'} "
        f"pkls={len(descs) - sum(1 for d in descs if d.pkl_path in missing)}/{len(descs)}"
    )
    if missing:
        for item in missing[:10]:
            print(f"  - MISSING: {item}")
        return 1
    print("[OK] Dry-check superado.")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Laboratorio streaming Fase 5B v2 & Aislamiento de Carriles (LNM Sniper)."
    )
    parser.add_argument("--scope", choices=["fold1", "18m"], default="fold1")
    parser.add_argument("--scenarios", default="S0,S1,S2")
    parser.add_argument("--fee-rt-bps", default="16,20")
    parser.add_argument("--mode", choices=["geometry", "regimes", "lane_b", "exit_asym"], default="geometry")
    parser.add_argument("--sl-floors", default="0.0065,0.0115")
    parser.add_argument("--cooldown-hours", default="24,12,8")
    parser.add_argument("--short-exp-anchor", choices=["sl_dist", "raw_atr", "both"], default="sl_dist")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--initial-balance", type=int, default=DEFAULT_INITIAL_BALANCE)
    parser.add_argument("--dry-check", action="store_true")
    parser.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        default=True,
        help="Suprime warnings repetitivos por episodio e imprime progreso cada 10 bloques (default: True).",
    )
    parser.add_argument(
        "--no-quiet",
        dest="quiet",
        action="store_false",
        help="Muestra todos los mensajes/warnings por episodio sin filtrar.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    scenario_names = _parse_csv_strs(args.scenarios, "--scenarios", upper=True)
    scenarios = [(name, MicrostructureStressConfig.from_preset(name)) for name in scenario_names]
    fee_rt_list = _parse_csv_floats(args.fee_rt_bps, "--fee-rt-bps", min_val=0.0)
    sl_floors = _parse_csv_floats(args.sl_floors, "--sl-floors", min_val=0.0)
    cooldown_hours = _parse_csv_floats(args.cooldown_hours, "--cooldown-hours", min_val=0.0)
    anchors = ["sl_dist", "raw_atr"] if args.short_exp_anchor == "both" else [args.short_exp_anchor]

    descs = _discover_block_descs(args.scope, args.cache_dir)
    seed_brain = os.path.join(args.cache_dir, "q_brain_s2_clean_seed.json")

    if args.dry_check:
        return _run_dry_check(descs, args.cache_dir, seed_brain)

    if not os.path.exists(seed_brain):
        raise FileNotFoundError(f"Falta seed brain requerido: {seed_brain}")

    if args.mode == "geometry":
        results = _run_geometry_mode(
            descs=descs,
            seed_brain=seed_brain,
            scope=args.scope,
            scenarios=scenarios,
            fee_rt_list=fee_rt_list,
            sl_floors=sl_floors,
            cooldown_hours=cooldown_hours,
            anchors=anchors,
            initial_balance=args.initial_balance,
            quiet=args.quiet,
        )
    elif args.mode == "regimes":
        results = _run_regimes_mode(
            descs=descs,
            seed_brain=seed_brain,
            scope=args.scope,
            scenarios=scenarios,
            fee_rt_list=fee_rt_list,
            initial_balance=args.initial_balance,
            quiet=args.quiet,
        )
    elif args.mode == "exit_asym":
        results = _run_exit_asym_mode(
            descs=descs,
            seed_brain=seed_brain,
            scope=args.scope,
            scenarios=scenarios,
            fee_rt_list=fee_rt_list,
            initial_balance=args.initial_balance,
            quiet=args.quiet,
        )
    else:
        results = _run_lane_b_mode(
            descs=descs,
            seed_brain=seed_brain,
            scope=args.scope,
            scenarios=scenarios,
            fee_rt_list=fee_rt_list,
            sl_floors=sl_floors,
            initial_balance=args.initial_balance,
            quiet=args.quiet,
        )

    print(_format_table(results))

    if args.json_out:
        payload = {
            "scope": args.scope,
            "mode": args.mode,
            "scenarios": scenario_names,
            "fee_rt_bps": fee_rt_list,
            "sl_floors": sl_floors,
            "cooldown_hours": cooldown_hours,
            "short_exp_anchor": args.short_exp_anchor,
            "results": results,
        }
        _atomic_write_json(args.json_out, payload)
        print(f"[JSON] Escrito atómicamente en {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
