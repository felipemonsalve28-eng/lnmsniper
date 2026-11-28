# simulation/AGENTS.md — Reglas Estrictas del Simulador, Máquina Dual y OOS

Este directorio opera bajo [AGENTS.md](../AGENTS.md) más los invariantes de paridad de simulación, disciplina de memoria WSL, memoria anti-retrabajo y contratos matemáticos de la Máquina Dual (Carril A Trend + Carril B Scalp).

## 1. Invariantes Obligatorios en `simulation/`
1. **Paridad 1:1 con Producción (`C1_STREAK3_24H_INV` + Modo B):**
   - `SimTrainingEnvironment` (`sim_training_environment.py:L143-1562`) y `SimLNMExchange` (`sim_lnm_exchange.py:L75-600`) mantienen `inverse_contract_math=True` por defecto, incluso tras `reset()`.
   - Defaults idénticos a producción: `deterministic_trend_policy=True`, `fe_off_trend=True`, `short_exp_phys_hold=True`, `streak3_24h=True`, `forced_edge_loss_only=False` (opt-in para `exit_asym`), y `LiquidityConfig` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`).
   - En evaluaciones deterministic/Monte Carlo (`learning_enabled=False`): **0 mutaciones** (`mutations_detected == 0`) y cero escritura a `logs/binance_micro_events*.jsonl`.
2. **Aislamiento de Estrés Fase 5 (`S0` / `S1` / `S2`):**
   - Todo modelo de latencia/slippage de Fase 5 vive **exclusivamente** en `simulation/` detrás de parámetros opt-in con default neutro (`S0`: $\delta_{\text{base}}=0$, $k_{\text{vol}}=0$, $\Delta t_{\text{exec}}=0$) que garantice paridad bit a bit con Fold 1 OOS.
   - Nunca introducir hooks de estrés en `lnm_sniper_v6_advanced.py` ni alterar las tarifas reales por defecto sin flag explícito.
3. **Protección de Cerebros y Cachés WSL:**
   - **Prohibido** mutar `q_brain_advanced_lnm.json` o `q_brain_testnet_lnm.json` en simulaciones/tests; usar `tempfile.TemporaryDirectory()` o `*_simulated_candidate.json`.
   - **Intocables:** `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl` cada uno).
4. **Referencia Certificada Fold 1 OOS (`M13..M18`, escenario `S0`):**
   - Salida exacta: `N=130`, `Net=+26,933 sats`, `PF=1.729` (`1.73`), `MaxDD=4.10%`, `DD>=4.5%=0/6`, `DDG=0`, `OTR=0`, `FE=0`, `M16=+31,746 sats (52)`.
   - Validación: `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1`.

## 2. Disciplina de Memoria WSL (Streaming `1` `.pkl` a la Vez)
- Todo runner o motor `18M` (`run_phase5b_v2_lab.py`, `scalp_sim_engine.py`, `run_phase5_stress.py`) DEBE cargar y procesar **un solo `.pkl` por iteración** (`del blk; gc.collect()` al cerrar cada bloque).
- Presupuesto estricto en WSL: **RAM pico `< 500 MB`** y **`0` Swap**. Prohibido precargar los `58` bloques en listas globales ni ejecutar corridas `18M` concurrentes.

## 3. Memoria Anti-Retrabajo (`18M`, `58` Bloques, `S1 @ 16 bps`)
Prohibido reabrir, re-simular o proponer sin nueva evidencia OOS estas hipótesis ya descartadas:
- `SL=1.15%` (tanto `CD=24h` como `CD=8h`).
- `BASE 0.65% | CD=8h`.
- Recorte de `RR` (`2.7` / `2.3`) o eliminar Breakeven / Trailing en Trend (`Carril A`).
- `V2_PB_1H` (`PB_BASE` / `PB_EMA50`).
- Fase 5B vieja (`B1` / `B2` sin flujo real ni aislamiento de presupuesto).

## 4. Contratos Matemáticos de la Máquina Dual (`Carril A + Carril B`)
1. **Contrato Inverso Exacto (`BTCUSD`):**
   - $\text{GrossPnL}_{\text{sats}}(P_0 \to P_1) = M_{\text{sats}} \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right) \cdot d$, con $d \in \{+1\ (\text{LONG}), -1\ (\text{SHORT})\}$.
   - $\text{Fees}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(f_{\text{open}} + \frac{P_0}{P_1} f_{\text{close}}\right) + C_{\text{funding}}$.
   - $\text{LossAtSL}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(\frac{|P_0 - P_{\text{SL}}|}{P_{\text{SL}}} + f_{\text{open}} + \frac{P_0}{P_{\text{SL}}} f_{\text{close}}\right)$.
   - $\text{FER} = \frac{\text{NetPnL}_{\text{sats}}}{\text{Fees}_{\text{sats}}} = \frac{\text{GrossPnL}_{\text{sats}} - \text{Fees}_{\text{sats}}}{\text{Fees}_{\text{sats}}}$.
2. **Carril A (`Trend`):**
   - Línea base certificada: `BASE 0.65% | CD=24h | RR=4.2` (expansiones `\$1,600–\$2,500+`).
   - Recorte de sangría lateral vía `FORCED_EDGE_EXIT` asimétrico solo en pérdidas (`forced_edge_loss_only`, modo `exit_asym`), siempre detrás de flag opt-in que mantenga `S0` bit a bit idéntico cuando está desactivado.
3. **Punto Dulce Carril B (`Scalp`):**
   - Objetivo de pierna intradía: `\$500–\$1,000` (`0.85%–1.70%`).
   - Ejecución pasiva `LIMIT` (`limit_execution_manager.py`, fricción `~16–18 bps RT`), `SL` `0.45%–0.65%`, `TP` `0.95%–1.60%` (`1.9R–2.5R`), fricción total `<= 16%` del `TP`.
   - Módulos de señal y estructura: `liquidity_radar.py`, `limit_execution_manager.py` y `DEF_SCALP_LOCK`.
4. **Aislamiento `LaneBudgetController` (`simulation/lane_budget.py`) bajo `MAX_CONCURRENT_TRADES = 5`:**
   - **Carril A:** `3` slots reservados (hasta `5` totales cuando B no ocupa).
   - **Carril B:** máximo `2` slots (`1`/lado), solo si `N_A + N_B <= 3` y margen total de B `<= 15%`.
   - **Cero contagio de DD:** `lane_a_recovery_mode` (`3.50%`) lee exclusivamente `DD_A`.
   - **Escalonamiento y frenos de B:** reduce sizing a `0.5x` si `DD_B >= 0.80%`; pausa `24h` si `DD_B >= 1.50%` o tras `2` pérdidas seguidas en el mismo lado; freno cruzado bloquea entradas de B si `DD_A >= 2.00%` o `DD_global >= 2.20%`.
   - **Cota de riesgo abierto al SL en B:** $\sum \text{LossAtSL}_{B} \le (0.0150 - \text{DD}_B) \cdot E_{\text{global}}$.
5. **Microestructura (`micro_flow_guard.py`):**
   - Fail-open $O(1)$: retorna `ALLOW` ante fallo de lectura, snapshot ausente/corrupto o `age_sec > 5.0s`.
   - Umbrales mínimos de habilitación: `V_tot_1m >= \$250k`, `trades_1m >= 30`, `L_tot_1m >= \$75k`.
