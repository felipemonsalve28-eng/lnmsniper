# -*- coding: utf-8 -*-
"""
simulation/sim_market_feed.py
Alimentador de datos de mercado multitemporal (MTF) de alta fidelidad.
Ingesta series de velas históricas en 1m y construye matemáticamente los marcos
de 5m, 15m, 1h y 4h, calculando indicadores idénticos a los del bot de producción
mediante compute_indicators() y MarketRegimeClassifier.
"""
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Generator, Any
import collections
import json
import math
import requests

from guardian_execution_core import compat_np as np
from lnm_sniper_v6_advanced import compute_indicators
from sniper_perception_sats_core import MarketRegimeClassifier, ShortTermPerceptionEngine


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_binance_row(cls, row: list) -> "Candle":
        """Convierte una fila de la API de Binance [open_time, o, h, l, c, v, ...] a Candle."""
        return cls(
            timestamp=float(row[0]) / 1000.0,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Candle":
        return cls(
            timestamp=float(d.get("timestamp", d.get("time", 0.0))),
            open=float(d["open"]),
            high=float(d["high"]),
            low=float(d["low"]),
            close=float(d["close"]),
            volume=float(d.get("volume", 0.0)),
        )


class MTFAggregator:
    """
    Agregador multitemporal determinista.
    Recibe velas de 1m y mantiene buffers rodantes de velas completas
    en 5m, 15m, 1h y 4h, permitiendo consultar indicadores instantáneos.
    """
    TIMEFRAMES = {
        "5m": 5 * 60,
        "15m": 15 * 60,
        "1h": 60 * 60,
        "4h": 4 * 60 * 60,
    }
    MAX_HISTORY = 250  # Suficiente para EMA200 y SMA50

    def __init__(self):
        self.history_1m: collections.deque = collections.deque(maxlen=self.MAX_HISTORY)
        self.history_5m: collections.deque = collections.deque(maxlen=self.MAX_HISTORY)
        self.history_15m: collections.deque = collections.deque(maxlen=self.MAX_HISTORY)
        self.history_1h: collections.deque = collections.deque(maxlen=self.MAX_HISTORY)
        self.history_4h: collections.deque = collections.deque(maxlen=self.MAX_HISTORY)

        # Velas en construcción
        self.current_bar: Dict[str, Optional[Candle]] = {tf: None for tf in self.TIMEFRAMES}
        self.current_bar_start: Dict[str, float] = {tf: 0.0 for tf in self.TIMEFRAMES}

    def push_1m_candle(self, c: Candle) -> None:
        """Incorpora una nueva vela de 1m y actualiza los agregados superiores."""
        self.history_1m.append(c)

        for tf, duration in self.TIMEFRAMES.items():
            bucket_start = (c.timestamp // duration) * duration
            cur = self.current_bar[tf]

            if cur is None or bucket_start > self.current_bar_start[tf]:
                # Si existía una barra previa y cambió el bucket, cerrarla y archivarla
                if cur is not None:
                    self._get_history(tf).append(cur)
                # Iniciar nueva barra
                self.current_bar[tf] = Candle(
                    timestamp=bucket_start,
                    open=c.open,
                    high=c.high,
                    low=c.low,
                    close=c.close,
                    volume=c.volume,
                )
                self.current_bar_start[tf] = bucket_start
            else:
                # Actualizar barra en progreso
                cur.high = max(cur.high, c.high)
                cur.low = min(cur.low, c.low)
                cur.close = c.close
                cur.volume += c.volume

    def _get_history(self, tf: str) -> collections.deque:
        if tf == "5m":
            return self.history_5m
        elif tf == "15m":
            return self.history_15m
        elif tf == "1h":
            return self.history_1h
        elif tf == "4h":
            return self.history_4h
        return self.history_1m

    def get_arrays(self, tf: str) -> Tuple[Any, Any, Any]:
        """Extrae arrays NumPy (highs, lows, closes) incluyendo la vela en curso."""
        hist = list(self._get_history(tf))
        cur = self.current_bar.get(tf)
        if cur is not None:
            hist.append(cur)

        if not hist:
            return np.zeros(1), np.zeros(1), np.zeros(1)

        highs = np.array([c.high for c in hist], dtype=np.float64)
        lows = np.array([c.low for c in hist], dtype=np.float64)
        closes = np.array([c.close for c in hist], dtype=np.float64)
        return highs, lows, closes


class SimMarketFeed:
    """
    Motor integral de alimentación de mercado para simulación.
    Carga datos históricos, transmite tick/vela secuencialmente y computa
    indicadores multitemporales y regímenes en cada paso con rigor O(1).
    """

    def __init__(self, candles_1m: Optional[List[Candle]] = None):
        self.candles_1m: List[Candle] = candles_1m or []
        self.cursor: int = 0
        self.aggregator = MTFAggregator()
        self.last_candle: Optional[Candle] = None
        self.perception_engine = ShortTermPerceptionEngine()

    def load_candles(self, candles: List[Candle]) -> None:
        self.candles_1m = candles
        self.cursor = 0
        self.aggregator = MTFAggregator()
        self.last_candle = None
        self.perception_engine = ShortTermPerceptionEngine()

    def warmup(self, warmup_bars: int = 150) -> int:
        """
        Pre-carga las primeras N velas de 1m para calentar los agregados MTF
        y calcular EMAs sin saltos iniciales.
        """
        bars_to_warm = min(warmup_bars, len(self.candles_1m))
        for _ in range(bars_to_warm):
            c = self.candles_1m[self.cursor]
            self.aggregator.push_1m_candle(c)
            self.last_candle = c
            self.cursor += 1
        return bars_to_warm

    def has_next(self) -> bool:
        return self.cursor < len(self.candles_1m)

    def next_step(self) -> Optional[Candle]:
        """Avanza una vela de 1m y actualiza los indicadores."""
        if not self.has_next():
            return None
        c = self.candles_1m[self.cursor]
        self.cursor += 1
        self.aggregator.push_1m_candle(c)
        self.last_candle = c
        return c

    def get_indicators(self, tf: str) -> dict:
        """
        Calcula y retorna el diccionario de indicadores exacto para el timeframe tf.
        Reutiliza compute_indicators() de lnm_sniper_v6_advanced.py.
        """
        highs, lows, closes = self.aggregator.get_arrays(tf)
        ref_price = float(self.last_candle.close if self.last_candle else closes[-1])
        return compute_indicators(highs, lows, closes, lnm_price=ref_price)

    def get_perception_state(self) -> dict:
        """
        Retorna la tupla completa requerida por el core del bot:
        ind_5m, ind_15m, ind_1h, ind_4h, perception y market_regime.
        """
        ind_5m = self.get_indicators("5m")
        ind_15m = self.get_indicators("15m")
        ind_1h = self.get_indicators("1h")
        ind_4h = self.get_indicators("4h")

        now_ts = float(self.last_candle.timestamp if self.last_candle else 0.0)
        reg_info = MarketRegimeClassifier.classify(ind_5m, ind_15m, ind_1h)
        perception = self.perception_engine.evaluate(
            None, ind_5m, ind_15m, ind_1h, ind_4h, now=now_ts
        )
        return {
            "price": float(ind_5m["price"]),
            "ind_5m": ind_5m,
            "ind_15m": ind_15m,
            "ind_1h": ind_1h,
            "ind_4h": ind_4h,
            "regime_info": reg_info,
            "perception": perception,
            "timestamp": now_ts,
        }

    @staticmethod
    def fetch_binance_klines(symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 1000, start_time: Optional[int] = None) -> List[Candle]:
        """Descarga velas reales directamente desde el endpoint público de Binance (sin API key requerida)."""
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if start_time:
            url += f"&startTime={int(start_time)}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        return [Candle.from_binance_row(r) for r in raw]
