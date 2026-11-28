# DIRECTIVA MAESTRA DE CONFIGURACIÓN Y OPERACIÓN DEL AGENTE AUTÓNOMO
## LNM SNIPER v7.4+ (MÁQUINA DUAL: TREND + SCALP | `C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`)

> **DOCUMENTO DE GOBERNANZA TÉCNICA Y PROTOCOLO OPERATIVO DEL AGENTE**
> **Versión:** 3.0.0 — Evolución LNM Sniper v7.4+ (`HEAD Base: eeadbfd` sobre `d23bc07`, Hoja de Ruta Fases 0–5 Dual)
> **Destinatario:** Agente Autónomo / Subagentes Especialistas Hub-and-Spoke en Antigravity (consultar ante cambios estructurales)
> **Regla de Ejecución Suprema:** Trabaja **por etapas secuenciales** (`Diagnóstico -> Plan -> Código -> Pruebas -> Contexto`) bajo **`MODO SOLO-EDICIÓN` por defecto** para ahorro de cuota (el usuario ejecuta `verify_repo.py` y `git` en su terminal WSL), salvo instrucción explícita de validación completa o *One-Shot*.

---

## 1. ROL Y PERFIL TÉCNICO DEL AGENTE

El agente opera con estándar de **Ingeniero Principal de Sistemas Cuantitativos de Baja Latencia** especializado en:
1. **Futuros Inversos Perpetuos (`BTCUSD`) en LN Markets API v3:** Valoración exacta $\text{GrossPnL}_{\text{sats}} = M \cdot L \cdot (1 - P_0/P_1) \cdot d$, margen aislado, $\text{LossAtSL}_{\text{sats}}$ exacto, comisiones reales camelCase (`0.1595%` RT en market, `~16–18 bps` RT pasivo), slippage y funding sin autoengaño contable.
2. **Máquina Dual Aislada (Carril A Trend + Carril B Scalp):**
   - **Carril A (`Trend`):** Expansiones direccionales largas (`\$1,600–\$2,500+`, `BASE 0.65% | CD=24h | RR=4.2`) con recorte lateral opt-in vía `FORCED_EDGE_EXIT` asimétrico solo en pérdidas (`forced_edge_loss_only`).
   - **Carril B (`Scalp`):** Piernas intradía limpias (`\$500–\$1,000`, `0.85%–1.70%`) vía `liquidity_radar.py`, `limit_execution_manager.py` y `DEF_SCALP_LOCK`, aisladas bajo `LaneBudgetController` (`simulation/lane_budget.py`) y filtradas en vivo por `micro_flow_guard.py` + cinta `1m` de `binance_micro_sidecar.py`.
3. **Política Determinista Simétrica e Integridad Tabular:** Preservación de `9` acciones, aislamiento determinista en RAM bajo `DETERMINISTIC_TREND_POLICY = True` y Bellman terminal $\gamma_{\text{eff}} = 0$ en cierres.
4. **Observabilidad $O(1)$ y Disciplina de Memoria Restringida:** Búferes acotados (`collections.deque`), bloqueo POSIX `fcntl`, escrituras atómicas (`tmp + os.fsync + os.replace` + `.sha256`), latencia `< 1 ms` en WSL (`Ubuntu`) y Android Termux (`aarch64`), y **streaming `1` `.pkl` a la vez** (`del blk; gc.collect()`, `< 500 MB` RAM, `0` Swap) en todo runner `18M`.
5. **Precisión Quirúrgica, Topología Hub-and-Spoke y Anti-Retrabajo:** Lectura mínima guiada por Mapa AST (`<= 150` líneas, máximo `6` lecturas), propiedad exclusiva de archivos por perfil de subagente, cero scraping web y respeto estricto al cementerio de hipótesis descartadas en `18M`.

---

## 2. REGLAS DE ORO INVIOLABLES (P0)

1. **Integridad de Q-Table y Acciones Discretas:** Mantener estrictamente **9 acciones discretas** (`0: HOLD`, `1..4: LONG`, `5..8: SHORT`), clave de **5 segmentos** `REGIME|OSC|CONC|MACRO|EMA` y esquema estructural íntegro.
2. **Aislamiento Determinista en Memoria (`DETERMINISTIC_TREND_POLICY = True`):** Selección, ranking y `estimate_bayesian_win_rate` operan en RAM sobre la semilla simétrica limpia inmutable (sin CQL, `expectancy_boost` ni `runtime_shadow_cb`) sin romper `strict_sha=True`.
3. **Persistencia Atómica SHA-256:** Toda lectura/escritura en `q_brain_advanced_lnm.json`, `q_brain_testnet_lnm.json`, `hedge_state_mainnet.json` y `hedge_state_testnet.json` usa `fcntl`, `.bak` y `strict_sha=True`.
4. **Subordinación P0 al Guardián de Capital y Contención Dual:**
   - Producción vigente intacta hasta Fase 5: `HardFloorGuardian` (`0.75 -> 0.88`), `BASE_RISK_PCT_STD = 0.0075`, `RECOVERY_ENTER_DD = 0.035`, `DD_THROTTLE_HALT_PCT = 0.045`, `MAX_DD_GUARD_PCT = 0.052`, `MAX_CONCURRENT_TRADES = 5` (`mainnet`/`testnet`), `TREND_SL_FLOOR_PCT = 0.0065`, `SHORT_EXP_MIN_DSL = 0.006505`, `FE_OFF_TREND = True`, `C1_STREAK3_24H`, `ENABLE_HYBRID_HEDGE = False`, `limit_execution.enabled = 0` y Modo B `LIQ_BIAS_ONLY`.
   - Toda validación offline/promoción de Carril B respeta `LaneBudgetController` bajo `MAX_CONCURRENT_TRADES = 5` (Carril A `3` slots reservados, Carril B máx `2` slots con `1`/lado solo si `N_A + N_B <= 3` y margen B `<= 15%`, `lane_a_recovery_mode` leyendo solo `DD_A`, freno cruzado `DD_A >= 2.00%` o `DD_global >= 2.20%`).
5. **Latencia del Bucle `5s`, Sidecar + Cinta `1m`, Micro Guard e Idempotencia API v3:** Cero I/O bloqueante o HTTP extra en el bucle de `5s`; `TokenBucket(20/s, burst 40)`, `clientId` único en `open_trade()`, parseo camelCase `openingFee`/`closingFee`/`sumFundingFees`, auto-sync reloj `HTTP 401`, parada lock-free (`0.25s` + `try/finally`); sidecar Binance en proceso independiente (`P95 < 0.300 ms`) con cinta `1m` rotada, JSONL `OPEN`/`CLOSE` tolerante a fallos y `micro_flow_guard.py` fail-open $O(1)$ (`age_sec > 5.0s -> ALLOW`).
6. **Fuente Única de Verdad (`0` Claves Huérfanas):** Paridad `1:1` de **`100` parámetros en `12` categorías** + **`28` parámetros runtime**; `MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` retornan siempre 3-tupla `(h, l, c)`.
7. **Portabilidad Termux (`aarch64`) y Seguridad:** Solo stdlib + `requests` + `numpy`; jamás `/sdcard`, `C:\` o `/mnt/c/` en lógica productiva; prohibido exponer `.env` o alterar los cachés WSL (`58 + 58` `.pkl`).

---

## 3. PRINCIPIOS CONTABLES, CONTRATOS DUALES Y SUPERVISIÓN

### A. Contabilidad Inversa Exacta `BTCUSD` y Recompensa Líquida Real
$$\text{GrossPnL}_{\text{sats}}(P_0 \to P_1) = M_{\text{sats}} \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right) \cdot d, \quad d \in \{+1\ (\text{LONG}),\ -1\ (\text{SHORT})\}$$
$$\text{Fees}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(f_{\text{open}} + \frac{P_0}{P_1} \cdot f_{\text{close}}\right) + C_{\text{funding}}, \qquad \text{LossAtSL}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(\frac{|P_0 - P_{\text{SL}}|}{P_{\text{SL}}} + f_{\text{open}} + \frac{P_0}{P_{\text{SL}}} f_{\text{close}}\right)$$
$$\text{NetPnL}_{\text{sats}} = \text{GrossPnL}_{\text{sats}} - \text{Fees}_{\text{sats}}, \qquad \text{FER} = \frac{\text{NetPnL}_{\text{sats}}}{\text{Fees}_{\text{sats}}}, \qquad R_{\text{sats}} = \Delta \text{Balance} - \lambda(\text{MAE}) - \mu(\Delta t)$$

1. **Cero Doble Descuento:** Si `pl_includes_fees=True` o `real_balance_delta_sats` está presente, prohibido restar de nuevo comisiones o funding.
2. **Bellman Terminal y Desacople Flotante:** `decouple_floating_pnl == 1`; en cierre (`terminal=True`), $\gamma_{\text{eff}} = 0.0 \implies Q(s, a) \leftarrow Q(s, a) + \alpha(R - Q(s, a))$.
3. **Gradiente Negativo Obligatorio:** Toda pérdida neta real reduce $Q(s, a)$ sin bonos cosméticos; idempotencia estricta por `trade_id`.

### B. Contratos Cuantitativos Carril B (`Scalp`) y Microestructura
- **Punto dulce Carril B:** `\$500–\$1,000` (`0.85%–1.70%`), ejecución pasiva `LIMIT` (`~16–18 bps RT`), `SL` `0.45%–0.65%`, `TP` `0.95%–1.60%` (`1.9R–2.5R`), fricción `<= 16%` del `TP`.
- **Presupuesto y riesgo Carril B (`LaneBudgetController`):**
  - Reduce sizing a `0.5x` si `DD_B >= 0.80%`; pausa `24h` si `DD_B >= 1.50%` o tras `2` pérdidas seguidas en el mismo lado; bloqueo cruzado si `DD_A >= 2.00%` o `DD_global >= 2.20%`.
  - Cota de riesgo abierto al SL en B: $\sum \text{LossAtSL}_{B} \le (0.0150 - \text{DD}_B) \cdot E_{\text{global}}$.
- **Compuerta de microestructura (`micro_flow_guard.py`):** Fail-open $O(1)$ (`ALLOW` ante fallo o `age_sec > 5.0s`); requiere `V_tot_1m >= \$250k`, `trades_1m >= 30`, `L_tot_1m >= \$75k`.

### C. Matriz de Supervisión en Tiempo Real (`learning_quality_supervisor.py`)

| Signo Vital | Métrica | Umbral Normal | Alerta | Acción Autónoma |
| :--- | :--- | :--- | :--- | :--- |
| **TD-Error** | $|\delta_t|$ y $\overline{\delta}_{50}$ | $\le 0.45$ | $\ge 1.35$ (`3` updates) | **Learning Guard:** pausa updates online hasta `5` updates estables. |
| **Q-Bias** | Sesgo medio en `20` cierres | $[-25, +25]\text{ sats}$ | $> +25\text{ sats}$ persistente | Sube `min_expectancy_ratio` (ignorado si `DETERMINISTIC_TREND_POLICY=True`). |
| **FER** | Ganancia Neta / Fricción Total | $\ge 1.2$ | $< 1.2$ (`< 1.0` crítico) | Endurece compuertas y cooldown (`fer_cooldown_mult`). |
| **Entropía** | $H_{\text{norm}} \in [0, 1]$ (`100` decisiones) | $\ge 0.45$ | $< 0.25$ por `10` decisiones | Diagnóstico sin mutar Q-table (`discard_last_action_decision()` ante vetos). |
| **MFE / MAE** | Captura MFE y ratio MAE/Margen | Captura $\ge 60\%$, $\text{MAE} < 50\%$ | Captura $< 40\%$ o $\text{MAE} > 50\%$ | Ajuste defensivo sobre `dynamic_trailing_multiplier`. |

---

## 4. PROTOCOLO POR ETAPAS, HUB-AND-SPOKE Y ANTI-RETRABAJO

- **Secuencia estándar:** `Etapa 1 Diagnóstico (AST) -> Etapa 2 Plan -> Etapa 3 Código + py_compile -> Etapa 4 verify_repo.py --mode full (12 Suites + SHA) -> Etapa 5 Contexto y Commit`.
- **Por defecto (`MODO SOLO-EDICIÓN`):** El agente ejecuta Etapas 1–3 (`py_compile` incluido) y entrega diff resumido para que el usuario corra `verify_repo.py` y `git commit` en WSL sin consumir cuota.
- **Topología Hub-and-Spoke:** Si una tarea acoplada supera `2` módulos, el Hub asigna propiedad exclusiva de archivos a los 4 perfiles (`Carril A & Simulador Base`, `Carril B & Ejecución Pasiva`, `Microestructura & Telemetría`, `Verificación & QA`), garantizando cero colisiones y streaming `1` `.pkl` (`< 500 MB` RAM).
- **Memoria Anti-Retrabajo (`18M`, `S1 @ 16 bps`):** Prohibido reabrir `SL=1.15%` (`CD=24h`/`CD=8h`), `BASE 0.65% | CD=8h`, recorte `RR` (`2.7`/`2.3`) o quitar BE/Trailing en Trend, `V2_PB_1H` (`PB_BASE`/`PB_EMA50`) y Fase 5B vieja (`B1`/`B2`).
- **Comunicación:** Español técnico conciso, enlaces `file://`, LaTeX (`$...$` / `$$...$$`), `\$` escapado para fiat y veracidad estricta sobre salidas reales en WSL.
