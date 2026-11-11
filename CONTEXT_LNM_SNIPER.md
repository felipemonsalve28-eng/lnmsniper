# CONTEXTO OPERATIVO Y MEMORIA PERSISTENTE DEL PROYECTO
## LNM SNIPER v7.3+ (EDICIÓN SATS REALES, OBSERVABILIDAD Y UMBRALES DINÁMICOS)

> **Archivo de Contexto Persistente para Agentes Autónomos e Ingenieros**  
> **Última Actualización:** 2026-11-10  
> **Último Commit en GitHub:** `10ae129` (`master -> origin/master`)  
> **Directiva de Operación:** [AGENT_MAINTENANCE_DIRECTIVE.md](file:///c:/Users/mberm/OneDrive/Documentos/sniper/AGENT_MAINTENANCE_DIRECTIVE.md)

---

## 1. ESTADO ACTUAL DEL SISTEMA

| Componente | Estado | Ubicación / Referencia |
| :--- | :--- | :--- |
| **Rama Git** | `master` (100% limpia y sincronizada con `origin/master`) | Commit `10ae129` |
| **Entorno Primario** | Producción (`mainnet`) | LN Markets API v3 (`https://api.lnmarkets.com/v3`) |
| **Límite de Posiciones** | Máximo 5 posiciones simultáneas | Enforzado en [lnm_sniper_v6_advanced.py](file:///c:/Users/mberm/OneDrive/Documentos/sniper/lnm_sniper_v6_advanced.py) |
| **Piso Duro Unificado** | 75% inicial -> 88% de meta | [HardFloorGuardian](file:///c:/Users/mberm/OneDrive/Documentos/sniper/guardian_execution_core.py) |
| **Cerebro Activo** | 175 estados discretizados, 336 cierres, SHA-256 verificado | [q_brain_advanced_lnm.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/q_brain_advanced_lnm.json) |
| **Fuente de Umbrales** | 10 categorías formales con Hot-Reload atómico | [config/learning_thresholds.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/config/learning_thresholds.json) |
| **Telemetría en Vivo** | Headless atómica activa (exporta cada $\le 5\text{ s}$) | [telemetry_live.json](file:///c:/Users/mberm/OneDrive/Documentos/sniper/telemetry_live.json) |
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
