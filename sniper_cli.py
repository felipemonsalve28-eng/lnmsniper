#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sniper_cli.py
Consola de Control Cuantitativo, Hot-Reload Atómico y Telemetría para LNM Sniper v7.3+.
"""
from __future__ import annotations

import argparse
from collections import deque
import copy
import datetime
import difflib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple

from guardian_execution_core import AtomicBrainStore, FileLockContext
from learning_thresholds_manager import (
    DEFAULT_CONFIG_PATH,
    FALLBACK_THRESHOLDS,
    REQUIRED_CATEGORIES,
    STRUCTURAL_LOCKED_PARAMS,
    LearningThresholdsManager,
    resolve_thresholds_path_for_env,
)
from sniper_cli_catalog import (
    CATEGORY_TITLES,
    PARAM_OPERATIONAL_GUIDE,
    QUANTITATIVE_PRESETS,
    RUNTIME_PARAMETERS_SPEC,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_OVERRIDES_PATH = os.path.join(BASE_DIR, "config", "cli_runtime_overrides.json")
ACTIVE_PRESET_META_PATH = os.path.join(BASE_DIR, "config", "cli_active_preset.json")

P0_INVIOLABLE_KEYS = (
    "INITIAL_FLOOR_RATIO",
    "MILESTONE_FLOOR_RATIO",
    "MAX_DAILY_LOSS_PCT",
    "MAX_DRAWDOWN_PCT",
    "KILL_SWITCH_DD_PCT",
    "NUM_ACTIONS",
)


def get_term_width(default: int = 80) -> int:
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except Exception:
        cols = default
    return max(42, min(100, int(cols)))


def is_narrow_terminal() -> bool:
    return get_term_width() < 76


def _wrap_field(label: str, text: str, width: Optional[int] = None, indent: str = "  ") -> str:
    w = width or get_term_width()
    prefix = f"{indent}{label}: " if label else indent
    subseq = " " * len(prefix) if len(prefix) < (w - 18) else f"{indent}    "
    return textwrap.fill(
        str(text).strip(),
        width=max(36, w - 1),
        initial_indent=prefix,
        subsequent_indent=subseq,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _safe_input(prompt: str, default: str = "") -> Optional[str]:
    try:
        val = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n  ↩️ Operación cancelada.")
        return None
    s = val.strip()
    return s if s else default


def _parse_sats_input(raw: Any) -> Optional[int]:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw).strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    if s.endswith("sats"):
        s = s[:-4].strip()
    elif s.endswith("sat"):
        s = s[:-3].strip()
    mult = 1
    if s.endswith("k"):
        mult = 1000
        s = s[:-1].strip()
    elif s.endswith("m"):
        mult = 1_000_000
        s = s[:-1].strip()
    try:
        val = float(s) * mult
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return int(round(val))
    except (ValueError, TypeError):
        return None


class SniperControlCenter:
    def __init__(
        self,
        thresholds_path: str = DEFAULT_CONFIG_PATH,
        runtime_path: str = RUNTIME_OVERRIDES_PATH,
        base_dir: str = BASE_DIR,
        env_mode: Optional[str] = None,
    ) -> None:
        self.env_mode = str(env_mode).strip().lower() if env_mode else None
        abs_default = os.path.abspath(DEFAULT_CONFIG_PATH)
        if self.env_mode and os.path.abspath(thresholds_path) == abs_default:
            self.thresholds_path = os.path.abspath(resolve_thresholds_path_for_env(self.env_mode))
        else:
            self.thresholds_path = os.path.abspath(thresholds_path)
        self.runtime_path = os.path.abspath(runtime_path)
        self.base_dir = os.path.abspath(base_dir)

    def get_thresholds_path(self, env_mode: Optional[str] = None) -> str:
        if self.thresholds_path != os.path.abspath(DEFAULT_CONFIG_PATH):
            return self.thresholds_path
        return os.path.abspath(resolve_thresholds_path_for_env(env_mode or self.env_mode))

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

    def load_thresholds_tree(self, env_mode: Optional[str] = None) -> Dict[str, Dict[str, Dict[str, Any]]]:
        data = self._safe_read_json(self.get_thresholds_path(env_mode))
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

    def _telemetry_file_path(self, env_mode: str = "mainnet") -> str:
        env = str(env_mode or "mainnet").strip().lower()
        primary = os.path.join(self.base_dir, f"telemetry_live_{env}.json")
        if env == "mainnet" and not os.path.exists(primary):
            legacy = os.path.join(self.base_dir, "telemetry_live.json")
            if os.path.exists(legacy):
                return legacy
        return primary

    def _config_live_path(self, env_mode: str = "mainnet") -> str:
        env = str(env_mode or "mainnet").strip().lower()
        return os.path.join(self.base_dir, "config", f"config_live_{env}.json")

    def get_active_trades_count(self, env_mode: Optional[str] = None) -> Tuple[int, List[str]]:
        envs = [env_mode.lower()] if env_mode else ["mainnet", "testnet"]
        total_active = 0
        sources: List[str] = []
        for env in envs:
            tel_data = self._safe_read_json(self._telemetry_file_path(env))
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
        return os.path.join(self.base_dir, f"sniper_{env_mode.lower()}.pid")

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.path.exists(f"/proc/{pid}"):
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def check_daemon_status(self, env_mode: str = "mainnet") -> Tuple[bool, Optional[int], str]:
        """
        Verifica Liveness por Timestamp (snapshot_ts <= 15.0s) + PID bajo fcntl.LOCK_SH
        en telemetry_live_{env}.json y sniper_{env}.pid.
        """
        env = str(env_mode or "mainnet").strip().lower()
        tel_path = self._telemetry_file_path(env)
        tel_data = self._safe_read_json(tel_path)
        now_utc = time.time()
        snapshot_ts = 0.0
        tel_pid: Optional[int] = None

        if isinstance(tel_data, dict):
            raw_snap_ts = tel_data.get("snapshot_ts")
            if isinstance(raw_snap_ts, (int, float)) and float(raw_snap_ts) > 0.0:
                snapshot_ts = float(raw_snap_ts)
            else:
                raw_iso = str(tel_data.get("timestamp", "")).strip()
                if raw_iso:
                    try:
                        dt = datetime.datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
                        snapshot_ts = dt.timestamp()
                    except Exception:
                        snapshot_ts = 0.0
            raw_pid = tel_data.get("pid")
            if isinstance(raw_pid, int) and raw_pid > 0:
                tel_pid = raw_pid

        file_pid: Optional[int] = None
        for p_cand in (self._pid_file_path(env), os.path.join(self.base_dir, f".sniper_{env}.pid")):
            if os.path.exists(p_cand):
                try:
                    with FileLockContext(p_cand, exclusive=False, timeout=1.0, raise_on_timeout=False):
                        with open(p_cand, "r", encoding="utf-8") as fh:
                            file_pid = int(fh.read().strip())
                    break
                except (ValueError, OSError):
                    pass

        if file_pid and self._is_pid_alive(file_pid):
            candidate_pid = file_pid
        elif tel_pid and self._is_pid_alive(tel_pid):
            candidate_pid = tel_pid
        else:
            candidate_pid = file_pid or tel_pid
        proc_alive = bool(candidate_pid and self._is_pid_alive(candidate_pid))
        heartbeat_fresh = (snapshot_ts > 0.0) and ((now_utc - snapshot_ts) <= 15.0)

        if proc_alive or (heartbeat_fresh and candidate_pid is None):
            pid_label = str(candidate_pid) if candidate_pid else "LIVE"
            return True, candidate_pid, f"🟢 ACTIVO / OK (PID: {pid_label})"
        return False, None, "⚪ DETENIDO / EXTERNO"

    def get_running_pid(self, env_mode: str = "mainnet") -> Optional[int]:
        is_alive, pid, _ = self.check_daemon_status(env_mode)
        return pid if is_alive else None

    def notify_hot_reload_signal(self, env_mode: Optional[str] = None) -> List[int]:
        signaled_pids: List[int] = []
        sig_hup = getattr(signal, "SIGHUP", None)
        if sig_hup is None:
            return signaled_pids
        eff_env = str(env_mode or self.env_mode or "").strip().lower()
        envs = [eff_env] if eff_env in ("mainnet", "testnet") else ["mainnet", "testnet"]
        for env in envs:
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

    def resolve_threshold_key(self, query: str) -> Tuple[Optional[str], List[str]]:
        q = str(query or "").strip().lower()
        if not q:
            return None, []
        tree = self.load_thresholds_tree()
        all_keys: List[str] = []
        short_map: Dict[str, List[str]] = {}
        for cat, params in tree.items():
            for p_name in params.keys():
                fk = f"{cat}.{p_name}"
                all_keys.append(fk)
                short_map.setdefault(p_name.lower(), []).append(fk)

        for fk in all_keys:
            if fk.lower() == q:
                return fk, [fk]

        if q in short_map and len(short_map[q]) == 1:
            only = short_map[q][0]
            return only, [only]
        if q in short_map and len(short_map[q]) > 1:
            return None, sorted(short_map[q])

        substr_matches = [fk for fk in all_keys if q in fk.lower()]
        if len(substr_matches) == 1 and "." not in q and q in [fk.split(".", 1)[1].lower() for fk in substr_matches]:
            return substr_matches[0], [substr_matches[0]]

        close_full = difflib.get_close_matches(q, all_keys, n=5, cutoff=0.45)
        close_short_names = difflib.get_close_matches(q, list(short_map.keys()), n=5, cutoff=0.5)
        close_from_short: List[str] = []
        for s_name in close_short_names:
            close_from_short.extend(short_map[s_name])

        suggestions: List[str] = []
        for candidate in substr_matches + close_from_short + close_full:
            if candidate not in suggestions:
                suggestions.append(candidate)
        return None, suggestions[:6]

    def set_threshold_parameter(
        self, full_key: str, raw_value: Any, env_check: Optional[str] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if isinstance(raw_value, bool):
            return False, "❌ Valor booleano inválido para un umbral numérico.", None
        if isinstance(raw_value, str) and raw_value.strip().lower() in ("true", "false"):
            return False, "❌ Valor booleano inválido para un umbral numérico.", None

        eff_env = str(env_check or self.env_mode or "").strip().lower() or None
        target_path = self.get_thresholds_path(eff_env)
        resolved_key, suggestions = self.resolve_threshold_key(full_key)
        if resolved_key is None:
            sug_txt = f" ¿Quisiste decir: {', '.join(suggestions)}?" if suggestions else " Usa 'catalog' para listar claves."
            return False, f"❌ Parámetro '{full_key}' no encontrado o ambiguo.{sug_txt}", None

        category, param_name = resolved_key.split(".", 1)
        tree = self.load_thresholds_tree(eff_env)
        param_obj = tree[category][param_name]
        old_val, min_lim, max_lim = param_obj["value"], param_obj["min_limit"], param_obj["max_limit"]
        is_int = isinstance(old_val, int) and not isinstance(old_val, bool) and isinstance(min_lim, int) and isinstance(max_lim, int)
        try:
            num_val = float(raw_value)
            if num_val != num_val or num_val in (float("inf"), float("-inf")):
                return False, f"❌ Valor numérico inválido: {raw_value!r}.", None
            coerced_val: Any = int(round(num_val)) if is_int else round(float(num_val), 6)
        except (ValueError, TypeError):
            return False, f"❌ Valor no numérico '{raw_value}' para '{resolved_key}'. Rango permitido: [{min_lim}, {max_lim}].", None

        if float(coerced_val) < float(min_lim) or float(coerced_val) > float(max_lim):
            return (
                False,
                f"❌ Valor fuera de rango en '{resolved_key}': {coerced_val} no pertenece a [{min_lim}, {max_lim}] ({param_obj['unit']}).",
                None,
            )

        if self.is_param_structurally_locked(category, param_name, param_obj) and float(coerced_val) != float(old_val):
            active_cnt, active_srcs = self.get_active_trades_count(eff_env)
            if active_cnt > 0:
                return (
                    False,
                    f"🔒 [BLOQUEO ESTRUCTURAL ACTIVO] '{resolved_key}' no puede mutar con posiciones abiertas ({', '.join(active_srcs)}).",
                    None,
                )

        tree[category][param_name]["value"] = coerced_val
        is_valid, reason, validated_tree = LearningThresholdsManager.validate_schema_and_limits(tree)
        if not is_valid or validated_tree is None:
            return False, f"❌ Rechazado por LearningThresholdsManager: {reason}", None

        self._atomic_write_json(target_path, validated_tree, create_backup=True)
        signal_env = eff_env if eff_env in ("mainnet", "testnet") else "mainnet"
        pids = self.notify_hot_reload_signal(env_mode=signal_env)
        pid_msg = f" | SIGHUP enviado a [{signal_env.upper()}] PID(s): {pids}" if pids else f" | Hot-Reload [{signal_env.upper()}] vía mtime (<2s)"
        return (
            True,
            f"✅ Parámetro '{resolved_key}' actualizado ({os.path.basename(target_path)}): {old_val} ➔ {coerced_val} [{param_obj['unit']}]{pid_msg}",
            {"key": resolved_key, "old_value": old_val, "new_value": coerced_val, "unit": param_obj["unit"], "signaled_pids": pids, "thresholds_path": target_path},
        )

    def validate_runtime_param(self, key: str, raw_val: Any) -> Tuple[bool, Any, str]:
        k = str(key or "").strip().upper()
        if k.startswith("RUNTIME."):
            k = k.split(".", 1)[1].strip()
        if k in P0_INVIOLABLE_KEYS:
            return False, None, f"🛑 [PROTECCIÓN P0 INVIOLABLE] '{k}' pertenece a HardFloorGuardian / 9 acciones y es intocable."
        if k not in RUNTIME_PARAMETERS_SPEC:
            close = difflib.get_close_matches(k, list(RUNTIME_PARAMETERS_SPEC.keys()), n=4, cutoff=0.45)
            sug = f" ¿Quisiste decir: {', '.join(close)}?" if close else ""
            return False, None, f"❌ Parámetro de runtime desconocido '{k}'.{sug}"

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
            return False, None, f"❌ Booleano inválido para '{k}'. Usa true/false o 1/0."
        if ptype == "str":
            s = str(raw_val).strip().lower()
            if "allowed" in spec and s not in spec["allowed"]:
                return False, None, f"❌ Valor '{s}' no permitido en '{k}'. Opciones: {spec['allowed']}."
            return True, s, "OK"
        if ptype in ("int", "float"):
            if isinstance(raw_val, bool):
                return False, None, f"❌ Valor booleano inválido para '{k}'."
            try:
                fval = float(raw_val)
                if fval != fval or fval in (float("inf"), float("-inf")):
                    raise ValueError("NaN")
                val: Any = int(round(fval)) if ptype == "int" else round(fval, 5)
            except (ValueError, TypeError):
                return False, None, f"❌ Número inválido para '{k}': {raw_val!r}."
            if float(val) < float(spec["min_limit"]) or float(val) > float(spec["max_limit"]):
                return False, None, f"❌ '{k}' fuera de rango P0: {val} no está en [{spec['min_limit']}, {spec['max_limit']}]."
            return True, val, "OK"
        return False, None, "Tipo no soportado."

    def set_runtime_parameter(self, key: str, raw_val: Any) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        k = str(key or "").strip().upper()
        if k.startswith("RUNTIME."):
            k = k.split(".", 1)[1].strip()
        ok, coerced, reason = self.validate_runtime_param(k, raw_val)
        if not ok:
            return False, reason, None
        current = self.load_runtime_overrides()
        old_val = current.get(k)
        if k == "MIN_LEVERAGE" and int(coerced) > int(current.get("MAX_LEVERAGE", 85)):
            return False, "❌ MIN_LEVERAGE no puede superar a MAX_LEVERAGE.", None
        if k == "MAX_LEVERAGE" and int(coerced) < int(current.get("MIN_LEVERAGE", 15)):
            return False, "❌ MAX_LEVERAGE no puede ser menor que MIN_LEVERAGE.", None
        current[k] = coerced
        self._atomic_write_json(self.runtime_path, current, create_backup=True)
        unit = RUNTIME_PARAMETERS_SPEC[k]["unit"]
        return (
            True,
            f"✅ Parámetro táctico '{k}' guardado: {old_val} ➔ {coerced} [{unit}] (aplica en el próximo arranque/restart).",
            {"key": k, "old_value": old_val, "new_value": coerced, "unit": unit},
        )

    def reset_runtime_overrides(self) -> Tuple[bool, str]:
        defaults = {k: spec["default"] for k, spec in RUNTIME_PARAMETERS_SPEC.items()}
        self._atomic_write_json(self.runtime_path, defaults, create_backup=True)
        return True, "✅ Parámetros tácticos de runtime restaurados a valores de fábrica v7.3+."

    def apply_preset(self, preset_name: str, env_mode: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(preset_name or "").strip().lower()
        if name not in QUANTITATIVE_PRESETS:
            close = difflib.get_close_matches(name, list(QUANTITATIVE_PRESETS.keys()), n=3, cutoff=0.4)
            sug = f" ¿Quisiste decir: {', '.join(close)}?" if close else ""
            return False, f"❌ Preset '{name}' desconocido.{sug} Disponibles: {', '.join(QUANTITATIVE_PRESETS.keys())}.", {}

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

        active_cnt, active_srcs = self.get_active_trades_count(effective_env)
        target_thr_path = self.get_thresholds_path(effective_env)
        tree = self.load_thresholds_tree(effective_env)
        combined_thr = dict(QUANTITATIVE_PRESETS["baseline_v73"]["thresholds"])
        combined_thr.update(preset.get("thresholds", {}))

        applied_thr: List[Tuple[str, Any, Any]] = []
        skipped_locked: List[str] = []
        for full_k, target_v in combined_thr.items():
            cat, p_name = full_k.split(".", 1)
            if cat in tree and p_name in tree[cat]:
                p_obj = tree[cat][p_name]
                prev_v = p_obj["value"]
                is_int = isinstance(prev_v, int) and not isinstance(prev_v, bool)
                new_v = int(round(float(target_v))) if is_int else round(float(target_v), 6)
                if self.is_param_structurally_locked(cat, p_name, p_obj) and active_cnt > 0:
                    if prev_v != new_v:
                        skipped_locked.append(full_k)
                    continue
                if prev_v != new_v:
                    applied_thr.append((full_k, prev_v, new_v))
                tree[cat][p_name]["value"] = new_v

        ok, reason, validated_tree = LearningThresholdsManager.validate_schema_and_limits(tree)
        if not ok or validated_tree is None:
            return False, f"❌ Error validando preset '{name}': {reason}", {}
        self._atomic_write_json(target_thr_path, validated_tree, create_backup=True)

        base_rt = {k: spec["default"] for k, spec in RUNTIME_PARAMETERS_SPEC.items()}
        base_rt["ENV_MODE"] = effective_env if env_mode else runtime_cfg.get("ENV_MODE", "mainnet")
        base_rt["SHADOW_MODE"] = runtime_cfg.get("SHADOW_MODE", False)
        for rk, rv in preset.get("runtime", {}).items():
            if rk in RUNTIME_PARAMETERS_SPEC:
                ok_r, coerced_r, _ = self.validate_runtime_param(rk, rv)
                if ok_r:
                    base_rt[rk] = coerced_r

        applied_rt = [(rk, runtime_cfg.get(rk), rv) for rk, rv in base_rt.items() if runtime_cfg.get(rk) != rv]
        self._atomic_write_json(self.runtime_path, base_rt, create_backup=True)
        self._atomic_write_json(
            os.path.join(self.base_dir, "config", "cli_active_preset.json"),
            {
                "active_preset": name,
                "title": preset["title"],
                "applied_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            create_backup=False,
        )
        pids = self.notify_hot_reload_signal(env_mode=effective_env)
        lock_note = ""
        if active_cnt > 0 and skipped_locked:
            lock_note = f" | 🔒 {len(skipped_locked)} omitidos por posiciones abiertas ({', '.join(active_srcs)})"
        return (
            True,
            f"✅ Perfil '{name}' ({preset['title']}) aplicado ({len(applied_thr)} umbrales Hot-Reload, {len(applied_rt)} runtime){lock_note}.",
            {
                "preset": name,
                "thresholds_diff": applied_thr,
                "runtime_diff": applied_rt,
                "skipped_locked": skipped_locked,
                "signaled_pids": pids,
            },
        )

    def update_brain_target_sats(self, target_sats: int, env_mode: str = "mainnet") -> Tuple[bool, str]:
        env = env_mode.strip().lower()
        parsed = _parse_sats_input(target_sats)
        if parsed is None or parsed < 10000:
            return False, "❌ La meta en satoshis debe ser un entero >= 10,000 sats (ej. 350000 o 350k)."
        target_val = int(parsed)
        active_cnt, active_srcs = self.get_active_trades_count(env)
        if active_cnt > 0:
            return False, f"⏸️ [BLOQUEO DE INTEGRIDAD] Hay posiciones abiertas ({', '.join(active_srcs)})."

        brain_file = "q_brain_advanced_lnm.json" if env == "mainnet" else "q_brain_testnet_lnm.json"
        brain_path = os.path.join(self.base_dir, brain_file)
        if not os.path.exists(brain_path):
            return False, f"❌ Archivo no encontrado: {brain_path}"

        store = AtomicBrainStore(brain_path)
        data = store.load()
        if not isinstance(data, dict) or "q_table" not in data:
            return False, f"❌ Fallo verificando {brain_file} bajo strict_sha=True."
        default_target = int(RUNTIME_PARAMETERS_SPEC["INITIAL_TARGET_SATS"]["default"])
        old_target = int(data.get("current_target", default_target))
        data["current_target"] = target_val
        digest = store.save(data)
        return True, f"✅ Meta en {brain_file} actualizada: {old_target:,} ➔ {target_val:,} sats (SHA-256: {digest[:16]}...)."

    def fetch_live_account_snapshot(self, env_mode: str = "mainnet") -> Dict[str, Any]:
        """Consulta en vivo /account, /futures/isolated/trades/running y /futures/ticker en LN Markets API v3."""
        env = str(env_mode or "mainnet").strip().lower()
        try:
            from lnm_sniper_v6_advanced import (
                LNMarketsClient,
                compute_account_equity,
                load_saved_credentials,
            )
            key, secret, passphrase = load_saved_credentials(env)
            if not key or not secret or not passphrase:
                return {
                    "ok": False,
                    "env": env,
                    "error": f"Faltan credenciales de '{env}' en .env o variables de entorno.",
                }
            base_url = (
                os.getenv("LNM_TESTNET_BASE_URL", "https://api.signet.lnmarkets.com/v3")
                if env == "testnet"
                else os.getenv("LNM_MAINNET_BASE_URL", "https://api.lnmarkets.com/v3")
            )
            client = LNMarketsClient(key, secret, passphrase, base_url=base_url)
            user = client.get_user(force_refresh=True)
            running = client.get_running_trades(force_refresh=True)
            btc_price = 0.0
            try:
                btc_price = float(client.get_ticker_price() or 0.0)
            except Exception:
                btc_price = 0.0
            bal = int(user.get("balance", 0)) if isinstance(user, dict) else 0
            eq, locked_margin, floating_pl = compute_account_equity(bal, running or [])
            return {
                "ok": True,
                "env": env,
                "balance_sats": bal,
                "equity_sats": int(eq),
                "locked_margin_sats": int(locked_margin),
                "floating_pl_sats": int(floating_pl),
                "open_positions": list(running or []),
                "btc_price": btc_price,
                "error": "",
            }
        except Exception as exc:
            msg = str(exc).strip().replace("\n", " ")[:120]
            return {"ok": False, "env": env, "error": f"Sin conexión con LN Markets ({msg})"}


def build_full_parameter_catalog(ctrl: SniperControlCenter) -> Dict[str, Dict[str, Any]]:
    tree = ctrl.load_thresholds_tree()
    catalog: Dict[str, Dict[str, Any]] = {}
    for cat in REQUIRED_CATEGORIES:
        for p_name, p_obj in tree.get(cat, {}).items():
            full_key = f"{cat}.{p_name}"
            guide = PARAM_OPERATIONAL_GUIDE.get(full_key, {})
            catalog[full_key] = {
                "key": full_key,
                "category": cat,
                "name": p_name,
                "group": cat,
                "scope": "HOT_RELOAD",
                "value": p_obj["value"],
                "unit": p_obj["unit"],
                "min_limit": p_obj["min_limit"],
                "max_limit": p_obj["max_limit"],
                "structural_lock": ctrl.is_param_structurally_locked(cat, p_name, p_obj),
                "description": p_obj["description"],
                "math_role": guide.get("math_role", f"Umbral dinámico de '{cat}'."),
                "impact_up": guide.get("impact_up", "SUBIR (↑): Incrementa el rigor del umbral."),
                "impact_down": guide.get("impact_down", "BAJAR (↓): Reduce la magnitud del umbral."),
            }
    rt_values = ctrl.load_runtime_overrides()
    for rk, spec in RUNTIME_PARAMETERS_SPEC.items():
        full_rt_key = f"runtime.{rk}"
        catalog[full_rt_key] = {
            "key": full_rt_key,
            "category": "runtime",
            "name": rk,
            "group": "runtime",
            "scope": "RUNTIME_BOOT",
            "value": rt_values.get(rk, spec["default"]),
            "unit": spec["unit"],
            "min_limit": spec.get("min_limit", "-"),
            "max_limit": spec.get("max_limit", "-"),
            "structural_lock": False,
            "description": spec["description"],
            "math_role": spec["math_role"],
            "impact_up": spec["impact_up"],
            "impact_down": spec["impact_down"],
        }
    return catalog


def print_status_dashboard(ctrl: SniperControlCenter, env_mode: str = "mainnet", fetch_live: bool = True) -> None:
    env = env_mode.strip().lower()
    w = get_term_width()
    narrow = is_narrow_terminal()
    sep_eq = "=" * w
    sep_ln = "-" * w

    tel_path = ctrl._telemetry_file_path(env)
    tel_file = os.path.basename(tel_path)
    brain_file = "q_brain_advanced_lnm.json" if env == "mainnet" else "q_brain_testnet_lnm.json"
    hedge_file = "hedge_state_mainnet.json" if env == "mainnet" else "hedge_state_testnet.json"

    _, pid, daemon_badge = ctrl.check_daemon_status(env)
    preset_meta = ctrl._safe_read_json(ACTIVE_PRESET_META_PATH) or {}
    rt_cfg = ctrl.load_runtime_overrides()
    active_preset = preset_meta.get("active_preset", "baseline_v73")

    print(sep_eq)
    print(f"  LNM SNIPER v7.3+ — PANEL DE CONTROL [{env.upper()}]")
    print(sep_eq)
    if narrow:
        print(f"  Demonio : {daemon_badge}")
        print(f"  Preset  : {active_preset}")
        print(f"  Boot Cfg: ENV={rt_cfg['ENV_MODE']} | SHADOW={rt_cfg['SHADOW_MODE']} | SAFE={rt_cfg['SAFE_MODE']}")
        print(f"  Límites : MaxPos={rt_cfg['MAX_CONCURRENT_TRADES']} | MaxLev={rt_cfg['MAX_LEVERAGE']}x | Hedge={rt_cfg['ENABLE_HYBRID_HEDGE']}")
    else:
        print(f"  Demonio CLI    : {daemon_badge} | Preset: {active_preset}")
        print(f"  Overrides Boot : ENV={rt_cfg['ENV_MODE']} | SHADOW={rt_cfg['SHADOW_MODE']} | SAFE={rt_cfg['SAFE_MODE']} | HEDGE={rt_cfg['ENABLE_HYBRID_HEDGE']} | MaxPos={rt_cfg['MAX_CONCURRENT_TRADES']} | MaxLev={rt_cfg['MAX_LEVERAGE']}x")

    if fetch_live:
        print(sep_ln)
        live = ctrl.fetch_live_account_snapshot(env)
        if live.get("ok"):
            bal_l = int(live.get("balance_sats", 0))
            eq_l = int(live.get("equity_sats", 0))
            mrg_l = int(live.get("locked_margin_sats", 0))
            fpl_l = int(live.get("floating_pl_sats", 0))
            pos_l = live.get("open_positions", [])
            btc_p = float(live.get("btc_price", 0.0))
            btc_tag = f" | BTC: ${btc_p:,.1f}" if btc_p > 0 else ""
            if narrow:
                print(f"  Cuenta VIVO [{env.upper()}]: Bal={bal_l:,} | Eq={eq_l:,}")
                print(f"  Margen={mrg_l:,} | Flot={fpl_l:+,} | Pos={len(pos_l)}{btc_tag}")
            else:
                print(f"  Cuenta VIVO [{env.upper()}] : Bal={bal_l:,} sats | Eq={eq_l:,} sats | Margen={mrg_l:,} | Flot={fpl_l:+,} | Pos={len(pos_l)}{btc_tag}")
            for t in pos_l[:5]:
                t_side = str(t.get("side", "?")).upper()
                t_mrg = int(t.get("margin") or 0)
                t_lev = float(t.get("leverage") or 0.0)
                t_pl = int(t.get("pl") or 0)
                t_ent = float(t.get("price") or t.get("entryPrice") or 0.0)
                t_sl = int(round(float(t.get("stoploss") or 0.0)))
                t_tp = int(round(float(t.get("takeprofit") or 0.0)))
                print(f"    • {t_side} {t_mrg:,}s@{t_lev:.0f}x | Ent:${t_ent:,.1f} | PnL:{t_pl:+,}s | SL:{t_sl} TP:{t_tp}")
        else:
            print(_wrap_field("Cuenta VIVO", f"⚠️ {live.get('error', 'No disponible')} (usando snapshot local).", w))

    print(sep_ln)
    brain_path = os.path.join(ctrl.base_dir, brain_file)
    if not os.path.exists(brain_path):
        print(f"  Cerebro Activo : ⚠️ {brain_file} [No inicializado o ausente]")
    else:
        sha_ok, b_data = AtomicBrainStore(brain_path).verify_file(brain_path, strict_sha=True)
        if not isinstance(b_data, dict):
            print(f"  Cerebro Activo : ⚠️ {brain_file} [Ilegible o fallo de integridad SHA-256]")
        else:
            sha_badge = "SHA-256 OK" if sha_ok else "⚠️ ALERTA SHA"
            q_len = len(b_data.get("q_table", {}))
            trained = int(b_data.get("total_trained", 0))
            eps = float(b_data.get("epsilon", 0.0))
            act_cnt = len(b_data.get("active_trades", {}))
            pnl = int(b_data.get("realized_net_profit_sats", 0))
            fees = int(b_data.get("total_fees_paid_sats", 0))
            floor_s = int(b_data.get("locked_floor", 0))
            target_s = int(b_data.get("current_target", 300000))
            if narrow:
                print(f"  Cerebro : {brain_file} [{sha_badge}]")
                print(f"  Política: {q_len} estados | Cierres: {trained} | Eps: {eps:.4f} | Pos: {act_cnt}")
                print(f"  PnL Neto: {pnl:+,} sats | Fees: {fees:,} sats")
                print(f"  Piso P0 : {floor_s:,} sats | Meta: {target_s:,} sats")
            else:
                print(f"  Cerebro Activo : {brain_file} [{sha_badge}]")
                print(f"  Política Q     : {q_len} estados | Cierres: {trained} | Eps: {eps:.4f} | Activas: {act_cnt}")
                print(f"  Contabilidad   : PnL Neto: {pnl:+,} sats | Fees: {fees:,} | Piso: {floor_s:,} | Meta: {target_s:,} sats")

    hedge_path = os.path.join(ctrl.base_dir, hedge_file)
    if not os.path.exists(hedge_path):
        print(f"  Hedge Engine   : ⚠️ {hedge_file} [No inicializado o sin coberturas]")
    else:
        h_ok, h_data = AtomicBrainStore(hedge_path, required_key="pairs").verify_file(hedge_path, strict_sha=True)
        if not isinstance(h_data, dict):
            print(f"  Hedge Engine   : ⚠️ {hedge_file} [Ilegible o fallo SHA-256]")
        else:
            hm = h_data.get("hedge_metrics", {})
            saved_sats = int(hm.get("net_sats_saved_vs_sl", 0))
            if narrow:
                print(f"  Hedge Q : {hedge_file} [{'OK' if h_ok else 'WARN'}] | Pares: {len(h_data.get('pairs', {}))}")
                print(f"            Estados: {len(h_data.get('hedge_q_table', {}))} | Ahorro vs SL: {saved_sats:+,} sats")
            else:
                print(f"  Hedge Engine   : {hedge_file} [{'OK' if h_ok else 'WARN'}] | Pares: {len(h_data.get('pairs', {}))} | Estados Hedge Q: {len(h_data.get('hedge_q_table', {}))} | Ahorrado vs SL: {saved_sats:+,} sats")

    tel = ctrl._safe_read_json(tel_path)
    sb_snap = None
    if isinstance(tel, dict) and isinstance(tel.get("side_streak_brake"), dict):
        sb_snap = tel.get("side_streak_brake")
    else:
        try:
            from side_streak_brake import SideStreakBrakeStore
            _sb_probe = SideStreakBrakeStore(env_mode=env, persist_to_disk=False)
            if os.path.exists(_sb_probe.filepath):
                _sb_disk = SideStreakBrakeStore(env_mode=env, filepath=_sb_probe.filepath, persist_to_disk=True)
                sb_snap = _sb_disk.get_telemetry_snapshot()
            else:
                sb_snap = _sb_probe.get_telemetry_snapshot()
        except Exception:
            sb_snap = None

    if isinstance(sb_snap, dict):
        st_b = int((sb_snap.get("side_loss_streak") or {}).get("b", 0))
        st_s = int((sb_snap.get("side_loss_streak") or {}).get("s", 0))
        pa_b = bool((sb_snap.get("side_paused") or {}).get("b", False))
        pa_s = bool((sb_snap.get("side_paused") or {}).get("s", False))
        rm_b = float((sb_snap.get("side_remaining_sec") or {}).get("b", 0.0))
        rm_s = float((sb_snap.get("side_remaining_sec") or {}).get("s", 0.0))
        tp_b = int((sb_snap.get("total_pauses_triggered") or {}).get("b", 0))
        tp_s = int((sb_snap.get("total_pauses_triggered") or {}).get("s", 0))
        sb_sha = "SHA-256 OK" if sb_snap.get("sha256_valid", True) else "⚠️ ALERTA SHA"
        b_status = f"PAUSADO 24H ({rm_b/3600.0:.1f}h rest)" if pa_b else "ACTIVO 1.00x"
        s_status = f"PAUSADO 24H ({rm_s/3600.0:.1f}h rest)" if pa_s else "ACTIVO 1.00x (d_SL>0.65%)"
        if narrow:
            print(f"  Política: C1_STREAK3_24H_INV [DETERMINISTIC | E2_FE_OFF | {sb_sha}]")
            print(f"  Freno L : Racha={st_b}/3 | {b_status} (Pausas:{tp_b})")
            print(f"  Freno S : Racha={st_s}/3 | {s_status} (Pausas:{tp_s})")
        else:
            print(
                f"  Freno C1_24H   : [C1_STREAK3_24H_INV | {sb_sha}] "
                f"LONG: Racha {st_b}/3 ({b_status}, Tot:{tp_b}) | "
                f"SHORT: Racha {st_s}/3 ({s_status}, Tot:{tp_s})"
            )
    print(sep_ln)
    if not isinstance(tel, dict):
        print(f"  Telemetría Vivo: ℹ️ Sin {tel_file} aún (se genera automáticamente con el bot activo).")
    else:
        rl = tel.get("rl_metrics", {})
        adj = rl.get("autonomous_adjustments", {})
        if narrow:
            print(f"  Snapshot RL: {tel.get('timestamp', '-')}")
            print(f"  Snap Cap   : Bal={int(tel.get('balance_sats', 0)):,} | Eq={int(tel.get('equity_sats', 0)):,} | Pos={int(tel.get('active_trades', 0))}")
            print(f"  Salud RL   : Policy={rl.get('policy_health', '-')} | Guard={rl.get('learning_guard_status', '-')}")
            print(f"  Ajuste Aut : ExpMin={adj.get('effective_min_expectancy_ratio', 3.85):.2f}x | Trail={adj.get('dynamic_trailing_multiplier', 1.0):.2f}x")
            print(f"  1.TD-Error : avg50={rl.get('rolling_td_error_avg', 0.0):.4f} [{rl.get('td_error_status', '-')}]")
            print(f"  2.Q-Bias   : {rl.get('q_bias_sats', 0.0):+.2f}s (std={rl.get('q_bias_std_sats', 0.0):.1f}) [{rl.get('q_bias_status', '-')}]")
            print(f"  3.FER      : Roll={rl.get('fee_efficiency_ratio', 0.0):.2f} | Glob={rl.get('global_fee_efficiency_ratio', 0.0):.2f} [{rl.get('fer_status', '-')}]")
            print(f"  4.Entropía : H={rl.get('normalized_policy_entropy', 0.0):.3f} | Dom={rl.get('dominant_action', '-')} ({float(rl.get('dominant_action_freq', 0.0))*100:.0f}%)")
            print(f"  5.MFE/MAE  : MFE={float(rl.get('mfe_capture_avg', 0.0))*100:.1f}% | MAE/Mrg={float(rl.get('mae_margin_ratio_avg', 0.0))*100:.1f}%")
        else:
            print(f"  Snapshot RL    : {tel.get('timestamp', '-')} | Bal: {int(tel.get('balance_sats', 0)):,} sats | Eq: {int(tel.get('equity_sats', 0)):,} sats | Activas: {int(tel.get('active_trades', 0))}")
            print(f"  Salud / Guard  : Policy={rl.get('policy_health', '-')} | Guard={rl.get('learning_guard_status', '-')} | ExpEfectivo={adj.get('effective_min_expectancy_ratio', 3.85):.2f}x | TrailMult={adj.get('dynamic_trailing_multiplier', 1.0):.2f}x")
            print(f"    1. TD-Error  : avg50={rl.get('rolling_td_error_avg', 0.0):.4f} (last={rl.get('last_abs_td_error', 0.0):.4f}) -> [{rl.get('td_error_status', '-')}]")
            print(f"    2. Q-Bias    : {rl.get('q_bias_sats', 0.0):+.2f} sats (std={rl.get('q_bias_std_sats', 0.0):.2f}) -> [{rl.get('q_bias_status', '-')}]")
            print(f"    3. FER       : Rolling={rl.get('fee_efficiency_ratio', 0.0):.2f} | Global={rl.get('global_fee_efficiency_ratio', 0.0):.2f} -> [{rl.get('fer_status', '-')}]")
            print(f"    4. Entropía  : H_norm={rl.get('normalized_policy_entropy', 0.0):.3f} | Dom={rl.get('dominant_action', '-')} ({float(rl.get('dominant_action_freq', 0.0))*100:.1f}%)")
            print(f"    5. MFE / MAE : Captura MFE={float(rl.get('mfe_capture_avg', 0.0))*100:.1f}% | MAE/Margen={float(rl.get('mae_margin_ratio_avg', 0.0))*100:.1f}%")
    print(sep_ln)
    print(_wrap_field("Guía Rápida", "FER >= 1.2 indica ganancias limpias sobre comisiones | Q-Bias cercano a 0 indica proyecciones realistas | TD-Error bajo confirma convergencia Q | Piso P0 protege 75% inicial y 88% en hitos.", w))
    print(sep_eq)


def print_parameter_explanation(catalog: Dict[str, Dict[str, Any]], query_key: str) -> bool:
    q = str(query_key or "").strip()
    if q not in catalog:
        matches = [
            k for k in catalog
            if k.lower() == q.lower() or k.lower().endswith(f".{q.lower()}")
        ]
        if len(matches) == 1:
            q = matches[0]
        else:
            substr = [k for k in catalog if q.lower() in k.lower()]
            close = difflib.get_close_matches(q.lower(), list(catalog.keys()), n=5, cutoff=0.45)
            sug = substr[:5] if substr else close
            sug_txt = f" ¿Quisiste decir: {', '.join(sug)}?" if sug else ""
            print(f"❌ [ERROR] Parámetro '{query_key}' no encontrado.{sug_txt}")
            return False

    info = catalog[q]
    w = get_term_width()
    sep_eq = "=" * w
    sep_ln = "-" * w
    scope_desc = (
        "HOT_RELOAD (Se aplica en vivo en <2s sin reiniciar el bot)"
        if info["scope"] == "HOT_RELOAD"
        else "RUNTIME_BOOT (Se aplica al iniciar o reiniciar el demonio)"
    )
    lock_desc = (
        "🔒 SÍ (No puede cambiarse mientras haya posiciones abiertas)"
        if info["structural_lock"]
        else "🟢 NO (Modificable en cualquier momento)"
    )
    cmd_example = (
        f"python3 sniper_cli.py set {q} {info['value']}"
        if info["scope"] == "HOT_RELOAD"
        else f"python3 sniper_cli.py runtime-set {info.get('name', q.replace('runtime.', ''))} {info['value']}"
    )

    print(sep_eq)
    print(f"  FICHA MATEMÁTICA Y OPERATIVA: {q}")
    print(sep_eq)
    print(_wrap_field("Ámbito", scope_desc, w))
    print(_wrap_field("Valor Actual", f"{info['value']} [{info['unit']}]  |  Rango permitido: [{info['min_limit']}, {info['max_limit']}]", w))
    print(_wrap_field("Candado Pos. Abiertas", lock_desc, w))
    print(sep_ln)
    print(_wrap_field("¿Qué es y qué hace?", info["description"], w))
    print(_wrap_field("Rol Matemático", info["math_role"], w))
    print(_wrap_field("Efecto al SUBIR (↑)", info["impact_up"], w))
    print(_wrap_field("Efecto al BAJAR (↓)", info["impact_down"], w))
    print(sep_ln)
    print(_wrap_field("Comando para ajustar", cmd_example, w))
    print(sep_eq)
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

    w = get_term_width()
    narrow = is_narrow_terminal()
    sep_eq = "=" * w
    sep_ln = "-" * w

    if not grouped:
        print(f"ℹ️ Sin coincidencias para categoria={category_filter!r}, busqueda={search_term!r}.")
        return

    for grp, items in grouped.items():
        print("\n" + sep_eq)
        print(_wrap_field(f"[{grp.upper()}]", f"{CATEGORY_TITLES.get(grp, 'Parámetros Tácticos de Runtime')} ({len(items)})", w))
        print(sep_eq)
        for full_k, meta in items:
            lock_b = " 🔒[LOCK-OPEN]" if meta["structural_lock"] else ""
            if narrow:
                print(f"  • {full_k}{lock_b}")
                print(f"    Valor: {meta['value']} ({meta['unit']}) | Rango: [{meta['min_limit']}, {meta['max_limit']}]")
            else:
                print(f"  • {full_k:<44} = {str(meta['value']):<8} ({meta['unit']}) | [{meta['min_limit']}, {meta['max_limit']}]{lock_b}")
            print(_wrap_field("Qué hace", meta["description"], w, indent="    "))
            if not compact:
                print(_wrap_field("Rol Mat.", meta["math_role"], w, indent="    "))
                print(_wrap_field("Subir ↑ ", meta["impact_up"], w, indent="    "))
                print(_wrap_field("Bajar ↓ ", meta["impact_down"], w, indent="    "))
            print(sep_ln)


def print_presets_info(preset_name: Optional[str] = None) -> bool:
    w = get_term_width()
    sep_eq = "=" * w
    sep_ln = "-" * w
    if not preset_name or preset_name.strip().lower() == "list":
        print(sep_eq)
        print("  5 CONFIGURACIONES PREDETERMINADAS (PRESETS CUANTITATIVOS)")
        print(sep_eq)
        for name, p in QUANTITATIVE_PRESETS.items():
            tag = "MAINNET / TESTNET" if p.get("allow_live_mainnet") else "SOLO TESTNET / MODO SOMBRA"
            print(f"  🔹 {name}  [{tag}]")
            print(_wrap_field("Título", p.get("title", ""), w, indent="     "))
            print(_wrap_field("Cuándo usarlo", p.get("recommended_for", p.get("regime_target", "")), w, indent="     "))
            print(_wrap_field("Filosofía Mat.", p.get("philosophy", p.get("math_summary", "")), w, indent="     "))
            print(sep_ln)
        print(_wrap_field("Comandos útiles", "Ver detalle: python3 sniper_cli.py presets show <id>  |  Aplicar: python3 sniper_cli.py presets apply <id> --env mainnet", w))
        print(sep_eq)
        return True
    name = preset_name.strip().lower()
    if name not in QUANTITATIVE_PRESETS:
        close = difflib.get_close_matches(name, list(QUANTITATIVE_PRESETS.keys()), n=3, cutoff=0.4)
        sug = f" ¿Quisiste decir: {', '.join(close)}?" if close else ""
        print(f"❌ [ERROR] Preset '{name}' no existe.{sug}")
        return False
    p = QUANTITATIVE_PRESETS[name]
    print(sep_eq)
    print(_wrap_field(f"PERFIL {name}", p.get("title", ""), w))
    print(sep_eq)
    print(_wrap_field("Régimen ideal", p.get("recommended_for", p.get("regime_target", "")), w))
    print(_wrap_field("Resumen Mat.", p.get("philosophy", p.get("math_summary", "")), w))
    print(sep_ln)
    print(f"  Umbrales Hot-Reload incluidos ({len(p.get('thresholds', {}))}):")
    for k, v in p.get("thresholds", {}).items():
        print(f"    • {k:<42} -> {v}")
    print(f"  Parámetros Runtime incluidos ({len(p.get('runtime', {}))}):")
    for rk, rv in p.get("runtime", {}).items():
        print(f"    • runtime.{rk:<34} -> {rv}")
    print(sep_ln)
    print(_wrap_field("Para activarlo", f"python3 sniper_cli.py presets apply {name} --env mainnet", w))
    print(sep_eq)
    return True


def run_bot_with_overrides(
    ctrl: SniperControlCenter, env_override: Optional[str] = None,
    shadow_override: Optional[bool] = None, safe_override: Optional[bool] = None, once_mode: bool = False
) -> None:
    from hedge_risk_manager import HedgeRiskManager
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
        explicit_saved = ctrl._safe_read_json(ctrl.runtime_path) or {}
        full_rt = ctrl.load_runtime_overrides()
        source_dict = explicit_saved if bot_mod.Config.ENV_MODE == "testnet" else full_rt
        for k, raw_v in source_dict.items():
            if k in ("ENV_MODE", "SAFE_MODE") or k not in full_rt:
                continue
            val = full_rt[k]
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
            try:
                subprocess.run(["termux-wake-lock"], check=False, timeout=3.0)
            except Exception:
                pass
        cmd = [sys.executable, os.path.abspath(__file__), "_runner", "--env", env]
        if shadow_flag is True:
            cmd.append("--shadow")
        if safe_flag is True:
            cmd.append("--safe-mode")
        try:
            with open(log_path, "a", encoding="utf-8") as log_fh:
                proc = subprocess.Popen(
                    cmd, cwd=ctrl.base_dir, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True
                )
        except OSError as exc:
            print(f"❌ No se pudo iniciar el demonio [{env.upper()}]: {exc}")
            return 1
        time.sleep(0.35)
        exit_code = proc.poll()
        if exit_code is not None:
            pid_file = ctrl._pid_file_path(env)
            if os.path.exists(pid_file):
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
            print(f"❌ El demonio [{env.upper()}] terminó prematuramente (código {exit_code}). Últimas líneas de {log_path}:")
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    for ln in deque(fh, maxlen=15):
                        print("  " + ln.rstrip("\n"))
            return 1
        print(f"🚀 Demonio [{env.upper()}] iniciado en background (PID: {proc.pid} | Log: {log_path}).")
        return 0

    if act == "stop":
        pid = ctrl.get_running_pid(env)
        if pid is None:
            print(f"ℹ️ Sin demonio activo para [{env.upper()}].")
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            print(f"⚠️ No fue posible enviar SIGTERM a PID {pid}: {exc}")
            return 1
        for _ in range(30):
            if not ctrl._is_pid_alive(pid):
                print(f"✅ Demonio [{env.upper()}] detenido limpiamente con SIGTERM y cerebros sellados.")
                return 0
            time.sleep(0.2)
        if ctrl._is_pid_alive(pid):
            print(f"⚠️ El proceso PID {pid} [{env.upper()}] sigue activo tras 6s esperando cierre limpio.")
            return 1
        return 0

    if act == "restart":
        manage_daemon(ctrl, "stop", env_mode=env)
        return manage_daemon(ctrl, "start", env_mode=env, shadow_flag=shadow_flag, safe_flag=safe_flag)

    if act == "reload":
        target_env = env_mode.strip().lower() if env_mode else env
        pids = ctrl.notify_hot_reload_signal(env_mode=target_env)
        print(f"🔄 SIGHUP enviado a [{target_env.upper()}] PID(s): {pids}" if pids else f"ℹ️ Sin PID activo para [{target_env.upper()}]; el bot recargará automáticamente por mtime (<2s).")
        return 0

    if act == "logs":
        if not os.path.exists(log_path):
            print(f"ℹ️ Sin archivo de log en {log_path}.")
            return 0
        max_l = max(1, int(lines))
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for ln in deque(fh, maxlen=max_l):
                print(ln.rstrip("\n"))
        return 0
    return 1


def _watch_dashboard_loop(ctrl: SniperControlCenter, env_mode: str = "mainnet", interval: float = 3.0, fetch_live: bool = True) -> None:
    wait_s = max(1.0, float(interval))
    try:
        while True:
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
            else:
                os.system("clear" if os.name != "nt" else "cls")
            print_status_dashboard(ctrl, env_mode, fetch_live=fetch_live)
            print(f"  ⏱️ Refrescando cada {wait_s:.1f}s | Presiona Ctrl+C para volver/salir...")
            time.sleep(wait_s)
    except KeyboardInterrupt:
        print("\n  ⏹️ Monitor en vivo finalizado.")


def run_interactive_menu(ctrl: SniperControlCenter) -> None:
    while True:
        w = get_term_width()
        narrow = is_narrow_terminal()
        sep_eq = "=" * w
        sep_ln = "-" * w

        pid_main = ctrl.get_running_pid("mainnet")
        pid_test = ctrl.get_running_pid("testnet")
        active_cnt, active_srcs = ctrl.get_active_trades_count()
        preset_meta = ctrl._safe_read_json(ACTIVE_PRESET_META_PATH) or {}
        active_preset = preset_meta.get("active_preset", "baseline_v73")

        m_stat = f"🟢PID:{pid_main}" if pid_main else "⚪OFF"
        t_stat = f"🟢PID:{pid_test}" if pid_test else "⚪OFF"
        pos_stat = f"{active_cnt} ({','.join(active_srcs)})" if active_cnt > 0 else "0"

        print("\n" + sep_eq)
        print("  LNM SNIPER v7.3+ — CENTRO DE MANDO INTERACTIVO (TERMUX & LINUX)")
        print(sep_eq)
        print(_wrap_field("Estado Rápido", f"MAINNET:{m_stat} | TESTNET:{t_stat} | Pos.Abiertas:{pos_stat} | Perfil:{active_preset}", w))
        print(sep_ln)
        if narrow:
            print("  [1] 📊 Panel de Estado y Signos Vitales RL")
            print("  [2] 📡 Monitor en Vivo (Auto-refresco)")
            print("  [3] 🎛️ Perfiles Cuantitativos (Ver / Aplicar Preset)")
            print("  [4] 📖 Explorar Catálogo y Explicación de Parámetros")
            print("  [5] ⚡ Ajustar Umbral de Aprendizaje en Caliente (Set)")
            print("  [6] ⚙️ Configurar Parámetros Tácticos de Runtime")
            print("  [7] 🚀 Controlar Demonio Background (Start/Stop/Logs)")
            print("  [8] 🧠 Administrar Cerebros Q-Learning (Meta/Sync/Init)")
            print("  [0] 🚪 Salir")
        else:
            print("  [1] 📊 Panel de Estado y Signos Vitales    [5] ⚡ Ajustar Umbral en Caliente (Hot-Reload)")
            print("  [2] 📡 Monitor en Vivo (Auto-refresco)     [6] ⚙️ Configurar Parámetros Tácticos Runtime")
            print("  [3] 🎛️ Perfiles Cuantitativos (Presets)    [7] 🚀 Controlar Demonio (Start/Stop/Logs)")
            print("  [4] 📖 Explorar Catálogo y Explicaciones   [8] 🧠 Administrar Cerebros (Meta/Sync/Init)")
            print("  [0] 🚪 Salir de la consola")
        print(sep_ln)

        choice = _safe_input("  👉 Selecciona una opción [0-8]: ")
        if choice is None:
            break
        choice = choice.strip().lower()
        if choice in ("0", "q", "exit", "salir"):
            print("  👋 Saliendo del Centro de Mando LNM Sniper.")
            break

        elif choice == "1":
            env = _safe_input("  Entorno a consultar (mainnet/testnet) [mainnet]: ", "mainnet")
            if env and env.lower() in ("mainnet", "testnet"):
                print_status_dashboard(ctrl, env.lower())
            elif env is not None:
                print("  ⚠️ Entorno inválido. Usa 'mainnet' o 'testnet'.")

        elif choice == "2":
            env = _safe_input("  Entorno a monitorear (mainnet/testnet) [mainnet]: ", "mainnet")
            if env and env.lower() in ("mainnet", "testnet"):
                iv_raw = _safe_input("  Segundos entre refrescos [3]: ", "3")
                if iv_raw is not None:
                    try:
                        iv = float(iv_raw)
                    except ValueError:
                        iv = 3.0
                    _watch_dashboard_loop(ctrl, env.lower(), iv)

        elif choice == "3":
            print_presets_info("list")
            sub_opt = _safe_input("  Acción: [v] Ver detalle de un perfil | [a] Aplicar perfil | [Enter] Volver: ")
            if not sub_opt:
                continue
            sub_opt = sub_opt.strip().lower()
            if sub_opt in ("v", "ver", "show"):
                p_name = _safe_input("  Nombre del perfil a inspeccionar (ej. capital_fortress): ")
                if p_name:
                    print_presets_info(p_name)
            elif sub_opt in ("a", "aplicar", "apply"):
                p_name = _safe_input("  Nombre del perfil a activar: ")
                if not p_name:
                    continue
                env_p = _safe_input("  Entorno destino (mainnet/testnet) [mainnet]: ", "mainnet")
                if not env_p or env_p.lower() not in ("mainnet", "testnet"):
                    print("  ⚠️ Entorno inválido.")
                    continue
                confirm = _safe_input(f"  ¿Confirmas aplicar '{p_name}' en {env_p.upper()}? (s/n) [n]: ", "n")
                if confirm and confirm.lower() in ("s", "si", "sí", "y", "yes"):
                    _, msg, _ = ctrl.apply_preset(p_name, env_mode=env_p.lower())
                    print("  " + msg)
            else:
                if sub_opt in QUANTITATIVE_PRESETS:
                    print_presets_info(sub_opt)

        elif choice == "4":
            all_c = list(REQUIRED_CATEGORIES) + ["runtime"]
            print("\n  Categorías del Sistema:")
            for idx, c in enumerate(all_c, 1):
                print(_wrap_field(f"[{idx}] {c}", CATEGORY_TITLES.get(c, "Parámetros Tácticos de Runtime"), w, indent="    "))
            pick = _safe_input("\n  Ingresa número [1-11], palabra clave a buscar o nombre de parámetro: ")
            if not pick:
                continue
            catalog = build_full_parameter_catalog(ctrl)
            if pick.isdigit() and 1 <= int(pick) <= len(all_c):
                print_catalog(ctrl, category_filter=all_c[int(pick) - 1])
            elif pick.lower() in all_c:
                print_catalog(ctrl, category_filter=pick.lower())
            else:
                res_k, _ = ctrl.resolve_threshold_key(pick)
                if res_k or pick in catalog or f"runtime.{pick.upper()}" in catalog:
                    print_parameter_explanation(catalog, res_k or pick)
                else:
                    print_catalog(ctrl, search_term=pick)

        elif choice == "5":
            k_in = _safe_input("  Parámetro a ajustar (nombre corto ej. 'min_expectancy_ratio' o '<cat>.<param>'): ")
            if not k_in:
                continue
            res_k, sugs = ctrl.resolve_threshold_key(k_in)
            if res_k is None:
                sug_msg = f" Sugerencias: {', '.join(sugs)}" if sugs else ""
                print(f"  ❌ No se encontró un umbral único para '{k_in}'.{sug_msg}")
                continue
            print_parameter_explanation(build_full_parameter_catalog(ctrl), res_k)
            v_in = _safe_input(f"  Nuevo valor numérico para '{res_k}' (o Enter para cancelar): ")
            if v_in:
                _, msg, _ = ctrl.set_threshold_parameter(res_k, v_in)
                print("  " + msg)

        elif choice == "6":
            print_catalog(ctrl, category_filter="runtime", compact=True)
            rk = _safe_input("  Escribe el parámetro Runtime a editar, 'reset' para restaurar fábrica, o Enter para volver: ")
            if not rk:
                continue
            if rk.strip().lower() == "reset":
                conf = _safe_input("  ¿Restaurar los 21 parámetros de runtime a fábrica? (s/n) [n]: ", "n")
                if conf and conf.lower() in ("s", "si", "sí", "y", "yes"):
                    _, msg = ctrl.reset_runtime_overrides()
                    print("  " + msg)
                continue
            cat_full = build_full_parameter_catalog(ctrl)
            rk_clean = rk.strip().upper().replace("RUNTIME.", "")
            if f"runtime.{rk_clean}" in cat_full:
                print_parameter_explanation(cat_full, f"runtime.{rk_clean}")
            rv = _safe_input(f"  Nuevo valor para '{rk_clean}' (o Enter para cancelar): ")
            if rv:
                _, msg, _ = ctrl.set_runtime_parameter(rk_clean, rv)
                print("  " + msg)

        elif choice == "7":
            act = _safe_input("  Acción del demonio (start / stop / restart / reload / logs): ")
            if not act:
                continue
            act = act.strip().lower()
            if act not in ("start", "stop", "restart", "reload", "logs"):
                print("  ⚠️ Acción inválida. Opciones: start, stop, restart, reload, logs.")
                continue
            env = _safe_input("  Entorno (mainnet/testnet) [mainnet]: ", "mainnet")
            if not env or env.lower() not in ("mainnet", "testnet"):
                print("  ⚠️ Entorno inválido.")
                continue
            sh_flag: Optional[bool] = None
            sf_flag: Optional[bool] = None
            lines_cnt = 40
            if act in ("start", "restart"):
                mode_in = _safe_input("  Modo de operación: [1] Normal | [2] Shadow (Sin órdenes reales) | [3] Safe-Mode [1]: ", "1")
                if mode_in is None:
                    continue
                if mode_in.strip() == "2":
                    sh_flag = True
                elif mode_in.strip() == "3":
                    sf_flag = True
            elif act == "logs":
                l_in = _safe_input("  Cantidad de líneas a mostrar [40]: ", "40")
                if l_in and l_in.isdigit():
                    lines_cnt = int(l_in)
            manage_daemon(ctrl, act, env_mode=env.lower(), shadow_flag=sh_flag, safe_flag=sf_flag, lines=lines_cnt)

        elif choice == "8":
            b_act = _safe_input("  Acción Cerebro: [1] Ver estado | [2] Cambiar Meta Sats | [3] Sync Testnet➔Mainnet | [4] Init-Clean: ")
            if not b_act:
                continue
            b_act = b_act.strip().lower()
            if b_act in ("1", "info"):
                env_b = _safe_input("  Entorno (mainnet/testnet) [mainnet]: ", "mainnet")
                if env_b and env_b.lower() in ("mainnet", "testnet"):
                    print_status_dashboard(ctrl, env_b.lower())
            elif b_act in ("2", "set-target", "meta"):
                env_b = _safe_input("  Entorno (mainnet/testnet) [mainnet]: ", "mainnet")
                if not env_b or env_b.lower() not in ("mainnet", "testnet"):
                    continue
                sats_raw = _safe_input("  Nueva meta en satoshis (ej. 350000 o 350k): ")
                if not sats_raw:
                    continue
                parsed_sats = _parse_sats_input(sats_raw)
                if parsed_sats is None:
                    print(f"  ❌ Valor de satoshis inválido: '{sats_raw}'.")
                    continue
                _, msg = ctrl.update_brain_target_sats(parsed_sats, env_mode=env_b.lower())
                print("  " + msg)
            elif b_act in ("3", "sync"):
                dry = _safe_input("  ¿Simular primero con dry-run? (s/n) [s]: ", "s")
                if dry is None:
                    continue
                from sync_brains_dual import sync_dual_brains
                sync_dual_brains(dry_run=dry.lower() in ("s", "si", "sí", "y", "yes"))
            elif b_act in ("4", "init-clean"):
                cnt, srcs = ctrl.get_active_trades_count()
                if cnt > 0:
                    print(f"  🛑 [BLOQUEO] No se puede reiniciar cerebros con posiciones activas ({', '.join(srcs)}).")
                    continue
                conf = _safe_input("  ⚠️ Escribe 'REINICIAR' para confirmar el reseteo limpio de cerebros (con backup previo): ")
                if conf == "REINICIAR":
                    from init_clean_brains import create_clean_brains
                    create_clean_brains()
                else:
                    print("  ↩️ Operación init-clean cancelada.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sniper_cli.py",
        description="CLI de Control Cuantitativo, Hot-Reload Atómico y Telemetría para LNM Sniper v7.3+",
    )
    sub = parser.add_subparsers(dest="subcommand")

    p_st = sub.add_parser("status", help="Muestra el panel completo de estado y signos vitales RL")
    p_st.add_argument("--env", default="mainnet", choices=["mainnet", "testnet"])
    p_st.add_argument("--offline", action="store_true", help="Omitir consulta en vivo a LN Markets API")

    p_wt = sub.add_parser("watch", help="Monitor en vivo con auto-refresco continuo")
    p_wt.add_argument("--env", default="mainnet", choices=["mainnet", "testnet"])
    p_wt.add_argument("--interval", type=float, default=3.0)
    p_wt.add_argument("--offline", action="store_true", help="Omitir consulta en vivo a LN Markets API")

    p_cat = sub.add_parser("catalog", help="Explora o busca en el catálogo de 99 parámetros")
    p_cat.add_argument("--category", "-c", default=None)
    p_cat.add_argument("--search", "-s", default=None)
    p_cat.add_argument("--compact", action="store_true")

    p_exp = sub.add_parser("explain", help="Muestra la ficha matemática completa de un parámetro")
    p_exp.add_argument("param")

    p_set = sub.add_parser("set", help="Modifica un umbral de aprendizaje en caliente (Hot-Reload)")
    p_set.add_argument("param")
    p_set.add_argument("value")
    p_set.add_argument("--env", default=None, choices=["mainnet", "testnet"])

    sub.add_parser("runtime-list", help="Lista los 21 parámetros tácticos de runtime")
    p_rset = sub.add_parser("runtime-set", help="Modifica un parámetro táctico de runtime")
    p_rset.add_argument("param")
    p_rset.add_argument("value")
    sub.add_parser("runtime-reset", help="Restaura los parámetros de runtime a valores de fábrica")

    p_pre = sub.add_parser("presets", help="Lista, inspecciona o aplica perfiles cuantitativos")
    p_pre.add_argument("action", nargs="?", default="list", choices=["list", "show", "apply"])
    p_pre.add_argument("name", nargs="?", default=None)
    p_pre.add_argument("--env", default=None, choices=["mainnet", "testnet"])

    p_dae = sub.add_parser("daemon", help="Gestiona el demonio en segundo plano (start/stop/restart/reload/logs)")
    p_dae.add_argument("action", choices=["start", "stop", "restart", "reload", "logs"])
    p_dae.add_argument("--env", default=None, choices=["mainnet", "testnet"])
    p_dae.add_argument("--shadow", action="store_true", default=None)
    p_dae.add_argument("--safe-mode", action="store_true", default=None)
    p_dae.add_argument("--lines", type=int, default=40)

    p_brn = sub.add_parser("brain", help="Administra los cerebros Q-Learning (info/set-target/sync/init-clean)")
    p_brn.add_argument("action", choices=["info", "set-target", "sync", "init-clean"])
    p_brn.add_argument("--env", default="mainnet", choices=["mainnet", "testnet"])
    p_brn.add_argument("--sats", default=None)
    p_brn.add_argument("--dry-run", action="store_true")

    sub.add_parser("menu", help="Abre el centro de mando interactivo guiado")

    p_run = sub.add_parser("_runner")
    p_run.add_argument("--env", default="mainnet")
    p_run.add_argument("--shadow", action="store_true", default=False)
    p_run.add_argument("--no-shadow", action="store_true", default=False)
    p_run.add_argument("--safe-mode", action="store_true", default=False)
    p_run.add_argument("--once", action="store_true", default=False)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        parser = build_arg_parser()
        args = parser.parse_args(argv)
        ctrl = SniperControlCenter()
        if args.subcommand is None:
            if sys.stdin.isatty():
                run_interactive_menu(ctrl)
                return 0
            print_status_dashboard(ctrl, "mainnet")
            return 0
        cmd = args.subcommand

        if cmd == "_runner":
            sh_ovr = True if args.shadow else (False if args.no_shadow else None)
            run_bot_with_overrides(ctrl, args.env, sh_ovr, True if args.safe_mode else None, args.once)
            return 0
        if cmd == "status":
            print_status_dashboard(ctrl, args.env, fetch_live=not args.offline)
            return 0
        if cmd == "watch":
            _watch_dashboard_loop(ctrl, args.env, args.interval, fetch_live=not args.offline)
            return 0
        if cmd == "catalog":
            print_catalog(ctrl, args.category, args.search, args.compact)
            return 0
        if cmd == "explain":
            return 0 if print_parameter_explanation(build_full_parameter_catalog(ctrl), args.param) else 1
        if cmd == "set":
            ok, msg, _ = ctrl.set_threshold_parameter(args.param, args.value, env_check=args.env)
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
            if not args.name:
                print("❌ Indica el nombre del preset a aplicar. Ejemplo: python3 sniper_cli.py presets apply capital_fortress --env mainnet")
                return 1
            ok, msg, _ = ctrl.apply_preset(args.name, env_mode=args.env)
            print(msg)
            return 0 if ok else 1
        if cmd == "daemon":
            return manage_daemon(ctrl, args.action, args.env, True if args.shadow else None, True if args.safe_mode else None, args.lines)
        if cmd == "brain":
            if args.action == "info":
                print_status_dashboard(ctrl, args.env)
                return 0
            if args.action == "set-target":
                if args.sats is None:
                    print("❌ Debes indicar la meta con --sats. Ejemplo: python3 sniper_cli.py brain set-target --sats 350000 --env mainnet")
                    return 1
                parsed = _parse_sats_input(args.sats)
                if parsed is None:
                    print(f"❌ Valor inválido en --sats: {args.sats!r}. Ejemplo: --sats 350000 o --sats 350k")
                    return 1
                ok, msg = ctrl.update_brain_target_sats(parsed, env_mode=args.env)
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
    except KeyboardInterrupt:
        print("\n⏹️ Operación interrumpida por el usuario.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
