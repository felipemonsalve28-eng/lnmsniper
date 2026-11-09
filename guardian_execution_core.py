# -*- coding: utf-8 -*-
"""
guardian_execution_core.py
Almacenamiento atómico con verificación SHA-256, bloqueo POSIX fcntl (con fallback multiplataforma),
recuperación de backup, telemetría de latencia P50/P95/P99, prevención de órdenes duplicadas,
rotación de logs, cliente HTTP resiliente y protección de piso duro
para LNM Sniper v7.3+ (Mainnet & Testnet Dual - LN Markets API v3).
"""
import os
import json
import time
import math
import shutil
import random
import hashlib
import threading
from collections import deque
import requests
from requests.adapters import HTTPAdapter

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    fcntl = None
    _HAS_FCNTL = False

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:
    msvcrt = None
    _HAS_MSVCRT = False


class FileLockContext:
    """
    Bloqueo de archivo POSIX exclusivo/compartido compatible con Termux/Linux
    y fallback seguro en Windows. Lanza TimeoutError si no logra adquirir el
    bloqueo dentro de `timeout`, impidiendo accesos concurrentes sin candado.
    """
    def __init__(self, lock_path, exclusive=True, timeout=5.0, raise_on_timeout=True):
        self.lock_path = f"{lock_path}.lock"
        self.exclusive = bool(exclusive)
        self.timeout = max(0.05, float(timeout))
        self.raise_on_timeout = bool(raise_on_timeout)
        self.fd = None
        self.acquired = False

    def _try_lock(self):
        if _HAS_FCNTL:
            flag = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            fcntl.flock(self.fd.fileno(), flag | fcntl.LOCK_NB)
            return True
        if _HAS_MSVCRT:
            self.fd.seek(0)
            msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        return True

    def _unlock(self):
        if not self.acquired or self.fd is None:
            return
        try:
            if _HAS_FCNTL:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            elif _HAS_MSVCRT:
                self.fd.seek(0)
                msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        self.acquired = False

    def __enter__(self):
        self.fd = open(self.lock_path, "a+", encoding="utf-8")
        t0 = time.monotonic()
        while True:
            try:
                self._try_lock()
                self.acquired = True
                break
            except (IOError, OSError):
                if (time.monotonic() - t0) >= self.timeout:
                    try:
                        self.fd.close()
                    except Exception:
                        pass
                    self.fd = None
                    if self.raise_on_timeout:
                        raise TimeoutError(
                            f"Timeout ({self.timeout:.2f}s) adquiriendo bloqueo sobre {self.lock_path}"
                        )
                    break
                time.sleep(0.025)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            self._unlock()
            try:
                self.fd.close()
            except Exception:
                pass
            self.fd = None


class AtomicBrainStore:
    """
    Persistencia atómica con validación SHA-256, verificación de consistencia
    de estados Q y recuperación automática desde respaldo .bak.
    """
    def __init__(self, filename="q_brain_advanced_lnm.json", required_key="q_table"):
        self.filename = filename
        self.required_key = required_key
        self.tmp_file = f"{filename}.tmp.{os.getpid()}"
        self.bak_file = f"{filename}.bak"
        self.sha_file = f"{filename}.sha256"

    @staticmethod
    def _compute_bytes_sha256(raw_bytes):
        return hashlib.sha256(raw_bytes).hexdigest()

    @staticmethod
    def _validate_q_consistency(data, required_key="q_table"):
        if not isinstance(data, dict):
            return False
        if required_key and required_key not in data:
            return False
        q_tbl = data.get("q_table")
        if isinstance(q_tbl, dict):
            for k, vec in q_tbl.items():
                if not isinstance(k, str) or not isinstance(vec, list) or len(vec) == 0:
                    return False
                for v in vec:
                    if not isinstance(v, (int, float)) or math.isnan(float(v)) or math.isinf(float(v)):
                        return False
        return True

    def save(self, data):
        if not self._validate_q_consistency(data, self.required_key):
            raise ValueError(
                f"Intento de guardar estructura inválida o corrupta en {self.filename} (clave requerida: {self.required_key})."
            )
        raw_json = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = self._compute_bytes_sha256(raw_json)
        tmp_path = f"{self.filename}.tmp.{os.getpid()}.{int(time.monotonic() * 1000000) % 1000000}"

        with FileLockContext(self.filename, exclusive=True, timeout=6.0):
            try:
                with open(tmp_path, "wb") as f:
                    f.write(raw_json)
                    f.flush()
                    os.fsync(f.fileno())

                if os.path.exists(self.filename):
                    try:
                        shutil.copy2(self.filename, self.bak_file)
                    except Exception:
                        pass

                os.replace(tmp_path, self.filename)
                with open(self.sha_file, "w", encoding="utf-8") as sf:
                    sf.write(digest)
                    sf.flush()
                    os.fsync(sf.fileno())
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
        return digest

    def verify_file(self, path, strict_sha=False):
        if not os.path.exists(path):
            return False, None
        try:
            with open(path, "rb") as f:
                raw = f.read()
            if not raw:
                return False, None
            data = json.loads(raw.decode("utf-8"))
            if not self._validate_q_consistency(data, self.required_key):
                return False, None
            if path == self.filename and os.path.exists(self.sha_file):
                with open(self.sha_file, "r", encoding="utf-8") as sf:
                    parts = sf.read().strip().split()
                    expected_sha = parts[0] if parts else ""
                actual_sha = self._compute_bytes_sha256(raw)
                if expected_sha and actual_sha != expected_sha:
                    if strict_sha:
                        return False, None
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
                    raw_bak = json.dumps(data_bak, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    with open(self.sha_file, "w", encoding="utf-8") as wsf:
                        wsf.write(self._compute_bytes_sha256(raw_bak))
                except Exception:
                    pass
                return data_bak
        return None


class LatencyTelemetry:
    """
    Sistema de medición real de latencia por etapa con cálculo de percentiles
    móviles (P50, P95, P99) y detección de degradación para bloqueo preventivo.
    """
    METRICS = (
        "network_ms",
        "api_ms",
        "processing_ms",
        "decision_ms",
        "order_dispatch_ms",
        "confirm_ms",
        "cycle_ms",
    )

    def __init__(self, window_size=256):
        self.window_size = max(16, int(window_size))
        self.samples = {m: deque(maxlen=self.window_size) for m in self.METRICS}
        self.last_values = {m: 0.0 for m in self.METRICS}
        self._cached_stats = {}

    def record(self, metric_name, elapsed_ms):
        val = max(0.0, round(float(elapsed_ms), 2))
        if metric_name not in self.samples:
            self.samples[metric_name] = deque(maxlen=self.window_size)
        self.samples[metric_name].append(val)
        self.last_values[metric_name] = val
        self._cached_stats.pop(metric_name, None)
        return val

    def get_percentiles(self, metric_name):
        cached = self._cached_stats.get(metric_name)
        if cached is not None:
            return cached
        buf = self.samples.get(metric_name)
        if not buf:
            res = {"p50": 0.0, "p95": 0.0, "p99": 0.0, "last": 0.0, "count": 0}
            self._cached_stats[metric_name] = res
            return res
        arr = sorted(buf)
        n = len(arr)
        p50 = arr[min(n - 1, int(n * 0.50))]
        p95 = arr[min(n - 1, int(n * 0.95))]
        p99 = arr[min(n - 1, int(n * 0.99))]
        res = {
            "p50": round(p50, 1),
            "p95": round(p95, 1),
            "p99": round(p99, 1),
            "last": round(self.last_values.get(metric_name, 0.0), 1),
            "count": n,
        }
        self._cached_stats[metric_name] = res
        return res

    def get_summary(self):
        return {m: self.get_percentiles(m) for m in self.METRICS}

    def evaluate_execution_health(self, max_last_api_ms=3500.0, max_p95_api_ms=5000.0, max_cycle_ms=8500.0):
        api_stats = self.get_percentiles("api_ms")
        cyc_stats = self.get_percentiles("cycle_ms")
        last_api = api_stats["last"]
        p95_api = api_stats["p95"]
        last_cyc = cyc_stats["last"]

        if last_api > max_last_api_ms:
            return True, f"LATENCY_API_SPIKE(last={last_api:.0f}ms>{max_last_api_ms:.0f}ms)"
        if api_stats["count"] >= 8 and p95_api > max_p95_api_ms:
            return True, f"LATENCY_API_P95_DEGRADED(p95={p95_api:.0f}ms>{max_p95_api_ms:.0f}ms)"
        if last_cyc > max_cycle_ms:
            return True, f"LATENCY_CYCLE_DEGRADED(cycle={last_cyc:.0f}ms>{max_cycle_ms:.0f}ms)"
        return False, "LATENCY_OK"

    def format_compact_tag(self):
        api_s = self.get_percentiles("api_ms")
        cyc_s = self.get_percentiles("cycle_ms")
        return f"API:{api_s['last']:.0f}ms(P50:{api_s['p50']:.0f}|P95:{api_s['p95']:.0f}|P99:{api_s['p99']:.0f})|CycP95:{cyc_s['p95']:.0f}ms"


class InFlightOrderGuard:
    """
    Previene órdenes duplicadas tras una interrupción de conexión o timeout de lectura
    en POST /v3/futures/isolated/trade.
    """
    def __init__(self, ttl_sec=20.0):
        self.ttl_sec = float(ttl_sec)
        self.pending_orders = {}

    def _cleanup(self, now):
        expired = [k for k, v in self.pending_orders.items() if (now - v["ts"]) > self.ttl_sec]
        for k in expired:
            self.pending_orders.pop(k, None)

    def can_send_order(self, side, margin_sats, leverage, running_trades=None, now=None):
        now = float(now if now is not None else time.time())
        self._cleanup(now)
        norm_side = str(side).lower().strip()

        for fp, meta in self.pending_orders.items():
            if meta["side"] == norm_side and (now - meta["ts"]) <= self.ttl_sec:
                return False, f"DUPLICATE_INFLIGHT_BLOCKED(fp={fp[:10]}|age={now-meta['ts']:.1f}s)"

        if isinstance(running_trades, list):
            for t in running_trades:
                if not isinstance(t, dict):
                    continue
                t_side = str(t.get("side", "")).lower()
                t_margin = int(t.get("margin") or 0)
                t_lev = int(round(float(t.get("leverage") or 0)))
                created_ts = float(t.get("created_at_ts") or 0.0)
                if (
                    t_side == norm_side
                    and abs(t_margin - int(margin_sats)) <= 25
                    and abs(t_lev - int(leverage)) <= 2
                    and created_ts > 0.0
                    and (now - created_ts) <= self.ttl_sec
                ):
                    return False, f"DUPLICATE_LIVE_TRADE_DETECTED(id={str(t.get('id',''))[:6]})"
        return True, "ORDER_ALLOWED"

    def register_intent(self, side, margin_sats, leverage, state_key="", now=None):
        now = float(now if now is not None else time.time())
        self._cleanup(now)
        norm_side = str(side).lower().strip()
        raw = f"{norm_side}:{int(margin_sats)}:{int(leverage)}:{state_key}:{int(now // 10)}"
        fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        self.pending_orders[fp] = {
            "side": norm_side,
            "margin": int(margin_sats),
            "leverage": int(leverage),
            "state": str(state_key),
            "ts": now,
        }
        return fp

    def clear_intent(self, fp=None):
        if fp is None:
            self.pending_orders.clear()
        else:
            self.pending_orders.pop(fp, None)


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
    """
    Pool de conexiones persistentes Keep-Alive, control de concurrencia HTTP y
    timeouts independientes para solicitudes GET rápidas, GET autenticadas y POST/PUT críticas.
    """
    TIMEOUT_GET_FAST = (2.0, 3.8)
    TIMEOUT_GET_AUTH = (2.8, 6.0)
    TIMEOUT_POST_CRITICAL = (3.5, 8.0)
    TIMEOUT_PUT_CRITICAL = (3.0, 6.5)
    _CONCURRENCY_SEM = threading.BoundedSemaphore(4)

    @staticmethod
    def build_session(user_agent="LNMSniper/7.3-Dual"):
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=16, max_retries=0, pool_block=True)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=30, max=200",
            "Accept": "application/json",
            "User-Agent": user_agent,
        })
        return session

    @classmethod
    def get_timeout_for(cls, method, path=""):
        m = str(method).upper()
        if m == "GET":
            if "/ticker" in str(path):
                return cls.TIMEOUT_GET_FAST
            return cls.TIMEOUT_GET_AUTH
        if m == "PUT":
            return cls.TIMEOUT_PUT_CRITICAL
        return cls.TIMEOUT_POST_CRITICAL

    @classmethod
    def acquire_slot(cls, timeout=4.0):
        return cls._CONCURRENCY_SEM.acquire(timeout=timeout)

    @classmethod
    def release_slot(cls):
        try:
            cls._CONCURRENCY_SEM.release()
        except ValueError:
            pass

    @staticmethod
    def compute_backoff(attempt, base=0.55, cap=5.0):
        return min(cap, (base * (1.75 ** attempt)) + random.uniform(0.06, 0.24))


def rotate_log_file_if_needed(log_path, max_bytes=5 * 1024 * 1024, max_backups=3):
    """Rotación ligera de archivos de log para entornos Termux/Linux con almacenamiento limitado."""
    if not log_path or not os.path.exists(log_path):
        return False
    try:
        if os.path.getsize(log_path) < max_bytes:
            return False
        for idx in range(max_backups - 1, 0, -1):
            src = f"{log_path}.{idx}"
            dst = f"{log_path}.{idx + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        shutil.copy2(log_path, f"{log_path}.1")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[LOG ROTATED AT {time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
        return True
    except Exception:
        return False


class _FloatArray(list):
    """Vector numérico ligero en Python puro para fallback cuando numpy no está instalado."""
    def __init__(self, seq=()):
        super().__init__(float(x) for x in seq)

    def copy(self):
        return _FloatArray(self)

    def tolist(self):
        return list(self)

    def sum(self):
        return float(sum(self))

    def __getitem__(self, idx):
        if isinstance(idx, list):
            return _FloatArray(x for x, flag in zip(self, idx) if flag)
        res = super().__getitem__(idx)
        return _FloatArray(res) if isinstance(res, list) else res

    def __neg__(self):
        return _FloatArray(-x for x in self)

    def __add__(self, other):
        if isinstance(other, (list, tuple)):
            return _FloatArray(a + float(b) for a, b in zip(self, other))
        val = float(other)
        return _FloatArray(a + val for a in self)

    def __radd__(self, other):
        return self.__add__(other)

    def __iadd__(self, other):
        if isinstance(other, (list, tuple)):
            for i in range(len(self)):
                self[i] += float(other[i])
        else:
            val = float(other)
            for i in range(len(self)):
                self[i] += val
        return self

    def __sub__(self, other):
        if isinstance(other, (list, tuple)):
            return _FloatArray(a - float(b) for a, b in zip(self, other))
        val = float(other)
        return _FloatArray(a - val for a in self)

    def __mul__(self, other):
        if isinstance(other, (list, tuple)):
            return _FloatArray(a * float(b) for a, b in zip(self, other))
        val = float(other)
        return _FloatArray(a * val for a in self)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __truediv__(self, other):
        if isinstance(other, (list, tuple)):
            return _FloatArray(a / float(b) for a, b in zip(self, other))
        val = float(other)
        return _FloatArray(a / val for a in self)

    def __itruediv__(self, other):
        val = float(other)
        for i in range(len(self)):
            self[i] /= val
        return self

    def __ge__(self, val):
        v = float(val)
        return [x >= v for x in self]

    def __lt__(self, val):
        v = float(val)
        return [x < v for x in self]


class _CompatRandom:
    @staticmethod
    def choice(n, size=1, p=None, replace=False):
        population = list(range(int(n))) if isinstance(n, int) else list(n)
        k = min(int(size), len(population))
        if replace or p is None:
            return random.sample(population, k) if not replace else random.choices(population, weights=p, k=k)
        weights = [max(0.0, float(w)) for w in p]
        chosen = []
        avail_idx = list(range(len(population)))
        for _ in range(k):
            tot = sum(weights[i] for i in avail_idx)
            if tot <= 0:
                pick = random.choice(avail_idx)
            else:
                r = random.uniform(0.0, tot)
                acc = 0.0
                pick = avail_idx[-1]
                for i in avail_idx:
                    acc += weights[i]
                    if r <= acc:
                        pick = i
                        break
            chosen.append(population[pick])
            avail_idx.remove(pick)
        return chosen


class _CompatNumpy:
    float64 = float
    random = _CompatRandom()

    @staticmethod
    def array(seq, dtype=float):
        return _FloatArray(seq)

    @staticmethod
    def full(n, val, dtype=float):
        return _FloatArray([float(val)] * int(n))

    @staticmethod
    def diff(seq):
        return _FloatArray(float(seq[i + 1]) - float(seq[i]) for i in range(len(seq) - 1))

    @staticmethod
    def mean(seq):
        return float(sum(seq)) / max(1, len(seq))

    @staticmethod
    def sum(seq):
        return float(sum(seq))

    @staticmethod
    def std(seq):
        n = len(seq)
        if n == 0:
            return 0.0
        m = float(sum(seq)) / n
        return math.sqrt(sum((float(x) - m) ** 2 for x in seq) / n)

    @staticmethod
    def maximum(a, b):
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return _FloatArray(max(float(x), float(y)) for x, y in zip(a, b))
        return max(float(a), float(b))

    @staticmethod
    def abs(seq):
        if isinstance(seq, (list, tuple)):
            return _FloatArray(abs(float(x)) for x in seq)
        return abs(float(seq))

    @staticmethod
    def argmax(seq):
        if not seq:
            return 0
        return int(max(range(len(seq)), key=lambda i: float(seq[i])))

    @staticmethod
    def sqrt(val):
        return math.sqrt(float(val))

    @staticmethod
    def clip(val, low, high):
        return max(float(low), min(float(high), float(val)))


compat_np = _CompatNumpy()

