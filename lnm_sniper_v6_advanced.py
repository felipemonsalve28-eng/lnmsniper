# -*- coding: utf-8 -*-
"""
lnm_sniper_v6_advanced.py
Motor Principal LNM Sniper v7.3 Consolidado (Dual Mainnet & Testnet Explorer).
Incluye Caché MTF Persistente Compartida, Telemetría por Estilo, Pool HTTP Resiliente
y Separación Estricta de Cerebros y Credenciales.
"""
import os
import re
import sys
import time
import math
import json
import hmac
import signal
import shutil
import random
import hashlib
import base64
import tempfile
import urllib.parse
from collections import deque
from datetime import datetime, date

import requests
try:
    import numpy as np
except ImportError:
    from guardian_execution_core import compat_np as np

from guardian_execution_core import (
    AtomicBrainStore,
    ResilientLNMClient,
    HardFloorGuardian,
    FileLockContext,
    LatencyTelemetry,
    InFlightOrderGuard,
    rotate_log_file_if_needed,
)
from sniper_perception_sats_core import (
    MarketRegimeClassifier,
    ShortTermPerceptionEngine,
    SatsExpectancyGate,
    sync_brain_floor_with_reality,
)
from hedge_state_store import HedgeStateStore
from portfolio_hedge import PortfolioHedgeCoordinator
from adaptive_controller import AdaptiveHedgeController
from learning_thresholds_manager import get_thresholds_manager
from learning_quality_supervisor import LearningQualitySupervisor, AtomicTelemetryDispatcher
from liquidity_radar import (
    DeterministicMTFAggregator,
    LiquidityConfig,
    LiquidityRadar,
    TF_POOL_WEIGHTS,
    compute_liquidity_bias,
    compute_opposite_pool_room,
    compute_structural_td9_trailing_stop,
)

PERCEPTION_ENGINE = ShortTermPerceptionEngine()
THRESHOLDS_MANAGER = get_thresholds_manager()
TELEMETRY_DISPATCHER = AtomicTelemetryDispatcher()
from limit_execution_manager import (
    LimitExecutionConfig,
    LimitExecutionManager,
    compute_limit_entry_plan,
    should_route_limit,
)
from side_streak_brake import SideStreakBrakeStore
from binance_micro_sidecar import read_binance_micro_snapshot

LIQUIDITY_RADAR = LiquidityRadar()


def _build_binance_micro_telemetry(lnm_price, now=None, state_path=None):
    """Construye el bloque compacto shadow_only de microestructura Binance sin bloquear el bucle."""
    try:
        snap = read_binance_micro_snapshot(state_path=state_path, now_ts=now, ttl_sec=15.0)
        available = bool(snap.get("available", False))
        stale = bool(snap.get("stale", True))
        raw_age = snap.get("age_sec")
        age_sec = round(float(raw_age), 3) if raw_age is not None else None
        bin_px = float(snap.get("last_price", 0.0) or 0.0)
        lnm_px = float(lnm_price or 0.0)
        if available and bin_px > 0.0 and lnm_px > 0.0:
            basis_bps = round(((bin_px - lnm_px) / lnm_px) * 10000.0, 2)
        else:
            basis_bps = None
        return {
            "available": available,
            "stale": stale,
            "age_sec": age_sec,
            "basis_bps": basis_bps,
            "cvd_1m_usd": round(float(snap.get("cvd_1m_usd", 0.0) or 0.0), 2),
            "cvd_5m_usd": round(float(snap.get("cvd_5m_usd", 0.0) or 0.0), 2),
            "liq_buy_1m_usd": round(float(snap.get("liq_buy_1m_usd", 0.0) or 0.0), 2),
            "liq_sell_1m_usd": round(float(snap.get("liq_sell_1m_usd", 0.0) or 0.0), 2),
            "funding_rate": round(float(snap.get("funding_rate", 0.0) or 0.0), 8),
            "open_interest_btc": round(float(snap.get("open_interest_btc", 0.0) or 0.0), 4),
        }
    except Exception:
        return {
            "available": False,
            "stale": True,
            "age_sec": None,
            "basis_bps": None,
            "cvd_1m_usd": 0.0,
            "cvd_5m_usd": 0.0,
            "liq_buy_1m_usd": 0.0,
            "liq_sell_1m_usd": 0.0,
            "funding_rate": 0.0,
            "open_interest_btc": 0.0,
        }


def _evaluate_liquidity_from_mtf(mtf_cache, ind_5m, ind_15m, lnm_price):
    """Evalúa LiquidityRadar en O(1) sobre velas cacheadas con invarianza al basis BTCUSD/BTCUSDT."""
    try:
        cfg = LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
        if not cfg.enabled:
            return LIQUIDITY_RADAR.evaluate(None, None, 1.0, 1.0, float(lnm_price or 0.0), config_override=cfg)
        c5_item = mtf_cache.cache.get("5m", {})
        c15_item = mtf_cache.cache.get("15m", {})
        d5 = c5_item.get("data")
        d15 = c15_item.get("data")
        b_ratio = float(c5_item.get("basis_ratio") or 1.0)
        if not (0.95 <= b_ratio <= 1.05):
            b_ratio = 1.0
        spot_lnm = float(lnm_price or (ind_5m or {}).get("price") or 0.0)
        spot_bin = (spot_lnm / b_ratio) if spot_lnm > 0.0 else 0.0
        atr_5m_bin = max(1.0, float((ind_5m or {}).get("atr") or 1.0) / b_ratio)
        atr_15m_bin = max(1.0, float((ind_15m or {}).get("atr") or atr_5m_bin) / b_ratio)
        sig = LIQUIDITY_RADAR.evaluate(
            candles_5m=d5,
            candles_15m=d15,
            atr_5m=atr_5m_bin,
            atr_15m=atr_15m_bin,
            spot_price=spot_bin,
            volumes_5m=c5_item.get("vol"),
            volumes_15m=c15_item.get("vol"),
            config_override=cfg,
        )
        if abs(b_ratio - 1.0) > 1e-6:
            for _pk in ("swept_level", "wick_extreme", "nearest_high_pool", "nearest_low_pool"):
                if float(sig.get(_pk) or 0.0) > 0.0:
                    sig[_pk] = round(float(sig[_pk]) * b_ratio, 2)
        return sig
    except Exception:
        return LIQUIDITY_RADAR.evaluate(None, None, 1.0, 1.0, 0.0, config_override=LiquidityConfig(enabled=False))


# =====================================================================
# 1. CONFIGURACIÓN CENTRALIZADA Y PERFILES DINÁMICOS (MAINNET / TESTNET)
# =====================================================================
class Config:
    ENV_MODE = "mainnet"
    EXPLORER_MODE = False
    BASE_URL = "https://api.lnmarkets.com/v3"
    BRAIN_FILE = "q_brain_advanced_lnm.json"
    HEDGE_STATE_FILE = "hedge_state_mainnet.json"
    ENABLE_HYBRID_HEDGE = False
    DETERMINISTIC_TREND_POLICY = True
    FE_OFF_TREND = True
    PURE_TREND_ONLY = True
    TREND_SL_FLOOR_PCT = 0.0065
    SHORT_EXP_PHYS_HOLD = True
    SHORT_EXP_MIN_DSL = 0.006505
    STRUCTURAL_TD9_TRAILING_ENABLED = False
    STREAK3_24H_ENABLED = True
    STREAK3_MAX_LOSSES = 3
    STREAK3_PAUSE_SEC = 86400.0
    BASE_RISK_PCT_STD = 0.0075
    DD_THROTTLE_HALT_PCT = 0.045
    MAX_DD_GUARD_PCT = 0.052
    BRAIN_SAVE_INTERVAL = 30.0
    SHADOW_MODE = os.getenv("SNIPER_SHADOW_MODE", "0").lower() in ("1", "true", "yes")
    SHARED_MTF_DISK_CACHE = os.path.join(tempfile.gettempdir(), "sniper_mtf_candles_v73.json")
    MIN_MARGIN_SATS = 1000

    # Meta dinámica basada EXCLUSIVAMENTE en Balance Disponible (Sats Netos)
    INITIAL_TARGET_SATS = 300000
    TARGET_STEP_MULTIPLIER = 1.50
    INITIAL_FLOOR_RATIO = 0.75
    MILESTONE_FLOOR_RATIO = 0.88

    # Gestión de apalancamiento, riesgo y concurrencia estricta
    MIN_LEVERAGE = 15
    MAX_LEVERAGE = 85
    TAKER_FEE = 0.001
    WARMUP_TRADES = 20
    RISK_WARMUP = 0.05
    RISK_NORMAL = 0.10
    MAX_SINGLE_TRADE_MARGIN_PCT = 0.20
    MIN_EFFECTIVE_RISK_WARMUP = 0.025
    MIN_EFFECTIVE_RISK_NORMAL = 0.030
    MAX_CONCURRENT_TRADES = 5
    SOFT_EXPOSURE_LIMIT = 0.38
    ENTRY_COOLDOWN_SEC = 35.0

    # Frecuencia de análisis adaptativa de alta velocidad
    FAST_ANALYZE_INTERVAL_SEC = 5.0
    ACTIVE_ANALYZE_INTERVAL_SEC = 7.0
    IDLE_ANALYZE_INTERVAL_SEC = 11.0
    USER_SYNC_TTL_SEC = 10.0

    # Aprendizaje intermedio táctico (desacoplado de la Q-table cuando decouple_floating_pnl=1)
    ENABLE_INTERMEDIATE_LEARNING = False
    INTERMEDIATE_EVAL_INTERVAL_SEC = 180.0
    ALPHA_INTERMEDIATE = 0.018
    INTERMEDIATE_DECAY_PER_EVAL = 0.82
    MAX_INTERMEDIATE_REWARD_CLIP = 0.10
    MIN_INTERMEDIATE_DELTA_ATR = 0.18

    # Controlador de degradación de Edge en el Guardian
    EDGE_DEGRADATION_WARN = -0.22
    EDGE_DEGRADATION_TIGHTEN = -0.38
    EDGE_DEGRADATION_EXIT = -0.60
    EDGE_DEGRADATION_PERSISTENCE = 3

    @classmethod
    def sync_from_thresholds(cls, mgr=None):
        """Sincroniza parámetros dinámicos desde config/learning_thresholds.json."""
        tm = mgr or get_thresholds_manager()
        cls.TAKER_FEE = tm.get_float("fees", "taker_fee_rate", 0.001)
        cls.ENABLE_INTERMEDIATE_LEARNING = (tm.get_int("reward", "decouple_floating_pnl", 1) == 0)
        cls.EDGE_DEGRADATION_PERSISTENCE = tm.get_int("market_sensitivity", "edge_degradation_persistence", 3)
        cls.EDGE_DEGRADATION_WARN = -abs(tm.get_float("market_sensitivity", "edge_degradation_warn", 0.40))
        cls.EDGE_DEGRADATION_TIGHTEN = -abs(tm.get_float("market_sensitivity", "edge_degradation_tighten", 0.60))
        cls.EDGE_DEGRADATION_EXIT = -abs(tm.get_float("market_sensitivity", "edge_degradation_exit", 0.78))
        raw_atr_ext = tm.get_float("market_sensitivity", "atr_extreme_pct", 0.85)
        atr_ext_frac = (raw_atr_ext / 100.0) if raw_atr_ext > 0.05 else max(0.001, raw_atr_ext)
        cls.ATR_EXTREME_PCT = round(max(0.0120, atr_ext_frac * 1.65), 5)

    # Umbrales de seguridad y recuperación auditable
    MAX_DAILY_LOSS_PCT = 0.11
    MAX_DRAWDOWN_PCT = 0.16
    KILL_SWITCH_DD_PCT = 0.22
    CONSECUTIVE_LOSS_LIMIT = 4
    CONSECUTIVE_LOSS_PAUSE_SEC = 6 * 60

    RECOVERY_ENTER_DAILY = -0.075
    RECOVERY_EXIT_DAILY = -0.045
    RECOVERY_ENTER_DD = 0.035
    RECOVERY_EXIT_DD = 0.025
    RECOVERY_BOUNCE_GAIN_PCT = 0.018
    RECOVERY_RISK_MULT = 0.45
    RECOVERY_RR_MULT = 1.15

    ATR_EXTREME_PCT = 0.0145
    ANTI_MARTINGALE_FACTOR = 0.78
    DRAWDOWN_RISK_REDUCTION = 0.70
    DRAWDOWN_RISK_THRESHOLD = 0.085

    # Salud de red y TTL de caché MTF (210 velas para EMA 200 exacta)
    API_CB_ERROR_THRESHOLD = 3
    API_CB_MAX_COOLDOWN_SEC = 120
    CANDLE_LIMIT = 210
    TTL_1M = 12.0
    TTL_5M = 25.0
    TTL_15M = 75.0
    TTL_1H = 240.0
    TTL_4H = 480.0
    TTL_1D = 1800.0

    NUM_ACTIONS = 9
    NUM_MACRO_ACTIONS = 4

    @classmethod
    def configure_environment(cls, env_name="mainnet"):
        raw_env = str(env_name or os.getenv("ENV_MODE") or os.getenv("SNIPER_ENV") or "mainnet").strip().lower()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if raw_env in ("testnet", "test", "explorer"):
            cls.ENV_MODE = "testnet"
            cls.EXPLORER_MODE = False if getattr(cls, "DETERMINISTIC_TREND_POLICY", True) else True
            cls.BASE_URL = os.getenv("LNM_TESTNET_BASE_URL", "https://api.signet.lnmarkets.com/v3")
            cls.BRAIN_FILE = "q_brain_testnet_lnm.json"
            cls.HEDGE_STATE_FILE = "hedge_state_testnet.json"
            cls.TELEMETRY_FILE = "telemetry_live_testnet.json"
            cls.CONFIG_LIVE_FILE = os.path.join(base_dir, "config", "config_live_testnet.json")
            cls.PID_FILE = os.path.join(base_dir, "sniper_testnet.pid")
            cls.SHARED_MTF_DISK_CACHE = os.path.join(tempfile.gettempdir(), "sniper_mtf_candles_v73_testnet.json")
            cls.MAX_CONCURRENT_TRADES = 5
            cls.SOFT_EXPOSURE_LIMIT = 0.45
            cls.ENTRY_COOLDOWN_SEC = 18.0
            cls.RISK_NORMAL = 0.08
            cls.RISK_WARMUP = 0.06
            cls.MAX_DRAWDOWN_PCT = 0.26
            cls.KILL_SWITCH_DD_PCT = 0.35
            cls.ENABLE_HYBRID_HEDGE = False
            cls.DETERMINISTIC_TREND_POLICY = True
            cls.FE_OFF_TREND = True
            cls.PURE_TREND_ONLY = True
            cls.TREND_SL_FLOOR_PCT = 0.0065
            cls.SHORT_EXP_PHYS_HOLD = True
            cls.SHORT_EXP_MIN_DSL = 0.006505
            cls.STRUCTURAL_TD9_TRAILING_ENABLED = False
            cls.BASE_RISK_PCT_STD = 0.0075
            cls.RECOVERY_ENTER_DD = 0.035
            cls.RECOVERY_EXIT_DD = 0.025
            cls.DD_THROTTLE_HALT_PCT = 0.045
            cls.MAX_DD_GUARD_PCT = 0.052
        else:
            cls.ENV_MODE = "mainnet"
            cls.EXPLORER_MODE = False
            cls.BASE_URL = os.getenv("LNM_MAINNET_BASE_URL", "https://api.lnmarkets.com/v3")
            cls.BRAIN_FILE = "q_brain_advanced_lnm.json"
            cls.HEDGE_STATE_FILE = "hedge_state_mainnet.json"
            cls.TELEMETRY_FILE = "telemetry_live_mainnet.json"
            cls.CONFIG_LIVE_FILE = os.path.join(base_dir, "config", "config_live_mainnet.json")
            cls.PID_FILE = os.path.join(base_dir, "sniper_mainnet.pid")
            cls.SHARED_MTF_DISK_CACHE = os.path.join(tempfile.gettempdir(), "sniper_mtf_candles_v73_mainnet.json")
            cls.MAX_CONCURRENT_TRADES = 5
            cls.SOFT_EXPOSURE_LIMIT = 0.38
            cls.ENTRY_COOLDOWN_SEC = 35.0
            cls.RISK_NORMAL = 0.06
            cls.RISK_WARMUP = 0.04
            cls.MAX_DRAWDOWN_PCT = 0.16
            cls.KILL_SWITCH_DD_PCT = 0.22
            cls.ENABLE_HYBRID_HEDGE = False
            cls.DETERMINISTIC_TREND_POLICY = True
            cls.FE_OFF_TREND = True
            cls.PURE_TREND_ONLY = True
            cls.TREND_SL_FLOOR_PCT = 0.0065
            cls.SHORT_EXP_PHYS_HOLD = True
            cls.SHORT_EXP_MIN_DSL = 0.006505
            cls.STRUCTURAL_TD9_TRAILING_ENABLED = False
            cls.BASE_RISK_PCT_STD = 0.0075
            cls.RECOVERY_ENTER_DD = 0.035
            cls.RECOVERY_EXIT_DD = 0.025
            cls.DD_THROTTLE_HALT_PCT = 0.045
            cls.MAX_DD_GUARD_PCT = 0.052
        try:
            THRESHOLDS_MANAGER.switch_environment(cls.ENV_MODE)
            cls.sync_from_thresholds(THRESHOLDS_MANAGER)
        except Exception:
            pass
        try:
            TELEMETRY_DISPATCHER.filepath = os.path.join(base_dir, cls.TELEMETRY_FILE)
        except Exception:
            pass


try:
    Config.sync_from_thresholds(THRESHOLDS_MANAGER)
    THRESHOLDS_MANAGER.register_callback(Config.sync_from_thresholds)
except Exception:
    pass


# =====================================================================
# 2. CARGADOR SEGURO DE CREDENCIALES SEPARADAS (MAINNET vs TESTNET)
# =====================================================================
def load_saved_credentials(env_mode="mainnet"):
    is_test = str(env_mode).lower() == "testnet"
    if is_test:
        key_names = ("LNM_TESTNET_KEY", "LNM_TESTNET_API_KEY", "TESTNET_API_KEY")
        sec_names = ("LNM_TESTNET_SECRET", "LNM_TESTNET_API_SECRET", "TESTNET_API_SECRET")
        pas_names = ("LNM_TESTNET_PASSPHRASE", "TESTNET_PASSPHRASE")
    else:
        key_names = ("LNM_API_KEY", "API_KEY")
        sec_names = ("LNM_API_SECRET", "API_SECRET")
        pas_names = ("LNM_PASSPHRASE", "API_PASSPHRASE")

    k = next((os.getenv(x) for x in key_names if os.getenv(x)), "")
    s = next((os.getenv(x) for x in sec_names if os.getenv(x)), "")
    p = next((os.getenv(x) for x in pas_names if os.getenv(x)), "")
    if k and s and p and "TU_" not in k:
        return k.strip(), s.strip(), p.strip()

    search_files = [
        ".env",
        os.path.expanduser("~/.env"),
        os.path.expanduser("~/.bashrc"),
    ]
    k_pat = "|".join(key_names)
    s_pat = "|".join(sec_names)
    p_pat = "|".join(pas_names)

    for filepath in search_files:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not k or "TU_" in k:
                    m_k = re.search(rf'(?:{k_pat})\s*[=:]\s*["\']?([^"\'\s#]+)["\']?', content)
                    if m_k and "TU_" not in m_k.group(1):
                        k = m_k.group(1)
                if not s or "TU_" in s:
                    m_s = re.search(rf'(?:{s_pat})\s*[=:]\s*["\']?([^"\'\s#]+)["\']?', content)
                    if m_s and "TU_" not in m_s.group(1):
                        s = m_s.group(1)
                if not p or "TU_" in p:
                    m_p = re.search(rf'(?:{p_pat})\s*[=:]\s*["\']?([^"\'\s#]+)["\']?', content)
                    if m_p and "TU_" not in m_p.group(1):
                        p = m_p.group(1)
            except Exception:
                continue
    k_clean, s_clean, p_clean = k.strip(), s.strip(), p.strip()
    if is_test:
        if not k_clean or not s_clean or not p_clean or "TU_" in k_clean or "TU_" in s_clean or "TU_" in p_clean:
            raise SystemExit("ERROR CRITICO: Credenciales Testnet ausentes. Abortando para proteger Mainnet.")
    return k_clean, s_clean, p_clean


# =====================================================================
# 3. MONITOR DE SALUD DE API Y CIRCUIT BREAKER ÁGIL
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
            exponent = min(4, self.consecutive_errors - Config.API_CB_ERROR_THRESHOLD)
            pause_sec = min(Config.API_CB_MAX_COOLDOWN_SEC, int(12 * (1.85 ** exponent)))
            self.cooldown_until = now + pause_sec
            print(f"[API CIRCUIT BREAKER] {self.consecutive_errors} errores seguidos → Pausa defensiva de {pause_sec}s.")

    def check_cooldown(self):
        remaining = self.cooldown_until - time.time()
        if remaining > 0:
            time.sleep(min(remaining, 5.0))
            return True
        return False


# =====================================================================
# 4. GESTOR DE CACHÉ MTF COMPARTIDA EN DISCO + RAM + FALLBACK
# =====================================================================
class MultiTimeframeCache:
    def __init__(self):
        self.session = ResilientLNMClient.build_session("LNMSniper/7.3-MTFCache")
        self.disk_path = Config.SHARED_MTF_DISK_CACHE
        self.ram_aggregator = DeterministicMTFAggregator(max_history=max(256, int(Config.CANDLE_LIMIT)))
        self.cache = {
            "1m":  {"last": 0.0, "ttl": Config.TTL_1M,  "period": 60,    "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0},
            "5m":  {"last": 0.0, "ttl": Config.TTL_5M,  "period": 300,   "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0},
            "15m": {"last": 0.0, "ttl": Config.TTL_15M, "period": 900,   "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0},
            "30m": {"last": 0.0, "ttl": Config.TTL_15M * 2.0, "period": 1800, "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0, "ram_derived": True},
            "45m": {"last": 0.0, "ttl": Config.TTL_15M * 3.0, "period": 2700, "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0, "ram_derived": True},
            "1h":  {"last": 0.0, "ttl": Config.TTL_1H,  "period": 3600,  "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0},
            "4h":  {"last": 0.0, "ttl": Config.TTL_4H,  "period": 14400, "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0},
            "1d":  {"last": 0.0, "ttl": Config.TTL_1D,  "period": 86400, "data": None, "vol": None, "ts": None, "open": None, "prefix": None, "ind_cache": None, "ind_price": 0.0},
        }
        self._load_shared_disk_cache()

    def push_1m_candle(self, ts, o, h, l, c, v=0.0, is_closed=True):
        self.ram_aggregator.push_1m(ts, o, h, l, c, v=v, is_closed=is_closed)

    def ingest_1m_ohlcv(self, ts_arr, o_arr, h_arr, l_arr, c_arr, v_arr=None, last_is_live=True):
        self.ram_aggregator.ingest_1m_series(
            ts_arr, o_arr, h_arr, l_arr, c_arr, v_seq=v_arr, last_is_live=last_is_live
        )

    def get_ram_candles(self, tf, include_forming=True):
        return self.ram_aggregator.get_hlc_arrays(tf, include_forming=include_forming)

    def get_ram_ohlcv(self, tf, include_forming=True):
        return self.ram_aggregator.get_ohlcv(tf, include_forming=include_forming)

    def get_session_vwap(self):
        return self.ram_aggregator.get_session_vwap()

    def get_td9_state(self):
        return self.ram_aggregator.get_td9_state()

    def get_structural_context(self, atr_ref, spot_price, radar=None, cfg=None):
        rad = radar or LIQUIDITY_RADAR
        conf = cfg or LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
        tf_ohlcv = {}
        for _tf in ("15m", "30m", "45m", "1h", "4h"):
            _o, _h, _l, _c, _v, _ts = self.ram_aggregator.get_ohlcv(_tf, include_forming=True)
            if len(_c) > 0:
                tf_ohlcv[_tf] = (_h, _l, _c, _ts)
        hierarchy = rad.evaluate_multi_tf_hierarchy(
            tf_ohlcv=tf_ohlcv,
            atr_ref=atr_ref,
            spot_price=spot_price,
            config_override=conf,
        )
        return {
            "hierarchy": hierarchy,
            "td9": self.ram_aggregator.get_td9_state(),
            "session_vwap": self.ram_aggregator.get_session_vwap(),
        }

    def _load_shared_disk_cache(self):
        if not os.path.exists(self.disk_path):
            return
        try:
            with FileLockContext(self.disk_path, exclusive=False, timeout=1.5):
                with open(self.disk_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            now = time.time()
            for tf, entry in raw.items():
                if tf in self.cache and isinstance(entry, dict):
                    ts = float(entry.get("last", 0.0))
                    if (now - ts) <= 900.0 and "h" in entry and "l" in entry and "c" in entry:
                        h_arr = np.array(entry["h"], dtype=np.float64)
                        l_arr = np.array(entry["l"], dtype=np.float64)
                        c_arr = np.array(entry["c"], dtype=np.float64)
                        self.cache[tf]["data"] = (h_arr, l_arr, c_arr)
                        v_arr = None
                        if "v" in entry and entry["v"] is not None:
                            v_arr = np.array(entry["v"], dtype=np.float64)
                            self.cache[tf]["vol"] = v_arr
                        if "ts" in entry and entry["ts"] is not None:
                            self.cache[tf]["ts"] = np.array(entry["ts"], dtype=np.float64)
                        if "o" in entry and entry["o"] is not None:
                            self.cache[tf]["open"] = np.array(entry["o"], dtype=np.float64)
                        self.cache[tf]["last"] = ts
                        self.cache[tf]["prefix"] = None
                        self.cache[tf]["ind_cache"] = None
                        if (
                            tf == "1m"
                            and self.cache[tf]["ts"] is not None
                            and self.cache[tf]["open"] is not None
                        ):
                            self.ingest_1m_ohlcv(
                                self.cache[tf]["ts"],
                                self.cache[tf]["open"],
                                h_arr,
                                l_arr,
                                c_arr,
                                v_arr=v_arr,
                                last_is_live=True,
                            )
        except Exception:
            pass

    def _save_shared_disk_cache(self):
        try:
            payload = {}
            for tf, item in self.cache.items():
                if item.get("ram_derived"):
                    continue
                if item["data"] is not None:
                    h, l, c = item["data"]
                    entry_payload = {
                        "last": item["last"],
                        "h": h.tolist(),
                        "l": l.tolist(),
                        "c": c.tolist(),
                    }
                    if item.get("vol") is not None:
                        entry_payload["v"] = item["vol"].tolist()
                    if tf == "1m" and item.get("ts") is not None and item.get("open") is not None:
                        entry_payload["ts"] = item["ts"].tolist()
                        entry_payload["o"] = item["open"].tolist()
                    payload[tf] = entry_payload
            with FileLockContext(self.disk_path, exclusive=True, timeout=1.5):
                tmp_p = f"{self.disk_path}.tmp.{os.getpid()}"
                with open(tmp_p, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(tmp_p, self.disk_path)
        except Exception:
            pass

    def invalidate(self, tf):
        if tf in self.cache:
            self.cache[tf]["last"] = 0.0
            self.cache[tf]["prefix"] = None
            self.cache[tf]["ind_cache"] = None

    def _ensure_raw_candles(self, tf, current_lnm_price=0.0):
        now = time.time()
        item = self.cache[tf]
        if tf in ("30m", "45m"):
            if self.ram_aggregator.closed_count("1m") == 0 and self.cache["1m"]["data"] is None:
                self._ensure_raw_candles("1m", current_lnm_price)
            h_ram, l_ram, c_ram = self.get_ram_candles(tf, include_forming=True)
            if len(c_ram) > 0:
                item["data"] = (h_ram, l_ram, c_ram)
                _, _, _, _, v_ram, _ = self.ram_aggregator.get_ohlcv(tf, include_forming=True)
                item["vol"] = v_ram
                item["last"] = now
                return item["data"]
            if self.cache["1m"]["data"] is not None:
                return self.cache["1m"]["data"]
            if current_lnm_price > 0:
                p = float(current_lnm_price)
                synth_c = np.full(Config.CANDLE_LIMIT, p, dtype=np.float64)
                return synth_c * 1.0012, synth_c * 0.9988, synth_c
            raise RuntimeError(f"Fallo crítico obteniendo velas TF {tf}: sin fuente 1m")

        if tf in ("15m", "1h", "4h") and self.ram_aggregator.has_enough_bars(tf, min_bars=Config.CANDLE_LIMIT):
            h_ram, l_ram, c_ram = self.get_ram_candles(tf, include_forming=True)
            item["data"] = (h_ram, l_ram, c_ram)
            _, _, _, _, v_ram, _ = self.ram_aggregator.get_ohlcv(tf, include_forming=True)
            item["vol"] = v_ram
            item["last"] = now
            return item["data"]

        crossed_boundary = (int(now) // item["period"]) != (int(item["last"]) // item["period"])

        if item["data"] is None or (now - item["last"]) >= item["ttl"] or crossed_boundary:
            self._load_shared_disk_cache()
            crossed_after_disk = (int(now) // item["period"]) != (int(item["last"]) // item["period"])
            if item["data"] is None or (now - item["last"]) >= item["ttl"] or crossed_after_disk:
                try:
                    fetched = self._fetch_remote(tf)
                    if isinstance(fetched, tuple) and len(fetched) == 4:
                        h, l, c, v = fetched
                        item["vol"] = v
                    else:
                        h, l, c = fetched
                    item["data"] = (h, l, c)
                    item["last"] = now
                    item["prefix"] = None
                    item["ind_cache"] = None
                    self._save_shared_disk_cache()
                except Exception as e:
                    if item["data"] is None:
                        if current_lnm_price > 0:
                            p = float(current_lnm_price)
                            synth_c = np.full(Config.CANDLE_LIMIT, p, dtype=np.float64)
                            synth_h = synth_c * 1.0012
                            synth_l = synth_c * 0.9988
                            item["data"] = (synth_h, synth_l, synth_c)
                            item["vol"] = None
                            item["last"] = now - (item["ttl"] * 0.8)
                            item["prefix"] = None
                            item["ind_cache"] = None
                        else:
                            raise RuntimeError(f"Fallo crítico obteniendo velas TF {tf}: {e}")
        return item["data"]

    def get_candles(self, tf, current_lnm_price=0.0):
        h_arr, l_arr, c_arr = self._ensure_raw_candles(tf, current_lnm_price)
        if tf in ("1m", "5m", "15m") and current_lnm_price > 0:
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
        h_arr, l_arr, c_arr = self._ensure_raw_candles(tf, current_lnm_price)
        if tf in ("1h", "4h", "1d") and item["ind_cache"] is not None:
            return item["ind_cache"]
        if tf in ("1m", "5m", "15m") and item["ind_cache"] is not None and abs(current_lnm_price - item["ind_price"]) < 0.5:
            return item["ind_cache"]

        vols = item.get("vol")
        # Actualización O(1) incremental sin copia de arrays para 1m/5m/15m (Fase 6 Termux)
        if tf in ("1m", "5m", "15m") and len(c_arr) >= 26:
            if item.get("prefix") is None or (current_lnm_price > 0.0 and not item.get("prefix_has_basis", False)):
                item["prefix"] = _build_indicator_prefix_state(
                    h_arr, l_arr, c_arr, volumes=vols, lnm_price=current_lnm_price
                )
                item["prefix_has_basis"] = bool(current_lnm_price > 0.0)
                if current_lnm_price > 0.0 and len(c_arr) > 0 and float(c_arr[-1]) > 0.0:
                    _br = float(current_lnm_price) / float(c_arr[-1])
                    item["basis_ratio"] = _br if (0.95 <= _br <= 1.05) else 1.0
                else:
                    item["basis_ratio"] = 1.0
            ind = compute_indicators_from_prefix(item["prefix"], current_lnm_price)
        else:
            ind = compute_indicators(h_arr, l_arr, c_arr, current_lnm_price if tf in ("1m", "5m", "15m") else 0.0, volumes=vols)
        item["ind_cache"] = ind
        item["ind_price"] = float(current_lnm_price)
        return ind

    def _fetch_remote_with_vol(self, tf):
        limit = Config.CANDLE_LIMIT
        map_binance = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        interval = map_binance.get(tf, "5m")
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
            r = self.session.get(url, timeout=(2.0, 3.2))
            r.raise_for_status()
            data = r.json()
            if len(data) < 50:
                raise RuntimeError("Respuesta incompleta de Binance")
            ts_arr = np.array([float(c[0]) / 1000.0 for c in data], dtype=np.float64)
            o_arr = np.array([float(c[1]) for c in data], dtype=np.float64)
            h = np.array([float(c[2]) for c in data], dtype=np.float64)
            l = np.array([float(c[3]) for c in data], dtype=np.float64)
            c = np.array([float(c[4]) for c in data], dtype=np.float64)
            v = np.array([float(c[5]) for c in data], dtype=np.float64)
            if tf in self.cache:
                self.cache[tf]["vol"] = v
                self.cache[tf]["ts"] = ts_arr
                self.cache[tf]["open"] = o_arr
            if tf == "1m":
                self.ingest_1m_ohlcv(ts_arr, o_arr, h, l, c, v, last_is_live=True)
            return h, l, c, v
        except Exception:
            map_bybit = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
            by_interval = map_bybit.get(tf, "5")
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval={by_interval}&limit={limit}"
            r = self.session.get(url, timeout=(2.0, 3.5))
            r.raise_for_status()
            rows = list(reversed(r.json()["result"]["list"]))
            if len(rows) < 50:
                raise RuntimeError("Respuesta incompleta de Bybit")
            ts_arr = np.array([float(c[0]) / 1000.0 for c in rows], dtype=np.float64)
            o_arr = np.array([float(c[1]) for c in rows], dtype=np.float64)
            h = np.array([float(c[2]) for c in rows], dtype=np.float64)
            l = np.array([float(c[3]) for c in rows], dtype=np.float64)
            c = np.array([float(c[4]) for c in rows], dtype=np.float64)
            v = np.array([float(c[5]) for c in rows], dtype=np.float64)
            if tf in self.cache:
                self.cache[tf]["vol"] = v
                self.cache[tf]["ts"] = ts_arr
                self.cache[tf]["open"] = o_arr
            if tf == "1m":
                self.ingest_1m_ohlcv(ts_arr, o_arr, h, l, c, v, last_is_live=True)
            return h, l, c, v

    def _fetch_remote(self, tf):
        h, l, c, _v = self._fetch_remote_with_vol(tf)
        return h, l, c


# =====================================================================
# 5. MOTOR VECTORIZADO DE INDICADORES (EMA 9/21/50/200, RSI, BB, ATR)
# =====================================================================
def calc_ema(series, period):
    n = len(series)
    if n == 0:
        return 0.0
    p_eff = max(1, int(period))
    alpha = 2.0 / (p_eff + 1.0)
    ema = float(series[0])
    one_minus_alpha = 1.0 - alpha
    for i in range(1, n):
        ema = alpha * float(series[i]) + one_minus_alpha * ema
    return ema


def _normalize_ohlc_basis(highs, lows, closes, lnm_price=0.0):
    """Normaliza OHLC externos (BTCUSDT) al spot de LN Markets (BTCUSD) sin escalar volumen."""
    if not hasattr(highs, "__sub__"):
        highs = np.array(highs, dtype=np.float64)
    if not hasattr(lows, "__sub__"):
        lows = np.array(lows, dtype=np.float64)
    if not hasattr(closes, "__sub__"):
        closes = np.array(closes, dtype=np.float64)
    if lnm_price > 0.0 and len(closes) > 0 and float(closes[-1]) > 0.0:
        basis_ratio = float(lnm_price) / float(closes[-1])
        if 0.985 <= basis_ratio <= 1.015 and abs(basis_ratio - 1.0) > 1e-6:
            highs = highs * basis_ratio
            lows = lows * basis_ratio
            closes = closes * basis_ratio
    return highs, lows, closes


def compute_indicators(highs, lows, closes, lnm_price=0.0, volumes=None):
    highs, lows, closes = _normalize_ohlc_basis(highs, lows, closes, lnm_price=lnm_price)
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

    if len(closes) >= 26:
        prev20 = closes[-25:-5]
        prev_sma20 = max(float(np.mean(prev20)), 1.0)
        prev_width_pct = (4.0 * float(np.std(prev20))) / prev_sma20
        bb_expansion_rate = bb_width_pct / max(prev_width_pct, 1e-6)
    else:
        bb_expansion_rate = 1.0

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    atr = calc_ema(tr, 14)
    atr_sma50 = float(np.mean(tr[-50:])) if len(tr) >= 10 else atr
    atr_rel_ratio = atr / max(atr_sma50, 1e-6)
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)

    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = ema12 - ema26
    macd_norm = macd_line / max(atr, 1.0)

    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.array([u if (u > d and u > 0) else 0.0 for u, d in zip(up_move, down_move)], dtype=np.float64)
    minus_dm = np.array([d if (d > u and d > 0) else 0.0 for u, d in zip(up_move, down_move)], dtype=np.float64)
    atr_safe = max(atr, 1e-6)
    plus_di = 100.0 * calc_ema(plus_dm, 14) / atr_safe
    minus_di = 100.0 * calc_ema(minus_dm, 14) / atr_safe
    adx = 100.0 * abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-6)

    ref_price = float(lnm_price if lnm_price > 0 else closes[-1])
    if volumes is not None and len(volumes) >= 20 and float(np.sum(volumes[-20:])) > 0:
        typ_p = (highs[-20:] + lows[-20:] + closes[-20:]) / 3.0
        vwap = float(np.sum(typ_p * volumes[-20:]) / max(float(np.sum(volumes[-20:])), 1e-9))
    else:
        vwap = float(sma20)
    price_vs_vwap_pct = (ref_price - vwap) / max(vwap, 1.0)

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

    ind_base = {
        "price": ref_price,
        "high": float(highs[-1]),
        "low": float(lows[-1]),
        "open": float(closes[-2]) if len(closes) > 1 else ref_price,
        "rsi": float(rsi),
        "bb_pos": float(bb_pos),
        "bb_width_pct": float(bb_width_pct),
        "bb_expansion_rate": float(bb_expansion_rate),
        "atr": float(atr),
        "atr_pct": float(atr_pct),
        "atr_rel_ratio": float(atr_rel_ratio),
        "ema9": float(ema9),
        "ema21": float(ema21),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "ema_state": ema_state,
        "ema_score": float(ema_score),
        "ema_macro_spread_pct": float(ema_macro_spread_pct),
        "price_vs_ema200_pct": float(price_vs_ema200_pct),
        "macd_norm": float(macd_norm),
        "adx": float(adx),
        "vwap": float(vwap),
        "price_vs_vwap_pct": float(price_vs_vwap_pct),
    }
    reg_info = MarketRegimeClassifier.classify(ind_base)
    ind_base["regime"] = reg_info["canonical_regime"]
    ind_base["market_regime"] = reg_info["market_regime"]
    ind_base["volatility_state"] = reg_info["volatility_state"]
    return ind_base


def _build_indicator_prefix_state(highs, lows, closes, volumes=None, lnm_price=0.0):
    """
    Precalcula una sola vez por vela el estado prefijo de las primeras N-1 velas
    cerradas para permitir actualizaciones O(1) en cada tick sin copiar arrays.
    """
    highs, lows, closes = _normalize_ohlc_basis(highs, lows, closes, lnm_price=lnm_price)
    n_total = len(closes)
    h_pre = highs[:-1]
    l_pre = lows[:-1]
    c_pre = closes[:-1]

    deltas_pre = np.diff(c_pre)
    seed = deltas_pre[:14]
    up_prev = float(seed[seed >= 0].sum()) / 14.0
    down_prev = float(-seed[seed < 0].sum()) / 14.0
    for delta in deltas_pre[14:]:
        d = float(delta)
        up_prev = (up_prev * 13.0 + (d if d > 0.0 else 0.0)) / 14.0
        down_prev = (down_prev * 13.0 + (-d if d < 0.0 else 0.0)) / 14.0

    tail19 = tuple(float(x) for x in closes[-20:-1])
    sum19 = sum(tail19)

    prev20 = closes[-25:-5]
    prev_sma20 = max(float(np.mean(prev20)), 1.0)
    prev_width_pct = (4.0 * float(np.std(prev20))) / prev_sma20

    tr_pre = np.maximum(
        h_pre[1:] - l_pre[1:],
        np.maximum(np.abs(h_pre[1:] - c_pre[:-1]), np.abs(l_pre[1:] - c_pre[:-1]))
    )
    atr_prev = calc_ema(tr_pre, 14)
    tr_tail49 = tr_pre[-49:]
    tr_tail49_sum = float(sum(tr_tail49))
    tr_tail_count = len(tr_tail49)

    def _ema_prefix(series_pre, period):
        p_eff = max(1, min(period, n_total))
        alpha = 2.0 / (p_eff + 1.0)
        ema_val = float(series_pre[0])
        one_m = 1.0 - alpha
        for idx in range(1, len(series_pre)):
            ema_val = alpha * float(series_pre[idx]) + one_m * ema_val
        return ema_val, alpha

    ema9_prev, alpha9 = _ema_prefix(c_pre, 9)
    ema21_prev, alpha21 = _ema_prefix(c_pre, 21)
    ema50_prev, alpha50 = _ema_prefix(c_pre, 50)
    ema200_prev, alpha200 = _ema_prefix(c_pre, 200)
    ema12_prev, alpha12 = _ema_prefix(c_pre, 12)
    ema26_prev, alpha26 = _ema_prefix(c_pre, 26)
    alpha_atr = 2.0 / (min(14, len(tr_pre) + 1) + 1.0)

    up_move_pre = h_pre[1:] - h_pre[:-1]
    down_move_pre = l_pre[:-1] - l_pre[1:]
    plus_dm_pre = np.array([u if (u > d and u > 0) else 0.0 for u, d in zip(up_move_pre, down_move_pre)], dtype=np.float64)
    minus_dm_pre = np.array([d if (d > u and d > 0) else 0.0 for u, d in zip(up_move_pre, down_move_pre)], dtype=np.float64)
    plus_dm_ema_prev = calc_ema(plus_dm_pre, 14)
    minus_dm_ema_prev = calc_ema(minus_dm_pre, 14)
    alpha_dm = 2.0 / (min(14, len(plus_dm_pre) + 1) + 1.0)

    if volumes is not None and len(volumes) >= 20 and float(np.sum(volumes[-20:])) > 0:
        vol_tail19 = volumes[-20:-1]
        typ_pre19 = (highs[-20:-1] + lows[-20:-1] + closes[-20:-1]) / 3.0
        vwap_num19 = float(np.sum(typ_pre19 * vol_tail19))
        vwap_den19 = float(np.sum(vol_tail19))
        vol_last = float(volumes[-1])
        has_vol = (vwap_den19 + vol_last) > 0
    else:
        has_vol = False
        vwap_num19 = 0.0
        vwap_den19 = 0.0
        vol_last = 0.0

    return {
        "orig_h_last": float(highs[-1]),
        "orig_l_last": float(lows[-1]),
        "orig_c_last": float(closes[-1]),
        "h_prev": float(highs[-2]),
        "l_prev": float(lows[-2]),
        "c_prev": float(closes[-2]),
        "up_prev": up_prev,
        "down_prev": down_prev,
        "tail19": tail19,
        "sum19": sum19,
        "prev_width_pct": prev_width_pct,
        "atr_prev": atr_prev,
        "alpha_atr": alpha_atr,
        "tr_tail49_sum": tr_tail49_sum,
        "tr_tail_count": tr_tail_count,
        "ema9_prev": ema9_prev,
        "alpha9": alpha9,
        "ema21_prev": ema21_prev,
        "alpha21": alpha21,
        "ema50_prev": ema50_prev,
        "alpha50": alpha50,
        "ema200_prev": ema200_prev,
        "alpha200": alpha200,
        "ema12_prev": ema12_prev,
        "alpha12": alpha12,
        "ema26_prev": ema26_prev,
        "alpha26": alpha26,
        "plus_dm_ema_prev": plus_dm_ema_prev,
        "minus_dm_ema_prev": minus_dm_ema_prev,
        "alpha_dm": alpha_dm,
        "has_vol": has_vol,
        "vwap_num19": vwap_num19,
        "vwap_den19": vwap_den19,
        "vol_last": vol_last,
    }


def compute_indicators_from_prefix(prefix, lnm_price=0.0):
    """
    Actualiza todos los indicadores técnicos en tiempo O(1) cuando cambia el
    precio spot (lnm_price) dentro de la misma ventana de velas.
    """
    c_last = float(lnm_price if lnm_price > 0 else prefix["orig_c_last"])
    if lnm_price > 0:
        h_last = c_last if c_last > prefix["orig_h_last"] else prefix["orig_h_last"]
        l_last = c_last if c_last < prefix["orig_l_last"] else prefix["orig_l_last"]
    else:
        h_last = prefix["orig_h_last"]
        l_last = prefix["orig_l_last"]

    c_prev = prefix["c_prev"]
    d_last = c_last - c_prev
    up = (prefix["up_prev"] * 13.0 + (d_last if d_last > 0.0 else 0.0)) / 14.0
    down = (prefix["down_prev"] * 13.0 + (-d_last if d_last < 0.0 else 0.0)) / 14.0
    rsi = 100.0 - (100.0 / (1.0 + (up / max(down, 1e-9))))

    sma20 = (prefix["sum19"] + c_last) / 20.0
    var20 = (c_last - sma20) ** 2
    for x in prefix["tail19"]:
        diff = x - sma20
        var20 += diff * diff
    std20 = math.sqrt(max(0.0, var20 / 20.0))
    upper_bb, lower_bb = sma20 + 2.0 * std20, sma20 - 2.0 * std20
    bb_width_pct = (upper_bb - lower_bb) / max(sma20, 1.0)
    bb_pos = (c_last - lower_bb) / max((upper_bb - lower_bb), 1e-5)
    bb_expansion_rate = bb_width_pct / max(prefix["prev_width_pct"], 1e-6)

    tr_last = max(h_last - l_last, abs(h_last - c_prev), abs(l_last - c_prev))
    atr = prefix["alpha_atr"] * tr_last + (1.0 - prefix["alpha_atr"]) * prefix["atr_prev"]
    atr_sma50 = (prefix["tr_tail49_sum"] + tr_last) / float(prefix["tr_tail_count"] + 1)
    atr_rel_ratio = atr / max(atr_sma50, 1e-6)

    ema9 = prefix["alpha9"] * c_last + (1.0 - prefix["alpha9"]) * prefix["ema9_prev"]
    ema21 = prefix["alpha21"] * c_last + (1.0 - prefix["alpha21"]) * prefix["ema21_prev"]
    ema50 = prefix["alpha50"] * c_last + (1.0 - prefix["alpha50"]) * prefix["ema50_prev"]
    ema200 = prefix["alpha200"] * c_last + (1.0 - prefix["alpha200"]) * prefix["ema200_prev"]

    ema12 = prefix["alpha12"] * c_last + (1.0 - prefix["alpha12"]) * prefix["ema12_prev"]
    ema26 = prefix["alpha26"] * c_last + (1.0 - prefix["alpha26"]) * prefix["ema26_prev"]
    macd_line = ema12 - ema26
    macd_norm = macd_line / max(atr, 1.0)

    u_last = h_last - prefix["h_prev"]
    d_last_dm = prefix["l_prev"] - l_last
    pdm_last = u_last if (u_last > d_last_dm and u_last > 0.0) else 0.0
    mdm_last = d_last_dm if (d_last_dm > u_last and d_last_dm > 0.0) else 0.0
    alpha_dm = prefix["alpha_dm"]
    plus_dm_ema = alpha_dm * pdm_last + (1.0 - alpha_dm) * prefix["plus_dm_ema_prev"]
    minus_dm_ema = alpha_dm * mdm_last + (1.0 - alpha_dm) * prefix["minus_dm_ema_prev"]
    atr_safe = max(atr, 1e-6)
    plus_di = 100.0 * plus_dm_ema / atr_safe
    minus_di = 100.0 * minus_dm_ema / atr_safe
    adx = 100.0 * abs(plus_di - minus_di) / max(plus_di + minus_di, 1e-6)

    ref_price = c_last
    if prefix.get("has_vol", False):
        typ_last = (h_last + l_last + c_last) / 3.0
        vwap = (prefix["vwap_num19"] + typ_last * prefix["vol_last"]) / max(prefix["vwap_den19"] + prefix["vol_last"], 1e-9)
    else:
        vwap = float(sma20)
    price_vs_vwap_pct = (ref_price - vwap) / max(vwap, 1.0)

    atr_pct = atr / max(ref_price, 1.0)
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

    ind_base = {
        "price": ref_price,
        "high": h_last,
        "low": l_last,
        "open": c_prev,
        "rsi": float(rsi),
        "bb_pos": float(bb_pos),
        "bb_width_pct": float(bb_width_pct),
        "bb_expansion_rate": float(bb_expansion_rate),
        "atr": float(atr),
        "atr_pct": float(atr_pct),
        "atr_rel_ratio": float(atr_rel_ratio),
        "ema9": float(ema9),
        "ema21": float(ema21),
        "ema50": float(ema50),
        "ema200": float(ema200),
        "ema_state": ema_state,
        "ema_score": float(ema_score),
        "ema_macro_spread_pct": float(ema_macro_spread_pct),
        "price_vs_ema200_pct": float(price_vs_ema200_pct),
        "macd_norm": float(macd_norm),
        "adx": float(adx),
        "vwap": float(vwap),
        "price_vs_vwap_pct": float(price_vs_vwap_pct),
    }
    reg_info = MarketRegimeClassifier.classify(ind_base)
    ind_base["regime"] = reg_info["canonical_regime"]
    ind_base["market_regime"] = reg_info["market_regime"]
    ind_base["volatility_state"] = reg_info["volatility_state"]
    return ind_base


# =====================================================================
# 6. CEREBRO AUTÓNOMO Q-LEARNING CON MÉTRICAS POR ESTILO Y MODO EXPLORER
# =====================================================================
class AutonomousGoalBrain:
    REWARD_SCHEMA_VERSION = 2

    def __init__(self, filename=None, explorer_mode=None, thresholds_manager=None, learning_enabled=None):
        self.filename = filename or Config.BRAIN_FILE
        self.explorer_mode = Config.EXPLORER_MODE if explorer_mode is None else bool(explorer_mode)
        self.learning_enabled = True if learning_enabled is None else bool(learning_enabled)
        self.thresholds = thresholds_manager or get_thresholds_manager()
        self.reward_schema_version = self.REWARD_SCHEMA_VERSION
        self.alpha = self.thresholds.get_float("exploration", "alpha_learning_rate", 0.18)
        self.alpha_intermediate = Config.ALPHA_INTERMEDIATE
        self.alpha_macro = 0.18
        self.gamma = self.thresholds.get_float("exploration", "gamma_discount", 0.92)
        self.epsilon = (
            self.thresholds.get_float("exploration", "epsilon_explorer_initial", 0.28)
            if self.explorer_mode
            else self.thresholds.get_float("exploration", "epsilon_initial", 0.06)
        )
        self.epsilon_min = (
            self.thresholds.get_float("exploration", "epsilon_explorer_min", 0.08)
            if self.explorer_mode
            else self.thresholds.get_float("exploration", "epsilon_min", 0.015)
        )
        self.epsilon_decay = self.thresholds.get_float("exploration", "epsilon_decay", 0.992)
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

        # Idempotencia de transiciones (S, A, R, S') y trazabilidad auditable
        self.processed_closed_trade_ids = deque(maxlen=2500)
        self._processed_closed_trade_ids_set = set()
        self.duplicate_transitions_blocked = 0
        self.transition_audit_log = deque(maxlen=500)
        self._last_td_error = 0.0
        self._last_delta_q = 0.0
        self._last_decision_exploratory = False

        # Motor de supervisión de calidad del aprendizaje en tiempo real
        self.supervisor = LearningQualitySupervisor(
            thresholds_manager=self.thresholds,
            snapshot_callback=self._save_divergence_snapshot,
        )

        self.q_table_1h = {}
        self.q_table_4h = {}

        self.style_metrics = {
            "TREND":     {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
            "REVERSION": {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
            "SNIPER":    {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
            "DEF_SCALP": {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
        }

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
                "trail_trig_atr": 0.75, "trail_slack_atr": 0.55, "exit_mode": "LOCK", "harvest_atr": 1.05, "exhaust_sens": "HIGH",
            },
            3: {
                "side": "buy", "style": "SNIPER", "sl_atr": 1.2, "rr": 3.2, "lev_f": 1.00, "name": "LONG_SNIPER",
                "trail_trig_atr": 0.88, "trail_slack_atr": 0.68, "exit_mode": "HYBRID", "harvest_atr": 1.15, "exhaust_sens": "MED",
            },
            4: {
                "side": "buy", "style": "DEF_SCALP", "sl_atr": 1.4, "rr": 2.0, "lev_f": 0.42, "name": "LONG_DEF_SCALP",
                "trail_trig_atr": 0.80, "trail_slack_atr": 0.42, "exit_mode": "LOCK", "harvest_atr": 0.95, "exhaust_sens": "MAX",
            },
            5: {
                "side": "sell", "style": "TREND", "sl_atr": 2.0, "rr": 4.2, "lev_f": 0.65, "name": "SHORT_TREND",
                "trail_trig_atr": 1.15, "trail_slack_atr": 0.95, "exit_mode": "RUNNER", "harvest_atr": 1.85, "exhaust_sens": "LOW",
            },
            6: {
                "side": "sell", "style": "REVERSION", "sl_atr": 1.6, "rr": 2.4, "lev_f": 0.75, "name": "SHORT_REVERSION",
                "trail_trig_atr": 0.75, "trail_slack_atr": 0.55, "exit_mode": "LOCK", "harvest_atr": 1.05, "exhaust_sens": "HIGH",
            },
            7: {
                "side": "sell", "style": "SNIPER", "sl_atr": 1.2, "rr": 3.2, "lev_f": 1.00, "name": "SHORT_SNIPER",
                "trail_trig_atr": 0.88, "trail_slack_atr": 0.68, "exit_mode": "HYBRID", "harvest_atr": 1.15, "exhaust_sens": "MED",
            },
            8: {
                "side": "sell", "style": "DEF_SCALP", "sl_atr": 1.4, "rr": 2.0, "lev_f": 0.42, "name": "SHORT_DEF_SCALP",
                "trail_trig_atr": 0.80, "trail_slack_atr": 0.42, "exit_mode": "LOCK", "harvest_atr": 0.95, "exhaust_sens": "MAX",
            },
        }

        self.store = AtomicBrainStore(self.filename)
        self.dirty = False
        self.last_save_time = 0.0
        self._last_disk_mtime = 0.0
        self.load_brain()

    @staticmethod
    def _append_transition_audit_jsonl(entry: dict):
        try:
            LearningQualitySupervisor.append_transition_audit_jsonl(entry, env_mode=Config.ENV_MODE)
        except Exception:
            pass

    @staticmethod
    def _init_q_vector(state_key):
        q = [0.015] * Config.NUM_ACTIONS
        q[0] = 0.0
        parts = str(state_key).split("|")
        regime = parts[0] if len(parts) > 0 else "RANGE_NORMAL"
        osc = parts[1] if len(parts) > 1 else "MID"
        macro = parts[3] if len(parts) > 3 else "CHOP"
        ema_ctx = parts[4] if len(parts) > 4 else "EMA_TRANS"

        bull_sig = (regime == "TREND_BULL") or (macro == "BULL") or (ema_ctx == "FULL_BULL")
        bear_sig = (regime == "TREND_BEAR") or (macro == "BEAR") or (ema_ctx == "FULL_BEAR")
        dip_sig = (osc == "DIP") or (ema_ctx == "BULL_DIP")
        peak_sig = (osc == "PEAK") or (ema_ctx == "BEAR_RALLY")

        if bull_sig and not bear_sig:
            q[1], q[3], q[2] = 0.065, 0.060, 0.045
        elif bear_sig and not bull_sig:
            q[5], q[7], q[6] = 0.065, 0.060, 0.045
        elif bull_sig and bear_sig:
            # Conflicto mixto alcista/bajista -> empate neutral simétrico (HOLD en top-1)
            q[2], q[4], q[6], q[8] = 0.045, 0.045, 0.045, 0.045
        elif dip_sig and not peak_sig:
            q[2], q[3], q[4] = 0.065, 0.055, 0.050
        elif peak_sig and not dip_sig:
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
        q[0] = 0.0
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
            res = [0.015] * Config.NUM_ACTIONS
            res[0] = 0.0
            return res
        if len(vec) == Config.NUM_ACTIONS:
            res = [float(x) for x in vec]
            res[0] = 0.0
            return res
        new_vec = [0.015] * Config.NUM_ACTIONS
        for i in range(min(len(vec), Config.NUM_ACTIONS)):
            new_vec[i] = float(vec[i])
        new_vec[0] = 0.0
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
        data = None
        if hasattr(self.store, "load"):
            try:
                data = self.store.load()
            except Exception as e:
                print(f"[AVISO STORE LOAD] {e}")

        if not isinstance(data, dict):
            return

        try:
            raw_q = data.get("q_table", {})
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
            loaded_schema_ver = int(data.get("reward_schema_version", 1))
            has_legacy_meta = ("total_trained" in data) or ("memory" in data)
            is_legacy_v1 = (loaded_schema_ver < self.REWARD_SCHEMA_VERSION) and has_legacy_meta

            if is_legacy_v1 and os.path.basename(self.filename) in ("q_brain_advanced_lnm.json", "q_brain_testnet_lnm.json"):
                v1_bak = f"{self.filename}.pre_v2_schema.bak"
                if os.path.exists(self.filename) and not os.path.exists(v1_bak):
                    try:
                        shutil.copy2(self.filename, v1_bak)
                    except Exception:
                        pass

            for k in self.q_table:
                if is_legacy_v1:
                    # Reescalar valores Q heredados del esquema v1 (inflados por 1/(1-gamma) y bonos)
                    self.q_table[k] = [
                        0.0 if idx == 0 else round(float(val) * (0.35 if float(val) > 0.0 else 0.50), 4)
                        for idx, val in enumerate(self.q_table[k])
                    ]
                else:
                    self.q_table[k][0] = 0.0

            self.reward_schema_version = self.REWARD_SCHEMA_VERSION
            self.state_visits = new_visits
            det_clean_seed = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not self.explorer_mode)
            if det_clean_seed:
                for _k in list(self.q_table.keys()):
                    self.q_table[_k] = self._init_q_vector(_k)
                    self.state_visits[_k] = 1
                self.q_table_1h = {}
                self.q_table_4h = {}
                for st_k in self.style_metrics:
                    self.style_metrics[st_k] = {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0}
                for tf in ("1h", "4h"):
                    self.cycle_state[tf]["action"] = 0
                self.epsilon = 0.0
            else:
                self.q_table_1h = data.get("q_table_1h", {})
                self.q_table_4h = data.get("q_table_4h", {})

                saved_styles = data.get("style_metrics", {})
                if isinstance(saved_styles, dict):
                    for st_k, st_v in saved_styles.items():
                        if st_k in self.style_metrics and isinstance(st_v, dict):
                            self.style_metrics[st_k].update(st_v)

                saved_cycles = data.get("cycle_state", {})
                for tf in ("1h", "4h"):
                    if tf in saved_cycles and isinstance(saved_cycles[tf], dict):
                        for k in self.cycle_state[tf]:
                            if k in saved_cycles[tf]:
                                self.cycle_state[tf][k] = saved_cycles[tf][k]

                loaded_eps = float(data.get("epsilon", self.epsilon))
                self.epsilon = max(self.epsilon_min, loaded_eps)
            self.total_trained = int(data.get("total_trained", 0))
            self.total_intermediate_updates = int(data.get("total_intermediate_updates", 0))
            self.current_target = int(data.get("current_target", Config.INITIAL_TARGET_SATS))
            self.milestone_level = int(data.get("milestone_level", 1))
            self.locked_floor = int(data.get("locked_floor", 0))
            self.peak_equity = int(data.get("peak_equity", 0))
            self.realized_net_profit_sats = int(data.get("realized_net_profit_sats", 0))
            self.total_fees_paid_sats = int(data.get("total_fees_paid_sats", 0))
            self.active_trades = data.get("active_trades", {})

            for tid_saved in data.get("processed_closed_trade_ids", []):
                tid_str = str(tid_saved).strip()
                if tid_str and tid_str not in self._processed_closed_trade_ids_set:
                    if len(self.processed_closed_trade_ids) == self.processed_closed_trade_ids.maxlen:
                        ev = self.processed_closed_trade_ids.popleft()
                        self._processed_closed_trade_ids_set.discard(ev)
                    self.processed_closed_trade_ids.append(tid_str)
                    self._processed_closed_trade_ids_set.add(tid_str)

            # Solo cargar memory si proviene del esquema v2 y no está en modo determinista congelado
            if det_clean_seed:
                self.memory.clear()
            elif not is_legacy_v1:
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
            else:
                self.dirty = True
            if os.path.exists(self.filename):
                try:
                    self._last_disk_mtime = os.path.getmtime(self.filename)
                except OSError:
                    pass
            print(
                f"[CEREBRO CARGADO SHA-256 ({Config.ENV_MODE.upper()})] Archivo: {self.filename} | "
                f"Estados: {len(self.q_table)} | Cierres: {self.total_trained} | Online: {self.total_intermediate_updates}"
            )
        except Exception as e:
            print(f"[AVISO PROCESAMIENTO CEREBRO] {e}")

    def sync_from_disk_if_idle(self):
        """Recarga q_table, state_visits y current_target si sync_brains_dual o sniper_cli actualizaron el disco."""
        if len(self.active_trades) > 0 or self.dirty or not os.path.exists(self.filename):
            return False
        try:
            cur_mtime = os.path.getmtime(self.filename)
            if cur_mtime <= (self._last_disk_mtime + 0.05):
                return False
            data = self.store.load()
            if not isinstance(data, dict) or "q_table" not in data:
                return False
            det_clean_seed = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not self.explorer_mode)
            if not det_clean_seed:
                for k, vec in data.get("q_table", {}).items():
                    if isinstance(vec, list) and len(vec) == Config.NUM_ACTIONS:
                        norm_k = self._normalize_state_key(k)
                        self.q_table[norm_k] = [0.0 if i == 0 else round(float(v), 4) for i, v in enumerate(vec)]
                for k, v_cnt in data.get("state_visits", {}).items():
                    norm_k = self._normalize_state_key(k)
                    self.state_visits[norm_k] = max(int(self.state_visits.get(norm_k, 0)), int(v_cnt))
            if "current_target" in data:
                self.current_target = int(data["current_target"])
            self._last_disk_mtime = cur_mtime
            return True
        except Exception:
            return False

    def save_brain(self, force=False):
        now = time.time()
        if not force and not self.dirty:
            return False
        if not force and (now - self.last_save_time) < Config.BRAIN_SAVE_INTERVAL:
            return False
        if not self.q_table:
            return False
        try:
            payload = {
                "env": Config.ENV_MODE,
                "reward_schema_version": self.REWARD_SCHEMA_VERSION,
                "epsilon": round(self.epsilon, 4),
                "total_trained": self.total_trained,
                "total_intermediate_updates": self.total_intermediate_updates,
                "current_target": self.current_target,
                "milestone_level": self.milestone_level,
                "locked_floor": self.locked_floor,
                "peak_equity": self.peak_equity,
                "realized_net_profit_sats": self.realized_net_profit_sats,
                "total_fees_paid_sats": self.total_fees_paid_sats,
                "style_metrics": self.style_metrics,
                "q_table": self.q_table,
                "q_table_1h": self.q_table_1h,
                "q_table_4h": self.q_table_4h,
                "cycle_state": self.cycle_state,
                "state_visits": self.state_visits,
                "active_trades": self.active_trades,
                "processed_closed_trade_ids": list(self.processed_closed_trade_ids)[-500:],
                "memory": list(self.memory)[-1600:],
            }
            if os.path.exists(self.filename):
                try:
                    disk_d = self.store.load()
                    if isinstance(disk_d, dict) and "synced_test_visits" in disk_d:
                        payload["synced_test_visits"] = disk_d["synced_test_visits"]
                except Exception:
                    pass
            self.store.save(payload)
            self.dirty = False
            self.last_save_time = now
            if os.path.exists(self.filename):
                try:
                    self._last_disk_mtime = os.path.getmtime(self.filename)
                except OSError:
                    pass
            return True
        except Exception as e:
            print(f"[ERROR I/O] Fallo al guardar cerebro ({self.filename}): {e}")
            return False

    def _save_divergence_snapshot(self, reason="CRITICAL_TD_DIVERGENCE"):
        """Guarda un snapshot de emergencia del cerebro antes de suspender aprendizaje online."""
        if not self.q_table:
            return
        try:
            snap_name = f"{self.filename}.snapshot_guard.json"
            snap_store = AtomicBrainStore(snap_name)
            payload = {
                "snapshot_reason": str(reason),
                "timestamp": int(time.time()),
                "env": Config.ENV_MODE,
                "epsilon": round(self.epsilon, 4),
                "total_trained": self.total_trained,
                "realized_net_profit_sats": self.realized_net_profit_sats,
                "total_fees_paid_sats": self.total_fees_paid_sats,
                "q_table": self.q_table,
                "state_visits": self.state_visits,
            }
            snap_store.save(payload)
        except Exception as e:
            print(f"[AVISO SNAPSHOT GUARD] {e}")

    def check_and_advance_goal(self, available_balance):
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

    def record_trade_in_cycles(self, net_pl_sats, action=0, fee_sats=0):
        for tf in ("1h", "4h"):
            self.cycle_state[tf]["trades"] = int(self.cycle_state[tf].get("trades", 0)) + 1
            if net_pl_sats > 0:
                self.cycle_state[tf]["wins"] = int(self.cycle_state[tf].get("wins", 0)) + 1
            self.cycle_state[tf]["pnl"] = int(self.cycle_state[tf].get("pnl", 0)) + int(net_pl_sats)

        if int(action) in self.strategies:
            st_name = self.strategies[int(action)]["style"]
            if st_name in self.style_metrics:
                m = self.style_metrics[st_name]
                m["trades"] = int(m.get("trades", 0)) + 1
                if net_pl_sats > 0:
                    m["wins"] = int(m.get("wins", 0)) + 1
                m["net_pnl_sats"] = int(m.get("net_pnl_sats", 0)) + int(net_pl_sats)
                m["fees_sats"] = int(m.get("fees_sats", 0)) + int(fee_sats)
        self.dirty = True

    def get_parallel_modifiers(self, ind_5m=None, ind_15m=None, ind_1h=None, ind_4h=None):
        det_clean_seed = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not self.explorer_mode)
        a_1h = 0 if det_clean_seed else (int(self.cycle_state["1h"].get("action", 0)) % Config.NUM_MACRO_ACTIONS)
        a_4h = 0 if det_clean_seed else (int(self.cycle_state["4h"].get("action", 0)) % Config.NUM_MACRO_ACTIONS)
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
        effective_delta = realized_pnl if trades_cnt > 0 else eq_delta
        cycle_roi_pct = effective_delta / start_eq
        btc_move_pct = (current_price - start_price) / start_price
        winrate_pct = (wins_cnt / trades_cnt * 100.0) if trades_cnt > 0 else 0.0

        reward_scale = max(1.0, self.thresholds.get_float("reward", "reward_scale_sats", 100.0))
        # Recompensa macro basada estrictamente en satoshis netos consolidados del ciclo (M3)
        # Sin bonos de direccion de BTC (align_bonus) que sesgan la seleccion micro
        net_cycle_sats = float(realized_pnl) if trades_cnt > 0 else float(eq_delta)
        macro_reward = float(np.clip(net_cycle_sats / (reward_scale * 3.0), -0.50, 0.50))
        next_macro_state = self.discretize_macro_state(ind_tf, cycle_roi_pct)

        if prev_state not in q_macro or len(q_macro[prev_state]) != Config.NUM_MACRO_ACTIONS:
            q_macro[prev_state] = self._init_macro_q_vector(prev_state)
        if next_macro_state not in q_macro or len(q_macro[next_macro_state]) != Config.NUM_MACRO_ACTIONS:
            q_macro[next_macro_state] = self._init_macro_q_vector(next_macro_state)

        cur_q = q_macro[prev_state][prev_action]
        max_next_q = max(q_macro[next_macro_state])
        det_clean_seed = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not self.explorer_mode)
        if det_clean_seed and not self.learning_enabled:
            new_macro_action = 0
        else:
            q_macro[prev_state][prev_action] = round(
                cur_q + self.alpha_macro * (macro_reward + self.gamma * max_next_q - cur_q), 4
            )
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
        if not (det_clean_seed and not self.learning_enabled):
            self.dirty = True
            self.save_brain(force=True)

        mods = self.get_parallel_modifiers()
        print("--------------------------------------------------------------------")
        print(f"[CIERRE VELA {tf.upper()} & CONSOLIDACIÓN MACRO ({Config.ENV_MODE.upper()})] Estado: {next_macro_state}")
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

    def get_conservative_q_values(self, state):
        """
        Penalización Conservadora en Inferencia (CQL Shrinkage) sin mutar la tabla Q:
        Q_CQL(s, a) = Q(s, a) - (kappa * max(0, B_Q) + beta * sigma_Q) / (S_Q * sqrt(N(s, a) + 1))
        con kappa = 0.75 y beta = 0.25.
        """
        norm_s = self._normalize_state_key(state)
        raw_q = self.get_state_q_values(norm_s)
        visits = max(0, int(self.state_visits.get(norm_s, 0)))
        q_scale = max(1.0, self.thresholds.get_float("q_calibration", "q_to_sats_scale", 100.0))
        b_q, sigma_q = (0.0, 0.0)
        if hasattr(self, "supervisor") and self.supervisor is not None and hasattr(self.supervisor, "get_q_bias_stats"):
            b_q, sigma_q = self.supervisor.get_q_bias_stats()
        kappa = 0.75
        beta = 0.25
        penalty_sats = (kappa * max(0.0, float(b_q)) + beta * max(0.0, float(sigma_q))) / math.sqrt(float(visits) + 1.0)
        penalty_q = penalty_sats / q_scale
        q_min = self.thresholds.get_float("q_calibration", "q_clip_min", -2.0)
        q_max = self.thresholds.get_float("q_calibration", "q_clip_max", 2.0)
        c_vec = [0.0] * Config.NUM_ACTIONS
        for idx in range(1, min(len(raw_q), Config.NUM_ACTIONS)):
            c_vec[idx] = round(float(np.clip(float(raw_q[idx]) - penalty_q, q_min, q_max)), 4)
        return c_vec

    def get_conservative_q(self, state, action):
        act_idx = int(action) % Config.NUM_ACTIONS
        if act_idx == 0:
            return 0.0
        c_vec = self.get_conservative_q_values(state)
        return float(c_vec[act_idx])

    def choose_action(self, state, ind_5m, ind_15m, ind_1h, ind_4h, recovery_mode=False, current_dd_pct=0.0):
        norm_s = self._normalize_state_key(state)
        det_policy = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not self.explorer_mode)
        q_vals = self.get_state_q_values(norm_s) if det_policy else self.get_conservative_q_values(norm_s)
        visits = self.state_visits.get(norm_s, 0)

        self.epsilon_decay = self.thresholds.get_float("exploration", "epsilon_decay", self.epsilon_decay)
        if self.explorer_mode:
            self.epsilon_min = self.thresholds.get_float("exploration", "epsilon_explorer_min", self.epsilon_min)
        else:
            self.epsilon_min = self.thresholds.get_float("exploration", "epsilon_min", self.epsilon_min)

        if recovery_mode and current_dd_pct >= 0.12 and not self.explorer_mode:
            allowed_actions = [0, 2, 3, 4, 6, 7, 8]
        else:
            allowed_actions = [0, 1, 2, 3, 4, 5, 6, 7, 8]

        # F5: Supresión estricta de aperturas aleatorias en Mainnet (mainnet_epsilon=0.0 por defecto)
        self._last_decision_exploratory = False
        if self.explorer_mode:
            self.alpha = 0.24
            self.epsilon = max(self.epsilon_min, round(self.epsilon * self.epsilon_decay, 4))
            if random.uniform(0, 1) < self.epsilon:
                active_candidates = [a for a in allowed_actions if a != 0]
                chosen_act = int(random.choice(active_candidates))
                self._last_decision_exploratory = True
                self.supervisor.record_policy_decision(chosen_act)
                return chosen_act
        elif not det_policy:
            mainnet_eps = self.thresholds.get_float("exploration", "mainnet_epsilon", 0.0)
            if recovery_mode:
                self.alpha = self.thresholds.get_float("exploration", "alpha_recovery_rate", 0.28)
                effective_eps = min(mainnet_eps, 0.02)
            else:
                self.alpha = self.thresholds.get_float("exploration", "alpha_learning_rate", 0.18)
                effective_eps = mainnet_eps

            if effective_eps > 0.0 and random.uniform(0, 1) < effective_eps:
                if visits < 25 and not recovery_mode:
                    non_rev = [a for a in allowed_actions if a not in (0, 2, 6)]
                    chosen_act = int(random.choice(non_rev if non_rev else allowed_actions))
                    self._last_decision_exploratory = True
                    self.supervisor.record_policy_decision(chosen_act)
                    return chosen_act
                self.supervisor.record_policy_decision(0)
                return 0

        collapse_dom_name = None
        sup = getattr(self, "supervisor", None)
        if sup is not None:
            try:
                adj = sup.get_autonomous_adjustments()
                if isinstance(adj, dict) and adj.get("policy_collapse_detected", False):
                    collapse_dom_name = getattr(sup, "last_dominant_action", None)
                    if not collapse_dom_name and hasattr(sup, "get_policy_entropy_metrics"):
                        ent_m = sup.get_policy_entropy_metrics()
                        if isinstance(ent_m, dict):
                            collapse_dom_name = ent_m.get("dominant_action")
            except Exception:
                collapse_dom_name = None

        if det_policy:
            # Selección determinista simétrica P1_NO_CASCADE_SYM:
            # Cero lcb_penalty (evita parálisis 100% HOLD con visits=0) y empate inter-lado -> HOLD (0).
            eff_q = {}
            for a in allowed_actions:
                if a == 0:
                    continue
                strat = self.strategies[a]
                qv = float(q_vals[a])
                if collapse_dom_name and strat["name"] == collapse_dom_name:
                    qv -= 0.06
                eff_q[a] = qv
            max_q = max(eff_q.values()) if eff_q else 0.0
            if max_q <= max(0.0, float(q_vals[0])):
                chosen_act = 0
            else:
                top_acts = [a for a in allowed_actions if a != 0 and abs(eff_q[a] - max_q) <= 1e-12]
                if any(1 <= a <= 4 for a in top_acts) and any(5 <= a <= 8 for a in top_acts):
                    chosen_act = 0
                else:
                    chosen_act = int(top_acts[0])
            self.supervisor.record_policy_decision(chosen_act)
            return chosen_act

        mods = self.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)
        lcb_penalty = (0.12 / float(np.sqrt(visits + 1.0))) if (visits < 80 and not self.explorer_mode) else 0.0

        def biased_q(a):
            if a == 0:
                return float(q_vals[0])
            strat = self.strategies[a]
            q_val = float(q_vals[a]) - lcb_penalty
            if mods["name_1h"] == "BULL_EXPANSION" and a in (5, 6, 7, 8):
                q_val = (q_val * 0.65) if q_val >= 0 else (q_val - 0.04)
            elif mods["name_1h"] == "BEAR_EXPANSION" and a in (1, 2, 3, 4):
                q_val = (q_val * 0.65) if q_val >= 0 else (q_val - 0.04)
            if collapse_dom_name and strat["name"] == collapse_dom_name:
                q_val -= 0.06

            b = mods["long_bias"] if strat["side"] == "buy" else mods["short_bias"]
            if strat["style"] in ("REVERSION", "DEF_SCALP"):
                b += mods["rev_bias"]
            return q_val + b

        chosen_act = int(max(allowed_actions, key=biased_q))
        self.supervisor.record_policy_decision(chosen_act)
        return chosen_act

    def _update_q(
        self, s, a, r, ns, custom_alpha=None, force_negative_on_loss=False,
        check_guard=False, terminal=True, shadow_virtual=False, freeze_q_table=False
    ):
        if freeze_q_table or not getattr(self, "learning_enabled", True):
            self._last_td_error = 0.0
            self._last_delta_q = 0.0
            return 0.0
        norm_s = self._normalize_state_key(s)
        norm_ns = self._normalize_state_key(ns)
        if norm_s not in self.q_table or len(self.q_table[norm_s]) != Config.NUM_ACTIONS:
            self.q_table[norm_s] = self._init_q_vector(norm_s)
        if norm_ns not in self.q_table or len(self.q_table[norm_ns]) != Config.NUM_ACTIONS:
            self.q_table[norm_ns] = self._init_q_vector(norm_ns)
        a = int(a) % Config.NUM_ACTIONS
        lr = custom_alpha if custom_alpha is not None else self.alpha
        self.gamma = self.thresholds.get_float("exploration", "gamma_discount", self.gamma)
        eff_gamma = 0.0 if terminal else self.gamma

        current_q = float(self.q_table[norm_s][a])
        max_future_q = max(self.q_table[norm_ns])
        raw_td = float(r) + (eff_gamma * max_future_q) - current_q
        signed_td = raw_td

        self._last_td_error = signed_td

        if not shadow_virtual:
            if check_guard:
                allow_update, _ = self.supervisor.evaluate_td_error_pre_update(signed_td)
                if not allow_update:
                    self._last_delta_q = 0.0
                    return 0.0
            elif not self.supervisor.can_update_q_table():
                self._last_delta_q = 0.0
                return 0.0

        q_min = self.thresholds.get_float("q_calibration", "q_clip_min", -2.0)
        q_max = self.thresholds.get_float("q_calibration", "q_clip_max", 2.0)
        new_q = float(np.clip(current_q + lr * signed_td, q_min, q_max))
        if force_negative_on_loss and float(r) < 0.0 and current_q >= 0.0 and new_q >= current_q:
            new_q = max(q_min, current_q - max(0.0005, abs(lr * float(r) * 0.25)))
        self._last_delta_q = abs(new_q - current_q)
        self.q_table[norm_s][a] = round(new_q, 4)
        self.q_table[norm_s][0] = 0.0
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
        if len(self.memory) < 6 or not self.supervisor.can_update_q_table():
            return
        mem_list = list(self.memory)
        weights = np.array([max(float(m[0]), 0.01) for m in mem_list], dtype=np.float64)
        weights /= weights.sum()
        sz = min(len(mem_list), sample_count)
        chosen_indices = np.random.choice(len(mem_list), size=sz, p=weights, replace=False)
        for idx in chosen_indices:
            _, s, a, r, ns = mem_list[idx]
            dq = self._update_q(
                s, a, r, ns,
                custom_alpha=custom_alpha,
                force_negative_on_loss=(float(r) < 0.0),
                check_guard=False,
                terminal=True,
            )
            reg_change = 1.0 if str(s).split("|")[0] != str(ns).split("|")[0] else 0.0
            new_prio = round(abs(float(r)) + (0.30 * dq) + (0.20 * reg_change) + 0.02, 4)
            self.memory[idx] = (new_prio, s, a, r, ns)

    def remember_intermediate(
        self, origin_state, action, current_state, side,
        delta_atr, guardian_mode, sl_locked_confirmed, eval_count, edge_score=0.0
    ):
        """
        Desacople de PnL flotante (Sección 4 Directiva Maestra):
        Cuando `reward.decouple_floating_pnl == 1` (activo por defecto), las
        fluctuaciones intermedias no consolidadas NO modifican la Q-table;
        únicamente las transiciones de cierre líquido generan recompensas terminales.
        """
        if self.thresholds.get_int("reward", "decouple_floating_pnl", 1) == 1 or not getattr(self, "learning_enabled", True) or getattr(Config, "DETERMINISTIC_TREND_POLICY", True):
            return 0.0

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
        if float(edge_score) <= -0.45:
            raw_shaping = (-abs(raw_shaping) * 1.8) if raw_shaping < 0 else (raw_shaping * 1.8)

        shaped_partial = float(np.clip(raw_shaping, -Config.MAX_INTERMEDIATE_REWARD_CLIP, Config.MAX_INTERMEDIATE_REWARD_CLIP))
        self._update_q(origin_state, action, shaped_partial, current_state, custom_alpha=self.alpha_intermediate, terminal=False)
        self.total_intermediate_updates += 1
        self.dirty = True
        return round(shaped_partial, 4)

    def remember_and_train(
        self, state, action, reward_sats, margin_used, next_state,
        equity_after=0, available_balance_after=None, real_balance_delta_sats=None,
        hit_tp=False, harvested=False, is_no_trade=False,
        recovery_mode=False, exited_recovery_bonus=False, lev_used=35.0,
        exact_fee_sats=0, pl_includes_fees=False,
        funding_fee_sats=0, slippage_sats=0,
        trade_id=None, mae_sats=0.0, mfe_sats=0.0, duration_ticks=0.0,
        predicted_q_entry=None, freeze_q_table=False, exact_fee_is_explicit=False
    ):
        """
        Función de Recompensa de Acumulación Real de Satoshis (Sección 4):
          R_sats = Delta_Balance_disponible - lambda * (MAE_sats) - mu * (Delta_t_ticks)

        CONVENCIÓN CONTABLE LN MARKETS API v3 (Prevención de Doble Descuento):
          1. En LN Markets API v3, el balance líquido disponible (`/account -> balance`)
             ya deduce comisión de apertura, comisión de cierre y funding periódico
             al liquidar la orden, y `pl` ya refleja el precio real de fill (slippage incluido).
          2. Si el llamador suministra `real_balance_delta_sats` (variación real de
             satoshis líquidos en balance) o `pl_includes_fees=True`, NO se restan
             nuevamente las comisiones taker, el funding ni el slippage.
          3. Solo cuando `real_balance_delta_sats is None` y `pl_includes_fees=False`
             (es decir, `reward_sats` es PnL bruto antes de fricción), se deduce una
             única vez el costo total de fricción (`fee_sats`).
          4. Idempotencia estricta: Si se provee `trade_id`, se garantiza que la tupla
             (S, A, R, S') nunca se procese más de una vez para la misma posición.
        """
        norm_s = self._normalize_state_key(state)
        norm_ns = self._normalize_state_key(next_state)

        is_frozen = bool(freeze_q_table or not getattr(self, "learning_enabled", True))
        if is_no_trade:
            if not is_frozen:
                self.q_table.setdefault(norm_s, self._init_q_vector(norm_s))
                self.q_table[norm_s][0] = 0.0
            return 0.0

        # Control de idempotencia por ID de posición / orden
        if trade_id is not None and str(trade_id).strip() != "":
            tid_key = str(trade_id).strip()
            if tid_key in self._processed_closed_trade_ids_set:
                self.duplicate_transitions_blocked += 1
                print(f"[IDEMPOTENCY-GUARD] Transición duplicada bloqueada para trade_id={tid_key}")
                return 0.0
            if len(self.processed_closed_trade_ids) == self.processed_closed_trade_ids.maxlen:
                evicted = self.processed_closed_trade_ids.popleft()
                self._processed_closed_trade_ids_set.discard(evicted)
            self.processed_closed_trade_ids.append(tid_key)
            self._processed_closed_trade_ids_set.add(tid_key)

        act_idx = int(action) % Config.NUM_ACTIONS
        q_vec_before = self.get_state_q_values(norm_s)
        has_explicit_entry_q = predicted_q_entry is not None
        entry_q = float(predicted_q_entry) if has_explicit_entry_q else float(q_vec_before[act_idx])

        taker_rate = self.thresholds.get_float("fees", "taker_fee_rate", Config.TAKER_FEE)
        taker_fee_sats = (
            max(0, int(exact_fee_sats))
            if (exact_fee_is_explicit or int(exact_fee_sats or 0) > 0)
            else max(2, int(round(float(margin_used) * float(lev_used) * (2.0 * taker_rate))))
        )
        funding_sats = int(round(float(funding_fee_sats or 0)))
        slip_sats = int(round(float(slippage_sats or 0)))
        settled_fee_sats = max(0, taker_fee_sats + funding_sats)

        # Prevención estricta de doble descuento de comisiones y slippage
        if real_balance_delta_sats is not None:
            net_balance_delta = int(round(float(real_balance_delta_sats)))
            fee_sats = settled_fee_sats
        elif pl_includes_fees:
            net_balance_delta = int(round(float(reward_sats)))
            fee_sats = settled_fee_sats
        else:
            fee_sats = max(0, settled_fee_sats + slip_sats)
            net_balance_delta = int(round(float(reward_sats))) - fee_sats

        self.realized_net_profit_sats += net_balance_delta
        self.total_fees_paid_sats += fee_sats
        self.record_trade_in_cycles(net_balance_delta, action=act_idx, fee_sats=fee_sats)

        # Excursiones MFE / MAE y tiempo de capital inmovilizado (delta_t_ticks)
        tracked_exc = (
            self.supervisor.get_open_position_excursion(str(trade_id))
            if trade_id is not None
            else {"mfe_sats": 0.0, "mae_sats": 0.0, "ticks_observed": 0.0}
        )
        mae_val = max(
            0.0,
            float(mae_sats) if float(mae_sats or 0.0) > 0.0 else float(tracked_exc.get("mae_sats", 0.0)),
        )
        mfe_val = max(
            0.0,
            float(mfe_sats)
            if float(mfe_sats or 0.0) > 0.0
            else float(tracked_exc.get("mfe_sats", max(0, net_balance_delta))),
        )
        ticks_val = max(
            0.0,
            float(duration_ticks)
            if float(duration_ticks or 0.0) > 0.0
            else float(tracked_exc.get("ticks_observed", 0.0)),
        )

        lam = self.thresholds.get_float("reward", "mae_penalty_lambda", 0.15)
        mu = self.thresholds.get_float("reward", "holding_time_penalty_mu", 0.05)
        reward_scale = max(1.0, self.thresholds.get_float("reward", "reward_scale_sats", 100.0))
        loss_mult = max(1.0, self.thresholds.get_float("reward", "loss_penalty_multiplier", 1.0))

        # Modelo Conceptual de Recompensa:
        # R_econ_sats = Delta_Balance_disponible - lambda * MAE_sats - mu * Delta_t_ticks
        # Invarianza dimensional al Margen Canónico (M_ref = 1000 sats)
        ref_margin = float(Config.MIN_MARGIN_SATS)
        scale_eta = ref_margin / max(ref_margin, float(margin_used or ref_margin))
        norm_net_delta = float(net_balance_delta) * scale_eta
        norm_mae_val = float(mae_val) * scale_eta
        econ_reward_sats = float(net_balance_delta) - (lam * mae_val) - (mu * ticks_val)
        norm_econ_reward_sats = norm_net_delta - (lam * norm_mae_val) - (mu * ticks_val)

        is_real_loss = net_balance_delta < 0
        if is_real_loss:
            # Toda pérdida neta en balance debe penalizar negativamente el valor Q
            econ_reward_sats = min(econ_reward_sats, float(net_balance_delta))
            norm_econ_reward_sats = min(norm_econ_reward_sats, norm_net_delta)
            shaped = (norm_econ_reward_sats / reward_scale) * loss_mult
        elif net_balance_delta == 0 and norm_econ_reward_sats <= 0.0:
            shaped = (norm_econ_reward_sats / reward_scale) - 0.010
        else:
            shaped = norm_econ_reward_sats / reward_scale

        q_min = self.thresholds.get_float("q_calibration", "q_clip_min", -2.0)
        q_max = self.thresholds.get_float("q_calibration", "q_clip_max", 2.0)
        shaped = float(np.clip(shaped, q_min, q_max))

        alpha_rec = self.thresholds.get_float("exploration", "alpha_recovery_rate", 0.28)
        alpha_std = self.thresholds.get_float("exploration", "alpha_learning_rate", 0.18)
        self.alpha = alpha_rec if recovery_mode else alpha_std

        dq = self._update_q(
            norm_s,
            act_idx,
            shaped,
            norm_ns,
            custom_alpha=self.alpha,
            force_negative_on_loss=is_real_loss,
            check_guard=True,
            terminal=True,
            freeze_q_table=is_frozen,
        )

        if not is_frozen:
            self.state_visits[norm_s] = int(self.state_visits.get(norm_s, 0)) + 1
        sup_trade_meta = self.supervisor.record_closed_trade(
            trade_id=str(trade_id) if trade_id is not None else None,
            predicted_q_entry=(entry_q if has_explicit_entry_q else None),
            net_realized_sats=net_balance_delta,
            total_fees_sats=fee_sats,
            margin_sats=int(margin_used),
            mfe_sats=mfe_val,
            mae_sats=mae_val,
            econ_reward_sats=econ_reward_sats,
        )

        reg_change = 1.0 if norm_s.split("|")[0] != norm_ns.split("|")[0] else 0.0
        priority = round(abs(shaped) + (0.30 * dq) + (0.20 * reg_change) + 0.02, 4)
        if not is_frozen:
            self.memory.append((priority, norm_s, act_idx, round(shaped, 4), norm_ns))

        # Trazabilidad estricta de la transición
        audit_entry = {
            "timestamp": int(time.time()),
            "trade_id": str(trade_id) if trade_id is not None else None,
            "state": norm_s,
            "action": act_idx,
            "predicted_q": round(entry_q, 4),
            "predicted_sats": sup_trade_meta["q_entry_sats"],
            "net_realized_sats": net_balance_delta,
            "econ_reward_sats": round(econ_reward_sats, 2),
            "mae_sats": round(mae_val, 2),
            "mfe_sats": round(mfe_val, 2),
            "duration_ticks": round(ticks_val, 2),
            "fee_sats": fee_sats,
            "shaped_reward": round(shaped, 4),
            "td_error": round(self._last_td_error, 4),
            "delta_q": round(dq, 4),
            "learning_guard_status": self.supervisor.learning_guard_status,
        }
        self.transition_audit_log.append(audit_entry)
        self._append_transition_audit_jsonl(audit_entry)

        if not is_frozen:
            self.total_trained += 1
            if self.total_trained % 400 == 0:
                self._compact_memory()
            self._experience_replay(self.batch_size, custom_alpha=self.alpha * 0.85)
            self.epsilon_decay = self.thresholds.get_float("exploration", "epsilon_decay", self.epsilon_decay)
            if recovery_mode and not self.explorer_mode:
                self.epsilon = max(0.04, round(self.epsilon, 4))
            else:
                self.epsilon = max(self.epsilon_min, round(self.epsilon * self.epsilon_decay, 4))
        self.dirty = True
        self.save_brain(force=False)
        return round(shaped, 4)


# =====================================================================
# 7. CLIENTE HTTP IDEMPOTENTE CON BAJA LATENCIA Y POOL PERSISTENTE (API v3)
# =====================================================================
def _clean_numbers(obj):
    if isinstance(obj, dict):
        return {k: _clean_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_numbers(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


class LNMarketsClient(ResilientLNMClient):
    def __init__(self, key="", secret="", passphrase="", base_url=None):
        super().__init__(key=key, secret=secret, passphrase=passphrase, base_url=base_url or Config.BASE_URL)
        self.key = key
        self.secret = secret
        self.passphrase = passphrase
        self.base_url = base_url or Config.BASE_URL
        self._server_time_offset_ms: int = 0
        self.session = ResilientLNMClient.build_session(f"LNMSniper/7.3-{Config.ENV_MODE}")
        self.telemetry = LatencyTelemetry(window_size=256)
        self.order_guard = InFlightOrderGuard(ttl_sec=20.0)
        self._user_cache = None
        self._user_cache_ts = 0.0
        self._running_cache = None
        self._running_cache_ts = 0.0
        self._last_ticker_price = 0.0

    def _sync_clock_offset_from_error_or_headers(self, err_text: str = "", headers: dict = None) -> bool:
        """Extrae el timestamp del servidor LN Markets ante HTTP 401 (outdated timestamp) y calibra el offset en RAM."""
        from email.utils import parsedate_to_datetime
        local_ms = int(time.time() * 1000)
        if err_text:
            m = re.search(r"server timestamp\s*=\s*(\d{12,14})", str(err_text), re.IGNORECASE)
            if m:
                srv_ms = int(m.group(1))
                self._server_time_offset_ms = srv_ms - local_ms
                ResilientLNMClient._server_time_offset_ms = self._server_time_offset_ms
                return True
        if headers:
            date_hdr = headers.get("Date") or headers.get("date")
            if date_hdr:
                try:
                    dt = parsedate_to_datetime(date_hdr)
                    srv_ms = int(dt.timestamp() * 1000)
                    self._server_time_offset_ms = srv_ms - local_ms
                    ResilientLNMClient._server_time_offset_ms = self._server_time_offset_ms
                    return True
                except Exception:
                    pass
        return False

    def _request(self, method, path, params=None, max_retries=2):
        method_up = method.upper()
        clean_p = _clean_numbers(params) if params else None
        req_timeout = ResilientLNMClient.get_timeout_for(method_up, path)
        eff_max_retries = max(1, int(max_retries))
        clock_synced_retry = False
        attempt = 0

        while attempt < eff_max_retries:
            req_t0 = time.perf_counter()
            acquired = ResilientLNMClient.acquire_slot(timeout=4.0)
            try:
                ts_ms = str(int(time.time() * 1000) + int(getattr(self, "_server_time_offset_ms", 0)))
                data_str, url = "", f"{self.base_url}{path}"
                if clean_p:
                    if method_up in ("GET", "DELETE"):
                        url += f"?{urllib.parse.urlencode(clean_p)}"
                    else:
                        data_str = json.dumps(clean_p, separators=(",", ":"))
                payload = ts_ms + method.lower() + "/v3" + path + data_str
                signature = base64.b64encode(
                    hmac.new(self.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
                ).decode("utf-8")
                headers = {
                    "LNM-ACCESS-KEY": self.key,
                    "LNM-ACCESS-SIGNATURE": signature,
                    "LNM-ACCESS-PASSPHRASE": self.passphrase,
                    "LNM-ACCESS-TIMESTAMP": ts_ms,
                }
                if method_up in ("POST", "PUT") and data_str:
                    headers["Content-Type"] = "application/json"
                resp = self.session.request(
                    method_up,
                    url,
                    headers=headers,
                    data=data_str if method_up in ("POST", "PUT") and data_str else None,
                    timeout=req_timeout,
                )
                elapsed_ms = (time.perf_counter() - req_t0) * 1000.0
                if "/ticker" in path:
                    self.telemetry.record("network_ms", elapsed_ms)
                else:
                    self.telemetry.record("api_ms", elapsed_ms)

                if resp.status_code == 401 and not clock_synced_retry:
                    err_body = str(getattr(resp, "text", "") or "")
                    body_lower = err_body.lower()
                    resp_headers = dict(getattr(resp, "headers", {}) or {})
                    if "outdated timestamp" in body_lower or "timestamp" in body_lower:
                        if self._sync_clock_offset_from_error_or_headers(err_body, resp_headers):
                            clock_synced_retry = True
                            if attempt >= eff_max_retries - 1:
                                eff_max_retries += 1
                            attempt += 1
                            continue

                if resp.status_code in (429, 500, 502, 503, 504) and attempt < eff_max_retries - 1:
                    time.sleep(ResilientLNMClient.compute_backoff(attempt))
                    attempt += 1
                    continue
                if not resp.ok:
                    raise RuntimeError(f"HTTP {resp.status_code} ({path}): {resp.text[:140]}")
                return resp.json() if resp.text else {}
            except Exception as e:
                elapsed_ms = (time.perf_counter() - req_t0) * 1000.0
                self.telemetry.record("api_ms", elapsed_ms)
                err_str = str(e)
                if not clock_synced_retry and "401" in err_str and ("outdated timestamp" in err_str.lower() or "timestamp" in err_str.lower()):
                    if self._sync_clock_offset_from_error_or_headers(err_str, None):
                        clock_synced_retry = True
                        if attempt >= eff_max_retries - 1:
                            eff_max_retries += 1
                        attempt += 1
                        continue
                if attempt >= eff_max_retries - 1:
                    raise RuntimeError(f"Fallo en {path}: {e}")
                time.sleep(ResilientLNMClient.compute_backoff(attempt))
                attempt += 1
            finally:
                if acquired:
                    ResilientLNMClient.release_slot()

    def get_ticker_price(self):
        try:
            t = self._request("GET", "/futures/ticker", max_retries=2)
            p = float(t.get("lastPrice") or t.get("index") or 0.0) if isinstance(t, dict) else 0.0
            if p > 0:
                self._last_ticker_price = p
            return p
        except Exception:
            if self._last_ticker_price > 0:
                return self._last_ticker_price
            raise

    def get_user(self, force_refresh=False):
        now = time.time()
        if not force_refresh and self._user_cache is not None and (now - self._user_cache_ts) < Config.USER_SYNC_TTL_SEC:
            return self._user_cache
        u = self._request("GET", "/account", max_retries=2)
        if isinstance(u, dict):
            self._user_cache = u
            self._user_cache_ts = now
        return u

    def get_running_trades(self, force_refresh=False):
        now = time.time()
        if not force_refresh and self._running_cache is not None and (now - self._running_cache_ts) < 1.1:
            return list(self._running_cache)
        r = self._request("GET", "/futures/isolated/trades/running", max_retries=2)
        rows = r.get("data", []) if isinstance(r, dict) else r
        if not isinstance(rows, list):
            return []
        unique = {}
        for row in rows:
            if isinstance(row, dict) and "id" in row:
                unique[str(row["id"])] = row
        result = list(unique.values())
        self._running_cache = result
        self._running_cache_ts = now
        return list(result)

    def get_closed_trades(self, limit=25):
        r = self._request("GET", "/futures/isolated/trades/closed", max_retries=2)
        rows = r.get("data", []) if isinstance(r, dict) else r
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict) and "id" in row][:limit]

    def open_trade(self, side, margin, leverage, stoploss, takeprofit, client_id=None):
        self._user_cache_ts = 0.0
        self._running_cache_ts = 0.0
        t0 = time.perf_counter()
        cid_clean = str(
            client_id or ResilientLNMClient.generate_client_id(Config.ENV_MODE, order_type="mkt")
        ).strip()[:64]
        res = self._request(
            "POST",
            "/futures/isolated/trade",
            {
                "type": "market",
                "side": side,
                "margin": int(margin),
                "leverage": int(leverage),
                "stoploss": int(round(stoploss)),
                "takeprofit": int(round(takeprofit)),
                "clientId": cid_clean,
            },
            max_retries=1,
        )
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def get_open_orders(self, force_refresh=False):
        r = self._request("GET", "/futures/isolated/trades/open", max_retries=2)
        rows = r.get("data", []) if isinstance(r, dict) else r
        if not isinstance(rows, list):
            return []
        unique = {}
        for row in rows:
            if isinstance(row, dict) and "id" in row:
                unique[str(row["id"])] = row
        return list(unique.values())

    def open_limit_trade(self, side, margin, leverage, price, stoploss, takeprofit, client_id=""):
        self._user_cache_ts = 0.0
        self._running_cache_ts = 0.0
        t0 = time.perf_counter()
        api_side = "buy" if str(side).lower().strip() in ("b", "buy", "long") else "sell"
        lim_px = round(round(float(price) * 2.0) / 2.0, 2)
        payload = {
            "type": "limit",
            "side": api_side,
            "margin": int(margin),
            "leverage": int(leverage),
            "price": lim_px,
            "stoploss": int(round(stoploss)),
            "takeprofit": int(round(takeprofit)),
        }
        cid_clean = str(client_id or "").strip()[:64]
        if cid_clean:
            payload["clientId"] = cid_clean
        res = self._request("POST", "/futures/isolated/trade", payload, max_retries=1)
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def cancel_open_order(self, trade_id):
        self._user_cache_ts = 0.0
        self._running_cache_ts = 0.0
        t0 = time.perf_counter()
        try:
            res = self._request("POST", "/futures/isolated/trade/cancel", {"id": str(trade_id)}, max_retries=2)
        except Exception as err:
            err_l = str(err).lower()
            if "404" in err_l or "405" in err_l:
                res = self._request("DELETE", "/futures/isolated/trade", {"id": str(trade_id)}, max_retries=2)
            else:
                raise
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def update_stoploss(self, trade_id, new_sl):
        t0 = time.perf_counter()
        res = self._request(
            "PUT",
            "/futures/isolated/trade/stoploss",
            {"id": str(trade_id), "value": int(round(new_sl))},
            max_retries=2,
        )
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def update_takeprofit(self, trade_id, new_tp):
        t0 = time.perf_counter()
        res = self._request(
            "PUT",
            "/futures/isolated/trade/takeprofit",
            {"id": str(trade_id), "value": int(round(new_tp))},
            max_retries=2,
        )
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def add_margin(self, trade_id, amount_sats):
        self._user_cache_ts = 0.0
        self._running_cache_ts = 0.0
        t0 = time.perf_counter()
        res = self._request(
            "POST",
            "/futures/isolated/trade/add-margin",
            {"id": str(trade_id), "amount": max(1, int(round(amount_sats)))},
            max_retries=2,
        )
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def cash_in(self, trade_id, amount_sats):
        self._user_cache_ts = 0.0
        self._running_cache_ts = 0.0
        t0 = time.perf_counter()
        res = self._request(
            "POST",
            "/futures/isolated/trade/cash-in",
            {"id": str(trade_id), "amount": max(1, int(round(amount_sats)))},
            max_retries=2,
        )
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res

    def close_trade(self, trade_id):
        self._user_cache_ts = 0.0
        self._running_cache_ts = 0.0
        t0 = time.perf_counter()
        try:
            res = self._request("POST", "/futures/isolated/trade/close", {"id": str(trade_id)}, max_retries=2)
        except Exception:
            res = self._request("DELETE", "/futures/isolated/trade", {"id": str(trade_id)}, max_retries=2)
        self.telemetry.record("confirm_ms", (time.perf_counter() - t0) * 1000.0)
        return res


# =====================================================================
# 8. PATRIMONIO, RIESGO Y GUARDIÁN UNIFICADO CON CONFIRMACIÓN Y EDGE
# =====================================================================
def extract_trade_funding_sats(trade_obj):
    if not isinstance(trade_obj, dict):
        return 0
    for key in ("sumFundingFees", "sum_carry_fees", "funding_fees"):
        val = trade_obj.get(key)
        if val is not None:
            try:
                return int(round(float(val)))
            except (TypeError, ValueError):
                pass
    return 0


def extract_trade_fees_v3(trade_obj):
    if not isinstance(trade_obj, dict):
        return 0, 0, 0, False

    open_val = None
    for key in ("openingFee", "opening_fee"):
        if trade_obj.get(key) is not None:
            open_val = trade_obj.get(key)
            break

    close_val = None
    for key in ("closingFee", "closing_fee"):
        if trade_obj.get(key) is not None:
            close_val = trade_obj.get(key)
            break

    has_explicit_taker = (open_val is not None) or (close_val is not None)
    open_fee = max(0, int(round(float(open_val or 0)))) if open_val is not None else 0
    close_fee = max(0, int(round(float(close_val or 0)))) if close_val is not None else 0
    funding_fee = extract_trade_funding_sats(trade_obj)
    return open_fee, close_fee, funding_fee, has_explicit_taker


def compute_account_equity(available_balance, running_trades):
    locked_margin = sum(int(t.get("margin") or 0) for t in running_trades)
    floating_pl = sum(int(t.get("pl") or 0) for t in running_trades)
    est_exit_fees = sum(
        int(round(int(t.get("margin") or 0) * float(t.get("leverage") or 20.0) * Config.TAKER_FEE))
        for t in running_trades
    )
    carry_fees = sum(extract_trade_funding_sats(t) for t in running_trades)
    true_equity = max(1, int(available_balance) + locked_margin + floating_pl - est_exit_fees - max(0, carry_fees))
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
        "is_exploratory": False,
    }
    if isinstance(existing_meta, dict):
        for k in (
            "state", "action", "style", "entry_time", "opened_at", "last_eval_time",
            "last_pnl_atr", "eval_count", "sl_locked", "sl_pending_target",
            "ai_closed", "close_order_sent", "edge_deg_streak", "last_edge_score",
            "is_exploratory"
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
    resp = client.update_stoploss(tid, target_sl)
    applied_sl = float(resp.get("stoploss") or 0.0) if isinstance(resp, dict) else 0.0
    if applied_sl <= 0.0:
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


def compute_live_edge(
    brain,
    state,
    action,
    ind_5m,
    perception=None,
    margin_sats=1000,
    leverage=15.0,
    entry_price=0.0,
    carry_fees_sats=0,
    simulated_entry=False,
    orig_meta=None,
    now=None,
):
    """
    Unificador simétrico de Edge Neto post-comisión y post-CQL para admisión Pre-Flight
    y monitoreo en vivo del Guardián.
    Costo fijo round-trip: C_RT = 2 * M * L * f_taker (con f_taker = 0.001).
    """
    act_idx = int(action) % Config.NUM_ACTIONS
    is_long = (1 <= act_idx <= 4)
    p_spot = max(1.0, float(ind_5m.get("price", 70000.0)))
    atr = max(1.0, float(ind_5m.get("atr", p_spot * 0.0015)))
    m_sats = max(100, int(margin_sats or 1000))
    lev_f = max(1.0, float(leverage or 15.0))
    notional_sats = float(m_sats) * lev_f
    taker_fee = float(Config.TAKER_FEE)
    carry_sats = max(0, int(round(float(carry_fees_sats or 0))))

    c_rt_sats = (2.0 * notional_sats * taker_fee) + float(carry_sats)
    det_freeze_q = bool(
        getattr(Config, "DETERMINISTIC_TREND_POLICY", True)
        and not getattr(brain, "explorer_mode", False)
        and not getattr(brain, "learning_enabled", True)
    )
    if det_freeze_q:
        raw_q = brain.get_state_q_values(state)
        q_cql = float(raw_q[act_idx]) if act_idx < len(raw_q) else 0.0
    else:
        q_cql = brain.get_conservative_q(state, act_idx)
    q_scale = max(1.0, brain.thresholds.get_float("q_calibration", "q_to_sats_scale", 100.0))

    p_entry = p_spot if (simulated_entry or float(entry_price or 0.0) <= 0.0) else float(entry_price)
    fee_price_offset = p_entry * ((2.0 * taker_fee) + (float(carry_sats) / max(1.0, notional_sats)))

    if simulated_entry:
        pnl_atr_raw = 0.0
        pnl_atr_net = -fee_price_offset / atr
        # q_cql ya es expectativa neta post-comision (normalizada a 1,000 sats):
        # evitar triple descuento en Pre-Flight y penalizar solo compresion extrema (fee > 1.0 ATR)
        excess_fee_atr = max(0.0, (fee_price_offset / atr) - 1.0)
        edge_pnl_term = -0.20 * excess_fee_atr
        fee_drag_q = 0.0
    else:
        signed_move = (p_spot - p_entry) if is_long else (p_entry - p_spot)
        pnl_atr_raw = signed_move / atr
        pnl_atr_net = (signed_move - fee_price_offset) / atr
        edge_pnl_term = 0.20 * pnl_atr_net
        fee_drag_q = 0.0

    now_ts = float(now if now is not None else ((ind_5m or {}).get("_now_ts") or time.time()))
    opened_ts = float((orig_meta or {}).get("entry_time") or (orig_meta or {}).get("opened_at") or now_ts)
    trade_age_sec = max(0.0, now_ts - opened_ts)
    is_exploratory = bool((orig_meta or {}).get("is_exploratory", False))
    has_age_meta = isinstance(orig_meta, dict) and ("entry_time" in orig_meta or "opened_at" in orig_meta or "is_exploratory" in orig_meta)

    if not simulated_entry and q_cql < 0.0 and (is_exploratory or (has_age_meta and trade_age_sec < 180.0 and pnl_atr_raw > -0.45)):
        effective_q_cql = 0.0
    else:
        effective_q_cql = q_cql

    ovr = perception.get("override_side") if isinstance(perception, dict) else None
    st_str = str(state)
    macro_c = (is_long and "|BEAR|" in st_str) or ((not is_long) and "|BULL|" in st_str)

    if (is_long and ovr == "BEAR") or ((not is_long) and ovr == "BULL"):
        ovr_bias = -0.35
    elif simulated_entry and macro_c:
        # En Pre-Flight contratendencia macro no se acredita el bono efímero +0.15 de OVR
        ovr_bias = -0.12
    elif (is_long and ovr == "BULL") or ((not is_long) and ovr == "BEAR"):
        ovr_bias = 0.15
    elif macro_c:
        ovr_bias = -0.12
    else:
        ovr_bias = 0.0

    liq_sig = perception.get("liquidity") if isinstance(perception, dict) else None
    if simulated_entry and isinstance(liq_sig, dict):
        _liq_cfg = LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
        liq_bias = compute_liquidity_bias(
            action=action,
            liquidity_signal=liq_sig,
            cfg=_liq_cfg,
            num_actions=getattr(brain, "num_actions", 9),
        )
    else:
        liq_bias = 0.0

    edge_score = round(effective_q_cql + edge_pnl_term - fee_drag_q + ovr_bias + liq_bias, 4)
    if not simulated_entry and (is_exploratory or has_age_meta) and (-0.35 < pnl_atr_raw < 0.45) and (trade_age_sec < 90.0):
        edge_score = round(max(float(edge_score), -0.65), 4)

    return {
        "edge_score": edge_score,
        "q_cql": round(q_cql, 4),
        "effective_q_cql": round(effective_q_cql, 4),
        "pnl_atr_raw": round(pnl_atr_raw, 4),
        "pnl_atr_net": round(pnl_atr_net, 4),
        "c_rt_sats": round(c_rt_sats, 2),
        "ovr_bias": round(ovr_bias, 4),
        "liq_bias": round(liq_bias, 4),
    }


def execute_strategy_guardian(
    client, brain, trade, ind_5m, ind_15m, trade_meta, system_mode,
    edge_score=0.0, perception=None, current_dd_pct=0.0, is_hedged=False,
    structural_td9_trailing_enabled=None
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
        carry_sats = max(0, extract_trade_funding_sats(trade))
        notional_sats = max(1.0, float(margin) * float(lev))
        fee_offset = entry * ((2.4 * Config.TAKER_FEE) + (carry_sats / notional_sats))
        est_round_fee_sats = max(2, int(round(notional_sats * 2.0 * Config.TAKER_FEE))) + carry_sats

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

        sl_locked_server = (
            (side == "buy" and current_sl > (entry + fee_offset * 0.8)) or
            (side == "sell" and current_sl > 0 and current_sl < (entry - fee_offset * 0.8))
        )
        mode_label = f"{style}_{exit_mode}"
        trade_meta.update({
            "last_guardian_mode": mode_label,
            "sl_locked": bool(sl_locked_server),
            "last_edge_score": round(float(edge_score), 4),
            "is_hedged": bool(is_hedged),
        })

        if edge_score <= Config.EDGE_DEGRADATION_EXIT:
            trade_meta["edge_deg_streak"] = int(trade_meta.get("edge_deg_streak", 0)) + 1
        elif edge_score <= Config.EDGE_DEGRADATION_WARN:
            trade_meta["edge_deg_streak"] = min(int(trade_meta.get("edge_deg_streak", 0)), Config.EDGE_DEGRADATION_PERSISTENCE - 1)
        else:
            trade_meta["edge_deg_streak"] = 0

        deg_streak = int(trade_meta["edge_deg_streak"])

        # Ventana de Amortiguación Post-Apertura (Minimum Hold Grace Period: tau_grace = 45.0s)
        now = float((ind_5m or {}).get("_now_ts") or time.time())
        t = trade
        meta = trade_meta
        opened_at_ts = float(meta.get("entry_time") or meta.get("opened_at") or now)
        trade_age_sec = max(0.0, float(now) - opened_at_ts)
        curr_adverse_sats = max(0.0, float(-pl_sats), float(trade_meta.get("mae_sats", 0.0)))
        mae_over_margin = curr_adverse_sats / float(max(1, margin))
        in_grace_period = (trade_age_sec < 45.0) and (mae_over_margin < 0.045)

        if not is_hedged and not in_grace_period and deg_streak >= Config.EDGE_DEGRADATION_PERSISTENCE and edge_score <= Config.EDGE_DEGRADATION_EXIT:
            # Cálculo exacto de comisión round-trip y beneficio neto estimado
            t_margin = max(1000, int(t.get("margin") or meta.get("margin_sats") or meta.get("margin", 1000)))
            t_lev = max(1.0, float(t.get("leverage") or meta.get("leverage", 15.0)))
            t_notional = t_margin * t_lev
            rt_fee_sats = max(2, int(round(t_notional * 2.0 * Config.TAKER_FEE)))
            gross_pl_sats = int(t.get("pl") or 0)
            est_net_sats = gross_pl_sats - rt_fee_sats
            min_net_harvest = max(15, int(round(0.25 * rt_fee_sats)))
            min_physical_loss = max(30, int(round(0.04 * t_margin)))

            allow_forced_edge_exit = True
            # E2_FE_OFF: Desactivar FORCED_EDGE_EXIT en TREND (actions 1, 5) para preservar el piso sl_floor=0.65%
            if getattr(Config, "FE_OFF_TREND", True) and (action_id in (1, 5) or style == "TREND"):
                allow_forced_edge_exit = False
            # Regla 1: Prohibir FORCED EDGE EXIT en los primeros 90s salvo colapso físico severo (pnl_atr <= -0.85)
            elif trade_age_sec < 90.0 and pnl_atr > -0.85:
                allow_forced_edge_exit = False
            # Regla 2: Zona muerta de comisiones (pnl_atr > -0.45):
            # Si el trade está plano o en ganancia bruta pero NO cubre comisiones round-trip + margen mínimo,
            # PROHIBIR liquidarlo a pérdida neta; dejar actuar al Breakeven/Trailing Lock o al SL/TP estructural.
            elif pnl_atr > -0.45 and est_net_sats < min_net_harvest:
                meta["edge_deg_streak"] = max(0, int(meta.get("edge_deg_streak", 1)) - 1)
                meta["edge_decay_streak"] = meta["edge_deg_streak"]
                deg_streak = int(meta["edge_deg_streak"])
                allow_forced_edge_exit = False
            # Regla 3: En zona negativa (-0.45 >= pnl_atr), exigir pérdida física real confirmada
            elif pnl_atr <= -0.45 and gross_pl_sats > -min_physical_loss:
                allow_forced_edge_exit = False

            if allow_forced_edge_exit:
                if confirm_and_close_trade(client, brain, tid, trade_meta, "FORCED_EDGE_EXIT"):
                    print(
                        f"  🛑 [FORCED EDGE EXIT] Cierre forzado en {tid[:6]} tras {deg_streak} ciclos "
                        f"(Edge:{edge_score:+.3f} <= {Config.EDGE_DEGRADATION_EXIT} | PnL:{pnl_atr:+.2f}ATR | {pl_sats:+,} sats)"
                    )
                    return True

        if not is_hedged and not in_grace_period and system_mode == "RECOVERY" and current_dd_pct >= 0.08 and edge_score < -0.30:
            if confirm_and_close_trade(client, brain, tid, trade_meta, "RECOVERY_FORCED_REDUCTION"):
                print(
                    f"  🛑 [RECOVERY REDUCTION] Cierre forzado en {tid[:6]} por Recovery DD {current_dd_pct*100:.1f}% "
                    f"(Edge:{edge_score:+.3f} < -0.30 | Age:{trade_age_sec:.0f}s | {pl_sats:+,} sats)"
                )
                return True
        vel_ema = float(perception.get("vel_ema_atr", 0.0)) if isinstance(perception, dict) else 0.0
        ovr_side = perception.get("override_side") if isinstance(perception, dict) else None
        st_str = str(trade_meta.get("state", ""))
        macro_counter = (
            (side == "sell" and ("|BULL|" in st_str or "BULL_DIP" in st_str or "FULL_BULL" in st_str)) or
            (side == "buy" and ("|BEAR|" in st_str or "BEAR_RALLY" in st_str or "FULL_BEAR" in st_str))
        )
        contrary_momentum = (
            (side == "buy" and (vel_ema < -0.30 or rsi < 45.0 or ovr_side == "BEAR" or (macro_counter and ovr_side is None))) or
            (side == "sell" and (vel_ema > 0.30 or rsi > 55.0 or ovr_side == "BULL" or (macro_counter and ovr_side is None)))
        )

        if deg_streak >= Config.EDGE_DEGRADATION_PERSISTENCE and contrary_momentum:
            if pl_sats > max(20, int(est_round_fee_sats * 1.65)) and pnl_dist > (fee_offset * 1.65):
                if confirm_and_close_trade(client, brain, tid, trade_meta, "EDGE_DEGRADATION_PROFIT_PROTECT"):
                    print(
                        f"  🛡️ [GUARDIÁN EDGE PROTECT] Cosecha defensiva en {tid[:6]} tras {deg_streak} ciclos "
                        f"de degradación (Edge:{edge_score:+.3f} | +{pl_sats} sats)"
                    )
                    return True
            elif (
                not is_hedged
                and not in_grace_period
                and not (getattr(Config, "FE_OFF_TREND", True) and (action_id in (1, 5) or style == "TREND"))
                and ((edge_score <= Config.EDGE_DEGRADATION_EXIT and pnl_atr <= -1.00) or (macro_counter and ovr_side is None and edge_score <= -0.48 and pnl_atr <= -0.80))
            ):
                if confirm_and_close_trade(client, brain, tid, trade_meta, "EDGE_DEGRADATION_DEFENSIVE_EXIT"):
                    print(
                        f"  🛑 [GUARDIÁN EDGE CUT] Cierre defensivo en {tid[:6]} (Streak:{deg_streak} | "
                        f"Edge:{edge_score:+.3f} | PnL:{pnl_atr:+.2f}ATR | PnL:{pl_sats:+,} sats)"
                    )
                    return True

        vol_scale = 1.12 if atr_pct < 0.0025 else (0.88 if atr_pct > 0.0068 else 1.00)
        sys_scale = 0.72 if (system_mode in ("DEFENSIVE", "RECOVERY") and regime == "RANGE_SQUEEZE") else (0.86 if system_mode in ("DEFENSIVE", "RECOVERY") else 1.00)
        if deg_streak >= 2 and edge_score <= Config.EDGE_DEGRADATION_TIGHTEN:
            sys_scale *= 0.80

        harvest_target_atr = strat["harvest_atr"] * sys_scale
        min_profit_ok = pnl_dist > (fee_offset * 2.15) and pl_sats >= max(24, int(est_round_fee_sats * 2.15), int(margin * 0.045))

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

        auto_trail_mult = 1.0
        if hasattr(brain, "supervisor") and brain.supervisor is not None:
            auto_trail_mult = float(brain.supervisor.get_autonomous_adjustments().get("dynamic_trailing_multiplier", 1.0))

        # Piso duro de 0.65x a la compresión acumulada para evitar estrangular el trade por ruido
        effective_trail_scale = max(0.65, vol_scale * sys_scale * auto_trail_mult)
        # Exigir al menos 1.55x fee_offset de ganancia asegurada (FER >= 1.25 garantizado al bloquear SL)
        profit_lock_offset = fee_offset * 1.55
        min_trig = max(strat["trail_trig_atr"] * atr * effective_trail_scale, profit_lock_offset * 1.45)
        slack = max(atr * 0.45, strat["trail_slack_atr"] * atr * effective_trail_scale)

        if (side == "buy" and rsi > 74) or (side == "sell" and rsi < 26) or deg_streak >= 2:
            slack = max(atr * 0.38, slack * 0.85)

        if pnl_dist <= min_trig:
            return False

        if (
            sl_locked_server
            and not trade_meta.get("partial_cashed_in", False)
            and pnl_atr >= max(1.25, strat["trail_trig_atr"] * 1.15)
            and pl_sats >= max(35, int(round(est_round_fee_sats * 2.50)))
            and hasattr(client, "cash_in")
        ):
            cash_amt = max(15, int(round((pl_sats - est_round_fee_sats) * 0.40)))
            try:
                ci_res = client.cash_in(tid, cash_amt)
                if ci_res is not None:
                    trade_meta["partial_cashed_in"] = True
                    trade_meta["cashed_in_sats"] = int(trade_meta.get("cashed_in_sats", 0)) + cash_amt
                    brain.dirty = True
                    print(f"  💰 [CASH-IN 0% FEE] Extraídos +{cash_amt} sats al disponible en {tid[:6]} (SL en Breakeven).")
            except Exception:
                pass

        step_min = max(12.0, 0.15 * atr)
        use_struct_td9 = (
            bool(getattr(Config, "STRUCTURAL_TD9_TRAILING_ENABLED", False))
            if structural_td9_trailing_enabled is None
            else bool(structural_td9_trailing_enabled)
        )
        struct_sl = None
        if use_struct_td9 and isinstance(perception, dict):
            struct_sl = compute_structural_td9_trailing_stop(
                side_is_long=(side == "buy"),
                spot_price=lnm_price,
                current_sl=current_sl,
                entry_price=entry,
                atr=atr,
                td9_state=perception.get("td9"),
                hierarchy_pools=perception.get("structural_pools"),
                enabled=True,
            )
        if side == "buy":
            be_floor = entry + profit_lock_offset
            be_floor_int = int(math.ceil(be_floor))
            raw_sl = max(float(be_floor_int), lnm_price - slack)
            target_sl = int(round(min(raw_sl, lnm_price - max(22.0, 0.25 * atr))))
            if struct_sl is not None:
                target_sl = max(target_sl, int(round(min(struct_sl, lnm_price - max(22.0, 0.25 * atr)))))
            if target_sl >= be_floor_int and target_sl > (liq + 15.0) and target_sl > int(round(current_sl + step_min)):
                confirm_and_update_stoploss(client, brain, tid, target_sl, side, entry, fee_offset, trade_meta)
        elif side == "sell":
            be_ceil = entry - profit_lock_offset
            be_ceil_int = int(math.floor(be_ceil))
            raw_sl = min(float(be_ceil_int), lnm_price + slack)
            target_sl = int(round(max(raw_sl, lnm_price + max(22.0, 0.25 * atr))))
            if struct_sl is not None:
                target_sl = min(target_sl, int(round(max(struct_sl, lnm_price + max(22.0, 0.25 * atr)))))
            liq_ok = (liq <= 0.0) or (target_sl < (liq - 15.0))
            if target_sl <= be_ceil_int and liq_ok and (current_sl == 0.0 or target_sl < int(round(current_sl - step_min))):
                confirm_and_update_stoploss(client, brain, tid, target_sl, side, entry, fee_offset, trade_meta)
    except Exception as e:
        print(f"[GUARDIÁN AVISO] {trade.get('id','')[:6]}: {e}")
    return False


def evaluate_open_trades_online(brain, running, ind_5m, current_state, now):
    if not running:
        return
    atr, lnm_price = max(ind_5m["atr"], 1.0), ind_5m["price"]
    summaries = []

    for trade in running:
        tid = str(trade["id"])
        meta = brain.active_trades.get(tid)
        if not isinstance(meta, dict):
            continue

        # Monitoreo en tiempo real de excursión MFE / MAE neta en satoshis sin contaminar la Q-table
        pl_sats = float(trade.get("pl") or 0.0)
        margin_sats = float(trade.get("margin") or meta.get("margin", 1000.0))
        lev_f = float(trade.get("leverage") or meta.get("leverage", 25.0))
        est_round_fee = margin_sats * lev_f * (2.0 * Config.TAKER_FEE)
        carry_sats = max(0.0, float(extract_trade_funding_sats(trade)))
        net_unrealized_sats = pl_sats - est_round_fee - carry_sats
        if hasattr(brain, "supervisor") and brain.supervisor is not None:
            exc_rec = brain.supervisor.update_open_position_excursion(tid, net_unrealized_sats, margin_sats=margin_sats)
            meta["mfe_sats"] = exc_rec["mfe_sats"]
            meta["mae_sats"] = exc_rec["mae_sats"]
            meta["ticks_observed"] = exc_rec["ticks_observed"]

        if not Config.ENABLE_INTERMEDIATE_LEARNING:
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
            edge_score=float(meta.get("last_edge_score", 0.0)),
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

    is_long = (side == "buy")
    aligned_override = (is_long and override_side == "BULL") or ((not is_long) and override_side == "BEAR")
    mkt_regime = (
        perception.get("market_regime") if isinstance(perception, dict) and perception.get("market_regime")
        else ind_5m.get("market_regime", ind_5m.get("regime", "RANGE_NORMAL"))
    )

    if aligned_override:
        regime_mult = 0.72 if not recovery_mode else 0.52
    elif mkt_regime == "EXTREME_VOLATILITY":
        regime_mult = 0.35
    elif "RANGE_SQUEEZE" in norm_s and "CHOP" in norm_s:
        regime_mult = 0.36
    elif "CHOP" in norm_s:
        regime_mult = 0.45
    elif mkt_regime == "VOLATILITY_EXPANSION":
        regime_mult = 0.62
    elif "RANGE_SQUEEZE" in norm_s:
        regime_mult = 0.54
    elif "TREND" in norm_s and "LOW_EXP" in norm_s:
        regime_mult = 0.88
    else:
        regime_mult = 0.68

    bull_1h = "BULL" in mods.get("name_1h", "") or float(ind_1h.get("rsi", 50.0)) >= 54.0
    bear_1h = "BEAR" in mods.get("name_1h", "") or float(ind_1h.get("rsi", 50.0)) <= 46.0
    bull_4h = "BULL" in mods.get("name_4h", "")
    bear_4h = "BEAR" in mods.get("name_4h", "")

    if aligned_override:
        mtf_mult = 1.00
    elif (is_long and bull_1h and bull_4h) or (not is_long and bear_1h and bear_4h):
        mtf_mult = 1.08
    elif (is_long and bear_1h) or (not is_long and bull_1h):
        mtf_mult = 0.50
    else:
        mtf_mult = 0.80

    if q_val >= 0.08 and visits >= 5:
        conv_mult = 1.10
    elif q_val >= 0.03 and visits >= 3:
        conv_mult = 0.95
    elif q_val >= 0.0:
        conv_mult = 0.75
    else:
        conv_mult = 0.45

    dd_mult = max(0.45, 1.0 - (current_dd_pct * 2.2))
    rec_mult = 0.52 if recovery_mode else 1.0
    streak_mult = 0.82 ** min(4, max(0, consecutive_losses))

    macro_counter_trend = (
        (is_long and ("|BEAR|" in norm_s or "FULL_BEAR" in norm_s or "BEAR_RALLY" in norm_s)) or
        ((not is_long) and ("|BULL|" in norm_s or "FULL_BULL" in norm_s or "BULL_DIP" in norm_s))
    )
    if macro_counter_trend and "RANGE_SQUEEZE" in norm_s:
        mtf_mult = min(mtf_mult, 0.45)

    situational_mult = regime_mult * mtf_mult * conv_mult * dd_mult * rec_mult * streak_mult
    dynamic_lev = int(round(float(raw_lev) * situational_mult))
    dynamic_lev = max(Config.MIN_LEVERAGE, min(75 if not aligned_override else 80, dynamic_lev))
    if "RANGE_SQUEEZE" in norm_s:
        dynamic_lev = min(25 if macro_counter_trend else 32, dynamic_lev)
    elif macro_counter_trend:
        dynamic_lev = min(32, dynamic_lev)
    if recovery_mode:
        dynamic_lev = min(22 if "RANGE_SQUEEZE" in norm_s else 30, dynamic_lev)

    min_sl_dist = max(atr * 1.20, price * 0.0042)
    orig_sl_dist = abs(price - float(raw_sl)) if raw_sl else (atr * 1.38)
    desired_sl_dist = max(min_sl_dist, orig_sl_dist)

    safe_lev_cap = int(math.floor((price * 0.68) / max(desired_sl_dist, 1.0)))
    dynamic_lev = max(Config.MIN_LEVERAGE, min(dynamic_lev, safe_lev_cap))
    max_safe_sl_dist = price * (0.70 / max(dynamic_lev, Config.MIN_LEVERAGE))

    sl_dist = min(desired_sl_dist, max_safe_sl_dist)

    strat_rr = float(strat.get("rr", 1.50))
    adjusted_rr = max(1.30, strat_rr * float(combined_rr))
    min_tp_dist = max(sl_dist * adjusted_rr, price * 0.0080)
    orig_tp_dist = abs(float(raw_tp) - price) if raw_tp else (sl_dist * max(1.65, adjusted_rr))
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


def _is_hybrid_hedge_active(hedge_store) -> bool:
    if hedge_store is None:
        return False
    if bool(getattr(Config, "ENABLE_HYBRID_HEDGE", False)):
        return True
    fn = os.path.basename(str(getattr(hedge_store, "filename", "") or ""))
    return bool(fn and fn not in ("hedge_state_mainnet.json", "hedge_state_testnet.json"))


def reconcile_portfolio_with_hedges(
    client, brain, hedge_store, running, equity_sats=30000, btc_price=70000.0, now=None, safe_mode=False
):
    """
    Reconcilia posiciones aisladas de LN Markets API v3 separando posiciones
    primarias (gestionadas por AdvancedQBrain de 9 acciones) y patas de cobertura
    (gestionadas por AdaptiveHedgeController de 7 acciones). Limpia coberturas huérfanas.
    """
    now = float(now if now is not None else time.time())
    if not _is_hybrid_hedge_active(hedge_store):
        return running, {
            "primary_trades": list(running or []),
            "hedge_trades_map": {},
            "live_map": {str(t["id"]): t for t in (running or []) if isinstance(t, dict) and "id" in t},
            "locked_margin_sats": sum(int(t.get("margin") or 0) for t in (running or [])),
            "net_delta_notional_sats": 0,
            "gross_total_notional_sats": 0,
            "net_exposure_ratio": 0.0,
        }

    live_map = {str(t["id"]): t for t in (running or []) if isinstance(t, dict) and "id" in t}
    closed_orphan_ids = set()

    # Detectar coberturas huérfanas (la primaria cerró en el servidor pero la pata hedge sigue abierta)
    for orig_id, p in list(hedge_store.pairs.items()):
        hedge_id = str(p.get("hedge_trade_id")) if p.get("hedge_trade_id") else None
        orig_alive = orig_id in live_map
        hedge_alive = bool(hedge_id and hedge_id in live_map)
        if not orig_alive and hedge_alive:
            if not safe_mode:
                try:
                    resp = client.close_trade(hedge_id)
                    if isinstance(resp, dict) and (resp.get("closed") is True or str(resp.get("id", "")) == hedge_id):
                        closed_orphan_ids.add(hedge_id)
                        h_obj = live_map.get(hedge_id) or {}
                        h_pl = int(h_obj.get("pl") or 0)
                        h_mrg = int(h_obj.get("margin") or p.get("hedge_margin_sats", 0))
                        h_lev = float(h_obj.get("leverage") or p.get("hedge_leverage", 15.0))
                        h_notional = int(round(h_mrg * h_lev))
                        h_rt_fee = max(2, int(round(h_notional * 2.0 * Config.TAKER_FEE)))
                        if orig_id in brain.active_trades and isinstance(brain.active_trades[orig_id], dict):
                            brain.active_trades[orig_id]["realized_hedge_net_sats"] = (
                                int(brain.active_trades[orig_id].get("realized_hedge_net_sats", 0)) + (h_pl - h_rt_fee)
                            )
                            brain.active_trades[orig_id]["realized_hedge_fee_sats"] = (
                                int(brain.active_trades[orig_id].get("realized_hedge_fee_sats", 0)) + h_rt_fee
                            )
                            brain.dirty = True
                        print(f"  🧹 [LIMPIEZA HEDGE HUÉRFANO] Cerrada cobertura {hedge_id[:6]} tras cierre de primaria {orig_id[:6]} en servidor (net_est:{h_pl - h_rt_fee:+,}s).")
                except Exception:
                    pass
            hedge_store.pairs.pop(orig_id, None)
            hedge_store.dirty = True

    filtered_running = [t for t in (running or []) if str(t.get("id", "")) not in closed_orphan_ids]
    portfolio_snap = PortfolioHedgeCoordinator.reconcile_portfolio(
        filtered_running, hedge_store, equity_sats, btc_price, now=now
    )

    # Garantizar que ninguna pata de cobertura contamine active_trades del cerebro de 9 acciones
    for ht_id in portfolio_snap["hedge_trades_map"].keys():
        if ht_id in brain.active_trades:
            brain.active_trades.pop(ht_id, None)
            brain.dirty = True

    return filtered_running, portfolio_snap


def run_integrated_guardian_and_hedge_cycle(
    client, brain, hedge_store, portfolio_snap, primary_running,
    ind_5m, ind_15m, ind_1h, state, current_mode, edge_scores_by_tid,
    perception, q_vec, account_state, now, safe_mode=False
):
    """
    Ejecuta en cooperación el AdaptiveHedgeController (Fase 5) y el
    execute_strategy_guardian (Fases 1-3) sobre las posiciones primarias abiertas.
    """
    still_running = []
    current_dd_pct = float(account_state.get("current_dd_pct", 0.0))
    equity_sats = int(account_state.get("equity_sats", 30000))
    balance_sats = int(account_state.get("balance_sats", equity_sats))
    locked_floor_sats = int(account_state.get("locked_floor_sats", 0))
    lat_degraded = False
    if hasattr(client, "telemetry") and client.telemetry is not None:
        lat_degraded, _ = client.telemetry.evaluate_execution_health()

    hedge_map = portfolio_snap.get("hedge_trades_map", {})

    for trade in (primary_running or []):
        tid = str(trade["id"])
        t_meta = brain.active_trades.setdefault(tid, infer_adopted_trade(trade, ind_5m, state, now))
        act_idx = int(t_meta.get("action", 2 if trade.get("side", "buy") == "buy" else 6))
        q_val = float(q_vec[act_idx]) if act_idx < len(q_vec) else 0.0
        edge_sc = float(edge_scores_by_tid.get(tid, 0.0))

        closed_by_hedge = False
        if _is_hybrid_hedge_active(hedge_store):
            prev_pair = hedge_store.pairs.get(tid)
            prev_h_state = str(prev_pair.get("state", "NORMAL")) if isinstance(prev_pair, dict) else "NORMAL"
            h_tid = str(prev_pair.get("hedge_trade_id")) if (isinstance(prev_pair, dict) and prev_pair.get("hedge_trade_id")) else None
            hedge_trade_obj = hedge_map.get(h_tid) if h_tid else None

            hedge_dec = AdaptiveHedgeController.evaluate_and_manage_pair(
                client=client,
                orig_trade=trade,
                hedge_trade=hedge_trade_obj,
                orig_meta=t_meta,
                portfolio_snap=portfolio_snap,
                hedge_store=hedge_store,
                ind_5m=ind_5m,
                ind_15m=ind_15m,
                ind_1h=ind_1h,
                perception=perception,
                q_val_main=q_val,
                account_equity_sats=equity_sats,
                available_balance_sats=balance_sats,
                locked_floor_sats=locked_floor_sats,
                latency_degraded=lat_degraded,
                explorer_mode=Config.EXPLORER_MODE,
                safe_mode=safe_mode,
                now=now,
            )
            new_h_state = hedge_dec.get("state", "NORMAL")
            exec_info = hedge_dec.get("execution", {})
            pair_econ_dec = hedge_dec.get("pair_economics", {})
            if exec_info.get("executed") and hedge_dec.get("selected_action_name") in ("CLOSE_HEDGE", "REDUCE_HEDGE"):
                h_pl_realized = int(pair_econ_dec.get("hedge_pl_sats", 0))
                h_notional = int(pair_econ_dec.get("hedge_notional_sats", 0))
                h_fee_est = max(2, int(round(h_notional * 2.0 * Config.TAKER_FEE)))
                if hedge_dec.get("selected_action_name") == "REDUCE_HEDGE":
                    h_pl_realized = int(round(h_pl_realized * 0.5))
                    h_fee_est = max(1, h_fee_est // 2)
                t_meta["realized_hedge_net_sats"] = int(t_meta.get("realized_hedge_net_sats", 0)) + (h_pl_realized - h_fee_est)
                t_meta["realized_hedge_fee_sats"] = int(t_meta.get("realized_hedge_fee_sats", 0)) + h_fee_est
                brain.dirty = True
            if exec_info.get("executed") or (new_h_state != prev_h_state and new_h_state != "NORMAL"):
                eff_r = pair_econ_dec.get("effective_hedge_ratio", 0.0)
                adv_p = hedge_dec.get("predictive_signal", {}).get("adverse_continuation_prob", 0.0)
                print(
                    f"  🛡️ [HEDGE ENGINE] {tid[:6]} | {prev_h_state} ➔ {new_h_state} | "
                    f"Acción:{hedge_dec.get('selected_action_name')} (Ratio:{eff_r:.2f} | "
                    f"ProbAdv:{adv_p:.2f} | {exec_info.get('reason')})"
                )
            if hedge_dec.get("selected_action_name") == "CLOSE_ORIGINAL_POSITION" and exec_info.get("executed"):
                t_meta["ai_closed"] = True
                t_meta["close_reason"] = exec_info.get("reason", "HEDGE_CLOSE_ORIGINAL")
                brain.dirty = True
                closed_by_hedge = True

        if closed_by_hedge:
            continue

        pair_rec = hedge_store.pairs.get(tid) if _is_hybrid_hedge_active(hedge_store) else None
        is_hedged = bool(isinstance(pair_rec, dict) and pair_rec.get("hedge_trade_id"))

        closed_by_guardian = execute_strategy_guardian(
            client, brain, trade, ind_5m, ind_15m, t_meta, current_mode,
            edge_score=edge_sc, perception=perception,
            current_dd_pct=current_dd_pct, is_hedged=is_hedged
        )
        if closed_by_guardian:
            if is_hedged and not safe_mode:
                h_tid = str(pair_rec.get("hedge_trade_id", ""))
                if h_tid:
                    try:
                        client.close_trade(h_tid)
                        print(f"  🛡️ [HEDGE ENGINE] Cerrada cobertura vinculada {h_tid[:6]} tras cosecha/cierre de primaria {tid[:6]}.")
                    except Exception:
                        pass
                hedge_store.pairs.pop(tid, None)
                hedge_store.dirty = True
        else:
            still_running.append(trade)

    return still_running


def record_shadow_order(order_data: dict):
    """Registra una orden simulada del modo sombra en logs/shadow_signals.jsonl (Fase F7)."""
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "shadow_signals.jsonl")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(order_data, default=str) + "\n")
    except Exception:
        pass


def update_shadow_trades(brain, spot_price: float, now: float = None, current_state: str = None, ind_5m: dict = None):
    """
    Gemelo digital en Modo Sombra con Breakeven Lock, Dynamic Trailing Stop por estrategia
    y actualización amortiguada de Bellman normalizada al margen canónico (M_ref = 1000 sats).
    """
    now_ts = time.time() if now is None else float(now)
    shadow_dict = getattr(brain, "shadow_trades", None)
    if not isinstance(shadow_dict, dict) or not shadow_dict:
        return []
    closed_sims = []
    for sid in list(shadow_dict.keys()):
        st = shadow_dict[sid]
        entry = max(1.0, float(st["entry_price"]))
        sl = float(st["stoploss"])
        tp = float(st["takeprofit"])
        side = str(st["side"]).lower()
        m_sats = max(1000.0, float(st["margin_sats"]))
        lev = max(1.0, float(st["leverage"]))
        notional_sats = m_sats * lev
        cost = float(st.get("total_cost_sats", notional_sats * 2.0 * Config.TAKER_FEE))
        opened_ts = float(st.get("opened_at", st.get("timestamp", now_ts)))
        act_idx = int(st.get("action", 2 if side == "buy" else 6)) % Config.NUM_ACTIONS
        strat = brain.strategies.get(act_idx, brain.strategies[2 if side == "buy" else 6])

        atr = float(ind_5m.get("atr", 0.0)) if isinstance(ind_5m, dict) else 0.0
        if atr <= 0.0:
            atr = max(entry * 0.0018, abs(entry - sl) / max(1.0, float(strat.get("sl_atr", 1.6))))

        pnl_dist = (spot_price - entry) if side == "buy" else (entry - spot_price)
        pnl_atr = pnl_dist / max(1.0, atr)
        unrealized_net = (notional_sats * (pnl_dist / entry)) - cost
        st["mfe_sats"] = max(float(st.get("mfe_sats", 0.0)), max(0.0, unrealized_net))
        st["mae_sats"] = max(float(st.get("mae_sats", 0.0)), max(0.0, -unrealized_net))

        # Dynamic Trailing Stop & Breakeven Lock simétrico al Guardián en vivo
        fee_offset = entry * (2.4 * Config.TAKER_FEE)
        profit_lock_offset = fee_offset * 1.55
        min_trig = max(float(strat.get("trail_trig_atr", 0.85)) * atr, profit_lock_offset * 1.45)
        slack = max(atr * 0.45, float(strat.get("trail_slack_atr", 0.55)) * atr)

        if pnl_dist > min_trig:
            if side == "buy":
                be_floor = entry + profit_lock_offset
                new_sl = max(be_floor, spot_price - slack)
                if new_sl > sl and new_sl < spot_price:
                    sl = round(new_sl, 1)
                    st["stoploss"] = sl
                    st["sl_locked"] = True
            else:
                be_ceil = entry - profit_lock_offset
                new_sl = min(be_ceil, spot_price + slack)
                if (sl <= 0.0 or new_sl < sl) and new_sl > spot_price:
                    sl = round(new_sl, 1)
                    st["stoploss"] = sl
                    st["sl_locked"] = True

        outcome = None
        if side == "buy":
            if spot_price >= tp:
                gross = notional_sats * (tp - entry) / entry
                outcome = ("CLOSED_TP", gross - cost)
            elif spot_price <= sl:
                gross = notional_sats * (sl - entry) / entry
                status_lbl = "CLOSED_TRAIL_PROFIT" if st.get("sl_locked") and (gross - cost) > 0 else "CLOSED_SL"
                outcome = (status_lbl, gross - cost)
            elif (now_ts - opened_ts) >= 900.0:
                gross = notional_sats * (spot_price - entry) / entry
                outcome = ("CLOSED_TIMEOUT", gross - cost)
        else:
            if spot_price <= tp:
                gross = notional_sats * (entry - tp) / entry
                outcome = ("CLOSED_TP", gross - cost)
            elif spot_price >= sl:
                gross = notional_sats * (entry - sl) / entry
                status_lbl = "CLOSED_TRAIL_PROFIT" if st.get("sl_locked") and (gross - cost) > 0 else "CLOSED_SL"
                outcome = (status_lbl, gross - cost)
            elif (now_ts - opened_ts) >= 900.0:
                gross = notional_sats * (entry - spot_price) / entry
                outcome = ("CLOSED_TIMEOUT", gross - cost)

        if outcome is not None:
            status, net_sats = outcome
            rec = dict(st)
            rec["status"] = status
            rec["closed_price"] = spot_price
            rec["closed_timestamp"] = int(now_ts)
            rec["net_realized_sats"] = round(net_sats, 1)

            orig_st = str(st.get("state") or current_state or "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS")
            next_st = str(current_state or orig_st)
            reward_scale = max(1.0, brain.thresholds.get_float("reward", "reward_scale_sats", 100.0))
            q_min = brain.thresholds.get_float("q_calibration", "q_clip_min", -2.0)
            q_max = brain.thresholds.get_float("q_calibration", "q_clip_max", 2.0)

            # Normalización canónica (M_ref = 1000 sats) para invarianza de escala con Mainnet
            ref_m = float(Config.MIN_MARGIN_SATS)
            scale_eta = ref_m / max(ref_m, m_sats)
            norm_net_sats = float(net_sats) * scale_eta
            norm_mae_sats = float(st.get("mae_sats", 0.0)) * scale_eta
            lam = brain.thresholds.get_float("reward", "mae_penalty_lambda", 0.15)
            norm_econ_sats = norm_net_sats - (lam * norm_mae_sats)
            if net_sats < 0.0:
                norm_econ_sats = min(norm_econ_sats, norm_net_sats)

            shaped_r = float(np.clip(norm_econ_sats / reward_scale, q_min, q_max))
            alpha_base = brain.thresholds.get_float("exploration", "alpha_learning_rate", 0.18)
            alpha_recovery = 0.5 * alpha_base

            if act_idx > 0:
                dq = brain._update_q(
                    orig_st,
                    act_idx,
                    shaped_r,
                    next_st,
                    custom_alpha=alpha_recovery,
                    force_negative_on_loss=(net_sats < 0.0),
                    check_guard=False,
                    terminal=True,
                    shadow_virtual=True,
                )
                brain.state_visits[brain._normalize_state_key(orig_st)] = (
                    int(brain.state_visits.get(brain._normalize_state_key(orig_st), 0)) + 1
                )
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.record_shadow_closed_trade(
                        predicted_q_entry=st.get("entry_q", None),
                        net_realized_sats=net_sats,
                        signed_td_error=brain._last_td_error,
                        margin_sats=m_sats,
                    )
                rec["norm_net_sats_1k"] = round(norm_net_sats, 2)
                rec["td_error"] = round(brain._last_td_error, 4)
                rec["delta_q"] = round(dq, 4)
                brain.dirty = True

            record_shadow_order(rec)
            shadow_dict.pop(sid, None)
            closed_sims.append(rec)
            print(
                f"  🕶️ [MODO SOMBRA SIMULADO] {status} {sid[:12]} | Net: {net_sats:+.1f}s "
                f"(Norm1k: {norm_net_sats:+.1f}s) | Spot: ${spot_price:.1f} | dQ:{rec.get('delta_q', 0.0):.4f}"
            )
    return closed_sims


def process_settlement_queue(
    client,
    brain,
    closed_ids,
    state,
    now,
    recovery_anchor_equity,
    current_mode,
    consecutive_losses,
    risk_multiplier,
    pause_until,
    side_brake=None,
):
    """
    Maneja la cola de liquidación diferida y conciliación de órdenes cerradas (Fase F6).
    """
    settled_in_cycle = 0
    if not closed_ids:
        return settled_in_cycle, consecutive_losses, risk_multiplier, pause_until

    closed_map = {str(ct["id"]): ct for ct in client.get_closed_trades(limit=50)}
    tick_ref_sec = max(1.0, THRESHOLDS_MANAGER.get_float("reward", "tick_duration_sec", 15.0))
    for cid in closed_ids:
        info = brain.active_trades.get(cid)
        if not isinstance(info, dict):
            brain.active_trades.pop(cid, None)
            continue
        if cid in closed_map:
            brain.active_trades.pop(cid, None)
            brain.dirty = True
            settled_in_cycle += 1
            ct_obj = closed_map[cid]
            pl = int(round(float(ct_obj.get("pl") or 0)))
            m_used = int(info.get("margin", ct_obj.get("margin", 1000)))
            lev_used = float(ct_obj.get("leverage") or info.get("leverage", 35.0))
            open_fee, close_fee, funding_fee, has_explicit_fee = extract_trade_fees_v3(ct_obj)
            exact_fee = open_fee + close_fee
            fill_price = float(
                ct_obj.get("entryPrice")
                if ct_obj.get("entryPrice") is not None
                else (ct_obj.get("price") or ct_obj.get("entry_price") or 0.0)
            )
            dec_price = float(info.get("decision_price") or fill_price)
            t_side = str(ct_obj.get("side") or info.get("side") or "buy")
            econ = SatsExpectancyGate.compute_trade_economic_breakdown(
                pl,
                m_used,
                lev_used,
                exact_fee_sats=exact_fee,
                funding_fee_sats=funding_fee,
                entry_price=fill_price,
                decision_price=dec_price,
                side=t_side,
                gross_includes_execution=True,
                exact_fee_is_explicit=has_explicit_fee,
            )
            was_harvested = bool(info.get("ai_closed", False))
            orig_st = info.get("state", state)
            act_taken = int(info.get("action", 2))
            in_rec = (current_mode == "RECOVERY") or (recovery_anchor_equity is not None)
            balance = int(client.get_user(force_refresh=False).get("balance", 0))
            beat_anchor = recovery_anchor_equity is not None and balance >= int(round(recovery_anchor_equity * 1.03))

            realized_hedge_net = int(info.get("realized_hedge_net_sats", 0))
            realized_hedge_fee = int(info.get("realized_hedge_fee_sats", 0))
            cashed_in_adj = int(info.get("cashed_in_sats", 0))
            total_net_pnl_sats = int(econ["net_pnl_sats"]) + realized_hedge_net + cashed_in_adj
            total_taker_fee_sats = int(econ["taker_fee_sats"]) + realized_hedge_fee

            hit_tp_real = bool(ct_obj.get("takeprofit_wi") or ct_obj.get("takeprofitWi")) or (
                total_net_pnl_sats > m_used * 0.16 and not bool(ct_obj.get("stoploss_wi") or ct_obj.get("stoplossWi"))
            )
            entry_ts = float(info.get("entry_time", now))
            dur_ticks = max(1.0, (now - entry_ts) / tick_ref_sec)
            mae_tracked = float(info.get("mae_sats", 0.0))
            mfe_tracked = float(info.get("mfe_sats", max(0, total_net_pnl_sats)))
            pred_q = info.get("entry_q", None)

            brain.remember_and_train(
                orig_st,
                act_taken,
                pl + realized_hedge_net + cashed_in_adj,
                m_used,
                state,
                equity_after=balance,
                available_balance_after=balance,
                real_balance_delta_sats=total_net_pnl_sats,
                hit_tp=hit_tp_real,
                harvested=was_harvested,
                recovery_mode=in_rec,
                exited_recovery_bonus=beat_anchor,
                lev_used=lev_used,
                exact_fee_sats=total_taker_fee_sats,
                pl_includes_fees=True,
                funding_fee_sats=funding_fee,
                slippage_sats=econ["slippage_sats"],
                trade_id=cid,
                mae_sats=mae_tracked,
                mfe_sats=mfe_tracked,
                duration_ticks=dur_ticks,
                predicted_q_entry=pred_q,
                freeze_q_table=bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not Config.EXPLORER_MODE),
                exact_fee_is_explicit=has_explicit_fee,
            )
            if side_brake is not None:
                sb_info = side_brake.record_trade_close(t_side, total_net_pnl_sats, close_ts=now)
                if sb_info.get("triggered_pause"):
                    print(
                        f"  🛑 [FRENO C1_STREAK3_24H] Lado {str(t_side).upper()} pausado 24h tras "
                        f"{side_brake.max_consecutive_losses} pérdidas consecutivas "
                        f"(hasta ts={int(sb_info['pause_until_ts'])}). Lado opuesto activo a 1.00x."
                    )
            if total_net_pnl_sats < 0:
                consecutive_losses += 1
                risk_multiplier = max(0.55, risk_multiplier * Config.ANTI_MARTINGALE_FACTOR)
                if consecutive_losses >= Config.CONSECUTIVE_LOSS_LIMIT and not Config.EXPLORER_MODE:
                    pause_until = now + Config.CONSECUTIVE_LOSS_PAUSE_SEC
                    consecutive_losses = 0
            else:
                consecutive_losses = 0
                risk_multiplier = min(1.0, risk_multiplier + 0.15)
        else:
            wait_cyc = int(info.get("settlement_wait_cycles", 0)) + 1
            info["settlement_wait_cycles"] = wait_cyc
            brain.dirty = True
            if wait_cyc >= 8:
                brain.active_trades.pop(cid, None)
                print(f"  [AVISO LIQUIDACIÓN] Posición {str(cid)[:6]} no indexada en /closed tras {wait_cyc} ciclos; liberada.")

    return settled_in_cycle, consecutive_losses, risk_multiplier, pause_until


def manage_open_positions_and_hedges(
    client,
    brain,
    hedge_store,
    portfolio_state,
    primary_running,
    ind_5m,
    ind_15m,
    ind_1h,
    state,
    current_mode,
    edge_scores_by_tid,
    perception,
    q_vec,
    account_state,
    now,
    safe_mode=False,
):
    """
    Supervisa excursiones MFE/MAE en tiempo real, trailing stops y hedges integrados (Fase F6).
    """
    still_running = []
    if primary_running:
        still_running = run_integrated_guardian_and_hedge_cycle(
            client,
            brain,
            hedge_store,
            portfolio_state,
            primary_running,
            ind_5m,
            ind_15m,
            ind_1h,
            state,
            current_mode,
            edge_scores_by_tid,
            perception,
            q_vec,
            account_state,
            now,
            safe_mode=safe_mode,
        )
        evaluate_open_trades_online(brain, still_running, ind_5m, state, now)
    return still_running


def evaluate_and_dispatch_entries(
    client,
    brain,
    state,
    ind_5m,
    ind_15m,
    ind_1h,
    ind_4h,
    mods,
    recovery_mode,
    current_dd_pct,
    dynamic_max_pos,
    active_count,
    locked_margin,
    equity,
    balance,
    last_entry_time,
    now,
    consecutive_losses,
    warmup,
    risk_multiplier,
    sprint_mode,
    mkt_reg_tag,
    still_running,
    safe_mode,
    hedge_store,
    perception,
    shadow_mode=False,
    ind_1m=None,
    ind_1d=None,
    limit_mgr=None,
    side_brake=None,
):
    """
    Evalúa compuertas Bayesianas, calcula sizing 1/4 Kelly y despacha o simula en sombra (Fases F4, F5, F6, F7).
    """
    action_taken = 0
    det_policy = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not Config.EXPLORER_MODE)
    if det_policy and current_dd_pct >= float(getattr(Config, "DD_THROTTLE_HALT_PCT", 0.045)):
        return action_taken, last_entry_time

    _open_lim_cnt = limit_mgr.active_open_count() if limit_mgr is not None else 0
    if (active_count + _open_lim_cnt) >= min(dynamic_max_pos, Config.MAX_CONCURRENT_TRADES):
        return action_taken, last_entry_time

    fer_mult = 1.0
    if not det_policy and hasattr(brain, "supervisor") and brain.supervisor is not None:
        rolling_fer = brain.supervisor.get_rolling_fer()
        if rolling_fer < 1.0:
            fer_cooldown_cfg = THRESHOLDS_MANAGER.get_float("exploration", "fer_cooldown_mult", 2.0)
            fer_mult = max(1.0, fer_cooldown_cfg)

    effective_cooldown = (
        Config.ENTRY_COOLDOWN_SEC
        if det_policy
        else Config.ENTRY_COOLDOWN_SEC * fer_mult * (
            0.75 if Config.EXPLORER_MODE
            else (0.85 if perception.get("override_side") is not None else (1.8 if (recovery_mode or "CHOP" in state) else 1.0))
        )
    )

    if (locked_margin / max(equity, 1)) < Config.SOFT_EXPOSURE_LIMIT and (now - last_entry_time) >= effective_cooldown:
        dec_t0 = time.perf_counter()
        raw_action = brain.choose_action(
            state, ind_5m, ind_15m, ind_1h, ind_4h,
            recovery_mode=recovery_mode, current_dd_pct=current_dd_pct
        )
        raw_q_vec = brain.get_state_q_values(state)
        q_vec = raw_q_vec if det_policy else brain.get_conservative_q_values(state)
        action, gate_reason = SatsExpectancyGate.filter_and_rank_action(
            raw_action, state, q_vec, brain.strategies, perception,
            recovery_mode=recovery_mode, current_dd_pct=current_dd_pct,
            explorer_mode=Config.EXPLORER_MODE,
            deterministic_trend_policy=det_policy,
        )
        decision_elapsed_ms = (time.perf_counter() - dec_t0) * 1000.0
        client.telemetry.record("decision_ms", decision_elapsed_ms)

        if det_policy and action > 0:
            parts_st = str(state).split("|")
            seg1 = parts_st[0] if len(parts_st) > 0 else "RANGE_NORMAL"
            seg5 = parts_st[4] if len(parts_st) > 4 else "EMA_TRANS"
            if getattr(Config, "PURE_TREND_ONLY", True) and action not in (1, 5):
                action, gate_reason = 0, "BLOCKED_NON_TREND_OTR"
            elif action in (1, 5) and seg1 in ("RANGE_NORMAL", "RANGE_SQUEEZE", "CHOP_HIGHVOL", "EXTREME_VOLATILITY"):
                action, gate_reason = 0, f"BLOCKED_TREND_IN_{seg1}"
            elif action == 1 and (seg1 == "TREND_BEAR" or seg5 in ("FULL_BEAR", "BEAR_RALLY")):
                action, gate_reason = 0, "BLOCKED_L_TREND_STRICT_DIR_BEAR"
            elif action == 5 and (seg1 == "TREND_BULL" or seg5 in ("FULL_BULL", "BULL_DIP")):
                action, gate_reason = 0, "BLOCKED_S_TREND_STRICT_DIR_BULL"

        if action == 0 and raw_action > 0:
            if hasattr(brain, "supervisor") and brain.supervisor is not None:
                brain.supervisor.discard_last_action_decision()
            brain.remember_and_train(state, 0, 0, 1000, state, is_no_trade=True, freeze_q_table=det_policy)
            print(f"  ⏸️ [FILTRO SATS/SEGURIDAD] Entrada bloqueada ({gate_reason} | OVR:{perception.get('override_reason')})")

        if action > 0:
            q_val = float(q_vec[action]) if action < len(q_vec) else 0.0
            strat = brain.strategies[action]

            if side_brake is not None and getattr(Config, "STREAK3_24H_ENABLED", True):
                paused_side, rem_side_sec = side_brake.is_side_paused(strat["side"], now=now)
                if paused_side:
                    if hasattr(brain, "supervisor") and brain.supervisor is not None:
                        brain.supervisor.discard_last_action_decision()
                    print(
                        f"  ⏸️ [FRENO C1_STREAK3_24H] Entrada {strat['name']} bloqueada "
                        f"(BLOCKED_SIDE_STREAK3_24H | Restante:{int(rem_side_sec)}s)"
                    )
                    return 0, last_entry_time

            for t in (still_running or []):
                if t.get("side") == strat["side"]:
                    t_locked = bool(brain.active_trades.get(str(t["id"]), {}).get("sl_locked", False))
                    t_entry = float(t.get("price") or t.get("entryPrice") or ind_5m["price"])
                    if (not t_locked) or abs(float(ind_5m["price"]) - t_entry) < (1.25 * float(ind_5m["atr"])):
                        if hasattr(brain, "supervisor") and brain.supervisor is not None:
                            brain.supervisor.discard_last_action_decision()
                        print("  ⏸️ [GUARDIA ANTI-STACKING] Posición del mismo lado activa sin SL asegurado o muy cercana en precio.")
                        return 0, last_entry_time

            if isinstance(ind_1m, dict) and not det_policy:
                if (
                    strat["side"] == "buy"
                    and float(ind_1m.get("rsi", 50.0)) < 36.0
                    and float(ind_1m.get("macd_norm", 0.0)) < -0.15
                    and float(ind_1m.get("price_vs_vwap_pct", 0.0)) < -0.0015
                ):
                    if hasattr(brain, "supervisor") and brain.supervisor is not None:
                        brain.supervisor.discard_last_action_decision()
                    print("  ⏸️ [FILTRO MICRO 1M] Entrada bloqueada (MICRO_1M_FALLING_KNIFE)")
                    return 0, last_entry_time
                if (
                    strat["side"] == "sell"
                    and float(ind_1m.get("rsi", 50.0)) > 64.0
                    and float(ind_1m.get("macd_norm", 0.0)) > 0.15
                    and float(ind_1m.get("price_vs_vwap_pct", 0.0)) > 0.0015
                ):
                    if hasattr(brain, "supervisor") and brain.supervisor is not None:
                        brain.supervisor.discard_last_action_decision()
                    print("  ⏸️ [FILTRO MICRO 1M] Entrada bloqueada (MICRO_1M_BULL_SPIKE)")
                    return 0, last_entry_time

            if strat["style"] == "TREND" and not det_policy and float(ind_5m.get("adx", 25.0)) < 16.0:
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print("  ⏸️ [FILTRO ADX] Entrada TREND bloqueada (ADX_TOO_LOW_FOR_TREND)")
                return 0, last_entry_time

            if det_policy:
                price_f = float(ind_5m["price"])
                atr_f = float(ind_5m["atr"])
                eff_rr_mult = max(1.12, float(mods.get("rr_mult", 1.0)))
                lev, sl, tp = calculate_strategy_order_params(strat, price_f, atr_f, rr_mult=eff_rr_mult)
                effective_rr = max(1.5, float(strat["rr"]) * eff_rr_mult)
                floor_pct = float(getattr(Config, "TREND_SL_FLOOR_PCT", 0.0065))
                if floor_pct > 0.0 and action in (1, 5):
                    raw_sl_dist = max(atr_f * float(strat["sl_atr"]), price_f * 0.0024)
                    floor_dist = price_f * floor_pct
                    if floor_dist > raw_sl_dist:
                        sl_distance = floor_dist
                        lev = max(
                            Config.MIN_LEVERAGE,
                            min(Config.MAX_LEVERAGE, int((0.68 / (sl_distance / price_f)) * float(strat["lev_f"]))),
                        )
                        tp_distance = (sl_distance * effective_rr) + (price_f * (2.2 * Config.TAKER_FEE))
                        sl = (price_f - sl_distance) if strat["side"] == "buy" else (price_f + sl_distance)
                        tp = (price_f + tp_distance) if strat["side"] == "buy" else (price_f - tp_distance)
            else:
                combined_rr = mods["rr_mult"] * (Config.RECOVERY_RR_MULT if recovery_mode else 1.0)
                raw_lev, raw_sl, raw_tp = calculate_strategy_order_params(
                    strat, ind_5m["price"], ind_5m["atr"], rr_mult=combined_rr
                )
                lev, sl, tp = compute_situational_order_params(
                    raw_lev, raw_sl, raw_tp, strat, state, ind_5m, ind_15m, ind_1h, ind_4h,
                    mods, brain, action, recovery_mode, current_dd_pct, consecutive_losses,
                    combined_rr, perception=perception
                )
            reg_adapt = SatsExpectancyGate.compute_regime_risk_adaptation(
                mkt_reg_tag, current_dd_pct=current_dd_pct, recovery_mode=recovery_mode
            )
            base_risk = compute_effective_risk(
                warmup, risk_multiplier, recovery_mode, sprint_mode,
                current_dd_pct, parallel_risk_mult=mods["risk_mult"]
            )
            strat_style = strat.get("style", "SNIPER")
            norm_st_key = brain._normalize_state_key(state)
            st_visits = 1 if det_policy else int(brain.state_visits.get(norm_st_key, 0))
            eff_style_metrics = None if det_policy else brain.style_metrics
            p_hat, p_lcb, bayes_info = SatsExpectancyGate.estimate_bayesian_win_rate(
                state,
                action,
                strat_style,
                mkt_reg_tag,
                eff_style_metrics,
                q_val=q_val,
                state_visits=st_visits,
            )
            sl_dist_pct = abs(float(ind_5m["price"]) - float(sl)) / max(float(ind_5m["price"]), 1.0)
            if det_policy and getattr(Config, "SHORT_EXP_PHYS_HOLD", True) and action == 5 and sl_dist_pct <= float(getattr(Config, "SHORT_EXP_MIN_DSL", 0.006505)):
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print(
                    f"  ⏸️ [FILTRO C1_SHORT_EXP_PHYS] Entrada S_TREND bloqueada "
                    f"(BLOCKED_SHORT_NO_EXPANSION | d_SL={sl_dist_pct*100:.4f}% <= 0.6505%)"
                )
                return 0, last_entry_time

            fee_rt = THRESHOLDS_MANAGER.get_float("fees", "round_trip_fee_rate", 0.0020)
            tp_dist_pct = abs(float(tp) - float(ind_5m["price"])) / max(float(ind_5m["price"]), 1.0)

            b_ratio = max(0.2, (tp_dist_pct - fee_rt) / max(0.0001, sl_dist_pct + fee_rt))
            kelly_scale = THRESHOLDS_MANAGER.get_float("q_calibration", "fractional_kelly_scale", 0.25)
            raw_kelly = max(0.0, (p_hat * b_ratio - (1.0 - p_hat)) / b_ratio)
            if not det_policy and raw_kelly <= 0.0:
                kelly_mult = 0.55
            else:
                kelly_mult = float(np.clip(0.70 + (raw_kelly * kelly_scale * 3.0), 0.55, 1.40))
            if recovery_mode and not Config.EXPLORER_MODE and not det_policy:
                kelly_mult = min(kelly_mult, 0.72)

            if det_policy:
                dd_dampener = max(0.25, 1.0 - (current_dd_pct / 0.06) ** 2) if current_dd_pct > 0 else 1.0
                base_risk_pct = float(getattr(Config, "BASE_RISK_PCT_STD", 0.0075)) * dd_dampener
                eff_risk_pct = base_risk_pct * kelly_mult * reg_adapt["size_mult"]
                risk_budget_sats = max(120, int(round(equity * eff_risk_pct)))
            else:
                base_risk_pct = float(np.clip(base_risk * 0.35, 0.012, 0.035))
                eff_risk_pct = base_risk_pct * kelly_mult * reg_adapt["size_mult"]
                if (
                    strat["style"] == "TREND"
                    and isinstance(ind_1d, dict)
                    and (
                        (strat["side"] == "buy" and ind_1d.get("ema_state") == "BEAR_STRONG")
                        or (strat["side"] == "sell" and ind_1d.get("ema_state") == "BULL_STRONG")
                    )
                ):
                    eff_risk_pct *= 0.75
                risk_budget_sats = max(150, int(round(equity * eff_risk_pct)))

            loss_per_margin_sat = float(lev) * (sl_dist_pct + fee_rt)
            if loss_per_margin_sat > 0.0001:
                derived_margin = int(round(risk_budget_sats / loss_per_margin_sat))
            else:
                derived_margin = int(round(equity * 0.04))

            max_single_margin = int(round(equity * Config.MAX_SINGLE_TRADE_MARGIN_PCT))
            margin_sats = max(Config.MIN_MARGIN_SATS, min(max_single_margin, derived_margin))
            if margin_sats > derived_margin and derived_margin > 0:
                adjusted_lev = max(1, int(round((float(derived_margin) * float(lev)) / float(margin_sats))))
                if adjusted_lev < lev:
                    lev = adjusted_lev

            est_entry_fee = max(1, int(round(margin_sats * lev * Config.TAKER_FEE)))
            exp_ok, exp_reason, proj_exp = SatsExpectancyGate.verify_projected_exposure(
                locked_margin, margin_sats, equity, balance,
                max_exposure_ratio=Config.SOFT_EXPOSURE_LIMIT,
                est_entry_fee_sats=est_entry_fee,
                max_single_trade_risk_pct=Config.MAX_SINGLE_TRADE_MARGIN_PCT,
            )
            est_slip_bps = (
                THRESHOLDS_MANAGER.get_float("market_sensitivity", "max_slippage_bps_volatile", 7.5) * 0.35
                if mkt_reg_tag in ("EXTREME_VOLATILITY", "VOLATILITY_EXPANSION")
                else THRESHOLDS_MANAGER.get_float("market_sensitivity", "max_slippage_bps_normal", 3.0) * 0.35
            )
            auto_adj = brain.supervisor.get_autonomous_adjustments()
            viable_sats, sats_meta = SatsExpectancyGate.verify_net_sats_expectancy(
                ind_5m["price"], margin_sats, lev, sl, tp,
                explorer_mode=Config.EXPLORER_MODE,
                slippage_bps=est_slip_bps,
                expectancy_boost=(0.0 if det_policy else auto_adj["expectancy_boost_applied"]),
                state=state,
                action=action,
                style=strat_style,
                regime=mkt_reg_tag,
                style_metrics=eff_style_metrics,
                q_val=q_val,
                state_visits=st_visits,
            )
            if (
                not viable_sats
                and float(sats_meta.get("fee_ratio", 0.0)) >= 4.50
                and int(sats_meta.get("net_tp_sats", 0)) >= 120
                and float(sats_meta.get("net_rr", 0.0)) >= 1.25
            ):
                viable_sats = True

            if viable_sats and not Config.EXPLORER_MODE and (recovery_mode or "RANGE_SQUEEZE" in state):
                floor_rec = THRESHOLDS_MANAGER.get_int("expectancy", "min_net_profit_sats_recovery", 44)
                floor_std = THRESHOLDS_MANAGER.get_int("expectancy", "min_net_profit_sats", 38)
                base_cost_ref = int(sats_meta.get("total_cost_sats", sats_meta["fee_sats"]))
                min_strict_tp = max(
                    floor_rec if recovery_mode else floor_std,
                    int(round(base_cost_ref * (3.2 + current_dd_pct * 3.0 if recovery_mode else 2.9))),
                )
                if sats_meta["net_tp_sats"] < min_strict_tp:
                    viable_sats = False

            if viable_sats and not Config.EXPLORER_MODE and equity < 25000:
                if not (
                    float(sats_meta.get("net_rr", 0.0)) >= 1.35
                    or float(sats_meta.get("fee_ratio", 0.0)) >= 4.20
                ):
                    viable_sats = False

            _liq_cfg_entry = LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
            _liq_sig_entry = perception.get("liquidity") if isinstance(perception, dict) else None
            _liq_room = compute_opposite_pool_room(
                side_is_long=(1 <= (int(action) % 9) <= 4),
                entry_price=float(ind_5m["price"]),
                margin_sats=int(margin_sats),
                leverage=float(lev),
                taker_fee=THRESHOLDS_MANAGER.get_float("fees", "taker_fee_rate", 0.001),
                liquidity_signal=_liq_sig_entry,
                min_wall_fee_ratio=_liq_cfg_entry.min_opposite_wall_fee_ratio,
                min_wall_touches=_liq_cfg_entry.min_wall_touches,
            )
            sats_meta["liq_room"] = _liq_room
            if (
                viable_sats
                and _liq_cfg_entry.enabled
                and (not _liq_cfg_entry.shadow_only)
                and _liq_cfg_entry.opposite_wall_enabled
                and (not Config.EXPLORER_MODE)
                and (strat_style != "TREND")
                and (action not in (1, 5))
                and _liq_room.get("blocked_by_wall", False)
            ):
                viable_sats = False
                sats_meta["reason"] = (
                    f"BLOCKED_OPPOSITE_LIQ_WALL(pool={_liq_room['opposite_pool']}|"
                    f"ratio={_liq_room['room_fee_ratio']:.2f}x<{_liq_cfg_entry.min_opposite_wall_fee_ratio:.2f}x)"
                )

            pre_edge_info = compute_live_edge(
                brain=brain,
                state=state,
                action=action,
                ind_5m=ind_5m,
                perception=perception,
                margin_sats=margin_sats,
                leverage=lev,
                entry_price=float(ind_5m["price"]),
                simulated_entry=True,
            )
            edge_pre = float(pre_edge_info["edge_score"])
            theta_rec = -0.30
            delta_hyst = 0.45
            min_preflight_edge = (theta_rec + delta_hyst) if (recovery_mode or current_dd_pct >= 0.08) else 0.0
            preflight_blocked = (not Config.EXPLORER_MODE) and (edge_pre < min_preflight_edge)

            runtime_shadow_cb = False
            if (not det_policy) and hasattr(brain, "supervisor") and brain.supervisor is not None and hasattr(brain.supervisor, "evaluate_runtime_shadow_circuit_breaker"):
                runtime_shadow_cb = bool(brain.supervisor.evaluate_runtime_shadow_circuit_breaker())
            effective_shadow = bool(shadow_mode or Config.SHADOW_MODE or runtime_shadow_cb)
            raw_entry_q = float(raw_q_vec[action]) if action < len(raw_q_vec) else float(q_val)

            lat_degraded, lat_reason = client.telemetry.evaluate_execution_health()
            can_send, dup_reason = client.order_guard.can_send_order(
                strat["side"], margin_sats, lev, running_trades=still_running, now=now
            )

            if preflight_blocked:
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print(
                    f"  ⏸️ [FILTRO PRE-FLIGHT] Entrada bloqueada por riesgo de Churning "
                    f"(Edge_pre:{edge_pre:+.3f} < {min_preflight_edge:+.2f} | C_RT:{pre_edge_info['c_rt_sats']:.1f}s)"
                )
            elif not exp_ok:
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print(f"  ⏸️ [EXPOSICIÓN PROYECTADA BLOQUEADA] {exp_reason} (Margen:{margin_sats} sats)")
            elif not viable_sats:
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print(
                    f"  ⏸️ [EXPECTANCY SATS INSUFICIENTE] NetTP:+{sats_meta['net_tp_sats']} sats vs "
                    f"Coste:{sats_meta.get('total_cost_sats', sats_meta['fee_sats'])} sats (Ratio:{sats_meta['fee_ratio']}x)"
                )
            elif lat_degraded and not Config.EXPLORER_MODE:
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print(f"  ⏸️ [PROTECCIÓN DE LATENCIA] Entrada bloqueada por condición de ejecución degradada ({lat_reason})")
            elif not can_send:
                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                    brain.supervisor.discard_last_action_decision()
                print(f"  ⏸️ [GUARDIA ANTI-DUPLICADOS] {dup_reason}")
            elif safe_mode:
                print(
                    f"  🛡️ [MODO SEGURO - SIMULACIÓN] Señal válida {strat['name']} "
                    f"(Lev {lev}x | Margen {margin_sats} sats | SL:{sl} | TP:{tp} | ExpProy:{proj_exp*100:.1f}%)"
                )
            elif effective_shadow:
                shadow_tid = f"shadow-{int(now)}-{action}"
                shadow_rec = {
                    "id": shadow_tid,
                    "timestamp": int(now),
                    "opened_at": float(now),
                    "action": action,
                    "style": strat_style,
                    "name": strat["name"],
                    "side": strat["side"],
                    "margin_sats": margin_sats,
                    "leverage": lev,
                    "entry_price": float(ind_5m["price"]),
                    "stoploss": float(sl),
                    "takeprofit": float(tp),
                    "total_cost_sats": sats_meta.get("total_cost_sats", 0),
                    "expected_net_sats": sats_meta.get("expected_net_sats", 0.0),
                    "p_hat": sats_meta.get("p_hat", 0.0),
                    "p_star": sats_meta.get("p_star", 0.0),
                    "entry_q": raw_entry_q,
                    "edge_pre": edge_pre,
                    "state": state,
                    "status": "OPEN_SIMULATED",
                }
                record_shadow_order(shadow_rec)
                if not hasattr(brain, "shadow_trades"):
                    brain.shadow_trades = {}
                brain.shadow_trades[shadow_tid] = shadow_rec
                last_entry_time = now
                action_taken = action
                print(
                    f"  🕶️ [MODO SOMBRA - ORDEN SIMULADA] {strat['name']} {strat['side'].upper()} "
                    f"(Lev {lev}x | Margen {margin_sats} sats | Entry:${ind_5m['price']:.1f} | "
                    f"SL:{sl:.1f} | TP:{tp:.1f} | E_net:+{sats_meta.get('expected_net_sats', 0):.1f} sats)"
                )
            else:
                live_running = client.get_running_trades(force_refresh=False)
                hedge_tid_set = hedge_store.get_all_hedge_trade_ids() if Config.ENABLE_HYBRID_HEDGE else set()
                live_primary = [t for t in live_running if str(t.get("id")) not in hedge_tid_set]
                _eff_slot_cap = min(dynamic_max_pos, Config.MAX_CONCURRENT_TRADES)
                _open_lim_now = limit_mgr.active_open_count() if limit_mgr is not None else 0
                if (len(live_primary) + _open_lim_now) < _eff_slot_cap:
                    _lim_cfg = LimitExecutionConfig.from_thresholds(THRESHOLDS_MANAGER)
                    _liq_sig_cur = perception.get("liquidity") if isinstance(perception, dict) else None
                    _use_limit, _lim_route_reason = should_route_limit(
                        action=action,
                        style=strat_style,
                        market_regime=mkt_reg_tag,
                        liquidity_signal=_liq_sig_cur,
                        spot_price=float(ind_5m["price"]),
                        atr_5m=float(ind_5m["atr"]),
                        taker_fee=THRESHOLDS_MANAGER.get_float("fees", "taker_fee_rate", 0.001),
                        recovery_mode=recovery_mode,
                        cfg=_lim_cfg,
                    )
                    if _use_limit and limit_mgr is not None:
                        _lim_plan = compute_limit_entry_plan(
                            side=strat["side"],
                            style=strat_style,
                            spot_price=float(ind_5m["price"]),
                            atr_5m=float(ind_5m["atr"]),
                            base_sl=float(sl),
                            base_tp=float(tp),
                            liquidity_signal=_liq_sig_cur,
                            cfg=_lim_cfg,
                        )
                        if not _lim_plan.get("valid", False):
                            if hasattr(brain, "supervisor") and brain.supervisor is not None:
                                brain.supervisor.discard_last_action_decision()
                            print(f"  ⏸️ [LÍMITE OMITIDA] {strat['name']} | {_lim_plan.get('reason')} ({_lim_route_reason})")
                        else:
                            client.telemetry.record("order_dispatch_ms", (time.perf_counter() - dec_t0) * 1000.0)
                            _ok_lim, _sub_reason, _sub_rec = limit_mgr.submit_limit_order(
                                client=client,
                                side=strat["side"],
                                margin_sats=int(margin_sats),
                                leverage=float(lev),
                                plan=_lim_plan,
                                meta={
                                    "style": strat_style,
                                    "action": int(action),
                                    "state": state,
                                    "entry_q": float(raw_entry_q),
                                    "is_exploratory": bool(getattr(brain, "_last_decision_exploratory", False)),
                                    "liq_score": float((_liq_sig_cur or {}).get("liquidity_score", 0.0)),
                                },
                                running_count=len(live_primary),
                                atr_5m=float(ind_5m["atr"]),
                                max_slots=_eff_slot_cap,
                                now=now,
                            )
                            if _ok_lim and _sub_rec is not None:
                                last_entry_time = now
                                action_taken = action
                                print(
                                    f"  🎯 [LÍMITE COLOCADA] {strat['name']} [{len(live_primary)}+{limit_mgr.active_open_count()}/{_eff_slot_cap}] "
                                    f"@ ${_lim_plan['limit_price']:.1f} (Mejora:${_lim_plan['improvement_usd']:.1f} | TTL:{int(_lim_plan['ttl_sec'])}s | "
                                    f"SL:{_lim_plan['stoploss']:.1f} | TP:{_lim_plan['takeprofit']:.1f} | {_lim_route_reason})"
                                )
                            else:
                                if hasattr(brain, "supervisor") and brain.supervisor is not None:
                                    brain.supervisor.discard_last_action_decision()
                                print(f"  ⏸️ [LÍMITE RECHAZADA] {strat['name']} | {_sub_reason}")
                    else:
                        last_entry_time = now
                        action_taken = action
                        intent_fp = client.order_guard.register_intent(
                            strat["side"], margin_sats, lev, state_key=state, now=now
                        )
                        client.telemetry.record("order_dispatch_ms", (time.perf_counter() - dec_t0) * 1000.0)
                        try:
                            new_t = client.open_trade(strat["side"], margin_sats, lev, sl, tp)
                            client.order_guard.clear_intent(intent_fp)
                            if isinstance(new_t, dict) and "id" in new_t:
                                nt_id = str(new_t["id"])
                                adopted_meta = infer_adopted_trade(
                                    new_t, ind_5m, state, now, forced_action=action
                                )
                                adopted_meta["decision_price"] = float(ind_5m["price"])
                                adopted_meta["entry_q"] = float(raw_entry_q)
                                adopted_meta["is_exploratory"] = bool(getattr(brain, "_last_decision_exploratory", False))
                                adopted_meta["entry_time"] = float(now)
                                adopted_meta["opened_at"] = float(now)
                                brain.active_trades[nt_id] = adopted_meta
                                brain.dirty = True
                                brain.save_brain(force=False)
                                print(
                                    f"  → Abierta {strat['name']} [{len(live_primary)+1}/{Config.MAX_CONCURRENT_TRADES}] "
                                    f"(Lev {lev}x | Margen {margin_sats} sats | ExpProy:{proj_exp*100:.1f}% | Salida:{strat['exit_mode']})"
                                )
                        except Exception as open_err:
                            print(f"  [VERIFICANDO ORDEN POST-TIMEOUT] {open_err}")
                            time.sleep(1.2)
                            verify_running = client.get_running_trades(force_refresh=True)
                            hedge_tid_set = hedge_store.get_all_hedge_trade_ids() if Config.ENABLE_HYBRID_HEDGE else set()
                            matched_post_timeout = False
                            for vt in verify_running:
                                vtid = str(vt["id"])
                                if vtid not in brain.active_trades and vtid not in hedge_tid_set:
                                    adopted_meta = infer_adopted_trade(
                                        vt, ind_5m, state, now, forced_action=action
                                    )
                                    adopted_meta["decision_price"] = float(ind_5m["price"])
                                    adopted_meta["entry_q"] = float(q_val)
                                    adopted_meta["is_exploratory"] = bool(getattr(brain, "_last_decision_exploratory", False))
                                    adopted_meta["entry_time"] = float(now)
                                    adopted_meta["opened_at"] = float(now)
                                    brain.active_trades[vtid] = adopted_meta
                                    brain.dirty = True
                                    matched_post_timeout = True
                            if matched_post_timeout:
                                client.order_guard.clear_intent(intent_fp)
                            brain.save_brain(force=False)
                else:
                    if hasattr(brain, "supervisor") and brain.supervisor is not None:
                        brain.supervisor.discard_last_action_decision()

    return action_taken, last_entry_time


def _parse_cli_env():
    env_val = os.getenv("ENV_MODE") or os.getenv("SNIPER_ENV") or "mainnet"
    for idx, arg in enumerate(sys.argv):
        if arg == "--env" and idx + 1 < len(sys.argv):
            env_val = sys.argv[idx + 1]
        elif arg.startswith("--env="):
            env_val = arg.split("=", 1)[1]
        elif arg == "--testnet":
            env_val = "testnet"
    return env_val


def _print_cli_usage():
    print(
        "Uso: python3 lnm_sniper_v6_advanced.py [OPCIONES]\n"
        "  --env {mainnet|testnet}   Selecciona entorno de ejecucion\n"
        "  --testnet                 Atajo para --env testnet\n"
        "  --safe-mode               Gestiona posiciones abiertas sin nuevas entradas\n"
        "  --once                    Ejecuta un ciclo unico y termina\n"
        "  --dry-check               Valida configuracion efectiva sin credenciales ni red\n"
        "  -h, --help                Muestra esta ayuda y termina"
    )


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        _print_cli_usage()
        return

    env_selected = _parse_cli_env()
    Config.configure_environment(env_selected)

    if "--dry-check" in sys.argv:
        liq_cfg = LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
        lim_cfg = LimitExecutionConfig.from_thresholds(THRESHOLDS_MANAGER)
        SideStreakBrakeStore(env_mode=Config.ENV_MODE, persist_to_disk=False)
        print(
            f"[DRY-CHECK OK] env={Config.ENV_MODE} | det={Config.DETERMINISTIC_TREND_POLICY} | "
            f"explorer={Config.EXPLORER_MODE} | hedge={Config.ENABLE_HYBRID_HEDGE} | "
            f"max_pos={Config.MAX_CONCURRENT_TRADES} | risk={Config.BASE_RISK_PCT_STD} | "
            f"sl_floor={Config.TREND_SL_FLOOR_PCT} | lim_en={lim_cfg.enabled} | "
            f"liq_en={liq_cfg.enabled} | liq_shadow={liq_cfg.shadow_only} | "
            f"opp_wall={liq_cfg.opposite_wall_enabled}"
        )
        return

    safe_mode = ("--safe-mode" in sys.argv) or (os.getenv("SNIPER_SAFE_MODE", "0") == "1")
    once_mode = ("--once" in sys.argv)
    log_file_path = os.getenv("SNIPER_LOG_FILE", f"sniper_{Config.ENV_MODE}.log")

    api_key, api_secret, api_passphrase = load_saved_credentials(Config.ENV_MODE)
    if not api_key or not api_secret or not api_passphrase:
        if Config.ENV_MODE == "testnet":
            raise SystemExit("ERROR CRITICO: Credenciales Testnet ausentes. Abortando para proteger Mainnet.")
        print(f"[ERROR CRÍTICO] Faltan credenciales de LN Markets para el entorno '{Config.ENV_MODE}' en variables o .env.")
        return

    pid_path = getattr(Config, "PID_FILE", f"sniper_{Config.ENV_MODE}.pid")
    try:
        with FileLockContext(pid_path, exclusive=True, timeout=2.0, raise_on_timeout=False):
            with open(pid_path, "w", encoding="utf-8") as pfh:
                pfh.write(str(os.getpid()))
                pfh.flush()
    except Exception:
        pass

    brain = AutonomousGoalBrain(
        filename=Config.BRAIN_FILE,
        explorer_mode=Config.EXPLORER_MODE,
        learning_enabled=(not getattr(Config, "DETERMINISTIC_TREND_POLICY", True) or Config.EXPLORER_MODE),
    )
    client = LNMarketsClient(api_key, api_secret, api_passphrase, base_url=Config.BASE_URL)
    mtf_cache = MultiTimeframeCache()
    api_health = APIHealthMonitor()
    floor_guardian = HardFloorGuardian(
        initial_floor_ratio=Config.INITIAL_FLOOR_RATIO,
        milestone_floor_ratio=Config.MILESTONE_FLOOR_RATIO,
        max_dd_pct=Config.MAX_DRAWDOWN_PCT,
        max_daily_loss_pct=Config.MAX_DAILY_LOSS_PCT,
        kill_switch_dd_pct=getattr(Config, "KILL_SWITCH_DD_PCT", 0.22),
    )
    hedge_store = HedgeStateStore(
        env_mode=Config.ENV_MODE,
        filename=Config.HEDGE_STATE_FILE,
    )

    stop_state = {"requested": False, "sig": None}

    limit_mgr = LimitExecutionManager(
        env_mode=Config.ENV_MODE,
        base_dir=os.path.dirname(os.path.abspath(__file__)),
        max_concurrent_trades=Config.MAX_CONCURRENT_TRADES,
    )
    side_brake = SideStreakBrakeStore(
        env_mode=Config.ENV_MODE,
        max_consecutive_losses=getattr(Config, "STREAK3_MAX_LOSSES", 3),
        pause_duration_sec=getattr(Config, "STREAK3_PAUSE_SEC", 86400.0),
        enabled=getattr(Config, "STREAK3_24H_ENABLED", True),
    )

    def _handle_shutdown_signal(signum, _frame):
        stop_state["requested"] = True
        stop_state["sig"] = signum

    for sig_name in ("SIGTERM", "SIGINT"):
        sig_obj = getattr(signal, sig_name, None)
        if sig_obj is not None:
            try:
                signal.signal(sig_obj, _handle_shutdown_signal)
            except Exception:
                pass

    daily_start_equity, daily_start_date, peak_equity = None, None, None
    recovery_anchor_equity = None
    consecutive_losses, last_entry_time, risk_multiplier, pause_until = 0, 0.0, 1.0, 0.0
    flat_defensive_streak = 0
    cycle_counter = 0
    current_mode = "NORMAL"
    last_known_balance = None

    print("====================================================================")
    print(f"  LNM SNIPER v7.3+ DUAL [{Config.ENV_MODE.upper()}] – ARQUITECTURA DE SATS NETOS + HEDGE ENGINE (API v3)")
    print(f"  Endpoint: {Config.BASE_URL} | Cerebro: {Config.BRAIN_FILE} | Hedge: {Config.HEDGE_STATE_FILE}")
    print(f"  Meta Balance: {brain.current_target:,} sats | Máx Pos: {Config.MAX_CONCURRENT_TRADES} | Explorer: {Config.EXPLORER_MODE}")
    if safe_mode:
        print("  🛡️ [MODO SEGURO ACTIVO] Solo reconciliación, Guardian y análisis (sin nuevas órdenes reales).")
    print("====================================================================")

    try:
        while not stop_state["requested"]:
            cycle_t0 = time.perf_counter()
            cycle_counter += 1
            if cycle_counter % 120 == 0:
                rotate_log_file_if_needed(log_file_path)

            warmup = brain.is_warming_up()
            if api_health.check_cooldown():
                continue

            try:
                now = time.time()
                brain.sync_from_disk_if_idle()
                bucket_1h = int(now // 3600)
                bucket_4h = int(now // 14400)

                if bucket_1h > int(brain.cycle_state["1h"].get("bucket", 0)):
                    mtf_cache.invalidate("1h")
                if bucket_4h > int(brain.cycle_state["4h"].get("bucket", 0)):
                    mtf_cache.invalidate("4h")

                running = client.get_running_trades(force_refresh=True)
                live_id_set = {str(x["id"]) for x in (running or []) if isinstance(x, dict) and "id" in x}
                prelim_closed = [tid for tid in list(brain.active_trades.keys()) if str(tid) not in live_id_set]

                user = client.get_user(force_refresh=bool(prelim_closed) or (last_known_balance is None))
                balance = int(user.get("balance", 0))
                if last_known_balance is None:
                    last_known_balance = balance

                prelim_equity, _, _ = compute_account_equity(balance, running)
                lnm_price = client.get_ticker_price()

                pre_reconcile_count = len(running or [])
                running, portfolio_state = reconcile_portfolio_with_hedges(
                    client, brain, hedge_store, running,
                    equity_sats=max(1, prelim_equity or last_known_balance or 30000),
                    btc_price=max(1.0, float(lnm_price or 70000.0)),
                    now=now,
                    safe_mode=safe_mode,
                )
                if len(running or []) < pre_reconcile_count:
                    user = client.get_user(force_refresh=True)
                    balance = int(user.get("balance", balance))
                primary_running = portfolio_state["primary_trades"]
                hedge_running = list(portfolio_state["hedge_trades_map"].values())
                primary_id_set = {str(x["id"]) for x in primary_running}
                closed_ids = [tid for tid in list(brain.active_trades.keys()) if str(tid) not in primary_id_set]

                if len(primary_running) > Config.MAX_CONCURRENT_TRADES:
                    primary_running = enforce_concurrency_cap(client, brain, primary_running)
                    running = primary_running + hedge_running
                    user = client.get_user(force_refresh=True)
                    balance = int(user.get("balance", 0))

                equity, locked_margin, floating_pl = compute_account_equity(balance, running)
                if equity <= 0:
                    time.sleep(Config.IDLE_ANALYZE_INTERVAL_SEC)
                    continue

                brain.check_and_advance_goal(balance)

                proc_t0 = time.perf_counter()
                ind_5m = mtf_cache.get_indicators_fast("5m", lnm_price)
                ind_15m = mtf_cache.get_indicators_fast("15m", lnm_price)
                ind_1h = mtf_cache.get_indicators_fast("1h")
                ind_4h = mtf_cache.get_indicators_fast("4h")
                try:
                    ind_1m = mtf_cache.get_indicators_fast("1m", lnm_price)
                except Exception:
                    ind_1m = ind_5m
                try:
                    ind_1d = mtf_cache.get_indicators_fast("1d")
                except Exception:
                    ind_1d = ind_4h

                api_health.record_success()
                THRESHOLDS_MANAGER.check_and_reload(active_trades_count=len(primary_running))
                state = brain.discretize_state(ind_5m, ind_15m, ind_1h, ind_4h, len(primary_running))

                settled_in_cycle, consecutive_losses, risk_multiplier, pause_until = process_settlement_queue(
                    client, brain, closed_ids, state, now,
                    recovery_anchor_equity, current_mode,
                    consecutive_losses, risk_multiplier, pause_until,
                    side_brake=side_brake,
                )
                if closed_ids:
                    last_known_balance = balance

                if Config.SHADOW_MODE or getattr(brain.supervisor, "runtime_shadow_active", False) or hasattr(brain, "shadow_trades"):
                    update_shadow_trades(brain, float(ind_5m["price"]), now=now, current_state=state, ind_5m=ind_5m)

                for t in primary_running:
                    tid = str(t["id"])
                    existing = brain.active_trades.get(tid)
                    if not isinstance(existing, dict) or not existing.get("style") or str(existing.get("style")) == "None":
                        brain.active_trades[tid] = infer_adopted_trade(t, ind_5m, state, now, existing_meta=existing)
                        brain.dirty = True
                    elif int(existing.get("settlement_wait_cycles", 0)) > 0:
                        existing["settlement_wait_cycles"] = 0

                brain.evaluate_and_close_macro_cycle("1h", bucket_1h, equity, ind_5m["price"], ind_1h, primary_running)
                brain.evaluate_and_close_macro_cycle("4h", bucket_4h, equity, ind_5m["price"], ind_4h, primary_running)

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
                prev_cnt = getattr(brain, "_last_loop_count", len(primary_running))
                if len(primary_running) < prev_cnt and equity < prev_eq - 35 and not Config.EXPLORER_MODE:
                    if settled_in_cycle == 0:
                        consecutive_losses += 1
                    pause_until = max(pause_until, now + min(600, 120 * max(1, consecutive_losses)))
                    last_entry_time = now
                    print(f"  🛑 [PROTECCIÓN STOP-OUT] Pérdida detectada (Racha: {consecutive_losses}). Enfriamiento por {int(pause_until - now)}s.")
                elif len(primary_running) < prev_cnt and equity > prev_eq + 25 and settled_in_cycle == 0:
                    consecutive_losses = max(0, consecutive_losses - 1)
                brain._last_loop_equity = equity
                brain._last_loop_count = len(primary_running)

                daily_pnl_pct = (equity - daily_start_equity) / max(daily_start_equity, 1)
                prot = floor_guardian.evaluate_protection(equity, balance, peak_equity, brain.locked_floor, daily_pnl_pct)
                current_dd_pct = prot["current_dd_pct"]
                floor_threat = prot["should_liquidate"] and not Config.EXPLORER_MODE

                # Prevención de bloqueo permanente (Deadlock) en DEFENSIVE cuando no hay posiciones abiertas (Act: 0/5)
                if len(running) == 0 and current_dd_pct >= Config.MAX_DRAWDOWN_PCT and not floor_threat and daily_pnl_pct > -abs(Config.MAX_DAILY_LOSS_PCT):
                    flat_defensive_streak += 1
                    if flat_defensive_streak >= 25 and equity > int(round(brain.locked_floor * 1.03)):
                        target_reanchor = int(round(equity / (1.0 - (Config.MAX_DRAWDOWN_PCT - 0.015))))
                        peak_equity = min(peak_equity, max(equity, target_reanchor))
                        brain.peak_equity = peak_equity
                        brain.dirty = True
                        prot = floor_guardian.evaluate_protection(equity, balance, peak_equity, brain.locked_floor, daily_pnl_pct)
                        current_dd_pct = prot["current_dd_pct"]
                        flat_defensive_streak = 0
                        print(
                            f"  🔓 [DESBLOQUEO RECOVERY PLANO] 0 pos activas y piso seguro -> "
                            f"Re-anclando Peak={peak_equity:,} sats (DD ajustado: {current_dd_pct*100:.1f}%) para habilitar modo RECOVERY."
                        )
                else:
                    flat_defensive_streak = 0

                goal_pct = (balance / max(brain.current_target, 1)) * 100.0
                sprint_mode = goal_pct >= 88.0

                perception = PERCEPTION_ENGINE.evaluate(state, ind_5m, ind_15m, ind_1h, ind_4h, now=now)
                perception["liquidity"] = _evaluate_liquidity_from_mtf(mtf_cache, ind_5m, ind_15m, lnm_price)
                client.telemetry.record("processing_ms", (time.perf_counter() - proc_t0) * 1000.0)

                ovr_tag = perception["override_side"] or "OFF"
                mkt_reg_tag = perception.get("market_regime", ind_5m.get("market_regime", "RANGE"))
                q_vec = brain.get_conservative_q_values(state)

                edge_scores_by_tid = {}
                for _t in primary_running:
                    _tid = str(_t["id"])
                    _meta = brain.active_trades.get(_tid, {})
                    _act = int(_meta.get("action", 2 if _t.get("side", "buy") == "buy" else 6))
                    _carry = extract_trade_funding_sats(_t)
                    _edge_res = compute_live_edge(
                        brain=brain,
                        state=state,
                        action=_act,
                        ind_5m=ind_5m,
                        perception=perception,
                        margin_sats=int(_t.get("margin") or _meta.get("margin", 1000)),
                        leverage=float(_t.get("leverage") or _meta.get("leverage", 15.0)),
                        entry_price=float(_t.get("price") or _t.get("entryPrice") or ind_5m["price"]),
                        carry_fees_sats=_carry,
                        simulated_entry=False,
                        orig_meta=_meta,
                        now=now,
                    )
                    edge_score = float(_edge_res["edge_score"])
                    _pnl_atr = float(_edge_res["pnl_atr_raw"])
                    _q_act = float(_edge_res["q_cql"])
                    _meta["last_pnl_atr"] = _pnl_atr
                    edge_scores_by_tid[_tid] = edge_score
                    _deg_cnt = int(_meta.get("edge_deg_streak", 0))
                    if edge_score <= Config.EDGE_DEGRADATION_WARN and (_deg_cnt == 1 or _deg_cnt % 5 == 0):
                        print(f"  ⚠️ [DEGRADACIÓN DE EDGE] ID:{_tid[:6]} | Act:{_act} | Edge:{edge_score:+.3f} (Q_cql:{_q_act:+.3f} | PnL:{_pnl_atr:+.2f}ATR | OVR:{ovr_tag} | Streak:{_deg_cnt})")

                if floor_threat and (now >= pause_until):
                    print(f"  🚨 [GUARDIÁN PISO DURO UNIFICADO] {prot['reason']} | Eq:{equity:,} <= Piso:{brain.locked_floor:,}. Liquidando portafolio completo y pausando 45m.")
                    for t_kill in list(running):
                        try:
                            confirm_and_close_trade(client, brain, str(t_kill["id"]), brain.active_trades.get(str(t_kill["id"]), {}), "HARD_FLOOR_KILL_SWITCH")
                        except Exception:
                            pass
                    running = []
                    primary_running = []
                    hedge_running = []
                    brain.active_trades.clear()
                    hedge_store.pairs.clear()
                    hedge_store.dirty = True
                    pause_until = now + 2700
                    peak_equity = equity
                    brain.peak_equity = equity
                    milestone_floor = (
                        int(round(brain.current_target * Config.MILESTONE_FLOOR_RATIO / 1.40))
                        if getattr(brain, "milestone_level", 1) > 1
                        else 0
                    )
                    brain.locked_floor = max(milestone_floor, int(round(equity * Config.INITIAL_FLOOR_RATIO)))
                    brain.dirty = True
                    brain.save_brain(force=True)
                    hedge_store.save(force=True)
                    current_dd_pct = 0.0
                    floor_threat = False

                det_guard_active = bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True) and not Config.EXPLORER_MODE)
                if (
                    det_guard_active
                    and running
                    and (not safe_mode)
                    and current_dd_pct >= float(getattr(Config, "MAX_DD_GUARD_PCT", 0.052))
                ):
                    print(
                        f"  🚨 [MAX_DD_GUARD 5.2%] DD={current_dd_pct*100:.2f}% >= "
                        f"{float(getattr(Config, 'MAX_DD_GUARD_PCT', 0.052))*100:.2f}%. Liquidando portafolio defensivamente."
                    )
                    for t_dd in list(running):
                        try:
                            confirm_and_close_trade(
                                client,
                                brain,
                                str(t_dd["id"]),
                                brain.active_trades.get(str(t_dd["id"]), {}),
                                "MAX_DD_GUARD",
                            )
                        except Exception:
                            pass
                    running = []
                    primary_running = []
                    hedge_running = []
                    brain.active_trades.clear()
                    hedge_store.pairs.clear()
                    hedge_store.dirty = True
                    brain.dirty = True
                    brain.save_brain(force=True)
                    hedge_store.save(force=True)

                current_mode, new_entries_allowed, recovery_mode, recovery_anchor_equity, exited_recovery = evaluate_system_mode(
                    now, pause_until, ind_5m, daily_pnl_pct, current_dd_pct,
                    floor_threat, equity, recovery_anchor_equity
                )
                if Config.EXPLORER_MODE:
                    new_entries_allowed = True

                mods = brain.get_parallel_modifiers(ind_5m, ind_15m, ind_1h, ind_4h)
                latency_ms = int(round((time.perf_counter() - cycle_t0) * 1000.0))
                client.telemetry.record("cycle_ms", latency_ms)
                lat_tag = client.telemetry.format_compact_tag()
                hedge_tag = f"|H:{len(hedge_running)}" if len(hedge_running) > 0 else ""
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}][{Config.ENV_MODE[:4].upper()}:{current_mode}][{state}][Reg:{mkt_reg_tag}]"
                    f"[1h:{mods['name_1h'][:4]}|4h:{mods['name_4h'][:4]}|OVR:{ovr_tag}] "
                    f"${ind_5m['price']:.1f} | Bal:{balance:,} ({goal_pct:.1f}% Meta) | Eq:{equity:,} | "
                    f"Act:{len(primary_running)}/{Config.MAX_CONCURRENT_TRADES}{hedge_tag} | DD:{current_dd_pct*100:.1f}% | {latency_ms}ms [{lat_tag}]"
                )

                account_state = {
                    "equity_sats": equity,
                    "balance_sats": balance,
                    "locked_margin_sats": locked_margin,
                    "locked_floor_sats": brain.locked_floor,
                    "hard_floor_breach": floor_threat,
                    "current_dd_pct": current_dd_pct,
                }
                still_running = manage_open_positions_and_hedges(
                    client, brain, hedge_store, portfolio_state,
                    primary_running, ind_5m, ind_15m, ind_1h,
                    state, current_mode, edge_scores_by_tid, perception,
                    q_vec, account_state, now, safe_mode=safe_mode
                )

                active_count = max(len(still_running), len(brain.active_trades))
                if recovery_mode and current_dd_pct >= 0.08:
                    dynamic_max_pos = 1
                elif Config.EXPLORER_MODE:
                    dynamic_max_pos = min(Config.MAX_CONCURRENT_TRADES, max(1, int((equity * 0.85) // 1000)))
                else:
                    capital_max_pos = max(1, min(Config.MAX_CONCURRENT_TRADES, int(equity // 10000)))
                    floor_headroom_pct = (equity - brain.locked_floor) / max(equity, 1) if brain.locked_floor > 0 else 1.0
                    if floor_headroom_pct <= 0.06 or mkt_reg_tag == "EXTREME_VOLATILITY":
                        dynamic_max_pos = 1
                    elif recovery_mode:
                        dynamic_max_pos = min(capital_max_pos, 1 if (equity < 10000 or current_dd_pct >= 0.08) else 2)
                    elif "CHOP" in state and perception.get("override_side") is None:
                        dynamic_max_pos = min(capital_max_pos, 1 if equity < 6000 else 2)
                    else:
                        dynamic_max_pos = capital_max_pos

                _lim_cfg_loop = LimitExecutionConfig.from_thresholds(THRESHOLDS_MANAGER)
                _lim_rec = limit_mgr.reconcile_open_orders(
                    client=client,
                    running_trades=still_running,
                    spot_price=float(ind_5m["price"]),
                    perception=perception,
                    cfg=_lim_cfg_loop,
                    force_cancel_all=bool(safe_mode or floor_threat),
                    now=now,
                )
                _still_ids = {str(x.get("id")) for x in still_running if isinstance(x, dict) and x.get("id")}
                for _fev in _lim_rec.get("filled", []):
                    _ft = _fev["trade"]
                    _fm = _fev["meta"]
                    _ftid = str(_ft.get("id") or "")
                    if _ftid:
                        _am = infer_adopted_trade(
                            _ft, ind_5m, _fm.get("state_key") or state, now,
                            forced_action=int(_fm.get("action") or 1),
                        )
                        _am["entry_mode"] = "LIMIT"
                        _am["decision_price"] = float(_fm.get("limit_price") or ind_5m["price"])
                        _am["entry_q"] = float(_fm.get("entry_q", 0.0))
                        _am["is_exploratory"] = bool(_fm.get("is_exploratory", False))
                        _am["liq_score"] = float(_fm.get("liq_score", 0.0))
                        _am["entry_time"] = float(now)
                        _am["opened_at"] = float(now)
                        brain.active_trades[_ftid] = _am
                        brain.dirty = True
                        if _ftid not in _still_ids:
                            still_running.append(_ft)
                            _still_ids.add(_ftid)
                        print(f"  ✅ [LÍMITE LLENADA] {_fm.get('style')} id={_ftid[:8]} @ ${_ft.get('price', _fm.get('limit_price'))}")
                for _cev in _lim_rec.get("cancelled", []):
                    print(f"  🧹 [LÍMITE CANCELADA] id={str(_cev.get('order_id',''))[:8]} | Motivo: {_cev.get('reason')}")

                _eff_avail_balance = max(0, int(balance) - limit_mgr.reserved_margin_sats())
                _eff_active_count = max(len(still_running), len(brain.active_trades)) + limit_mgr.active_open_count()

                if new_entries_allowed and _eff_active_count < dynamic_max_pos:
                    _action_taken, last_entry_time = evaluate_and_dispatch_entries(
                        client=client,
                        brain=brain,
                        state=state,
                        ind_5m=ind_5m,
                        ind_15m=ind_15m,
                        ind_1h=ind_1h,
                        ind_4h=ind_4h,
                        mods=mods,
                        recovery_mode=recovery_mode,
                        current_dd_pct=current_dd_pct,
                        dynamic_max_pos=dynamic_max_pos,
                        active_count=max(len(still_running), len(brain.active_trades)),
                        locked_margin=locked_margin + limit_mgr.reserved_margin_sats(),
                        equity=equity,
                        balance=_eff_avail_balance,
                        last_entry_time=last_entry_time,
                        now=now,
                        consecutive_losses=consecutive_losses,
                        warmup=warmup,
                        risk_multiplier=risk_multiplier,
                        sprint_mode=sprint_mode,
                        mkt_reg_tag=mkt_reg_tag,
                        still_running=still_running,
                        safe_mode=safe_mode,
                        hedge_store=hedge_store,
                        perception=perception,
                        shadow_mode=Config.SHADOW_MODE,
                        ind_1m=ind_1m,
                        ind_1d=ind_1d,
                        limit_mgr=limit_mgr,
                        side_brake=side_brake,
                    )

                _liq_sig_tel = perception.get("liquidity") if isinstance(perception, dict) else {}
                _liq_cfg_tel = LiquidityConfig.from_thresholds(THRESHOLDS_MANAGER)
                TELEMETRY_DISPATCHER.publish(
                    balance_sats=balance,
                    equity_sats=equity,
                    available_sats=_eff_avail_balance,
                    active_trades=len(still_running) if still_running else 0,
                    supervisor=brain.supervisor,
                    extra_meta={
                        "architecture_profile": "C1_STREAK3_24H_INV",
                        "deterministic_policy_mode": bool(getattr(Config, "DETERMINISTIC_TREND_POLICY", True)),
                        "side_streak_brake": side_brake.get_telemetry_snapshot(now=now),
                        "liquidity_radar": {
                            "enabled": bool(_liq_cfg_tel.enabled),
                            "shadow_only": bool(_liq_cfg_tel.shadow_only),
                            "opposite_wall_enabled": bool(_liq_cfg_tel.opposite_wall_enabled),
                            "score": float(_liq_sig_tel.get("liquidity_score", 0.0)),
                            "event_type": str(_liq_sig_tel.get("event_type", "NONE")),
                            "confirmation": str(_liq_sig_tel.get("confirmation", "NONE")),
                            "timeframe": str(_liq_sig_tel.get("timeframe", "NONE")),
                            "swept_level": float(_liq_sig_tel.get("swept_level", 0.0)),
                            "nearest_high_pool": float(_liq_sig_tel.get("nearest_high_pool", 0.0)),
                            "nearest_low_pool": float(_liq_sig_tel.get("nearest_low_pool", 0.0)),
                            "active_high_pools": int(_liq_sig_tel.get("active_high_pools", 0)),
                            "active_low_pools": int(_liq_sig_tel.get("active_low_pools", 0)),
                        },
                        "limit_execution": {
                            "enabled": bool(_lim_cfg_loop.enabled),
                            "open_count": int(limit_mgr.active_open_count()),
                            "reserved_margin_sats": int(limit_mgr.reserved_margin_sats()),
                            "stats": dict(limit_mgr.stats),
                        },
                        "binance_micro": _build_binance_micro_telemetry(
                            lnm_price or ind_5m.get("price", 0.0),
                            now=now,
                        ),
                    },
                    force=bool(closed_ids),
                )
                brain.save_brain(force=False)
                hedge_store.save(force=False)
            except RuntimeError as api_err:
                api_health.record_error("API/RED", api_err)
            except Exception as e:
                api_health.record_error(type(e).__name__, e)

            sys.stdout.flush()
            if once_mode or stop_state["requested"]:
                break

            # Frecuencia operativa adaptativa según calidad de datos, latencia, posiciones y volatilidad (Fase 1.4)
            lat_deg, _ = client.telemetry.evaluate_execution_health()
            if lat_deg:
                loop_wait = min(14.0, Config.IDLE_ANALYZE_INTERVAL_SEC * 1.25)
            elif PERCEPTION_ENGINE.override_side is not None:
                loop_wait = Config.FAST_ANALYZE_INTERVAL_SEC * 0.85
            elif len(brain.active_trades) > 0:
                loop_wait = Config.ACTIVE_ANALYZE_INTERVAL_SEC * 0.85
            else:
                loop_wait = Config.IDLE_ANALYZE_INTERVAL_SEC
            sleep_rem = max(1.0, loop_wait)
            while sleep_rem > 0.0 and not stop_state["requested"]:
                step_s = min(0.25, sleep_rem)
                time.sleep(step_s)
                sleep_rem -= step_s

    finally:
        if stop_state["requested"]:
            print(f"\n[SEÑAL RECIBIDA ({stop_state['sig']})] Cancelando límites pendientes y guardando estado atómicamente...")
        try:
            if limit_mgr.active_open_count() > 0:
                limit_mgr.cancel_all_for_shutdown(client)
        except Exception as exc:
            print(f"[AVISO CIERRE LÍMITES] {exc}")
        try:
            brain.save_brain(force=True)
        except Exception as exc:
            print(f"[AVISO CIERRE CEREBRO] {exc}")
        try:
            hedge_store.save(force=True)
        except Exception as exc:
            print(f"[AVISO CIERRE HEDGE] {exc}")
        try:
            side_brake.save()
        except Exception as exc:
            print(f"[AVISO CIERRE SIDE-BRAKE] {exc}")
        try:
            if pid_path and os.path.exists(pid_path):
                with open(pid_path, "r", encoding="utf-8") as pfh:
                    if pfh.read().strip() == str(os.getpid()):
                        os.remove(pid_path)
        except Exception:
            pass
        sys.stdout.flush()


if __name__ == "__main__":
    main()
