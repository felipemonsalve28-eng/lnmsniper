#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sniper_cli_catalog.py
Catálogo matemático, explicaciones operativas (subir vs bajar) para los 78 umbrales
de config/learning_thresholds.json + 21 parámetros de runtime y 5 Presets Cuantitativos.
"""
from typing import Any, Dict, Tuple

CATEGORY_TITLES: Dict[str, str] = {
    "expectancy": "Compuerta de Esperanza Matemática Neta e Inferencia Bayesiana",
    "fees": "Estructura de Fricción Operativa y Eficiencia de Comisiones (FER)",
    "td_error": "Convergencia Temporal-Difference (TD-Error) y Estabilidad de Bellman",
    "q_calibration": "Calibración de Satoshis Proyectados (Q-Bias), Clipping y Kelly",
    "policy_entropy": "Diversidad de Política (Entropía de Shannon sobre 9 Acciones)",
    "mfe_mae": "Captura de Excursión Favorable (MFE) y Control de Excursión Adversa (MAE)",
    "reward": "Función de Recompensa Contable en Satoshis Líquidos Reales (Schema v2)",
    "exploration": "Dinámica de Aprendizaje (Alpha, Gamma, Epsilon y Cooldown por FER)",
    "learning_guard": "Escudo Autónomo de Supervisión con Histéresis (Learning Guard)",
    "market_sensitivity": "Sensibilidad Microestructural de Régimen, Slippage y Degradación de Edge",
}

# Formato compacto: clave -> (math_role, impact_up, impact_down)
_RAW_GUIDE: Dict[str, Tuple[str, str, str]] = {
    # 1. EXPECTANCY (12)
    "expectancy.min_expectancy_ratio": (
        "Exige Gross_TP_sats / Total_Cost_sats >= umbral en modo NORMAL (Total_Cost = Taker_RT + Funding + Slippage).",
        "SUBIR (↑): Exige mayor holgura bruta sobre comisiones; reduce frecuencia de trades pero eleva el FER.",
        "BAJAR (↓): Permite recorridos TP más cortos frente a costos; aumenta frecuencia pero eleva riesgo de fee-drag.",
    ),
    "expectancy.min_expectancy_ratio_explorer": (
        "Exige Gross_TP_sats / Total_Cost_sats >= umbral en modo explorador (Testnet).",
        "SUBIR (↑): Hace más selectiva la exploración en Testnet descartando trades con alta fricción relativa.",
        "BAJAR (↓): Acelera el muestreo de estados Q en Testnet aceptando objetivos más cercanos.",
    ),
    "expectancy.min_net_to_cost_ratio": (
        "Exige Net_TP_sats / Total_Cost_sats >= umbral en modo NORMAL (donde Net_TP = Gross_TP - Total_Cost).",
        "SUBIR (↑): Asegura que la ganancia limpia multiplique varias veces las comisiones pagadas.",
        "BAJAR (↓): Autoriza scalps donde la comisión consume mayor proporción del beneficio bruto.",
    ),
    "expectancy.min_net_to_cost_ratio_explorer": (
        "Exige Net_TP_sats / Total_Cost_sats >= umbral cuando EXPLORER_MODE=True (Testnet).",
        "SUBIR (↑): Evita entrenar Testnet con operaciones cuya ganancia neta apenas cubre costos.",
        "BAJAR (↓): Incrementa el volumen de cierres de entrenamiento en Testnet.",
    ),
    "expectancy.min_net_profit_sats": (
        "Cota inferior absoluta en satoshis: Net_TP_sats >= min_net_profit_sats en modo NORMAL.",
        "SUBIR (↑): Impide abrir micro-posiciones cuyo beneficio neto proyectado sea trivial en satoshis.",
        "BAJAR (↓): Facilita operar con cuentas pequeñas o márgenes mínimos de 1,000 sats.",
    ),
    "expectancy.min_net_profit_sats_explorer": (
        "Piso absoluto de ganancia neta proyectada (Net_TP_sats) en modo explorador (Testnet).",
        "SUBIR (↑): Filtra órdenes de prueba con beneficio neto insignificante.",
        "BAJAR (↓): Permite disparar órdenes de aprendizaje con lote mínimo de 1,000 sats y bajo apalancamiento.",
    ),
    "expectancy.min_net_profit_sats_recovery": (
        "Piso absoluto exigido a Net_TP_sats cuando el bot opera en modo RECOVERY por drawdown.",
        "SUBIR (↑): Exige que cada entrada en recuperación aporte satoshis sustanciales para salir del bache.",
        "BAJAR (↓): Facilita hallar entradas en RECOVERY cuando el margen está reducido por RECOVERY_RISK_MULT.",
    ),
    "expectancy.min_net_rr": (
        "Relación Riesgo-Beneficio neta post-comisiones: Net_TP_sats / Net_SL_Loss_sats >= min_net_rr en modo NORMAL.",
        "SUBIR (↑): Reduce el win rate de equilibrio p* necesario para ser rentable exigiendo mayor asimetría TP/SL.",
        "BAJAR (↓): Permite estrategias de reversión rápida con TP más corto que el SL (requiere alto win rate).",
    ),
    "expectancy.min_net_rr_explorer": (
        "Relación Net_TP_sats / Net_SL_Loss_sats mínima en modo explorador (Testnet).",
        "SUBIR (↑): Enforza asimetría riesgo-beneficio estricta durante la exploración en Testnet.",
        "BAJAR (↓): Permite evaluar estilos DEF_SCALP y REVERSION con TP conservador en Testnet.",
    ),
    "expectancy.breakeven_safety_margin": (
        "Margen probabilístico sobre equilibrio: exige p_LCB >= p* + margen, con p* = (L + C) / (G + L).",
        "SUBIR (↑): Exige mayor certeza estadística Bayesiana de superar el punto de equilibrio p* antes de abrir.",
        "BAJAR (↓): Relaja el colchón probabilístico permitiendo operar celdas (s,a) con menor historial.",
    ),
    "expectancy.bayesian_prior_alpha": (
        "Hiperparámetro alpha_0 (pseudo-aciertos) del prior Beta(alpha_0, beta_0) para win rate jerárquico.",
        "SUBIR (↑): Eleva el win rate a priori asumido en estados nuevos, facilitando la aprobación inicial.",
        "BAJAR (↓): Hace más escéptico al estimador Bayesiano hasta acumular victorias reales.",
    ),
    "expectancy.bayesian_prior_beta": (
        "Hiperparámetro beta_0 (pseudo-fallos) del prior Beta(alpha_0, beta_0) (WR_prior = alpha_0 / (alpha_0 + beta_0)).",
        "SUBIR (↑): Reduce el win rate a priori exigiendo más victorias empíricas para superar p*.",
        "BAJAR (↓): Disminuye el peso de los pseudo-fallos iniciales acelerando la convergencia a la muestra.",
    ),
    # 2. FEES (7)
    "fees.taker_fee_rate": (
        "[BLOQUEO ESTRUCTURAL] Tasa Taker por lado en LN Markets API v3 (0.001 = 0.10% del nocional).",
        "SUBIR (↑): Incrementa el costo modelado por lado endureciendo breakeven y compuertas de entrada.",
        "BAJAR (↓): No reducir salvo cambio oficial de tarifas en LN Markets (subestimaría costos reales).",
    ),
    "fees.round_trip_fee_rate": (
        "[BLOQUEO ESTRUCTURAL] Tasa combinada apertura + cierre Taker (2 * taker_fee_rate = 0.20%).",
        "SUBIR (↑): Aumenta la reserva de fricción en el sizing por Stop Loss y en verify_net_sats_expectancy.",
        "BAJAR (↓): Disminuye la fricción round-trip modelada (mantener siempre en 2x taker_fee_rate).",
    ),
    "fees.funding_rate_8h_est": (
        "Tasa estimada de carry/funding por ventana de 8 horas sumada preventivamente al costo proyectado.",
        "SUBIR (↑): Penaliza entradas que proyectan sostenerse abiertas largo tiempo.",
        "BAJAR (↓): Reduce la reserva preventiva de funding en scalps rápidos.",
    ),
    "fees.fer_high_threshold": (
        "Umbral de Fee-Efficiency Ratio (FER = Sum(Net_Sats)/Sum(Fees_Sats) >= umbral) para HIGH_EFFICIENCY.",
        "SUBIR (↑): Exige mayor múltiplo de ganancia neta sobre comisiones para declarar eficiencia óptima.",
        "BAJAR (↓): Clasifica antes la operativa en estado de alta eficiencia.",
    ),
    "fees.fer_moderate_threshold": (
        "Cota inferior de eficiencia moderada; si FER < umbral por N trades activa fer_expectancy_boost.",
        "SUBIR (↑): Activa más temprano el escudo contra fee-drag subiendo la exigencia de expectancy.",
        "BAJAR (↓): Tolera un FER más ajustado antes de endurecer automáticamente las entradas.",
    ),
    "fees.fer_warning_threshold": (
        "Umbral crítico de FER (1.0); debajo de este valor se activa fer_cooldown_mult y alerta CAPITAL_DESTRUCTION.",
        "SUBIR (↑): Dispara antes la multiplicación del cooldown entre operaciones cuando el FER se debilita.",
        "BAJAR (↓): Retrasa la activación del freno de enfriamiento por erosión de comisiones.",
    ),
    "fees.fer_window_size": (
        "Tamaño de la ventana móvil de operaciones cerradas para computar el FER reciente.",
        "SUBIR (↑): Suaviza el FER móvil otorgándole mayor memoria histórica.",
        "BAJAR (↓): Hace que el FER reaccione de inmediato a las últimas pocas operaciones cerradas.",
    ),
    # 3. TD_ERROR (7)
    "td_error.window_size": (
        "Tamaño de ventana móvil de actualizaciones Q para media |delta_t| y pendiente lineal.",
        "SUBIR (↑): Evalúa la estabilidad de Bellman sobre un horizonte más amplio de transiciones.",
        "BAJAR (↓): Aumenta la reactividad del monitor ante saltos bruscos recientes de TD-Error.",
    ),
    "td_error.warmup_min_samples": (
        "Mínimo de actualizaciones Q antes de abandonar el estado WARMING_UP en TD-Error.",
        "SUBIR (↑): Otorga más margen inicial tras arrancar antes de emitir alertas por promedio.",
        "BAJAR (↓): Clasifica la convergencia Q desde las primeras transiciones.",
    ),
    "td_error.stable_threshold": (
        "Cota superior de la media móvil |delta_t| para declarar convergencia STABLE.",
        "SUBIR (↑): Considera estable la Q-table incluso con variaciones moderadas de recompensa.",
        "BAJAR (↓): Exige errores de predicción muy bajos para reportar estado STABLE.",
    ),
    "td_error.warning_threshold": (
        "Umbral de |delta_t| para estado WARNING y cota requerida para recuperar la histéresis del Learning Guard.",
        "SUBIR (↑): Facilita cumplir la histéresis de recuperación para reactivar Q-updates online.",
        "BAJAR (↓): Exige transiciones muy precisas para levantar una suspensión del Learning Guard.",
    ),
    "td_error.critical_threshold": (
        "Umbral de divergencia severa |delta_t| >= critical_threshold que alimenta la suspensión del Learning Guard.",
        "SUBIR (↑): Tolera sorpresas extremas de mercado sin congelar las actualizaciones de la Q-table.",
        "BAJAR (↓): Congela más rápido el aprendizaje online y toma snapshot ante errores altos de predicción.",
    ),
    "td_error.improving_slope_threshold": (
        "Pendiente negativa d|delta_t|/dt <= umbral para clasificar tendencia IMPROVING.",
        "SUBIR (↑, acercar a 0): Reconoce como IMPROVING descensos suaves del error de Bellman.",
        "BAJAR (↓, más negativo): Exige una reducción rápida del TD-Error para declarar mejora.",
    ),
    "td_error.diverging_slope_threshold": (
        "Pendiente positiva d|delta_t|/dt > umbral que alerta divergencia progresiva.",
        "SUBIR (↑): Disminuye avisos preventivos por incrementos lentos en el error temporal.",
        "BAJAR (↓): Detecta al instante cualquier pendiente creciente en |delta_t|.",
    ),
    # 4. Q_CALIBRATION (8)
    "q_calibration.window_size": (
        "Ventana móvil de cierres para estimar Q_bias = Q_entry_sats - R_econ_sats.",
        "SUBIR (↑): Promedia el sesgo de calibración en satoshis sobre una muestra más extensa.",
        "BAJAR (↓): Detecta más rápido el sobreoptimismo reciente de la tabla Q.",
    ),
    "q_calibration.warmup_min_samples": (
        "Mínimo de cierres requeridos antes de activar diagnósticos de Q-Bias.",
        "SUBIR (↑): Evita penalizar expectancy por sesgo durante las primeras operaciones.",
        "BAJAR (↓): Activa la auditoría de calibración desde los primeros trades.",
    ),
    "q_calibration.q_to_sats_scale": (
        "[BLOQUEO ESTRUCTURAL] Factor de conversión Q_entry_sats = Q(s,a) * q_to_sats_scale (sincronizado con reward_scale_sats).",
        "SUBIR (↑): Incrementa los satoshis proyectados por cada unidad Q (mantener igual a reward_scale_sats).",
        "BAJAR (↓): Reduce la proyección en satoshis por unidad Q.",
    ),
    "q_calibration.overestimation_alert_sats": (
        "Umbral positivo en satoshis que diagnostica OVERESTIMATING y suma q_bias_expectancy_step.",
        "SUBIR (↑): Tolera mayor optimismo en las predicciones Q antes de endurecer entradas.",
        "BAJAR (↓): Penaliza rápido cualquier sobreestimación subiendo el min_expectancy_ratio efectivo.",
    ),
    "q_calibration.underestimation_alert_sats": (
        "Umbral negativo en satoshis que diagnostica UNDERESTIMATING (ganancia real supera predicción Q).",
        "SUBIR (↑, acercar a -1): Detecta antes cuando la política gana más satoshis de los que predice.",
        "BAJAR (↓, más negativo): Solo reporta subestimación ante diferencias amplias a favor.",
    ),
    "q_calibration.q_clip_min": (
        "[BLOQUEO ESTRUCTURAL] Cota inferior dura de valores Q(s,a) y recompensas moldeadas.",
        "SUBIR (↑, acercar a 0): Limita el castigo máximo acumulable en una acción.",
        "BAJAR (↓, más negativo): Permite enterrar más profundo las acciones perdedoras.",
    ),
    "q_calibration.q_clip_max": (
        "[BLOQUEO ESTRUCTURAL] Cota superior dura de valores Q(s,a) para impedir explosión numérica.",
        "SUBIR (↑): Amplía el techo de valoración Q para estrategias muy ganadoras.",
        "BAJAR (↓): Acota la valoración máxima asignable a cualquier acción.",
    ),
    "q_calibration.fractional_kelly_scale": (
        "Escala fraccionaria aplicada al criterio de Kelly f* = (p_hat*b - q)/b en el sizing (0.25 = 1/4 Kelly).",
        "SUBIR (↑): Escala con más fuerza el margen en satoshis cuando hay alta ventaja Bayesiana (mayor crecimiento y varianza).",
        "BAJAR (↓): Aplana el tamaño de posición hacia el riesgo base conservador reduciendo el drawdown.",
    ),
    # 5. POLICY_ENTROPY (6)
    "policy_entropy.window_size": (
        "Ventana móvil de decisiones para calcular la entropía de Shannon H(pi) sobre las 9 acciones.",
        "SUBIR (↑): Evalúa la diversidad estratégica de la política en un horizonte más largo.",
        "BAJAR (↓): Detecta más rápido fijaciones de corto plazo en una sola acción.",
    ),
    "policy_entropy.warmup_min_samples": (
        "Mínimo de decisiones antes de emitir diagnósticos de colapso de entropía.",
        "SUBIR (↑): Evita alertas de baja diversidad recién arrancado el bot.",
        "BAJAR (↓): Audita la concentración de decisiones desde los primeros ciclos.",
    ),
    "policy_entropy.num_actions": (
        "[BLOQUEO ESTRUCTURAL INVIOLABLE] Cardinalidad fija de 9 acciones discretas (0: HOLD, 1..4: LONG, 5..8: SHORT).",
        "INTOCABLE: Fijo en 9 por Regla de Oro #1 de AGENT_MAINTENANCE_DIRECTIVE.md.",
        "INTOCABLE: Fijo en 9 por Regla de Oro #1 de AGENT_MAINTENANCE_DIRECTIVE.md.",
    ),
    "policy_entropy.min_normalized_entropy": (
        "Entropía normalizada mínima H_norm = H(pi)/ln(9) en [0, 1] debajo de la cual se alerta colapso.",
        "SUBIR (↑): Exige mayor distribución entre las 9 acciones para no reportar concentración.",
        "BAJAR (↓): Tolera políticas altamente concentradas sin lanzar alerta.",
    ),
    "policy_entropy.healthy_normalized_entropy": (
        "Referencia de entropía normalizada saludable exportada en telemetry_live.json.",
        "SUBIR (↑): Eleva el objetivo de diversidad de referencia en telemetría.",
        "BAJAR (↓): Ajusta la referencia para mercados donde predomina esperar en HOLD.",
    ),
    "policy_entropy.max_dominant_action_freq": (
        "Frecuencia máxima permitida a una sola acción antes de diagnosticar PARALYSIS_HOLD o MONOPOLY_COLLAPSE.",
        "SUBIR (↑): Permite mayor porcentaje de decisiones HOLD en mercados lentos sin emitir alerta.",
        "BAJAR (↓): Alerta más temprano cuando una única estrategia domina la ventana.",
    ),
    # 6. MFE_MAE (5)
    "mfe_mae.window_size": (
        "Ventana móvil de cierres para promediar captura MFE (Realized/MFE) y estrés MAE (MAE/Margin).",
        "SUBIR (↑): Estabiliza las métricas de excursión sobre más operaciones cerradas.",
        "BAJAR (↓): Hace más reactivo el ajuste dinámico del trailing stop ante los últimos cierres.",
    ),
    "mfe_mae.target_mfe_capture_ratio": (
        "Objetivo de referencia de captura de excursión máxima favorable (Realized_Sats / MFE_Sats).",
        "SUBIR (↑): Eleva la meta de eficiencia de cosecha en los reportes de supervisión.",
        "BAJAR (↓): Adapta la referencia para estrategias RUNNER que dan más respiro al precio.",
    ),
    "mfe_mae.min_mfe_capture_warn": (
        "Umbral inferior de captura MFE (< 0.40) que activa trailing_tighten_factor tras N operaciones.",
        "SUBIR (↑): Ciñe antes los trailing stops cuando las posiciones devuelven ganancia flotante.",
        "BAJAR (↓): Permite mayor retroceso desde el pico flotante sin estrechar el trailing stop.",
    ),
    "mfe_mae.max_mae_margin_ratio_warn": (
        "Umbral máximo tolerable para MAE_sats / Margin_sats promedio antes de ceñir el trailing stop.",
        "SUBIR (↑): Tolera mayor drawdown intratrade sobre el margen aislado sin activar defensa MFE/MAE.",
        "BAJAR (↓): Activa la contracción defensiva del trailing stop ante excursiones adversas moderadas.",
    ),
    "mfe_mae.trailing_tighten_factor": (
        "Multiplicador (< 1.0) aplicado a trail_trig_atr y trail_slack_atr cuando se activa la defensa MFE/MAE.",
        "SUBIR (↑, acercar a 1.0): Deja más holgura al trailing stop respecto al precio spot.",
        "BAJAR (↓): Asegura breakeven y acerca el stop loss mucho más rápido para proteger satoshis.",
    ),
    # 7. REWARD (6)
    "reward.mae_penalty_lambda": (
        "Peso lambda en R_econ_sats = Delta_Balance - lambda*MAE_sats - mu*Delta_t_ticks.",
        "SUBIR (↑): Penaliza en la Q-table las operaciones que sufren gran drawdown intratrade aunque cierren positivas.",
        "BAJAR (↓): Califica las acciones casi exclusivamente por los satoshis netos liquidados.",
    ),
    "reward.holding_time_penalty_mu": (
        "Costo de oportunidad mu en satoshis por cada tick de capital inmovilizado.",
        "SUBIR (↑): Premia rotaciones rápidas de capital y castiga posiciones estancadas.",
        "BAJAR (↓): Reduce la penalización por duración para favorecer posiciones TREND largas.",
    ),
    "reward.tick_duration_sec": (
        "Segundos por tick para calcular Delta_t_ticks = (t_close - t_open) / tick_duration_sec.",
        "SUBIR (↑): Disminuye el número de ticks computados para una misma duración, suavizando la penalización mu.",
        "BAJAR (↓): Incrementa los ticks contabilizados por minuto aumentando el peso del tiempo en posición.",
    ),
    "reward.reward_scale_sats": (
        "[BLOQUEO ESTRUCTURAL] Escala en satoshis para normalizar R_econ_sats a unidades Q (shaped = R_econ_sats / scale).",
        "SUBIR (↑): Comprime la magnitud de las actualizaciones Q (mantener sincronizado con q_to_sats_scale).",
        "BAJAR (↓): Amplifica el cambio en Q por cada satoshi ganado o perdido.",
    ),
    "reward.loss_penalty_multiplier": (
        "Multiplicador de severidad sobre pérdidas netas reales en balance (1.0 = estimador insesgado).",
        "SUBIR (↑): Castiga con mayor fuerza los stop-outs en la Q-table incrementando la aversión al riesgo.",
        "BAJAR (↓, hasta 1.0): Mantiene simetría contable exacta entre ganancias y pérdidas netas.",
    ),
    "reward.decouple_floating_pnl": (
        "[BLOQUEO ESTRUCTURAL] Bandera P0 (1=activo) que prohíbe mutar la Q-table con PnL flotante abierto.",
        "MANTENER EN 1: Asegura que solo cierres líquidos reales entrenen la Q-table.",
        "BAJAR A 0: Habilita shaping intermedio con fluctuaciones flotantes (no recomendado en Mainnet).",
    ),
    # 8. EXPLORATION (10)
    "exploration.epsilon_initial": (
        "Probabilidad inicial de exploración epsilon en modos no deterministas.",
        "SUBIR (↑): Aumenta la tasa inicial de exploración fuera de Mainnet.",
        "BAJAR (↓): Prioriza explotación desde el inicio.",
    ),
    "exploration.epsilon_min": (
        "Cota inferior de decaimiento de epsilon en modo estándar.",
        "SUBIR (↑): Conserva mayor exploración residual a largo plazo.",
        "BAJAR (↓): Reduce al mínimo la exploración residual tras converger.",
    ),
    "exploration.epsilon_decay": (
        "Factor multiplicativo por cierre: epsilon <- max(epsilon_min, epsilon * epsilon_decay).",
        "SUBIR (↑, acercar a 1.0):Ralentiza el decaimiento de la exploración entre operaciones.",
        "BAJAR (↓): Consume rápido la fase de exploración hacia epsilon_min.",
    ),
    "exploration.epsilon_explorer_initial": (
        "Probabilidad inicial de exploración aleatoria en Testnet (EXPLORER_MODE=True).",
        "SUBIR (↑): Fuerza a Testnet a probar activamente las 8 estrategias operativas en nuevos estados.",
        "BAJAR (↓): Hace que Testnet elija con mayor frecuencia la acción de mayor Q actual.",
    ),
    "exploration.epsilon_explorer_min": (
        "Piso permanente de exploración epsilon en Testnet.",
        "SUBIR (↑): Garantiza descubrimiento continuo de nuevas celdas estado-acción en Testnet.",
        "BAJAR (↓): Permite que Testnet se vuelva más explotador tras acumular experiencia.",
    ),
    "exploration.alpha_learning_rate": (
        "Tasa de aprendizaje alpha en Q(s,a) <- Q(s,a) + alpha * (R - Q(s,a)) durante modo NORMAL.",
        "SUBIR (↑): Adapta rápido los valores Q a los resultados recientes (mayor plasticidad).",
        "BAJAR (↓): Protege la memoria histórica entrenada (18 meses) frente a ruido de corto plazo.",
    ),
    "exploration.alpha_recovery_rate": (
        "Tasa de aprendizaje alpha aplicada cuando el bot se encuentra en modo RECOVERY.",
        "SUBIR (↑): Descarta rápidamente en la Q-table cualquier estrategia que falle durante un drawdown.",
        "BAJAR (↓): Evita que pérdidas en modo recuperación alteren bruscamente pesos Q consolidados.",
    ),
    "exploration.gamma_discount": (
        "[BLOQUEO ESTRUCTURAL] Factor de descuento temporal de Bellman para ciclos macro 1h/4h (en cierres micro gamma_eff=0).",
        "SUBIR (↑): Da más peso a la recompensa futura esperada en las tablas macro q_table_1h y q_table_4h.",
        "BAJAR (↓): Prioriza el rendimiento inmediato del bloque horario actual.",
    ),
    "exploration.mainnet_epsilon": (
        "Tasa de exploración aleatoria en Mainnet (0.0 = política 100% determinista/greedy sobre LCB).",
        "SUBIR (↑): Permite un margen mínimo de prueba aleatoria en estados con < 25 visitas en Mainnet.",
        "MANTENER EN 0.0: Cero órdenes aleatorias con satoshis reales.",
    ),
    "exploration.fer_cooldown_mult": (
        "Multiplicador aplicado al cooldown de entrada cuando el FER móvil cae por debajo de 1.0.",
        "SUBIR (↑): Frena en seco la cadencia de aperturas cuando las comisiones superan la ganancia neta.",
        "BAJAR (↓): Mantiene el ritmo normal de disparo aunque el FER reciente sea menor a 1.0.",
    ),
    # 9. LEARNING_GUARD (8)
    "learning_guard.critical_td_streak_to_suspend": (
        "Updates consecutivos con TD-Error CRITICAL requeridos para suspender Q-updates online y guardar snapshot.",
        "SUBIR (↑): Tolera más errores extremos seguidos antes de congelar la Q-table.",
        "BAJAR (↓): Activa de inmediato el escudo de suspensión ante anomalías de recompensa.",
    ),
    "learning_guard.hysteresis_recovery_updates": (
        "Eventos estables consecutivos requeridos por histéresis para levantar defensas activas del supervisor.",
        "SUBIR (↑): Exige una racha más larga de normalidad antes de restaurar umbrales base o Q-updates.",
        "BAJAR (↓): Desactiva más rápido los escudos defensivos tras estabilizarse las métricas.",
    ),
    "learning_guard.q_bias_persistence_trades": (
        "Trades consecutivos con sobreestimación Q > overestimation_alert_sats para activar q_bias_expectancy_step.",
        "SUBIR (↑): Requiere mayor persistencia del sesgo antes de elevar el filtro de expectancy.",
        "BAJAR (↓): Endurece la compuerta de entrada a la primera señal de optimismo excesivo en Q.",
    ),
    "learning_guard.q_bias_expectancy_step": (
        "Incremento sumado a min_expectancy_ratio cuando se activa la defensa por sobreestimación Q-Bias.",
        "SUBIR (↑): Exige un salto fuerte en beneficio/costo mientras la Q-table esté sobreestimando.",
        "BAJAR (↓): Aplica una corrección moderada al umbral de expectancy.",
    ),
    "learning_guard.fer_persistence_trades": (
        "Trades consecutivos con FER < fer_moderate_threshold para activar fer_expectancy_boost.",
        "SUBIR (↑): Da más margen antes de activar el recargo de expectancy por baja eficiencia de comisiones.",
        "BAJAR (↓): Activa rápido el filtro anti-fee-drag ante caídas del FER.",
    ),
    "learning_guard.fer_expectancy_boost": (
        "Incremento sumado a min_expectancy_ratio cuando está activa la defensa contra fee-drag (FER bajo).",
        "SUBIR (↑): Bloquea con severidad entradas cortas hasta que se recupere el ratio ganancia/comisión.",
        "BAJAR (↓): Suaviza el recargo de expectancy durante episodios de bajo FER.",
    ),
    "learning_guard.entropy_persistence_ticks": (
        "Decisiones consecutivas con baja entropía antes de confirmar alerta de colapso de política.",
        "SUBIR (↑): Evita alertas por rachas breves de repetición de acción.",
        "BAJAR (↓): Notifica más rápido cuando una sola acción acapara las decisiones.",
    ),
    "learning_guard.mfe_mae_persistence_trades": (
        "Trades consecutivos con baja captura MFE o alto MAE/Margin para activar trailing_tighten_factor.",
        "SUBIR (↑): Exige más cierres ineficientes seguidos antes de estrechar el trailing stop.",
        "BAJAR (↓): Ciñe los trailing stops rápidamente tras pocos cierres con retroceso adverso.",
    ),
    # 10. MARKET_SENSITIVITY (9)
    "market_sensitivity.atr_extreme_pct": (
        "ATR porcentual (ATR/Precio) que clasifica EXTREME_VOLATILITY y sincroniza Config.ATR_EXTREME_PCT.",
        "SUBIR (↑): Permite operar con mayor volatilidad antes de pasar a DEFENSIVE o reducir tamaño a 0.35x.",
        "BAJAR (↓): Activa más temprano el escudo de volatilidad extrema protegiendo el portafolio en sacudidas.",
    ),
    "market_sensitivity.squeeze_bb_width_pct": (
        "Ancho porcentual máximo de Bandas de Bollinger para declarar compresión RANGE_SQUEEZE.",
        "SUBIR (↑): Detecta antes los mercados comprimidos aplicando límites de apalancamiento (<=32x) y filtro anti-chop.",
        "BAJAR (↓): Solo clasifica RANGE_SQUEEZE cuando las bandas están extremadamente estrechas.",
    ),
    "market_sensitivity.expansion_atr_rel_ratio": (
        "Ratio mínimo ATR_14 / SMA_50(TR) para confirmar régimen VOLATILITY_EXPANSION.",
        "SUBIR (↑): Exige un estallido de rango más fuerte sobre su media para declarar expansión.",
        "BAJAR (↓): Reconoce antes el inicio de rupturas de volatilidad.",
    ),
    "market_sensitivity.max_slippage_bps_normal": (
        "Slippage estimado en puntos básicos (bps) sumado al costo proyectado en regímenes normales.",
        "SUBIR (↑): Incrementa el colchón de fricción exigido para aprobar entradas en mercado normal.",
        "BAJAR (↓): Asume menor deslizamiento de precio al ejecutar órdenes a mercado.",
    ),
    "market_sensitivity.max_slippage_bps_volatile": (
        "Slippage estimado en puntos básicos (bps) aplicado en VOLATILITY_EXPANSION y EXTREME_VOLATILITY.",
        "SUBIR (↑): Filtra con rigor entradas durante velas rápidas donde el spread/slippage suele ampliarse.",
        "BAJAR (↓): Relaja la penalización por deslizamiento en alta volatilidad.",
    ),
    "market_sensitivity.edge_degradation_warn": (
        "Magnitud mapeada a negativo (-|val|) para emitir alerta preventiva cuando el Edge en vivo cae.",
        "SUBIR (↑): Retrasa los avisos en consola por pérdida leve de ventaja en posiciones abiertas.",
        "BAJAR (↓): Alerta más temprano cuando el Edge técnico de una posición empieza a debilitarse.",
    ),
    "market_sensitivity.edge_degradation_tighten": (
        "Magnitud mapeada a negativo (-|val|) donde el Guardián comprime trailing stop y cosecha un 20% (sys_scale*=0.80).",
        "SUBIR (↑): Otorga más espacio a la posición antes de apretar defensivamente su stop por caída de Edge.",
        "BAJAR (↓): Ajusta stops más rápido cuando el contexto técnico pierde fuerza.",
    ),
    "market_sensitivity.edge_degradation_exit": (
        "Magnitud crítica mapeada a negativo (-|val|) que ejecuta FORCED_EDGE_EXIT en posiciones no cubiertas.",
        "SUBIR (↑): Evita cierres anticipados por deterioro de Edge dejando actuar al SL o al Hedge Engine.",
        "BAJAR (↓): Corta rápidamente pérdidas o protege ganancias apenas el Edge cruza a terreno muy negativo.",
    ),
    "market_sensitivity.edge_degradation_persistence": (
        "Ciclos consecutivos requeridos con Edge degradado antes de ejecutar cosecha defensiva o salida forzada.",
        "SUBIR (↑): Evita cerrar posiciones por un único tick de ruido adverso.",
        "BAJAR (↓): Ejecuta la salida defensiva tras menos confirmaciones consecutivas.",
    ),
    "liquidity_radar.enabled": (
        "Habilita el cálculo en memoria del radar causal de liquidez Sweep+Reclaim (5m/15m) sin I/O adicional.",
        "SUBIR (1): Activa el monitoreo de pools EQH/EQL y eventos de barrida.",
        "BAJAR (0): Desactiva completamente el radar devolviendo señal neutra.",
    ),
    "liquidity_radar.shadow_only": (
        "Mantiene el radar en modo observador puro: registra telemetría sin modificar Pre-Flight ni bloquear muros.",
        "SUBIR (1): Modo sombra seguro (liq_bias=0.0, cero impacto en ejecución).",
        "BAJAR (0): Activa el sesgo acotado liq_bias en Pre-Flight y bloqueo ante muro EQH/EQL.",
    ),
    "liquidity_radar.swing_k_5m": (
        "Orden causal k de pivotes Swing en velas de 5m (bloqueado con posiciones abiertas).",
        "SUBIR (↑): Detecta únicamente pivotes más amplios y estructurales (menos señales).",
        "BAJAR (↓): Detecta swings locales más rápido con mayor frecuencia de pools.",
    ),
    "liquidity_radar.swing_k_15m": (
        "Orden causal k de pivotes Swing en velas de 15m (bloqueado con posiciones abiertas).",
        "SUBIR (↑): Filtra ruido intradía y prioriza niveles macro de 15m.",
        "BAJAR (↓): Acelera la confirmación causal de pivotes en 15m.",
    ),
    "liquidity_radar.cluster_atr_mult": (
        "Distancia máxima (en múltiplos de ATR) para fusionar swings cercanos en un cluster EQH/EQL.",
        "SUBIR (↑): Agrupa pivotes más dispersos generando clusters con mayor peso.",
        "BAJAR (↓): Exige máximos/mínimos casi idénticos para clasificar EQH/EQL.",
    ),
    "liquidity_radar.mitigation_atr_mult": (
        "Cierre mínimo más allá del pool (en ATR) para considerarlo mitigado y retirarlo del radar.",
        "SUBIR (↑): Mantiene pools activos ante cierres marginales cercanos al nivel.",
        "BAJAR (↓): Descarta más rápido pools que fueron atravesados al cierre.",
    ),
    "liquidity_radar.min_pierce_atr": (
        "Penetración mínima de la mecha más allá del pool (en ATR) para reconocer una barrida real.",
        "SUBIR (↑): Exige barridas más profundas descartando simples toques del nivel.",
        "BAJAR (↓): Permite detectar barridas superficiales en rangos estrechos.",
    ),
    "liquidity_radar.max_pierce_atr": (
        "Penetración máxima permitida (en ATR) para descartar rupturas violentas y cuchillos cayendo.",
        "SUBIR (↑): Tolera mechas más extensas en spikes de alta volatilidad.",
        "BAJAR (↓): Filtra con mayor rigor cualquier ruptura excesiva del nivel.",
    ),
    "liquidity_radar.min_wick_ratio_close": (
        "Fracción mínima de mecha de rechazo respecto al rango total en velas cerradas.",
        "SUBIR (↑): Exige rechazo agresivo y cierre limpio lejos del extremo.",
        "BAJAR (↓): Acepta velas de rechazo con cuerpo más amplio.",
    ),
    "liquidity_radar.min_wick_ratio_intrabar": (
        "Fracción mínima de mecha de rechazo exigida en la vela viva para señal intrabar.",
        "SUBIR (↑): Reduce falsos positivos antes del cierre de vela.",
        "BAJAR (↓): Anticipa entradas intrabar más temprano tras el rebote.",
    ),
    "liquidity_radar.max_liq_bonus": (
        "Bono máximo en unidades Q aplicado en Pre-Flight cuando la operación confluye con un Sweep+Reclaim.",
        "SUBIR (↑): Da mayor peso a entradas respaldadas por barrida de liquidez (nunca vence OVR).",
        "BAJAR (↓): Limita la influencia alcista del radar dejando dominar a la Q-table.",
    ),
    "liquidity_radar.max_liq_penalty": (
        "Penalización máxima en unidades Q aplicada en Pre-Flight al operar contra un Sweep reciente.",
        "SUBIR (↑): Endurece el veto contra entradas que actúan como liquidez de salida.",
        "BAJAR (↓): Suaviza el bloqueo direccional ante barridas opuestas.",
    ),
    "liquidity_radar.min_opposite_wall_fee_ratio": (
        "Ratio mínimo de espacio bruto hasta un muro EQH/EQL opuesto respecto al costo ida-vuelta C_RT.",
        "SUBIR (↑): Exige mayor recorrido libre antes de abrir frente a una resistencia/soporte fuerte.",
        "BAJAR (↓): Permite abrir operaciones más cerca de clusters opuestos.",
    ),
}

PARAM_OPERATIONAL_GUIDE: Dict[str, Dict[str, str]] = {
    k: {"math_role": v[0], "impact_up": v[1], "impact_down": v[2]}
    for k, v in _RAW_GUIDE.items()
}

RUNTIME_PARAMETERS_SPEC: Dict[str, Dict[str, Any]] = {
    "ENV_MODE": {
        "target": "Config", "type": "str", "allowed": ["mainnet", "testnet"], "default": "mainnet", "unit": "mode",
        "description": "Entorno operativo primario conectado a LN Markets API v3 (mainnet real o testnet signet).",
        "math_role": "Selecciona los archivos de cerebro/hedge/telemetría y fija EXPLORER_MODE=False (mainnet) o True (testnet).",
        "impact_up": "MAINNET: Política determinista (epsilon=0.0), tope P0 de 5 posiciones y protección estricta de piso duro.",
        "impact_down": "TESTNET: Modo explorador activo para nutrir la Q-table sin arriesgar satoshis reales.",
    },
    "SHADOW_MODE": {
        "target": "Config", "type": "bool", "default": False, "unit": "bool",
        "description": "Modo Sombra (Fase F7): simula órdenes y cierres contra precio spot real en logs/shadow_signals.jsonl sin enviar POST.",
        "math_role": "Evalúa virtualmente TP y SL en cada tick computando PnL neto en satoshis con 0 riesgo de capital.",
        "impact_up": "TRUE: Permite auditar cualquier preset o configuración en vivo sin comprometer balance real.",
        "impact_down": "FALSE: Ejecuta órdenes reales aisladas en LN Markets API v3.",
    },
    "SAFE_MODE": {
        "target": "RuntimeFlag", "type": "bool", "default": False, "unit": "bool",
        "description": "Modo Seguro: mantiene activos el Guardián, el Hedge Engine y cierres, pero bloquea nuevas entradas.",
        "math_role": "Modo de drenaje ordenado (drain mode) para gestionar únicamente las posiciones ya abiertas hasta quedar en 0.",
        "impact_up": "TRUE: Impide abrir nuevas posiciones mientras protege las actuales.",
        "impact_down": "FALSE: Operación autónoma completa.",
    },
    "ENABLE_HYBRID_HEDGE": {
        "target": "Config", "type": "bool", "default": True, "unit": "bool",
        "description": "Habilita o deshabilita el motor de coberturas híbridas (AdaptiveHedgeController de 7 acciones).",
        "math_role": "Autoriza coberturas cuando C_hedge_net < 0 y el ahorro esperado supera >= 1.45x las comisiones round-trip.",
        "impact_up": "TRUE: Neutraliza giros adversos evitando stop-outs directos en posiciones primarias.",
        "impact_down": "FALSE: Las posiciones primarias operan únicamente con su Stop Loss y Guardián táctico.",
    },
    "MAX_CONCURRENT_TRADES": {
        "target": "Config", "type": "int", "min_limit": 1, "max_limit": 5, "default": 5, "unit": "positions",
        "description": "[PROTECCIÓN P0] Máximo de posiciones primarias simultáneas (tope inquebrantable de 5 en Mainnet).",
        "math_role": "Acota la cardinalidad del portafolio primario en enforce_concurrency_cap y dynamic_max_pos.",
        "impact_up": "SUBIR (hasta 5): Permite distribuir entradas concurrentes cuando el capital y margen libre lo permiten.",
        "impact_down": "BAJAR (1..4): Reduce el número máximo de posiciones abiertas al mismo tiempo.",
    },
    "SOFT_EXPOSURE_LIMIT": {
        "target": "Config", "type": "float", "min_limit": 0.10, "max_limit": 0.45, "default": 0.38, "unit": "ratio",
        "description": "Fracción máxima del Equity que puede estar bloqueada en margen aislado para permitir nuevas entradas.",
        "math_role": "Exige (Locked_Margin + New_Margin) / Equity <= SOFT_EXPOSURE_LIMIT.",
        "impact_up": "SUBIR (↑): Permite utilizar mayor porcentaje del capital en posiciones simultáneas.",
        "impact_down": "BAJAR (↓): Reserva un mayor colchón de satoshis libres para coberturas y seguridad.",
    },
    "MAX_SINGLE_TRADE_MARGIN_PCT": {
        "target": "Config", "type": "float", "min_limit": 0.05, "max_limit": 0.25, "default": 0.20, "unit": "ratio",
        "description": "Fracción máxima del Equity asignable como margen aislado a una sola operación.",
        "math_role": "Acota Margin_sats <= round(Equity * MAX_SINGLE_TRADE_MARGIN_PCT).",
        "impact_up": "SUBIR (↑): Permite asignar más colateral a una operación individual con bajo apalancamiento.",
        "impact_down": "BAJAR (↓): Limita estrictamente el margen máximo por operación.",
    },
    "MIN_LEVERAGE": {
        "target": "Config", "type": "int", "min_limit": 1, "max_limit": 30, "default": 15, "unit": "leverage_x",
        "description": "Apalancamiento mínimo base para posiciones primarias.",
        "math_role": "Cota inferior de apalancamiento en compute_situational_order_params.",
        "impact_up": "SUBIR (↑): Reduce el margen requerido por posición pero acerca el precio de liquidación.",
        "impact_down": "BAJAR (↓): Permite apalancamientos más conservadores con liquidación más lejana.",
    },
    "MAX_LEVERAGE": {
        "target": "Config", "type": "int", "min_limit": 15, "max_limit": 85, "default": 85, "unit": "leverage_x",
        "description": "Apalancamiento máximo techo antes de aplicar reductores situacionales por régimen y drawdown.",
        "math_role": "Cota superior: Lev <= min(MAX_LEVERAGE, safe_lev_cap).",
        "impact_up": "SUBIR (↑): Permite mayor eficiencia de margen en entradas SNIPER de alta convicción.",
        "impact_down": "BAJAR (↓): Reduce el riesgo de liquidación ante mechas bruscas de precio.",
    },
    "RISK_NORMAL": {
        "target": "Config", "type": "float", "min_limit": 0.02, "max_limit": 0.12, "default": 0.06, "unit": "ratio",
        "description": "Parámetro base de riesgo en modo NORMAL (escalado por 0.35x -> [1.2%, 3.5%] de riesgo real por SL).",
        "math_role": "Define el presupuesto de pérdida en satoshis sobre el Stop Loss: risk_budget_sats = Equity * eff_risk_pct.",
        "impact_up": "SUBIR (↑): Incrementa el tamaño en satoshis de las posiciones en modo NORMAL.",
        "impact_down": "BAJAR (↓): Disminuye el riesgo en satoshis asumido en cada entrada.",
    },
    "RISK_WARMUP": {
        "target": "Config", "type": "float", "min_limit": 0.015, "max_limit": 0.08, "default": 0.04, "unit": "ratio",
        "description": "Parámetro base de riesgo reducido mientras total_trained < WARMUP_TRADES.",
        "math_role": "Reemplaza a RISK_NORMAL durante la etapa de calentamiento del cerebro.",
        "impact_up": "SUBIR (↑): Incrementa el tamaño de posición durante los primeros cierres de calentamiento.",
        "impact_down": "BAJAR (↓): Mantiene exposición mínima mientras el cerebro acumula experiencia inicial.",
    },
    "ENTRY_COOLDOWN_SEC": {
        "target": "Config", "type": "float", "min_limit": 10.0, "max_limit": 300.0, "default": 35.0, "unit": "seconds",
        "description": "Tiempo mínimo de enfriamiento en segundos entre dos aperturas consecutivas.",
        "math_role": "Determina effective_cooldown = ENTRY_COOLDOWN_SEC * fer_mult * mode_mult.",
        "impact_up": "SUBIR (↑): Evita abrir múltiples posiciones seguidas en la misma vela de 5m.",
        "impact_down": "BAJAR (↓): Permite capturar señales sucesivas con menor espera.",
    },
    "CONSECUTIVE_LOSS_LIMIT": {
        "target": "Config", "type": "int", "min_limit": 2, "max_limit": 10, "default": 4, "unit": "trades",
        "description": "Pérdidas netas consecutivas que disparan la pausa defensiva automática.",
        "math_role": "Al alcanzar este conteo en Mainnet, activa pause_until = now + CONSECUTIVE_LOSS_PAUSE_SEC.",
        "impact_up": "SUBIR (↑): Tolera más stop-outs seguidos antes de pausar entradas.",
        "impact_down": "BAJAR (↓): Pausa las entradas rápidamente ante las primeras pérdidas seguidas.",
    },
    "CONSECUTIVE_LOSS_PAUSE_SEC": {
        "target": "Config", "type": "int", "min_limit": 60, "max_limit": 3600, "default": 360, "unit": "seconds",
        "description": "Duración en segundos de la pausa defensiva tras alcanzar CONSECUTIVE_LOSS_LIMIT.",
        "math_role": "Tiempo de enfriamiento forzado en estado DEFENSIVE tras una racha adversa.",
        "impact_up": "SUBIR (↑): Otorga más tiempo para que pase la condición adversa de mercado.",
        "impact_down": "BAJAR (↓): Reanuda antes la evaluación de entradas tras la pausa.",
    },
    "BRAIN_SAVE_INTERVAL": {
        "target": "Config", "type": "float", "min_limit": 5.0, "max_limit": 180.0, "default": 30.0, "unit": "seconds",
        "description": "Intervalo de debounce en segundos para guardados ordinarios del cerebro en disco.",
        "math_role": "Evita saturar la memoria flash en Termux omitiendo save_brain(force=False) dentro de la ventana.",
        "impact_up": "SUBIR (↑): Reduce escrituras en disco (los eventos críticos siempre fuerzan force=True).",
        "impact_down": "BAJAR (↓): Guarda cambios ordinarios en disco con mayor frecuencia.",
    },
    "FAST_ANALYZE_INTERVAL_SEC": {
        "target": "Config", "type": "float", "min_limit": 2.0, "max_limit": 15.0, "default": 5.0, "unit": "seconds",
        "description": "Intervalo del ciclo principal cuando hay override direccional activo.",
        "math_role": "Define loop_wait = FAST_ANALYZE_INTERVAL_SEC * 0.85 en impulsos fuertes.",
        "impact_up": "SUBIR (↑): Reduce peticiones HTTP y uso de CPU en Termux.",
        "impact_down": "BAJAR (↓): Aumenta la velocidad de reacción del bucle en mercados rápidos.",
    },
    "ACTIVE_ANALYZE_INTERVAL_SEC": {
        "target": "Config", "type": "float", "min_limit": 3.0, "max_limit": 20.0, "default": 7.0, "unit": "seconds",
        "description": "Intervalo del ciclo cuando existen posiciones abiertas.",
        "math_role": "Define loop_wait = ACTIVE_ANALYZE_INTERVAL_SEC * 0.85 con órdenes activas.",
        "impact_up": "SUBIR (↑): Ahorra batería y red mientras vigila posiciones abiertas.",
        "impact_down": "BAJAR (↓): Evalúa trailing stops y coberturas con mayor frecuencia.",
    },
    "IDLE_ANALYZE_INTERVAL_SEC": {
        "target": "Config", "type": "float", "min_limit": 5.0, "max_limit": 30.0, "default": 11.0, "unit": "seconds",
        "description": "Intervalo del ciclo cuando no hay posiciones abiertas ni override.",
        "math_role": "Espera base del bucle principal estando 100% en liquidez.",
        "impact_up": "SUBIR (↑): Minimiza el consumo en segundo plano en Termux.",
        "impact_down": "BAJAR (↓): Escanea el mercado más seguido buscando entradas.",
    },
    "INITIAL_TARGET_SATS": {
        "target": "Config", "type": "int", "min_limit": 10000, "max_limit": 100000000, "default": 300000, "unit": "sats",
        "description": "Meta inicial de balance disponible en satoshis para el sistema de hitos.",
        "math_role": "Al superarse, asegura locked_floor al 88% de la meta y avanza al siguiente nivel.",
        "impact_up": "SUBIR (↑): Fija un objetivo inicial de balance más alto.",
        "impact_down": "BAJAR (↓): Permite asegurar antes el primer piso del 88% en cuentas pequeñas.",
    },
    "TARGET_STEP_MULTIPLIER": {
        "target": "Config", "type": "float", "min_limit": 1.10, "max_limit": 3.00, "default": 1.50, "unit": "multiplier",
        "description": "Multiplicador geométrico para calcular la siguiente meta tras conquistar el hito actual.",
        "math_role": "Calcula current_target = round(old_target * TARGET_STEP_MULTIPLIER).",
        "impact_up": "SUBIR (↑): Amplía la distancia entre cada nuevo nivel de meta.",
        "impact_down": "BAJAR (↓): Crea escalones de meta más cercanos y progresivos.",
    },
    "MAX_HEDGE_LEVERAGE": {
        "target": "HedgeRiskManager", "type": "int", "min_limit": 5, "max_limit": 50, "default": 35, "unit": "leverage_x",
        "description": "Apalancamiento máximo permitido para las patas de cobertura.",
        "math_role": "Limita hedge_lev <= min(orig_lev, safe_lev_from_atr, MAX_HEDGE_LEVERAGE).",
        "impact_up": "SUBIR (↑): Requiere menos margen libre para abrir una cobertura.",
        "impact_down": "BAJAR (↓): Fuerza coberturas más conservadoras con liquidación más alejada.",
    },
}

QUANTITATIVE_PRESETS: Dict[str, Dict[str, Any]] = {
    "baseline_v73": {
        "title": "Calibración Oficial Certificada LNM Sniper v7.3+ (Monte Carlo Baseline)",
        "regime_target": "Todo terreno / Operación estándar en Mainnet certificada (18 meses histórico).",
        "math_summary": "Equilibrio oficial: min_expectancy_ratio=3.85, breakeven_safety_margin=0.04, 1/4 Kelly (0.25) y política determinista en Mainnet (mainnet_epsilon=0.0).",
        "allow_live_mainnet": True,
        "thresholds": {
            "expectancy.min_expectancy_ratio": 3.85, "expectancy.min_expectancy_ratio_explorer": 2.4,
            "expectancy.min_net_to_cost_ratio": 2.8, "expectancy.min_net_to_cost_ratio_explorer": 1.5,
            "expectancy.min_net_profit_sats": 38, "expectancy.min_net_profit_sats_explorer": 16,
            "expectancy.min_net_profit_sats_recovery": 44, "expectancy.min_net_rr": 1.05,
            "expectancy.min_net_rr_explorer": 0.72, "expectancy.breakeven_safety_margin": 0.04,
            "expectancy.bayesian_prior_alpha": 7.0, "expectancy.bayesian_prior_beta": 15.0,
            "q_calibration.fractional_kelly_scale": 0.25, "mfe_mae.target_mfe_capture_ratio": 0.65,
            "mfe_mae.min_mfe_capture_warn": 0.40, "mfe_mae.max_mae_margin_ratio_warn": 0.50,
            "mfe_mae.trailing_tighten_factor": 0.85, "reward.mae_penalty_lambda": 0.15,
            "reward.holding_time_penalty_mu": 0.05, "reward.loss_penalty_multiplier": 1.0,
            "exploration.alpha_learning_rate": 0.18, "exploration.alpha_recovery_rate": 0.28,
            "exploration.mainnet_epsilon": 0.0, "exploration.fer_cooldown_mult": 2.0,
            "learning_guard.fer_persistence_trades": 3, "learning_guard.fer_expectancy_boost": 0.5,
            "market_sensitivity.atr_extreme_pct": 0.85, "market_sensitivity.squeeze_bb_width_pct": 0.75,
            "market_sensitivity.expansion_atr_rel_ratio": 1.32, "market_sensitivity.max_slippage_bps_normal": 3.0,
            "market_sensitivity.edge_degradation_warn": 0.40, "market_sensitivity.edge_degradation_tighten": 0.60,
            "market_sensitivity.edge_degradation_exit": 0.78, "market_sensitivity.edge_degradation_persistence": 3,
        },
        "runtime": {
            "SOFT_EXPOSURE_LIMIT": 0.38, "MAX_SINGLE_TRADE_MARGIN_PCT": 0.20, "MAX_LEVERAGE": 85,
            "RISK_NORMAL": 0.06, "ENTRY_COOLDOWN_SEC": 35.0, "CONSECUTIVE_LOSS_LIMIT": 4, "ENABLE_HYBRID_HEDGE": True,
        },
    },
    "capital_fortress": {
        "title": "Escudo Institucional de Preservación de Satoshis (Ultra-Defensivo)",
        "regime_target": "Protección de capital acumulado, cercanía a meta o alta incertidumbre.",
        "math_summary": "Sube barrera Bayesiana (breakeven_safety_margin=0.07), exige min_expectancy_ratio=4.60, reduce Kelly a 0.15 y limita apalancamiento a 50x.",
        "allow_live_mainnet": True,
        "thresholds": {
            "expectancy.min_expectancy_ratio": 4.60, "expectancy.min_net_to_cost_ratio": 3.40,
            "expectancy.min_net_profit_sats": 52, "expectancy.min_net_profit_sats_recovery": 60,
            "expectancy.min_net_rr": 1.25, "expectancy.breakeven_safety_margin": 0.07,
            "q_calibration.fractional_kelly_scale": 0.15, "mfe_mae.min_mfe_capture_warn": 0.48,
            "mfe_mae.max_mae_margin_ratio_warn": 0.38, "mfe_mae.trailing_tighten_factor": 0.78,
            "reward.mae_penalty_lambda": 0.25, "reward.loss_penalty_multiplier": 1.25,
            "exploration.mainnet_epsilon": 0.0, "exploration.fer_cooldown_mult": 2.8,
            "learning_guard.fer_persistence_trades": 2, "learning_guard.fer_expectancy_boost": 0.65,
            "market_sensitivity.atr_extreme_pct": 0.72, "market_sensitivity.edge_degradation_warn": 0.32,
            "market_sensitivity.edge_degradation_tighten": 0.50, "market_sensitivity.edge_degradation_exit": 0.68,
            "market_sensitivity.edge_degradation_persistence": 2,
        },
        "runtime": {
            "SOFT_EXPOSURE_LIMIT": 0.25, "MAX_SINGLE_TRADE_MARGIN_PCT": 0.12, "MAX_LEVERAGE": 50,
            "RISK_NORMAL": 0.045, "ENTRY_COOLDOWN_SEC": 55.0, "CONSECUTIVE_LOSS_LIMIT": 3, "ENABLE_HYBRID_HEDGE": True,
        },
    },
    "fee_sniper_anti_chop": {
        "title": "Erradicador de Fee-Drag y Filtro Anti-Chop (Mercado Lateral / Squeeze)",
        "regime_target": "Mercados en compresión (RANGE_SQUEEZE), ruido lateral o FER cercano a 1.2.",
        "math_summary": "Exige min_net_to_cost_ratio=3.60, detecta antes compresión Bollinger (squeeze_bb_width_pct=0.85) y triplica cooldown si FER < 1.0.",
        "allow_live_mainnet": True,
        "thresholds": {
            "expectancy.min_expectancy_ratio": 4.40, "expectancy.min_net_to_cost_ratio": 3.60,
            "expectancy.min_net_profit_sats": 48, "expectancy.min_net_rr": 1.18,
            "expectancy.breakeven_safety_margin": 0.06, "q_calibration.fractional_kelly_scale": 0.20,
            "mfe_mae.trailing_tighten_factor": 0.80, "reward.holding_time_penalty_mu": 0.08,
            "exploration.mainnet_epsilon": 0.0, "exploration.fer_cooldown_mult": 3.0,
            "learning_guard.fer_persistence_trades": 2, "learning_guard.fer_expectancy_boost": 0.75,
            "market_sensitivity.squeeze_bb_width_pct": 0.85, "market_sensitivity.max_slippage_bps_normal": 4.0,
            "market_sensitivity.edge_degradation_warn": 0.34, "market_sensitivity.edge_degradation_tighten": 0.52,
            "market_sensitivity.edge_degradation_exit": 0.70, "market_sensitivity.edge_degradation_persistence": 2,
        },
        "runtime": {
            "SOFT_EXPOSURE_LIMIT": 0.30, "MAX_SINGLE_TRADE_MARGIN_PCT": 0.15, "MAX_LEVERAGE": 60,
            "RISK_NORMAL": 0.05, "ENTRY_COOLDOWN_SEC": 48.0, "CONSECUTIVE_LOSS_LIMIT": 3, "ENABLE_HYBRID_HEDGE": True,
        },
    },
    "trend_expansion_kelly": {
        "title": "Captura de Tendencia Direccional y Expansión de Volatilidad (Runner Mode)",
        "regime_target": "Tendencias limpias 1h/4h (TREND_BULL / TREND_BEAR) y rupturas (VOLATILITY_EXPANSION).",
        "math_summary": "Exige mayor R:R neto (min_net_rr=1.30), potencia Kelly (0.32) en señales con alta ventaja y da mayor holgura al trailing stop (0.90).",
        "allow_live_mainnet": True,
        "thresholds": {
            "expectancy.min_expectancy_ratio": 3.45, "expectancy.min_net_to_cost_ratio": 2.50,
            "expectancy.min_net_profit_sats": 40, "expectancy.min_net_rr": 1.30,
            "expectancy.breakeven_safety_margin": 0.04, "q_calibration.fractional_kelly_scale": 0.32,
            "mfe_mae.target_mfe_capture_ratio": 0.72, "mfe_mae.min_mfe_capture_warn": 0.35,
            "mfe_mae.trailing_tighten_factor": 0.90, "reward.mae_penalty_lambda": 0.12,
            "reward.holding_time_penalty_mu": 0.03, "exploration.mainnet_epsilon": 0.0,
            "market_sensitivity.expansion_atr_rel_ratio": 1.24, "market_sensitivity.edge_degradation_warn": 0.45,
            "market_sensitivity.edge_degradation_tighten": 0.65, "market_sensitivity.edge_degradation_exit": 0.82,
            "market_sensitivity.edge_degradation_persistence": 4,
        },
        "runtime": {
            "SOFT_EXPOSURE_LIMIT": 0.38, "MAX_SINGLE_TRADE_MARGIN_PCT": 0.20, "MAX_LEVERAGE": 80,
            "RISK_NORMAL": 0.065, "ENTRY_COOLDOWN_SEC": 28.0, "CONSECUTIVE_LOSS_LIMIT": 4, "ENABLE_HYBRID_HEDGE": True,
        },
    },
    "testnet_curriculum_lab": {
        "title": "Laboratorio de Exploración Curricular Acelerada (Exclusivo Testnet / Modo Sombra)",
        "regime_target": "Entrenamiento acelerado en Testnet o validación sin riesgo en Modo Sombra.",
        "math_summary": "Eleva exploración en Testnet (epsilon_explorer_initial=0.32, min=0.10) y aprendizaje (alpha=0.22) para poblar estados rápidamente.",
        "allow_live_mainnet": False,
        "thresholds": {
            "expectancy.min_expectancy_ratio_explorer": 2.10, "expectancy.min_net_to_cost_ratio_explorer": 1.25,
            "expectancy.min_net_profit_sats_explorer": 12, "expectancy.min_net_rr_explorer": 0.65,
            "exploration.epsilon_explorer_initial": 0.32, "exploration.epsilon_explorer_min": 0.10,
            "exploration.alpha_learning_rate": 0.22, "exploration.alpha_recovery_rate": 0.30,
        },
        "runtime": {
            "SOFT_EXPOSURE_LIMIT": 0.45, "ENTRY_COOLDOWN_SEC": 18.0, "RISK_NORMAL": 0.08, "ENABLE_HYBRID_HEDGE": True,
        },
    },
}
