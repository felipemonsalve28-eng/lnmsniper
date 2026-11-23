# CONTEXTO OPERATIVO Y MEMORIA PERSISTENTE DEL PROYECTO
## LNM SNIPER v7.3+ (EDICIÓN SATS REALES, OBSERVABILIDAD Y UMBRALES DINÁMICOS)

> **Archivo de Contexto Persistente para Agentes Autónomos e Ingenieros**  
> **Última Actualización:** 2026-11-23  
> **Último Commit Base:** `d133d28` (`master`, Etapa 27 — Refactorización Versionada hacia `C1_STREAK3_24H_INV`)  
> **Directiva de Operación:** [AGENT_MAINTENANCE_DIRECTIVE.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/AGENT_MAINTENANCE_DIRECTIVE.md)

---

## 1. ESTADO ACTUAL DEL SISTEMA

| Componente | Estado | Ubicación / Referencia |
| :--- | :--- | :--- |
| **Rama Git** | `master` (Limpieza de estado operativo / arranque en `NORMAL` desde `git clone`, base `9daa37f`) | `master` |
| **Entorno Primario** | Producción (`mainnet`) | LN Markets API v3 (`https://api.lnmarkets.com/v3`) |
| **Límite de Posiciones** | Máximo 5 posiciones simultáneas | Enforzado en [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py) |
| **Piso Duro Unificado** | 75% inicial -> 88% de meta (preserva hitos post Kill-Switch) | [HardFloorGuardian](file:///c:/Users/mberm/OneDrive/Documentos/sniper/guardian_execution_core.py) |
| **Cerebro Activo** | `360` estados discretizados (`q_table`/`state_visits` reentrenados 18M bajo paridad Etapa 26), `0` cierres/historial (`epsilon=0.0`), Schema v2, SHA-256 estricto (`strict_sha=True`) | [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json) / [hedge_state_mainnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_mainnet.json) (`162` estados Q) |
| **Fuente de Umbrales** | 10 categorías formales con Hot-Reload atómico aislado por entorno (`mainnet` vs `testnet`) | [config/learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json) / [config/learning_thresholds_testnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds_testnet.json) |
| **Telemetría en Vivo** | Headless atómica con FileLock exclusivo y aislamiento Mainnet/Testnet | [telemetry_live.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/telemetry_live.json) / `telemetry_live_testnet.json` |
| **Compatibilidad Móvil** | Cero dependencias C/C++ pesadas, $O(1)$ en memoria | Android Termux y Linux/Ubuntu |

---

## 2. REGISTRO CRONOLÓGICO DE ETAPAS EJECUTADAS

### Etapa 1: Auditoría Técnica y Diagnóstico Pre-Modificación
- **Problema detectado:** Autoengaño de la IA con valores Q inflados por bonos aditivos (`+0.28`, `+0.22`), cálculo asimétrico de comisiones al cerrar posiciones, y contaminación de la Q-table por fluctuaciones flotantes de ATR (1,189 updates flotantes vs 336 updates reales de cierre).
- **Resultado:** Diagnóstico aprobado por el usuario antes de editar código.

### Etapa 2: Centralización Externa de Umbrales con Hot-Reload Atómico
- **Implementación:** Creación de [config/learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json) bajo 10 categorías formales (`expectancy`, `fees`, `td_error`, `q_calibration`, `policy_entropy`, `mfe_mae`, `reward`, `exploration`, `learning_guard`, `market_sensitivity`).
- **Manejador:** Creación de [learning_thresholds_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_thresholds_manager.py) con validación estricta de esquema/límites, rechazo de corrupción JSON o valores fuera de rango, y bloqueo de parámetros estructurales en caliente cuando `active_trades > 0`.

### Etapa 3: Refactorización Contable de la Función de Recompensa
- **Implementación:** Reemplazo de bonos cosméticos por la fórmula matemática real:
  $$R_{\text{sats}} = \Delta \text{Balance\_disponible} - \lambda(\text{MAE\_sats}) - \mu(\Delta t_{\text{ticks}})$$
- **Reglas Contables:**
  - Cero doble deducción de comisiones al conciliar con la API v3 de LN Markets.
  - Toda pérdida neta real en balance penaliza obligatoriamente con gradiente negativo a la acción Q.
  - Desacople estricto de PnL flotante (`decouple_floating_pnl == 1`): fluctuaciones abiertas no tocan la Q-table.
  - Idempotencia por `trade_id` en búfer circular para prevenir dobles actualizaciones `(S, A, R, S')`.
  - Trazabilidad auditable completa en `transition_audit_log`.

### Etapa 4: Motor de Supervisión y Calidad de Aprendizaje en Tiempo Real
- **Implementación:** Creación de [learning_quality_supervisor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py) con 5 indicadores matemáticos $O(1)$:
  1. **TD-Error y Learning Guard:** Suspensión de updates online ante divergencia crítica sostenida (3 updates consecutivos), guardado de snapshot de emergencia e histéresis de 5 updates estables para reactivación. Exploración $\epsilon$ preservada.
  2. **Q-Bias en Satoshis:** Monitoreo del sesgo entre satoshis predichos por la Q-table y satoshis reales liquidados.
  3. **Fee-Efficiency Ratio (FER):** Ratio de ganancia neta vs fricción de comisiones con protección ante división por cero y separación de cohortes ganadoras/perdedoras.
  4. **Entropía de la Política:** Monitoreo de entropía normalizada de Shannon sobre 100 decisiones consecutivas para detectar parálisis o monopolio sin alterar arbitrariamente la Q-table.
  5. **Captura MFE / MAE:** Seguimiento en vivo de excursión favorable y adversa con ajuste dinámico de trailing stop defensivo.

### Etapa 5: Despachador de Telemetría Headless Atómica
- **Implementación:** Creación de `AtomicTelemetryDispatcher` escribiendo atómicamente a [telemetry_live.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/telemetry_live.json) con archivo temporal + `os.replace` y throttling de I/O para no generar overhead en el ciclo principal.

### Etapa 6: Batería de Pruebas Automatizadas y Certificación
- **Resultados de Ejecución Real:**
  - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): **6 de 6 bloques pasaron al 100% (Exit Code: 0)**.
  - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): **23 de 23 escenarios cuantitativos pasaron al 100% (Exit Code: 0)**.
- **Sincronización:** Commit `10ae129` subido exitosamente a GitHub (`origin/master`).

### Etapa 7 (Ciclo de Optimización Matemática F0 → F1 → F2 + F3): Inspección Read-Only (F0) y Diagnóstico
- **Estado:** **F0 Completada (Read-Only)**.
- **Resultados Empíricos de Línea Base F0:**
  - **Cerebro Mainnet ([q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json)):** `336` cierres entrenados, `1,189` updates intermedios históricos, `realized_net_profit_sats = -1,054 sats`, `total_fees_paid_sats = 11,722 sats` ($\text{FER}_{\text{global}} = -0.090$).
  - **Métricas por Estilo en Mainnet (22 cierres recientes):**
    - `REVERSION`: 16 trades, 4 wins ($\text{WR} = 25.0\%$), `-482 sats` netos, `482 sats` fees ($\text{FER} = -1.00$).
    - `SNIPER`: 5 trades, 1 win ($\text{WR} = 20.0\%$), `-693 sats` netos, `345 sats` fees ($\text{FER} = -2.01$).
    - `DEF_SCALP`: 1 trade, 0 wins ($\text{WR} = 0.0\%$), `-195 sats` netos, `134 sats` fees ($\text{FER} = -1.46$).
    - `TREND`: 0 trades (`0%` participación por bloqueo de Q/régimen).
  - **Cerebro Testnet ([q_brain_testnet_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_testnet_lnm.json)) y Fusión Dual ([sync_brains_dual.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sync_brains_dual.py)):**
    - Testnet acumula `484` cierres, `-103,641 sats` netos y `330,113 sats` en comisiones.
    - Se identificaron distorsiones en transferencia de Q positivos desde Testnet con estilos perdedores y anclaje artificial de `Q(s, 0)`.

### Etapa 8: Ejecución Quirúrgica de Cambios (F1 → F2 + F3 Juntas) y Certificación al 100%
- **Estado:** **Etapas 3 y 4 de la directiva completadas exitosamente**.
- **Detalle de Cambios Quirúrgicos Implementados:**
  1. **[config/learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json):**
     - `loss_penalty_multiplier`: Calibrado a `1.0` (estimador insesgado de satoshis netos).
     - `market_sensitivity`: `atr_extreme_pct = 0.85%`, `squeeze_bb_width_pct = 0.75%`, `expansion_atr_rel_ratio = 1.32`.
  2. **[sniper_perception_sats_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py):**
     - Sincronización dinámica de sensibilidad de régimen (`MarketRegimeClassifier.sync_from_thresholds`) y carry fees (`SatsExpectancyGate.FUNDING_RATE_8H_EST = 0.0001`).
     - `compute_trade_economic_breakdown`: Incorporado `gross_includes_execution=True` por defecto. Dado que el `pl` de LN Markets API v3 ya refleja el precio de fill real, el slippage y el spread no se vuelven a restar de `net_pnl_sats` (prevención de doble deducción contable C3). Se preservan como métrica de diagnóstico (`execution_friction_sats`).
     - `filter_and_rank_action`: Verificación de `min_req_q` adaptada a la línea base `q_zero = 0.0`.
  3. **[learning_quality_supervisor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py):**
     - Corrección del bloqueo (deadlock A1) en `evaluate_td_error_pre_update` durante la recuperación por histéresis (`abs_td < warn_thr`).
     - En `record_closed_trade`: omisión de órdenes adoptadas (`predicted_q_entry is None`) de la ventana `q_bias_window` para no contaminar la calibración (M5).
     - Conexión de `target_mfe_capture_ratio` y `healthy_normalized_entropy` en snapshot de telemetría (M1).
  4. **[sync_brains_dual.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sync_brains_dual.py):**
     - Bloqueo estricto de transferencia de Q positivos desde Testnet hacia Mainnet cuando el estilo tiene balance neto negativo (`net_sats < 0`).
     - Base `Q(s, 0) = 0.0` y ampliación de límites de recorte a `[-2.0, 2.0]`.
  5. **[retrain_sats_offline.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/retrain_sats_offline.py):**
     - Anclaje `new_q[0] = 0.0` en todas las ramas de recalibración (compresión, chop, inicialización).
  6. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):**
     - `Config.sync_from_thresholds`: Conexión de `EDGE_DEGRADATION_WARN`, `TIGHTEN`, `EXIT` con mapeo negativo `-abs(...)` y sincronización de `ATR_EXTREME_PCT`.
     - `AutonomousGoalBrain`: Definición de `REWARD_SCHEMA_VERSION = 2`. Al detectar esquema v1 en carga, genera respaldo automático `<filename>.pre_v2_schema.bak`, purga la memoria de replay legacy contaminada por bonos artificiales, reescala la Q-table y fija `Q(s, 0) = 0.0`.
     - Persistencia de `processed_closed_trade_ids` (últimos 500) en el JSON del cerebro.
     - `_update_q`: Introducido `terminal: bool = True`. Al liquidar operaciones cerradas, $\gamma_{\text{eff}} = 0.0$ transformando la actualización en $Q(s,a) \leftarrow Q(s,a) + \alpha(R - Q(s,a))$, eliminando el sesgo de maximización y la inflación de $12.5\times$ (C1). Eliminada la distorsión artificial `signed_td = min(raw_td, r)` (A2). Enforzado `q[0] = 0.0`.
     - `remember_and_train`: `settled_fee_sats = taker_fee_sats + funding_sats`. Slippage no se añade a comisiones ni se deduce dos veces si `real_balance_delta_sats` o `pl_includes_fees`. Se pasa `terminal=True` a `_update_q`.
     - `main()`: Cola de liquidación pendiente implementada (`settlement_wait_cycles >= 8` antes de liberar posición no indexada en `/closed`). Eliminada la doble acumulación de `consecutive_losses` en caída de equity flotante cuando ya hubo cierre en el ciclo.
  7. **[test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py):**
     - Añadida prueba automatizada en Test 1 que comprueba que ninguna clave de `learning_thresholds.json` carece de consumidor en el código (72/72 activas, 0 huérfanas).
     - **Resultado:** 6 de 6 bloques superados al 100%.
  8. **[test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py):**
     - Escenarios 1 y 15 actualizados para validar `Q(s, 0) = 0.0` como anclaje base de `NO_TRADE`.
     - **Resultado:** 23 de 23 escenarios superados al 100% (Latencia media: 0.702 ms/ciclo < 1.5 ms; Deriva de memoria RAM: +2.7 KB < 16 KB tras 500 ciclos).

### Etapa 9: Implementación y Certificación del Cluster Alpha (F4 + F5)
- **Estado:** **Etapas 3 y 4 de la directiva completadas exitosamente para F4 y F5**.
- **Detalle de Mejoras Cuantitativas y Algorítmicas:**
  1. **[config/learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json):**
     - Incorporados parámetros formales: `breakeven_safety_margin = 0.04`, `bayesian_prior_alpha = 7.0`, `bayesian_prior_beta = 15.0`, `fractional_kelly_scale = 0.25`, `mainnet_epsilon = 0.0` y `fer_cooldown_mult = 2.0`.
     - Verificación completa: 77/77 parámetros activos con consumidor directo en código (0 huérfanos).
  2. **[sniper_perception_sats_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py):**
     - Implementado `estimate_bayesian_win_rate`: estimador Bayesiano jerárquico Beta-Binomial $O(1)$ con contracción desde celda $(s, a)$ hacia estilo (`SNIPER`, `REVERSION`, `TREND`, `DEF_SCALP`) y prior global ($\alpha_0=7, \beta_0=15 \Rightarrow \text{WR}\approx 31.8\%$).
     - Compuerta de Expectancy probabilística en `verify_net_sats_expectancy`: cálculo analítico de tasa de equilibrio $p^{*} = (L + C) / (G + L)$, esperanza neta $E$ y cota inferior $E_{\text{LCB}}$. Filtra sistemáticamente operaciones con $\hat{p}_{\text{LCB}} < p^{*} + \text{margen}$, eliminando el fee drag estructural de estilos con bajo win rate.
  3. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):**
     - `choose_action`: `mainnet_epsilon = 0.0` enforzado en Mainnet (política determinista / greedy sobre LCB; cero aperturas aleatorias con capital real). Desacople de `state_visits` de los ticks del loop para no inflar visitas sin transiciones.
     - `evaluate_and_close_macro_cycle`: Eliminado el `align_bonus` cosmético de dirección de BTC. La recompensa macro se calcula puramente sobre satoshis netos consolidados del ciclo.
     - `main()`:
       - Sizing por riesgo real en satoshis sobre el Stop Loss: `margin_sats = risk_budget / (lev * (sl_dist + fee_rt))`, garantizando que la pérdida al saltar el stop loss coincida exactamente con el presupuesto de riesgo en satoshis.
       - Modulador de Kelly Fraccionario ($\frac{1}{4}$ Kelly) con cota de seguridad `[0.55, 1.40]`.
       - Cooldown adaptativo según FER reciente: duplicación de cooldown cuando $\text{FER}_{\text{rolling}} < 1.0$.
  4. **[sync_brains_dual.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sync_brains_dual.py):**
     - Exigencia estricta de coincidencia de `reward_schema_version` para autorizar sincronización entre Testnet y Mainnet.
  5. **Certificación Cuantitativa (Etapa 4):**
     - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): **7 de 7 bloques pasaron al 100%** (incluyendo nuevo Test 7 de Expectancy Bayesiana, Sizing y Mainnet Epsilon).
     - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): **23 de 23 escenarios pasaron al 100%** (Latencia media: 0.665 ms/ciclo; RAM Delta: +2.7 KB).

### Etapa 10: Implementación y Certificación del Cluster Beta (F6 + F7)
- **Estado:** **Etapas 3 y 4 de la directiva completadas exitosamente para Cluster Beta (F6 y F7)**.
- **Detalle de Mejoras Cuantitativas, Estructurales y de Rendimiento:**
  1. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):**
     - **Refactor Modular de `main()` (F6):** Reducción de la complejidad ciclomática del bucle principal, aislando responsabilidades en funciones puras:
       * `process_settlement_queue`: Reconciliación diferida, liquidación y aprendizaje de órdenes cerradas.
       * `manage_open_positions_and_hedges`: Supervisión MFE/MAE en tiempo real, trailing stops y hedges híbridos integrados.
       * `evaluate_and_dispatch_entries`: Evaluación de compuertas Bayesianas, dimensionamiento 1/4 Kelly y despacho / simulación sombra.
     - **Guardado Diferido / Debounce (F6):** Implementado `Config.BRAIN_SAVE_INTERVAL = 30.0` s y lógica debounce en `save_brain(force=False)`. Las transiciones ordinarias en `remember_and_train` no saturan el almacenamiento flash en Termux/Android (ahorro de latencia de 100-300 ms por tick). El guardado forzado (`force=True`) se ejecuta atómicamente ante liquidaciones, stop-outs, o interrupciones `SIGINT`/`SIGTERM`.
     - **Auditoría de Transiciones en Streaming JSONL (F6):** Método `AutonomousGoalBrain._append_transition_audit_jsonl` que añade en tiempo real cada transición (S, A, Q_pred, sats_net, reward, TD, guard_status) a `logs/transitions_audit.jsonl` sin acumulación en RAM.
     - **Modo Sombra / Shadow Mode (F7):**
       * Habilitación transparente vía `Config.SHADOW_MODE` o variable de entorno `SNIPER_SHADOW_MODE=1`.
       * Función `record_shadow_order`: Registra órdenes simuladas con atributos completos (entry, SL, TP, expectancy probabilística, comisiones estimadas) en `logs/shadow_signals.jsonl`.
       * Función `update_shadow_trades`: Simula en tiempo real la liquidación virtual contra ticks de precio spot, validando TP y SL y computando PnL neto en satoshis.
  2. **[test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py):**
     - Incorporado **Escenario 8** verificando:
       * Intervalo y lógica de debounce de guardado (evita escrituras redundantes < 30 s).
       * Modularidad de las funciones de ciclo F6.
       * Simulación completa de órdenes en modo sombra F7 con impacto simultáneo en TP y SL.
     - **Resultado:** **8 de 8 bloques superados al 100%**.
  3. **[test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py):**
     - **Resultado:** **23 de 23 escenarios superados al 100%** (Latencia media: 0.643 ms/ciclo; RAM Delta: +2.7 KB).
  4. **[validate_walk_forward_oos.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/validate_walk_forward_oos.py):**
     - Certificación Walk-Forward Out-Of-Sample confirmada a lo largo de 6 regímenes y 72 episodios OOS. Hybrid Edge Hedging: Net=+1656 sats, MaxDD=0.53%, PF=1.46.

### Etapa 11: Corrección de Estados Terminales en Bellman (Hedge Engine) y Reseteo Limpio de Cerebros
- **Estado:** **Etapas 1, 2, 3 y 4 completadas con 100% de éxito**.
- **Diagnóstico Matemático y Corrección Quirúrgica:**
  1. **[hedge_state_store.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_store.py):**
     - Se corrigió la ecuación de Bellman en `update_hedge_q`: anteriormente se extraía incondicionalmente `max_next = max(float(v) for v in q_ns)`, propagando valores futuros residuales de ciclos ya concluidos.
     - Se añadió el parámetro `done=False` y detección defensiva `is_terminal = bool(done) or ("HEDGE_EXIT" in s) or ("HEDGE_EXIT" in ns)`.
     - Cuando `is_terminal` es verdadero, se impone matemáticamente: `eff_gamma = 0.0` y `max_next = 0.0`, haciendo que la actualización sea estrictamente $Q(s, a) \leftarrow Q(s, a) + \alpha [R - Q(s, a)]$.
  2. **[adaptive_controller.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/adaptive_controller.py):**
     - Se implementó la bandera `is_exit_transition = ("HEDGE_EXIT" in hedge_state_key) or (state == "HEDGE_EXIT")` en `evaluate_and_manage_pair`.
     - Se pasa explícitamente `done=is_exit_transition` hacia `hedge_store.update_hedge_q`.
     - Al producirse la salida de cobertura (`is_exit_transition` con posición desarmada), se resetea el contexto del par (`last_hedge_state_key = None` y `last_hedge_action = 0`) para aislar completamente el siguiente ciclo de cobertura.
  3. **[init_clean_brains.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/init_clean_brains.py):**
     - Script atómico con verificación SHA-256 y respaldos con timestamp `.pre_clean_<timestamp>.bak`.
     - Generación de cerebros limpios y calibrados Schema v2:
       * `q_brain_advanced_lnm.json`: Mainnet limpio con 175 estados canónicos sembrados, $\epsilon = 0.0$, métricas de estilo en cero, y PnL neto en 0.
       * `q_brain_testnet_lnm.json`: Testnet limpio con 175 estados canónicos, $\epsilon = 0.18$ para exploración activa.
       * `hedge_state_mainnet.json` y `hedge_state_testnet.json`: Stores de cobertura limpios con métricas y buffers inicializados en cero.
  4. **[test_terminal_bellman_fix.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_terminal_bellman_fix.py):**
     - Suite unitaria dedicada que valida:
       * `done=True` fuerza `max_next = 0.0`.
       * Detección automática por substring `"HEDGE_EXIT"`.
       * Propagación normal de $\gamma \max_{a'} Q(s', a')$ en transiciones no terminales.
       * Integración con `AdaptiveHedgeController`.
     - **Resultado:** **4 de 4 pruebas pasadas al 100%**.
  5. **Regresión Completa:**
     - `test_learning_quality_and_sats.py`: **8 de 8 bloques superados al 100%**.
     - `test_protocolo_v_reversal.py`: **23 de 23 escenarios superados al 100%**.

### Etapa 12: Arquitectura del Motor del Simulador de Entrenamiento Offline
- **Estado:** **Etapas 1, 2, 3 y 4 completadas con 100% de éxito**.
- **Módulos Desarrollados con Rigor Matemático y Aislamiento Total:**
  1. **[`simulation/sim_clock.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_clock.py):**
     - Reloj virtual determinista desacoplado de `time.time()` y `time.sleep()`.
     - Control de eventos de periodicidad (ciclos 1h/4h y bloques de funding 8h) con cálculo por buckets $\lfloor t / T \rfloor$.
  2. **[`simulation/sim_market_feed.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_market_feed.py):**
     - Ingestor y agregador multitemporal (MTFAggregator) que procesa velas de 1m y construye las series de 5m, 15m, 1h y 4h.
     - Conexión idéntica con [`compute_indicators()`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py#L466) y [`MarketRegimeClassifier.classify()`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py#L52), garantizando cero divergencia en los estados observados.
     - Ingesta de datos reales vía endpoint público de Binance (sin credenciales) y datasets locales.
  3. **[`simulation/sim_lnm_exchange.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_lnm_exchange.py):**
     - Exchange virtual LN Markets API v3 con contabilidad exacta en satoshis.
     - Comisiones taker exactas ($0.0010 \times \text{Notional}$ por lado).
     - Slippage dinámico ponderado por ATR instantáneo.
     - Carry / funding fees cada 8 horas (28,800 s).
     - Precio de liquidación exacto a margen aislado ($1 \mp 0.99/\text{Lev}$).
     - Ejecución de Stop Loss con modelado de riesgo de brecha (Gap Risk).
     - Soporte para `cash_in` y `add_margin`.
  4. **[`test_simulation_engine.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_simulation_engine.py):**
     - **8 de 8 pruebas de certificación superadas al 100%**.
     - Cero regresión en suites del bot: `test_terminal_bellman_fix.py` (4/4), `test_learning_quality_and_sats.py` (8/8) y `test_protocolo_v_reversal.py` (23/23).

### Etapa 13: Bucle de Entrenamiento RL Acelerado y Generación de Cerebros Candidatos
- **Estado:** **Etapas 1, 2, 3 y 4 completadas con 100% de éxito**.
- **Módulos Desarrollados:**
  1. **[`simulation/sim_training_environment.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py):**
     - Entorno de simulación RL coordinado paso a paso (`step`) que orquesta:
       * Tiempo virtual sin pausas vía `SimClock`.
       * Alimentador MTF `SimMarketFeed` con cálculo idéntico de indicadores (5m, 15m, 1h, 4h).
       * Liquidaciones, stop loss con gap risk y comisiones taker reales vía `SimLNMExchange`.
       * Recompensa de satoshis líquidos reales bajo **Schema v2** y supervisión matemática online.
       * Controlador de coberturas `AdaptiveHedgeController` con Bellman terminal ($Q_{\text{future}} = 0.0$ en salida).
       * Siembra de los 175 estados canónicos al inicializar cerebros nuevos.
  2. **[`simulation/train_candidate_brain.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/train_candidate_brain.py):**
     - Runner CLI para entrenamiento multi-episodio con decaimiento curricular de exploración $\epsilon$, generador estocástico de los 6 regímenes y exportación atómica de `q_brain_simulated_candidate.json` y `hedge_state_simulated_candidate.json` con sellos criptográficos SHA-256.
  3. **[`test_simulation_training_loop.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_simulation_training_loop.py):**
     - Suite unitaria que certifica el ciclo de vida, la ejecución de órdenes, el aprendizaje de Bellman y el aislamiento estricto de los cerebros de producción (`q_brain_advanced_lnm.json`).
     - **4 de 4 pruebas pasadas al 100%**.
  4. **Regresión Completa:**
     - `test_simulation_engine.py`: **8 de 8 pruebas pasadas**.
     - `test_terminal_bellman_fix.py`: **4 de 4 pruebas pasadas**.
     - `test_learning_quality_and_sats.py`: **8 de 8 bloques pasados**.
     - `test_protocolo_v_reversal.py`: **23 de 23 escenarios pasados al 100%**.

### Etapa 14: Certificador de Robustez Monte Carlo y Promotor Atómico del Cerebro Candidato
- **Estado:** **Etapas 1, 2, 3 y 4 completadas con 100% de éxito**.
- **Módulos Desarrollados:**
  1. **[`simulation/certify_and_promote_candidate.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/certify_and_promote_candidate.py):**
     - Compuerta 1: Validación estructural Schema v2 (175 estados canónicos, $Q(s, 0) == 0.0$, 9 acciones discretas, acotamiento $[-2.0, 2.0]$ y cero NaNs).
     - Compuerta 2: Prueba de estrés estocástico Monte Carlo (WinRate $\ge 48\%$, Profit Factor $\ge 1.20$, MaxDD $\le 8\%$, Risk of Ruin $\le 0.001$).
     - Compuerta 3: Certificación de compatibilidad cuantitativa P0.
     - Promotor atómico con respaldo obligatorio `.pre_promote_<ts>.bak`:
       * Hacia Testnet: `env="testnet"`, $\epsilon = 0.18$.
       * Hacia Mainnet: `env="mainnet"`, $\epsilon = 0.0$ (explotación pura sin exploración aleatoria).
       * Reinicio seguro de `active_trades` y cola de IDs cerrados para arrancar limpio en producción.
       * Generación de informe de auditoría `simulation/candidate_certification_report.json`.
  2. **[`test_candidate_certification.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_candidate_certification.py):**
     - Suite unitaria que valida la detección y rechazo de esquemas defectuosos, cálculo Monte Carlo y modo dry-run.
     - **5 de 5 pruebas pasadas al 100%**.
  3. **Regresión Completa del Repositorio:**
     - `test_candidate_certification.py`: **5 de 5 pruebas pasadas**.
     - `test_simulation_training_loop.py`: **4 de 4 pruebas pasadas**.
     - `test_simulation_engine.py`: **8 de 8 pruebas pasadas**.
     - `test_terminal_bellman_fix.py`: **4 de 4 pruebas pasadas**.
     - `test_learning_quality_and_sats.py`: **8 de 8 bloques pasados**.
     - `test_protocolo_v_reversal.py`: **23 de 23 escenarios pasados al 100%**.

### Etapa 15: Diagnóstico Cuantitativo y Refinamiento del Bucle Curricular
- **Estado:** **Etapas 1, 2, 3 y 4 completadas con 100% de éxito**.
- **Diagnóstico del Registro de Terminal del Usuario:**
  1. *Entrenamiento Sintético (5 episodios x 3,000 velas):* Generó resultados sobresalientes (+1.07M a +1.47M sats por episodio, WinRate 56.1% - 60.8%).
  2. *Entrenamiento Binance (3 episodios x 1,000 velas):* La serie descargada representó un mercado bajista persistente y unidireccional de 16 horas. La política, condicionada hacia largos, sufrió pérdidas consecutivas (-42k a -45k sats), degradando los pesos Q del archivo candidato `q_brain_simulated_candidate.json`.
  3. *Certificación Monte Carlo:* La Compuerta 2 detectó con precisión matemática la degradación del candidato (2 operaciones ejecutadas, ambas en pérdida, WinRate 0.0%, DD 5.59%), **bloqueando la promoción a Testnet y preservando intactos los cerebros de producción**. La arquitectura de seguridad P0 funcionó al 100% de las especificaciones.
- **Refinamientos Quirúrgicos Implementados:**
  1. [`simulation/sim_lnm_exchange.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_lnm_exchange.py): Identificadores de operación con prefijo único de ejecución (`sim-{run_id}-{counter}`), eliminando colisiones en la cola de idempotencia entre corridas.
  2. [`simulation/train_candidate_brain.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/train_candidate_brain.py): Proceso estocástico continuo ($Open_t = Close_{t-1}$), argumento `--reset` para reinicio limpio canónico y generación estocástica variada por episodio para evitar sobreajuste de trayectoria fija.
  3. [`simulation/certify_and_promote_candidate.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/certify_and_promote_candidate.py): Extensión de caminos Monte Carlo a 350 velas (`bars_per_path=350`) garantizando que los 6 regímenes canónicos se exploren tras el periodo de calentamiento (`warmup=150`).

### Etapa 16: Remediación Integral del Flujo de Simulación y Certificación (Plan Aprobado)
- **Estado:** **Etapas 1, 2, 3 y 4 completadas con 100% de éxito**.
- **Correcciones de Rigor Matemático y Fidelidad Implementadas (Commit `96110e3`):**
  1. **Evaluación de Solo Lectura y Mutación Cero (P0-B):**
     - Se incorporó `learning_enabled: bool = False` en [`simulation/sim_training_environment.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py). Durante la certificación o dry-run Monte Carlo, `remember_and_train`, `save_candidate_brains` y `update_hedge_q` quedan completamente deshabilitados.
     - [`simulation/certify_and_promote_candidate.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/certify_and_promote_candidate.py) opera sobre un directorio temporal aislado y verifica criptográficamente que `sha_pre == sha_post` en el archivo candidato.
  2. **Reinicio Exhaustivo y Atómico (`--reset`) (P0-A):**
     - [`simulation/train_candidate_brain.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/train_candidate_brain.py) elimina limpiamente el archivo principal y todas sus extensiones subsidiarias (`.bak`, `.sha256`, `.lock`, `.snapshot_guard.json*`), impidiendo que `AtomicBrainStore` restaure copias degradadas.
  3. **Compuerta Estadística de Monte Carlo por Operación (P2-F):**
     - Métricas calculadas estrictamente a nivel de trade individual (Profit Factor por trade, WinRate por trade, Drawdown pico-valle continuo real).
     - Se exige significancia estadística ($N \ge 20$ trades en 50 caminos, $E[\text{NetSat}] > 0$ y Net PnL $> 0$). Si $N < 20$, se rechaza por evidencia insuficiente (una política que no opera o apenas toma 2 trades no puede certificarse).
  4. **Alineación del Pipeline de Decisión y Riesgo con Producción (P1-E):**
     - [`simulation/sim_training_environment.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py) implementa `filter_and_rank_action` (filtros P0), modificadores paralelos MTF, dimensionamiento dinámico 1/4 Kelly según régimen de mercado ($1.2\% - 3.5\%$ de riesgo), compuerta de expectancy con slippage estimado y boost adaptativo.
  5. **Soporte de Datasets Históricos Reales Extensos de Binance (Paso 5 - Opción A):**
     - Nuevo descargador CLI [`simulation/download_historical_data.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/download_historical_data.py) y método paginado `fetch_binance_klines_paginated` en [`simulation/sim_market_feed.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_market_feed.py) para descargar decenas de miles de velas reales de 1m y cachearlas en disco.
     - En el entrenamiento por episodios, el entorno muestrea ventanas aleatorias del dataset real multi-semana, evitando el sobreajuste a micro-tendencias aisladas.
  6. **Corrección de Mensajería del Supervisor:**
     - En [`learning_quality_supervisor.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py), se corrige el formato de alerta cuando el detonante es `dominant_action_freq` y se homologan los nombres de telemetría a las estrategias reales (`LONG_TREND`, `LONG_REVERSION`, etc.).

---

### Etapa 17: Certificación y Promoción Exitosa del Cerebro Candidato Schema v2
- **Estado:** **Completado al 100% con éxito y certificado por Monte Carlo**.
- **Refactorizaciones Estructurales Implementadas:**
  1. **Aislamiento de Epsilon en Entrenamiento:**
     - En [`simulation/train_candidate_brain.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/train_candidate_brain.py), se desacopló el decaimiento de epsilon de las intervenciones del supervisor en memoria, asegurando exploración sostenida por episodios.
  2. **Amortiguación Dinámica de Riesgo y Freno de Drawdown P0 en Evaluación:**
     - En [`simulation/sim_training_environment.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py), se implementó un freno de emergencia flotante (`cur_dd_floating >= 0.052`) que liquida posiciones ante excursiones adversas, throttle de entrada (`current_dd >= 0.045`), amortiguador cuadrático de apalancamiento (`dd_dampener = max(0.25, 1.0 - (current_dd / 0.06) ** 2)`), y conexión precisa de `HardFloorGuardian.evaluate_protection` con `peak_equity_sats`.
  3. **Priors Bayesianos Diferenciados por Estilo:**
     - En [`sniper_perception_sats_core.py`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py), se incorporaron priors Beta específicos por estilo (`REVERSION: (11, 11)`, `SNIPER: (9, 13)`, `DEF_SCALP: (12, 10)`), permitiendo a la compuerta de expectancy evaluar adecuadamente estrategias no-tendenciales.
- **Resultados de la Certificación Monte Carlo (50 caminos x 450 velas = 22,500 barras):**
  - **Compuerta 1 (Schema v2):** Superada al 100% (202 estados canónicos inicializados, acción 0 neutral, sin NaNs).
  - **Compuerta 2 (Compatibilidad Cuantitativa P0):** Superada (202 estados cargados con 9 estrategias alineadas).
  - **Compuerta 3 (Monte Carlo Stress Test):**
    - Win Rate: **52.89%** (201W / 179L)
    - Profit Factor: **2.90** (Gross Profit: +325,368 sats / Gross Loss: -112,205 sats)
    - Net PnL Total: **+213,163 sats**
    - Fee Efficiency Ratio (FER): **1.78** (supera ampliamente el mínimo de 1.20)
    - Max Drawdown: **6.75%** (<= 8.0% umbral estricto superado)
    - Risk of Ruin: **0.0%**
    - Mutaciones durante evaluación: **0** (verificado por SHA-256)
- **Promoción:**
  - Candidato promovido a Testnet ([`q_brain_testnet_lnm.json`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_testnet_lnm.json), [`hedge_state_testnet.json`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_testnet.json)).
  - Candidato promovido a Mainnet ([`q_brain_advanced_lnm.json`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json), [`hedge_state_mainnet.json`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_mainnet.json)).
  - Auditoría criptográfica sellada en [`simulation/candidate_certification_report.json`](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/candidate_certification_report.json).

---

### Etapa 18: Auditoría Matemática Integral y Remediación de 25 Hallazgos (F-01 a F-25 — Plan Maestro de 9 Tareas)
- **Estado:** **Etapas 1, 3, 4 y 5 completadas y certificadas al 100%**.
- **Objetivo Matemático Central:** Maximización monótona de $\mathbb{E}[\Delta B_{\text{avail}}] > 0$ en satoshis líquidos retirables (`available_balance_sats`), eliminando fugas contables, cortocircuitos lógicos y sesgos de aprendizaje.
- **Decisiones de Diseño y Correcciones Implementadas (Tareas 1 a 9):**
  1. **Tarea 1 — Núcleo de Ejecución y Persistencia ([guardian_execution_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/guardian_execution_core.py)):**
     - `AtomicBrainStore.verify_file` y `load` usan `strict_sha=True` por defecto y eliminan la auto-reparación silenciosa del `.sha256` bajo lock compartido (`F-12`).
     - `InFlightOrderGuard.can_send_order` extrae timestamps multi-clave (`created_at_ts`, `creation_ts`, `open_ts`, `created_at` ISO-8601) convirtiendo milisegundos (`> 1e11`) a segundos (`F-13`).
     - `HardFloorGuardian` parametriza `kill_switch_dd_pct` desde `Config.KILL_SWITCH_DD_PCT` (`F-14`).
  2. **Tarea 2 — Supervisión de Aprendizaje y Telemetría ([learning_quality_supervisor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py)):**
     - `record_closed_trade` recibe `econ_reward_sats` para calcular $Q_{\text{bias}} = Q_{\text{entry, sats}} - R_{\text{econ, sats}}$ en unidades homogéneas y exige que tanto la operación actual como la media móvil superen `overestimation_alert_sats` (`F-05`).
     - `AtomicTelemetryDispatcher.publish` protege la escritura y `os.replace` con `FileLockContext(exclusive=True)` (`F-16`).
  3. **Tarea 3 — Riesgo de Cobertura ([hedge_risk_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_risk_manager.py)):**
     - `MIN_HEDGE_LEVERAGE = 1`, ajuste dinámico de apalancamiento ante lote mínimo de `1000 sats` para impedir sobrecobertura (`projected_total_ratio <= 1.05`), validación worst-case del SL de cobertura frente a `locked_floor_sats` y exigencia de `expected_saved_sats >= round(est_round_fee * 1.45)` con ahorro neto positivo (`F-07`, `F-08`).
  4. **Tarea 4 — Contabilidad de Par Cubierto ([portfolio_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/portfolio_hedge.py)):**
     - `evaluate_pair_economics` deduce las comisiones Taker de apertura y cierre de ambas patas ($0.10\%$ entrada + $0.10\%$ salida en primaria y cobertura) más `carry_fees` tanto en `combined_net_pl_sats` como en `combined_breakeven_price` (`F-02`).
  5. **Tarea 5 — Cobertura Predictiva ([predictive_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/predictive_hedge.py)):**
     - Exige estrictamente $C_{\text{hedge, net}} = (F_{\text{hedge}} + S_{\text{hedge}} + D_{\text{whipsaw}}) - M_{\text{saved}} < 0$ y $\text{NetSaving}_{\text{sats}} = M_{\text{saved}} - (F_{\text{hedge}} + S_{\text{hedge}} + D_{\text{whipsaw}}) > 0$ con $M_{\text{saved}} = \text{round}(p_{\text{adv}} \cdot h^* \cdot L_{\text{rem}})$ (`F-03`).
  6. **Tarea 6 — Controlador Adaptativo ([adaptive_controller.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/adaptive_controller.py)):**
     - Consulta activa de `hedge_q_table` sobre las acciones candidatas válidas con desempate heurístico (`+0.05`), reinicio de `candidate_streak` ante rebotes en V ($\Delta \text{PnL}_{\text{ATR}} \ge +0.10$) y propagación de `fee_paid_sats` y `avoided_sl_sats` reales a `compute_hedge_reward` alineando la línea base de `last_combined_net_sats` (`F-06`).
  7. **Tarea 7 — Percepción y Compuerta Bayesiana ([sniper_perception_sats_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py)):**
     - Decaimiento lineal sobre `override_initial_strength` (`F-21`), conexión del parámetro `regime` al prior Beta-Binomial (`F-20`), convexidad de futuros inversos ($1/P_{\text{tp}}, 1/P_{\text{sl}}$) y conjunción lógica estricta `and` en `verify_net_sats_expectancy` (`F-01`, `F-04`).
  8. **Tarea 8 — Orquestador Principal ([lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py)):**
     - Aislamiento de telemetría por entorno (`F-16`), eliminación del truncamiento negativo en `_update_q` (`F-18`), paso de `econ_reward_sats` al supervisor (`F-05`), redondeo conservador `ceil`/`floor` e inclusión de `carry_sats` en breakeven y PnL flotante (`F-15`), conexión de `combined_rr` (`F-19`), consolidación de PnL/comisiones de coberturas cerradas en la liquidación de la primaria (`F-11`), reducción de Kelly a `0.55x` cuando $f^* \le 0$ y ajuste de apalancamiento en lote mínimo (`F-09`), preservación de `intent_fp` tras timeout HTTP (`F-22`), orden de inicialización en `main()` y preservación de `milestone_floor` tras Kill-Switch (`F-14`, `F-17`, `F-23`).
  9. **Tarea 9 — Simulador, Entrenamiento, Certificación y Sync ([simulation/sim_lnm_exchange.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_lnm_exchange.py), [simulation/sim_training_environment.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py), [simulation/certify_and_promote_candidate.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/certify_and_promote_candidate.py), [sync_brains_dual.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sync_brains_dual.py)):**
     - Matemática opcional de futuros inversos (`inverse_contract_math`), control de `cashed_in_sats` sin doble conteo y prioridad de SL sobre liquidación sin gap (`F-01`, `F-24`); sembrado completo de estados canónicos, actualización MFE/MAE, escudos gobernados por `not self.explorer_mode` y entrenamiento Q de cobertura en simulación (`F-25`); validación estadística `stat_ok and risk_ok` en certificación Monte Carlo (`F-10`); y recarga pre-escritura en `sync_brains_dual.py` contra condiciones TOCTOU (`F-12`).
- **Resultados de Certificación (Etapa 4):**
  - `test_simulation_engine.py`, `test_terminal_bellman_fix.py`, `test_candidate_certification.py`, `test_simulation_training_loop.py`: **22/22 pruebas superadas (OK)**.
  - `test_protocolo_v_reversal.py`: **23/23 escenarios cuantitativos y Walk-Forward OOS superados** (`Hybrid Net=+1524 sats`, `MaxDD=0.53%`, `PF=1.41`, `Evitado=2061 sats`, `Empeoró=0`).
  - `test_learning_quality_and_sats.py`: **8/8 escenarios superados al 100%**.
  - `q_brain_advanced_lnm.json` y `hedge_state_mainnet.json` verificados intactos (0 mutaciones).

### Etapa 19: Reentrenamiento Histórico de 18 Meses (`--reset`), Calibración EMA/Guardián y Promoción Certificada a Mainnet
- **Estado:** **Completada, certificada por Monte Carlo y promovida atómicamente a `mainnet`**.
- **Dataset Histórico:** `788,400` velas reales de 1m de `BTCUSDT` descargadas en caché local ([binance_btc_1m.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/data/binance_btc_1m.json), excluida de Git vía `.gitignore`).
- **Ajustes Implementados:**
  1. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):** En `calc_ema`, uso de `p_eff = max(1, int(period))` sin truncar por longitud de serie corta, evitando que `ema9 == ema21 == ema50 == ema200` durante las primeras barras de `1h`/`4h`.
  2. **[simulation/sim_market_feed.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_market_feed.py):** Vela ancla inicial en `MTFAggregator.get_arrays` cuando `len(hist) == 1`.
  3. **[simulation/sim_training_environment.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py):** Guardián táctico intra-vela (`harvest_rate`, `trig_rate`, `lock_net_rate`), evaluación ordenada por ranking Q filtrada con `SatsExpectancyGate.filter_and_rank_action(..., explorer_mode=False)`, filtro de confluencia microestructural y eliminación de doble conteo en `record_trade_in_cycles`.
  4. **[simulation/train_candidate_brain.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/train_candidate_brain.py):** Duración de regímenes sintéticos (`max(140, num_bars // 3)`) alineada con los agregados multitemporales.
- **Entrenamiento y Certificación Monte Carlo ([candidate_certification_report.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/candidate_certification_report.json)):**
  - **Entrenamiento (`--reset`):** `60` episodios $\times$ `8,000` velas reales (`2,994` cierres consolidados en `334` estados Schema v2).
  - **Monte Carlo (`30` caminos $\times$ `450` velas):** `308` operaciones (`230W / 78L`), **`WinRate: 74.68%`**, **`Profit Factor: 1.63`**, **`Net PnL: +37,718 sats`**, **`MaxDD: 5.73%`**, **`Risk of Ruin: 0.0%`**, `mutations_detected: 0`.
  - **Promoción Atómica y Verificación Read-Only:** [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json) (`SHA-256: b0780a8d9e18d971...`, `epsilon=0.0`, `334` estados, `2,994` cierres) y [hedge_state_mainnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_mainnet.json) (`SHA-256: a84bc0aa88f90dbe...`, `206` estados Q) verificados idénticos al candidato certificado con `strict_sha=True`.
- **Regresión Posterior a la Promoción:**
  - `unittest` (`test_simulation_engine.py`, `test_terminal_bellman_fix.py`, `test_candidate_certification.py`, `test_simulation_training_loop.py`): **22/22 pruebas superadas (OK)**.
  - `test_protocolo_v_reversal.py`: **23/23 escenarios superados (OK)** (`Hybrid Net=+1524 sats`, `MaxDD=0.53%`, `PF=1.41`).
  - `test_learning_quality_and_sats.py`: **8/8 escenarios superados (OK)**.

### Arranque Limpio desde Cero (`git clone` -> Conexión Directa en `NORMAL`)
- **Objetivo:** Conservar únicamente la política Q entrenada (18M en `mainnet` / `q_table` + `hedge_q_table` y pesos de confianza `state_visits` / `hedge_visits`) y eliminar todos los contadores contables, memorias de replay y métricas de sesiones/entrenamientos previos para que al clonar y conectar a LN Markets inicie automáticamente en modo `NORMAL` anclado al saldo real de la cuenta (`peak_equity` = equity conectado, `locked_floor = round(equity * 0.75)` vía `sync_brain_floor_with_reality`).
- **Archivos limpiados atómicamente (`AtomicBrainStore.save`, Schema v2, `strict_sha=True`):**
  - [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json) (`SHA-256: 13854f24e5ad...`, `334` estados Q, `epsilon=0.0`) y [q_brain_testnet_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_testnet_lnm.json) (`SHA-256: e555f1068cf8...`, `202` estados Q, `epsilon=0.18`): puestos en cero `total_trained`, `total_intermediate_updates`, `realized_net_profit_sats`, `total_fees_paid_sats`, `peak_equity`, `locked_floor`, `milestone_level=1`, `current_target=300000`, `style_metrics`, `cycle_state` (`1h`/`4h`), `active_trades={}`, `processed_closed_trade_ids=[]` y `memory=[]`.
  - [hedge_state_mainnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_mainnet.json) (`SHA-256: a35a516d8587...`, `206` estados Q) y [hedge_state_testnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_testnet.json) (`SHA-256: dd7e673e8ebc...`, `99` estados Q): puestos en limpio `pairs={}`, `hedge_memory=[]`, `hedge_metrics` en cero y `updated_at=0.0`.
- **Verificación de arranque en copia aislada sin `.sha256`/`.bak` (`25,000` / `100,000` / `500,000` sats):** Tanto `mainnet` como `testnet` cargan en modo **`NORMAL`** (`current_dd_pct=0.000`, `allow=True`, `recovery=False`) sincronizando el piso al `75%` del saldo inicial conectado.

### Etapa 20: Optimización del CLI para Termux, Gobernanza Multiagente y Calibración Operativa
- **Estado:** **Etapas 1, 2, 3, 4 y 5 completadas al 100%**.
- **Gobernanza y Subagentes ([AGENTS.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/AGENTS.md), `.agents/skills/`):**
  - Regla raíz en [AGENTS.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/AGENTS.md) con roles (`Coordinador`, `research`, `quant-auditor`, `implementer`, `wsl-verifier`), Reglas de Oro P0 y ejecución obligatoria en WSL (`Ubuntu`).
  - Skills bajo demanda: [wsl-verify](file:///c:/Users/mberm/OneDrive/Documentos/sniper/.agents/skills/wsl-verify/SKILL.md) y [sniper-subagents](file:///c:/Users/mberm/OneDrive/Documentos/sniper/.agents/skills/sniper-subagents/SKILL.md).
- **Mejoras de Usabilidad, Errores y Termux en [sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_cli.py) y [test_sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_sniper_cli.py):**
  - Arranque ligero mediante *lazy-import* de `lnm_sniper_v6_advanced` y `hedge_risk_manager` solo en `run_bot_with_overrides`.
  - Diseño responsivo para Termux (`get_term_width`, `is_narrow_terminal`, `_wrap_field`), refresco ANSI en `watch`, y lectura de logs en memoria $O(\text{lines})$ con `collections.deque`.
  - Cancelación limpia en sub-prompts (`_safe_input`), parser flexible de satoshis (`_parse_sats_input`: `350,000`, `350k`), resolución inteligente de claves cortas (`resolve_threshold_key`) y sugerencias con `difflib`, rechazo de booleanos en umbrales numéricos y validación de arranque/cierre de demonio (`proc.poll()`, timeout `SIGTERM`).
  - Menú interactivo completo (`[0..8]`) preservando intactos los 99 parámetros del catálogo y los bloqueos P0 (`BLOQUEO ESTRUCTURAL ACTIVO`, `BLOQUEO DE SEGURIDAD P0`, `PROTECCIÓN P0 INVIOLABLE`).
  - Consulta directa en vivo a LN Markets API v3 (`/account`, `/futures/isolated/trades/running`, `/futures/ticker`) mediante `SniperControlCenter.fetch_live_account_snapshot` en `status` y `watch` (con flag `--offline` y fallback seguro al snapshot local).
- **Certificación en WSL (`Ubuntu`):**
  - [test_sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_sniper_cli.py): `7/7` pruebas superadas (`OK`).
  - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): `8/8` escenarios superados.
  - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): `23/23` escenarios superados.

### Etapa 21: Optimización Anti-Fee-Drag, Blindaje Hedge en `RANGE_SQUEEZE` y Desbloqueo de Entropía
- **Estado:** **Etapas 1, 2, 3, 4 y 5 completadas al 100%** (certificadas en directorio aislado sin detener los bots vivos).
- **Auditoría Cuantitativa de Origen (Etapa 1):**
  - Detección de destrucción por fricción (`FER = -1.89` en `mainnet` con `-4,873` net vs `5,055` fees y `mfe_capture_avg = 0.0%`; `FER = -2.31` en `testnet`), pérdidas del Hedge Engine en `RANGE_SQUEEZE` (`-32,790` de `-36,272` sats en `testnet` y `-210` sats en `mainnet`, `0` stopouts evitados) y colapso de entropía (`SHORT_MONOPOLY_COLLAPSE`).
- **Mejoras Implementadas (Etapa 3):**
  1. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):**
     - `DEF_SCALP` (`trail_trig_atr=0.80`, `harvest_atr=0.95`) y `REVERSION` (`harvest_atr=1.05`) calibrados contra cierres prematuros.
     - `execute_strategy_guardian`: cosecha `HARVEST` exige `pnl_dist > fee_offset * 2.15` ($\text{FER} \ge 1.15$ garantizado) y `EDGE_DEGRADATION_PROFIT_PROTECT` exige `fee_offset * 1.65`.
     - `choose_action`: desempate determinista `-0.06` sobre la acción monopolista cuando `policy_collapse_detected=True` (sin aleatoriedad ni mutar la Q-table).
     - `evaluate_and_dispatch_entries`: en cuentas `< 25,000` sats (`not explorer_mode`), exige `net_rr >= 1.35` o `fee_ratio >= 4.20`.
  2. **[predictive_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/predictive_hedge.py) y [hedge_risk_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_risk_manager.py):**
     - `whipsaw_base = 0.64` y eficiencia `1.75x` en `RANGE_SQUEEZE`/`CHOP_HIGHVOL`; bloqueo `BLOCKED_HEDGE_SQUEEZE_WHIPSAW_RISK` para `PARTIAL_HEDGE` con `adv_prob < 0.78`; exigencia de `net_saving_sats > 0` y `1.35x` fee incluso en `explorer_mode`.
  3. **[sniper_perception_sats_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py) y [cli_active_preset.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/cli_active_preset.json):**
     - En `RECOVERY` sin override, `min_req_q` sube de `0.010` a `0.020`; metadata de preset alineada a `baseline_v73`.
- **Certificación en WSL (Etapa 4 — Entorno Aislado):**
  - [test_sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_sniper_cli.py): `7/7` (`OK`).
  - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): `9/9` escenarios superados (incluye nuevo `TEST 9`).
  - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): `23/23` escenarios superados (`0.718 ms/ciclo`, Walk-Forward OOS `Hybrid Net=+1524 sats`).
  - Simulación `unittest`: `22/22` (`OK`).

### Etapa 22: Fusión Bellman-Bayes, Normalización de Basis, Sync Idempotente y Reentrenamiento Limpio (`--reset`)
- **Estado:** **Completada, certificada al 100%, reentrenada sobre 18M y promovida atómicamente a `testnet` y `mainnet`** (código en commit `ce8712c`).
- **Cambios de Ingeniería Aplicados (7 archivos):**
  1. **[learning_thresholds_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_thresholds_manager.py):** Sincronización de `FALLBACK_THRESHOLDS` a las **78 claves exactas** de [learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json) y [sniper_cli_catalog.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_cli_catalog.py) (`breakeven_safety_margin`, `bayesian_prior_alpha=9.0`, `bayesian_prior_beta=11.0`, `fractional_kelly_scale=0.25`, `loss_penalty_multiplier=1.0`, `mainnet_epsilon=0.0`, `fer_cooldown_mult=2.0` y calibración `market_sensitivity`).
  2. **[sniper_perception_sats_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py):** Compuerta `BLOCKED_SUB_FEE_VOLATILITY_SCALP` (`atr/price < 0.0014` sin override en `REVERSION`/`DEF_SCALP`), y fusión Bellman-Bayes (`q_val`, `state_visits`) en `estimate_bayesian_win_rate` y `verify_net_sats_expectancy` preservando las penalizaciones por régimen (`z_lcb = 1.45`) y la conjunción estricta `probabilistic_ok`.
  3. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):** Helper `_normalize_ohlc_basis` (`BTCUSDT` $\rightarrow$ `BTCUSD` sin escalar volumen) conectado en `compute_indicators`, `_build_indicator_prefix_state` y `MultiTimeframeCache.get_indicators_fast` (`prefix_has_basis`); recarga `sync_from_disk_if_idle()` al inicio del ciclo en `main()` y preservación de `synced_test_visits` en `save_brain`; eliminación de inflación de `state_visits` en las 3 ramas de `choose_action` trasladando el conteo a `remember_and_train`; `Progressive Cash-In Lock` (`client.cash_in`) en `execute_strategy_guardian` con consolidación de `cashed_in_sats` en `process_settlement_queue`; paso de `q_val` y `state_visits` en `evaluate_and_dispatch_entries`.
  4. **[sync_brains_dual.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sync_brains_dual.py):** Marca de agua idempotente `synced_test_visits` (`delta_t_visits`) y exclusión mutua `FileLockContext` anti-TOCTOU.
  5. **[sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_cli.py):** Preservación de los defaults nativos de `testnet` en `_patched_configure` cuando no existan overrides explícitos.
  6. **[simulation/sim_training_environment.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py):** Paso de `q_val_entry` y `st_visits_entry` a `estimate_bayesian_win_rate` y `verify_net_sats_expectancy`.
  7. **[test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py):** Incorporación de `TEST 10` (78 umbrales, invarianza al basis `BTCUSD`/`BTCUSDT`, idempotencia `synced_test_visits` y no-inflación de `state_visits`).
- **Reentrenamiento Offline 18M (`--reset --fetch-binance --episodes 60 --bars 8000`):**
  - Dataset real de `788,400` velas en [binance_btc_1m.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/data/binance_btc_1m.json).
  - Candidato generado bajo las ecuaciones de la Etapa 22: [q_brain_simulated_candidate.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_simulated_candidate.json) (`343` estados, `2,763` cierres consolidados, `SHA-256: 339b4b456abddf7f0a9217254acb2183afd180a423819e093d088f7c34fa5cbc`) y [hedge_state_simulated_candidate.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_simulated_candidate.json) (`205` estados Q, `SHA-256: 9a11aa0c00b2d663...`).
- **Certificación Monte Carlo (`50` caminos $\times$ `450` velas = `22,500` barras, [candidate_certification_report.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/candidate_certification_report.json)):**
  - **Promoción `testnet`:** **`WinRate: 85.69%`**, **`Profit Factor: 3.64`**, **`MaxDD: 5.85%`**, `Risk of Ruin: 0.0%`, `mutations_detected: 0`.
  - **Promoción `mainnet`:** `899` operaciones (`797W / 102L`), **`WinRate: 88.65%`**, **`Profit Factor: 4.76`** (`Gross Profit: +364,615 sats` / `Gross Loss: -76,623 sats`), **`Net PnL: +287,992 sats`**, `FER: 0.92`, **`MaxDD: 5.91%`**, `Risk of Ruin: 0.0%`, `mutations_detected: 0`.
- **Limpieza de Contadores para Arranque en `NORMAL` (verificado con `strict_sha=True` y cold start a `100,000 sats` $\rightarrow$ `mode=NORMAL`, `floor=75,000`):**
  - [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json) (`343` estados, `epsilon=0.0`, `SHA-256: fa1d602e586c2a9b20d6e3c9fc786e31284cd5e42190df565a1fd9358728beaf`)
  - [q_brain_testnet_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_testnet_lnm.json) (`343` estados, `epsilon=0.18`, `SHA-256: 6b80792bf17682544607f5db34fa5f3df3542872899634e70ea91477504c1cf0`)
  - [hedge_state_mainnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_mainnet.json) (`205` estados, `SHA-256: de2253abce473fb5c63250fe4670aae5a02fe4f18ccb358d0342e648853161bd`)
  - [hedge_state_testnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_testnet.json) (`205` estados, `SHA-256: 39e6ad4093c4a870667dd297ad6c864c87a77aefd24f7c3df5bbdf646f191734`)
- **Regresión Post-Promoción en WSL (`Ubuntu`):**
  - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): **`10/10`** escenarios superados (incluye `TEST 10`).
  - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): **`23/23`** escenarios superados (`0.759 ms/ciclo`, Walk-Forward OOS `Hybrid Net=+1524 sats`, `MaxDD=0.53%`, `PF=1.41`).
  - `unittest` (`test_simulation_engine.py`, `test_terminal_bellman_fix.py`, `test_candidate_certification.py`, `test_simulation_training_loop.py`): `22/22` (`OK`).
  - [test_sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_sniper_cli.py): `7/7` (`OK`).
  - Congruencia `FALLBACK_THRESHOLDS` vs `PARAM_OPERATIONAL_GUIDE`: `78/78` (`100% CONGRUENTE`).

### Etapa 23: Normalización Dimensional Canónica (1,000 sats), Aislamiento de Umbrales por Entorno y Gemelo Digital Sombra
- **Estado:** **Completada, certificada al 100% en WSL y aplicada en vivo sobre Mainnet (`SIGHUP`) y Testnet (`restart`)**.
- **Cambios de Ingeniería Aplicados:**
  1. **Aislamiento Mainnet/Testnet de Umbrales y SIGHUP ([learning_thresholds_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_thresholds_manager.py), [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py), [sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_cli.py)):**
     - Enrutamiento por entorno vía `resolve_thresholds_path_for_env()` y `LearningThresholdsManager.switch_environment()` invocado dentro de `Config.configure_environment()`.
     - Restauración de `baseline_v73` en [config/learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json) (`bayesian_prior_alpha=7.0`, `bayesian_prior_beta=15.0`, `breakeven_safety_margin=0.04`, `epsilon_explorer_min=0.08`, `epsilon_explorer_initial=0.28`).
     - Creación de [config/learning_thresholds_testnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds_testnet.json) calibrado para exploración sin colapso de Win Rate (`bayesian_prior_alpha=9.0`, `bayesian_prior_beta=10.0`, `breakeven_safety_margin=0.02`, `epsilon_explorer_min=0.12`, `epsilon_explorer_initial=0.24`).
     - `SniperControlCenter` actualizado con `env_mode`, `get_thresholds_path(env_mode)`, `notify_hot_reload_signal(env_mode)` dirigido exclusivamente al PID del entorno objetivo, `--env` en `set`, y corrección de liveness en `check_daemon_status` / `manage_daemon("stop")` + fallback de endpoint Testnet (`https://api.signet.lnmarkets.com/v3`).
  2. **Invarianza Dimensional por Margen Canónico ($M_{\text{ref}} = 1,000\text{ sats}$) ([learning_quality_supervisor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py), [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py)):**
     - Factor $\eta(M) = 1000.0 / \max(1000.0, M)$ aplicado en `AutonomousGoalBrain.remember_and_train` (preservando la contabilidad real en satoshis íntegra), `LearningQualitySupervisor.normalize_to_canonical_margin`, `record_closed_trade` y `record_shadow_closed_trade` (con avance continuo de histéresis en modo sombra y sanitización `[-250, +250]` de outliers legados en `get_q_bias_stats` / `get_rl_metrics_snapshot`).
  3. **Gemelo Digital del Guardián en `update_shadow_trades` ([lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py)):**
     - Breakeven Lock (`be_floor`/`be_ceil` con `profit_lock_offset = fee_offset * 1.55`), Dynamic Trailing Stop por estrategia (`trail_trig_atr`, `trail_slack_atr`), estado `CLOSED_TRAIL_PROFIT`, paso de `ind_5m` desde `main()` y actualización Bellman amortiguada normalizada a $1,000\text{ sats}$ (`norm_net_sats_1k`).
  4. **Ventana Móvil de Certificación en [audit_shadow_convergence.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/audit_shadow_convergence.py):**
     - Separación entre histórico append-only y Ventana Móvil de los últimos 20 cierres virtuales (`rolling_20`), incluyendo `CLOSED_TRAIL_PROFIT`, `Rolling_20_Norm_PnL_1k`, Win Rate y `Rolling_20_Virtual_FER` (objetivo `>= 1.20`).
- **Certificación en WSL (`Ubuntu`):**
  - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): **`11/11`** escenarios superados (incluye `TEST 11`).
  - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): **`23/23`** escenarios superados.
  - [test_sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_sniper_cli.py): **`8/8`** pruebas superadas (incluye `test_08`).
  - Simulación `unittest`: **`22/22`** (`OK`); Integridad SHA-256 (`strict_sha=True`): `MAIN=True`, `TEST=True`, `HEDGE_MAIN=True`, `HEDGE_TEST=True`.

### Etapa 24: Blindaje Integral del Hedge Engine y Conciliación Anti-Equity-Fantasma
- **Estado:** **Completada, certificada al 100% en WSL y desplegada en vivo sobre Mainnet (`PID 2671`) y Testnet (`PID 2672`)**.
- **Parches Estructurales Aplicados (6 módulos core + tests):**
  1. **[hedge_state_store.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_store.py):** Incorporados `orig_opened_at`, `hedges_opened_count`, `hedge_entry_fee_sats` y `hedge_opened_at` por par; cooldown de reconciliación ampliado a `60.0s`.
  2. **[adaptive_controller.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/adaptive_controller.py):** Histéresis física y temporal endurecida (`MIN_PRIMARY_AGE_SEC=60.0`, `MIN_HEDGE_HOLD_SEC=45.0`, `CANDIDATE_MIN_ADVERSE_PNL_ATR=-0.60`, `CANDIDATE_MIN_LOSS_MARGIN_RATIO=0.04`, `MAX_HEDGES_PER_PAIR=1`, `MIN_CANDIDATE_STREAK=3`, `MIN_RECOVERY_STREAK=4`, `warning_streak>=2`); escalonamiento obligatorio (`PARTIAL_HEDGE` único permitido en primera pata); inclusión de `NO_HEDGE` en `RECOVERY` para prohibir desarmes prematuros a pérdida.
  3. **[hedge_risk_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_risk_manager.py):** Bloqueos duros `BLOCKED_INCREASE_REQUIRES_PARTIAL_FIRST`, `BLOCKED_MAX_HEDGES_PER_PAIR`, `BLOCKED_HEDGE_PRIMARY_GRACE_PERIOD`, `BLOCKED_HEDGE_NO_PHYSICAL_LOSS`; `PARTIAL_HEDGE` acotado a `h ∈ [0.40, 0.55]`; `BLOCKED_HEDGE_SQUEEZE_WHIPSAW_RISK` extendido a `INCREASE_HEDGE`; `min_fee_mult` unificado (`1.85` en rango/squeeze/chop, `1.65` en tendencia/expansión).
  4. **[dynamic_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/dynamic_hedge.py):** Registro de `hedge_entry_fee_sats`, `hedge_opened_at` y `hedges_opened_count` al abrir (`cooldown=45s`), y contabilidad round-trip exacta `net_hedge_gain = h_pl - exit_fee - entry_fee` al cerrar (`cooldown=60s`).
  5. **[predictive_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/predictive_hedge.py):** Exigencia física `pnl_atr <= -0.50` y `fee_eff_mult` unificado (`1.85` / `1.65`) para recomendar cobertura.
  6. **[lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):** Registro de `realized_hedge_net_sats` / `realized_hedge_fee_sats` al cerrar cobertura huérfana en `reconcile_portfolio_with_hedges`, y refresco inmediato `client.get_user(force_refresh=True)` en `main()` cuando `len(running) < pre_reconcile_count`, erradicando la caída fantasma de equity (`25%–29.9% DD`) y falsos Kill-Switch.
- **Certificación en WSL (`Ubuntu`):**
  - [test_anti_whipsaw_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_anti_whipsaw_hedge.py): **`7/7`** pruebas dedicadas aprobadas (`OK`).
  - [test_learning_quality_and_sats.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_learning_quality_and_sats.py): **`11/11`** escenarios aprobados.
  - [test_protocolo_v_reversal.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_protocolo_v_reversal.py): **`23/23`** escenarios aprobados (Walk-Forward OOS: `Hybrid Net=+2544 sats`, `MaxDD=0.51%`, `PF=1.45`, `Evitado=2283 sats`, `Empeoró=0`).
  - [test_sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_sniper_cli.py): **`8/8`** (`OK`) y simulación `unittest`: **`22/22`** (`OK`).
  - Integridad SHA-256 (`strict_sha=True`): `MAIN=True`, `TEST=True`, `HEDGE_MAIN=True`, `HEDGE_TEST=True`.

### Etapa 25: Compuerta de Viabilidad Neta en FORCED EDGE EXIT, Anti-Fee-Drag en Cash-In, Sanitización de Entropía y Auto-Sync Clock HTTP 401
- **Estado:** **Completada y Certificada al 100% en WSL (`Ubuntu`)**.
- **Parches Quirúrgicos Aplicados:**
  1. **[edge_monitor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/edge_monitor.py) y [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):** Neutralización del prior negativo $Q_{\text{eff}} = \max(0.0, Q_{\text{cql}})$ durante exploración (`is_exploratory=True`) o ventana de maduración ($\Delta t < 180\text{s} \land \text{pnl\_atr} > -0.45$), cota inferior `edge_score >= -0.65` en primeros $90\text{s}$ ($-0.35 < \text{pnl\_atr} < 0.45$), y Compuerta de Viabilidad Neta en `[FORCED EDGE EXIT]` que prohíbe liquidar en zona muerta de comisiones ($\Pi_{\text{net}} = \Pi_{\text{gross}} - F_{\text{RT}} < \max(15, 0.25 F_{\text{RT}})$) o sin pérdida física confirmada ($\ge \max(30, 0.04 M)$).
  2. **[hedge_state_store.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_store.py), [adaptive_controller.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/adaptive_controller.py), [hedge_risk_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_risk_manager.py) y [dynamic_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/dynamic_hedge.py):** Contadores `orig_reductions_count` / `hedge_reductions_count` (máx. 1 reducción parcial por pata, `cooldown = 60.0s`) y umbral mínimo de fricción (`orig_pl >= 1.50 * orig_rt_fee` / `hedge_pl >= 1.35 * hedge_rt_fee`).
  3. **[learning_quality_supervisor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py) y [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):** Método `discard_last_action_decision()` invocado ante vetos pre-despacho (`[GUARDIA ANTI-STACKING]`, filtros micro/pre-flight y cupo lleno) para que $H_{\text{norm}}$ mida únicamente decisiones ejecutables.
  4. **[guardian_execution_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/guardian_execution_core.py) y [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py):** Auto-sincronización de `_server_time_offset_ms` en `ResilientLNMClient` / `LNMarketsClient` ante `HTTP 401 outdated timestamp` con recálculo HMAC-SHA256 y reintento inmediato en el bucle.
  5. **[learning_quality_supervisor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/learning_quality_supervisor.py), [audit_shadow_convergence.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/audit_shadow_convergence.py) y [.gitignore](file:///c:/Users/mberm/OneDrive/Documentos/sniper/.gitignore):** Aislamiento de registros sintéticos hacia `logs/transitions_audit_sim.jsonl`, purga de 31,191 líneas sintéticas en `logs/transitions_audit.jsonl` (conservando 31 cierres reales con UUID) y complemento `LIVE_SIGNET` / `SHADOW_VIRTUAL` en `Rolling_20`.
- **Certificación en WSL (`Ubuntu`):** `test_anti_whipsaw_hedge.py` (`9/9 OK`), `test_learning_quality_and_sats.py` (`12/12 OK`), `test_protocolo_v_reversal.py` (`23/23 OK`), `unittest` (`22/22 OK`), `test_sniper_cli.py` (`8/8 OK`), SHA-256 `strict_sha=True` (`4/4 True`).

### Etapa 26: Auditoría y Alineación de Paridad 100% del Simulador y Pipeline de Entrenamiento Offline
- **Estado:** **Completada y Certificada al 100% en WSL (`Ubuntu`)**.
- **Parches de Paridad Aplicados:**
  1. **[edge_monitor.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/edge_monitor.py), [hedge_state_store.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_store.py), [portfolio_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/portfolio_hedge.py), [hedge_risk_manager.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_risk_manager.py) y [dynamic_hedge.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/dynamic_hedge.py):** Normalización bipolar de dirección (`"b"`/`"s"` $\leftrightarrow$ `"buy"`/`"sell"`), resolución de `orig_opened_at` con `open_ts` y soporte de retorno 2-tupla `(bool, dict)` en `close_trade` de clientes simulados (`SimLNMExchange` y `WalkForwardSimClient`).
  2. **[adaptive_controller.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/adaptive_controller.py) y [simulation/sim_lnm_exchange.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_lnm_exchange.py):** Parámetro `learning_enabled=True` en `evaluate_and_manage_pair` para garantizar 0 mutaciones en evaluaciones Monte Carlo (`learning_enabled=False`), e incorporación de `update_stoploss` y `update_takeprofit` en `SimLNMExchange`.
  3. **[simulation/sim_training_environment.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py):**
     - Integración de `EdgeMonitor.evaluate_position_edge` con `q_val_action` real y `orig_meta` completo (`entry_time`, `is_exploratory`) y las 3 reglas de la **Compuerta de Viabilidad Neta en `FORCED_EDGE_EXIT`**.
     - Orquestación completa en `step()` de `PortfolioHedgeCoordinator.reconcile_portfolio` + `AdaptiveHedgeController.evaluate_and_manage_pair` (invocando `select_hedge_action`, `HedgeRiskManager.authorize_hedge_action` y `DynamicHedgeExecutor.execute_hedge_action`), separación estricta de patas `_is_hedge` y consolidación de coberturas huérfanas (`_consolidate_orphan_hedge`, `_settle_primary_event`).
     - Propagación completa de `predicted_q_entry`, `mae_sats`, `mfe_sats` y `duration_ticks` en `EPISODE_END` para ejecutar `LearningQualitySupervisor.normalize_to_canonical_margin` en el 100% de los cierres, y llamada a `discard_last_action_decision()` ante vetos pre-despacho.
  4. **[test_simulation_training_loop.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_simulation_training_loop.py):** Incorporado `test_simulator_parity_patched_functions` certificando los 4 ejes auditados.
- **Certificación en WSL (`Ubuntu`):** `unittest` (`32/32 OK`), `test_sniper_cli.py` (`8/8 OK`), `test_learning_quality_and_sats.py` (`12/12 OK`), `test_protocolo_v_reversal.py` (`23/23 OK`, Walk-Forward OOS `Hybrid Net=+2544 sats`, `MaxDD=0.51%`, `PF=1.45`), SHA-256 `strict_sha=True` (`4/4 True`).
- **Reentrenamiento Offline 18M y Promoción Dual (`--reset --fetch-binance --episodes 60 --bars 8000`):**
  - **Candidato Generado:** `360` estados Q, `2,826` cierres consolidados en [q_brain_simulated_candidate.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_simulated_candidate.json) (`SHA-256: e6b2221228ddbbf2...`) y `162` estados Q en [hedge_state_simulated_candidate.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_simulated_candidate.json) (`SHA-256: eaf2b6b7b5e2c1b4...`).
  - **Certificación Monte Carlo (`50` caminos × `450` velas):**
    * **Testnet:** `608` operaciones (`541W / 67L`), **`WinRate: 88.98%`**, **`Profit Factor: 4.42`**, **`Net PnL: +175,826 sats`**, **`MaxDD: 5.71%`**, `Risk of Ruin: 0.0%`, `mutations_detected: 0`.
    * **Mainnet:** `594` operaciones (`540W / 54L`), **`WinRate: 90.91%`**, **`Profit Factor: 5.93`**, **`Net PnL: +189,331 sats`**, **`MaxDD: 5.61%`**, `Risk of Ruin: 0.0%`, `mutations_detected: 0` ([candidate_certification_report.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/candidate_certification_report.json)).
  - **Arranque Limpio en `NORMAL` y Verificación SHA-256 (`strict_sha=True`):**
    * [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json) (`360` estados, `epsilon=0.0`, `SHA-256: 986eff5ca876526f...`)
    * [q_brain_testnet_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_testnet_lnm.json) (`360` estados, `epsilon=0.18`, `SHA-256: 8b7713e2f705a31e...`)
    * [hedge_state_mainnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_mainnet.json) (`162` estados, `SHA-256: e98eb4584ac5384e...`)
    * [hedge_state_testnet.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/hedge_state_testnet.json) (`162` estados, `SHA-256: 780a54f95c36ef56...`)
  - **Limpieza de Repositorio:** Eliminados 26 residuos temporales/backups (`.snapshot_guard*`, `.pre_promote_*`, `.pre_surgery_*`, `.pre_v2_schema.bak`, `.recalibrated.json*`, `.sniper_*.pid*`, locks obsoletos), desrastreados los logs rotados (`sniper_mainnet.log.1`, `sniper_mainnet.log.2`, `sniper_testnet.log.1`) y añadida la regla `*.log.*` en [.gitignore](file:///c:/Users/mberm/OneDrive/Documentos/sniper/.gitignore).

### Etapa 27: Consolidación de `C1_STREAK3_24H_INV` (Política Determinista Simétrica, `E2_FE_OFF`, `C1_SHORT_EXP_PHYS` y Freno por Lado Persistente POSIX `fcntl` + SHA-256)
- **Estado:** **Completada y Certificada al 100% en WSL (`Ubuntu`) mediante 4 Commits Atómicos (Fases 0 a 4)**.
- **Evidencia Empírica de Investigación (18 Meses Reales `M01..M18`, 2 Folds Cruzados OOS `12M` con Purga $\ge 24\text{h}$ y Matemática de Futuros Inversos `inverse_contract_math = True`):**
  1. **Módulos y Ramas Descartados Definitivamente (Falsificados Empíricamente):**
     - **Percepción v2 (`Wilder ADX(14)` + histeresis):** Descartada. El suavizado doble llegó tarde a rupturas rápidas y perdió `-30,657 sats` de `GrossPnL` en Train frente a Percepción v1 (`DX EMA(14)` + `RANGE_SQUEEZE`).
     - **Reentrenamiento Q-learning Tabular (`SCALE_1600`) y TD Online:** Descartado en vivo (`DETERMINISTIC_TREND_POLICY = True`). Sufre colapso por *state aliasing* (`TREND_BULL|PEAK|LOW_EXP|BULL|FULL_BULL` absorbe `1,156` trades en Fold 1 y apaga `M16 BULL`) y parálisis 100% `HOLD` por `lcb_penalty` cuando `visits = 0`. Se preserva el esquema de 9 acciones (`0..8`) y llave de 5 segmentos intacto por contrato P0 #1, con `q_table` congelada en producción.
     - **`FORCED_EDGE_EXIT` en `TREND` y `Hybrid Hedge Engine`:** Descartados (`FE_OFF_TREND = True`, `ENABLE_HYBRID_HEDGE = False`). Apagar `FORCED_EDGE_EXIT` en `TREND` (`E2_FE_OFF`) sumó **`+15,250 sats`** en 18M y eliminó las salidas prematuras antes del piso `sl_floor = 0.65%`.
     - **Entradas `LIMIT` en Retroceso (`D2`/`D3`) y `LONG_ONLY` (`D1`):** Descartadas (`limit_execution.enabled = 0`). Sufren selección adversa severa (pierden el 25% de las mejores rupturas y desploman `GrossPnL` un 52%).
     - **Veto/Sesgo de `liquidity_radar.py` (`BLOCKED_OPPOSITE_LIQ_WALL`) en `TREND`:** Descartado para `TREND` (`shadow_only = 1`). Los 44 trades adyacentes a muros `EQH/EQL` tuvieron `63.64% WR` y `+1,633 sats` (`PF 1.17`); en rupturas tendenciales el pool opuesto actúa como imán de liquidez.
     - **Soft-Sizing Continuo Online (`OnlineExpectedRModel` / `FastSoftExpectedRModel` `B1..B3`, `C2..C4B`):** Descartado. Reducir tamaño a `0.25x..0.55x` impidió alcanzar `recovery_mode (3.5%)` / `dd_throttle_halt (4.5%)` en meses laterales (inflando trades en `RANGE` de `334` a `619..924`) y provocó *whipsaw* intra-rally en `M16 BULL` (`63.5%..68.5%` de captura).
  2. **Arquitectura Certificada `C1_STREAK3_24H_INV` (`4/4 PASS` en Fase 3):**
     - **Selección Determinista Simétrica (`P1_NO_CASCADE_SYM` + `G1_STRICT_DIR` + `H4` + `OTR = 0`):** Top-1 candidato sin cascada (`max_cands = 1`), inicialización `_init_q_vector` estrictamente simétrica sin sesgo `BULL > BEAR`, empate inter-lado $\rightarrow$ `HOLD (0)`, `lcb_penalty = 0.0`, `action in (0, 1, 5)` (`OTR = 0`), y `recovery_mode` (`DD >= 3.5%`) devolviendo `HOLD (0)` limpio sin remapear `TREND (1, 5)` a `SNIPER (3, 7)`.
     - **Asimetría Física en Cortos (`C1_SHORT_EXP_PHYS`):** `L_TREND (1)` opera a `1.00x` con piso `TREND_SL_FLOOR_PCT = 0.0065` (`0.65%`). `S_TREND (5)` exige expansión orgánica de volatilidad **`sl_dist_pct > 0.006505`** ($2 \times \text{ATR}_{5m} > 0.65\%$); cuando `sl_dist_pct <= 0.006505` (mercado calmo donde `S_TREND` tenía `PF 0.56` y `-37,463 sats` en Train), bloquea a `HOLD (0)` (`BLOCKED_SHORT_NO_EXPANSION`).
     - **Freno Discreto por Lado Persistente (`C1_STREAK3_24H`, [side_streak_brake.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/side_streak_brake.py)):** `SideStreakBrakeStore` mantiene contadores independientes `side_loss_streak[s]` y `side_pause_until_ts[s]` para $s \in \{\text{'b'}, \text{'s'}\}$. Al acumular `3` pérdidas consecutivas (`net_pnl_sats <= 0`) en el lado $s$, pausa únicamente ese lado por `24 horas` (`+86,400 s`) y reinicia `side_loss_streak[s] = 0`, dejando el lado opuesto libre a `1.00x`. Se persiste atómicamente (`tmp + os.fsync + os.replace`) con bloqueo POSIX `fcntl` (`FileLockContext`) y verificación `.sha256` en `$HOME/.sniper_state/side_brake_state_{env}.json` únicamente al cerrar un trade.
  3. **Métricas Certificadas en Fase 3 (`C1_STREAK3_24H_INV`, `inverse_contract_math = True`):**
     - **`12M OOS` (Fold 1 + Fold 2, `0.20% RT`):** **`+18,092 sats`** (`PF = 1.15`, `536` trades, `WR = 49.44%`, `MaxDD = 4.40%`, `0/12` meses con `DD >= 4.5%`, `DDG = 0`, `PosMo = 5/12`; sube a **`+31,603 sats`, `PF = 1.27`** a `0.15% RT` y **`+45,104 sats`, `PF = 1.41`** a `0.10% RT`).
     - **`Total 18M` (`0.20% RT`):** **`-8,196 sats`** (`PF = 0.96`, `MaxDD = 4.66%`, `1/18` meses con `DD >= 4.5%`, `DDG = 0`; pasa a **`+14,938 sats`, `PF = 1.07`** a `0.15% RT` y **`+38,049 sats`, `PF = 1.19`** a `0.10% RT`).
     - **Motores Tendenciales Intactos (`INV`):** **`M16 BULL = +31,746 sats`** (`100.0%` de captura, `52` trades, `75.0% WR`, `DD = 3.87%`), **`M02 BULL = +12,750 sats`**, **`M09 BEAR = +8,392 sats`** (`S_TREND = +9,508`).
     - **Estrés de Microestructura (`+50%` slippage + `1-bar` latencia adversa en `12M OOS`):** **`+13,488 sats`** (`PF = 1.11`, `MaxDD = 4.63%`, `DDG = 0`).
     - **Monte Carlo (`1,000` trayectorias block-bootstrap, `seed=20260331`, `12M OOS @ 0.20% RT`):** **`P(Net > 0) = 62.10%`**, **`Mediana (P50) = +9,790 sats`** (`PF P50 = 1.08`), **`P95 RealDD = 3.83%`**, **`P95 BarDD = 4.31%`**, **`P(DDG >= 5.2%) = 0.00%`**, **`P(HardFloor) = 0.00%`**.
     - **WFO Rolling `2M` (`9` ventanas OOS de 2 meses):** **`+16,593 sats`** (`PF = 1.10`, `5/9` ventanas positivas, `MaxDD = 4.66%`, `DDG = 0`).
  4. **Archivos Versionados Refactorizados en Fase 4 (4 Commits Atómicos en WSL):**
     - Commit 1 (`refactor(core)`): [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py) y [sniper_perception_sats_core.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_perception_sats_core.py).
     - Commit 2 (`feat(risk)`): [side_streak_brake.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/side_streak_brake.py) y conexión en [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py).
     - Commit 3 (`feat(sim-cli)`): [simulation/sim_training_environment.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/simulation/sim_training_environment.py), [sniper_cli.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_cli.py) y [sniper_cli_catalog.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/sniper_cli_catalog.py).
     - Commit 4 (`test(regression)`): [test_c1_streak3_24h_production.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/test_c1_streak3_24h_production.py) y actualización de [CONTEXT_LNM_SNIPER.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/CONTEXT_LNM_SNIPER.md).

---

## 3. ARQUITECTURA DE ARCHIVOS CLAVE

```text
sniper/
├── config/
│   └── learning_thresholds.json     # Fuente única de verdad de los 10 grupos de umbrales
├── learning_thresholds_manager.py   # Gestor con Hot-Reload atómico y validación de rangos
├── learning_quality_supervisor.py   # Motor de supervisión matemática en tiempo real
├── telemetry_live.json              # Telemetría headless exportada en tiempo real
├── lnm_sniper_v6_advanced.py        # Bucle principal, cerebro Q-learning y ejecución
├── sniper_perception_sats_core.py   # Compuertas de expectancy, régimen y percepción
├── guardian_execution_core.py       # HardFloorGuardian y protecciones P0 de capital
├── test_learning_quality_and_sats.py# Tests de validación de aprendizaje y umbrales
├── test_protocolo_v_reversal.py     # Suite de 23 escenarios de regresión cuantitativa
├── AGENT_MAINTENANCE_DIRECTIVE.md   # Directiva maestra de gobernanza para agentes
└── CONTEXT_LNM_SNIPER.md            # Memoria persistente del proyecto (este archivo)
```

---

## 4. INSTRUCCIONES PARA EL PRÓXIMO AGENTE

1. **Lee primero [AGENT_MAINTENANCE_DIRECTIVE.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/AGENT_MAINTENANCE_DIRECTIVE.md)** antes de realizar cualquier acción o sugerencia.
2. **Trabaja obligatoriamente por etapas secuenciales:** No saltes directamente a editar código; ejecuta primero la Etapa 1 de inspección de solo lectura, emite el diagnóstico y **espera la aprobación explícita del humano**.
3. **No alteres la estructura de 9 acciones discretas** ni el esquema de serialización de [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json).
4. **Preserva la fórmula contable de satoshis líquidos reales** y la subordinación jerárquica ante el `Unified Guardian`.
5. **Actualiza este archivo [CONTEXT_LNM_SNIPER.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/CONTEXT_LNM_SNIPER.md)** con cada avance o decisión técnica aprobada.
