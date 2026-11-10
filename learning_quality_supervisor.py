#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
LNM SNIPER — MOTOR DE SUPERVISIÓN DE CALIDAD DEL APRENDIZAJE Y TELEMETRÍA ATÓMICA
================================================================================
Implementa observabilidad matemática incremental O(1) en memoria y protección
autónoma con histéresis sobre el motor Q-Learning:

  A. TD-Error (Temporal Difference Error): |delta_t|, media móvil (ventana 50),
     gradiente lineal, clasificación (WARMING_UP, STABLE, IMPROVING, WARNING,
     CRITICAL) y Learning Guard con suspensión de updates online e histéresis.
  B. Q-Bias en Satoshis: Proyección inversa de Q a satoshis (Q_entry_sats),
     sesgo realizado (Q_entry_sats - Net_Sats_Realized) en ventana móvil de 20
     operaciones, dispersión y tendencia.
  C. Fee-Efficiency Ratio (FER): Ventana móvil (20 ops) y acumulada global,
     separación de cohortes ganadoras/perdedoras y protección contra división
     por cero.
  D. Entropía de la Política (Policy Entropy): Ventana móvil de 100 decisiones
     sobre las 9 acciones discretas, entropía absoluta y normalizada [0, 1],
     acción dominante y detección de colapso sin mutar la Q-table.
  E. Captura MFE / MAE: Monitoreo por posición en satoshis y ratios de captura
     MFE (Realized / MFE) y exposición adversa (MAE / Margin).
  F. Matriz de Respuesta Autónoma con Histéresis y Despachador de Telemetría
     Headless Atómica (telemetry_live.json vía archivo temporal + os.replace).
================================================================================
"""

from __future__ import annotations

import collections
import datetime
import json
import logging
import math
import os
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from learning_thresholds_manager import LearningThresholdsManager, get_thresholds_manager

logger = logging.getLogger("SniperLearningSupervisor")

DEFAULT_TELEMETRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "telemetry_live.json",
)

ACTION_TELEMETRY_NAMES: Dict[int, str] = {
    0: "HOLD",
    1: "LONG_CONSERVATIVE",
    2: "LONG_MODERATE",
    3: "LONG_AGGRESSIVE",
    4: "LONG_CONTRARIAN",
    5: "SHORT_CONSERVATIVE",
    6: "SHORT_MODERATE",
    7: "SHORT_AGGRESSIVE",
    8: "SHORT_REVERSION",
}


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convierte a float garantizando ausencia de NaN o Inf para JSON."""
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return float(default)
        return f
    except (TypeError, ValueError):
        return float(default)


def _compute_linear_slope(values: Deque[float]) -> float:
    """Calcula la pendiente de regresión lineal simple sobre una ventana O(N)."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / float(n)
    num = 0.0
    den = 0.0
    for idx, y_val in enumerate(values):
        dx = idx - x_mean
        num += dx * (y_val - y_mean)
        den += dx * dx
    if den <= 1e-12:
        return 0.0
    return num / den


def _compute_std(values: Deque[float], mean_val: Optional[float] = None) -> float:
    """Calcula la desviación estándar poblacional sobre una ventana."""
    n = len(values)
    if n < 2:
        return 0.0
    mu = (sum(values) / float(n)) if mean_val is None else mean_val
    var = sum((v - mu) ** 2 for v in values) / float(n)
    return math.sqrt(max(0.0, var))


class LearningQualitySupervisor:
    """
    Motor de supervisión matemática de calidad de aprendizaje en tiempo real
    y matriz de respuesta autónoma con histéresis.
    """

    def __init__(
        self,
        thresholds_manager: Optional[LearningThresholdsManager] = None,
        snapshot_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.thresholds = thresholds_manager or get_thresholds_manager()
        self.snapshot_callback = snapshot_callback

        # Dimensiones iniciales de ventanas desde config/learning_thresholds.json
        td_win = max(5, self.thresholds.get_int("td_error", "window_size", 50))
        q_win = max(5, self.thresholds.get_int("q_calibration", "window_size", 20))
        fer_win = max(5, self.thresholds.get_int("fees", "fer_window_size", 20))
        ent_win = max(10, self.thresholds.get_int("policy_entropy", "window_size", 100))
        mfe_win = max(3, self.thresholds.get_int("mfe_mae", "window_size", 20))

        # A. Ventana TD-Error
        self.td_error_window: Deque[float] = collections.deque(maxlen=td_win)
        self.signed_td_error_window: Deque[float] = collections.deque(maxlen=td_win)
        self.last_abs_td_error: float = 0.0
        self.last_signed_td_error: float = 0.0
        self.td_error_status: str = "WARMING_UP"
        self.td_error_trend: str = "INSUFFICIENT_DATA"

        # Learning Guard (Suspensión de Q-updates e histéresis)
        self.learning_guard_status: str = "ACTIVE_LEARNING"
        self.critical_td_streak: int = 0
        self.hysteresis_recovery_streak: int = 0
        self.suspended_updates_count: int = 0
        self.snapshots_triggered_count: int = 0

        # B. Ventana Q-Bias en satoshis
        self.q_bias_window: Deque[float] = collections.deque(maxlen=q_win)
        self.realized_sats_window: Deque[float] = collections.deque(maxlen=fer_win)
        self.q_bias_status: str = "WARMING_UP"
        self.q_bias_overestimation_streak: int = 0
        self.q_bias_recovery_streak: int = 0
        self.q_bias_defense_active: bool = False

        # C. Ventana Fee-Efficiency Ratio (FER) y acumulados globales
        self.fer_trades_window: Deque[Tuple[int, int]] = collections.deque(maxlen=fer_win)
        self.global_net_sats: int = 0
        self.global_gross_profit_sats: int = 0
        self.global_fees_sats: int = 0
        self.global_wins_net_sats: int = 0
        self.global_wins_fees_sats: int = 0
        self.global_wins_count: int = 0
        self.global_losses_net_sats: int = 0
        self.global_losses_fees_sats: int = 0
        self.global_losses_count: int = 0
        self.fer_status: str = "WARMING_UP"
        self.low_fer_streak: int = 0
        self.fer_recovery_streak: int = 0
        self.fer_defense_active: bool = False

        # D. Ventana Entropía de la Política (100 decisiones)
        self.policy_decisions_window: Deque[int] = collections.deque(maxlen=ent_win)
        self.low_entropy_streak: int = 0
        self.entropy_recovery_streak: int = 0
        self.policy_collapse_detected: bool = False
        self.policy_collapse_reason: str = "NONE"

        # E. Seguimiento MFE / MAE por posición abierta y ventana cerrada (20 ops)
        self.open_positions_excursion: Dict[str, Dict[str, float]] = {}
        self.mfe_capture_window: Deque[float] = collections.deque(maxlen=mfe_win)
        self.mae_margin_window: Deque[float] = collections.deque(maxlen=mfe_win)
        self.mfe_mae_bad_streak: int = 0
        self.mfe_mae_recovery_streak: int = 0
        self.mfe_mae_defense_active: bool = False

        # Registro estructurado de alertas de supervisión
        self.alerts_log: Deque[Dict[str, Any]] = collections.deque(maxlen=100)

    def _emit_alert(self, indicator: str, severity: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "indicator": indicator,
            "severity": severity,
            "message": message,
            "meta": meta or {},
        }
        self.alerts_log.append(entry)
        log_line = f"[LEARNING-SUPERVISOR][{severity}][{indicator}] {message}"
        if severity in ("CRITICAL", "WARNING"):
            logger.warning(log_line)
        else:
            logger.info(log_line)

    # =========================================================================
    # A. TD-ERROR Y LEARNING GUARD (CON HISTÉRESIS)
    # =========================================================================
    def can_update_q_table(self) -> bool:
        """Indica si las actualizaciones online a la Q-table están permitidas."""
        with self._lock:
            return self.learning_guard_status == "ACTIVE_LEARNING"

    def evaluate_td_error_pre_update(self, signed_td_error: float) -> Tuple[bool, str]:
        """
        Evalúa el TD-Error de una transición antes/durante el update Q.
        Actualiza las métricas de TD-Error y el estado del Learning Guard
        con histéresis.

        Retorna (allow_q_update, learning_guard_status).
        """
        with self._lock:
            abs_td = abs(_safe_float(signed_td_error, 0.0))
            self.last_abs_td_error = abs_td
            self.last_signed_td_error = _safe_float(signed_td_error, 0.0)
            self.td_error_window.append(abs_td)
            self.signed_td_error_window.append(self.last_signed_td_error)

            warmup_min = self.thresholds.get_int("td_error", "warmup_min_samples", 5)
            stable_thr = self.thresholds.get_float("td_error", "stable_threshold", 0.45)
            warn_thr = self.thresholds.get_float("td_error", "warning_threshold", 0.85)
            crit_thr = self.thresholds.get_float("td_error", "critical_threshold", 1.35)
            imp_slope = self.thresholds.get_float("td_error", "improving_slope_threshold", -0.005)
            div_slope = self.thresholds.get_float("td_error", "diverging_slope_threshold", 0.015)

            crit_streak_limit = self.thresholds.get_int("learning_guard", "critical_td_streak_to_suspend", 3)
            hyst_recovery_req = self.thresholds.get_int("learning_guard", "hysteresis_recovery_updates", 5)

            n = len(self.td_error_window)
            rolling_avg = (sum(self.td_error_window) / float(n)) if n > 0 else 0.0
            slope = _compute_linear_slope(self.td_error_window)

            # Determinación de estado de TD-Error
            is_single_critical = abs_td >= crit_thr
            if n < warmup_min and not is_single_critical:
                self.td_error_status = "WARMING_UP"
                self.td_error_trend = "INSUFFICIENT_DATA"
            else:
                if abs_td >= crit_thr or (rolling_avg >= crit_thr and abs_td >= warn_thr):
                    self.td_error_status = "CRITICAL"
                    self.td_error_trend = "CRITICAL"
                elif rolling_avg >= warn_thr or (abs_td >= warn_thr and slope > div_slope):
                    self.td_error_status = "WARNING"
                    self.td_error_trend = "WARNING"
                elif slope <= imp_slope and rolling_avg < warn_thr:
                    self.td_error_status = "IMPROVING"
                    self.td_error_trend = "IMPROVING"
                elif rolling_avg <= stable_thr:
                    self.td_error_status = "STABLE"
                    self.td_error_trend = "STABLE"
                else:
                    self.td_error_status = "STABLE" if slope <= div_slope else "WARNING"
                    self.td_error_trend = self.td_error_status

            # Lógica de Protección Learning Guard con Histéresis
            if self.learning_guard_status == "ACTIVE_LEARNING":
                if self.td_error_status == "CRITICAL":
                    self.critical_td_streak += 1
                    if self.critical_td_streak >= crit_streak_limit:
                        self.learning_guard_status = "SUSPENDED_CRITICAL_TD"
                        self.hysteresis_recovery_streak = 0
                        self.suspended_updates_count += 1
                        self._emit_alert(
                            indicator="TD_ERROR",
                            severity="CRITICAL",
                            message=(
                                f"Divergencia sostenida detectada ({self.critical_td_streak} updates CRITICAL, "
                                f"|delta_t|={abs_td:.4f}, avg50={rolling_avg:.4f}). Se suspenden temporalmente "
                                f"las actualizaciones online de la Q-table."
                            ),
                            meta={"abs_td": abs_td, "rolling_avg": rolling_avg, "streak": self.critical_td_streak},
                        )
                        if self.snapshot_callback is not None:
                            try:
                                self.snapshots_triggered_count += 1
                                self.snapshot_callback("CRITICAL_TD_DIVERGENCE")
                            except Exception as exc:
                                logger.warning(f"Error ejecutando snapshot_callback: {exc}")
                        return False, self.learning_guard_status
                else:
                    self.critical_td_streak = 0
                return True, self.learning_guard_status

            else:
                # Estado suspendido (SUSPENDED_CRITICAL_TD o RECOVERING_HYSTERESIS)
                # Evaluar la transición actual sin quedar bloqueado por el promedio histórico previo
                if abs_td < warn_thr:
                    self.hysteresis_recovery_streak += 1
                    self.learning_guard_status = "RECOVERING_HYSTERESIS"
                    if self.hysteresis_recovery_streak >= hyst_recovery_req:
                        self.learning_guard_status = "ACTIVE_LEARNING"
                        self.critical_td_streak = 0
                        self.hysteresis_recovery_streak = 0
                        self._emit_alert(
                            indicator="TD_ERROR",
                            severity="INFO",
                            message=(
                                f"Estabilidad comprobada tras {hyst_recovery_req} transiciones en histeresis "
                                f"(|delta_t|={abs_td:.4f}, avg50={rolling_avg:.4f}). Se reactivan Q-updates online."
                            ),
                        )
                        return True, self.learning_guard_status
                else:
                    self.hysteresis_recovery_streak = 0
                    self.learning_guard_status = "SUSPENDED_CRITICAL_TD"

                self.suspended_updates_count += 1
                return False, self.learning_guard_status

    # =========================================================================
    # B, C, E. REGISTRO DE OPERACIÓN CERRADA (Q-BIAS, FER, MFE/MAE)
    # =========================================================================
    def record_closed_trade(
        self,
        trade_id: Optional[str],
        predicted_q_entry: Optional[float],
        net_realized_sats: int,
        total_fees_sats: int,
        margin_sats: int,
        mfe_sats: Optional[float] = None,
        mae_sats: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Actualiza incrementalmente Q-Bias en satoshis, Fee-Efficiency Ratio (FER)
        y métricas de captura MFE/MAE al liquidarse una posición.
        Si `predicted_q_entry is None` (orden adoptada sin Q de apertura), se
        omite de la ventana de Q-Bias para no contaminar la calibración.
        """
        with self._lock:
            # Recuperar excursiones registradas en vivo si no fueron provistas explícitamente
            tracked_exc = self.open_positions_excursion.pop(str(trade_id), None) if trade_id else None
            if mfe_sats is None:
                mfe_sats = tracked_exc["mfe_sats"] if tracked_exc else max(0.0, float(net_realized_sats))
            if mae_sats is None:
                mae_sats = tracked_exc["mae_sats"] if tracked_exc else max(0.0, float(-net_realized_sats))

            mfe_val = max(0.0, _safe_float(mfe_sats, 0.0))
            mae_val = max(0.0, _safe_float(mae_sats, 0.0))
            net_sats = int(round(_safe_float(net_realized_sats, 0.0)))
            fee_sats = max(0, int(round(_safe_float(total_fees_sats, 0.0))))
            margin_val = max(1, int(round(_safe_float(margin_sats, 1000.0))))

            self.realized_sats_window.append(float(net_sats))

            # -----------------------------------------------------------------
            # B. Q-Bias en Satoshis: Q_entry_sats - Net_Sats_Realized
            # -----------------------------------------------------------------
            q_scale = self.thresholds.get_float("q_calibration", "q_to_sats_scale", 100.0)
            q_warmup = self.thresholds.get_int("q_calibration", "warmup_min_samples", 3)
            over_alert = self.thresholds.get_float("q_calibration", "overestimation_alert_sats", 25.0)
            under_alert = self.thresholds.get_float("q_calibration", "underestimation_alert_sats", -25.0)
            q_persist_req = self.thresholds.get_int("learning_guard", "q_bias_persistence_trades", 3)
            hyst_req = self.thresholds.get_int("learning_guard", "hysteresis_recovery_updates", 5)

            if predicted_q_entry is not None:
                q_entry_sats = _safe_float(predicted_q_entry, 0.0) * q_scale
                q_bias_trade = q_entry_sats - float(net_sats)
                self.q_bias_window.append(q_bias_trade)

                mean_q_bias = sum(self.q_bias_window) / float(len(self.q_bias_window))
                if mean_q_bias > over_alert:
                    self.q_bias_overestimation_streak += 1
                    self.q_bias_recovery_streak = 0
                else:
                    self.q_bias_overestimation_streak = 0

                if len(self.q_bias_window) < q_warmup:
                    self.q_bias_status = "WARMING_UP"
                else:
                    if mean_q_bias > over_alert:
                        self.q_bias_status = "OVERESTIMATING"
                        if (
                            not self.q_bias_defense_active
                            and self.q_bias_overestimation_streak >= q_persist_req
                        ):
                            self.q_bias_defense_active = True
                            self._emit_alert(
                                indicator="Q_BIAS",
                                severity="WARNING",
                                message=(
                                    f"Sobreestimacion Q sistematica detectada ({mean_q_bias:+.1f} sats > "
                                    f"{over_alert:+.1f} sats por {self.q_bias_overestimation_streak} ops). "
                                    f"Se incrementa umbral dinamico de expectancy."
                                ),
                                meta={"q_bias_sats": mean_q_bias},
                            )
                    elif mean_q_bias < under_alert:
                        self.q_bias_status = "UNDERESTIMATING"
                    else:
                        self.q_bias_status = "CALIBRATED"
                        if self.q_bias_defense_active:
                            self.q_bias_recovery_streak += 1
                            if self.q_bias_recovery_streak >= hyst_req:
                                self.q_bias_defense_active = False
                                self.q_bias_recovery_streak = 0
                                self._emit_alert(
                                    indicator="Q_BIAS",
                                    severity="INFO",
                                    message=f"Q-Bias recalibrado ({mean_q_bias:+.1f} sats). Se restaura umbral de expectancy.",
                                )
            else:
                q_entry_sats = 0.0
                q_bias_trade = 0.0

            # -----------------------------------------------------------------
            # C. Fee-Efficiency Ratio (FER)
            # -----------------------------------------------------------------
            self.fer_trades_window.append((net_sats, fee_sats))
            self.global_net_sats += net_sats
            self.global_fees_sats += fee_sats
            if net_sats > 0:
                self.global_gross_profit_sats += net_sats + fee_sats
                self.global_wins_net_sats += net_sats
                self.global_wins_fees_sats += fee_sats
                self.global_wins_count += 1
            else:
                self.global_losses_net_sats += net_sats
                self.global_losses_fees_sats += fee_sats
                self.global_losses_count += 1

            fer_val = self.get_rolling_fer()
            fer_high = self.thresholds.get_float("fees", "fer_high_threshold", 2.5)
            fer_mod = self.thresholds.get_float("fees", "fer_moderate_threshold", 1.2)
            fer_warn = self.thresholds.get_float("fees", "fer_warning_threshold", 1.0)
            fer_persist_req = self.thresholds.get_int("learning_guard", "fer_persistence_trades", 3)

            if fer_val < fer_mod:
                self.low_fer_streak += 1
                self.fer_recovery_streak = 0
            else:
                self.low_fer_streak = 0

            if len(self.fer_trades_window) < q_warmup:
                self.fer_status = "WARMING_UP"
            else:
                if fer_val >= fer_high:
                    self.fer_status = "HIGH_EFFICIENCY"
                elif fer_val >= fer_mod:
                    self.fer_status = "MODERATE_EFFICIENCY"
                elif fer_val >= fer_warn:
                    self.fer_status = "WARNING_FEE_DRAG"
                else:
                    self.fer_status = "CAPITAL_DESTRUCTION"

                if fer_val < fer_mod:
                    if not self.fer_defense_active and self.low_fer_streak >= fer_persist_req:
                        self.fer_defense_active = True
                        self._emit_alert(
                            indicator="FER",
                            severity="WARNING" if fer_val >= fer_warn else "CRITICAL",
                            message=(
                                f"Fee-Efficiency Ratio degradado (FER={fer_val:.2f} < {fer_mod:.2f} "
                                f"durante {self.low_fer_streak} ops). Endureciendo filtros de volatilidad y expectancy."
                            ),
                            meta={"fer": fer_val, "status": self.fer_status},
                        )
                else:
                    if self.fer_defense_active:
                        self.fer_recovery_streak += 1
                        if self.fer_recovery_streak >= hyst_req:
                            self.fer_defense_active = False
                            self.fer_recovery_streak = 0
                            self._emit_alert(
                                indicator="FER",
                                severity="INFO",
                                message=f"Fee-Efficiency Ratio recuperado (FER={fer_val:.2f}). Normalizando filtros.",
                            )

            # -----------------------------------------------------------------
            # E. Captura MFE / MAE
            # -----------------------------------------------------------------
            if mfe_val > 0.0:
                mfe_capture = max(0.0, min(1.5, float(max(0, net_sats)) / mfe_val))
            elif net_sats > 0:
                mfe_capture = 1.0
            else:
                mfe_capture = 0.0

            mae_margin_ratio = max(0.0, mae_val / float(margin_val))
            self.mfe_capture_window.append(mfe_capture)
            self.mae_margin_window.append(mae_margin_ratio)

            min_mfe_warn = self.thresholds.get_float("mfe_mae", "min_mfe_capture_warn", 0.40)
            max_mae_warn = self.thresholds.get_float("mfe_mae", "max_mae_margin_ratio_warn", 0.50)
            mfe_persist_req = self.thresholds.get_int("learning_guard", "mfe_mae_persistence_trades", 3)

            avg_mfe_cap = sum(self.mfe_capture_window) / float(len(self.mfe_capture_window))
            avg_mae_rat = sum(self.mae_margin_window) / float(len(self.mae_margin_window))
            if avg_mfe_cap < min_mfe_warn or avg_mae_rat > max_mae_warn:
                self.mfe_mae_bad_streak += 1
                self.mfe_mae_recovery_streak = 0
            else:
                self.mfe_mae_bad_streak = 0

            if len(self.mfe_capture_window) >= q_warmup:
                if avg_mfe_cap < min_mfe_warn or avg_mae_rat > max_mae_warn:
                    if not self.mfe_mae_defense_active and self.mfe_mae_bad_streak >= mfe_persist_req:
                        self.mfe_mae_defense_active = True
                        self._emit_alert(
                            indicator="MFE_MAE",
                            severity="WARNING",
                            message=(
                                f"Ineficiencia de excursion detectada (MFE_capture={avg_mfe_cap:.2f}, "
                                f"MAE/Margin={avg_mae_rat:.2f}). Ajustando multiplicador de trailing stop."
                            ),
                            meta={"mfe_capture_avg": avg_mfe_cap, "mae_margin_ratio_avg": avg_mae_rat},
                        )
                else:
                    if self.mfe_mae_defense_active:
                        self.mfe_mae_recovery_streak += 1
                        if self.mfe_mae_recovery_streak >= hyst_req:
                            self.mfe_mae_defense_active = False
                            self.mfe_mae_recovery_streak = 0

            return {
                "q_entry_sats": round(q_entry_sats, 3),
                "q_bias_trade_sats": round(q_bias_trade, 3),
                "mfe_sats": round(mfe_val, 2),
                "mae_sats": round(mae_val, 2),
                "mfe_capture_ratio": round(mfe_capture, 4),
                "mae_margin_ratio": round(mae_margin_ratio, 4),
            }

    # =========================================================================
    # D. ENTROPÍA DE LA POLÍTICA (VENTANA MÓVIL DE 100 DECISIONES)
    # =========================================================================
    def record_policy_decision(self, action_id: int) -> Dict[str, Any]:
        """
        Registra una decisión de acción (0..8) en la ventana móvil de 100
        decisiones y evalúa la entropía de Shannon y posibles colapsos.
        """
        with self._lock:
            act = int(action_id) if 0 <= int(action_id) <= 8 else 0
            self.policy_decisions_window.append(act)

            metrics = self.get_policy_entropy_metrics()
            warmup_min = self.thresholds.get_int("policy_entropy", "warmup_min_samples", 10)
            min_norm_ent = self.thresholds.get_float("policy_entropy", "min_normalized_entropy", 0.25)
            max_dom_freq = self.thresholds.get_float("policy_entropy", "max_dominant_action_freq", 0.85)
            persist_ticks = self.thresholds.get_int("learning_guard", "entropy_persistence_ticks", 10)
            hyst_req = self.thresholds.get_int("learning_guard", "hysteresis_recovery_updates", 5)

            n = len(self.policy_decisions_window)
            h_norm = metrics["normalized_policy_entropy"]
            dom_freq = metrics["dominant_action_freq"]
            dom_act = metrics["dominant_action_id"]

            if h_norm < min_norm_ent or dom_freq > max_dom_freq:
                self.low_entropy_streak += 1
                self.entropy_recovery_streak = 0
            else:
                self.low_entropy_streak = 0

            if n >= warmup_min:
                if h_norm < min_norm_ent or dom_freq > max_dom_freq:
                    if not self.policy_collapse_detected and self.low_entropy_streak >= persist_ticks:
                        self.policy_collapse_detected = True
                        if dom_act == 0:
                            self.policy_collapse_reason = "PARALYSIS_HOLD_FIXATION"
                        elif 1 <= dom_act <= 4:
                            self.policy_collapse_reason = "LONG_MONOPOLY_COLLAPSE"
                        else:
                            self.policy_collapse_reason = "SHORT_MONOPOLY_COLLAPSE"

                        self._emit_alert(
                            indicator="POLICY_ENTROPY",
                            severity="WARNING",
                            message=(
                                f"Colapso de politica detectado ({self.policy_collapse_reason}): "
                                f"H_norm={h_norm:.3f} < {min_norm_ent:.3f}, accion dominante="
                                f"{metrics['dominant_action']} ({dom_freq*100:.1f}%). "
                                f"Diagnostico registrado sin mutar Q-table."
                            ),
                            meta=metrics,
                        )
                else:
                    if self.policy_collapse_detected:
                        self.entropy_recovery_streak += 1
                        if self.entropy_recovery_streak >= hyst_req:
                            self.policy_collapse_detected = False
                            self.policy_collapse_reason = "NONE"
                            self.entropy_recovery_streak = 0
                            self._emit_alert(
                                indicator="POLICY_ENTROPY",
                                severity="INFO",
                                message=f"Diversidad de politica restaurada (H_norm={h_norm:.3f}).",
                            )
            return metrics

    # =========================================================================
    # E. MONITOREO EN VIVO DE MFE / MAE POR POSICIÓN ABIERTA
    # =========================================================================
    def update_open_position_excursion(
        self,
        trade_id: str,
        unrealized_pnl_sats: float,
        margin_sats: float = 0.0,
    ) -> Dict[str, float]:
        """
        Actualiza en tiempo real el Maximum Favorable Excursion (MFE) y
        Maximum Adverse Excursion (MAE) en satoshis para una orden abierta.
        """
        with self._lock:
            tid = str(trade_id)
            pnl = _safe_float(unrealized_pnl_sats, 0.0)
            favorable = max(0.0, pnl)
            adverse = max(0.0, -pnl)

            rec = self.open_positions_excursion.get(tid)
            if rec is None:
                rec = {
                    "mfe_sats": favorable,
                    "mae_sats": adverse,
                    "last_pnl_sats": pnl,
                    "margin_sats": max(1.0, _safe_float(margin_sats, 1000.0)),
                    "ticks_observed": 1.0,
                }
                self.open_positions_excursion[tid] = rec
            else:
                rec["mfe_sats"] = max(rec["mfe_sats"], favorable)
                rec["mae_sats"] = max(rec["mae_sats"], adverse)
                rec["last_pnl_sats"] = pnl
                if margin_sats > 0:
                    rec["margin_sats"] = float(margin_sats)
                rec["ticks_observed"] += 1.0

            return dict(rec)

    def get_open_position_excursion(self, trade_id: str) -> Dict[str, float]:
        with self._lock:
            rec = self.open_positions_excursion.get(str(trade_id))
            if rec is None:
                return {"mfe_sats": 0.0, "mae_sats": 0.0, "last_pnl_sats": 0.0, "ticks_observed": 0.0}
            return dict(rec)

    # =========================================================================
    # CÁLCULO DE MÉTRICAS Y RESPUESTAS AUTÓNOMAS
    # =========================================================================
    @staticmethod
    def _compute_fer_safe(sum_net_sats: int, sum_fees_sats: int) -> float:
        """
        Calcula FER = Net_Sats / Fees_Sats con protección contra división por cero.
        Si no hubo comisiones pero hubo ganancia neta, retorna ganancia acotada;
        si no hubo operaciones, retorna 0.0.
        """
        if sum_fees_sats <= 0:
            if sum_net_sats > 0:
                return float(min(50.0, sum_net_sats))
            if sum_net_sats < 0:
                return -1.0
            return 0.0
        return float(sum_net_sats) / float(sum_fees_sats)

    def get_rolling_fer(self) -> float:
        with self._lock:
            if not self.fer_trades_window:
                return 0.0
            total_net = sum(t[0] for t in self.fer_trades_window)
            total_fees = sum(t[1] for t in self.fer_trades_window)
            return self._compute_fer_safe(total_net, total_fees)

    def get_global_fer(self) -> float:
        with self._lock:
            return self._compute_fer_safe(self.global_net_sats, self.global_fees_sats)

    def get_fer_cohorts(self) -> Dict[str, Any]:
        """Separa eficiencia de comisiones entre cohortes ganadoras y perdedoras."""
        with self._lock:
            win_nets = [n for n, _ in self.fer_trades_window if n > 0]
            win_fees = [f for n, f in self.fer_trades_window if n > 0]
            loss_nets = [n for n, _ in self.fer_trades_window if n <= 0]
            loss_fees = [f for n, f in self.fer_trades_window if n <= 0]

            return {
                "rolling_winners_count": len(win_nets),
                "rolling_winners_fer": round(self._compute_fer_safe(sum(win_nets), sum(win_fees)), 4),
                "rolling_losers_count": len(loss_nets),
                "rolling_losers_fer": round(self._compute_fer_safe(sum(loss_nets), sum(loss_fees)), 4),
                "global_winners_count": self.global_wins_count,
                "global_winners_fer": round(
                    self._compute_fer_safe(self.global_wins_net_sats, self.global_wins_fees_sats), 4
                ),
                "global_losers_count": self.global_losses_count,
                "global_losers_fer": round(
                    self._compute_fer_safe(self.global_losses_net_sats, self.global_losses_fees_sats), 4
                ),
            }

    def get_policy_entropy_metrics(self) -> Dict[str, Any]:
        """Calcula entropía absoluta, normalizada [0, 1], acción dominante y frecuencias."""
        with self._lock:
            num_actions = max(2, self.thresholds.get_int("policy_entropy", "num_actions", 9))
            n = len(self.policy_decisions_window)
            if n == 0:
                freqs = {ACTION_TELEMETRY_NAMES.get(i, f"ACT_{i}"): 0.0 for i in range(num_actions)}
                return {
                    "policy_entropy": 0.0,
                    "normalized_policy_entropy": 0.0,
                    "dominant_action_id": 0,
                    "dominant_action": "HOLD",
                    "dominant_action_freq": 0.0,
                    "action_frequencies": freqs,
                    "samples": 0,
                }

            counts = [0] * num_actions
            for act in self.policy_decisions_window:
                if 0 <= act < num_actions:
                    counts[act] += 1

            entropy = 0.0
            freqs = {}
            dom_id = 0
            dom_count = -1
            for i in range(num_actions):
                c = counts[i]
                p = float(c) / float(n)
                freqs[ACTION_TELEMETRY_NAMES.get(i, f"ACT_{i}")] = round(p, 4)
                if c > dom_count:
                    dom_count = c
                    dom_id = i
                if p > 0.0:
                    entropy -= p * math.log(p)

            max_entropy = math.log(float(num_actions))
            norm_entropy = (entropy / max_entropy) if max_entropy > 0 else 0.0
            norm_entropy = max(0.0, min(1.0, norm_entropy))

            return {
                "policy_entropy": round(entropy, 4),
                "normalized_policy_entropy": round(norm_entropy, 4),
                "dominant_action_id": dom_id,
                "dominant_action": ACTION_TELEMETRY_NAMES.get(dom_id, f"ACT_{dom_id}"),
                "dominant_action_freq": round(float(dom_count) / float(n), 4),
                "action_frequencies": freqs,
                "samples": n,
            }

    def get_autonomous_adjustments(self) -> Dict[str, Any]:
        """
        Retorna los ajustes activos dictados por la Matriz de Respuesta Autónoma
        con Histéresis (Sección 7 de la Directiva Maestra).
        """
        with self._lock:
            base_exp = self.thresholds.get_float("expectancy", "min_expectancy_ratio", 3.85)
            exp_boost = 0.0
            if self.q_bias_defense_active:
                exp_boost += self.thresholds.get_float("learning_guard", "q_bias_expectancy_step", 0.35)
            if self.fer_defense_active:
                exp_boost += self.thresholds.get_float("learning_guard", "fer_expectancy_boost", 0.50)

            trailing_mult = (
                self.thresholds.get_float("mfe_mae", "trailing_tighten_factor", 0.85)
                if self.mfe_mae_defense_active
                else 1.0
            )

            return {
                "allow_online_q_updates": self.learning_guard_status == "ACTIVE_LEARNING",
                "learning_guard_status": self.learning_guard_status,
                "effective_min_expectancy_ratio": round(base_exp + exp_boost, 4),
                "expectancy_boost_applied": round(exp_boost, 4),
                "q_bias_defense_active": self.q_bias_defense_active,
                "fer_defense_active": self.fer_defense_active,
                "policy_collapse_detected": self.policy_collapse_detected,
                "policy_collapse_reason": self.policy_collapse_reason,
                "mfe_mae_defense_active": self.mfe_mae_defense_active,
                "dynamic_trailing_multiplier": round(trailing_mult, 4),
            }

    def get_rl_metrics_snapshot(self) -> Dict[str, Any]:
        """
        Construye el diccionario `rl_metrics` exacto requerido para
        `telemetry_live.json` junto con metadatos de diagnóstico.
        """
        with self._lock:
            n_td = len(self.td_error_window)
            rolling_td_avg = (sum(self.td_error_window) / float(n_td)) if n_td > 0 else 0.0
            td_slope = _compute_linear_slope(self.td_error_window)

            n_qb = len(self.q_bias_window)
            q_bias_avg = (sum(self.q_bias_window) / float(n_qb)) if n_qb > 0 else 0.0
            q_bias_std = _compute_std(self.q_bias_window, q_bias_avg) if n_qb > 1 else 0.0
            q_bias_slope = _compute_linear_slope(self.q_bias_window)

            n_sats = len(self.realized_sats_window)
            avg_sats_20 = (sum(self.realized_sats_window) / float(n_sats)) if n_sats > 0 else 0.0

            fer_rolling = self.get_rolling_fer()
            fer_global = self.get_global_fer()
            ent_metrics = self.get_policy_entropy_metrics()

            n_mfe = len(self.mfe_capture_window)
            mfe_avg = (sum(self.mfe_capture_window) / float(n_mfe)) if n_mfe > 0 else 0.0
            target_mfe = self.thresholds.get_float("mfe_mae", "target_mfe_capture_ratio", 0.65)
            healthy_ent = self.thresholds.get_float("policy_entropy", "healthy_normalized_entropy", 0.45)

            n_mae = len(self.mae_margin_window)
            mae_avg = (sum(self.mae_margin_window) / float(n_mae)) if n_mae > 0 else 0.0

            # Salud global de la política
            if self.learning_guard_status != "ACTIVE_LEARNING":
                policy_health = "GUARD_SUSPENDED"
            elif self.policy_collapse_detected:
                policy_health = "COLLAPSE_WARNING"
            elif self.fer_defense_active or self.q_bias_defense_active or self.mfe_mae_defense_active:
                policy_health = "DEGRADED_DEFENSIVE"
            elif n_td < self.thresholds.get_int("td_error", "warmup_min_samples", 5):
                policy_health = "WARMING_UP"
            else:
                policy_health = "OPTIMAL"

            return {
                "rolling_td_error_avg": round(_safe_float(rolling_td_avg), 4),
                "td_error_trend": self.td_error_trend,
                "td_error_status": self.td_error_status,
                "td_error_slope": round(_safe_float(td_slope), 5),
                "last_abs_td_error": round(_safe_float(self.last_abs_td_error), 4),
                "q_bias_sats": round(_safe_float(q_bias_avg), 2),
                "q_bias_std_sats": round(_safe_float(q_bias_std), 2),
                "q_bias_slope": round(_safe_float(q_bias_slope), 4),
                "q_bias_status": self.q_bias_status,
                "fee_efficiency_ratio": round(_safe_float(fer_rolling), 2),
                "global_fee_efficiency_ratio": round(_safe_float(fer_global), 2),
                "fer_status": self.fer_status,
                "fer_cohorts": self.get_fer_cohorts(),
                "policy_entropy": round(_safe_float(ent_metrics["policy_entropy"]), 4),
                "normalized_policy_entropy": round(_safe_float(ent_metrics["normalized_policy_entropy"]), 4),
                "healthy_normalized_entropy": round(_safe_float(healthy_ent), 4),
                "dominant_action": ent_metrics["dominant_action"],
                "dominant_action_freq": ent_metrics["dominant_action_freq"],
                "action_frequencies": ent_metrics["action_frequencies"],
                "avg_sats_per_trade_last20": round(_safe_float(avg_sats_20), 2),
                "policy_health": policy_health,
                "mfe_capture_avg": round(_safe_float(mfe_avg), 4),
                "target_mfe_capture_ratio": round(_safe_float(target_mfe), 4),
                "mae_margin_ratio_avg": round(_safe_float(mae_avg), 4),
                "learning_guard_status": self.learning_guard_status,
                "autonomous_adjustments": self.get_autonomous_adjustments(),
                "window_samples": {
                    "td_error_samples": n_td,
                    "q_bias_samples": n_qb,
                    "fer_samples": len(self.fer_trades_window),
                    "entropy_samples": ent_metrics["samples"],
                    "mfe_mae_samples": n_mfe,
                },
            }


class AtomicTelemetryDispatcher:
    """
    Despachador de telemetría headless atómica en tiempo real (Sección 8).
    Escribe `telemetry_live.json` mediante archivo temporal y reemplazo
    atómico `os.replace`, con throttling de I/O para proteger la latencia
    del bucle principal y el almacenamiento en Termux/Linux.
    """

    def __init__(
        self,
        filepath: str = DEFAULT_TELEMETRY_PATH,
        min_write_interval_sec: float = 5.0,
    ) -> None:
        self._lock = threading.Lock()
        self.filepath = os.path.abspath(filepath)
        self.min_write_interval_sec = float(min_write_interval_sec)
        self._last_write_ts: float = 0.0
        self.writes_count: int = 0
        self.write_errors_count: int = 0

    def publish(
        self,
        balance_sats: int,
        equity_sats: int,
        available_sats: int,
        active_trades: int,
        supervisor: LearningQualitySupervisor,
        extra_meta: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> bool:
        """
        Exporta el snapshot de telemetría a `telemetry_live.json` de forma
        atómica. Retorna True si se escribió a disco, False si fue omitido por
        throttling o error de I/O.
        """
        now = time.time()
        if not force and (now - self._last_write_ts) < self.min_write_interval_sec:
            return False

        rl_metrics = supervisor.get_rl_metrics_snapshot()
        payload: Dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "balance_sats": int(round(_safe_float(balance_sats, 0.0))),
            "equity_sats": int(round(_safe_float(equity_sats, 0.0))),
            "available_sats": int(round(_safe_float(available_sats, 0.0))),
            "active_trades": max(0, int(round(_safe_float(active_trades, 0.0)))),
            "rl_metrics": rl_metrics,
        }
        if extra_meta:
            payload["meta"] = extra_meta

        with self._lock:
            parent_dir = os.path.dirname(self.filepath)
            if parent_dir and not os.path.exists(parent_dir):
                try:
                    os.makedirs(parent_dir, exist_ok=True)
                except OSError:
                    pass

            tmp_path = f"{self.filepath}.tmp.{os.getpid()}.{threading.get_ident()}"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, ensure_ascii=False)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, self.filepath)
                self._last_write_ts = now
                self.writes_count += 1
                return True
            except Exception as exc:
                self.write_errors_count += 1
                logger.warning(f"[TELEMETRY-DISPATCHER] Error escribiendo {self.filepath}: {exc}")
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                return False
