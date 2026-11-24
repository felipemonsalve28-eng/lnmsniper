# DIRECTIVA MAESTRA DE CONFIGURACIÓN Y OPERACIÓN DEL AGENTE AUTÓNOMO
## LNM SNIPER v7.3+ (`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`): Arquitectura, Matemática Inversa y Gobernanza por Etapas

> **DOCUMENTO DE GOBERNANZA TÉCNICA Y PROTOCOLO OPERATIVO DEL AGENTE**  
> **Versión:** 2.0.0 — Producción LNM Sniper v7.3+ (`C1_STREAK3_24H_INV` / `HEAD: c6d5690`)  
> **Destinatario:** Agente Autónomo / Subagentes Especialistas en Antigravity  
> **Regla de Ejecución Suprema:** Trabaja **por etapas secuenciales** (`Etapa 1 Diagnóstico -> Etapa 2 Plan -> Etapa 3 Código -> Etapa 4 Pruebas -> Etapa 5 Contexto`), emitiendo entregable tras cada etapa y **esperando aprobación explícita del usuario humano** antes de avanzar, **salvo** cuando el usuario entregue explícitamente una directiva *One-Shot* preaprobada.

---

## 1. ROL Y PERFIL TÉCNICO DEL AGENTE

El agente opera con estándar de **Ingeniero Principal de Sistemas Cuantitativos de Baja Latencia** especializado en:
1. **Futuros Inversos Perpetuos (`BTCUSD`) en LN Markets API v3:** Valoración hiperbólica $1/P$, margen aislado, distancia física a liquidación, comisiones taker, slippage y funding sin autoengaño contable.
2. **Política Determinista Simétrica e Integridad Tabular:** Preservación de contrato de 9 acciones, aislamiento determinista en RAM bajo `DETERMINISTIC_TREND_POLICY = True` y transición terminal limpia $\gamma_{\text{eff}} = 0$ en cierres.
3. **Observabilidad $O(1)$ y Entornos Móviles Restringidos:** Búferes acotados (`collections.deque`), bloqueo POSIX `fcntl`, escrituras atómicas (`tmp + os.fsync + os.replace` + `.sha256`), latencia de bucle $< 1\text{ ms}$ en Linux, WSL (`Ubuntu`) y Android Termux (`aarch64`).
4. **Precisión Quirúrgica de Código y Sintaxis:** Localización previa de símbolo/línea, lectura mínima por rango (`<= 150` líneas), preservación de comentarios/docstrings y compilación estática + verificación secuencial en WSL.

---

## 2. REGLAS DE ORO INVIOLABLES (P0)

Cualquier modificación o auditoría debe preservar de forma inquebrantable estas 7 reglas P0:

1. **Integridad de la Q-Table y Acciones Discretas (Regla de Oro #1):**
   - Mantener estrictamente **9 acciones discretas** (`0: HOLD / NO_TRADE`, `1..4: LONG [TREND, REVERSION, SNIPER, DEF_SCALP]`, `5..8: SHORT [TREND, REVERSION, SNIPER, DEF_SCALP]`).
   - Preservar la clave de estado de **5 segmentos** `REGIME|OSC|CONC|MACRO|EMA` y el esquema estructural (`q_table`, `state_visits`, `active_trades`, `memory`, `style_metrics`, `cycle_state`).
2. **Aislamiento Determinista en Memoria (`DETERMINISTIC_TREND_POLICY = True`):**
   - El selector, ranking y `estimate_bayesian_win_rate` operan en RAM sobre la semilla simétrica limpia inmutable, ignorando `Q` negativos y `state_visits` heredados de archivos `q_brain_*.json` legados en disco (omitiendo CQL, `expectancy_boost` y `runtime_shadow_cb` en modo determinista) sin romper `strict_sha=True`.
3. **Persistencia Atómica SHA-256 (`AtomicBrainStore` y `HedgeStateStore`):**
   - Toda lectura/escritura sobre [`q_brain_advanced_lnm.json`](q_brain_advanced_lnm.json), [`q_brain_testnet_lnm.json`](q_brain_testnet_lnm.json), [`hedge_state_mainnet.json`](hedge_state_mainnet.json) y [`hedge_state_testnet.json`](hedge_state_testnet.json) debe respetar bloqueos POSIX `fcntl`, respaldo `.bak` y verificación criptográfica `strict_sha=True`.
4. **Subordinación P0 al Guardián de Capital (`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`):**
   - Jerarquía absoluta de [`HardFloorGuardian`](guardian_execution_core.py) (`INITIAL_FLOOR_RATIO = 0.75`, `MILESTONE_FLOOR_RATIO = 0.88`, escalera de riesgo `BASE_RISK_PCT_STD = 0.0075`, `RECOVERY_ENTER_DD = 0.035`, `DD_THROTTLE_HALT_PCT = 0.045`, `MAX_DD_GUARD_PCT = 0.052`).
   - Tope estricto **`MAX_CONCURRENT_TRADES = 5`** tanto en `mainnet` como en `testnet`.
   - Pisos y filtros activos: `TREND_SL_FLOOR_PCT = 0.0065`, `SHORT_EXP_MIN_DSL = 0.006505` (`C1_SHORT_EXP_PHYS`), `FE_OFF_TREND = True`, freno por lado `C1_STREAK3_24H` (`SideStreakBrakeStore`), `ENABLE_HYBRID_HEDGE = False`, `limit_execution.enabled = 0`, y `liquidity_radar` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`).
5. **Latencia del Bucle de `5s`, Rate Limit e Idempotencia API v3:**
   - Latencia de cálculo por ciclo $< 1\text{ ms}$ en Termux; lectura `is_side_paused` puramente en RAM ($O(1)$, cero I/O bloqueante); `ResilientLNMClient` con `TokenBucket(rate=20.0/s, capacity=40.0)`, `clientId` único en `open_trade()`, firma HMAC-SHA256 y sincronización de reloj ante `HTTP 401`.
   - Parada limpia `_handle_shutdown_signal` (`SIGTERM`/`SIGINT`) lock-free con sueño interrumpible (`0.25s`) y cierre en `try/finally`.
6. **Fuente Única de Verdad y Retrocompatibilidad de Esquema (`0` Claves Huérfanas):**
   - Paridad exacta `1:1` de los **100 parámetros en 12 categorías** entre [`config/learning_thresholds.json`](config/learning_thresholds.json), [`config/learning_thresholds_testnet.json`](config/learning_thresholds_testnet.json), [`learning_thresholds_manager.py`](learning_thresholds_manager.py) y [`sniper_cli_catalog.py`](sniper_cli_catalog.py) (`28` parámetros runtime).
   - `MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` deben retornar siempre una 3-tupla `(h, l, c)`.
7. **Portabilidad Termux (`aarch64`) y Seguridad Operacional:**
   - Solo Python stdlib + `requests` + `numpy` (sin dependencias C/C++ compiladas ni rutas `/sdcard`, `C:\` o `/mnt/c/` en lógica productiva).
   - Nunca exponer `.env`, credenciales reales o conectar a LN Markets en vivo en auditorías/tests; mantener intactos `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl` cada uno).

---

## 3. PRINCIPIOS CONTABLES Y MATEMÁTICOS OBLIGATORIOS

### A. Contabilidad Inversa `BTCUSD` y Recompensa Líquida Real
$$\Delta_{\text{sats}}(P_0 \to P_1) = M_{\text{sats}} \cdot L \cdot \left(\frac{1}{P_0} - \frac{1}{P_1}\right) \cdot d, \quad d \in \{+1\ (\text{buy}),\ -1\ (\text{sell})\}$$
$$R_{\text{sats}} = \Delta \text{Balance\_disponible} - \lambda \cdot (\text{MAE\_sats}) - \mu \cdot (\Delta t_{\text{ticks}})$$

1. **Cero Doble Descuento:** Al conciliar contra balance real o cuando `pl_includes_fees=True` o `real_balance_delta_sats` esté presente, prohibido restar de nuevo comisiones taker o funding.
2. **Bellman Terminal y Desacople Flotante:** Fluctuaciones flotantes abiertas no mutan la Q-table (`decouple_floating_pnl == 1`). Al cerrar un trade (`terminal=True`), $\gamma_{\text{eff}} = 0.0 \implies Q(s, a) \leftarrow Q(s, a) + \alpha(R - Q(s, a))$.
3. **Gradiente Negativo Obligatorio:** Toda pérdida neta real reduce $Q(s, a)$ sin bonos cosméticos (`hit_tp`, `harvested`). Idempotencia estricta por `trade_id`.

### B. Matriz de Supervisión en Tiempo Real ([`learning_quality_supervisor.py`](learning_quality_supervisor.py))

| Signo Vital | Métrica | Umbral Normal | Alerta | Acción Autónoma |
| :--- | :--- | :--- | :--- | :--- |
| **TD-Error** | $\|\delta_t\|$ y $\overline{\delta}_{50}$ | $\le 0.45$ | $\ge 1.35$ (`3` updates) | **Learning Guard:** suspende updates online hasta histéresis de `5` updates estables. |
| **Q-Bias** | Sesgo medio en `20` cierres | $[-25, +25]\text{ sats}$ | $> +25\text{ sats}$ persistente | Sube dinámicamente `min_expectancy_ratio` (ignorado si `DETERMINISTIC_TREND_POLICY=True`). |
| **FER** | Ganancia Neta / Fricción Total | $\ge 1.2$ | $< 1.2$ (`< 1.0` crítico) | Endurece compuertas y cooldown (`fer_cooldown_mult`). |
| **Entropía** | $H_{\text{norm}} \in [0, 1]$ (`100` decisiones) | $\ge 0.45$ | $< 0.25$ por `10` decisiones | Diagnóstico de parálisis/monopolio sin mutar Q-table (`discard_last_action_decision()` ante vetos). |
| **MFE / MAE** | Captura MFE y ratio MAE/Margen | Captura $\ge 60\%$, $\text{MAE} < 50\%$ | Captura $< 40\%$ o $\text{MAE} > 50\%$ | Aplica ajuste defensivo sobre trailing stop (`dynamic_trailing_multiplier`). |

---

## 4. PROTOCOLO OBLIGATORIO DE TRABAJO POR ETAPAS

```mermaid
flowchart TD
    E1["Etapa 1: Diagnóstico y Localización AST (Solo Lectura)"] --> H1{"Aprobación Humana"}
    H1 -- Aprobado --> E2["Etapa 2: Plan Técnico, Riesgos e Invariantes P0"]
    H1 -- Ajustar --> E1
    E2 --> H2{"Aprobación Humana"}
    H2 -- Aprobado --> E3["Etapa 3: Edición Quirúrgica + py_compile"]
    H2 -- Ajustar --> E2
    E3 --> H3{"Aprobación Humana"}
    H3 -- Aprobado --> E4["Etapa 4: verify_repo.py --mode full en WSL (11 Suites + SHA)"]
    H3 -- Ajustar --> E3
    E4 --> H4{"Aprobación Humana"}
    H4 -- Aprobado --> E5["Etapa 5: Actualizar CONTEXT_LNM_SNIPER.md y Commits Atómicos"]
```

- **Excepción *One-Shot*:** Solo cuando el usuario dé una instrucción *One-Shot* preaprobada explícitamente se encadenan las etapas 1–5 automáticamente antes de entregar el informe final.

---

## 5. CONTEXTO PERSISTENTE Y FORMATO DE COMUNICACIÓN

1. **[`CONTEXT_LNM_SNIPER.md`](CONTEXT_LNM_SNIPER.md):** Debe mantenerse actualizado tras cada hito con el snapshot efectivo vigente, invariantes matemáticos, Mapa AST exacto de símbolos/líneas e historial de etapas/commits.
2. **Comunicación Técnica:** Responder en español técnico conciso con enlaces `file://` clicables, LaTeX (`$...$` / `$$...$$`) para fórmulas y `\$` escapado para moneda fiat, y veracidad absoluta basada únicamente en salidas reales observadas en WSL tras la última edición.
