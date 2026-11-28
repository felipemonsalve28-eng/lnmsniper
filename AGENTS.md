# AGENTS.md — Gobernanza y Orquestación Quirúrgica (LNM Sniper v7.4+ Máquina Dual)

Workspace: `/mnt/c/Users/mberm/OneDrive/Documentos/sniper` (WSL `Ubuntu`, Python 3.12+ / Termux `aarch64`).

## 1. Dirección Estratégica, Presupuesto de Cuota y Ejecución en WSL
- **Arquitectura Objetivo (`v7.4+` Máquina Dual sobre LN Markets API v3 BTCUSD inverso):**
  - **Carril A (`Trend`):** capturar expansiones direccionales largas (`\$1,600–\$2,500+`, `RR=4.2`) sobre la línea base certificada (`BASE 0.65% | CD=24h | RR=4.2`, Fold 1 paridad exacta `N=130`, `+26,933`), recortando sangría en meses laterales con `FORCED_EDGE_EXIT` asimétrico solo en pérdidas (`forced_edge_loss_only`).
  - **Carril B (`Scalp`):** capturar piernas intradía limpias (`\$500–\$1,000`, `0.85%–1.70%`) en rangos y barridos de liquidez usando [`liquidity_radar.py`](./liquidity_radar.py), [`limit_execution_manager.py`](./limit_execution_manager.py) y `DEF_SCALP_LOCK`, aislados bajo [`simulation/lane_budget.py`](./simulation/lane_budget.py) y filtrados en vivo por [`micro_flow_guard.py`](./micro_flow_guard.py) + cinta real `1m` de [`binance_micro_sidecar.py`](./binance_micro_sidecar.py).
- **Lectura previa mínima:** Usa las Secciones 1–3 de [CONTEXT_LNM_SNIPER.md](./CONTEXT_LNM_SNIPER.md) y [ROADMAP_LNM_SNIPER.md](./ROADMAP_LNM_SNIPER.md). Lee [AGENT_MAINTENANCE_DIRECTIVE.md](./AGENT_MAINTENANCE_DIRECTIVE.md) **solo** ante cambios arquitectónicos mayores.
- **Presupuesto estricto de lectura:** Localiza primero con el Mapa AST o `grep`/`rg -n`. Lee **únicamente** los archivos autorizados en el prompt en bloques `<= 150` líneas (máximo `6` lecturas antes de planear/editar). Prohibido abrir archivos fuera de alcance o cargar completo `lnm_sniper_v6_advanced.py` (`5199L`).
- **`MODO SOLO-EDICIÓN` por defecto (ahorro de cuota):** Ejecuta únicamente localización mínima, edición quirúrgica y `python3 -B -m py_compile` sobre los archivos `.py` tocados; **no** ejecutes `verify_repo.py --mode full`, `oos-fold1`, ni `git commit`/`push` salvo instrucción explícita del usuario (el usuario los corre directo en su terminal WSL).
- **Streaming obligatorio en WSL (`1` `.pkl` a la vez):** Todo runner/backtest `18M` (`58` bloques) DEBE procesar un `.pkl` por iteración liberando memoria (`del blk; gc.collect()`), manteniéndose `< 500 MB` de RAM y `0` Swap. Prohibido precargar los `58` bloques simultáneamente.
- **Ciclo por etapas:** Trabaja secuencialmente (`Diagnóstico -> Plan -> Código -> Pruebas -> Contexto`) salvo instrucción *One-Shot* explícita.
- **Terminal WSL (`Ubuntu`) y anti-CRLF:** Todo `python3 -B` y `git` de escritura corre en WSL (`wsl -d Ubuntu -- ...`). Nunca incrustes Bash multilínea con comillas complejas desde PowerShell; usa `@' ... '@ | wsl -d Ubuntu -- python3 -B -` o `(@' ... '@).Replace("`r","") | wsl -d Ubuntu -- bash -s`.

## 2. Reglas de Oro P0 (Inviolables sin Autorización Explícita)
1. **Q-Table y Acciones:** Preservar `9` acciones discretas (`0`: `HOLD`, `1..4`: `LONG`, `5..8`: `SHORT`) y claves de `5` segmentos `REGIME|OSC|CONC|MACRO|EMA`.
2. **Aislamiento Determinista en RAM:** Con `DETERMINISTIC_TREND_POLICY = True`, la selección, ranking y `estimate_bayesian_win_rate` operan sobre la semilla simétrica limpia inmutable en RAM (`lcb_penalty = 0.0`, sin CQL/boost/shadow-CB), ignorando `Q` y `state_visits` legados sin romper `strict_sha=True`.
3. **Contabilidad Real Inversa (`BTCUSD`):** `inverse_contract_math=True` con $\Delta_{\text{sats}} = M_{\text{sats}} \cdot L \cdot (1 - P_0 / P_1) \cdot d$; parseo camelCase API v3 (`openingFee`, `closingFee`, `sumFundingFees`), cero doble descuento cuando `real_balance_delta_sats` o `pl_includes_fees=True` existen; Bellman terminal ($\gamma_{\text{eff}}=0$); toda pérdida neta reduce $Q(s, a)$.
4. **Capital, Módulos Vigentes y Contención Dual (`C1_STREAK3_24H_INV` + Modo B):** `HardFloorGuardian` manda en P0 (`MAX_CONCURRENT_TRADES = 5` en `mainnet` y `testnet`); `ENABLE_HYBRID_HEDGE = False`, `FE_OFF_TREND = True`, `limit_execution.enabled = 0`, `liquidity_radar` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`); `BASE_RISK_PCT_STD = 0.0075`, `RECOVERY_ENTER_DD = 0.035`, `DD_THROTTLE_HALT_PCT = 0.045`, `MAX_DD_GUARD_PCT = 0.052`, `TREND_SL_FLOOR_PCT = 0.0065`, `SHORT_EXP_MIN_DSL = 0.006505`, freno `C1_STREAK3_24H` (`SideStreakBrakeStore`). Toda evolución de Carril B offline/promoción opera estrictamente bajo el aislamiento `LaneBudgetController` en `simulation/lane_budget.py` (ver [simulation/AGENTS.md](./simulation/AGENTS.md)) y `micro_flow_guard.py` fail-open $O(1)$ sin alterar producción hasta Fase 5.
5. **Esquema y Retrocompatibilidad:** `0` claves huérfanas (`100` umbrales en `12` categorías, `28` runtime); `MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` retornan siempre 3-tupla `(h, l, c)`.
6. **Atomicidad, IPC y Termux:** `tmp + os.fsync + os.replace` + `FileLockContext` (`fcntl`) + `strict_sha=True` en los `4` JSON de estado; estado móvil bajo `$HOME/.sniper_state/` (jamás `/sdcard`); cero I/O bloqueante o HTTP extra en el bucle de `5s`; `TokenBucket(20/s, burst 40)` + `clientId` único; sidecar Binance `shadow_only` + cinta `1m` (`P95 < 0.300 ms`, `logs/binance_micro_events*.jsonl` solo en `OPEN`/`CLOSE`, `micro_flow_guard.py` fail-open `age_sec > 5.0s`); solo stdlib + `requests` + `numpy`.
7. **Seguridad y Cachés:** Prohibido tocar `.env`, hacer scraping web o conectar a APIs en vivo en tests; prohibido alterar `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl` cada uno).
8. **Memoria Anti-Retrabajo (Cementerio de Ideas Descartadas — Prohibido Reabrir sin Evidencia OOS Nueva):**
   - **Descartadas en `18M` (`58` bloques, `S1 @ 16 bps`):**
     - `SL=1.15%` (tanto `CD=24h` como `CD=8h`).
     - `BASE 0.65% | CD=8h`.
     - Recorte de `RR` (`2.7`/`2.3`) o quitar Breakeven / Trailing en Trend.
     - `V2_PB_1H` (`PB_BASE` / `PB_EMA50`).
   - **Descartadas previas:** Fase 5B vieja (`B1`/`B2` sin flujo real), Fase 2 (`LIQ_RAID_1M`), Fase 3 (`RUNNER_4H`), `V_EXIT`/`V_ENTRY`, scale-out `50%`, `StochRSI` sobre cierres y reemplazo de `q_table`.

## 3. Topología Hub-and-Spoke (Propiedad Exclusiva de Archivos) y Verificación
- **Hub (Coordinator):** Arbitra contratos, actualiza gobernanza (`AGENTS.md`, `ROADMAP_LNM_SNIPER.md`, `CONTEXT_LNM_SNIPER.md`) y asigna frentes disjuntos sin colisiones entre subagentes:
  1. **Carril A & Simulador Base:** `simulation/sim_training_environment.py`, `simulation/run_phase5b_v2_lab.py`
  2. **Carril B & Ejecución Pasiva:** `simulation/scalp_sim_engine.py`, `liquidity_radar.py`, `limit_execution_manager.py`, `simulation/lane_budget.py`
  3. **Microestructura & Telemetría:** `binance_micro_sidecar.py`, `micro_flow_guard.py`, `tools/replay_micro_tape.py`
  4. **Verificación & QA:** `test_*.py`, `.agents/skills/wsl-verify/`
- Compila con `python3 -B -m py_compile` tras cada edición; purga `__pycache__` antes de commits.
- Nunca corras en paralelo suites que toquen disco/estado; usa `verify_repo.py --mode full` (`12` suites secuenciales) y `oos-fold1` cuando toque `simulation/` o lógica de decisión.
- Commits atómicos solo con rutas explícitas (`git add -- <rutas>`); **NUNCA** `git add -A`.

## 4. Skills Especializados (Carga Bajo Demanda)
- **`wsl-verify`** (`.agents/skills/wsl-verify/SKILL.md`): `verify_repo.py` (`quick`, `full` `12` suites, `oos-fold1`), streaming `1` `.pkl` y reglas `MODO SOLO-EDICIÓN`.
- **`quant-math-audit`** (`.agents/skills/quant-math-audit/SKILL.md`): Matemática inversa BTCUSD (`GrossPnL_sats`, `Fees_sats`, `LossAtSL_sats`, `FER`), máquina dual (`LaneBudgetController`, punto dulce Scalp, `micro_flow_guard`), estrés Fase 5, Kelly `0.25x`, esquema `100/12`.
- **`surgical-python-edit`** (`.agents/skills/surgical-python-edit/SKILL.md`): Localización AST, presupuesto `<= 150L`, propiedad exclusiva de archivos, atomicidad Termux y commits atómicos.
- **`sniper-subagents`** (`.agents/skills/sniper-subagents/SKILL.md`): Topología Hub-and-Spoke de 4 perfiles únicamente cuando la tarea supera `2` archivos acoplados.
