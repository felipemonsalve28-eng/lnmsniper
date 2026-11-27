# AGENTS.md — Gobernanza y Orquestación Quirúrgica (LNM Sniper)

Workspace: `/mnt/c/Users/mberm/OneDrive/Documentos/sniper` (WSL `Ubuntu`, Python 3.12+ / Termux `aarch64`).

## 1. Gobernanza, Presupuesto de Cuota y Ejecución en WSL
- **Lectura previa mínima:** Usa las Secciones 1–3 de [CONTEXT_LNM_SNIPER.md](./CONTEXT_LNM_SNIPER.md) y [ROADMAP_LNM_SNIPER.md](./ROADMAP_LNM_SNIPER.md). Lee [AGENT_MAINTENANCE_DIRECTIVE.md](./AGENT_MAINTENANCE_DIRECTIVE.md) **solo** ante cambios arquitectónicos mayores.
- **Presupuesto estricto de lectura:** Localiza primero con el Mapa AST o `grep`/`rg -n`. Lee **únicamente** los archivos autorizados en el prompt en bloques `<= 150` líneas (máximo `6` lecturas antes de planear/editar). Prohibido abrir archivos fuera de alcance o cargar completo `lnm_sniper_v6_advanced.py` (`5199L`).
- **MODO SOLO-EDICIÓN (ahorro de cuota):** Cuando el usuario indique `MODO SOLO-EDICIÓN`, ejecuta únicamente localización mínima, edición quirúrgica y `python3 -B -m py_compile` sobre los archivos tocados; **no** ejecutes `verify_repo.py --mode full`, `oos-fold1`, ni `git commit`/`push` (el usuario los corre directo en su terminal WSL).
- **Ciclo por etapas:** Trabaja secuencialmente (`Diagnóstico -> Plan -> Código -> Pruebas -> Contexto`) salvo instrucción *One-Shot* explícita.
- **Terminal WSL (`Ubuntu`) y anti-CRLF:** Todo `python3 -B` y `git` de escritura corre en WSL (`wsl -d Ubuntu -- ...`). Nunca incrustes Bash multilínea con comillas complejas desde PowerShell; usa `@' ... '@ | wsl -d Ubuntu -- python3 -B -` o `(@' ... '@).Replace("`r","") | wsl -d Ubuntu -- bash -s`.

## 2. Reglas de Oro P0 (Inviolables sin Autorización Explícita)
1. **Q-Table y Acciones:** Preservar `9` acciones discretas (`0`: `HOLD`, `1..4`: `LONG`, `5..8`: `SHORT`) y claves de `5` segmentos `REGIME|OSC|CONC|MACRO|EMA`.
2. **Aislamiento Determinista en RAM:** Con `DETERMINISTIC_TREND_POLICY = True`, la selección, ranking y `estimate_bayesian_win_rate` operan sobre la semilla simétrica limpia inmutable en RAM (`lcb_penalty = 0.0`, sin CQL/boost/shadow-CB), ignorando `Q` y `state_visits` legados sin romper `strict_sha=True`.
3. **Contabilidad Real Inversa (`BTCUSD`):** `inverse_contract_math=True` con $\Delta_{\text{sats}} = M_{\text{sats}} \cdot L \cdot (1 - P_0 / P_1) \cdot d$; parseo camelCase API v3 (`openingFee`, `closingFee`, `sumFundingFees`), cero doble descuento cuando `real_balance_delta_sats` o `pl_includes_fees=True` existen; Bellman terminal ($\gamma_{\text{eff}}=0$); toda pérdida neta reduce $Q(s, a)$.
4. **Capital y Módulos Vigentes (`C1_STREAK3_24H_INV` + Modo B):** `HardFloorGuardian` manda en P0 (`MAX_CONCURRENT_TRADES = 5` en `mainnet` y `testnet`); `ENABLE_HYBRID_HEDGE = False`, `FE_OFF_TREND = True`, `limit_execution.enabled = 0`, `liquidity_radar` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`); `BASE_RISK_PCT_STD = 0.0075`, `RECOVERY_ENTER_DD = 0.035`, `DD_THROTTLE_HALT_PCT = 0.045`, `MAX_DD_GUARD_PCT = 0.052`, `TREND_SL_FLOOR_PCT = 0.0065`, `SHORT_EXP_MIN_DSL = 0.006505`, freno `C1_STREAK3_24H` (`SideStreakBrakeStore`).
5. **Esquema y Retrocompatibilidad:** `0` claves huérfanas (`100` umbrales en `12` categorías, `28` runtime); `MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` retornan siempre 3-tupla `(h, l, c)`.
6. **Atomicidad, IPC y Termux:** `tmp + os.fsync + os.replace` + `FileLockContext` (`fcntl`) + `strict_sha=True` en los `4` JSON de estado; estado móvil bajo `$HOME/.sniper_state/` (jamás `/sdcard`); cero I/O bloqueante o HTTP extra en el bucle de `5s`; `TokenBucket(20/s, burst 40)` + `clientId` único; sidecar Binance `shadow_only` (`P95 < 0.300 ms`, `logs/binance_micro_events*.jsonl` solo en `OPEN`/`CLOSE`); solo stdlib + `requests` + `numpy`.
7. **Seguridad y Cachés:** Prohibido tocar `.env`, hacer scraping web o conectar a APIs en vivo en tests; prohibido alterar `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl` cada uno).
8. **Cementerio de Ideas Descartadas (No Proponer sin Evidencia OOS Nueva):** Fase 2 (`LIQ_RAID_1M`), Fase 3 (`RUNNER_4H`), `V_EXIT`/`V_ENTRY`, scale-out `50%`, `StochRSI` sobre cierres y reemplazo de `q_table`.

## 3. Verificación y Disciplina Git
- Compila con `python3 -B -m py_compile` tras cada edición; purga `__pycache__` antes de commits.
- Nunca corras en paralelo suites que toquen disco/estado; usa `verify_repo.py --mode full` (`12` suites secuenciales) y `oos-fold1` cuando toque `simulation/` o lógica de decisión.
- Commits atómicos solo con rutas explícitas (`git add -- <rutas>`); **NUNCA** `git add -A`.

## 4. Skills Especializados (Carga Bajo Demanda)
- **`wsl-verify`** (`.agents/skills/wsl-verify/SKILL.md`): `verify_repo.py` (`quick`, `full` `12` suites, `oos-fold1`) y reglas `MODO SOLO-EDICIÓN`.
- **`quant-math-audit`** (`.agents/skills/quant-math-audit/SKILL.md`): Matemática inversa exacta, estrés Fase 5, Kelly `0.25x` Fase 6, compuertas Fase 7 y esquema `100/12`.
- **`surgical-python-edit`** (`.agents/skills/surgical-python-edit/SKILL.md`): Localización AST, presupuesto `<= 150L`, atomicidad Termux y commits atómicos.
- **`sniper-subagents`** (`.agents/skills/sniper-subagents/SKILL.md`): Orquestación multi-módulo únicamente cuando la tarea supera `2` archivos acoplados.
