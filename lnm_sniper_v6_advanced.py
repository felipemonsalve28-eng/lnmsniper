from sniper_perception_sats_core import (
    ShortTermPerceptionEngine,
    SatsExpectancyGate,
    sync_brain_floor_with_reality,
)
PERCEPTION_ENGINE = ShortTermPerceptionEngine()
import os
import re
import glob
import time
from guardian_execution_core import AtomicBrainStore, ResilientLNMClient, HardFloorGuardian
import json
import hmac
import shutil
import random
import hashlib
import base64
import urllib.parse
from collections import deque
from datetime import datetime, date

import requests
import numpy as np


# =====================================================================
# 1. CONFIGURACIÓN CENTRALIZADA Y PARÁMETROS DEL SISTEMA
# =====================================================================
class Config:
    BASE_URL = "https://api.lnmarkets.com/v3"
    BRAIN_FILE = "q_brain_advanced_lnm.json"
    BRAIN_SAVE_INTERVAL = 90.0

    # Meta dinámica e interés compuesto con piso protegido
    INITIAL_TARGET_SATS = 300000
    TARGET_STEP_MULTIPLIER = 1.50
    MILESTONE_FLOOR_RATIO = 0.88

    # Gestión de apalancamiento, riesgo y concurrencia estricta
    MIN_LEVERAGE = 15
    MAX_LEVERAGE = 95
    TAKER_FEE = 0.001
    WARMUP_TRADES = 20
    RISK_WARMUP = 0.06
    RISK_NORMAL = 0.12
    MIN_EFFECTIVE_RISK_WARMUP = 0.025
    MIN_EFFECTIVE_RISK_NORMAL = 0.032
    MAX_CONCURRENT_TRADES = 5
    SOFT_EXPOSURE_LIMIT = 0.38
    ENTRY_COOLDOWN_SEC = 40.0

    # Aprendizaje intermedio online selectivo (bajo ruido)
    ENABLE_INTERMEDIATE_LEARNING = True
    INTERMEDIATE_EVAL_INTERVAL_SEC = 240.0
    ALPHA_INTERMEDIATE = 0.020
    INTERMEDIATE_DECAY_PER_EVAL = 0.82
    MAX_INTERMEDIATE_REWARD_CLIP = 0.12
    MIN_INTERMEDIATE_DELTA_ATR = 0.18

    # Umbrales de seguridad y recuperación auditable (escala decimal)
    MAX_DAILY_LOSS_PCT = 0.11
    MAX_DRAWDOWN_PCT = 0.16
    CONSECUTIVE_LOSS_LIMIT = 4
    CONSECUTIVE_LOSS_PAUSE_SEC = 6 * 60

    RECOVERY_ENTER_DAILY = -0.075
    RECOVERY_EXIT_DAILY = -0.045
    RECOVERY_ENTER_DD = 0.095
    RECOVERY_EXIT_DD = 0.065
    RECOVERY_BOUNCE_GAIN_PCT = 0.018
    RECOVERY_RISK_MULT = 0.50
    RECOVERY_RR_MULT = 0.88

    ATR_EXTREME_PCT = 0.0145
    ANTI_MARTINGALE_FACTOR = 0.78
    DRAWDOWN_RISK_REDUCTION = 0.75
    DRAWDOWN_RISK_THRESHOLD = 0.09

    # Salud de red y TTL de caché MTF (210 velas para EMA 200 exacta)
    API_CB_ERROR_THRESHOLD = 4
    API_CB_MAX_COOLDOWN_SEC = 180
    CANDLE_LIMIT = 210
    TTL_5M = 45.0
    TTL_15M = 120.0
    TTL_1H = 360.0
    TTL_4H = 720.0

    NUM_ACTIONS = 9
    NUM_MACRO_ACTIONS = 4


# =====================================================================
# 2. CARGADOR SEGURO DE CREDENCIALES
# =====================================================================
def load_saved_credentials():
    k = os.getenv("LNM_API_KEY") or os.getenv("API_KEY") or ""
    s = os.getenv("LNM_API_SECRET") or os.getenv("API_SECRET") or ""
    p = os.getenv("LNM_PASSPHRASE") or os.getenv("API_PASSPHRASE") or ""
    if k and s and p and "TU_API" not in k:
        return k.strip(), s.strip(), p.strip()

    current_script = os.path.basename(__file__) if "__file__" in globals() else "lnm_sniper_v6_advanced.py"
    search_files = [
        ".env",
        os.path.expanduser("~/.env"),
        os.path.expanduser("~/.bashrc"),
    ] + [f for f in glob.glob("*.py") if f != current_script]

    for filepath in search_files:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not k or "TU_API" in k:
                    m_k = re.search(r'(?:LNM_API_KEY|API_KEY)\s*[=:]\s*["\']([^"\']+)["\']', content)
                    if m_k and "TU_API" not in m_k.group(1):
                        k = m_k.group(1)
                if not s or "TU_API" in s:
                    m_s = re.search(r'(?:LNM_API_SECRET|API_SECRET)\s*[=:]\s*["\']([^"\']+)["\']', content)
                    if m_s and "TU_API" not in m_s.group(1):
                        s = m_s.group(1)
                if not p or "TU_PASSPHRASE" in p:
                    m_p = re.search(r'(?:LNM_PASSPHRASE|API_PASSPHRASE)\s*[=:]\s*["\']([^"\']+)["\']', content)
                    if m_p and "TU_PASSPHRASE" not in m_p.group(1):
                        p = m_p.group(1)
            except Exception:
                continue
    return k.strip(), s.strip(), p.strip()


# =====================================================================
# 3. MONITOR DE SALUD DE API Y CIRCUIT BREAKER DE RED
# =====================================================================
class APIHealthMonitor:
    def __init__(self):
        self.consecutive_errors = 0
        self.total_errors = 0
        self.cooldown_until = 0.0
        self.last_err_signature = ""
        self.repeat_count = 0

    def record_success(self):
        if self.consecutive_errors > 0:
            print(f"[SALUD API] Conexión restablecida tras {self.consecutive_errors} fallo(s).")
        self.consecutive_errors = 0
        self.last_err_signature = ""
        self.repeat_count = 0

    def record_error(self, category, err_obj):
        now = time.time()
        self.consecutive_errors += 1
        self.total_errors += 1
        msg = str(err_obj).strip().replace("\n", " ")[:140]
        signature = f"{category}:{msg[:60]}"

        if signature == self.last_err_signature:
            self.repeat_count += 1
            if self.repeat_count % 3 == 0:
                print(f"[ERROR {category}] (x{self.repeat_count + 1} consec:{self.consecutive_errors}) {msg}")
        else:
            self.last_err_signature = signature
            self.repeat_count = 0
            print(f"[ERROR {category}] (consec:{self.consecutive_errors} | total:{self.total_errors}) {msg}")

        if self.consecutive_errors >= Config.API_CB_ERROR_THRESHOLD:
            exponent = min(3, self.consecutive_errors - Config.API_CB_ERROR_THRESHOLD)
            pause_sec = min(Config.API_CB_MAX_COOLDOWN_SEC, int(25 * (1.7 ** exponent)))
            self.cooldown_until = now + pause_sec
            print(f"[API CIRCUIT BREAKER] {self.consecutive_errors} errores seguidos → Pausa de {pause_sec}s.")

    def check_cooldown(self):
        remaining = self.cooldown_until - time.time()
        if remaining > 0:
            time.sleep(min(remaining, 12.0))
            return True
        return False


# =====================================================================
# 4. GESTOR DE CACHÉ MULTI-TEMPORALIDAD CON CONEXIÓN PERSISTENTE
# =====================================================================
class MultiTimeframeCache:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Connection": "keep-alive", "User-Agent": "LNMSniper/7.3-Unified"})
        self.cache = {
            "5m":  {"last": 0.0, "ttl": Config.TTL_5M,  "period": 300,   "data": None},
            "15m": {"last": 0.0, "ttl": Config.TTL_15M, "period": 900,   "data": None},
            "1h":  {"last": 0.0, "ttl": Config.TTL_1H,  "period": 3600,  "data": None},
            "4h":  {"last": 0.0, "ttl": Config.TTL_4H,  "period": 14400, "data": None},
        }

    def invalidate(self, tf):
        if tf in self.cache:
            self.cache[tf]["last"] = 0.0

    def get_candles(self, tf, current_lnm_price=0.0):
        now = time.time()
        item = self.cache[tf]
        crossed_boundary = (int(now) // item["period"]) != (int(item["last"]) // item["period"])

        if item["data"] is None or (now - item["last"]) >= item["ttl"] or crossed_boundary:
            try:
                h, l, c = self._fetch_remote(tf)
                item["data"] = (h, l, c)
                item["last"] = now
            except Exception as e:
                if item["data"] is None:
                    raise RuntimeError(f"Fallo crítico obteniendo velas TF {tf}: {e}")
                if int(now) % 60 < 15:
                    print(f"[CACHE TF {tf}] Usando respaldo en memoria por fallo de red: {e}")

        h_arr, l_arr, c_arr = item["data"]
        if tf in ("5m", "15m") and current_lnm_price > 0:
            h_c, l_c, c_c = h_arr.copy(), l_arr.copy(), c_arr.copy()
            c_c[-1] = current_lnm_price
            if current_lnm_price > h_c[-1]:
                h_c[-1] = current_lnm_price
            if current_lnm_price < l_c[-1]:
                l_c[-1] = current_lnm_price
            return h_c, l_c, c_c
        return h_arr, l_arr, c_arr

    def _fetch_remote(self, tf):
        limit = Config.CANDLE_LIMIT
        map_binance = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
        interval = map_binance.get(tf, "5m")
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
            r = self.session.get(url, timeout=5)
            r.raise_for_status()
            data = r.json()
            if len(data) < 50:
                raise RuntimeError("Respuesta incompleta de Binance")
            return (
                np.array([float(c[2]) for c in data], dtype=np.float64),
                np.array([float(c[3]) for c in data], dtype=np.float64),
                np.array([float(c[4]) for c in data], dtype=np.float64),
            )
        except Exception:
            map_bybit = {"5m": "5", "15m": "15", "1h": "60", "4h": "240"}
            by_interval = map_bybit.get(tf, "5")
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval={by_interval}&limit={limit}"
            r = self.session.get(url, timeout=5)
            r.raise_for_status()
            rows = list(reversed(r.json()["result"]["list"]))
            if len(rows) < 50:
                raise RuntimeError("Respuesta incompleta de Bybit")
            return (
                np.array([float(c[2]) for c in rows], dtype=np.float64),
                np.array([float(c[3]) for c in rows], dtype=np.float64),
                np.array([float(c[4]) for c in rows], dtype=np.float64),
            )


# =====================================================================
# 5. MOTOR VECTORIZADO DE INDICADORES (EMA 9/21/50/200, RSI, BB, ATR)
# =====================================================================
def calc_ema(series, period):
    n = len(series)
    if n == 0:
        return 0.0
    if n < period:
        period = max(1, n)
    alpha = 2.0 / (period + 1.0)
    ema = float(series[0])
    one_minus_alpha = 1.0 - alpha
    for i in range(1, n):
        ema = alpha * float(series[i]) + one_minus_alpha * ema
    return ema


def compute_indicators(highs, lows, closes, lnm_price=0.0):
    deltas = np.diff(closes)
    seed = deltas[:14]
    up = float(seed[seed >= 0].sum()) / 14.0
    down = float(-seed[seed < 0].sum()) / 14.0
    for delta in deltas[14:]:
        d = float(delta)
        up = (up * 13.0 + (d if d > 0.0 else 0.0)) / 14.0
        down = (down * 13.0 + (-d if d < 0.0 else 0.0)) / 14.0
    rsi = 100.0 - (100.0 / (1.0 + (up / max(down, 1e-9))))

    tail20 = closes[-20:]
    sma20 = float(np.mean(tail20))
    std20 = float(np.std(tail20))
    upper_bb, lower_bb = sma20 + 2.0 * std20, sma20 - 2.0 * std20
    bb_width_pct = (upper_bb - lower_bb) / max(sma20, 1.0)
    bb_pos = (float(closes[-1]) - lower_bb) / max((upper_bb - lower_bb), 1e-5)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    atr = calc_ema(tr, 14)
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)

    ref_price = float(lnm_price if lnm_price > 0 else closes[-1])
    atr_pct = atr / max(ref_price, 1.0)
    trend_spread_pct = abs(ema9 - ema21) / max(ref_price, 1.0)
    ema_macro_spread_pct = (ema50 - ema200) / max(ref_price, 1.0)
    price_vs_ema200_pct = (ref_price - ema200) / max(ref_price, 1.0)

    if ema50 > ema200 and ref_price >= ema50:
        ema_state, ema_score = "BULL_STRONG", 1.0
    elif ema50 > ema200 and ref_price < ema50:
        ema_state, ema_score = "BULL_PULLBACK", 0.35
    elif ema50 < ema200 and ref_price <= ema50:
        ema_state, ema_score = "BEAR_STRONG", -1.0
    elif ema50 < ema200 and ref_price > ema50:
        ema_state, ema_score = "BEAR_BOUNCE", -0.35
    else:
        ema_state, ema_score = "EMA_FLAT", 0.0

    if atr_pct > 0.0075 and trend_spread_pct < 0.0018:
        regime = "CHOP_HIGHVOL"
    elif bb_width_pct < 0.0085 or atr_pct < 0.0022:
        regime = "RANGE_SQUEEZE"
    elif ema9 > ema21 and trend_spread_pct >= 0.0011:
        regime = "TREND_BULL"
    elif ema9 < ema21 and trend_spread_pct >= 0.0011:
        regime = "TREND_BEAR"
    else:
        regime = "RANGE_NORMAL"

    return {
        "price": ref_price,
        "rsi": float(rsi),
        "bb_pos": float(bb_pos),
        "bb_width_pct": float(bb_width_pct),
        "atr": float(atr),
        "atr_pct": float(atr_pct),
        "ema9": float(ema9),
        "ema21": float(ema21),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "ema_state": ema_state,
        "ema_score": float(ema_score),
        "ema_macro_spread_pct": float(ema_macro_spread_pct),
        "price_vs_ema200_pct": float(price_vs_ema200_pct),
        "regime": regime,
    }


# =====================================================================
# 6. CEREBRO AUTÓNOMO Q-LEARNING CON POLÍTICA DE SALIDA INTEGRADA
# =====================================================================
class AutonomousGoalBrain:
    def __init__(self, filename=Config.BRAIN_FILE):
        self.filename = filename
        self.alpha = 0.22
        self.alpha_intermediate = Config.ALPHA_INTERMEDIATE
        self.alpha_macro = 0.18
        self.gamma = 0.90
        self.epsilon = 0.40
        self.epsilon_min = 0.05
        self.epsilon_decay = 0.92
        self.batch_size = 18

        self.total_trained = 0
        self.total_intermediate_updates = 0
        self.current_target = Config.INITIAL_TARGET_SATS
        self.milestone_level = 1
        self.locked_floor = 0

        self.q_table = {}
        self.state_visits = {}
        self.memory = deque(maxlen=2200)
        self.active_trades = {}

        self.q_table_1h = {}
        self.q_table_4h = {}

        self.macro_strategies = {
            0: {"name": "RANGE_HARVEST",  "long_bias": 0.000, "short_bias": 0.000, "rev_bias": 0.018, "rr_m": 0.90, "risk_m": 0.92},
            1: {"name": "BULL_EXPANSION", "long_bias": 0.022, "short_bias": -0.012, "rev_bias": 0.005, "rr_m": 1.12, "risk_m": 1.04},
            2: {"name": "BEAR_EXPANSION", "long_bias": -0.012, "short_bias": 0.022, "rev_bias": 0.005, "rr_m": 1.12, "risk_m": 1.04},
            3: {"name": "VOL_SHIELD",     "long_bias": -0.008, "short_bias": -0.008, "rev_bias": 0.015, "rr_m": 0.82, "risk_m": 0.82},
        }

        self.cycle_state = {
            "1h": {"bucket": 0, "start_eq": 0, "start_price": 0.0, "trades": 0, "wins": 0, "pnl": 0, "state": None, "action": 0},
            "4h": {"bucket": 0, "start_eq": 0, "start_price": 0.0, "trades": 0, "wins": 0, "pnl": 0, "state": None, "action": 0},
        }

        # Estrategias de la IA: definen TANTO la entrada COMO la política del Guardián de salida
        self.strategies = {
            1: {
                "side": "buy", "style": "TREND", "sl_atr": 2.0, "rr": 4.2, "lev_f": 0.65, "name": "LONG_TREND",
                "trail_trig_atr": 1.15, "trail_slack_atr": 0.95, "exit_mode": "RUNNER", "harvest_atr": 1.85, "exhaust_sens": "LOW",
            },
            2: {
                "side": "buy", "style": "REVERSION", "sl_atr": 1.6, "rr": 2.4, "lev_f": 0.75, "name": "LONG_REVERSION",
                "trail_trig_atr": 0.75, "trail_slack_atr": 0.55, "exit_mode": "LOCK", "harvest_atr": 0.80, "exhaust_sens": "HIGH",
            },
            3: {
                "side": "buy", "style": "SNIPER", "sl_atr": 1.2, "rr": 3.2, "lev_f": 1.00, "name": "LONG_SNIPER",
                "trail_trig_atr": 0.88, "trail_slack_atr": 0.68, "exit_mode": "HYBRID", "harvest_atr": 1.15, "exhaust_sens": "MED",
            },
            4: {
                "side": "buy", "style": "DEF_SCALP", "sl_atr": 1.4, "rr": 2.0, "lev_f": 0.42, "name": "LONG_DEF_SCALP",
                "trail_trig_atr": 0.60, "trail_slack_atr": 0.42, "exit_mode": "LOCK", "harvest_atr": 0.65, "exhaust_sens": "MAX",
            },
            5: {
                "side": "sell", "style": "TREND", "sl_atr": 2.0, "rr": 4.2, "lev_f": 0.65, "name": "SHORT_TREND",
                "trail_trig_atr": 1.15, "trail_slack_atr": 0.95, "exit_mode": "RUNNER", "harvest_atr": 1.85, "exhaust_sens": "LOW",
            },
            6: {
                "side": "sell", "style": "REVERSION", "sl_atr": 1.6, "rr": 2.4, "lev_f": 0.75, "name": "SHORT_REVERSION",
                "trail_trig_atr": 0.75, "trail_slack_atr": 0.55, "exit_mode": "LOCK", "harvest_atr": 0.80, "exhaust_sens": "HIGH",
            },
            7: {
                "side": "sell", "style": "SNIPER", "sl_atr": 1.2, "rr": 3.2, "lev_f": 1.00, "name": "SHORT_SNIPER",
                "trail_trig_atr": 0.88, "trail_slack_atr": 0.68, "exit_mode": "HYBRID", "harvest_atr": 1.15, "exhaust_sens": "MED",
            },
            8: {
                "side": "sell", "style": "DEF_SCALP", "sl_atr": 1.4, "rr": 2.0, "lev_f": 0.42, "name": "SHORT_DEF_SCALP",
                "trail_trig_atr": 0.60, "trail_slack_atr": 0.42, "exit_mode": "LOCK", "harvest_atr": 0.65, "exhaust_sens": "MAX",
            },
        }

        self.store = AtomicBrainStore(self.filename)
        self.dirty = False
        self.last_save_time = 0.0
        self.load_brain()

    @staticmethod
    def _init_q_vector(state_key):
        q = [0.01] * Config.NUM_ACTIONS
        parts = state_key.split("|")
        regime = parts[0] if len(parts) > 0 else "RANGE_NORMAL"
        osc = parts[1] if len(parts) > 1 else "MID"
        macro = parts[3] if len(parts) > 3 else "CHOP"
        ema_ctx = parts[4] if len(parts) > 4 else "EMA_TRANS"

        if regime == "TREND_BULL" or macro == "BULL" or ema_ctx == "FULL_BULL":
            q[1], q[3], q[2] = 0.065, 0.060, 0.045
        elif regime == "TREND_BEAR" or macro == "BEAR" or ema_ctx == "FULL_BEAR":
            q[5], q[7], q[6] = 0.065, 0.060, 0.045
        elif osc == "DIP" or ema_ctx == "BULL_DIP":
            q[2], q[3], q[4] = 0.065, 0.055, 0.050
        elif osc == "PEAK" or ema_ctx == "BEAR_RALLY":
            q[6], q[7], q[8] = 0.065, 0.055, 0.050
        else:
            q[2], q[4], q[6], q[8] = 0.045, 0.045, 0.045, 0.045
        return [round(v, 4) for v in q]

    @staticmethod
    def _init_macro_q_vector(macro_state_key):
        q = [0.02] * Config.NUM_MACRO_ACTIONS
        if "TREND_BULL" in macro_state_key or "BULL_STRONG" in macro_state_key:
            q[1] = 0.08
        elif "TREND_BEAR" in macro_state_key or "BEAR_STRONG" in macro_state_key:
            q[2] = 0.08
        elif "CHOP_HIGHVOL" in macro_state_key or "LOSS" in macro_state_key:
            q[3] = 0.08
        else:
            q[0] = 0.07
        return [round(v, 4) for v in q]

    def _migrate_q_vector(self, vec):
        if not isinstance(vec, list):
            return [0.01] * Config.NUM_ACTIONS
        if len(vec) == Config.NUM_ACTIONS:
            return [float(x) for x in vec]
        new_vec = [0.01] * Config.NUM_ACTIONS
        for i in range(min(len(vec), Config.NUM_ACTIONS)):
            new_vec[i] = float(vec[i])
        return new_vec

    @staticmethod
    def _normalize_state_key(key):
        parts = str(key).split("|")
        if len(parts) == 5:
            return key
        if len(parts) == 4:
            ema_default = "FULL_BULL" if parts[3] == "BULL" else ("FULL_BEAR" if parts[3] == "BEAR" else "EMA_TRANS")
            return f"{parts[0]}|{parts[1]}|{parts[2]}|{parts[3]}|{ema_default}"
        if len(parts) == 3:
            return f"{parts[0]}|{parts[1]}|{parts[2]}|CHOP|EMA_TRANS"
        return "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS"

    def load_brain(self):
        candidates = [self.filename, f"{self.filename}.bak"]
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    raw_q = data.get("q_table", data if isinstance(data, dict) else {})
                    raw_visits = data.get("state_visits", {})

                    accum_q, accum_counts, new_visits = {}, {}, {}
                    for old_k, vec in raw_q.items():
                        if not isinstance(vec, list):
                            continue
                        migrated_v = np.array(self._migrate_q_vector(vec), dtype=np.float64)
                        norm_k = self._normalize_state_key(old_k)
                        v_cnt = max(1, int(raw_visits.get(old_k, 1)))
                        if norm_k not in accum_q:
                            accum_q[norm_k] = migrated_v * v_cnt
                            accum_counts[norm_k] = v_cnt
                            new_visits[norm_k] = v_cnt
                        else:
                            accum_q[norm_k] += migrated_v * v_cnt
                            accum_counts[norm_k] += v_cnt
                            new_visits[norm_k] += v_cnt

                    self.q_table = {
                        k: [round(float(x), 4) for x in (accum_q[k] / accum_counts[k])]
                        for k in accum_q
                    }
                    for k in self.q_table:
                        self.q_table[k][0] = min(0.02, self.q_table[k][0])

                    self.state_visits = new_visits
                    self.q_table_1h = data.get("q_table_1h", {})
                    self.q_table_4h = data.get("q_table_4h", {})

                    saved_cycles = data.get("cycle_state", {})
                    for tf in ("1h", "4h"):
                        if tf in saved_cycles and isinstance(saved_cycles[tf], dict):
                            for k in self.cycle_state[tf]:
                                if k in saved_cycles[tf]:
                                    self.cycle_state[tf][k] = saved_cycles[tf][k]

                    self.epsilon = float(data.get("epsilon", self.epsilon))
                    self.total_trained = int(data.get("total_trained", 0))
                    self.total_intermediate_updates = int(data.get("total_intermediate_updates", 0))
                    self.current_target = int(data.get("current_target", Config.INITIAL_TARGET_SATS))
                    self.milestone_level = int(data.get("milestone_level", 1))
                    self.locked_floor = int(data.get("locked_floor", 0))
                    self.active_trades = data.get("active_trades", {})

                    for item in data.get("memory", []):
                        if isinstance(item, (list, tuple)) and len(item) == 5:
                            p, s, a, r, ns = item
                            self.memory.append((
                                float(p),
                                self._normalize_state_key(s),
                                int(a) % Config.NUM_ACTIONS,
                                float(r),
                                self._normalize_state_key(ns),
                            ))
                    print(
                        f"[CEREBRO CARGADO] Estados: {len(self.q_table)} | "
                        f"1h:{len(self.q_table_1h)} | 4h:{len(self.q_table_4h)} | "
                        f"Cierres: {self.total_trained} | Online: {self.total_intermediate_updates}"
                    )
                    return
                except Exception as e:
                    print(f"[AVISO I/O] Error leyendo {path}: {e}")

    def save_brain(self, force=False):
        now = time.time()
        if not force and not self.dirty:
            return
        if not force and (now - self.last_save_time) < Config.BRAIN_SAVE_INTERVAL:
            return
        try:
            payload = {
                "epsilon": round(self.epsilon, 4),
                "total_trained": self.total_trained,
                "total_intermediate_updates": self.total_intermediate_updates,
                "current_target": self.current_target,
                "milestone_level": self.milestone_level,
                "locked_floor": self.locked_floor,
                "q_table": self.q_table,
                "q_table_1h": self.q_table_1h,
                "q_table_4h": self.q_table_4h,
                "cycle_state": self.cycle_state,
                "state_visits": self.state_visits,
                "active_trades": self.active_trades,
                "memory": list(self.memory)[-1600:],
            }
            self.store.save(payload)
            self.dirty = False
            self.last_save_time = now
        except Exception as e:
            print(f"[ERROR I/O] Fallo al guardar cerebro: {e}")

    def check_and_advance_goal(self, equity):
        advanced = False
        while equity >= self.current_target:
            old_target = self.current_target
            self.locked_floor = int(round(old_target * Config.MILESTONE_FLOOR_RATIO))
            self.current_target = int(round(old_target * Config.TARGET_STEP_MULTIPLIER))
            self.milestone_level += 1
            self.dirty = True
            advanced = True
            print(
                f"  [META CONQUISTADA] Nivel #{self.milestone_level} | "
                f"Nueva Meta: {self.current_target:,} sats | Piso Protegido: {self.locked_floor:,} sats"
            )
        if advanced:
            self.save_brain(force=True)

    def is_warming_up(self):
        return self.total_trained < Config.WARMUP_TRADES

    @staticmethod
    def discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, concurrent_count):
        rsi = (ind_5m["rsi"] * 0.60) + (ind_15m["rsi"] * 0.40)
        bb_pos = (ind_5m["bb_pos"] * 0.60) + (ind_15m["bb_pos"] * 0.40)
        regime = ind_5m["regime"]
        if regime == "RANGE_NORMAL" and ind_15m["regime"] in ("TREND_BULL", "TREND_BEAR"):
            regime = ind_15m["regime"]

        osc_z = "DIP" if (rsi < 37 or bb_pos < 0.23) else ("PEAK" if (rsi > 63 or bb_pos > 0.77) else "MID")
        conc_z = "LOW_EXP" if concurrent_count <= 2 else "HIGH_EXP"

        if ind_4h["ema9"] > ind_4h["ema21"] and ind_1h["ema9"] > ind_1h["ema21"]:
            macro_trend = "BULL"
        elif ind_4h["ema9"] < ind_4h["ema21"] and ind_1h["ema9"] < ind_1h["ema21"]:
            macro_trend = "BEAR"
        else:
            macro_trend = "CHOP"

        micro_ema_score = (ind_5m["ema_score"] * 0.5) + (ind_15m["ema_score"] * 0.5)
        macro_ema_score = (ind_1h["ema_score"] * 0.55) + (ind_4h["ema_score"] * 0.45)

        if macro_ema_score >= 0.30 and micro_ema_score >= 0.20:
            ema_struct = "FULL_BULL"
        elif macro_ema_score <= -0.30 and micro_ema_score <= -0.20:
            ema_struct = "FULL_BEAR"
        elif macro_ema_score >= 0.25 and micro_ema_score < 0.0:
            ema_struct = "BULL_DIP"
        elif macro_ema_score <= -0.25 and micro_ema_score > 0.0:
            ema_struct = "BEAR_RALLY"
        else:
            ema_struct = "EMA_TRANS"

        return f"{regime}|{osc_z}|{conc_z}|{macro_trend}|{ema_struct}"

    @staticmethod
    def discretize_macro_state(ind_tf, cycle_roi_pct):
        reg = ind_tf["regime"]
        rsi = ind_tf["rsi"]
        osc = "LOW" if rsi < 40 else ("HIGH" if rsi > 60 else "MID")
        ema_st = ind_tf.get("ema_state", "EMA_FLAT")
        perf = "WIN" if cycle_roi_pct > 0.004 else ("LOSS" if cycle_roi_pct < -0.008 else "FLAT")
        return f"{reg}|{osc}|{ema_st}|{perf}"

    def record_trade_in_cycles(self, pl_sats):
        for tf in ("1h", "4h"):
            self.cycle_state[tf]["trades"] = int(self.cycle_state[tf].get("trades", 0)) + 1
            if pl_sats > 0:
                self.cycle_state[tf]["wins"] = int(self.cycle_state[tf].get("wins", 0)) + 1
            self.cycle_state[tf]["pnl"] = int(self.cycle_state[tf].get("pnl", 0)) + int(pl_sats)
        self.dirty = True

    def get_parallel_modifiers(self, ind_5m=None, ind_15m=None, ind_1h=None, ind_4h=None):
        a_1h = int(self.cycle_state["1h"].get("action", 0)) % Config.NUM_MACRO_ACTIONS
        a_4h = int(self.cycle_state["4h"].get("action", 0)) % Config.NUM_MACRO_ACTIONS
        s_1h = self.macro_strategies[a_1h]
        s_4h = self.macro_strategies[a_4h]

        ema_long_nudge, ema_short_nudge = 0.0, 0.0
        if ind_5m and ind_15m and ind_1h and ind_4h:
            composite_ema = (
                ind_5m["ema_score"] * 0.15 +
                ind_15m["ema_score"] * 0.25 +
                ind_1h["ema_score"] * 0.30 +
                ind_4h["ema_score"] * 0.30
            )
            if composite_ema > 0:
                ema_long_nudge = round(composite_ema * 0.015, 4)
            elif composite_ema < 0:
                ema_short_nudge = round(abs(composite_ema) * 0.015, 4)

        return {
            "name_1h": s_1h["name"],
            "name_4h": s_4h["name"],
            "long_bias": round(s_1h["long_bias"] * 0.45 + s_4h["long_bias"] * 0.55 + ema_long_nudge, 4),
            "short_bias": round(s_1h["short_bias"] * 0.45 + s_4h["short_bias"] * 0.55 + ema_short_nudge, 4),
            "rev_bias": round(s_1h["rev_bias"] * 0.50 + s_4h["rev_bias"] * 0.50, 4),
            "rr_mult": round(s_1h["rr_m"] * 0.45 + s_4h["rr_m"] * 0.55, 3),
            "risk_mult": round(s_1h["risk_m"] * 0.45 + s_4h["risk_m"] * 0.55, 3),
        }

    def evaluate_and_close_macro_cycle(self, tf, current_bucket, equity, current_price, ind_tf, running_trades):
        c_info = self.cycle_state[tf]
        prev_bucket = int(c_info.get("bucket", 0))
        q_macro = self.q_table_1h if tf == "1h" else self.q_table_4h

        if prev_bucket == 0 or c_info.get("start_eq", 0) <= 0:
            init_state = self.discretize_macro_state(ind_tf, 0.0)
            if init_state not in q_macro or len(q_macro[init_state]) != Config.NUM_MACRO_ACTIONS:
                q_macro[init_state] = self._init_macro_q_vector(init_state)
            best_act = int(np.argmax(q_macro[init_state]))
            self.cycle_state[tf] = {
                "bucket": current_bucket, "start_eq": equity, "start_price": current_price,
                "trades": 0, "wins": 0, "pnl": 0, "state": init_state, "action": best_act
            }
            self.dirty = True
            return False

        if current_bucket <= prev_bucket:
            return False

        start_eq = max(1, int(c_info.get("start_eq", equity)))
        start_price = max(1.0, float(c_info.get("start_price", current_price)))
        trades_cnt = int(c_info.get("trades", 0))
        wins_cnt = int(c_info.get("wins", 0))
        realized_pnl = int(c_info.get("pnl", 0))
        prev_state = str(c_info.get("state") or self.discretize_macro_state(ind_tf, 0.0))
        prev_action = int(c_info.get("action", 0)) % Config.NUM_MACRO_ACTIONS

        eq_delta = equity - start_eq
        cycle_roi_pct = eq_delta / start_eq
        btc_move_pct = (current_price - start_price) / start_price
        winrate_pct = (wins_cnt / trades_cnt * 100.0) if trades_cnt > 0 else 0.0

        align_bonus = 0.0
        if prev_action == 1:
            align_bonus = 0.06 if btc_move_pct > 0.0025 else (-0.05 if btc_move_pct < -0.0025 else -0.01)
        elif prev_action == 2:
            align_bonus = 0.06 if btc_move_pct < -0.0025 else (-0.05 if btc_move_pct > 0.0025 else -0.01)
        elif prev_action == 0:
            align_bonus = 0.05 if abs(btc_move_pct) <= 0.0035 else -0.03
        elif prev_action == 3:
            align_bonus = 0.05 if cycle_roi_pct >= -0.005 else -0.04

        macro_reward = float(np.clip((cycle_roi_pct * 7.5) + align_bonus, -0.38, 0.38))
        next_macro_state = self.discretize_macro_state(ind_tf, cycle_roi_pct)

        if prev_state not in q_macro or len(q_macro[prev_state]) != Config.NUM_MACRO_ACTIONS:
            q_macro[prev_state] = self._init_macro_q_vector(prev_state)
        if next_macro_state not in q_macro or len(q_macro[next_macro_state]) != Config.NUM_MACRO_ACTIONS:
            q_macro[next_macro_state] = self._init_macro_q_vector(next_macro_state)

        cur_q = q_macro[prev_state][prev_action]
        max_next_q = max(q_macro[next_macro_state])
        q_macro[prev_state][prev_action] = round(
            cur_q + self.alpha_macro * (macro_reward + self.gamma * max_next_q - cur_q), 4
        )

        self._experience_replay(self.batch_size, custom_alpha=self.alpha * 0.45)

        if random.uniform(0, 1) < max(0.08, self.epsilon * 0.5):
            new_macro_action = random.randint(0, Config.NUM_MACRO_ACTIONS - 1)
        else:
            new_macro_action = int(np.argmax(q_macro[next_macro_state]))

        prev_strat_name = self.macro_strategies[prev_action]["name"]
        new_strat_name = self.macro_strategies[new_macro_action]["name"]

        self.cycle_state[tf] = {
            "bucket": current_bucket, "start_eq": equity, "start_price": current_price,
            "trades": 0, "wins": 0, "pnl": 0, "state": next_macro_state, "action": new_macro_action
        }
        self.dirty = True
        self.save_brain(force=True)

        mods = self.get_parallel_modifiers()
        print("--------------------------------------------------------------------")
        print(f"[CIERRE VELA {tf.upper()} & CONSOLIDACIÓN MACRO] Estado: {next_macro_state}")
        print(f"  → Mercado {tf.upper()}: ΔBTC {btc_move_pct*100:+.2f}% | RSI:{ind_tf['rsi']:.1f} | EMA50/200:{ind_tf['ema_state']}")
        print(f"  → Performance Ciclo: ΔEq {eq_delta:+,} sats ({cycle_roi_pct*100:+.2f}%) | Cierres:{trades_cnt} (WR:{winrate_pct:.0f}% | PnL:{realized_pnl:+,}) | Activos:{len(running_trades)}")
        print(f"  → Estrategia Macro {tf.upper()}: {prev_strat_name} (R:{macro_reward:+.3f}) ➔ {new_strat_name}")
        print(f"  → Modificadores Activos: RR_x{mods['rr_mult']:.2f} | Risk_x{mods['risk_mult']:.2f}")
        print("--------------------------------------------------------------------")
        return True

    def get_state_q_values(self, state):
        norm_s = self._normalize_state_key(state)
        if norm_s not in self.q_table or len(self.q_table[norm_s]) != Config.NUM_ACTIONS:
            self.q_table[norm_s] = self._init_q_vector(norm_s)
            self.state_visits[norm_s] = 0
        return self.q_table[norm_s]

    def choose_action(self, state, ind_5m, ind_15m, ind_1h, ind_4h, recovery_mode=False, current_dd_pct=0.0):
        norm_s = self._normalize_state_key(state)
        q_vals = self.get_state_q_values(norm_s)
        self.state_visits[norm_s] = self.state_visits.get(norm_s, 0) + 1
        visits = self.state_visits[norm_s]
        self.dirty = True

        if recovery_mode and current_dd_pct >= 0.12:
            allowed_actions = [2, 3, 4, 6, 7, 8]
        else:
            allowed_actions = [1, 2, 3, 4, 5, 6, 7, 8]

        visit_bonus = 0.0 if (recovery_mode or current_dd_pct > 0.04) else (0.01 if visits < 4 else 0.0)
        effective_eps = 0.0 if (recovery_mode or "CHOP" in norm_s) else min(0.02, self.epsilon + visit_bonus)

        if effective_eps > 0.0 and random.uniform(0, 1) < effective_eps:
            return 0

        mods = self.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)

        def biased_q(a):
            if a == 0:
                return q_vals[0]
            strat = self.strategies[a]
            b = mods["long_bias"] if strat["side"] == "buy" else mods["short_bias"]
            if strat["style"] in ("REVERSION", "DEF_SCALP"):
                b += mods["rev_bias"]
            return q_vals[a] + b

        return int(max(allowed_actions, key=biased_q))

    def _update_q(self, s, a, r, ns, custom_alpha=None):
        norm_s = self._normalize_state_key(s)
        norm_ns = self._normalize_state_key(ns)
        if norm_s not in self.q_table or len(self.q_table[norm_s]) != Config.NUM_ACTIONS:
            self.q_table[norm_s] = self._init_q_vector(norm_s)
        if norm_ns not in self.q_table or len(self.q_table[norm_ns]) != Config.NUM_ACTIONS:
            self.q_table[norm_ns] = self._init_q_vector(norm_ns)
        a = int(a) % Config.NUM_ACTIONS
        lr = custom_alpha if custom_alpha is not None else self.alpha
        current_q = self.q_table[norm_s][a]
        max_future_q = max(self.q_table[norm_ns][1:])
        new_q = float(np.clip(current_q + lr * (r + self.gamma * max_future_q - current_q), -0.95, 0.95))
        self.q_table[norm_s][a] = round(new_q, 4)

    def _experience_replay(self, sample_count, custom_alpha=None):
        if len(self.memory) < 6:
            return
        mem_list = list(self.memory)
        weights = np.array([max(float(m[0]), 0.01) for m in mem_list], dtype=np.float64)
        weights /= weights.sum()
        sz = min(len(mem_list), sample_count)
        for idx in np.random.choice(len(mem_list), size=sz, p=weights, replace=False):
            _, s, a, r, ns = mem_list[idx]
            self._update_q(s, a, r, ns, custom_alpha=custom_alpha)

    def remember_intermediate(
        self, origin_state, action, current_state, side,
        delta_atr, guardian_mode, sl_locked, eval_count
    ):
        if abs(delta_atr) < Config.MIN_INTERMEDIATE_DELTA_ATR and not (sl_locked and eval_count <= 2):
            return 0.0

        parts = current_state.split("|")
        ema_ctx = parts[4] if len(parts) > 4 else "EMA_TRANS"
        delta_score = float(np.clip(delta_atr * 0.075, -0.09, 0.10))

        ema_aligned = (
            (side == "buy" and ema_ctx in ("FULL_BULL", "BULL_DIP")) or
            (side == "sell" and ema_ctx in ("FULL_BEAR", "BEAR_RALLY"))
        )
        if delta_atr > 0:
            ema_bonus = 0.018 if ema_aligned else 0.014
        else:
            ema_bonus = -0.005 if ema_aligned else -0.014

        guardian_score = 0.025 if (sl_locked and "RUNNER" in guardian_mode) else (0.015 if sl_locked else 0.0)
        decay = Config.INTERMEDIATE_DECAY_PER_EVAL ** min(eval_count, 10)
        raw_shaping = (delta_score + ema_bonus + guardian_score) * decay

        shaped_partial = float(np.clip(raw_shaping, -Config.MAX_INTERMEDIATE_REWARD_CLIP, Config.MAX_INTERMEDIATE_REWARD_CLIP))
        self._update_q(origin_state, action, shaped_partial, current_state, custom_alpha=self.alpha_intermediate)
        self.total_intermediate_updates += 1
        self.dirty = True
        return round(shaped_partial, 4)

    def remember_and_train(
        self, state, action, reward_sats, margin_used, next_state,
        equity_after, hit_tp=False, harvested=False, is_no_trade=False
    ):
        norm_s = self._normalize_state_key(state)
        norm_ns = self._normalize_state_key(next_state)

        if is_no_trade:
            self.q_table.setdefault(norm_s, self._init_q_vector(norm_s))
            self.q_table[norm_s][0] = min(0.012, round(self.q_table[norm_s][0] * 0.90, 4))
            return

        self.record_trade_in_cycles(reward_sats)
        roi = reward_sats / max(margin_used, 1)
        gap_to_goal = max(self.current_target - (equity_after - reward_sats), 10000)
        goal_velocity = reward_sats / gap_to_goal
        shaped = (roi * 1.15) + (goal_velocity * 4.8)

        if roi < 0:
            shaped *= 1.65
        else:
            if hit_tp:
                shaped += 0.28
            if harvested:
                shaped += 0.18
            if equity_after >= self.current_target:
                shaped += 0.65

        self._update_q(norm_s, action, shaped, norm_ns)
        self.memory.append((abs(shaped) + 2.2, norm_s, int(action), round(shaped, 4), norm_ns))
        self.total_trained += 1
        self.dirty = True

        self._experience_replay(self.batch_size)
        if self.epsilon > self.epsilon_min:
            self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        self.save_brain(force=True)


# =====================================================================
# 7. CLIENTE HTTP IDEMPOTENTE LN MARKETS
# =====================================================================
def _clean_numbers(obj):
    if isinstance(obj, dict):
        return {k: _clean_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_numbers(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


class LNMarketsClient:
    def __init__(self, key, secret, passphrase):
        self.key = key
        self.secret = secret
        self.passphrase = passphrase
        self.session = requests.Session()
        self.session.headers.update({"Connection": "keep-alive", "User-Agent": "LNMSniper/7.3-Unified"})

    def _request(self, method, path, params=None, max_retries=3):
        method_up = method.upper()
        clean_p = _clean_numbers(params) if params else None
        for attempt in range(max_retries):
            try:
                ts = str(int(time.time() * 1000))
                data_str, url = "", f"{Config.BASE_URL}{path}"
                if clean_p:
                    if method_up in ("GET", "DELETE"):
                        url += f"?{urllib.parse.urlencode(clean_p)}"
                    else:
                        data_str = json.dumps(clean_p, separators=(",", ":"))
                payload = ts + method.lower() + "/v3" + path + data_str
                signature = base64.b64encode(
                    hmac.new(self.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
                ).decode("utf-8")
                headers = {
                    "LNM-ACCESS-KEY": self.key,
                    "LNM-ACCESS-SIGNATURE": signature,
                    "LNM-ACCESS-PASSPHRASE": self.passphrase,
                    "LNM-ACCESS-TIMESTAMP": ts,
                }
                if method_up in ("POST", "PUT") and data_str:
                    headers["Content-Type"] = "application/json"
                resp = self.session.request(
                    method_up,
                    url,
                    headers=headers,
                    data=data_str if method_up in ("POST", "PUT") and data_str else None,
                    timeout=(6.0, 15.0),
                )
                if resp.status_code in (429, 500, 502, 503, 504) and max_retries > 1:
                    time.sleep((1.5 ** attempt) + random.uniform(0.2, 0.6))
                    continue
                if not resp.ok:
                    raise RuntimeError(f"HTTP {resp.status_code} ({path}): {resp.text[:140]}")
                return resp.json() if resp.text else {}
            except Exception as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Fallo en {path}: {e}")
                time.sleep((1.5 ** attempt) + 0.3)

    def get_ticker_price(self):
        t = self._request("GET", "/futures/ticker")
        return float(t.get("lastPrice") or t.get("index") or 0.0) if isinstance(t, dict) else 0.0

    def get_user(self):
        return self._request("GET", "/account")

    def get_running_trades(self):
        r = self._request("GET", "/futures/isolated/trades/running")
        rows = r.get("data", []) if isinstance(r, dict) else r
        if not isinstance(rows, list):
            return []
        unique = {}
        for row in rows:
            if isinstance(row, dict) and "id" in row:
                unique[str(row["id"])] = row
        return list(unique.values())

    def get_closed_trades(self, limit=25):
        r = self._request("GET", "/futures/isolated/trades/closed")
        rows = r.get("data", []) if isinstance(r, dict) else r
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict) and "id" in row][:limit]

    def open_trade(self, side, margin, leverage, stoploss, takeprofit):
        return self._request(
            "POST",
            "/futures/isolated/trade",
            {
                "type": "market",
                "side": side,
                "margin": int(margin),
                "leverage": int(leverage),
                "stoploss": int(round(stoploss)),
                "takeprofit": int(round(takeprofit)),
            },
            max_retries=1,
        )

    def update_stoploss(self, trade_id, new_sl):
        return self._request(
            "PUT",
            "/futures/isolated/trade/stoploss",
            {"id": str(trade_id), "value": int(round(new_sl))},
            max_retries=2,
        )

    def close_trade(self, trade_id):
        try:
            return self._request("POST", "/futures/isolated/trade/close", {"id": str(trade_id)}, max_retries=2)
        except Exception:
            return self._request("DELETE", "/futures/isolated/trade", {"id": str(trade_id)}, max_retries=2)


# =====================================================================
# 8. PATRIMONIO, RIESGO Y GUARDIÁN UNIFICADO BAJO LA ESTRATEGIA IA
# =====================================================================
def compute_account_equity(available_balance, running_trades):
    locked_margin = sum(int(t.get("margin") or 0) for t in running_trades)
    floating_pl = sum(int(t.get("pl") or 0) for t in running_trades)
    est_exit_fees = sum(
        int(round(int(t.get("margin") or 0) * float(t.get("leverage") or 20.0) * Config.TAKER_FEE))
        for t in running_trades
    )
    true_equity = max(1, available_balance + locked_margin + floating_pl - est_exit_fees)
    return true_equity, locked_margin, floating_pl


def compute_effective_risk(warmup, risk_multiplier, recovery_mode, sprint_mode, current_dd_pct, parallel_risk_mult=1.0):
    base_risk = Config.RISK_WARMUP if warmup else Config.RISK_NORMAL
    min_floor = Config.MIN_EFFECTIVE_RISK_WARMUP if warmup else Config.MIN_EFFECTIVE_RISK_NORMAL
    if recovery_mode:
        state_factor = Config.RECOVERY_RISK_MULT
    elif current_dd_pct > Config.DRAWDOWN_RISK_THRESHOLD:
        state_factor = Config.DRAWDOWN_RISK_REDUCTION
    elif sprint_mode:
        state_factor = 0.78
    else:
        state_factor = 1.0
    combined = state_factor * max(0.65, risk_multiplier) * parallel_risk_mult
    return round(max(min_floor, base_risk * max(0.35, combined)), 4)


def infer_adopted_trade(trade, ind_5m, state, now, existing_meta=None, forced_action=None):
    side = trade.get("side", "buy")
    lev = float(trade.get("leverage") or 50.0)
    margin = int(trade.get("margin", 1000))
    entry = float(trade.get("price") or trade.get("entryPrice") or ind_5m["price"])
    atr = max(ind_5m["atr"], 1.0)
    pnl_dist = (ind_5m["price"] - entry) if side == "buy" else (entry - ind_5m["price"])
    regime = ind_5m["regime"]

    if forced_action in (1, 2, 3, 4, 5, 6, 7, 8):
        action = int(forced_action)
        style_map = {1: "TREND", 2: "REVERSION", 3: "SNIPER", 4: "DEF_SCALP", 5: "TREND", 6: "REVERSION", 7: "SNIPER", 8: "DEF_SCALP"}
        style = style_map[action]
    elif isinstance(existing_meta, dict) and existing_meta.get("action") in (1, 2, 3, 4, 5, 6, 7, 8):
        action = int(existing_meta["action"])
        style_map = {1: "TREND", 2: "REVERSION", 3: "SNIPER", 4: "DEF_SCALP", 5: "TREND", 6: "REVERSION", 7: "SNIPER", 8: "DEF_SCALP"}
        style = style_map[action]
    else:
        # Huérfano real sin metadata previa: asignar perfil conservador DEF_SCALP (LOCK)
        style, action = "DEF_SCALP", (4 if side == "buy" else 8)

    base = {
        "state": state,
        "action": action,
        "style": style,
        "margin": margin,
        "entry_time": now,
        "risk": Config.RISK_NORMAL,
        "last_eval_time": now,
        "last_pnl_atr": round(pnl_dist / atr, 4),
        "last_guardian_mode": f"{style}_INIT",
        "sl_locked": False,
        "eval_count": 0,
        "ai_closed": False,
    }
    if isinstance(existing_meta, dict):
        base["state"] = existing_meta.get("state", state)
        base["action"] = int(existing_meta.get("action", action))
        base["style"] = existing_meta.get("style", style)
        base["entry_time"] = existing_meta.get("entry_time", now)
        base["last_eval_time"] = existing_meta.get("last_eval_time", now)
        base["last_pnl_atr"] = existing_meta.get("last_pnl_atr", base["last_pnl_atr"])
        base["eval_count"] = int(existing_meta.get("eval_count", 0))
        base["sl_locked"] = bool(existing_meta.get("sl_locked", False))
        base["ai_closed"] = bool(existing_meta.get("ai_closed", False))
    return base


def calculate_strategy_order_params(strategy, price, atr, rr_mult=1.0):
    side = strategy["side"]
    sl_distance = max(atr * strategy["sl_atr"], price * 0.0024)
    leverage = max(
        Config.MIN_LEVERAGE,
        min(Config.MAX_LEVERAGE, int((0.68 / (sl_distance / price)) * strategy["lev_f"]))
    )
    effective_rr = max(1.5, strategy["rr"] * rr_mult)
    tp_distance = (sl_distance * effective_rr) + (price * (2.2 * Config.TAKER_FEE))
    sl_price = (price - sl_distance) if side == "buy" else (price + sl_distance)
    tp_price = (price + tp_distance) if side == "buy" else (price - tp_distance)
    return leverage, sl_price, tp_price


def enforce_concurrency_cap(client, brain, running_trades):
    if len(running_trades) <= Config.MAX_CONCURRENT_TRADES:
        return running_trades
    excess_count = len(running_trades) - Config.MAX_CONCURRENT_TRADES
    print(f"  [AUDITOR CONCURRENCIA] {len(running_trades)} activas (Máx: {Config.MAX_CONCURRENT_TRADES}). Reduciendo {excess_count}...")
    sorted_by_pl = sorted(running_trades, key=lambda t: int(t.get("pl") or 0), reverse=True)
    closed_ids = set()
    for trade in sorted_by_pl[:excess_count]:
        tid = str(trade["id"])
        pl_sats = int(trade.get("pl") or 0)
        try:
            client.close_trade(tid)
            closed_ids.add(tid)
            if tid in brain.active_trades and isinstance(brain.active_trades[tid], dict):
                brain.active_trades[tid]["ai_closed"] = (pl_sats > 0)
                brain.dirty = True
            print(f"  [REDUCCIÓN CONCURRENCIA] Cerrada {tid[:6]} (PnL: {pl_sats:+,} sats).")
        except Exception as e:
            print(f"  [AVISO REDUCCIÓN] {tid[:6]}: {e}")
    return [t for t in running_trades if str(t["id"]) not in closed_ids]


def execute_strategy_guardian(client, brain, trade, ind_5m, ind_15m, trade_meta, system_mode):
    try:
        tid = str(trade["id"])
        side = trade.get("side", "buy")
        margin = int(trade.get("margin") or 1000)
        pl_sats = int(trade.get("pl") or 0)
        lnm_price = ind_5m["price"]
        atr = max(ind_5m["atr"], 1.0)
        atr_pct = ind_5m["atr_pct"]
        regime = ind_5m["regime"]
        rsi = ind_5m["rsi"]
        bb_pos = ind_5m["bb_pos"]

        entry = float(trade.get("price") or trade.get("entryPrice") or lnm_price)
        current_sl = float(trade.get("stoploss") or 0.0)
        liq = float(trade.get("liquidation") or 0.0)
        fee_offset = entry * (2.4 * Config.TAKER_FEE)

        action_id = int(trade_meta.get("action", 2 if side == "buy" else 6))
        strat = brain.strategies.get(action_id, brain.strategies[2 if side == "buy" else 6])
        style = strat["style"]
        exit_mode = strat["exit_mode"]
        exhaust_sens = strat["exhaust_sens"]

        pnl_dist = (lnm_price - entry) if side == "buy" else (entry - lnm_price)
        pnl_atr = pnl_dist / atr
        prev_loop_pnl_atr = float(trade_meta.get("prev_loop_pnl_atr", trade_meta.get("last_pnl_atr", pnl_atr)))
        delta_atr = pnl_atr - prev_loop_pnl_atr
        trade_meta["prev_loop_pnl_atr"] = round(pnl_atr, 4)
        eval_count = int(trade_meta.get("eval_count", 0))

        sl_locked = (
            (side == "buy" and current_sl > (entry + fee_offset * 0.8)) or
            (side == "sell" and current_sl > 0 and current_sl < (entry - fee_offset * 0.8))
        )
        mode_label = f"{style}_{exit_mode}"
        trade_meta.update({"last_guardian_mode": mode_label, "sl_locked": bool(sl_locked)})

        vol_scale = 1.12 if atr_pct < 0.0025 else (0.88 if atr_pct > 0.0068 else 1.00)
        sys_scale = 0.72 if (system_mode in ("DEFENSIVE", "RECOVERY") and regime == "RANGE_SQUEEZE") else (0.86 if system_mode in ("DEFENSIVE", "RECOVERY") else 1.00)

        harvest_target_atr = strat["harvest_atr"] * sys_scale
        min_profit_ok = pnl_dist > (fee_offset * 1.35) and pl_sats >= max(12, int(margin * 0.030))

        if min_profit_ok and pnl_atr >= harvest_target_atr:
            can_check_harvest = not (exit_mode == "RUNNER" and sl_locked)
            if can_check_harvest:
                if exhaust_sens == "MAX":
                    is_exhausted = (
                        (side == "buy" and (rsi > 60 or bb_pos > 0.74 or delta_atr < -0.06)) or
                        (side == "sell" and (rsi < 40 or bb_pos < 0.26 or delta_atr < -0.06)) or
                        (regime in ("RANGE_SQUEEZE", "CHOP_HIGHVOL") and eval_count >= 2)
                    )
                elif exhaust_sens == "HIGH":
                    is_exhausted = (
                        (side == "buy" and (rsi > 64 or bb_pos > 0.80 or delta_atr < -0.10)) or
                        (side == "sell" and (rsi < 36 or bb_pos < 0.20 or delta_atr < -0.10)) or
                        (regime in ("RANGE_SQUEEZE", "CHOP_HIGHVOL") and eval_count >= 3)
                    )
                elif exhaust_sens == "MED":
                    ema_flip = (side == "buy" and ind_15m["ema9"] < ind_15m["ema21"]) or (side == "sell" and ind_15m["ema9"] > ind_15m["ema21"])
                    is_exhausted = (
                        ((side == "buy" and rsi > 69) or (side == "sell" and rsi < 31)) and delta_atr < -0.08
                    ) or (ema_flip and delta_atr < -0.10)
                else:
                    is_exhausted = (
                        ((side == "buy" and rsi > 75) or (side == "sell" and rsi < 25)) and delta_atr < -0.15
                    )

                if is_exhausted:
                    client.close_trade(tid)
                    trade_meta["ai_closed"] = True
                    brain.dirty = True
                    print(
                        f"  [GUARDIÁN {mode_label}] Cosecha de estrategia en {tid[:6]} → "
                        f"+{pnl_atr:.2f}ATR (+{pl_sats} sats | Sens:{exhaust_sens})"
                    )
                    return True

        min_trig = max(strat["trail_trig_atr"] * atr * vol_scale * sys_scale, fee_offset * 1.30)
        slack = strat["trail_slack_atr"] * atr * vol_scale * sys_scale

        if (side == "buy" and rsi > 72) or (side == "sell" and rsi < 28):
            slack *= 0.82

        if pnl_dist <= min_trig:
            return False

        step_min = max(10.0, 0.12 * atr)
        if side == "buy":
            be_floor = entry + fee_offset
            raw_sl = max(be_floor, lnm_price - slack)
            target_sl = int(round(min(raw_sl, lnm_price - 18.0)))
            if target_sl >= int(round(be_floor * 0.9995)) and target_sl > (liq + 15.0) and target_sl > int(round(current_sl + step_min)):
                client.update_stoploss(tid, target_sl)
                trade_meta["sl_locked"] = True
                brain.dirty = True
        elif side == "sell":
            be_ceil = entry - fee_offset
            raw_sl = min(be_ceil, lnm_price + slack)
            target_sl = int(round(max(raw_sl, lnm_price + 18.0)))
            liq_ok = (liq <= 0.0) or (target_sl < (liq - 15.0))
            if target_sl <= int(round(be_ceil * 1.0005)) and liq_ok and (current_sl == 0.0 or target_sl < int(round(current_sl - step_min))):
                client.update_stoploss(tid, target_sl)
                trade_meta["sl_locked"] = True
                brain.dirty = True
    except Exception as e:
        print(f"[GUARDIÁN AVISO] {trade.get('id','')[:6]}: {e}")
    return False


def evaluate_open_trades_online(brain, running, ind_5m, current_state, now):
    if not Config.ENABLE_INTERMEDIATE_LEARNING or not running:
        return
    atr, lnm_price = max(ind_5m["atr"], 1.0), ind_5m["price"]
    summaries = []

    for trade in running:
        tid = str(trade["id"])
        meta = brain.active_trades.get(tid)
        if not isinstance(meta, dict):
            continue
        last_eval = float(meta.get("last_eval_time", meta.get("entry_time", now)))
        if (now - last_eval) < Config.INTERMEDIATE_EVAL_INTERVAL_SEC:
            continue

        side = trade.get("side", "buy")
        entry = float(trade.get("price") or trade.get("entryPrice") or lnm_price)
        current_pnl_atr = round(((lnm_price - entry) if side == "buy" else (entry - lnm_price)) / atr, 4)
        delta_atr = round(current_pnl_atr - float(meta.get("last_pnl_atr", 0.0)), 4)
        style_str = str(meta.get("style") or "REVERSION")

        shaping_r = brain.remember_intermediate(
            origin_state=meta.get("state", current_state),
            action=int(meta.get("action", 2)),
            current_state=current_state,
            side=side,
            delta_atr=delta_atr,
            guardian_mode=str(meta.get("last_guardian_mode", f"{style_str}_INIT")),
            sl_locked=bool(meta.get("sl_locked", False)),
            eval_count=int(meta.get("eval_count", 0)),
        )
        meta.update({
            "last_eval_time": now,
            "last_pnl_atr": current_pnl_atr,
            "eval_count": int(meta.get("eval_count", 0)) + 1,
        })
        brain.dirty = True
        if shaping_r != 0.0:
            summaries.append(f"{tid[:4]}({style_str}:{delta_atr:+.2f}ATR→R:{shaping_r:+.3f})")

    if summaries:
        print(f"[IA ONLINE #{brain.total_intermediate_updates}] {' | '.join(summaries)}")


def compute_situational_order_params(
    raw_lev, raw_sl, raw_tp, strat, state, ind_5m, ind_15m, ind_1h, ind_4h,
    mods, brain, action, recovery_mode, current_dd_pct, consecutive_losses,
    combined_rr, perception=None
):
    price = float(ind_5m["price"])
    atr = max(float(ind_5m["atr"]), price * 0.0012)
    side = strat.get("side", "buy")
    norm_s = brain._normalize_state_key(state)
    q_vals = brain.get_state_q_values(norm_s)
    q_val = float(q_vals[action]) if action < len(q_vals) else 0.0
    visits = int(brain.state_visits.get(norm_s, 0))
    override_side = perception.get("override_side") if isinstance(perception, dict) else None

    # 1. Factor de Régimen y Momentum Override
    is_long = (side == "buy")
    aligned_override = (is_long and override_side == "BULL") or ((not is_long) and override_side == "BEAR")

    if aligned_override:
        regime_mult = 0.78 if not recovery_mode else 0.55
    elif "RANGE_SQUEEZE" in norm_s and "CHOP" in norm_s:
        regime_mult = 0.38
    elif "CHOP" in norm_s:
        regime_mult = 0.48
    elif "RANGE_SQUEEZE" in norm_s:
        regime_mult = 0.58
    elif "TREND" in norm_s and "LOW_EXP" in norm_s:
        regime_mult = 0.90
    else:
        regime_mult = 0.72

    # 2. Factor de Alineación Multi-Timeframe (con exención si hay Giro en V confirmado)
    bull_1h = "BULL" in mods.get("name_1h", "") or float(ind_1h.get("rsi", 50.0)) >= 54.0
    bear_1h = "BEAR" in mods.get("name_1h", "") or float(ind_1h.get("rsi", 50.0)) <= 46.0
    bull_4h = "BULL" in mods.get("name_4h", "")
    bear_4h = "BEAR" in mods.get("name_4h", "")

    if aligned_override:
        mtf_mult = 1.05  # No penalizar por lag de 4h cuando el Giro en V está activo
    elif (is_long and bull_1h and bull_4h) or (not is_long and bear_1h and bear_4h):
        mtf_mult = 1.10
    elif (is_long and bear_1h) or (not is_long and bull_1h):
        mtf_mult = 0.52
    else:
        mtf_mult = 0.82

    # 3. Factor de Convicción IA
    if q_val >= 0.08 and visits >= 5:
        conv_mult = 1.12
    elif q_val >= 0.03 and visits >= 3:
        conv_mult = 0.98
    elif q_val >= 0.0:
        conv_mult = 0.78
    else:
        conv_mult = 0.65

    # 4. Factor de Defensa de Cuenta
    dd_mult = max(0.50, 1.0 - (current_dd_pct * 2.0))
    rec_mult = 0.58 if recovery_mode else 1.0
    streak_mult = 0.84 ** min(4, max(0, consecutive_losses))

    situational_mult = regime_mult * mtf_mult * conv_mult * dd_mult * rec_mult * streak_mult
    dynamic_lev = int(round(float(raw_lev) * situational_mult))
    dynamic_lev = max(15, min(85 if not aligned_override else 90, dynamic_lev))
    if recovery_mode:
        dynamic_lev = min(38, dynamic_lev)

    # Distancias de SL y TP garantizando ratio Neto de Sats post-fees (>= 3.2x fee round-trip)
    min_sl_dist = max(atr * 1.18, price * 0.0040)
    max_safe_sl_dist = price * (0.70 / max(dynamic_lev, 15))
    orig_sl_dist = abs(price - float(raw_sl)) if raw_sl else (atr * 1.35)
    sl_dist = max(min_sl_dist, min(orig_sl_dist, max_safe_sl_dist))

    min_tp_dist = max(sl_dist * max(1.22, combined_rr), price * 0.0075)
    orig_tp_dist = abs(float(raw_tp) - price) if raw_tp else (sl_dist * 1.55)
    tp_dist = max(min_tp_dist, orig_tp_dist)

    if is_long:
        sl_price = round(price - sl_dist, 1)
        tp_price = round(price + tp_dist, 1)
    else:
        sl_price = round(price + sl_dist, 1)
        tp_price = round(price - tp_dist, 1)

    return dynamic_lev, sl_price, tp_price


def evaluate_system_mode(
    now, pause_until, ind_5m, daily_pnl_pct, current_dd_pct,
    floor_threat, equity, recovery_anchor_equity
):
    if ind_5m["atr_pct"] > Config.ATR_EXTREME_PCT or floor_threat:
        return "DEFENSIVE", False, False, recovery_anchor_equity, False

    if now < pause_until:
        # Desbloqueo táctico controlado: si hay Momentum Override fuerte y no hay amenaza de piso,
        # pasar a RECOVERY táctico en vez de parálisis DEFENSIVE eterna
        if getattr(PERCEPTION_ENGINE, "override_side", None) is not None and (pause_until - now) < 300:
            return "RECOVERY", True, True, recovery_anchor_equity or equity, False
        return "DEFENSIVE", False, False, recovery_anchor_equity, False

    enter_recovery = (current_dd_pct >= Config.RECOVERY_ENTER_DD)

    if recovery_anchor_equity is not None or enter_recovery:
        if recovery_anchor_equity is None:
            recovery_anchor_equity = equity
            print(f"  [MODO RECOVERY ACTIVADO] Ancla de capital: {recovery_anchor_equity:,} sats (DD Real: {current_dd_pct*100:.1f}%)")
        else:
            recovery_anchor_equity = min(recovery_anchor_equity, equity)

        bounced_from_anchor = (
            equity >= int(round(recovery_anchor_equity * 1.05)) and
            current_dd_pct <= (Config.RECOVERY_ENTER_DD * 0.85)
        )
        metrics_recovered = (current_dd_pct <= Config.RECOVERY_EXIT_DD)

        if metrics_recovered or bounced_from_anchor:
            print(f"  [SALIDA DE RECOVERY ➔ NORMAL] Capital actual: {equity:,} sats (Ancla: {recovery_anchor_equity:,} sats | DD Real: {current_dd_pct*100:.1f}%)")
            return "NORMAL", True, False, None, True

        return "RECOVERY", True, True, recovery_anchor_equity, False

    return "NORMAL", True, False, None, False


def main():
    api_key, api_secret, api_passphrase = load_saved_credentials()
    if not api_key or not api_secret or not api_passphrase:
        print("[ERROR CRÍTICO] Faltan credenciales de LN Markets.")
        return

    brain = AutonomousGoalBrain()
    client = LNMarketsClient(api_key, api_secret, api_passphrase)
    mtf_cache = MultiTimeframeCache()
    api_health = APIHealthMonitor()

    daily_start_equity, daily_start_date, peak_equity = None, None, None
    recovery_anchor_equity = None
    consecutive_losses, last_entry_time, risk_multiplier, pause_until = 0, 0.0, 1.0, 0.0
    current_mode = "NORMAL"

    print("====================================================================")
    print("  LNM SNIPER v7.3 CONSOLIDADO – GUARDIÁN UNIFICADO BAJO LA IA")
    print(f"  Meta Activa: {brain.current_target:,} sats | Límite Estricto: {Config.MAX_CONCURRENT_TRADES} pos")
    print("====================================================================")

    while True:
        warmup = brain.is_warming_up()
        loop_wait = 14 if warmup else 18
        if api_health.check_cooldown():
            continue

        try:
            now = time.time()
            bucket_1h = int(now // 3600)
            bucket_4h = int(now // 14400)

            if bucket_1h > int(brain.cycle_state["1h"].get("bucket", 0)):
                mtf_cache.invalidate("1h")
            if bucket_4h > int(brain.cycle_state["4h"].get("bucket", 0)):
                mtf_cache.invalidate("4h")

            user = client.get_user()
            balance = int(user.get("balance", 0))
            running = client.get_running_trades()

            if len(running) > Config.MAX_CONCURRENT_TRADES:
                running = enforce_concurrency_cap(client, brain, running)
                user = client.get_user()
                balance = int(user.get("balance", 0))

            equity, locked_margin, _ = compute_account_equity(balance, running)
            if equity <= 0:
                time.sleep(loop_wait)
                continue

            brain.check_and_advance_goal(equity)
            lnm_price = client.get_ticker_price()

            h_5m, l_5m, c_5m = mtf_cache.get_candles("5m", lnm_price)
            h_15m, l_15m, c_15m = mtf_cache.get_candles("15m", lnm_price)
            h_1h, l_1h, c_1h = mtf_cache.get_candles("1h")
            h_4h, l_4h, c_4h = mtf_cache.get_candles("4h")

            ind_5m = compute_indicators(h_5m, l_5m, c_5m, lnm_price)
            ind_15m = compute_indicators(h_15m, l_15m, c_15m, lnm_price)
            ind_1h = compute_indicators(h_1h, l_1h, c_1h)
            ind_4h = compute_indicators(h_4h, l_4h, c_4h)

            api_health.record_success()
            state = brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, len(running))

            running_id_set = {str(x["id"]) for x in running}
            closed_ids = [tid for tid in list(brain.active_trades.keys()) if str(tid) not in running_id_set]

            if closed_ids:
                closed_map = {str(ct["id"]): ct for ct in client.get_closed_trades(limit=25)}
                for cid in closed_ids:
                    info = brain.active_trades.pop(cid, None)
                    brain.dirty = True
                    if info and cid in closed_map:
                        pl = int(closed_map[cid].get("pl", 0))
                        m_used = int(info.get("margin", 1000))
                        was_harvested = bool(info.get("ai_closed", False))
                        orig_st = info.get("state", state)
                        act_taken = int(info.get("action", 2))

                        brain.remember_and_train(
                            orig_st, act_taken, pl, m_used, state,
                            equity_after=equity, hit_tp=(pl > m_used * 0.16), harvested=was_harvested
                        )
                        if pl < 0:
                            consecutive_losses += 1
                            risk_multiplier = max(0.55, risk_multiplier * Config.ANTI_MARTINGALE_FACTOR)
                            if consecutive_losses >= Config.CONSECUTIVE_LOSS_LIMIT:
                                pause_until = now + Config.CONSECUTIVE_LOSS_PAUSE_SEC
                                consecutive_losses = 0
                        else:
                            consecutive_losses = 0
                            risk_multiplier = min(1.0, risk_multiplier + 0.15)

            for t in running:
                tid = str(t["id"])
                existing = brain.active_trades.get(tid)
                if not isinstance(existing, dict) or not existing.get("style") or str(existing.get("style")) == "None":
                    brain.active_trades[tid] = infer_adopted_trade(t, ind_5m, state, now, existing_meta=existing)
                    brain.dirty = True

            brain.evaluate_and_close_macro_cycle("1h", bucket_1h, equity, ind_5m["price"], ind_1h, running)
            brain.evaluate_and_close_macro_cycle("4h", bucket_4h, equity, ind_5m["price"], ind_4h, running)

            today = date.today()
            if daily_start_date != today or daily_start_equity is None:
                daily_start_date, daily_start_equity = today, equity

            # Sincronizar Peak y Piso Duro (75%) con el capital real de LN Markets en el primer ciclo
            if peak_equity is None:
                resynced, synced_peak, synced_floor = sync_brain_floor_with_reality(brain, equity, floor_ratio=0.75)
                peak_equity = synced_peak
                if resynced:
                    brain.save_brain(force=True)
                    print(f"  🔄 [SINCRONIZACIÓN DE CAPITAL REAL] Peak={peak_equity:,} sats | Piso Duro (75%)={synced_floor:,} sats")
            elif equity > peak_equity:
                peak_equity = equity
                brain.peak_equity = peak_equity
                brain.locked_floor = int(round(peak_equity * 0.75))
                brain.dirty = True

            # Detectar cierres por Stop-Loss en servidor (Cooldown graduado anti-venganza)
            prev_eq = getattr(brain, "_last_loop_equity", equity)
            prev_cnt = getattr(brain, "_last_loop_count", len(running))
            if len(running) < prev_cnt and equity < prev_eq - 35:
                consecutive_losses += 1
                pause_until = max(pause_until, now + min(600, 120 * consecutive_losses))
                last_entry_time = now
                print(f"  🛑 [PROTECCIÓN STOP-OUT] Pérdida detectada (Racha: {consecutive_losses}). Enfriamiento por {int(pause_until - now)}s.")
            elif len(running) < prev_cnt and equity > prev_eq + 25:
                consecutive_losses = max(0, consecutive_losses - 1)
            brain._last_loop_equity = equity
            brain._last_loop_count = len(running)

            daily_pnl_pct = (equity - daily_start_equity) / max(daily_start_equity, 1)
            current_dd_pct = (peak_equity - equity) / max(peak_equity, 1)
            goal_pct = (equity / max(brain.current_target, 1)) * 100.0
            sprint_mode = goal_pct >= 88.0

            # Evaluar Percepción de Corto Plazo (Giro en V / Micro-Breakout)
            perception = PERCEPTION_ENGINE.evaluate(state, ind_5m, ind_15m, ind_1h, ind_4h, now=now)
            ovr_tag = perception["override_side"] or "OFF"

            # Kill Switch Duro al 15% de Drawdown real verificado
            floor_threat = (brain.locked_floor > 0 and equity <= brain.locked_floor) or (current_dd_pct >= 0.25)
            if floor_threat and (now >= pause_until):
                print(f"  🚨 [GUARDIÁN PISO DURO ACTIVADO] Equity ({equity:,}) <= Piso ({brain.locked_floor:,}) | DD: {current_dd_pct*100:.1f}%. Liquidando y pausando 45m.")
                for t_kill in list(running):
                    try:
                        client.close_trade(str(t_kill["id"]))
                    except Exception:
                        pass
                running = []
                brain.active_trades.clear()
                pause_until = now + 2700
                peak_equity = equity
                brain.peak_equity = equity
                brain.locked_floor = int(round(equity * 0.75))
                brain.dirty = True
                brain.save_brain(force=True)
                current_dd_pct = 0.0
                floor_threat = False

            current_mode, new_entries_allowed, recovery_mode, recovery_anchor_equity, exited_recovery = evaluate_system_mode(
                now, pause_until, ind_5m, daily_pnl_pct, current_dd_pct,
                floor_threat, equity, recovery_anchor_equity
            )

            mods = brain.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}][{current_mode}][{state}]"
                f"[1h:{mods['name_1h'][:4]}|4h:{mods['name_4h'][:4]}|OVR:{ovr_tag}] "
                f"${ind_5m['price']:.1f} | Eq:{equity:,} (Bal:{balance:,}) | "
                f"Act:{len(running)}/{Config.MAX_CONCURRENT_TRADES} | DD:{current_dd_pct*100:.1f}%"
            )

            still_running = []
            if running:
                for trade in running:
                    tid = str(trade["id"])
                    t_meta = brain.active_trades.setdefault(tid, infer_adopted_trade(trade, ind_5m, state, now))
                    closed_by_guardian = execute_strategy_guardian(
                        client, brain, trade, ind_5m, ind_15m, t_meta, current_mode
                    )
                    if not closed_by_guardian:
                        still_running.append(trade)
                evaluate_open_trades_online(brain, still_running, ind_5m, state, now)

            active_count = max(len(still_running), len(brain.active_trades))
            # Límite dinámico de posiciones concurrentes (nunca excede MAX_CONCURRENT_TRADES = 5)
            capital_max_pos = max(1, min(Config.MAX_CONCURRENT_TRADES, int((equity * 0.60) // 1000)))
            if recovery_mode:
                dynamic_max_pos = min(capital_max_pos, 1 if equity < 5000 else 2)
            elif "CHOP" in state and perception.get("override_side") is None:
                dynamic_max_pos = min(capital_max_pos, 1 if equity < 4500 else 2)
            else:
                dynamic_max_pos = capital_max_pos

            effective_cooldown = Config.ENTRY_COOLDOWN_SEC * (
                0.85 if perception.get("override_side") is not None
                else (2.0 if (recovery_mode or "CHOP" in state) else 1.0)
            )
            if new_entries_allowed and active_count < dynamic_max_pos:
                if (locked_margin / max(equity, 1)) < Config.SOFT_EXPOSURE_LIMIT and (now - last_entry_time) >= effective_cooldown:
                    raw_action = brain.choose_action(
                        state, ind_5m, ind_15m, ind_1h, ind_4h,
                        recovery_mode=recovery_mode, current_dd_pct=current_dd_pct
                    )
                    q_vec = brain.get_state_q_values(state)
                    action, gate_reason = SatsExpectancyGate.filter_and_rank_action(
                        raw_action, state, q_vec, brain.strategies, perception,
                        recovery_mode=recovery_mode, current_dd_pct=current_dd_pct
                    )
                    if action == 0 and raw_action > 0:
                        print(f"  ⏸️ [FILTRO SATS/IMPULSO] Entrada bloqueada ({gate_reason} | OVR:{perception.get('override_reason')})")
                    if action > 0:
                        live_running = client.get_running_trades()
                        if len(live_running) < min(dynamic_max_pos, Config.MAX_CONCURRENT_TRADES):
                            strat = brain.strategies[action]
                            combined_rr = mods["rr_mult"] * (Config.RECOVERY_RR_MULT if recovery_mode else 1.0)
                            raw_lev, raw_sl, raw_tp = calculate_strategy_order_params(
                                strat, ind_5m["price"], ind_5m["atr"], rr_mult=combined_rr
                            )
                            lev, sl, tp = compute_situational_order_params(
                                raw_lev, raw_sl, raw_tp, strat, state, ind_5m, ind_15m, ind_1h, ind_4h,
                                mods, brain, action, recovery_mode, current_dd_pct, consecutive_losses,
                                combined_rr, perception=perception
                            )
                            base_risk = compute_effective_risk(
                                warmup, risk_multiplier, recovery_mode, sprint_mode,
                                current_dd_pct, parallel_risk_mult=mods["risk_mult"]
                            )
                            q_val = float(q_vec[action]) if action < len(q_vec) else 0.0
                            kelly_mult = 1.45 if q_val >= 0.08 else (1.15 if q_val >= 0.03 else 0.85)
                            if perception.get("override_side") is not None:
                                kelly_mult *= 1.15
                            if recovery_mode:
                                kelly_mult = min(kelly_mult, 0.80)
                            # Marco de riesgo disciplinado: máximo 12% del equity por operación (mínimo 1000 sats LNM)
                            adj_risk = min(0.12, max(0.03, base_risk * kelly_mult))
                            margin_sats = max(1000, int(round(equity * adj_risk)))
                            viable_sats, sats_meta = SatsExpectancyGate.verify_net_sats_expectancy(
                                ind_5m["price"], margin_sats, lev, sl, tp
                            )
                            if not viable_sats:
                                print(
                                    f"  ⏸️ [EXPECTANCY SATS INSUFICIENTE] NetTP:+{sats_meta['net_tp_sats']} sats vs "
                                    f"Fee:{sats_meta['fee_sats']} sats (Ratio:{sats_meta['fee_ratio']}x)"
                                )
                            if viable_sats and balance >= margin_sats:
                                last_entry_time = now
                                try:
                                    new_t = client.open_trade(strat["side"], margin_sats, lev, sl, tp)
                                    if isinstance(new_t, dict) and "id" in new_t:
                                        nt_id = str(new_t["id"])
                                        brain.active_trades[nt_id] = infer_adopted_trade(
                                            new_t, ind_5m, state, now, forced_action=action
                                        )
                                        brain.dirty = True
                                        brain.save_brain(force=True)
                                        print(
                                            f"  → Abierta {strat['name']} [{len(live_running)+1}/{Config.MAX_CONCURRENT_TRADES}] "
                                            f"(Lev {lev}x | Margen {margin_sats} sats | Salida:{strat['exit_mode']} | RR_x{combined_rr:.2f})"
                                        )
                                except Exception as open_err:
                                    print(f"  [VERIFICANDO ORDEN POST-TIMEOUT] {open_err}")
                                    time.sleep(1.5)
                                    verify_running = client.get_running_trades()
                                    for vt in verify_running:
                                        vtid = str(vt["id"])
                                        if vtid not in brain.active_trades:
                                            brain.active_trades[vtid] = infer_adopted_trade(
                                                vt, ind_5m, state, now, forced_action=action
                                            )
                                            brain.dirty = True
                                    brain.save_brain(force=True)

            brain.save_brain(force=False)
        except RuntimeError as api_err:
            api_health.record_error("API/RED", api_err)
        except Exception as e:
            api_health.record_error(type(e).__name__, e)

        time.sleep(loop_wait)


# guardian_learner removed

if __name__ == "__main__":
    main()
