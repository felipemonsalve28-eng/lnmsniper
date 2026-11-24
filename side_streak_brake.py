#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
side_streak_brake.py
Freno discreto por lado C1_STREAK3_24H con persistencia atómica POSIX (fcntl) y verificación SHA-256.

Regla matemática certificada en Fase 3 (C1_STREAK3_24H_INV):
  - Contadores independientes por lado s in {'b' (LONG, acción 1), 's' (SHORT, acción 5)}:
      side_loss_streak[s] y side_pause_until_ts[s].
  - Al cerrar un trade del lado s:
      * Si net_pnl_sats > 0: side_loss_streak[s] = 0 y side_pause_until_ts[s] = 0.0.
      * Si net_pnl_sats <= 0: side_loss_streak[s] += 1; al llegar a 3 pérdidas consecutivas,
        activa pausa de 24 horas (close_ts + 86400.0) únicamente para el lado s y reinicia
        side_loss_streak[s] = 0, permitiendo que el lado opuesto opere normalmente a 1.00x.
  - Persistencia atómica (tmp + os.fsync + os.replace) bajo FileLockContext (fcntl) y sidecar
    .sha256 en $HOME/.sniper_state/side_brake_state_{env}.json, ejecutando I/O únicamente al
    cerrar un trade o cambiar estado de pausa (cero bloqueo en el bucle de 5s).
"""
import hashlib
import json
import os
import shutil
import time
from typing import Dict, Optional, Tuple, Union

from guardian_execution_core import FileLockContext


class SideStreakBrakeStore:
    """Almacén persistente y verificador O(1) en RAM del freno por lado C1_STREAK3_24H."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        env_mode: str = "mainnet",
        filepath: Optional[str] = None,
        base_dir: Optional[str] = None,
        max_consecutive_losses: int = 3,
        pause_duration_sec: float = 86400.0,
        persist_to_disk: bool = True,
        enabled: bool = True,
    ):
        self.env_mode = str(env_mode or "mainnet").strip().lower()
        self.max_consecutive_losses = max(1, int(max_consecutive_losses))
        self.pause_duration_sec = max(1.0, float(pause_duration_sec))
        self.persist_to_disk = bool(persist_to_disk)
        self.enabled = bool(enabled)

        if filepath:
            self.filepath = os.path.abspath(str(filepath))
        elif base_dir:
            self.filepath = os.path.abspath(
                os.path.join(str(base_dir), f"side_brake_state_{self.env_mode}.json")
            )
        else:
            home_dir = os.path.expanduser("~")
            state_dir = os.path.join(home_dir, ".sniper_state")
            self.filepath = os.path.abspath(
                os.path.join(state_dir, f"side_brake_state_{self.env_mode}.json")
            )

        self.side_loss_streak: Dict[str, int] = {"b": 0, "s": 0}
        self.side_pause_until_ts: Dict[str, float] = {"b": 0.0, "s": 0.0}
        self.total_pauses_triggered: Dict[str, int] = {"b": 0, "s": 0}
        self.last_updated_ts: float = 0.0
        self.sha256_valid: bool = True
        self.sha256_corrupted_detected: bool = False
        self.last_sha256_digest: str = ""

        if self.persist_to_disk:
            self.load()

    @staticmethod
    def _norm_side(side: Union[str, int]) -> str:
        if isinstance(side, int):
            return "b" if int(side) in (1, 2, 3, 4) else "s"
        s = str(side or "b").strip().lower()
        if s in ("b", "buy", "long", "l_trend", "1"):
            return "b"
        return "s"

    def _build_payload(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "env": self.env_mode,
            "enabled": bool(self.enabled),
            "max_consecutive_losses": int(self.max_consecutive_losses),
            "pause_duration_sec": float(self.pause_duration_sec),
            "side_loss_streak": {
                "b": int(self.side_loss_streak.get("b", 0)),
                "s": int(self.side_loss_streak.get("s", 0)),
            },
            "side_pause_until_ts": {
                "b": round(float(self.side_pause_until_ts.get("b", 0.0)), 3),
                "s": round(float(self.side_pause_until_ts.get("s", 0.0)), 3),
            },
            "total_pauses_triggered": {
                "b": int(self.total_pauses_triggered.get("b", 0)),
                "s": int(self.total_pauses_triggered.get("s", 0)),
            },
            "last_updated_ts": round(float(self.last_updated_ts or time.time()), 3),
        }

    def _apply_payload(self, data: dict) -> None:
        streaks = data.get("side_loss_streak") or {}
        pauses = data.get("side_pause_until_ts") or {}
        totals = data.get("total_pauses_triggered") or {}
        for s_key in ("b", "s"):
            self.side_loss_streak[s_key] = max(0, int(streaks.get(s_key, 0) or 0))
            self.side_pause_until_ts[s_key] = max(0.0, float(pauses.get(s_key, 0.0) or 0.0))
            self.total_pauses_triggered[s_key] = max(0, int(totals.get(s_key, 0) or 0))
        self.last_updated_ts = float(data.get("last_updated_ts", 0.0) or 0.0)

    def verify_disk_integrity(self) -> Tuple[bool, str]:
        """Verifica que el archivo JSON en disco coincida con su sidecar .sha256."""
        if not os.path.exists(self.filepath):
            return False, "STATE_FILE_MISSING"
        sha_path = f"{self.filepath}.sha256"
        if not os.path.exists(sha_path):
            return False, "SHA256_SIDECAR_MISSING"
        try:
            with open(self.filepath, "rb") as fh:
                raw_bytes = fh.read()
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            with open(sha_path, "r", encoding="utf-8") as sfh:
                expected_sha = sfh.read().strip().split()[0]
            if actual_sha != expected_sha:
                return False, f"SHA256_MISMATCH(expected={expected_sha[:12]}|actual={actual_sha[:12]})"
            json.loads(raw_bytes.decode("utf-8"))
            return True, actual_sha
        except Exception as exc:
            return False, f"VERIFY_ERROR({exc})"

    def save(self) -> str:
        """Persiste atómicamente el estado del freno por lado con bloqueo POSIX fcntl y sidecar SHA-256."""
        if not self.persist_to_disk:
            return ""
        parent = os.path.dirname(self.filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = self._build_payload()
        raw_bytes = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        tmp_suffix = f"{os.getpid()}.{int(time.monotonic() * 1000000) % 1000000}"
        tmp_path = f"{self.filepath}.tmp.{tmp_suffix}"
        sha_tmp_path = f"{self.filepath}.sha256.tmp.{tmp_suffix}"
        sha_path = f"{self.filepath}.sha256"
        bak_path = f"{self.filepath}.bak"

        with FileLockContext(self.filepath, exclusive=True, timeout=5.0, raise_on_timeout=False):
            try:
                if os.path.exists(self.filepath):
                    try:
                        shutil.copy2(self.filepath, bak_path)
                    except OSError:
                        pass
                with open(tmp_path, "wb") as fh:
                    fh.write(raw_bytes)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, self.filepath)

                with open(sha_tmp_path, "w", encoding="utf-8") as sfh:
                    sfh.write(f"{digest}  {os.path.basename(self.filepath)}\n")
                    sfh.flush()
                    try:
                        os.fsync(sfh.fileno())
                    except OSError:
                        pass
                os.replace(sha_tmp_path, sha_path)
            finally:
                for leftover in (tmp_path, sha_tmp_path):
                    if os.path.exists(leftover):
                        try:
                            os.remove(leftover)
                        except OSError:
                            pass

        self.last_sha256_digest = digest
        self.sha256_valid = True
        return digest

    def load(self) -> dict:
        """Carga el estado persistido verificando el hash SHA-256 bajo bloqueo POSIX fcntl."""
        if not self.persist_to_disk:
            return self._build_payload()
        parent = os.path.dirname(self.filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(self.filepath):
            self.save()
            return self._build_payload()

        with FileLockContext(self.filepath, exclusive=False, timeout=5.0, raise_on_timeout=False):
            ok_sha, sha_info = self.verify_disk_integrity()
            if ok_sha:
                try:
                    with open(self.filepath, "rb") as fh:
                        data = json.loads(fh.read().decode("utf-8"))
                    if isinstance(data, dict):
                        self._apply_payload(data)
                        self.sha256_valid = True
                        self.sha256_corrupted_detected = False
                        self.last_sha256_digest = sha_info
                        return self._build_payload()
                except Exception:
                    pass

            # Si el archivo principal o su SHA-256 fallan, registrar corrupción y recuperar desde .bak o neutral
            self.sha256_valid = False
            self.sha256_corrupted_detected = True
            print(f"[AVISO SIDE-BRAKE SHA-256] Integridad fallida en {self.filepath} ({sha_info}); intentando .bak/neutral.")
            bak_path = f"{self.filepath}.bak"
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "rb") as bfh:
                        bak_data = json.loads(bfh.read().decode("utf-8"))
                    if isinstance(bak_data, dict):
                        self._apply_payload(bak_data)
                        self.save()
                        return self._build_payload()
                except Exception:
                    pass

            # Fallback limpio a estado neutral
            self.side_loss_streak = {"b": 0, "s": 0}
            self.side_pause_until_ts = {"b": 0.0, "s": 0.0}
            self.total_pauses_triggered = {"b": 0, "s": 0}
            self.last_updated_ts = time.time()
            self.save()
            return self._build_payload()

    def reset(self, persist: Optional[bool] = None) -> None:
        """Reinicia contadores en RAM (y opcionalmente en disco)."""
        self.side_loss_streak = {"b": 0, "s": 0}
        self.side_pause_until_ts = {"b": 0.0, "s": 0.0}
        self.total_pauses_triggered = {"b": 0, "s": 0}
        self.last_updated_ts = time.time()
        should_save = self.persist_to_disk if persist is None else bool(persist)
        if should_save:
            self.save()

    def is_side_paused(self, side: Union[str, int], now: Optional[float] = None) -> Tuple[bool, float]:
        """
        Consulta O(1) en RAM (cero I/O de disco en tick):
        Retorna (paused: bool, remaining_sec: float) para el lado indicado.
        """
        if not self.enabled:
            return False, 0.0
        s_norm = self._norm_side(side)
        now_ts = float(time.time() if now is None else now)
        pause_until = float(self.side_pause_until_ts.get(s_norm, 0.0))
        if pause_until <= 0.0:
            return False, 0.0
        if now_ts < pause_until:
            return True, round(pause_until - now_ts, 3)
        # Expiró la ventana de 24h: limpiar pausa en memoria
        self.side_pause_until_ts[s_norm] = 0.0
        self.side_loss_streak[s_norm] = 0
        return False, 0.0

    def record_trade_close(
        self,
        side: Union[str, int],
        net_pnl_sats: int,
        close_ts: Optional[float] = None,
    ) -> dict:
        """
        Actualiza el contador de racha del lado s al liquidar un trade primario y persiste atómicamente:
          - net_pnl_sats > 0  -> side_loss_streak[s] = 0, side_pause_until_ts[s] = 0.0.
          - net_pnl_sats <= 0 -> side_loss_streak[s] += 1; si alcanza 3, pausa 24h ese lado y reinicia racha a 0.
        """
        s_norm = self._norm_side(side)
        now_ts = float(time.time() if close_ts is None else close_ts)
        pnl_int = int(round(float(net_pnl_sats)))

        if self.side_pause_until_ts.get(s_norm, 0.0) > 0.0 and now_ts >= self.side_pause_until_ts[s_norm]:
            self.side_pause_until_ts[s_norm] = 0.0

        triggered_pause = False
        if pnl_int > 0:
            self.side_loss_streak[s_norm] = 0
            self.side_pause_until_ts[s_norm] = 0.0
        else:
            self.side_loss_streak[s_norm] = int(self.side_loss_streak.get(s_norm, 0)) + 1
            if self.enabled and self.side_loss_streak[s_norm] >= self.max_consecutive_losses:
                self.side_pause_until_ts[s_norm] = now_ts + self.pause_duration_sec
                self.side_loss_streak[s_norm] = 0
                self.total_pauses_triggered[s_norm] = int(self.total_pauses_triggered.get(s_norm, 0)) + 1
                triggered_pause = True

        self.last_updated_ts = now_ts
        if self.persist_to_disk:
            self.save()

        paused_now, rem_sec = self.is_side_paused(s_norm, now=now_ts)
        return {
            "side": s_norm,
            "net_pnl_sats": pnl_int,
            "triggered_pause": triggered_pause,
            "loss_streak": int(self.side_loss_streak[s_norm]),
            "pause_until_ts": float(self.side_pause_until_ts[s_norm]),
            "is_paused": paused_now,
            "remaining_sec": rem_sec,
            "total_pauses_triggered": int(self.total_pauses_triggered[s_norm]),
        }

    def get_telemetry_snapshot(self, now: Optional[float] = None) -> dict:
        """Instantánea O(1) en RAM para telemetry_live_{env}.json y sniper_cli.py."""
        now_ts = float(time.time() if now is None else now)
        paused_b, rem_b = self.is_side_paused("b", now=now_ts)
        paused_s, rem_s = self.is_side_paused("s", now=now_ts)
        return {
            "enabled": bool(self.enabled),
            "mode": "C1_STREAK3_24H",
            "max_consecutive_losses": int(self.max_consecutive_losses),
            "pause_duration_sec": float(self.pause_duration_sec),
            "side_loss_streak": {
                "b": int(self.side_loss_streak.get("b", 0)),
                "s": int(self.side_loss_streak.get("s", 0)),
            },
            "side_pause_until_ts": {
                "b": round(float(self.side_pause_until_ts.get("b", 0.0)), 3),
                "s": round(float(self.side_pause_until_ts.get("s", 0.0)), 3),
            },
            "side_paused": {
                "b": bool(paused_b),
                "s": bool(paused_s),
            },
            "side_remaining_sec": {
                "b": round(float(rem_b), 1),
                "s": round(float(rem_s), 1),
            },
            "total_pauses_triggered": {
                "b": int(self.total_pauses_triggered.get("b", 0)),
                "s": int(self.total_pauses_triggered.get("s", 0)),
            },
            "sha256_valid": bool(self.sha256_valid),
            "last_sha256": str(self.last_sha256_digest[:16]) if self.last_sha256_digest else "",
            "state_file": str(self.filepath),
        }
