#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
LNM SNIPER — GESTOR DINÁMICO Y ATÓMICO DE UMBRALES DE APRENDIZAJE
================================================================================
Fuente única de verdad para todos los parámetros operativos, de expectativas,
comisiones, función de recompensa, calibración Q, entropía, MFE/MAE y
protección del aprendizaje (config/learning_thresholds.json).

Características:
  1. Validación estricta de esquema (10 categorías formales y 5 campos por
     parámetro: value, unit, description, min_limit, max_limit).
  2. Recarga atómica en caliente (Hot Reload) por modificación de archivo
     (mtime) o señal del sistema (SIGHUP en Linux/Termux) sin reiniciar el bot.
  3. Rechazo seguro ante sintaxis corrupta o valores fuera de rango
     [min_limit, max_limit], preservando el último estado válido conocido.
  4. Bloqueo de parámetros estructurales críticos cuando existen posiciones
     abiertas (active_trades_count > 0).
  5. Cero dependencias pesadas (100% compatible con Linux/Ubuntu y Termux).
================================================================================
"""

from __future__ import annotations

import copy
import json
import logging
import os
import signal
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("SniperThresholds")

BASE_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
DEFAULT_CONFIG_PATH = os.path.join(BASE_CONFIG_DIR, "learning_thresholds.json")
TESTNET_CONFIG_PATH = os.path.join(BASE_CONFIG_DIR, "learning_thresholds_testnet.json")


def resolve_thresholds_path_for_env(env_mode: Optional[str] = None) -> str:
    env = str(env_mode or os.getenv("ENV_MODE") or os.getenv("SNIPER_ENV") or "mainnet").strip().lower()
    if env in ("testnet", "test", "explorer"):
        if not os.path.exists(TESTNET_CONFIG_PATH):
            try:
                os.makedirs(BASE_CONFIG_DIR, exist_ok=True)
                if os.path.exists(DEFAULT_CONFIG_PATH):
                    import shutil
                    shutil.copy2(DEFAULT_CONFIG_PATH, TESTNET_CONFIG_PATH)
            except Exception:
                pass
        return TESTNET_CONFIG_PATH
    return DEFAULT_CONFIG_PATH

REQUIRED_CATEGORIES: Tuple[str, ...] = (
    "expectancy",
    "fees",
    "td_error",
    "q_calibration",
    "policy_entropy",
    "mfe_mae",
    "reward",
    "exploration",
    "learning_guard",
    "market_sensitivity",
    "liquidity_radar",
    "limit_execution",
)

REQUIRED_PARAM_FIELDS: Tuple[str, ...] = (
    "value",
    "unit",
    "description",
    "min_limit",
    "max_limit",
)

# Parámetros estructurales que no deben mutar en caliente con posiciones abiertas
STRUCTURAL_LOCKED_PARAMS: Tuple[Tuple[str, str], ...] = (
    ("fees", "taker_fee_rate"),
    ("fees", "round_trip_fee_rate"),
    ("q_calibration", "q_to_sats_scale"),
    ("q_calibration", "q_clip_min"),
    ("q_calibration", "q_clip_max"),
    ("policy_entropy", "num_actions"),
    ("reward", "reward_scale_sats"),
    ("reward", "decouple_floating_pnl"),
    ("exploration", "gamma_discount"),
    ("liquidity_radar", "swing_k_5m"),
    ("liquidity_radar", "swing_k_15m"),
    ("limit_execution", "enabled"),
    ("limit_execution", "tick_size_usd"),
)

FALLBACK_THRESHOLDS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "expectancy": {
        "min_expectancy_ratio": {
            "value": 3.85,
            "unit": "ratio",
            "description": "Ratio minimo de beneficio bruto proyectado sobre costos totales (comisiones + funding + slippage) para autorizar entrada en modo normal.",
            "min_limit": 1.1,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "min_expectancy_ratio_explorer": {
            "value": 2.4,
            "unit": "ratio",
            "description": "Ratio minimo de beneficio bruto sobre costos totales en modo explorador.",
            "min_limit": 1.05,
            "max_limit": 8.0,
            "structural_lock_when_open": False,
        },
        "min_net_to_cost_ratio": {
            "value": 2.8,
            "unit": "ratio",
            "description": "Multiplo minimo de ganancia neta en satoshis respecto al costo total de friccion en modo normal.",
            "min_limit": 0.5,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "min_net_to_cost_ratio_explorer": {
            "value": 1.5,
            "unit": "ratio",
            "description": "Multiplo minimo de ganancia neta en satoshis respecto al costo total de friccion en modo explorador.",
            "min_limit": 0.2,
            "max_limit": 8.0,
            "structural_lock_when_open": False,
        },
        "min_net_profit_sats": {
            "value": 38,
            "unit": "sats",
            "description": "Piso absoluto de ganancia neta proyectada en satoshis para abrir posicion en modo normal.",
            "min_limit": 1,
            "max_limit": 5000,
            "structural_lock_when_open": False,
        },
        "min_net_profit_sats_explorer": {
            "value": 16,
            "unit": "sats",
            "description": "Piso absoluto de ganancia neta proyectada en satoshis para abrir posicion en modo explorador.",
            "min_limit": 1,
            "max_limit": 2000,
            "structural_lock_when_open": False,
        },
        "min_net_profit_sats_recovery": {
            "value": 44,
            "unit": "sats",
            "description": "Piso absoluto de ganancia neta proyectada en satoshis durante modo recuperacion.",
            "min_limit": 5,
            "max_limit": 5000,
            "structural_lock_when_open": False,
        },
        "min_net_rr": {
            "value": 1.05,
            "unit": "ratio",
            "description": "Relacion Riesgo-Beneficio neta minima post-comisiones en modo normal.",
            "min_limit": 0.5,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "min_net_rr_explorer": {
            "value": 0.72,
            "unit": "ratio",
            "description": "Relacion Riesgo-Beneficio neta minima post-comisiones en modo explorador.",
            "min_limit": 0.3,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
        "breakeven_safety_margin": {
            "value": 0.04,
            "unit": "probability",
            "description": "Margen de seguridad probabilistico exigido sobre la tasa de equilibrio p* (p_LCB >= p* + margen).",
            "min_limit": 0.0,
            "max_limit": 0.25,
            "structural_lock_when_open": False,
        },
        "bayesian_prior_alpha": {
            "value": 9.0,
            "unit": "count",
            "description": "Hiperparametro alpha_0 (pseudo-aciertos iniciales) del prior Beta-Binomial.",
            "min_limit": 1.0,
            "max_limit": 100.0,
            "structural_lock_when_open": False,
        },
        "bayesian_prior_beta": {
            "value": 11.0,
            "unit": "count",
            "description": "Hiperparametro beta_0 (pseudo-fracasos iniciales) del prior Beta-Binomial.",
            "min_limit": 1.0,
            "max_limit": 100.0,
            "structural_lock_when_open": False,
        },
    },
    "fees": {
        "taker_fee_rate": {
            "value": 0.001,
            "unit": "rate",
            "description": "Tasa de comision taker por lado en LN Markets (0.10%).",
            "min_limit": 0.0001,
            "max_limit": 0.01,
            "structural_lock_when_open": True,
        },
        "round_trip_fee_rate": {
            "value": 0.002,
            "unit": "rate",
            "description": "Tasa de comision round-trip apertura + cierre en LN Markets (0.20%).",
            "min_limit": 0.0002,
            "max_limit": 0.02,
            "structural_lock_when_open": True,
        },
        "funding_rate_8h_est": {
            "value": 0.0001,
            "unit": "rate",
            "description": "Tasa estimada de funding/carry por ventana de 8 horas en LN Markets.",
            "min_limit": 0.0,
            "max_limit": 0.01,
            "structural_lock_when_open": False,
        },
        "fer_high_threshold": {
            "value": 2.5,
            "unit": "ratio",
            "description": "Umbral de Fee-Efficiency Ratio (FER >= 2.5) para clasificar eficiencia alta.",
            "min_limit": 1.2,
            "max_limit": 20.0,
            "structural_lock_when_open": False,
        },
        "fer_moderate_threshold": {
            "value": 1.2,
            "unit": "ratio",
            "description": "Umbral inferior de FER (1.2 <= FER < 2.5) para eficiencia moderada; debajo activa respuesta contra fee drag.",
            "min_limit": 0.5,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "fer_warning_threshold": {
            "value": 1.0,
            "unit": "ratio",
            "description": "Umbral critico de FER (FER < 1.0 indica destruccion de capital por friccion operativa).",
            "min_limit": 0.1,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
        "fer_window_size": {
            "value": 20,
            "unit": "trades",
            "description": "Ventana movil de operaciones cerradas para calculo de Fee-Efficiency Ratio.",
            "min_limit": 5,
            "max_limit": 500,
            "structural_lock_when_open": False,
        },
    },
    "td_error": {
        "window_size": {
            "value": 50,
            "unit": "updates",
            "description": "Ventana movil de actualizaciones de Q-table para media movil y gradiente de TD-Error.",
            "min_limit": 5,
            "max_limit": 500,
            "structural_lock_when_open": False,
        },
        "warmup_min_samples": {
            "value": 5,
            "unit": "updates",
            "description": "Minimo de actualizaciones antes de emitir clasificacion de estado TD-Error fuera de WARMING_UP.",
            "min_limit": 2,
            "max_limit": 100,
            "structural_lock_when_open": False,
        },
        "stable_threshold": {
            "value": 0.45,
            "unit": "q_units",
            "description": "Umbral superior de media movil |delta_t| para considerar convergencia STABLE.",
            "min_limit": 0.05,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
        "warning_threshold": {
            "value": 0.85,
            "unit": "q_units",
            "description": "Umbral de |delta_t| que dispara clasificacion WARNING por incremento sostenido.",
            "min_limit": 0.1,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "critical_threshold": {
            "value": 1.35,
            "unit": "q_units",
            "description": "Umbral de divergencia severa |delta_t| que clasifica el update como CRITICAL.",
            "min_limit": 0.2,
            "max_limit": 20.0,
            "structural_lock_when_open": False,
        },
        "improving_slope_threshold": {
            "value": -0.005,
            "unit": "q_per_update",
            "description": "Pendiente negativa maxima del gradiente de TD-Error para clasificar estado IMPROVING.",
            "min_limit": -5.0,
            "max_limit": 0.0,
            "structural_lock_when_open": False,
        },
        "diverging_slope_threshold": {
            "value": 0.015,
            "unit": "q_per_update",
            "description": "Pendiente positiva del gradiente de TD-Error que alerta divergencia progresiva.",
            "min_limit": 0.0001,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
    },
    "q_calibration": {
        "window_size": {
            "value": 20,
            "unit": "trades",
            "description": "Ventana movil de operaciones cerradas para calculo de Q-Bias en satoshis.",
            "min_limit": 5,
            "max_limit": 200,
            "structural_lock_when_open": False,
        },
        "warmup_min_samples": {
            "value": 3,
            "unit": "trades",
            "description": "Minimo de operaciones cerradas requeridas para evaluar sesgo Q.",
            "min_limit": 1,
            "max_limit": 50,
            "structural_lock_when_open": False,
        },
        "q_to_sats_scale": {
            "value": 100.0,
            "unit": "sats_per_q",
            "description": "Factor de conversion inversa de valor Q a satoshis proyectados (Q_entry_sats = Q * scale).",
            "min_limit": 1.0,
            "max_limit": 10000.0,
            "structural_lock_when_open": True,
        },
        "overestimation_alert_sats": {
            "value": 25.0,
            "unit": "sats",
            "description": "Umbral positivo de Q-Bias en satoshis que diagnostica sobreestimacion sistematica.",
            "min_limit": 1.0,
            "max_limit": 5000.0,
            "structural_lock_when_open": False,
        },
        "underestimation_alert_sats": {
            "value": -25.0,
            "unit": "sats",
            "description": "Umbral negativo de Q-Bias en satoshis que diagnostica subestimacion excesiva.",
            "min_limit": -5000.0,
            "max_limit": -1.0,
            "structural_lock_when_open": False,
        },
        "q_clip_min": {
            "value": -2.0,
            "unit": "q_units",
            "description": "Limite inferior de valores en la Q-table para evitar explosion numerica.",
            "min_limit": -20.0,
            "max_limit": -0.1,
            "structural_lock_when_open": True,
        },
        "q_clip_max": {
            "value": 2.0,
            "unit": "q_units",
            "description": "Limite superior de valores en la Q-table para evitar sobreestimacion descontrolada.",
            "min_limit": 0.1,
            "max_limit": 20.0,
            "structural_lock_when_open": True,
        },
        "fractional_kelly_scale": {
            "value": 0.25,
            "unit": "fraction",
            "description": "Fraccion del criterio de Kelly aplicada al dimensionamiento dinamico (0.25 = 1/4 Kelly).",
            "min_limit": 0.05,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
    },
    "policy_entropy": {
        "window_size": {
            "value": 100,
            "unit": "decisions",
            "description": "Ventana movil de decisiones consecutivas para evaluar entropia de la politica.",
            "min_limit": 10,
            "max_limit": 1000,
            "structural_lock_when_open": False,
        },
        "warmup_min_samples": {
            "value": 10,
            "unit": "decisions",
            "description": "Minimo de decisiones en ventana antes de evaluar colapso de entropia.",
            "min_limit": 5,
            "max_limit": 200,
            "structural_lock_when_open": False,
        },
        "num_actions": {
            "value": 9,
            "unit": "actions",
            "description": "Numero de acciones discretas del agente Q-Learning (0: HOLD, 1..4: LONG, 5..8: SHORT).",
            "min_limit": 9,
            "max_limit": 9,
            "structural_lock_when_open": True,
        },
        "min_normalized_entropy": {
            "value": 0.25,
            "unit": "ratio",
            "description": "Entropia normalizada minima [0, 1] debajo de la cual se diagnostica colapso de politica.",
            "min_limit": 0.01,
            "max_limit": 0.95,
            "structural_lock_when_open": False,
        },
        "healthy_normalized_entropy": {
            "value": 0.45,
            "unit": "ratio",
            "description": "Entropia normalizada objetivo para una politica sana y equilibrada.",
            "min_limit": 0.1,
            "max_limit": 0.99,
            "structural_lock_when_open": False,
        },
        "max_dominant_action_freq": {
            "value": 0.85,
            "unit": "ratio",
            "description": "Frecuencia relativa maxima de una sola accion antes de diagnosticar monopolio o paralisis.",
            "min_limit": 0.3,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
    },
    "mfe_mae": {
        "window_size": {
            "value": 20,
            "unit": "trades",
            "description": "Ventana movil de operaciones cerradas para promediar ratios MFE y MAE.",
            "min_limit": 3,
            "max_limit": 200,
            "structural_lock_when_open": False,
        },
        "target_mfe_capture_ratio": {
            "value": 0.65,
            "unit": "ratio",
            "description": "Objetivo de referencia de captura de MFE (Realized / MFE >= 60-70%).",
            "min_limit": 0.2,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "min_mfe_capture_warn": {
            "value": 0.4,
            "unit": "ratio",
            "description": "Umbral inferior de captura de MFE (< 40%) que dispara ajuste de trailing stop por cierre prematuro.",
            "min_limit": 0.05,
            "max_limit": 0.9,
            "structural_lock_when_open": False,
        },
        "max_mae_margin_ratio_warn": {
            "value": 0.5,
            "unit": "ratio",
            "description": "Umbral maximo aceptable de MAE respecto al margen asignado (MAE / Margin < 50%).",
            "min_limit": 0.05,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "trailing_tighten_factor": {
            "value": 0.85,
            "unit": "multiplier",
            "description": "Factor multiplicador aplicado al trailing stop cuando la captura MFE es baja o el MAE excede umbral.",
            "min_limit": 0.4,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
    },
    "reward": {
        "mae_penalty_lambda": {
            "value": 0.15,
            "unit": "weight",
            "description": "Coeficiente normalizado lambda de penalizacion por Maximum Adverse Excursion en satoshis (MAE_sats).",
            "min_limit": 0.0,
            "max_limit": 2.0,
            "structural_lock_when_open": False,
        },
        "holding_time_penalty_mu": {
            "value": 0.05,
            "unit": "sats_per_tick",
            "description": "Coeficiente normalizado mu de coste de oportunidad por tiempo de capital inmovilizado (delta_t_ticks).",
            "min_limit": 0.0,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "tick_duration_sec": {
            "value": 15.0,
            "unit": "seconds",
            "description": "Duracion base de un tick en segundos para convertir segundos de tenencia a delta_t_ticks.",
            "min_limit": 1.0,
            "max_limit": 300.0,
            "structural_lock_when_open": False,
        },
        "reward_scale_sats": {
            "value": 100.0,
            "unit": "sats",
            "description": "Escala de normalizacion en satoshis para transformar recompensa economica liquidada a unidades Q.",
            "min_limit": 1.0,
            "max_limit": 10000.0,
            "structural_lock_when_open": True,
        },
        "loss_penalty_multiplier": {
            "value": 1.0,
            "unit": "multiplier",
            "description": "Multiplicador de severidad sobre perdidas reales en balance para asegurar gradiente Q negativo.",
            "min_limit": 1.0,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
        "decouple_floating_pnl": {
            "value": 1,
            "unit": "flag",
            "description": "Bandera (1=activo, 0=inactivo) que prohibe actualizar la Q-table con PnL flotante no consolidado.",
            "min_limit": 0,
            "max_limit": 1,
            "structural_lock_when_open": True,
        },
    },
    "exploration": {
        "epsilon_initial": {
            "value": 0.06,
            "unit": "probability",
            "description": "Probabilidad inicial de exploracion epsilon en modo estandar.",
            "min_limit": 0.0,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "epsilon_min": {
            "value": 0.015,
            "unit": "probability",
            "description": "Limite inferior de exploracion epsilon en modo estandar.",
            "min_limit": 0.0,
            "max_limit": 0.5,
            "structural_lock_when_open": False,
        },
        "epsilon_decay": {
            "value": 0.992,
            "unit": "multiplier",
            "description": "Factor multiplicador de decaimiento de epsilon tras cada transicion cerrada.",
            "min_limit": 0.8,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "epsilon_explorer_initial": {
            "value": 0.28,
            "unit": "probability",
            "description": "Probabilidad inicial de exploracion epsilon en modo explorador.",
            "min_limit": 0.01,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "epsilon_explorer_min": {
            "value": 0.08,
            "unit": "probability",
            "description": "Limite inferior de exploracion epsilon en modo explorador.",
            "min_limit": 0.01,
            "max_limit": 0.5,
            "structural_lock_when_open": False,
        },
        "alpha_learning_rate": {
            "value": 0.18,
            "unit": "rate",
            "description": "Tasa de aprendizaje alpha de Q-Learning en regimen normal.",
            "min_limit": 0.001,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "alpha_recovery_rate": {
            "value": 0.28,
            "unit": "rate",
            "description": "Tasa de aprendizaje alpha de Q-Learning durante modo recuperacion.",
            "min_limit": 0.001,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "gamma_discount": {
            "value": 0.92,
            "unit": "factor",
            "description": "Factor de descuento temporal gamma sobre el valor futuro max Q(s', a').",
            "min_limit": 0.1,
            "max_limit": 0.999,
            "structural_lock_when_open": True,
        },
        "mainnet_epsilon": {
            "value": 0.0,
            "unit": "probability",
            "description": "Probabilidad de exploracion aleatoria en Mainnet (0.0 = politica 100% determinista).",
            "min_limit": 0.0,
            "max_limit": 0.25,
            "structural_lock_when_open": False,
        },
        "fer_cooldown_mult": {
            "value": 2.0,
            "unit": "multiplier",
            "description": "Multiplicador de enfriamiento entre entradas cuando el FER movil cae debajo de 1.0.",
            "min_limit": 1.0,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
    },
    "learning_guard": {
        "critical_td_streak_to_suspend": {
            "value": 3,
            "unit": "updates",
            "description": "Numero de updates consecutivos con TD-Error CRITICAL para suspender actualizaciones online de la Q-table.",
            "min_limit": 1,
            "max_limit": 50,
            "structural_lock_when_open": False,
        },
        "hysteresis_recovery_updates": {
            "value": 5,
            "unit": "updates",
            "description": "Numero de updates estables consecutivos requeridos por histeresis antes de reactivar Q-updates.",
            "min_limit": 1,
            "max_limit": 100,
            "structural_lock_when_open": False,
        },
        "q_bias_persistence_trades": {
            "value": 3,
            "unit": "trades",
            "description": "Operaciones consecutivas con Q-Bias sobre umbral antes de activar penalizacion de expectancy.",
            "min_limit": 1,
            "max_limit": 50,
            "structural_lock_when_open": False,
        },
        "q_bias_expectancy_step": {
            "value": 0.35,
            "unit": "ratio_delta",
            "description": "Incremento aplicado al umbral de expectancy cuando persiste sobreestimacion de Q-Bias.",
            "min_limit": 0.05,
            "max_limit": 3.0,
            "structural_lock_when_open": False,
        },
        "fer_persistence_trades": {
            "value": 3,
            "unit": "trades",
            "description": "Operaciones consecutivas con FER < 1.2 antes de endurecer filtros contra fee drag.",
            "min_limit": 1,
            "max_limit": 50,
            "structural_lock_when_open": False,
        },
        "fer_expectancy_boost": {
            "value": 0.5,
            "unit": "ratio_delta",
            "description": "Incremento adicional al umbral de expectancy cuando FER < 1.2 en ventana movil.",
            "min_limit": 0.05,
            "max_limit": 4.0,
            "structural_lock_when_open": False,
        },
        "entropy_persistence_ticks": {
            "value": 10,
            "unit": "ticks",
            "description": "Decisiones consecutivas con entropia normalizada baja para confirmar colapso de politica.",
            "min_limit": 2,
            "max_limit": 200,
            "structural_lock_when_open": False,
        },
        "mfe_mae_persistence_trades": {
            "value": 3,
            "unit": "trades",
            "description": "Operaciones consecutivas con baja captura MFE o alto MAE antes de ajustar trailing stop.",
            "min_limit": 1,
            "max_limit": 50,
            "structural_lock_when_open": False,
        },
    },
    "market_sensitivity": {
        "atr_extreme_pct": {
            "value": 0.85,
            "unit": "percent",
            "description": "ATR porcentual a partir del cual se clasifica el mercado en volatilidad extrema.",
            "min_limit": 0.1,
            "max_limit": 10.0,
            "structural_lock_when_open": False,
        },
        "squeeze_bb_width_pct": {
            "value": 0.75,
            "unit": "percent",
            "description": "Ancho maximo de Bandas de Bollinger para identificar compresion (squeeze).",
            "min_limit": 0.05,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
        "expansion_atr_rel_ratio": {
            "value": 1.32,
            "unit": "ratio",
            "description": "Ratio minimo de ATR actual sobre media para confirmar expansion de volatilidad.",
            "min_limit": 0.5,
            "max_limit": 5.0,
            "structural_lock_when_open": False,
        },
        "max_slippage_bps_normal": {
            "value": 3.0,
            "unit": "bps",
            "description": "Slippage estimado en puntos basicos en condiciones normales de mercado.",
            "min_limit": 0.0,
            "max_limit": 50.0,
            "structural_lock_when_open": False,
        },
        "max_slippage_bps_volatile": {
            "value": 7.5,
            "unit": "bps",
            "description": "Slippage estimado en puntos basicos durante alta volatilidad o cascada.",
            "min_limit": 0.5,
            "max_limit": 100.0,
            "structural_lock_when_open": False,
        },
        "edge_degradation_warn": {
            "value": 0.4,
            "unit": "score",
            "description": "Puntaje de degradacion de edge para emitir alerta preventiva.",
            "min_limit": 0.1,
            "max_limit": 0.95,
            "structural_lock_when_open": False,
        },
        "edge_degradation_tighten": {
            "value": 0.6,
            "unit": "score",
            "description": "Puntaje de degradacion de edge para ajustar stops defensivos.",
            "min_limit": 0.2,
            "max_limit": 0.98,
            "structural_lock_when_open": False,
        },
        "edge_degradation_exit": {
            "value": 0.78,
            "unit": "score",
            "description": "Puntaje de degradacion de edge para ordenar salida tactica.",
            "min_limit": 0.3,
            "max_limit": 1.0,
            "structural_lock_when_open": False,
        },
        "edge_degradation_persistence": {
            "value": 3,
            "unit": "ticks",
            "description": "Ticks consecutivos requeridos para confirmar degradacion sostenida de edge.",
            "min_limit": 1,
            "max_limit": 30,
            "structural_lock_when_open": False,
        },
    },
    "liquidity_radar": {
        "enabled": {
            "value": 1,
            "unit": "flag",
            "description": "Activa el calculo del radar causal de liquidez Sweep+Reclaim en 5m/15m (1=si, 0=no).",
            "min_limit": 0,
            "max_limit": 1,
            "structural_lock_when_open": False,
        },
        "shadow_only": {
            "value": 0,
            "unit": "flag",
            "description": "Modo observacion del radar de liquidez: registra telemetria con liq_bias=0 y sin bloqueos (1=sombra, 0=activo).",
            "min_limit": 0,
            "max_limit": 1,
            "structural_lock_when_open": False,
        },
        "opposite_wall_enabled": {
            "value": 0,
            "unit": "flag",
            "description": "Habilita el veto BLOCKED_OPPOSITE_LIQ_WALL ante muros EQH/EQL opuestos (0=desactivado; siempre inactivo en TREND).",
            "min_limit": 0,
            "max_limit": 1,
            "structural_lock_when_open": False,
        },
        "swing_k_5m": {
            "value": 3,
            "unit": "bars",
            "description": "Orden causal k de pivotes Swing High/Low en velas de 5m (requiere k velas cerradas a ambos lados).",
            "min_limit": 2,
            "max_limit": 6,
            "structural_lock_when_open": True,
        },
        "swing_k_15m": {
            "value": 2,
            "unit": "bars",
            "description": "Orden causal k de pivotes Swing High/Low en velas de 15m.",
            "min_limit": 2,
            "max_limit": 5,
            "structural_lock_when_open": True,
        },
        "cluster_atr_mult": {
            "value": 0.25,
            "unit": "atr_mult",
            "description": "Tolerancia de agrupacion de pivotes en clusters EQH/EQL como fraccion de ATR.",
            "min_limit": 0.10,
            "max_limit": 0.50,
            "structural_lock_when_open": False,
        },
        "mitigation_atr_mult": {
            "value": 0.35,
            "unit": "atr_mult",
            "description": "Penetracion minima al cierre (en ATR) para marcar un pool de liquidez como mitigado.",
            "min_limit": 0.10,
            "max_limit": 0.80,
            "structural_lock_when_open": False,
        },
        "min_pierce_atr": {
            "value": 0.10,
            "unit": "atr_mult",
            "description": "Penetracion minima de mecha sobre el pool (en ATR) para validar una barrida de liquidez.",
            "min_limit": 0.05,
            "max_limit": 0.40,
            "structural_lock_when_open": False,
        },
        "max_pierce_atr": {
            "value": 1.20,
            "unit": "atr_mult",
            "description": "Penetracion maxima de mecha (en ATR) para evitar atrapar rupturas violentas (cuchillos).",
            "min_limit": 0.60,
            "max_limit": 2.00,
            "structural_lock_when_open": False,
        },
        "min_wick_ratio_close": {
            "value": 0.45,
            "unit": "ratio",
            "description": "Proporcion minima de mecha de rechazo sobre el rango de vela cerrada en Sweep+Reclaim.",
            "min_limit": 0.30,
            "max_limit": 0.75,
            "structural_lock_when_open": False,
        },
        "min_wick_ratio_intrabar": {
            "value": 0.55,
            "unit": "ratio",
            "description": "Proporcion minima de mecha de rechazo en vela viva para confirmacion intrabar.",
            "min_limit": 0.40,
            "max_limit": 0.85,
            "structural_lock_when_open": False,
        },
        "max_liq_bonus": {
            "value": 0.10,
            "unit": "q_units",
            "description": "Tope positivo de liq_bias en Pre-Flight cuando la entrada confluye con un Sweep+Reclaim.",
            "min_limit": 0.00,
            "max_limit": 0.20,
            "structural_lock_when_open": False,
        },
        "max_liq_penalty": {
            "value": 0.12,
            "unit": "q_units",
            "description": "Tope de penalizacion de liq_bias en Pre-Flight al intentar entrar contra un Sweep opuesto.",
            "min_limit": 0.00,
            "max_limit": 0.25,
            "structural_lock_when_open": False,
        },
        "min_opposite_wall_fee_ratio": {
            "value": 1.80,
            "unit": "ratio",
            "description": "Multiplo minimo de comision ida-vuelta disponible hasta un muro EQH/EQL opuesto para autorizar entrada.",
            "min_limit": 1.00,
            "max_limit": 5.00,
            "structural_lock_when_open": False,
        },
    },
    "limit_execution": {
        "enabled": {
            "value": 0,
            "unit": "flag",
            "description": "Habilita el enrutamiento de ordenes limite pasivas con TTL para estilos SNIPER/REVERSION/SCALP (1=activo, 0=solo market).",
            "min_limit": 0,
            "max_limit": 1,
            "structural_lock_when_open": True,
        },
        "tick_size_usd": {
            "value": 0.5,
            "unit": "usd",
            "description": "Incremento minimo de precio (tick) para redondeo pasivo non-crossing en futuros BTCUSD.",
            "min_limit": 0.5,
            "max_limit": 5.0,
            "structural_lock_when_open": True,
        },
        "ttl_sec_5m": {
            "value": 90,
            "unit": "seconds",
            "description": "Tiempo maximo de vida (TTL) en segundos para ordenes limite ancladas a barridas de 5m o pullback.",
            "min_limit": 30,
            "max_limit": 300,
            "structural_lock_when_open": False,
        },
        "ttl_sec_15m": {
            "value": 150,
            "unit": "seconds",
            "description": "Tiempo maximo de vida (TTL) en segundos para ordenes limite ancladas a barridas de 15m.",
            "min_limit": 60,
            "max_limit": 600,
            "structural_lock_when_open": False,
        },
        "runaway_tp_frac": {
            "value": 0.55,
            "unit": "ratio",
            "description": "Fraccion del recorrido hacia TP que dispara cancelacion anticipada por fuga de precio (Runaway Guard).",
            "min_limit": 0.30,
            "max_limit": 0.80,
            "structural_lock_when_open": False,
        },
        "max_entry_offset_atr": {
            "value": 0.75,
            "unit": "atr_mult",
            "description": "Distancia maxima permitida (en ATR) entre spot actual y P_lim para evitar colocar ordenes muertas.",
            "min_limit": 0.25,
            "max_limit": 1.50,
            "structural_lock_when_open": False,
        },
        "retest_atr_sniper": {
            "value": 0.05,
            "unit": "atr_mult",
            "description": "Desplazamiento en ATR sobre el nivel barrido para fijar P_lim en entradas SNIPER.",
            "min_limit": 0.00,
            "max_limit": 0.30,
            "structural_lock_when_open": False,
        },
        "retest_atr_reversion": {
            "value": 0.12,
            "unit": "atr_mult",
            "description": "Desplazamiento en ATR sobre el nivel barrido para fijar P_lim en entradas REVERSION.",
            "min_limit": 0.00,
            "max_limit": 0.35,
            "structural_lock_when_open": False,
        },
    },
}


def _is_real_number(val: Any) -> bool:
    """Verifica que el valor sea int o float real (excluyendo bool y NaN/Inf)."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    if val != val or val == float("inf") or val == float("-inf"):
        return False
    return True


class LearningThresholdsManager:
    """
    Gestor centralizado con Hot-Reload atómico y validación estricta de
    límites para config/learning_thresholds.json.
    """

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        min_check_interval_sec: float = 2.0,
        register_signals: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.config_path = os.path.abspath(config_path)
        self.min_check_interval_sec = float(min_check_interval_sec)
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = copy.deepcopy(FALLBACK_THRESHOLDS)
        self._last_mtime: float = 0.0
        self._last_check_ts: float = 0.0
        self._signal_reload_requested: bool = False
        self._callbacks: List[Callable[[LearningThresholdsManager], None]] = []

        # Contadores de auditoría
        self.reload_success_count: int = 0
        self.reload_rejected_count: int = 0
        self.structural_lock_blocks_count: int = 0
        self.last_error_message: str = ""

        # Carga inicial
        self.check_and_reload(active_trades_count=0, force=True)

        if register_signals and hasattr(signal, "SIGHUP"):
            try:
                signal.signal(signal.SIGHUP, self._handle_sighup)
            except (ValueError, OSError):
                # Ignorar si se instancia fuera del hilo principal
                pass

    def _handle_sighup(self, signum: int, frame: Any) -> None:
        self._signal_reload_requested = True

    def register_callback(self, callback: Callable[[LearningThresholdsManager], None]) -> None:
        """Registra un callback invocado tras cada recarga exitosa."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def switch_environment(self, env_mode: str = "mainnet") -> str:
        """Conmuta atómicamente el archivo de umbrales según el entorno (mainnet/testnet) y recarga."""
        target_path = os.path.abspath(resolve_thresholds_path_for_env(env_mode))
        with self._lock:
            if self.config_path != target_path:
                self.config_path = target_path
                self._last_mtime = 0.0
                self.check_and_reload(active_trades_count=0, force=True)
        return self.config_path

    @staticmethod
    def validate_schema_and_limits(
        raw_payload: Any,
    ) -> Tuple[bool, str, Optional[Dict[str, Dict[str, Dict[str, Any]]]]]:
        """
        Valida que el payload JSON cumpla con:
          1. Las 10 categorías requeridas.
          2. Todos los parámetros base del esquema presentes.
          3. Los 5 campos obligatorios (value, unit, description, min_limit, max_limit).
          4. Tipos numéricos válidos y min_limit <= value <= max_limit.
        """
        if not isinstance(raw_payload, dict):
            return False, "Raiz del archivo JSON no es un objeto/diccionario.", None

        for cat in REQUIRED_CATEGORIES:
            if cat not in raw_payload:
                return False, f"Falta categoria obligatoria '{cat}'.", None
            if not isinstance(raw_payload[cat], dict) or len(raw_payload[cat]) == 0:
                return False, f"La categoria '{cat}' debe ser un diccionario no vacio.", None

        # Validar cada parámetro existente en el payload y verificar que las claves críticas existan
        for cat in REQUIRED_CATEGORIES:
            expected_params = FALLBACK_THRESHOLDS.get(cat, {})
            for req_param in expected_params:
                if req_param not in raw_payload[cat]:
                    return False, f"Falta parametro obligatorio '{cat}.{req_param}'.", None

            for param_name, param_obj in raw_payload[cat].items():
                if not isinstance(param_obj, dict):
                    return False, f"Parametro '{cat}.{param_name}' no es un objeto estructurado.", None

                for req_field in REQUIRED_PARAM_FIELDS:
                    if req_field not in param_obj:
                        return (
                            False,
                            f"Parametro '{cat}.{param_name}' carece del campo obligatorio '{req_field}'.",
                            None,
                        )

                val = param_obj["value"]
                min_lim = param_obj["min_limit"]
                max_lim = param_obj["max_limit"]
                unit = param_obj["unit"]
                desc = param_obj["description"]

                if not isinstance(unit, str) or not unit.strip():
                    return False, f"Campo 'unit' invalido en '{cat}.{param_name}'.", None
                if not isinstance(desc, str) or not desc.strip():
                    return False, f"Campo 'description' invalido en '{cat}.{param_name}'.", None
                if not _is_real_number(val):
                    return False, f"Valor no numerico en '{cat}.{param_name}.value': {val!r}.", None
                if not _is_real_number(min_lim) or not _is_real_number(max_lim):
                    return False, f"Limites no numericos en '{cat}.{param_name}'.", None
                if float(min_lim) > float(max_lim):
                    return (
                        False,
                        f"Rango incoherente en '{cat}.{param_name}': min_limit ({min_lim}) > max_limit ({max_lim}).",
                        None,
                    )
                if float(val) < float(min_lim) or float(val) > float(max_lim):
                    return (
                        False,
                        f"Valor fuera de rango en '{cat}.{param_name}': value={val} no pertenece a [{min_lim}, {max_lim}].",
                        None,
                    )

        return True, "OK", copy.deepcopy(raw_payload)

    def _is_structurally_locked(self, category: str, param_name: str, param_obj: Dict[str, Any]) -> bool:
        if (category, param_name) in STRUCTURAL_LOCKED_PARAMS:
            return True
        return bool(param_obj.get("structural_lock_when_open", False))

    def check_and_reload(self, active_trades_count: int = 0, force: bool = False) -> bool:
        """
        Comprueba si el archivo de configuración cambió en disco y ejecuta
        una recarga atómica validada.

        Retorna True si se aplicó una recarga válida, False si no hubo cambios
        o si la recarga fue rechazada por validación.
        """
        now = time.time()
        if not force and not self._signal_reload_requested:
            if (now - self._last_check_ts) < self.min_check_interval_sec:
                return False

        with self._lock:
            self._last_check_ts = now
            self._signal_reload_requested = False

            if not os.path.exists(self.config_path):
                msg = f"[THRESHOLDS-GUARD] Archivo no encontrado en {self.config_path}; manteniendo valores activos/fallback."
                self.last_error_message = msg
                logger.warning(msg)
                return False

            try:
                mtime = os.path.getmtime(self.config_path)
            except OSError as exc:
                msg = f"[THRESHOLDS-GUARD] Error leyendo mtime de {self.config_path}: {exc}"
                self.last_error_message = msg
                logger.warning(msg)
                return False

            if not force and mtime <= self._last_mtime and self.reload_success_count > 0:
                return False

            try:
                with open(self.config_path, "r", encoding="utf-8") as fh:
                    raw_payload = json.load(fh)
            except Exception as exc:
                self.reload_rejected_count += 1
                msg = (
                    f"[THRESHOLDS-GUARD] RECHAZO DE RECARGA — Sintaxis JSON corrupta en "
                    f"{self.config_path}: {exc}. Se mantienen los ultimos valores validos."
                )
                self.last_error_message = msg
                logger.error(msg)
                print(msg)
                return False

            is_valid, reason, validated_data = self.validate_schema_and_limits(raw_payload)
            if not is_valid or validated_data is None:
                self.reload_rejected_count += 1
                msg = (
                    f"[THRESHOLDS-GUARD] RECHAZO DE RECARGA — Validacion fallida ({reason}). "
                    f"Se mantienen los ultimos valores validos conocidos."
                )
                self.last_error_message = msg
                logger.error(msg)
                print(msg)
                return False

            # Protección de parámetros estructurales críticos cuando hay órdenes abiertas
            if int(active_trades_count) > 0 and self.reload_success_count > 0:
                for cat, params in validated_data.items():
                    for p_name, p_obj in params.items():
                        prev_obj = self._data.get(cat, {}).get(p_name)
                        if prev_obj is None:
                            continue
                        if self._is_structurally_locked(cat, p_name, prev_obj) or self._is_structurally_locked(
                            cat, p_name, p_obj
                        ):
                            if float(p_obj["value"]) != float(prev_obj["value"]):
                                self.structural_lock_blocks_count += 1
                                lock_msg = (
                                    f"[THRESHOLDS-GUARD] BLOQUEO ESTRUCTURAL ACTIVO ({active_trades_count} ordenes abiertas): "
                                    f"Se rechaza cambio en caliente de '{cat}.{p_name}' "
                                    f"({prev_obj['value']} -> {p_obj['value']})."
                                )
                                logger.warning(lock_msg)
                                print(lock_msg)
                                validated_data[cat][p_name]["value"] = prev_obj["value"]

            # Reemplazo atómico en memoria
            self._data = validated_data
            self._last_mtime = mtime
            self.reload_success_count += 1
            self.last_error_message = ""

            callbacks_snapshot = list(self._callbacks)

        for cb in callbacks_snapshot:
            try:
                cb(self)
            except Exception as cb_exc:
                logger.warning(f"[THRESHOLDS-GUARD] Error en callback post-recarga: {cb_exc}")

        return True

    def get(self, category: str, param_name: str, default: Any = None) -> Any:
        """Obtiene el valor ('value') de un parámetro en O(1)."""
        with self._lock:
            cat_dict = self._data.get(category)
            if not cat_dict:
                return default
            param_obj = cat_dict.get(param_name)
            if not param_obj:
                return default
            return param_obj.get("value", default)

    def get_float(self, category: str, param_name: str, default: float = 0.0) -> float:
        val = self.get(category, param_name, default)
        try:
            return float(val)
        except (TypeError, ValueError):
            return float(default)

    def get_int(self, category: str, param_name: str, default: int = 0) -> int:
        val = self.get(category, param_name, default)
        try:
            return int(round(float(val)))
        except (TypeError, ValueError):
            return int(default)

    def get_bool(self, category: str, param_name: str, default: bool = False) -> bool:
        val = self.get(category, param_name, 1 if default else 0)
        if isinstance(val, bool):
            return val
        try:
            return float(val) >= 0.5
        except (TypeError, ValueError):
            return bool(default)

    def get_meta(self, category: str, param_name: str) -> Optional[Dict[str, Any]]:
        """Retorna el diccionario completo (value, unit, description, min_limit, max_limit)."""
        with self._lock:
            cat_dict = self._data.get(category)
            if not cat_dict or param_name not in cat_dict:
                return None
            return copy.deepcopy(cat_dict[param_name])

    def get_snapshot(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Retorna una copia profunda del árbol completo de umbrales activos."""
        with self._lock:
            return copy.deepcopy(self._data)


# Singleton global accesible por todos los módulos del ecosistema LNM Sniper
_GLOBAL_THRESHOLDS_MANAGER: Optional[LearningThresholdsManager] = None
_GLOBAL_INIT_LOCK = threading.Lock()


def get_thresholds_manager(config_path: Optional[str] = None) -> LearningThresholdsManager:
    """Obtiene o inicializa el gestor global de umbrales."""
    global _GLOBAL_THRESHOLDS_MANAGER
    with _GLOBAL_INIT_LOCK:
        if _GLOBAL_THRESHOLDS_MANAGER is None:
            path = config_path if config_path else DEFAULT_CONFIG_PATH
            _GLOBAL_THRESHOLDS_MANAGER = LearningThresholdsManager(
                config_path=path,
                register_signals=True,
            )
        elif config_path and os.path.abspath(config_path) != _GLOBAL_THRESHOLDS_MANAGER.config_path:
            _GLOBAL_THRESHOLDS_MANAGER = LearningThresholdsManager(
                config_path=config_path,
                register_signals=False,
            )
        return _GLOBAL_THRESHOLDS_MANAGER
