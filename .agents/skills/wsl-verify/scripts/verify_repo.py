#!/usr/bin/env python3
"""Verificador integral y hermético de LNM Sniper para WSL (Ubuntu).

Modos disponibles:
  quick      Compila sintácticamente (.py), valida CLI/invariantes P0/esquema 100/28,
             verifica strict_sha=True en cerebros/hedges y comprueba cachés WSL.
  full       Ejecuta quick + las 12 suites secuenciales + post-check SHA-256.
  oos-fold1  Certifica paridad directa 1:1 en SimTrainingEnvironment (M13..M18).
"""

import argparse
import glob
import inspect
import json
import os
import pickle
import py_compile
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)

ALL_SUITES = (
    "test_anti_whipsaw_hedge.py",
    "test_binance_micro_sidecar.py",
    "test_c1_streak3_24h_production.py",
    "test_candidate_certification.py",
    "test_learning_quality_and_sats.py",
    "test_limit_execution_manager.py",
    "test_liquidity_radar.py",
    "test_protocolo_v_reversal.py",
    "test_simulation_engine.py",
    "test_simulation_training_loop.py",
    "test_sniper_cli.py",
    "test_terminal_bellman_fix.py",
)

WSL_CACHES = (
    "/home/akapana/.cache/sniper_wf_s2_cache",
    "/home/akapana/.cache/sniper_wf_s2_v2_cache",
)


def purge_pycache() -> int:
    """Elimina directorios __pycache__ del repositorio sin dejar .pyc residuales."""
    removed = 0
    for root, dirs, _ in os.walk("."):
        if ".git" in dirs:
            dirs.remove(".git")
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"))
            removed += 1
            dirs.remove("__pycache__")
    leftover = [
        os.path.join(r, f)
        for r, dirs, fs in os.walk(".")
        if ".git" not in r.split(os.sep)
        for f in fs
        if f.endswith(".pyc")
    ]
    if leftover:
        raise AssertionError(f"Quedaron archivos .pyc residuales: {leftover[:5]}")
    return removed


def check_syntax_compile() -> int:
    """Compila estáticamente todos los módulos Python usando cfile en directorio temporal."""
    py_files = sorted(
        set(
            glob.glob("*.py")
            + glob.glob("simulation/*.py")
            + [os.path.join(".agents", "skills", "wsl-verify", "scripts", "verify_repo.py")]
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, path in enumerate(py_files):
            py_compile.compile(path, cfile=os.path.join(tmpdir, f"mod_{idx}.pyc"), doraise=True)
    print(f"[OK] py_compile estatico: {len(py_files)} archivos sin errores")
    return len(py_files)


def check_cli_flags() -> None:
    """Verifica --help y --dry-check en subproceso sin credenciales ni red."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for flag in ("--help", "--dry-check"):
        res = subprocess.run(
            [sys.executable, "-B", "lnm_sniper_v6_advanced.py", flag],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Fallo lnm_sniper_v6_advanced.py {flag} (rc={res.returncode}):\n"
                f"{res.stdout}\n{res.stderr}"
            )
    print("[OK] CLI --help y --dry-check ejecutados sin red ni credenciales")


def _extract_threshold_categories(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc.get("thresholds"), dict):
        return doc["thresholds"]
    return {
        k: v
        for k, v in doc.items()
        if isinstance(v, dict) and not k.startswith("_")
    }


def check_startup_and_schema_invariants() -> None:
    """Verifica invariantes P0 efectivos, esquema 100/12/28 y RAM-only brake."""
    import lnm_sniper_v6_advanced as v6
    import sniper_cli_catalog as cat
    from guardian_execution_core import ResilientLNMClient
    from learning_thresholds_manager import FALLBACK_THRESHOLDS, LearningThresholdsManager
    from limit_execution_manager import LimitExecutionConfig
    from liquidity_radar import LiquidityConfig
    from side_streak_brake import SideStreakBrakeStore

    tm = LearningThresholdsManager("config/learning_thresholds.json")
    v6.Config.configure_environment("mainnet")
    lim_cfg = LimitExecutionConfig.from_thresholds(tm)
    liq_cfg = LiquidityConfig.from_thresholds(tm)

    assert v6.Config.ENABLE_HYBRID_HEDGE is False
    assert v6.Config.DETERMINISTIC_TREND_POLICY is True
    assert v6.Config.FE_OFF_TREND is True
    assert int(tm.get("limit_execution", "enabled")) == 0 and lim_cfg.enabled is False
    assert int(tm.get("liquidity_radar", "enabled")) == 1 and liq_cfg.enabled is True
    assert int(tm.get("liquidity_radar", "shadow_only")) == 0 and liq_cfg.shadow_only is False
    assert (
        int(tm.get("liquidity_radar", "opposite_wall_enabled")) == 0
        and liq_cfg.opposite_wall_enabled is False
    )
    assert abs(v6.Config.BASE_RISK_PCT_STD - 0.0075) < 1e-9
    assert abs(v6.Config.RECOVERY_ENTER_DD - 0.035) < 1e-9
    assert abs(v6.Config.DD_THROTTLE_HALT_PCT - 0.045) < 1e-9
    assert abs(v6.Config.MAX_DD_GUARD_PCT - 0.052) < 1e-9
    assert abs(v6.Config.TREND_SL_FLOOR_PCT - 0.0065) < 1e-9
    assert abs(v6.Config.SHORT_EXP_MIN_DSL - 0.006505) < 1e-9
    assert v6.Config.MAX_CONCURRENT_TRADES == 5

    v6.Config.configure_environment("testnet")
    assert v6.Config.MAX_CONCURRENT_TRADES == 5
    v6.Config.configure_environment("mainnet")

    raw_main = _extract_threshold_categories("config/learning_thresholds.json")
    raw_tn = _extract_threshold_categories("config/learning_thresholds_testnet.json")
    schema_keys = {f"{c}.{p}" for c, ps in FALLBACK_THRESHOLDS.items() for p in ps}
    main_keys = {f"{c}.{p}" for c, ps in raw_main.items() for p in ps}
    tn_keys = {f"{c}.{p}" for c, ps in raw_tn.items() for p in ps}
    guide_keys = set(cat.PARAM_OPERATIONAL_GUIDE.keys())

    assert len(FALLBACK_THRESHOLDS) == 12, f"Categorias != 12: {len(FALLBACK_THRESHOLDS)}"
    assert len(schema_keys) == 100, f"Esquema != 100: {len(schema_keys)}"
    assert schema_keys == main_keys == tn_keys == guide_keys, "Desalineacion 4-way en thresholds/catalogo"
    assert len(cat.RUNTIME_PARAMETERS_SPEC) == 28, f"Runtime specs != 28: {len(cat.RUNTIME_PARAMETERS_SPEC)}"

    assert ResilientLNMClient._RATE_LIMITER.rate == 20.0
    assert ResilientLNMClient._RATE_LIMITER.capacity == 40.0
    src_pause = inspect.getsource(SideStreakBrakeStore.is_side_paused)
    assert "open(" not in src_pause, "is_side_paused no debe hacer I/O en disco"
    print("[OK] Invariantes P0, Modo B LIQ_BIAS_ONLY y esquema 100/12 + 28 runtime alineados")


def check_strict_sha_and_caches() -> None:
    """Valida strict_sha=True en los 4 archivos de estado y preserva cachés WSL."""
    from guardian_execution_core import AtomicBrainStore
    from hedge_state_store import HedgeStateStore

    for bf in ("q_brain_advanced_lnm.json", "q_brain_testnet_lnm.json"):
        ok, data = AtomicBrainStore(bf, required_key="q_table").verify_file(bf, strict_sha=True)
        assert ok and data is not None, f"strict_sha=True fallo en {bf}"
    for hf in ("hedge_state_mainnet.json", "hedge_state_testnet.json"):
        req = HedgeStateStore(hf).store.required_key
        ok, data = AtomicBrainStore(hf, required_key=req).verify_file(hf, strict_sha=True)
        assert ok and data is not None, f"strict_sha=True fallo en {hf}"
    print("[OK] strict_sha=True valido en los 4 JSON de cerebro/cobertura")

    for cdir in WSL_CACHES:
        if os.path.exists(cdir):
            cnt = len(glob.glob(os.path.join(cdir, "*.pkl")))
            assert cnt == 58, f"Conteo de cache alterado en {cdir}: {cnt} != 58"
            print(f"[OK] Cache WSL intacto: {cdir} ({cnt} .pkl)")


def run_all_suites_sequential() -> None:
    """Ejecuta secuencialmente las 12 suites con -B y aborta al primer fallo."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for idx, suite in enumerate(ALL_SUITES, 1):
        print(f"--- [{idx}/{len(ALL_SUITES)}] Ejecutando {suite} ---")
        res = subprocess.run(
            [sys.executable, "-B", suite],
            cwd=REPO_ROOT,
            env=env,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Suite fallo: {suite} (exit code {res.returncode})")
    print(f"[OK] Las {len(ALL_SUITES)} suites pasaron secuencialmente al 100%")


def run_oos_fold1_parity() -> None:
    """Certifica paridad exacta directa en SimTrainingEnvironment sobre M13..M18 (Fold 1 OOS)."""
    from simulation.sim_training_environment import SimTrainingEnvironment

    cache_dir = WSL_CACHES[0]
    seed_brain = os.path.join(cache_dir, "q_brain_s2_clean_seed.json")
    assert os.path.exists(seed_brain), f"Falta semilla OOS: {seed_brain}"

    total_trades = 0
    total_net = 0
    gross_win = 0
    gross_loss = 0
    max_dd_pct = 0.0
    dd_45 = 0
    ddg = 0
    otr = 0
    fe = 0
    m16 = None

    with tempfile.TemporaryDirectory() as td:
        for idx in range(12, 18):
            pkl = os.path.join(cache_dir, f"oos_m{idx}.pkl")
            with open(pkl, "rb") as fh:
                block = pickle.load(fh)
            b_dst = os.path.join(td, f"brain_m{idx}.json")
            h_dst = os.path.join(td, f"hedge_m{idx}.json")
            shutil.copy2(seed_brain, b_dst)
            if os.path.exists(seed_brain + ".sha256"):
                shutil.copy2(seed_brain + ".sha256", b_dst + ".sha256")

            env = SimTrainingEnvironment(
                candidate_brain_file=b_dst,
                candidate_hedge_file=h_dst,
                initial_balance_sats=100000,
            )
            res = env.run_episode(block, initial_balance=100000)
            closed = res.get("closed_trades", [])
            n = int(res.get("total_settled_trades", len(closed)))
            net = int(res.get("net_profit_sats", 0))
            dd_pct = float(res.get("max_drawdown_pct", 0.0))

            total_trades += n
            total_net += net
            gross_win += sum(int(r.get("net_pnl_sats", 0)) for r in closed if int(r.get("net_pnl_sats", 0)) > 0)
            gross_loss += sum(abs(int(r.get("net_pnl_sats", 0))) for r in closed if int(r.get("net_pnl_sats", 0)) < 0)
            max_dd_pct = max(max_dd_pct, dd_pct)
            if dd_pct >= 4.5:
                dd_45 += 1
            ddg += int(getattr(env, "dd_guard_events", 0))
            otr += sum(1 for r in closed if r.get("close_reason", r.get("reason")) == "OVER_TIME_RISK")
            fe += sum(1 for r in closed if r.get("close_reason", r.get("reason")) == "FORCED_EDGE_EXIT")
            if idx == 15:
                m16 = (net, n, dd_pct)
            print(f"M{idx+1:02d} (oos_m{idx}): N={n} Net={net:+d} MaxDD={dd_pct:.2f}%")

    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    print(
        f"FOLD1 SUMMARY: N={total_trades} Net={total_net:+d} PF={pf:.3f} (2dp={pf:.2f}) "
        f"MaxDD={max_dd_pct:.2f}% DD>=4.5%={dd_45}/6 DDG={ddg} OTR={otr} FE={fe} M16={m16[0]:+d} ({m16[1]})"
    )
    assert total_trades == 130
    assert total_net == 26933
    assert round(pf, 2) == 1.73
    assert round(max_dd_pct, 2) == 4.10
    assert dd_45 == 0 and ddg == 0 and otr == 0 and fe == 0
    assert m16[0] == 31746 and m16[1] == 52
    print("[OK] Paridad directa Fold 1 OOS verificada exactamente")


def print_git_status() -> None:
    if os.path.exists(".git"):
        res = subprocess.run(["git", "status", "--short"], cwd=REPO_ROOT, text=True, capture_output=True)
        out = res.stdout.rstrip()
        print("=== git status --short ===")
        print(out if out else "(limpio)")


def main() -> int:
    sys.dont_write_bytecode = True
    os.chdir(REPO_ROOT)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    parser = argparse.ArgumentParser(description="Verificador oficial WSL de LNM Sniper.")
    parser.add_argument(
        "--mode",
        choices=("quick", "full", "oos-fold1"),
        default="quick",
        help="Nivel de verificacion (default: quick).",
    )
    parser.add_argument(
        "--keep-pycache",
        action="store_true",
        help="No purga __pycache__ al finalizar.",
    )
    args = parser.parse_args()

    check_syntax_compile()
    check_cli_flags()
    check_startup_and_schema_invariants()
    check_strict_sha_and_caches()

    if args.mode == "full":
        run_all_suites_sequential()
        check_strict_sha_and_caches()
    elif args.mode == "oos-fold1":
        run_oos_fold1_parity()

    if not args.keep_pycache:
        removed = purge_pycache()
        if removed:
            print(f"[OK] __pycache__ purgados: {removed}")

    print_git_status()
    print(f"VERIFY_REPO ({args.mode}): PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
