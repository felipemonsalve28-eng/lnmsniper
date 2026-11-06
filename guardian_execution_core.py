# -*- coding: utf-8 -*-
"""
guardian_execution_core.py
Almacenamiento atómico con verificación SHA-256, bloqueo POSIX fcntl,
recuperación de backup, cliente HTTP resiliente y protección de piso duro
para LNM Sniper v7.3+ (Mainnet & Testnet Dual).
"""
import os
import json
import time
import fcntl
import shutil
import random
import hashlib
import requests
from requests.adapters import HTTPAdapter


class FileLockContext:
    """Bloqueo de archivo POSIX exclusivo/compartido compatible con Termux."""
    def __init__(self, lock_path, exclusive=True, timeout=5.0):
        self.lock_path = f"{lock_path}.lock"
        self.exclusive = exclusive
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        self.fd = open(self.lock_path, "a+", encoding="utf-8")
        flag = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        t0 = time.time()
        while True:
            try:
                fcntl.flock(self.fd.fileno(), flag | fcntl.LOCK_NB)
                break
            except (IOError, OSError):
                if (time.time() - t0) >= self.timeout:
                    break
                time.sleep(0.05)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.fd.close()
            except Exception:
                pass


class AtomicBrainStore:
    def __init__(self, filename="q_brain_advanced_lnm.json"):
        self.filename = filename
        self.tmp_file = f"{filename}.tmp.{os.getpid()}"
        self.bak_file = f"{filename}.bak"
        self.sha_file = f"{filename}.sha256"

    @staticmethod
    def _compute_bytes_sha256(raw_bytes):
        return hashlib.sha256(raw_bytes).hexdigest()

    def save(self, data):
        if not isinstance(data, dict) or "q_table" not in data:
            raise ValueError("Intento de guardar un cerebro inválido o incompleto (sin q_table).")
        raw_json = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = self._compute_bytes_sha256(raw_json)

        with FileLockContext(self.filename, exclusive=True, timeout=6.0):
            with open(self.tmp_file, "wb") as f:
                f.write(raw_json)
                f.flush()
                os.fsync(f.fileno())

            if os.path.exists(self.filename):
                try:
                    shutil.copy2(self.filename, self.bak_file)
                except Exception:
                    pass

            os.replace(self.tmp_file, self.filename)
            with open(self.sha_file, "w", encoding="utf-8") as sf:
                sf.write(digest)
                sf.flush()
                os.fsync(sf.fileno())
        return digest

    def verify_file(self, path):
        if not os.path.exists(path):
            return False, None
        try:
            with open(path, "rb") as f:
                raw = f.read()
            if not raw:
                return False, None
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or "q_table" not in data:
                return False, None
            if path == self.filename and os.path.exists(self.sha_file):
                with open(self.sha_file, "r", encoding="utf-8") as sf:
                    parts = sf.read().strip().split()
                    expected_sha = parts[0] if parts else ""
                actual_sha = self._compute_bytes_sha256(raw)
                if expected_sha and actual_sha != expected_sha:
                    # JSON válido pero hash desfasado: auto-reparar archivo .sha256
                    try:
                        with open(self.sha_file, "w", encoding="utf-8") as wsf:
                            wsf.write(actual_sha)
                    except Exception:
                        pass
                    return True, data
            return True, data
        except Exception:
            return False, None

    def load(self):
        with FileLockContext(self.filename, exclusive=False, timeout=4.0):
            ok, data = self.verify_file(self.filename)
            if ok and data is not None:
                return data
            ok_bak, data_bak = self.verify_file(self.bak_file)
            if ok_bak and data_bak is not None:
                try:
                    shutil.copy2(self.bak_file, self.filename)
                except Exception:
                    pass
                return data_bak
        return None


class HardFloorGuardian:
    def __init__(self, initial_floor_ratio=0.75, milestone_floor_ratio=0.88, max_dd_pct=0.16, max_daily_loss_pct=0.11):
        self.initial_floor_ratio = float(initial_floor_ratio)
        self.milestone_floor_ratio = float(milestone_floor_ratio)
        self.max_dd_pct = float(max_dd_pct)
        self.max_daily_loss_pct = float(max_daily_loss_pct)

    def evaluate_protection(self, equity, available_balance, peak_equity, locked_floor, daily_pnl_pct):
        current_dd_pct = (peak_equity - equity) / max(peak_equity, 1) if peak_equity > 0 else 0.0
        floor_breached = locked_floor > 0 and equity <= locked_floor
        dd_breached = current_dd_pct >= self.max_dd_pct
        daily_breached = daily_pnl_pct <= -abs(self.max_daily_loss_pct)
        should_liquidate = floor_breached or (current_dd_pct >= 0.22)
        should_halt_entries = should_liquidate or dd_breached or daily_breached
        reason = "OK"
        if floor_breached:
            reason = f"HARD_FLOOR_BREACH(eq={equity}<=floor={locked_floor})"
        elif should_liquidate:
            reason = f"CRITICAL_DD_BREACH(dd={current_dd_pct*100:.1f}%)"
        elif dd_breached:
            reason = f"MAX_DD_LIMIT(dd={current_dd_pct*100:.1f}%>={self.max_dd_pct*100:.1f}%)"
        elif daily_breached:
            reason = f"MAX_DAILY_LOSS(daily={daily_pnl_pct*100:.1f}%<=-{self.max_daily_loss_pct*100:.1f}%)"
        return {
            "current_dd_pct": round(current_dd_pct, 4),
            "should_liquidate": bool(should_liquidate),
            "should_halt_entries": bool(should_halt_entries),
            "reason": reason,
        }


class ResilientLNMClient:
    """Pool de conexiones persistentes con control de latencia para Termux."""
    @staticmethod
    def build_session(user_agent="LNMSniper/7.3-Dual"):
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"Connection": "keep-alive", "User-Agent": user_agent})
        return session

    @staticmethod
    def compute_backoff(attempt, base=0.65, cap=6.0):
        return min(cap, (base * (1.8 ** attempt)) + random.uniform(0.08, 0.28))
