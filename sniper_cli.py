#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sniper_cli.py
Consola de Control Cuantitativo, Hot-Reload Atómico y Telemetría para LNM Sniper v7.3+.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from guardian_execution_core import AtomicBrainStore, FileLockContext
from hedge_risk_manager import HedgeRiskManager
from learning_thresholds_manager import (
    DEFAULT_CONFIG_PATH,
    FALLBACK_THRESHOLDS,
    REQUIRED_CATEGORIES,
    STRUCTURAL_LOCKED_PARAMS,
    LearningThresholdsManager,
)
from lnm_sniper_v6_advanced import Config
from sniper_cli_catalog import (
    CATEGORY_TITLES,
    PARAM_OPERATIONAL_GUIDE,
    QUANTITATIVE_PRESETS,
    RUNTIME_PARAMETERS_SPEC,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_OVERRIDES_PATH = os.path.join(BASE_DIR, "config", "cli_runtime_overrides.json")
ACTIVE_PRESET_META_PATH = os.path.join(BASE_DIR, "config", "cli_active_preset.json")


class SniperControlCenter:
    def __init__(
        self,
        thresholds_path: str = DEFAULT_CONFIG_PATH,
        runtime_path: str = RUNTIME_OVERRIDES_PATH,
        base_dir: str = BASE_DIR,
    ) -> None:
        self.thresholds_path = os.path.abspath(thresholds_path)
        self.runtime_path = os.path.abspath(runtime_path)
        self.base_dir = os.path.abspath(base_dir)

    @staticmethod
    def _atomic_write_json(filepath: str, payload: Dict[str, Any], create_backup: bool = True) -> None:
        parent = os.path.dirname(os.path.abspath(filepath))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        raw_bytes = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        tmp_path = f"{filepath}.tmp.{os.getpid()}.{int(time.monotonic() * 1000000) % 1000000}"
        with FileLockContext(filepath, exclusive=True, timeout=5.0):
            try:
                with open(tmp_path, "wb") as fh:
                    fh.write(raw_bytes)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                if create_backup and os.path.exists(filepath):
                    try:
                        shutil.copy2(filepath, f"{filepath}.bak")
                    except OSError:
                        pass
                os.replace(tmp_path, filepath)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

    @staticmethod
    def _safe_read_json(filepath: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(filepath):
            return None
        try:
            with FileLockContext(filepath, exclusive=False, timeout=2.5, raise_on_timeout=False):
                with open(filepath, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def load_thresholds_tree(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        data = self._safe_read_json(self.thresholds_path)
        if isinstance(data, dict):
            ok, _, validated = LearningThresholdsManager.validate_schema_and_limits(data)
            if ok and validated is not None:
                return validated
        return copy.deepcopy(FALLBACK_THRESHOLDS)

    def load_runtime_overrides(self) -> Dict[str, Any]:
        defaults = {k: spec["default"] for k, spec in RUNTIME_PARAMETERS_SPEC.items()}
        saved = self._safe_read_json(self.runtime_path)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in RUNTIME_PARAMETERS_SPEC:
                    ok, coerced, _ = self.validate_runtime_param(k, v)
                    if ok:
                        defaults[k] = coerced
        return defaults

    def get_active_trades_count(self, env_mode: Optional[str] = None) -> Tuple[int, List[str]]:
        envs = [env_mode.lower()] if env_mode else ["mainnet", "testnet"]
        total_active = 0
        sources: List[str] = []
        for env in envs:
            tel_file = "telemetry_live.json" if env == "mainnet" else "telemetry_live_testnet.json"
            tel_data = self._safe_read_json(os.path.join(self.base_dir, tel_file))
            tel_active = int(tel_data.get("active_trades", 0)) if isinstance(tel_data, dict) else 0

            brain_file = "q_brain_advanced_lnm.json" if env == "mainnet" else "q_brain_testnet_lnm.json"
            brain_path = os.path.join(self.base_dir, brain_file)
            brain_active = 0
            if os.path.exists(brain_path):
                b_data = AtomicBrainStore(brain_path).load()
                if isinstance(b_data, dict) and isinstance(b_data.get("active_trades"), dict):
                    brain_active = len(b_data["active_trades"])

            env_max = max(tel_active, brain_active)
            if env_max > 0:
                total_active += env_max
                sources.append(f"{env.upper()}:{env_max}")
        return total_active, sources

    def _pid_file_path(self, env_mode: str) -> str:
        return os.path.join(self.base_dir, f".sniper_{env_mode.lower()}.pid")

    def get_running_pid(self, env_mode: str = "mainnet") -> Optional[int]:
        pid_file = self._pid_file_path(env_mode)
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r", encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
                os.kill(pid, 0)
                return pid
            except (ValueError, OSError):
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
        return None

    def notify_hot_reload_signal(self) -> List[int]:
        signaled_pids: List[int] = []
        sig_hup = getattr(signal, "SIGHUP", None)
        if sig_hup is None:
            return signaled_pids
        for env in ("mainnet", "testnet"):
            pid = self.get_running_pid(env)
            if pid is not None:
                try:
                    os.kill(pid, sig_hup)
                    signaled_pids.append(pid)
                except OSError:
                    pass
        return signaled_pids

    @staticmethod
    def is_param_structurally_locked(category: str, param_name: str, param_obj: Dict[str, Any]) -> bool:
        if (category, param_name) in STRUCTURAL_LOCKED_PARAMS:
            return True
        return bool(param_obj.get("structural_lock_when_open", False))

    def set_threshold_parameter(
        self, full_key: str, raw_value: Any, env_check: Optional[str] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if "." not in full_key:
            return False, "Formato inválido. Usa '<categoria>.<parametro>'.", None
        category, param_name = [x.strip() for x in full_key.split(".", 1)]
        tree = self.load_thresholds_tree()
        if category not in tree or param_name not in tree[category]:
            return False, f"Parámetro '{full_key}' no existe en config/learning_thresholds.json.", None

        param_obj = tree[category][param_name]
        old_val, min_lim, max_lim = param_obj["value"], param_obj["min_limit"], param_obj["max_limit"]
        is_int = isinstance(old_val, int) and not isinstance(old_val, bool) and isinstance(min_lim, int) and isinstance(max_lim, int)
        try:
            num_val = float(raw_value)
            if num_val != num_val or num_val in (float("inf"), float("-inf")):
                return False, f"Valor numérico inválido: {raw_value!r}.", None
            coerced_val: Any = int(round(num_val)) if is_int else round(float(num_val), 6)
        except (ValueError, TypeError):
            return False, f"Valor no numérico '{raw_value}' para '{full_key}'.", None

        if float(coerced_val) < float(min_lim) or float(coerced_val) > float(max_lim):
            return False, f"Valor fuera de rango en '{full_key}': {coerced_val} no pertenece a [{min_lim}, {max_lim}].", None

        if self.is_param_structurally_locked(category, param_name, param_obj) and float(coerced_val) != float(old_val):
            active_cnt, active_srcs = self.get_active_trades_count(env_check)
            if active_cnt > 0:
                return (
                    False,
                    f"[BLOQUEO ESTRUCTURAL ACTIVO] '{full_key}' no puede mutar con posiciones abiertas ({', '.join(active_srcs)}).",
                    None,
                )

        tree[category][param_name]["value"] = coerced_val
        is_valid, reason, validated_tree = LearningThresholdsManager.validate_schema_and_limits(tree)
        if not is_valid or validated_tree is None:
            return False, f"Rechazado por LearningThresholdsManager: {reason}", None

        self._atomic_write_json(self.thresholds_path, validated_tree, create_backup=True)
        pids = self.notify_hot_reload_signal()
        pid_msg = f" | SIGHUP enviado a PID(s): {pids}" if pids else " | Hot-Reload vía mtime (<2s)"
        return (
            True,
            f"✅ Parámetro '{full_key}' actualizado: {old_val} ➔ {coerced_val} [{param_obj['unit']}]{pid_msg}",
            {"key": full_key, "old_value": old_val, "new_value": coerced_val, "unit": param_obj["unit"], "signaled_pids": pids},
        )

    def validate_runtime_param(self, key: str, raw_val: Any) -> Tuple[bool, Any, str]:
        k = key.strip().upper()
        if k in ("INITIAL_FLOOR_RATIO", "MILESTONE_FLOOR_RATIO", "MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT", "KILL_SWITCH_DD_PCT", "NUM_ACTIONS"):
            return False, None, f"[PROTECCIÓN P0 INVIOLABLE] '{k}' pertenece a HardFloorGuardian / 9 acciones y es intocable."
        if k not in RUNTIME_PARAMETERS_SPEC:
            return False, None, f"Parámetro de runtime desconocido '{k}'."

        spec = RUNTIME_PARAMETERS_SPEC[k]
        ptype = spec["type"]
        if ptype == "bool":
            if isinstance(raw_val, bool):
                return True, raw_val, "OK"
            s = str(raw_val).strip().lower()
            if s in ("1", "true", "yes", "on", "si", "sí"):
                return True, True, "OK"
            if s in ("0", "false", "no", "off"):
                return True, False, "OK"
            return False, None, f"Booleano inválido para '{k}'."
        if ptype == "str":
            s = str(raw_val).strip().lower()
            if "allowed" in spec and s not in spec["allowed"]:
                return False, None, f"Valor '{s}' no permitido en '{k}'. Opciones: {spec['allowed']}."
            return True, s, "OK"
        if ptype in ("int", "float"):
            try:
                fval = float(raw_val)
                if fval != fval or fval in (float("inf"), float("-inf")):
                    raise ValueError("NaN")
                val: Any = int(round(fval)) if ptype == "int" else round(fval, 5)
            except (ValueError, TypeError):
                return False, None, f"Número inválido para '{k}': {raw_val!r}."
            if float(val) < float(spec["min_limit"]) or float(val) > float(spec["max_limit"]):
                return False, None, f"'{k}' fuera de rango P0: {val} no está en [{spec['min_limit']}, {spec['max_limit']}]."
            return True, val, "OK"
        return False, None, "Tipo no soportado."

    def set_runtime_parameter(self, key: str, raw_val: Any) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        k = key.strip().upper()
        ok, coerced, reason = self.validate_runtime_param(k, raw_val)
        if not ok:
            return False, reason, None
        current = self.load_runtime_overrides()
        old_val = current.get(k)
        if k == "MIN_LEVERAGE" and int(coerced) > int(current.get("MAX_LEVERAGE", 85)):
            return False, "MIN_LEVERAGE no puede superar a MAX_LEVERAGE.", None
        if k == "MAX_LEVERAGE" and int(coerced) < int(current.get("MIN_LEVERAGE", 15)):
            return False, "MAX_LEVERAGE no puede ser menor que MIN_LEVERAGE.", None
        current[k] = coerced
        self._atomic_write_json(self.runtime_path, current, create_backup=True)
        unit = RUNTIME_PARAMETERS_SPEC[k]["unit"]
        return (
            True,
            f"✅ Parámetro táctico '{k}' guardado: {old_val} ➔ {coerced} [{unit}]",
            {"key": k, "old_value": old_val, "new_value": coerced, "unit": unit},
        )

    def reset_runtime_overrides(self) -> Tuple[bool, str]:
        defaults = {k: spec["default"] for k, spec in RUNTIME_PARAMETERS_SPEC.items()}
        self._atomic_write_json(self.runtime_path, defaults, create_backup=True)
        return True, "✅ Parámetros tácticos de runtime restaurados a valores base v7.3+."

    def apply_preset(self, preset_name: str, env_mode: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        name = preset_name.strip().lower()
        if name not in QUANTITATIVE_PRESETS:
            return False, f"Preset '{name}' desconocido. Opciones: {', '.join(QUANTITATIVE_PRESETS.keys())}.", {}

        preset = QUANTITATIVE_PRESETS[name]
        runtime_cfg = self.load_runtime_overrides()
        effective_env = (env_mode or runtime_cfg.get("ENV_MODE", "mainnet")).strip().lower()
        is_shadow = bool(runtime_cfg.get("SHADOW_MODE", False))

        if not preset.get("allow_live_mainnet", True) and effective_env == "mainnet" and not is_shadow:
            return (
                False,
                f"🛑 [BLOQUEO DE SEGURIDAD P0] El perfil '{name}' es exclusivo para Testnet o SHADOW_MODE=True.",
                {},
            )

        tree = self.load_thresholds_tree()
        combined_thr = dict(QUANTITATIVE_PRESETS["baseline_v73"]["thresholds"])
        combined_thr.update(preset.get("thresholds", {}))

        applied_thr: List[Tuple[str, Any, Any]] = []
        for full_k, target_v in combined_thr.items():
            cat, p_name = full_k.split(".", 1)
            if cat in tree and p_name in tree[cat]:
                p_obj = tree[cat][p_name]
                if self.is_param_structurally_locked(cat, p_name, p_obj):
                    continue
                prev_v = p_obj["value"]
                is_int = isinstance(prev_v, int) and not isinstance(prev_v, bool)
                new_v = int(round(float(target_v))) if is_int else round(float(target_v), 6)
                if prev_v != new_v:
                    applied_thr.append((full_k, prev_v, new_v))
                tree[cat][p_name]["value"] = new_v

        ok, reason, validated_tree = LearningThresholdsManager.validate_schema_and_limits(tree)
        if not ok or validated_tree is None:
            return False, f"Error validando preset '{name}': {reason}", {}
        self._atomic_write_json(self.thresholds_path, validated_tree, create_backup=True)

        base_rt = {k: spec["default"] for k, spec in RUNTIME_PARAMETERS_SPEC.items()}
        base_rt["ENV_MODE"] = runtime_cfg.get("ENV_MODE", "mainnet")
        base_rt["SHADOW_MODE"] = runtime_cfg.get("SHADOW_MODE", False)
        for rk, rv in preset.get("runtime", {}).items():
            if rk in RUNTIME_PARAMETERS_SPEC:
                ok_r, coerced_r, _ = self.validate_runtime_param(rk, rv)
                if ok_r:
                    base_rt[rk] = coerced_r

        applied_rt = [(rk, runtime_cfg.get(rk), rv) for rk, rv in base_rt.items() if runtime_cfg.get(rk) != rv]
        self._atomic_write_json(self.runtime_path, base_rt, create_backup=True)
        self._atomic_write_json(
            ACTIVE_PRESET_META_PATH,
            {
                "active_preset": name,
                "title": preset["title"],
                "applied_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            create_backup=False,
        )
        pids = self.notify_hot_reload_signal()
        return (
            True,
            f"✅ Perfil '{name}' ({preset['title']}) aplicado ({len(applied_thr)} umbrales Hot-Reload, {len(applied_rt)} runtime).",
            {"preset": name, "thresholds_diff": applied_thr, "runtime_diff": applied_rt, "signaled_pids": pids},
        )

    def update_brain_target_sats(self, target_sats: int, env_mode: str = "mainnet") -> Tuple[bool, str]:
        env = env_mode.strip().lower()
        target_val = int(target_sats)
        if target_val < 10000:
            return False, "La meta en satoshis debe ser >= 10,000 sats."
        active_cnt, active_srcs = self.get_active_trades_count(env)
        if active_cnt > 0:
            return False, f"⏸️ [BLOQUEO DE INTEGRIDAD] Hay posiciones abiertas ({', '.join(active_srcs)})."

        brain_file = "q_brain_advanced_lnm.json" if env == "mainnet" else "q_brain_testnet_lnm.json"
        brain_path = os.path.join(self.base_dir, brain_file)
        if not os.path.exists(brain_path):
            return False, f"Archivo no encontrado: {brain_path}"

        store = AtomicBrainStore(brain_path)
        data = store.load()
        if not isinstance(data, dict) or "q_table" not in data:
            return False, f"Fallo verificando {brain_file} bajo strict_sha=True."
        old_target = int(data.get("current_target", Config.INITIAL_TARGET_SATS))
        data["current_target"] = target_val
        digest = store.save(data)
        return True, f"✅ Meta en {brain_file} actualizada: {old_target:,} ➔ {target_val:,} sats (SHA-256: {digest[:16]}...)."


def build_full_parameter_catalog(ctrl: SniperControlCenter) -> Dict[str, Dict[str, Any]]:
    tree = ctrl.load_thresholds_tree()
    catalog: Dict[str, Dict[str, Any]] = {}
    for cat in REQUIRED_CATEGORIES:
        for p_name, p_obj in tree.get(cat, {}).items():
            full_key = f"{cat}.{p_name}"
            guide = PARAM_OPERATIONAL_GUIDE.get(full_key, {})
            catalog[full_key] = {
                "group": cat, "scope": "HOT_RELOAD", "value": p_obj["value"], "unit": p_obj["unit"],
                "min_limit": p_obj["min_limit"], "max_limit": p_obj["max_limit"],
                "structural_lock": ctrl.is_param_structurally_locked(cat, p_name, p_obj),
                "description": p_obj["description"],
                "math_role": guide.get("math_role", f"Umbral dinámico de '{cat}'."),
                "impact_up": guide.get("impact_up", "SUBIR (↑): Incrementa el rigor del umbral."),
                "impact_down": guide.get("impact_down", "BAJAR (↓): Reduce la magnitud del umbral."),
            }
    rt_values = ctrl.load_runtime_overrides()
    for rk, spec in RUNTIME_PARAMETERS_SPEC.items():
        catalog[f"runtime.{rk}"] = {
            "group": "runtime", "scope": "RUNTIME_BOOT", "value": rt_values.get(rk, spec["default"]),
            "unit": spec["unit"], "min_limit": spec.get("min_limit", "-"), "max_limit": spec.get("max_limit", "-"),
            "structural_lock": False, "description": spec["description"],
            "math_role": spec["math_role"], "impact_up": spec["impact_up"], "impact_down": spec["impact_down"],
        }
    return catalog


def print_status_dashboard(ctrl: SniperControlCenter, env_mode: str = "mainnet") -> None:
    env = env_mode.strip().lower()
    tel_file = "telemetry_live.json" if env == "mainnet" else "telemetry_live_testnet.json"
    brain_file = "q_brain_advanced_lnm.json" if env == "mainnet" else "q_brain_testnet_lnm.json"
    hedge_file = "hedge_state_mainnet.json" if env == "mainnet" else "hedge_state_testnet.json"

    pid = ctrl.get_running_pid(env)
    preset_meta = ctrl._safe_read_json(ACTIVE_PRESET_META_PATH) or {}
    rt_cfg = ctrl.load_runtime_overrides()

    print("=" * 84)
    print(f"  LNM SNIPER v7.3+ — PANEL DE CONTROL Y TELEMETRÍA [{env.upper()}]")
    print("=" * 84)
    print(f"  Demonio CLI    : {'ACTIVO (PID: ' + str(pid) + ')' if pid else 'DETENIDO / EXTERNO'} | Preset: {preset_meta.get('active_preset', 'baseline_v73')}")
    print(f"  Overrides Boot : ENV={rt_cfg['ENV_MODE']} | SHADOW={rt_cfg['SHADOW_MODE']} | SAFE={rt_cfg['SAFE_MODE']} | HEDGE={rt_cfg['ENABLE_HYBRID_HEDGE']} | MaxPos={rt_cfg['MAX_CONCURRENT_TRADES']} | MaxLev={rt_cfg['MAX_LEVERAGE']}x")

    brain_path = os.path.join(ctrl.base_dir, brain_file)
    if os.path.exists(brain_path):
        sha_ok, b_data = AtomicBrainStore(brain_path).verify_file(brain_path, strict_sha=True)
        if isinstance(b_data, dict):
            print("-" * 84)
            print(f"  Cerebro Activo : {brain_file} [{'SHA-256 OK' if sha_ok else 'ALERTA SHA'}]")
            print(f"  Política Q     : {len(b_data.get('q_table', {}))} estados | Cierres: {int(b_data.get('total_trained', 0))} | Eps: {float(b_data.get('epsilon', 0.0)):.4f} | Activas: {len(b_data.get('active_trades', {}))}")
            print(f"  Contabilidad   : PnL Neto: {int(b_data.get('realized_net_profit_sats', 0)):+,} sats | Fees: {int(b_data.get('total_fees_paid_sats', 0)):,} | Piso: {int(b_data.get('locked_floor', 0)):,} | Meta: {int(b_data.get('current_target', 300000)):,} sats")

    hedge_path = os.path.join(ctrl.base_dir, hedge_file)
    if os.path.exists(hedge_path):
        h_ok, h_data = AtomicBrainStore(hedge_path, required_key="pairs").verify_file(hedge_path, strict_sha=True)
        if isinstance(h_data, dict):
            hm = h_data.get("hedge_metrics", {})
            print(f"  Hedge Engine   : {hedge_file} [{'OK' if h_ok else 'WARN'}] | Pares: {len(h_data.get('pairs', {}))} | Estados Hedge Q: {len(h_data.get('hedge_q_table', {}))} | Ahorrado vs SL: {int(hm.get('net_sats_saved_vs_sl', 0)):+,} sats")

    tel = ctrl._safe_read_json(os.path.join(ctrl.base_dir, tel_file))
    print("-" * 84)
    if not isinstance(tel, dict):
        print(f"  Telemetría Vivo: Sin {tel_file} aún (se exporta automáticamente al correr el bot).")
    else:
        rl = tel.get("rl_metrics", {})
        adj = rl.get("autonomous_adjustments", {})
        print(f"  Telemetría Vivo: {tel.get('timestamp', '-')} | Bal: {int(tel.get('balance_sats', 0)):,} sats | Eq: {int(tel.get('equity_sats', 0)):,} sats | Activas: {int(tel.get('active_trades', 0))}")
        print(f"  Salud / Guard  : Policy={rl.get('policy_health', '-')} | Guard={rl.get('learning_guard_status', '-')} | ExpEfectivo={adj.get('effective_min_expectancy_ratio', 3.85):.2f}x | TrailMult={adj.get('dynamic_trailing_multiplier', 1.0):.2f}x")
        print(f"    1. TD-Error  : avg50={rl.get('rolling_td_error_avg', 0.0):.4f} (last={rl.get('last_abs_td_error', 0.0):.4f}) -> [{rl.get('td_error_status', '-')}]")
        print(f"    2. Q-Bias    : {rl.get('q_bias_sats', 0.0):+.2f} sats (std={rl.get('q_bias_std_sats', 0.0):.2f}) -> [{rl.get('q_bias_status', '-')}]")
        print(f"    3. FER       : Rolling={rl.get('fee_efficiency_ratio', 0.0):.2f} | Global={rl.get('global_fee_efficiency_ratio', 0.0):.2f} -> [{rl.get('fer_status', '-')}]")
        print(f"    4. Entropía  : H_norm={rl.get('normalized_policy_entropy', 0.0):.3f} | Dom={rl.get('dominant_action', '-')} ({float(rl.get('dominant_action_freq', 0.0))*100:.1f}%)")
        print(f"    5. MFE / MAE : Captura MFE={float(rl.get('mfe_capture_avg', 0.0))*100:.1f}% | MAE/Margen={float(rl.get('mae_margin_ratio_avg', 0.0))*100:.1f}%")
    print("=" * 84)


def print_parameter_explanation(catalog: Dict[str, Dict[str, Any]], query_key: str) -> bool:
    q = query_key.strip()
    if q not in catalog:
        matches = [k for k in catalog if k.lower() == q.lower() or k.lower().endswith(f".{q.lower()}")]
        if len(matches) == 1:
            q = matches[0]
        else:
            print(f"[ERROR] Parámetro '{query_key}' no encontrado.")
            return False
    info = catalog[q]
    print("=" * 84)
    print(f"  FICHA MATEMÁTICA Y OPERATIVA: {q}")
    print("=" * 84)
    print(f"  Valor Actual       : {info['value']} [{info['unit']}] | Rango: [{info['min_limit']}, {info['max_limit']}]")
    print(f"  Candado Open Trades: {'SÍ (Bloqueado si active_trades > 0)' if info['structural_lock'] else 'NO (Hot-Reload libre)'}")
    print(f"  Descripción        : {info['description']}")
    print(f"  Ecuación / Rol Mat.: {info['math_role']}")
    print(f"  • {info['impact_up']}")
    print(f"  • {info['impact_down']}")
    print("=" * 84)
    return True


def print_catalog(
    ctrl: SniperControlCenter, category_filter: Optional[str] = None, search_term: Optional[str] = None, compact: bool = False
) -> None:
    catalog = build_full_parameter_catalog(ctrl)
    cat_f = category_filter.strip().lower() if category_filter else None
    s_term = search_term.strip().lower() if search_term else None
    grouped: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for k, v in catalog.items():
        if cat_f and v["group"].lower() != cat_f:
            continue
        if s_term and s_term not in f"{k} {v['description']} {v['math_role']} {v['impact_up']} {v['impact_down']}".lower():
            continue
        grouped.setdefault(v["group"], []).append((k, v))

    for grp, items in grouped.items():
        print("\n" + "=" * 84)
        print(f"  [{grp.upper()}] — {CATEGORY_TITLES.get(grp, 'Parámetros Tácticos de Runtime')} ({len(items)})")
        print("=" * 84)
        for full_k, meta in items:
            lock_b = " 🔒[LOCK-OPEN]" if meta["structural_lock"] else ""
            print(f"  • {full_k:<44} = {str(meta['value']):<8} ({meta['unit']}) | [{meta['min_limit']}, {meta['max_limit']}]{lock_b}")
            print(f"      Explicación: {meta['description']}")
            if not compact:
                print(f"      Rol Mat.   : {meta['math_role']}")
                print(f"      Impacto (↑): {meta['impact_up']}")
                print(f"      Impacto (↓): {meta['impact_down']}")
            print("-" * 84)


def print_presets_info(preset_name: Optional[str] = None) -> bool:
    if not preset_name or preset_name.lower() == "list":
        print("=" * 84)
        print("  5 CONFIGURACIONES PREDETERMINADAS (PRESETS CUANTITATIVOS)")
        print("=" * 84)
        for name, p in QUANTITATIVE_PRESETS.items():
            tag = "MAINNET / TESTNET" if p.get("allow_live_mainnet") else "SOLO TESTNET / MODO SOMBRA"
            print(f"  🔹 {name:<24} [{tag}]\n     Título  : {p['title']}\n     Régimen : {p['regime_target']}\n     Síntesis: {p['math_summary']}\n" + "-" * 84)
        return True
    name = preset_name.strip().lower()
    if name not in QUANTITATIVE_PRESETS:
        print(f"[ERROR] Preset '{name}' no existe.")
        return False
    p = QUANTITATIVE_PRESETS[name]
    print("=" * 84)
    print(f"  PERFIL: {name} — {p['title']}")
    print("=" * 84)
    print(f"  Régimen : {p['regime_target']}\n  Resumen : {p['math_summary']}")
    print("-" * 84)
    for k, v in p.get("thresholds", {}).items():
        print(f"  • {k:<44} -> {v}")
    for rk, rv in p.get("runtime", {}).items():
        print(f"  • runtime.{rk:<36} -> {rv}")
    print("=" * 84)
    return True


def run_bot_with_overrides(
    ctrl: SniperControlCenter, env_override: Optional[str] = None,
    shadow_override: Optional[bool] = None, safe_override: Optional[bool] = None, once_mode: bool = False
) -> None:
    import lnm_sniper_v6_advanced as bot_mod
    rt_cfg = ctrl.load_runtime_overrides()
    target_env = (env_override or rt_cfg.get("ENV_MODE", "mainnet")).strip().lower()
    is_shadow = bool(rt_cfg.get("SHADOW_MODE", False)) if shadow_override is None else bool(shadow_override)
    is_safe = bool(rt_cfg.get("SAFE_MODE", False)) if safe_override is None else bool(safe_override)

    os.environ["SNIPER_ENV"] = target_env
    os.environ["SNIPER_SHADOW_MODE"] = "1" if is_shadow else "0"
    os.environ["SNIPER_SAFE_MODE"] = "1" if is_safe else "0"
    orig_configure = bot_mod.Config.configure_environment

    def _patched_configure(env_name: str = "mainnet") -> None:
        orig_configure(env_name)
        for k, val in ctrl.load_runtime_overrides().items():
            if k in ("ENV_MODE", "SAFE_MODE"):
                continue
            spec = RUNTIME_PARAMETERS_SPEC.get(k, {})
            if spec.get("target") == "Config":
                if k == "MAX_CONCURRENT_TRADES" and bot_mod.Config.ENV_MODE == "mainnet":
                    val = min(5, max(1, int(val)))
                setattr(bot_mod.Config, k, val)
            elif spec.get("target") == "HedgeRiskManager":
                setattr(HedgeRiskManager, k, val)
        if shadow_override is not None:
            bot_mod.Config.SHADOW_MODE = bool(shadow_override)
        bot_mod.Config.sync_from_thresholds(bot_mod.THRESHOLDS_MANAGER)

    bot_mod.Config.configure_environment = _patched_configure
    sys.argv = [sys.argv[0], "--env", target_env] + (["--safe-mode"] if is_safe else []) + (["--once"] if once_mode else [])
    pid_file = ctrl._pid_file_path(target_env)
    try:
        with open(pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        bot_mod.main()
    finally:
        if os.path.exists(pid_file):
            try:
                os.remove(pid_file)
            except OSError:
                pass


def manage_daemon(
    ctrl: SniperControlCenter, action: str, env_mode: Optional[str] = None,
    shadow_flag: Optional[bool] = None, safe_flag: Optional[bool] = None, lines: int = 40
) -> int:
    rt_cfg = ctrl.load_runtime_overrides()
    env = (env_mode or rt_cfg.get("ENV_MODE", "mainnet")).strip().lower()
    log_path = os.path.join(ctrl.base_dir, f"sniper_{env}.log")
    act = action.strip().lower()

    if act == "start":
        pid = ctrl.get_running_pid(env)
        if pid is not None:
            print(f"ℹ️ Demonio [{env.upper()}] ya activo (PID: {pid}).")
            return 0
        if shutil.which("termux-wake-lock"):
            subprocess.run(["termux-wake-lock"], check=False, timeout=3.0)
        cmd = [sys.executable, os.path.abspath(__file__), "_runner", "--env", env]
        if shadow_flag is True:
            cmd.append("--shadow")
        if safe_flag is True:
            cmd.append("--safe-mode")
        with open(log_path, "a", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(cmd, cwd=ctrl.base_dir, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(0.3)
        print(f"🚀 Demonio [{env.upper()}] iniciado (PID: {proc.pid} | Log: {log_path}).")
        return 0

    if act == "stop":
        pid = ctrl.get_running_pid(env)
        if pid is None:
            print(f"ℹ️ Sin demonio activo para [{env.upper()}].")
            return 0
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if ctrl.get_running_pid(env) is None:
                print(f"✅ Demonio [{env.upper()}] detenido con SIGTERM y cerebros sellados.")
                return 0
            time.sleep(0.2)
        return 0

    if act == "restart":
        manage_daemon(ctrl, "stop", env_mode=env)
        return manage_daemon(ctrl, "start", env_mode=env, shadow_flag=shadow_flag, safe_flag=safe_flag)

    if act == "reload":
        pids = ctrl.notify_hot_reload_signal()
        print(f"🔄 SIGHUP enviado a PID(s): {pids}" if pids else "ℹ️ Sin PID activo; recargará por mtime (<2s).")
        return 0

    if act == "logs":
        if not os.path.exists(log_path):
            print(f"ℹ️ Sin archivo de log en {log_path}.")
            return 0
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh.readlines()[-lines:]:
                print(ln.rstrip("\n"))
        return 0
    return 1


def run_interactive_menu(ctrl: SniperControlCenter) -> None:
    while True:
        print("\n" + "=" * 84)
        print("  LNM SNIPER v7.3+ — CONSOLA INTERACTIVA (TERMUX & LINUX)")
        print("=" * 84)
        print("  [1] Panel de Estado y Signos Vitales RL   [5] Configurar Parámetros Runtime")
        print("  [2] Configuraciones Predeterminadas       [6] Controlar Demonio (start/stop/logs)")
        print("  [3] Catálogo Matemático por Categoría     [7] Administrar Cerebros (Meta / Sync)")
        print("  [4] Modificar Umbral en Caliente (Set)    [0] Salir")
        print("-" * 84)
        try:
            choice = input("Opción [0-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice == "0":
            break
        elif choice == "1":
            print_status_dashboard(ctrl, input("Entorno (mainnet/testnet) [mainnet]: ").strip() or "mainnet")
        elif choice == "2":
            print_presets_info("list")
            p_in = input("Preset a aplicar (o Enter para cancelar): ").strip()
            if p_in:
                _, msg, _ = ctrl.apply_preset(p_in)
                print(msg)
        elif choice == "3":
            all_c = list(REQUIRED_CATEGORIES) + ["runtime"]
            for idx, c in enumerate(all_c, 1):
                print(f"  [{idx}] {c:<20} — {CATEGORY_TITLES.get(c, 'Runtime')}")
            pick = input("Categoría (número, nombre o texto a buscar): ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(all_c):
                print_catalog(ctrl, category_filter=all_c[int(pick) - 1])
            elif pick in all_c:
                print_catalog(ctrl, category_filter=pick)
            elif pick:
                print_catalog(ctrl, search_term=pick)
        elif choice == "4":
            k_in = input("Clave <categoria>.<parametro>: ").strip()
            if k_in and print_parameter_explanation(build_full_parameter_catalog(ctrl), k_in):
                v_in = input("Nuevo valor: ").strip()
                if v_in:
                    _, msg, _ = ctrl.set_threshold_parameter(k_in, v_in)
                    print(msg)
        elif choice == "5":
            print_catalog(ctrl, category_filter="runtime", compact=True)
            rk = input("Parámetro Runtime: ").strip().upper()
            if rk:
                rv = input("Nuevo valor: ").strip()
                if rv:
                    _, msg, _ = ctrl.set_runtime_parameter(rk, rv)
                    print(msg)
        elif choice == "6":
            act = input("Acción (start/stop/restart/reload/logs): ").strip().lower()
            env = input("Entorno [mainnet]: ").strip().lower() or "mainnet"
            if act in ("start", "stop", "restart", "reload", "logs"):
                manage_daemon(ctrl, act, env_mode=env)
        elif choice == "7":
            sats = input("Nueva meta en satoshis (o Enter para omitir): ").strip()
            if sats.isdigit():
                _, msg = ctrl.update_brain_target_sats(int(sats))
                print(msg)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sniper_cli.py", description="CLI de Control para LNM Sniper v7.3+")
    sub = parser.add_subparsers(dest="subcommand")

    p_st = sub.add_parser("status")
    p_st.add_argument("--env", default="mainnet", choices=["mainnet", "testnet"])

    p_wt = sub.add_parser("watch")
    p_wt.add_argument("--env", default="mainnet", choices=["mainnet", "testnet"])
    p_wt.add_argument("--interval", type=float, default=3.0)

    p_cat = sub.add_parser("catalog")
    p_cat.add_argument("--category", "-c", default=None)
    p_cat.add_argument("--search", "-s", default=None)
    p_cat.add_argument("--compact", action="store_true")

    p_exp = sub.add_parser("explain")
    p_exp.add_argument("param")

    p_set = sub.add_parser("set")
    p_set.add_argument("param")
    p_set.add_argument("value")

    sub.add_parser("runtime-list")
    p_rset = sub.add_parser("runtime-set")
    p_rset.add_argument("param")
    p_rset.add_argument("value")
    sub.add_parser("runtime-reset")

    p_pre = sub.add_parser("presets")
    p_pre.add_argument("action", nargs="?", default="list", choices=["list", "show", "apply"])
    p_pre.add_argument("name", nargs="?", default=None)
    p_pre.add_argument("--env", default=None, choices=["mainnet", "testnet"])

    p_dae = sub.add_parser("daemon")
    p_dae.add_argument("action", choices=["start", "stop", "restart", "reload", "logs"])
    p_dae.add_argument("--env", default=None, choices=["mainnet", "testnet"])
    p_dae.add_argument("--shadow", action="store_true", default=None)
    p_dae.add_argument("--safe-mode", action="store_true", default=None)
    p_dae.add_argument("--lines", type=int, default=40)

    p_brn = sub.add_parser("brain")
    p_brn.add_argument("action", choices=["info", "set-target", "sync", "init-clean"])
    p_brn.add_argument("--env", default="mainnet", choices=["mainnet", "testnet"])
    p_brn.add_argument("--sats", type=int, default=None)
    p_brn.add_argument("--dry-run", action="store_true")

    sub.add_parser("menu")

    p_run = sub.add_parser("_runner")
    p_run.add_argument("--env", default="mainnet")
    p_run.add_argument("--shadow", action="store_true", default=False)
    p_run.add_argument("--no-shadow", action="store_true", default=False)
    p_run.add_argument("--safe-mode", action="store_true", default=False)
    p_run.add_argument("--once", action="store_true", default=False)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    ctrl = SniperControlCenter()
    cmd = args.subcommand or "status"

    if cmd == "_runner":
        sh_ovr = True if args.shadow else (False if args.no_shadow else None)
        run_bot_with_overrides(ctrl, args.env, sh_ovr, True if args.safe_mode else None, args.once)
        return 0
    if cmd == "status":
        print_status_dashboard(ctrl, args.env)
        return 0
    if cmd == "watch":
        try:
            while True:
                os.system("clear" if os.name != "nt" else "cls")
                print_status_dashboard(ctrl, args.env)
                time.sleep(max(1.0, float(args.interval)))
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "catalog":
        print_catalog(ctrl, args.category, args.search, args.compact)
        return 0
    if cmd == "explain":
        return 0 if print_parameter_explanation(build_full_parameter_catalog(ctrl), args.param) else 1
    if cmd == "set":
        ok, msg, _ = ctrl.set_threshold_parameter(args.param, args.value)
        print(msg)
        return 0 if ok else 1
    if cmd == "runtime-list":
        print_catalog(ctrl, category_filter="runtime")
        return 0
    if cmd == "runtime-set":
        ok, msg, _ = ctrl.set_runtime_parameter(args.param, args.value)
        print(msg)
        return 0 if ok else 1
    if cmd == "runtime-reset":
        ok, msg = ctrl.reset_runtime_overrides()
        print(msg)
        return 0 if ok else 1
    if cmd == "presets":
        if args.action in ("list", "show"):
            return 0 if print_presets_info(args.name if args.action == "show" else "list") else 1
        ok, msg, _ = ctrl.apply_preset(args.name or "", env_mode=args.env)
        print(msg)
        return 0 if ok else 1
    if cmd == "daemon":
        return manage_daemon(ctrl, args.action, args.env, True if args.shadow else None, True if args.safe_mode else None, args.lines)
    if cmd == "brain":
        if args.action == "info":
            print_status_dashboard(ctrl, args.env)
            return 0
        if args.action == "set-target":
            ok, msg = ctrl.update_brain_target_sats(args.sats or 0, env_mode=args.env)
            print(msg)
            return 0 if ok else 1
        if args.action == "sync":
            from sync_brains_dual import sync_dual_brains
            return 0 if sync_dual_brains(dry_run=args.dry_run) else 1
        if args.action == "init-clean":
            cnt, srcs = ctrl.get_active_trades_count()
            if cnt > 0:
                print(f"🛑 [BLOQUEO] Posiciones activas ({', '.join(srcs)}).")
                return 1
            from init_clean_brains import create_clean_brains
            create_clean_brains()
            return 0
    if cmd == "menu":
        run_interactive_menu(ctrl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
