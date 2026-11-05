# -*- coding: utf-8 -*-
from sniper_perception_sats_core import (
    ShortTermPerceptionEngine,
    SatsExpectancyGate,
    sync_brain_floor_with_reality,
)
PERCEPTION_ENGINE = ShortTermPerceptionEngine()
import os
import re
import sys
import time
import math
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

    # Meta dinámica basada EXCLUSIVAMENTE en Balance Disponible (Sats Netos)
    INITIAL_TARGET_SATS = 300000
    TARGET_STEP_MULTIPLIER = 1.50
    INITIAL_FLOOR_RATIO = 0.75
    MILESTONE_FLOOR_RATIO = 0.88

    # Gestión de apalancamiento, riesgo y concurrencia estricta
    MIN_LEVERAGE = 15
    MAX_LEVERAGE = 95
    TAKER_FEE = 0.001
    WARMUP_TRADES = 20
    RISK_WARMUP = 0.06
    RISK_NORMAL = 0.12
    MAX_SINGLE_TRADE_MARGIN_PCT = 0.22
    MIN_EFFECTIVE_RISK_WARMUP = 0.025
    MIN_EFFECTIVE_RISK_NORMAL = 0.032
    MAX_CONCURRENT_TRADES = 5
    SOFT_EXPOSURE_LIMIT = 0.38
    ENTRY_COOLDOWN_SEC = 35.0

    # Frecuencia de análisis adaptativa (alta frecuencia de evaluación sin saturar API)
    FAST_ANALYZE_INTERVAL_SEC = 5.0
    ACTIVE_ANALYZE_INTERVAL_SEC = 7.0
    IDLE_ANALYZE_INTERVAL_SEC = 11.0
    USER_SYNC_TTL_SEC = 10.0

    # Aprendizaje intermedio táctico (separado del aprendizaje económico realizado)
    ENABLE_INTERMEDIATE_LEARNING = True
    INTERMEDIATE_EVAL_INTERVAL_SEC = 180.0
    ALPHA_INTERMEDIATE = 0.018
    INTERMEDIATE_DECAY_PER_EVAL = 0.82
    MAX_INTERMEDIATE_REWARD_CLIP = 0.10
    MIN_INTERMEDIATE_DELTA_ATR = 0.18

    # Controlador de degradación de Edge en el Guardian
    EDGE_DEGRADATION_WARN = -0.25
    EDGE_DEGRADATION_TIGHTEN = -0.42
    EDGE_DEGRADATION_EXIT = -0.65
    EDGE_DEGRADATION_PERSISTENCE = 3

    # Umbrales de seguridad y recuperación auditable (conectados al flujo operativo)
    MAX_DAILY_LOSS_PCT = 0.11
    MAX_DRAWDOWN_PCT = 0.16
    KILL_SWITCH_DD_PCT = 0.22
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
    TTL_5M = 30.0
    TTL_15M = 90.0
    TTL_1H = 300.0
    TTL_4H = 600.0

    NUM_ACTIONS = 9
    NUM_MACRO_ACTIONS = 4


# =====================================================================
# 2. CARGADOR SEGURO DE CREDENCIALES (SIN ESCANEO DE ARCHIVOS .PY)
# =====================================================================
def load_saved_credentials():
    k = os.getenv("LNM_API_KEY") or os.getenv("API_KEY") or ""
    s = os.getenv("LNM_API_SECRET") or os.getenv("API_SECRET") or ""
    p = os.getenv("LNM_PASSPHRASE") or os.getenv("API_PASSPHRASE") or ""
    if k and s and p and "TU_API" not in k:
        return k.strip(), s.strip(), p.strip()

    # Exclusivamente archivos de entorno protegidos (.env / ~/.env / ~/.bashrc)
    search_files = [
        ".env",
        os.path.expanduser("~/.env"),
        os.path.expanduser("~/.bashrc"),
    ]

    for filepath in search_files:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not k or "TU_API" in k:
                    m_k = re.search(r'(?:LNM_API_KEY|API_KEY)\s*[=:]\s*["\']?([^"\'\s#]+)["\']?', content)
                    if m_k and "TU_API" not in m_k.group(1):
                        k = m_k.group(1)
                if not s or "TU_API" in s:
                    m_s = re.search(r'(?:LNM_API_SECRET|API_SECRET)\s*[=:]\s*["\']?([^"\'\s#]+)["\']?', content)
                    if m_s and "TU_API" not in m_s.group(1):
                        s = m_s.group(1)
                if not p or "TU_PASSPHRASE" in p:
                    m_p = re.search(r'(?:LNM_PASSPHRASE|API_PASSPHRASE)\s*[=:]\s*["\']?([^"\'\s#]+)["\']?', content)
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
            time.sleep(min(remaining, 10.0))
            return True
        return False


# =====================================================================
# 4. GESTOR DE CACHÉ MULTI-TEMPORALIDAD CON CACHÉ DE INDICADORES
# =====================================================================
class MultiTimeframeCache:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Connection": "keep-alive", "User-Agent": "LNMSniper/7.3-Unified"})
        self.cache = {
            "5m":  {"last": 0.0, "ttl": Config.TTL_5M,  "period": 300,   "data": None, "ind_cache": None, "ind_price": 0.0},
            "15m": {"last": 0.0, "ttl": Config.TTL_15M, "period": 900,   "data": None, "ind_cache": None, "ind_price": 0.0},
            "1h":  {"last": 0.0, "ttl": Config.TTL_1H,  "period": 3600,  "data": None, "ind_cache": None, "ind_price": 0.0},
            "4h":  {"last": 0.0, "ttl": Config.TTL_4H,  "period": 14400, "data": None, "ind_cache": None, "ind_price": 0.0},
        }

    def invalidate(self, tf):
        if tf in self.cache:
            self.cache[tf]["last"] = 0.0
            self.cache[tf]["ind_cache"] = None

    def get_candles(self, tf, current_lnm_price=0.0):
        now = time.time()
        item = self.cache[tf]
        crossed_boundary = (int(now) // item["period"]) != (int(item["last"]) // item["period"])

        if item["data"] is None or (now - item["last"]) >= item["ttl"] or crossed_boundary:
            try:
                h, l, c = self._fetch_remote(tf)
                item["data"] = (h, l, c)
                item["last"] = now
                item["ind_cache"] = None
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

    def get_indicators_fast(self, tf, current_lnm_price=0.0):
        item = self.cache[tf]
        h, l, c = self.get_candles(tf, current_lnm_price)
        if tf in ("1h", "4h") and item["ind_cache"] is not None:
            return item["ind_cache"]
        if tf in ("5m", "15m") and item["ind_cache"] is not None and abs(current_lnm_price - item["ind_price"]) < 0.5:
            return item["ind_cache"]
        ind = compute_indicators(h, l, c, current_lnm_price if tf in ("5m", "15m") else 0.0)
        item["ind_cache"] = ind
        item["ind_price"] = float(current_lnm_price)
        return ind

    def _fetch_remote(self, tf):
        limit = Config.CANDLE_LIMIT
        map_binance = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
        interval = map_binance.get(tf, "5m")
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
            r = self.session.get(url, timeout=4.5)
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
            r = self.session.get(url, timeout=4.5)
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
        "high": float(highs[-1]),
        "low": float(lows[-1]),
        "open": float(closes[-2]) if len(closes) > 1 else ref_price,
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
# 6. CEREBRO AUTÓNOMO Q-LEARNING CON POLÍTICA ECONÓMICA DE SATS NETOS
# =====================================================================
class AutonomousGoalBrain:
    def __init__(self, filename=Config.BRAIN_FILE):
        self.filename = filename
        self.alpha = 0.22
        self.alpha_intermediate = Config.ALPHA_INTERMEDIATE
        self.alpha_macro = 0.18
        self.gamma = 0.90
        self.epsilon = 0.40
        self.epsilon_min = 0.008
        self.epsilon_decay = 0.995
        self.batch_size = 18

        self.total_trained = 0
        self.total_intermediate_updates = 0
        self.current_target = Config.INITIAL_TARGET_SATS
        self.milestone_level = 1
        self.locked_floor = 0
        self.peak_equity = 0
        self.realized_net_profit_sats = 0
        self.total_fees_paid_sats = 0

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
        if "RANGE_SQUEEZE" in state_key and "BEAR_RALLY" in state_key:
            c2 = q[2] * 0.45
            q[2] -= c2
            q[1], q[3], q[4] = q[1] + c2 * 0.30, q[3] + c2 * 0.35, q[4] + c2 * 0.35
            c6 = q[6] * 0.45
            q[6] -= c6
            q[5], q[7], q[8] = q[5] + c6 * 0.30, q[7] + c6 * 0.35, q[8] + c6 * 0.35
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
        # Integración real con AtomicBrainStore.load() y validación SHA-256 + fallback a .bak
        data = None
        if hasattr(self.store, "load"):
            try:
                data = self.store.load()
            except Exception as e:
                print(f"[AVISO STORE LOAD] {e}")

        if data is None:
            for path in (self.filename, f"{self.filename}.bak"):
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        break
                    except Exception as e:
                        print(f"[AVISO I/O] Error leyendo {path}: {e}")

        if not isinstance(data, dict):
            return

        try:
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
                self.q_table[k][0] = max(0.0, min(0.025, self.q_table[k][0]))
                if "RANGE_SQUEEZE" in k and "BEAR_RALLY" in k:
                    if self.q_table[k][2] > 0.02:
                        t2 = self.q_table[k][2] * 0.45
                        self.q_table[k][2] = round(self.q_table[k][2] - t2, 4)
                        self.q_table[k][1] = round(min(0.85, self.q_table[k][1] + t2 * 0.30), 4)
                        self.q_table[k][3] = round(min(0.85, self.q_table[k][3] + t2 * 0.35), 4)
                        self.q_table[k][4] = round(min(0.85, self.q_table[k][4] + t2 * 0.35), 4)
                    if self.q_table[k][6] > 0.02:
                        t6 = self.q_table[k][6] * 0.45
                        self.q_table[k][6] = round(self.q_table[k][6] - t6, 4)
                        self.q_table[k][5] = round(min(0.85, self.q_table[k][5] + t6 * 0.30), 4)
                        self.q_table[k][7] = round(min(0.85, self.q_table[k][7] + t6 * 0.35), 4)
                        self.q_table[k][8] = round(min(0.85, self.q_table[k][8] + t6 * 0.35), 4)

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
            self.peak_equity = int(data.get("peak_equity", 0))
            self.realized_net_profit_sats = int(data.get("realized_net_profit_sats", 0))
            self.total_fees_paid_sats = int(data.get("total_fees_paid_sats", 0))
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
                f"[CEREBRO CARGADO SHA-256] Estados: {len(self.q_table)} | "
                f"1h:{len(self.q_table_1h)} | 4h:{len(self.q_table_4h)} | "
                f"Cierres: {self.total_trained} | Online: {self.total_intermediate_updates}"
            )
        except Exception as e:
            print(f"[AVISO PROCESAMIENTO CEREBRO] {e}")

    def save_brain(self, force=False):
        now = time.time()
        if not force and not self.dirty:
            return
        if not force and (now - self.last_save_time) < Config.BRAIN_SAVE_INTERVAL:
            return
        if not self.q_table:
            return
        try:
            payload = {
                "epsilon": round(self.epsilon, 4),
                "total_trained": self.total_trained,
                "total_intermediate_updates": self.total_intermediate_updates,
                "current_target": self.current_target,
                "milestone_level": self.milestone_level,
                "locked_floor": self.locked_floor,
                "peak_equity": self.peak_equity,
                "realized_net_profit_sats": self.realized_net_profit_sats,
                "total_fees_paid_sats": self.total_fees_paid_sats,
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

    def check_and_advance_goal(self, available_balance):
        """
        CORRECCIÓN CRÍTICA P0.1:
        La meta avanza EXCLUSIVAMENTE cuando el balance disponible real (sats retirables)
        alcanza o supera target_balance_sats (self.current_target).
        """
        advanced = False
        available_balance = int(available_balance)
        while available_balance >= self.current_target:
            old_target = self.current_target
            self.locked_floor = max(self.locked_floor, int(round(old_target * Config.MILESTONE_FLOOR_RATIO)))
            self.current_target = int(round(old_target * Config.TARGET_STEP_MULTIPLIER))
            self.milestone_level += 1
            self.dirty = True
            advanced = True
            print(
                f"  🏆 [META DE BALANCE DISPONIBLE CONQUISTADA] Nivel #{self.milestone_level} | "
                f"Balance Real: {available_balance:,} sats | Nueva Meta: {self.current_target:,} sats | "
                f"Piso Protegido (88%): {self.locked_floor:,} sats"
            )
        if advanced:
            self.save_brain(force=True)
        return advanced

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

    def record_trade_in_cycles(self, net_pl_sats):
        for tf in ("1h", "4h"):
            self.cycle_state[tf]["trades"] = int(self.cycle_state[tf].get("trades", 0)) + 1
            if net_pl_sats > 0:
                self.cycle_state[tf]["wins"] = int(self.cycle_state[tf].get("wins", 0)) + 1
            self.cycle_state[tf]["pnl"] = int(self.cycle_state[tf].get("pnl", 0)) + int(net_pl_sats)
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
        # Ponderar principalmente el PnL neto realizado y secundariamente el cambio de equity
        effective_delta = realized_pnl if trades_cnt > 0 else eq_delta
        cycle_roi_pct = effective_delta / start_eq
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
        print(f"  → Performance Ciclo: PnL Neto Realizado:{realized_pnl:+,} sats | ΔEq:{eq_delta:+,} sats | Cierres:{trades_cnt} (WR:{winrate_pct:.0f}%) | Activos:{len(running_trades)}")
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
        """
        CORRECCIÓN CRÍTICA P0.2:
        NO_TRADE (acción 0) es ciudadano de primera clase en allowed_actions y compite
        directamente en la selección greedy: a* = argmax_{a in {0..8}} biased_q(s, a).
        """
        norm_s = self._normalize_state_key(state)
        q_vals = self.get_state_q_values(norm_s)
        self.state_visits[norm_s] = self.state_visits.get(norm_s, 0) + 1
        visits = self.state_visits[norm_s]
        self.dirty = True

        if recovery_mode and current_dd_pct >= 0.12:
            allowed_actions = [0, 2, 3, 4, 6, 7, 8]
        else:
            allowed_actions = [0, 1, 2, 3, 4, 5, 6, 7, 8]

        if recovery_mode:
            self.alpha = 0.28
            effective_eps = max(0.04, self.epsilon)
        else:
            self.alpha = 0.18
            self.epsilon = max(0.008, round(self.epsilon * 0.995, 4))
            effective_eps = max(0.07, self.epsilon) if visits < 30 else (0.0 if "CHOP" in norm_s else min(0.035, self.epsilon))

        if effective_eps > 0.0 and random.uniform(0, 1) < effective_eps:
            if visits < 30 and not recovery_mode:
                non_rev = [a for a in allowed_actions if a not in (0, 2, 6)]
                return int(random.choice(non_rev if non_rev else allowed_actions))
            return 0

        mods = self.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)
        lcb_penalty = (0.12 / float(np.sqrt(visits + 1.0))) if visits < 80 else 0.0

        def biased_q(a):
            if a == 0:
                return float(q_vals[0])
            strat = self.strategies[a]
            q_val = float(q_vals[a]) - lcb_penalty
            if mods["name_1h"] == "BULL_EXPANSION" and a in (5, 6, 7, 8):
                q_val = (q_val * 0.65) if q_val >= 0 else (q_val - 0.04)
            elif mods["name_1h"] == "BEAR_EXPANSION" and a in (1, 2, 3, 4):
                q_val = (q_val * 0.65) if q_val >= 0 else (q_val - 0.04)

            b = mods["long_bias"] if strat["side"] == "buy" else mods["short_bias"]
            if strat["style"] in ("REVERSION", "DEF_SCALP"):
                b += mods["rev_bias"]
            return q_val + b

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
        current_q = float(self.q_table[norm_s][a])
        # Incluir acción 0 (NO_TRADE) en la evaluación del valor futuro máximo
        max_future_q = max(self.q_table[norm_ns])
        new_q = float(np.clip(current_q + lr * (r + self.gamma * max_future_q - current_q), -0.95, 0.95))
        self._last_delta_q = abs(new_q - current_q)
        self.q_table[norm_s][a] = round(new_q, 4)
        return self._last_delta_q

    def _compact_memory(self):
        if len(self.memory) <= 400:
            return
        mem_list = list(self.memory)
        by_reward = sorted(mem_list, key=lambda m: abs(float(m[3])), reverse=True)
        top_400_set = {id(x) for x in by_reward[:400]}
        cutoff_idx = max(0, len(mem_list) - 250)
        compacted = [
            m for idx, m in enumerate(mem_list)
            if (id(m) in top_400_set) or (idx >= cutoff_idx) or (abs(float(m[3])) >= 0.08)
        ]
        self.memory = deque(compacted[-1600:], maxlen=2200)

    def _experience_replay(self, sample_count, custom_alpha=None):
        if len(self.memory) < 6:
            return
        mem_list = list(self.memory)
        weights = np.array([max(float(m[0]), 0.01) for m in mem_list], dtype=np.float64)
        weights /= weights.sum()
        sz = min(len(mem_list), sample_count)
        chosen_indices = np.random.choice(len(mem_list), size=sz, p=weights, replace=False)
        for idx in chosen_indices:
            _, s, a, r, ns = mem_list[idx]
            dq = self._update_q(s, a, r, ns, custom_alpha=custom_alpha)
            reg_change = 1.0 if str(s).split("|")[0] != str(ns).split("|")[0] else 0.0
            new_prio = round(abs(float(r)) + (0.30 * dq) + (0.20 * reg_change) + 0.02, 4)
            self.memory[idx] = (new_prio, s, a, r, ns)

    def remember_intermediate(
        self, origin_state, action, current_state, side,
        delta_atr, guardian_mode, sl_locked_confirmed, eval_count
    ):
        """
        Canal de Aprendizaje Táctico (Posiciones Abiertas):
        Limitado a micro-ajustes tácticos y exige que sl_locked esté confirmado por el servidor.
        No sustituye ni domina al canal de aprendizaje económico realizado.
        """
        if abs(delta_atr) < Config.MIN_INTERMEDIATE_DELTA_ATR and not (sl_locked_confirmed and eval_count <= 2):
            return 0.0

        parts = current_state.split("|")
        ema_ctx = parts[4] if len(parts) > 4 else "EMA_TRANS"
        net_delta_atr = delta_atr - 0.045 if delta_atr > 0 else delta_atr - 0.025
        delta_score = float(np.clip(net_delta_atr * 0.070, -0.08, 0.08))

        ema_aligned = (
            (side == "buy" and ema_ctx in ("FULL_BULL", "BULL_DIP")) or
            (side == "sell" and ema_ctx in ("FULL_BEAR", "BEAR_RALLY"))
        )
        if net_delta_atr > 0:
            ema_bonus = 0.015 if ema_aligned else 0.008
        else:
            ema_bonus = -0.008 if ema_aligned else -0.015

        guardian_score = 0.025 if (sl_locked_confirmed and "RUNNER" in guardian_mode) else (0.018 if sl_locked_confirmed else -0.005)
        decay = Config.INTERMEDIATE_DECAY_PER_EVAL ** min(eval_count, 10)
        raw_shaping = (delta_score + ema_bonus + guardian_score) * decay

        shaped_partial = float(np.clip(raw_shaping, -Config.MAX_INTERMEDIATE_REWARD_CLIP, Config.MAX_INTERMEDIATE_REWARD_CLIP))
        self._update_q(origin_state, action, shaped_partial, current_state, custom_alpha=self.alpha_intermediate)
        self.total_intermediate_updates += 1
        self.dirty = True
        return round(shaped_partial, 4)

    def remember_and_train(
        self, state, action, reward_sats, margin_used, next_state,
        equity_after=0, available_balance_after=None, real_balance_delta_sats=None,
        hit_tp=False, harvested=False, is_no_trade=False,
        recovery_mode=False, exited_recovery_bonus=False, lev_used=35.0,
        exact_fee_sats=0, pl_includes_fees=False
    ):
        """
        Canal de Aprendizaje Económico Definitivo (Operaciones Cerradas):
        Calcula la recompensa sobre el beneficio neto real acreditado en el balance disponible,
        evitando doble deducción de comisiones.
        """
        norm_s = self._normalize_state_key(state)
        norm_ns = self._normalize_state_key(next_state)

        if is_no_trade:
            self.q_table.setdefault(norm_s, self._init_q_vector(norm_s))
            self.q_table[norm_s][0] = max(0.005, min(0.020, round(self.q_table[norm_s][0] * 0.96, 4)))
            return

        fee_sats = int(exact_fee_sats) if exact_fee_sats > 0 else max(2, int(round(float(margin_used) * float(lev_used) * (2.0 * Config.TAKER_FEE))))

        # Determinar el cambio neto real en satoshis sin descontar comisiones dos veces
        if real_balance_delta_sats is not None:
            net_balance_delta = int(real_balance_delta_sats)
        elif pl_includes_fees:
            net_balance_delta = int(reward_sats)
        else:
            net_balance_delta = (int(reward_sats) - fee_sats) if reward_sats >= 0 else (int(reward_sats) - int(round(fee_sats * 0.5)))

        self.realized_net_profit_sats += net_balance_delta
        self.total_fees_paid_sats += fee_sats
        self.record_trade_in_cycles(net_balance_delta)

        ref_balance = int(available_balance_after if available_balance_after is not None else equity_after)
        net_balance_roi = net_balance_delta / max(float(margin_used), 1.0)

        gap_to_goal = max(self.current_target - max(1, ref_balance - net_balance_delta), 10000)
        goal_velocity = net_balance_delta / gap_to_goal
        shaped = (net_balance_roi * 1.25) + (goal_velocity * 5.0)

        if net_balance_roi < 0:
            shaped = (shaped * 1.65) - (0.065 if recovery_mode else 0.030)
        else:
            if hit_tp:
                shaped += 0.28
            if harvested:
                shaped += 0.22
            if exited_recovery_bonus:
                shaped += 0.25
            if ref_balance >= self.current_target:
                shaped += 0.65

        self.alpha = 0.28 if recovery_mode else 0.18
        dq = self._update_q(norm_s, action, shaped, norm_ns, custom_alpha=self.alpha)

        reg_change = 1.0 if norm_s.split("|")[0] != norm_ns.split("|")[0] else 0.0
        priority = round(abs(shaped) + (0.30 * dq) + (0.20 * reg_change) + 0.02, 4)
        self.memory.append((priority, norm_s, int(action), round(shaped, 4), norm_ns))

        self.total_trained += 1
        if self.total_trained % 400 == 0:
            self._compact_memory()
        self.dirty = True

        self._experience_replay(self.batch_size, custom_alpha=self.alpha * 0.85)
        if recovery_mode:
            self.epsilon = max(0.04, round(self.epsilon, 4))
        else:
            self.epsilon = max(self.epsilon_min, round(self.epsilon * self.epsilon_decay, 4))
        self.save_brain(force=True)


# =====================================================================
# 7. CLIENTE HTTP IDEMPOTENTE LN MARKETS CON CACHÉ DE CUENTA
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
        self._user_cache = None
        self._user_cache_ts = 0.0

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
                    timeout=(5.0, 12.0),
                )
                if resp.status_code in (429, 500, 502, 503, 504) and max_retries > 1:
                    time.sleep((1.5 ** attempt) + random.uniform(0.2, 0.5))
                    continue
                if not resp.ok:
                    raise RuntimeError(f"HTTP {resp.status_code} ({path}): {resp.text[:140]}")
                return resp.json() if resp.text else {}
            except Exception as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Fallo en {path}: {e}")
                time.sleep((1.5 ** attempt) + 0.25)

    def get_ticker_price(self):
        t = self._request("GET", "/futures/ticker")
        return float(t.get("lastPrice") or t.get("index") or 0.0) if isinstance(t, dict) else 0.0

    def get_user(self, force_refresh=False):
        now = time.time()
        if not force_refresh and self._user_cache is not None and (now - self._user_cache_ts) < Config.USER_SYNC_TTL_SEC:
            return self._user_cache
        u = self._request("GET", "/account")
        if isinstance(u, dict):
            self._user_cache = u
            self._user_cache_ts = now
        return u

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
        self._user_cache_ts = 0.0
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
        self._user_cache_ts = 0.0
        try:
            return self._request("POST", "/futures/isolated/trade/close", {"id": str(trade_id)}, max_retries=2)
        except Exception:
            return self._request("DELETE", "/futures/isolated/trade", {"id": str(trade_id)}, max_retries=2)


# =====================================================================
# 8. PATRIMONIO, RIESGO Y GUARDIÁN UNIFICADO CON CONFIRMACIÓN Y EDGE
# =====================================================================
def compute_account_equity(available_balance, running_trades):
    locked_margin = sum(int(t.get("margin") or 0) for t in running_trades)
    floating_pl = sum(int(t.get("pl") or 0) for t in running_trades)
    est_exit_fees = sum(
        int(round(int(t.get("margin") or 0) * float(t.get("leverage") or 20.0) * Config.TAKER_FEE))
        for t in running_trades
    )
    true_equity = max(1, int(available_balance) + locked_margin + floating_pl - est_exit_fees)
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

    if forced_action in (1, 2, 3, 4, 5, 6, 7, 8):
        action = int(forced_action)
        style_map = {1: "TREND", 2: "REVERSION", 3: "SNIPER", 4: "DEF_SCALP", 5: "TREND", 6: "REVERSION", 7: "SNIPER", 8: "DEF_SCALP"}
        style = style_map[action]
    elif isinstance(existing_meta, dict) and existing_meta.get("action") in (1, 2, 3, 4, 5, 6, 7, 8):
        action = int(existing_meta["action"])
        style_map = {1: "TREND", 2: "REVERSION", 3: "SNIPER", 4: "DEF_SCALP", 5: "TREND", 6: "REVERSION", 7: "SNIPER", 8: "DEF_SCALP"}
        style = style_map[action]
    else:
        style, action = "DEF_SCALP", (4 if side == "buy" else 8)

    base = {
        "state": state,
        "action": action,
        "style": style,
        "side": side,
        "margin": margin,
        "leverage": lev,
        "entry_time": now,
        "risk": Config.RISK_NORMAL,
        "last_eval_time": now,
        "last_pnl_atr": round(pnl_dist / atr, 4),
        "last_guardian_mode": f"{style}_INIT",
        "sl_locked": False,
        "sl_pending_target": 0,
        "eval_count": 0,
        "ai_closed": False,
        "close_order_sent": False,
        "edge_deg_streak": 0,
        "last_edge_score": 0.0,
    }
    if isinstance(existing_meta, dict):
        for k in (
            "state", "action", "style", "entry_time", "last_eval_time",
            "last_pnl_atr", "eval_count", "sl_locked", "sl_pending_target",
            "ai_closed", "close_order_sent", "edge_deg_streak", "last_edge_score"
        ):
            if k in existing_meta:
                base[k] = existing_meta[k]
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


def confirm_and_close_trade(client, brain, tid, trade_meta, reason_label):
    """
    CORRECCIÓN P0.5:
    Envía orden de cierre, evita duplicados concurrentes y verifica la confirmación
    de cierre antes de marcar ai_closed=True.
    """
    if trade_meta.get("close_order_sent") and (time.time() - float(trade_meta.get("close_sent_ts", 0))) < 15.0:
        return False
    trade_meta["close_order_sent"] = True
    trade_meta["close_sent_ts"] = time.time()
    resp = client.close_trade(tid)
    confirmed = False
    if isinstance(resp, dict) and (resp.get("closed") is True or str(resp.get("id", "")) == str(tid)):
        confirmed = True
    else:
        try:
            live_ids = {str(x["id"]) for x in client.get_running_trades()}
            confirmed = str(tid) not in live_ids
        except Exception:
            confirmed = False

    if confirmed:
        trade_meta["ai_closed"] = True
        trade_meta["close_reason"] = reason_label
        brain.dirty = True
    return confirmed


def confirm_and_update_stoploss(client, brain, tid, target_sl, side, entry, fee_offset, trade_meta):
    """
    CORRECCIÓN P0.5:
    Verifica explícitamente la respuesta de LN Markets antes de establecer sl_locked=True.
    """
    resp = client.update_stoploss(tid, target_sl)
    applied_sl = float(resp.get("stoploss") or 0.0) if isinstance(resp, dict) else 0.0
    if applied_sl <= 0.0:
        # Guardar como pendiente de reconciliación en el siguiente ciclo
        trade_meta["sl_pending_target"] = int(target_sl)
        return False

    is_locked = (
        (side == "buy" and applied_sl > (entry + fee_offset * 0.8)) or
        (side == "sell" and applied_sl > 0 and applied_sl < (entry - fee_offset * 0.8))
    )
    trade_meta["sl_locked"] = bool(is_locked)
    trade_meta["sl_pending_target"] = 0
    brain.dirty = True
    return bool(is_locked)


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
        t_meta = brain.active_trades.setdefault(tid, {})
        try:
            if confirm_and_close_trade(client, brain, tid, t_meta, "CONCURRENCY_CAP"):
                closed_ids.add(tid)
                print(f"  [REDUCCIÓN CONCURRENCIA CONFIRMADA] Cerrada {tid[:6]} (PnL: {pl_sats:+,} sats).")
        except Exception as e:
            print(f"  [AVISO REDUCCIÓN] {tid[:6]}: {e}")
    return [t for t in running_trades if str(t["id"]) not in closed_ids]


def execute_strategy_guardian(
    client, brain, trade, ind_5m, ind_15m, trade_meta, system_mode,
    edge_score=0.0, perception=None
):
    try:
        tid = str(trade["id"])
        side = trade.get("side", "buy")
        margin = int(trade.get("margin") or 1000)
        lev = float(trade.get("leverage") or 30.0)
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
        est_round_fee_sats = max(2, int(round(margin * lev * 2.0 * Config.TAKER_FEE)))

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

        # Reconciliar el estado real del Stop Loss reportado por el servidor LN Markets
        sl_locked_server = (
            (side == "buy" and current_sl > (entry + fee_offset * 0.8)) or
            (side == "sell" and current_sl > 0 and current_sl < (entry - fee_offset * 0.8))
        )
        mode_label = f"{style}_{exit_mode}"
        trade_meta.update({
            "last_guardian_mode": mode_label,
            "sl_locked": bool(sl_locked_server),
            "last_edge_score": round(float(edge_score), 4),
        })

        # =================================================================
        # CONTROLADOR DE DEGRADACIÓN DE EDGE (CORRECCIÓN P0.4)
        # =================================================================
        if edge_score <= Config.EDGE_DEGRADATION_WARN:
            trade_meta["edge_deg_streak"] = int(trade_meta.get("edge_deg_streak", 0)) + 1
        else:
            trade_meta["edge_deg_streak"] = 0

        deg_streak = int(trade_meta["edge_deg_streak"])
        vel_ema = float(perception.get("vel_ema_atr", 0.0)) if isinstance(perception, dict) else 0.0
        ovr_side = perception.get("override_side") if isinstance(perception, dict) else None
        contrary_momentum = (
            (side == "buy" and (vel_ema < -0.35 or rsi < 44.0 or ovr_side == "BEAR")) or
            (side == "sell" and (vel_ema > 0.35 or rsi > 56.0 or ovr_side == "BULL"))
        )

        if deg_streak >= Config.EDGE_DEGRADATION_PERSISTENCE and contrary_momentum:
            # Caso A: La posición tiene beneficio neto disponible -> Cosechar para proteger sats
            if pl_sats > (est_round_fee_sats + 10) and pnl_dist > (fee_offset * 1.15):
                if confirm_and_close_trade(client, brain, tid, trade_meta, "EDGE_DEGRADATION_PROFIT_PROTECT"):
                    print(
                        f"  🛡️ [GUARDIÁN EDGE PROTECT] Cosecha defensiva en {tid[:6]} tras {deg_streak} ciclos "
                        f"de degradación (Edge:{edge_score:+.3f} | +{pl_sats} sats)"
                    )
                    return True
            # Caso B: La posición está en pérdida con expectativa muy deteriorada y momentum en contra
            elif edge_score <= Config.EDGE_DEGRADATION_EXIT and pnl_atr <= -1.10:
                if confirm_and_close_trade(client, brain, tid, trade_meta, "EDGE_DEGRADATION_DEFENSIVE_EXIT"):
                    print(
                        f"  🛑 [GUARDIÁN EDGE CUT] Cierre defensivo en {tid[:6]} (Streak:{deg_streak} | "
                        f"Edge:{edge_score:+.3f} | PnL:{pnl_atr:+.2f}ATR | PnL:{pl_sats:+,} sats)"
                    )
                    return True

        vol_scale = 1.12 if atr_pct < 0.0025 else (0.88 if atr_pct > 0.0068 else 1.00)
        sys_scale = 0.72 if (system_mode in ("DEFENSIVE", "RECOVERY") and regime == "RANGE_SQUEEZE") else (0.86 if system_mode in ("DEFENSIVE", "RECOVERY") else 1.00)
        if deg_streak >= 2 and edge_score <= Config.EDGE_DEGRADATION_TIGHTEN:
            sys_scale *= 0.80  # Acelerar cosecha y ceñir trailing stop ante deterioro persistente

        harvest_target_atr = strat["harvest_atr"] * sys_scale
        min_profit_ok = pnl_dist > (fee_offset * 1.35) and pl_sats >= max(12, int(margin * 0.030))

        if min_profit_ok and pnl_atr >= harvest_target_atr:
            can_check_harvest = not (exit_mode == "RUNNER" and sl_locked_server)
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
                    if confirm_and_close_trade(client, brain, tid, trade_meta, f"HARVEST_{mode_label}"):
                        print(
                            f"  [GUARDIÁN {mode_label}] Cosecha confirmada en {tid[:6]} → "
                            f"+{pnl_atr:.2f}ATR (+{pl_sats} sats | Sens:{exhaust_sens})"
                        )
                        return True

        min_trig = max(strat["trail_trig_atr"] * atr * vol_scale * sys_scale, fee_offset * 1.30)
        slack = strat["trail_slack_atr"] * atr * vol_scale * sys_scale

        if (side == "buy" and rsi > 72) or (side == "sell" and rsi < 28) or deg_streak >= 2:
            slack *= 0.78

        if pnl_dist <= min_trig:
            return False

        step_min = max(10.0, 0.12 * atr)
        if side == "buy":
            be_floor = entry + fee_offset
            raw_sl = max(be_floor, lnm_price - slack)
            target_sl = int(round(min(raw_sl, lnm_price - 18.0)))
            if target_sl >= int(round(be_floor * 0.9995)) and target_sl > (liq + 15.0) and target_sl > int(round(current_sl + step_min)):
                confirm_and_update_stoploss(client, brain, tid, target_sl, side, entry, fee_offset, trade_meta)
        elif side == "sell":
            be_ceil = entry - fee_offset
            raw_sl = min(be_ceil, lnm_price + slack)
            target_sl = int(round(max(raw_sl, lnm_price + 18.0)))
            liq_ok = (liq <= 0.0) or (target_sl < (liq - 15.0))
            if target_sl <= int(round(be_ceil * 1.0005)) and liq_ok and (current_sl == 0.0 or target_sl < int(round(current_sl - step_min))):
                confirm_and_update_stoploss(client, brain, tid, target_sl, side, entry, fee_offset, trade_meta)
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
        current_sl = float(trade.get("stoploss") or 0.0)
        fee_offset = entry * (2.4 * Config.TAKER_FEE)
        sl_confirmed = (
            bool(meta.get("sl_locked", False)) and (
                (side == "buy" and current_sl > (entry + fee_offset * 0.8)) or
                (side == "sell" and current_sl > 0 and current_sl < (entry - fee_offset * 0.8))
            )
        )

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
            sl_locked_confirmed=sl_confirmed,
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
    """
    CORRECCIÓN P0.6:
    Garantiza matemáticamente que sl_dist NUNCA supere max_safe_sl_dist reduciendo
    el apalancamiento dinámico si min_sl_dist requiere más espacio respecto a liquidación.
    """
    price = float(ind_5m["price"])
    atr = max(float(ind_5m["atr"]), price * 0.0012)
    side = strat.get("side", "buy")
    norm_s = brain._normalize_state_key(state)
    q_vals = brain.get_state_q_values(norm_s)
    q_val = float(q_vals[action]) if action < len(q_vals) else 0.0
    visits = int(brain.state_visits.get(norm_s, 0))
    override_side = perception.get("override_side") if isinstance(perception, dict) else None

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

    bull_1h = "BULL" in mods.get("name_1h", "") or float(ind_1h.get("rsi", 50.0)) >= 54.0
    bear_1h = "BEAR" in mods.get("name_1h", "") or float(ind_1h.get("rsi", 50.0)) <= 46.0
    bull_4h = "BULL" in mods.get("name_4h", "")
    bear_4h = "BEAR" in mods.get("name_4h", "")

    if aligned_override:
        mtf_mult = 1.05
    elif (is_long and bull_1h and bull_4h) or (not is_long and bear_1h and bear_4h):
        mtf_mult = 1.10
    elif (is_long and bear_1h) or (not is_long and bull_1h):
        mtf_mult = 0.52
    else:
        mtf_mult = 0.82

    if q_val >= 0.08 and visits >= 5:
        conv_mult = 1.12
    elif q_val >= 0.03 and visits >= 3:
        conv_mult = 0.98
    elif q_val >= 0.0:
        conv_mult = 0.78
    else:
        conv_mult = 0.65

    dd_mult = max(0.50, 1.0 - (current_dd_pct * 2.0))
    rec_mult = 0.58 if recovery_mode else 1.0
    streak_mult = 0.84 ** min(4, max(0, consecutive_losses))

    situational_mult = regime_mult * mtf_mult * conv_mult * dd_mult * rec_mult * streak_mult
    dynamic_lev = int(round(float(raw_lev) * situational_mult))
    dynamic_lev = max(Config.MIN_LEVERAGE, min(85 if not aligned_override else 90, dynamic_lev))
    if recovery_mode:
        dynamic_lev = min(38, dynamic_lev)

    min_sl_dist = max(atr * 1.18, price * 0.0040)
    orig_sl_dist = abs(price - float(raw_sl)) if raw_sl else (atr * 1.35)
    desired_sl_dist = max(min_sl_dist, orig_sl_dist)

    # Ajustar apalancamiento hacia abajo si desired_sl_dist supera la distancia segura de liquidación (0.68 / lev)
    safe_lev_cap = int(math.floor((price * 0.68) / max(desired_sl_dist, 1.0)))
    dynamic_lev = max(Config.MIN_LEVERAGE, min(dynamic_lev, safe_lev_cap))
    max_safe_sl_dist = price * (0.70 / max(dynamic_lev, Config.MIN_LEVERAGE))

    # Garantía matemática: sl_dist jamás excede max_safe_sl_dist
    sl_dist = min(desired_sl_dist, max_safe_sl_dist)

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
    """
    CORRECCIÓN CRÍTICA P0.6:
    Conecta efectivamente MAX_DAILY_LOSS_PCT, MAX_DRAWDOWN_PCT, RECOVERY_ENTER_DAILY
    y RECOVERY_EXIT_DAILY al control de modos del sistema.
    """
    if (
        ind_5m["atr_pct"] > Config.ATR_EXTREME_PCT
        or floor_threat
        or current_dd_pct >= Config.MAX_DRAWDOWN_PCT
        or daily_pnl_pct <= -abs(Config.MAX_DAILY_LOSS_PCT)
    ):
        return "DEFENSIVE", False, False, recovery_anchor_equity, False

    if now < pause_until:
        if getattr(PERCEPTION_ENGINE, "override_side", None) is not None and (pause_until - now) < 300:
            return "RECOVERY", True, True, recovery_anchor_equity or equity, False
        return "DEFENSIVE", False, False, recovery_anchor_equity, False

    enter_recovery = (
        current_dd_pct >= Config.RECOVERY_ENTER_DD
        or daily_pnl_pct <= Config.RECOVERY_ENTER_DAILY
    )

    if recovery_anchor_equity is not None or enter_recovery:
        if recovery_anchor_equity is None:
            recovery_anchor_equity = equity
            print(
                f"  [MODO RECOVERY ACTIVADO] Ancla: {recovery_anchor_equity:,} sats "
                f"(DD: {current_dd_pct*100:.1f}% | Diario: {daily_pnl_pct*100:+.1f}%)"
            )
        else:
            recovery_anchor_equity = min(recovery_anchor_equity, equity)

        bounced_from_anchor = (
            (equity >= int(round(recovery_anchor_equity * 1.05)) and
             current_dd_pct <= (Config.RECOVERY_ENTER_DD * 0.85)) or
            (equity >= int(round(recovery_anchor_equity * 1.03)) and daily_pnl_pct > Config.RECOVERY_ENTER_DAILY)
        )
        metrics_recovered = (
            current_dd_pct <= Config.RECOVERY_EXIT_DD
            and daily_pnl_pct >= Config.RECOVERY_EXIT_DAILY
        )

        if metrics_recovered or bounced_from_anchor:
            print(
                f"  [SALIDA DE RECOVERY ➔ NORMAL] Capital actual: {equity:,} sats "
                f"(Ancla: {recovery_anchor_equity:,} sats | DD Real: {current_dd_pct*100:.1f}%)"
            )
            return "NORMAL", True, False, None, True

        return "RECOVERY", True, True, recovery_anchor_equity, False

    return "NORMAL", True, False, None, False


def main():
    safe_mode = ("--safe-mode" in sys.argv) or (os.getenv("SNIPER_SAFE_MODE", "0") == "1")
    once_mode = ("--once" in sys.argv)

    api_key, api_secret, api_passphrase = load_saved_credentials()
    if not api_key or not api_secret or not api_passphrase:
        print("[ERROR CRÍTICO] Faltan credenciales de LN Markets en variables de entorno o .env.")
        return

    brain = AutonomousGoalBrain()
    client = LNMarketsClient(api_key, api_secret, api_passphrase)
    mtf_cache = MultiTimeframeCache()
    api_health = APIHealthMonitor()
    floor_guardian = HardFloorGuardian(
        initial_floor_ratio=Config.INITIAL_FLOOR_RATIO,
        milestone_floor_ratio=Config.MILESTONE_FLOOR_RATIO,
        max_dd_pct=Config.MAX_DRAWDOWN_PCT,
        max_daily_loss_pct=Config.MAX_DAILY_LOSS_PCT,
    )

    daily_start_equity, daily_start_date, peak_equity = None, None, None
    recovery_anchor_equity = None
    consecutive_losses, last_entry_time, risk_multiplier, pause_until = 0, 0.0, 1.0, 0.0
    current_mode = "NORMAL"
    last_known_balance = None

    print("====================================================================")
    print("  LNM SNIPER v7.3 CONSOLIDADO – ARQUITECTURA DE SATS NETOS REALIZADOS")
    print(f"  Meta Balance Disponible: {brain.current_target:,} sats | Máx Pos: {Config.MAX_CONCURRENT_TRADES}")
    if safe_mode:
        print("  🛡️ [MODO SEGURO ACTIVO] Solo reconciliación, Guardian y análisis (sin nuevas órdenes reales).")
    print("====================================================================")

    while True:
        cycle_t0 = time.time()
        warmup = brain.is_warming_up()
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

            running = client.get_running_trades()
            running_id_set = {str(x["id"]) for x in running}
            closed_ids = [tid for tid in list(brain.active_trades.keys()) if str(tid) not in running_id_set]

            user = client.get_user(force_refresh=bool(closed_ids) or (last_known_balance is None))
            balance = int(user.get("balance", 0))
            if last_known_balance is None:
                last_known_balance = balance

            if len(running) > Config.MAX_CONCURRENT_TRADES:
                running = enforce_concurrency_cap(client, brain, running)
                user = client.get_user(force_refresh=True)
                balance = int(user.get("balance", 0))

            equity, locked_margin, floating_pl = compute_account_equity(balance, running)
            if equity <= 0:
                time.sleep(Config.IDLE_ANALYZE_INTERVAL_SEC)
                continue

            # CORRECCIÓN P0.1: Evaluar avance de meta EXCLUSIVAMENTE con el balance disponible real
            brain.check_and_advance_goal(balance)
            lnm_price = client.get_ticker_price()

            ind_5m = mtf_cache.get_indicators_fast("5m", lnm_price)
            ind_15m = mtf_cache.get_indicators_fast("15m", lnm_price)
            ind_1h = mtf_cache.get_indicators_fast("1h")
            ind_4h = mtf_cache.get_indicators_fast("4h")

            api_health.record_success()
            state = brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, len(running))

            if closed_ids:
                closed_map = {str(ct["id"]): ct for ct in client.get_closed_trades(limit=25)}
                for cid in closed_ids:
                    info = brain.active_trades.pop(cid, None)
                    brain.dirty = True
                    if info and cid in closed_map:
                        ct_obj = closed_map[cid]
                        pl = int(ct_obj.get("pl", 0))
                        m_used = int(info.get("margin", ct_obj.get("margin", 1000)))
                        lev_used = float(ct_obj.get("leverage") or info.get("leverage", 35.0))
                        exact_fee = int(ct_obj.get("opening_fee", 0) or 0) + int(ct_obj.get("closing_fee", 0) or 0)
                        was_harvested = bool(info.get("ai_closed", False))
                        orig_st = info.get("state", state)
                        act_taken = int(info.get("action", 2))
                        in_rec = (current_mode == "RECOVERY") or (recovery_anchor_equity is not None)
                        beat_anchor = (recovery_anchor_equity is not None and balance >= int(round(recovery_anchor_equity * 1.03)))

                        # Detección real de TP / SL a partir del objeto cerrado de LN Markets
                        hit_tp_real = bool(ct_obj.get("takeprofit_wi")) or (pl > m_used * 0.16 and not bool(ct_obj.get("stoploss_wi")))

                        brain.remember_and_train(
                            orig_st, act_taken, pl, m_used, state,
                            equity_after=balance, available_balance_after=balance,
                            hit_tp=hit_tp_real, harvested=was_harvested,
                            recovery_mode=in_rec, exited_recovery_bonus=beat_anchor,
                            lev_used=lev_used, exact_fee_sats=exact_fee
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
                last_known_balance = balance

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

            if peak_equity is None:
                resynced, synced_peak, synced_floor = sync_brain_floor_with_reality(brain, equity, floor_ratio=Config.INITIAL_FLOOR_RATIO)
                peak_equity = synced_peak
                if resynced:
                    brain.save_brain(force=True)
                    print(f"  🔄 [SINCRONIZACIÓN DE CAPITAL REAL] Peak={peak_equity:,} sats | Piso Duro (75%)={synced_floor:,} sats")
            elif equity > peak_equity:
                peak_equity = equity
                brain.peak_equity = peak_equity
                brain.locked_floor = max(brain.locked_floor, int(round(peak_equity * Config.INITIAL_FLOOR_RATIO)))
                brain.dirty = True

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
            prot = floor_guardian.evaluate_protection(equity, balance, peak_equity, brain.locked_floor, daily_pnl_pct)
            current_dd_pct = prot["current_dd_pct"]
            floor_threat = prot["should_liquidate"]

            # CORRECCIÓN P0.1: El porcentaje hacia la meta se mide respecto al BALANCE DISPONIBLE real
            goal_pct = (balance / max(brain.current_target, 1)) * 100.0
            sprint_mode = goal_pct >= 88.0

            perception = PERCEPTION_ENGINE.evaluate(state, ind_5m, ind_15m, ind_1h, ind_4h, now=now)
            ovr_tag = perception["override_side"] or "OFF"
            q_vec = brain.get_state_q_values(state)

            edge_scores_by_tid = {}
            for _t in running:
                _tid = str(_t["id"])
                _meta = brain.active_trades.get(_tid, {})
                _act = int(_meta.get("action", 2 if _t.get("side", "buy") == "buy" else 6))
                _pnl_atr = float(_meta.get("last_pnl_atr", 0.0))
                _q_act = float(q_vec[_act]) if _act < len(q_vec) else 0.0
                _ovr = perception.get("override_side")
                _ovr_bias = -0.35 if ((_act <= 4 and _ovr == "BEAR") or (_act >= 5 and _ovr == "BULL")) else (0.15 if ((_act <= 4 and _ovr == "BULL") or (_act >= 5 and _ovr == "BEAR")) else 0.0)
                edge_score = round(_q_act + (0.20 * _pnl_atr) + _ovr_bias, 4)
                edge_scores_by_tid[_tid] = edge_score
                if edge_score < 0.0:
                    print(f"  ⚠️ [DEGRADACIÓN DE EDGE] ID:{_tid[:6]} | Act:{_act} | Edge:{edge_score:+.3f} (Q:{_q_act:+.3f} | PnL:{_pnl_atr:+.2f}ATR | OVR:{ovr_tag})")

            if floor_threat and (now >= pause_until):
                print(f"  🚨 [GUARDIÁN PISO DURO UNIFICADO] {prot['reason']} | Eq:{equity:,} <= Piso:{brain.locked_floor:,}. Liquidando y pausando 45m.")
                for t_kill in list(running):
                    try:
                        confirm_and_close_trade(client, brain, str(t_kill["id"]), brain.active_trades.get(str(t_kill["id"]), {}), "HARD_FLOOR_KILL_SWITCH")
                    except Exception:
                        pass
                running = []
                brain.active_trades.clear()
                pause_until = now + 2700
                peak_equity = equity
                brain.peak_equity = equity
                brain.locked_floor = int(round(equity * Config.INITIAL_FLOOR_RATIO))
                brain.dirty = True
                brain.save_brain(force=True)
                current_dd_pct = 0.0
                floor_threat = False

            current_mode, new_entries_allowed, recovery_mode, recovery_anchor_equity, exited_recovery = evaluate_system_mode(
                now, pause_until, ind_5m, daily_pnl_pct, current_dd_pct,
                floor_threat, equity, recovery_anchor_equity
            )

            mods = brain.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)
            latency_ms = int(round((time.time() - cycle_t0) * 1000.0))
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}][{current_mode}][{state}]"
                f"[1h:{mods['name_1h'][:4]}|4h:{mods['name_4h'][:4]}|OVR:{ovr_tag}] "
                f"${ind_5m['price']:.1f} | Bal:{balance:,} ({goal_pct:.1f}% Meta) | Eq:{equity:,} | "
                f"Act:{len(running)}/{Config.MAX_CONCURRENT_TRADES} | DD:{current_dd_pct*100:.1f}% | {latency_ms}ms"
            )

            still_running = []
            if running:
                for trade in running:
                    tid = str(trade["id"])
                    t_meta = brain.active_trades.setdefault(tid, infer_adopted_trade(trade, ind_5m, state, now))
                    closed_by_guardian = execute_strategy_guardian(
                        client, brain, trade, ind_5m, ind_15m, t_meta, current_mode,
                        edge_score=edge_scores_by_tid.get(tid, 0.0), perception=perception
                    )
                    if not closed_by_guardian:
                        still_running.append(trade)
                evaluate_open_trades_online(brain, still_running, ind_5m, state, now)

            active_count = max(len(still_running), len(brain.active_trades))
            capital_max_pos = max(1, min(Config.MAX_CONCURRENT_TRADES, int((equity * 0.60) // 1000)))
            if recovery_mode:
                dynamic_max_pos = min(capital_max_pos, 1 if equity < 5000 else 2)
            elif "CHOP" in state and perception.get("override_side") is None:
                dynamic_max_pos = min(capital_max_pos, 1 if equity < 4500 else 2)
            else:
                dynamic_max_pos = capital_max_pos

            effective_cooldown = Config.ENTRY_COOLDOWN_SEC * (
                0.80 if perception.get("override_side") is not None
                else (1.8 if (recovery_mode or "CHOP" in state) else 1.0)
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
                        brain.remember_and_train(state, 0, 0, 1000, state, is_no_trade=True)
                        print(f"  ⏸️ [FILTRO SATS/SEGURIDAD] Entrada bloqueada ({gate_reason} | OVR:{perception.get('override_reason')})")
                    if action > 0:
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

                        adj_risk = min(0.12, max(0.03, base_risk * kelly_mult))
                        margin_sats = max(1000, int(round(equity * adj_risk)))

                        # CORRECCIÓN P0.6: Verificar exposición proyectada (incluyendo el margen nuevo y el mínimo de 1000 sats)
                        est_entry_fee = max(1, int(round(margin_sats * lev * Config.TAKER_FEE)))
                        exp_ok, exp_reason, proj_exp = SatsExpectancyGate.verify_projected_exposure(
                            locked_margin, margin_sats, equity, balance,
                            max_exposure_ratio=Config.SOFT_EXPOSURE_LIMIT,
                            est_entry_fee_sats=est_entry_fee,
                            max_single_trade_risk_pct=Config.MAX_SINGLE_TRADE_MARGIN_PCT,
                        )
                        viable_sats, sats_meta = SatsExpectancyGate.verify_net_sats_expectancy(
                            ind_5m["price"], margin_sats, lev, sl, tp
                        )
                        if viable_sats and (recovery_mode or "RANGE_SQUEEZE" in state):
                            min_strict_tp = max(38 if recovery_mode else 32, int(round(sats_meta["fee_sats"] * (2.8 + current_dd_pct * 3.0 if recovery_mode else 2.5))))
                            if sats_meta["net_tp_sats"] < min_strict_tp:
                                viable_sats = False

                        if not exp_ok:
                            print(f"  ⏸️ [EXPOSICIÓN PROYECTADA BLOQUEADA] {exp_reason} (Margen:{margin_sats} sats)")
                        elif not viable_sats:
                            print(
                                f"  ⏸️ [EXPECTANCY SATS INSUFICIENTE] NetTP:+{sats_meta['net_tp_sats']} sats vs "
                                f"Fee:{sats_meta['fee_sats']} sats (Ratio:{sats_meta['fee_ratio']}x)"
                            )
                        elif safe_mode:
                            print(
                                f"  🛡️ [MODO SEGURO - SIMULACIÓN] Señal válida {strat['name']} "
                                f"(Lev {lev}x | Margen {margin_sats} sats | SL:{sl} | TP:{tp} | ExpProy:{proj_exp*100:.1f}%)"
                            )
                        else:
                            live_running = client.get_running_trades()
                            if len(live_running) < min(dynamic_max_pos, Config.MAX_CONCURRENT_TRADES):
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
                                            f"(Lev {lev}x | Margen {margin_sats} sats | ExpProy:{proj_exp*100:.1f}% | Salida:{strat['exit_mode']})"
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

        if once_mode:
            break

        # Ciclo adaptativo de alta frecuencia de análisis
        if PERCEPTION_ENGINE.override_side is not None:
            loop_wait = Config.FAST_ANALYZE_INTERVAL_SEC
        elif len(brain.active_trades) > 0:
            loop_wait = Config.ACTIVE_ANALYZE_INTERVAL_SEC
        else:
            loop_wait = Config.IDLE_ANALYZE_INTERVAL_SEC
        time.sleep(loop_wait)


if __name__ == "__main__":
    main()
