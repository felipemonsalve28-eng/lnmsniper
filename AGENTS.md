# AGENTS.md — Gobernanza y Orquestación Quirúrgica (LNM Sniper)

Workspace: `/mnt/c/Users/mberm/OneDrive/Documentos/sniper` (WSL `Ubuntu`, Python 3.12+ / Termux `aarch64`).

## 1. Gobernanza y Ejecución en WSL
- **Lectura previa obligatoria:** Lee las Secciones 1–3 de [CONTEXT_LNM_SNIPER.md](./CONTEXT_LNM_SNIPER.md) y [AGENT_MAINTENANCE_DIRECTIVE.md](./AGENT_MAINTENANCE_DIRECTIVE.md) antes de cambios estructurales.
- **Ciclo por etapas:** Trabaja secuencialmente (`Etapa 1 Diagnóstico -> Etapa 2 Plan -> Etapa 3 Código -> Etapa 4 Pruebas -> Etapa 5 Contexto`) esperando aprobación humana entre etapas, salvo cuando el usuario entregue una directiva *One-Shot* explícita y preaprobada.
- **Terminal en WSL (`Ubuntu`):** Ejecuta todo `python3 -B` y toda operación `git` de escritura (`pull`, `commit`, `checkout`, `rm`, `reset`) dentro de **WSL (`Ubuntu`)** (`wsl -d Ubuntu -- ...`).
- **Regla anti-CRLF PowerShell->WSL:** Nunca incrustes Bash multilínea con comillas complejas en `wsl -d Ubuntu -- bash -c "..."`. Para scripts multilínea desde Windows usa siempre `@' ... '@ | wsl -d Ubuntu -- python3 -B -` o `(@' ... '@).Replace("`r","") | wsl -d Ubuntu -- bash -s`.

## 2. Reglas de Oro P0 (Inviolables sin Autorización Explícita)
1. **Q-Table y Acciones:** Preservar exactamente las `9` acciones discretas (`0`: `HOLD`, `1..4`: `LONG`, `5..8`: `SHORT`) y claves de estado de `5` segmentos `REGIME|OSC|CONC|MACRO|EMA`.
2. **Aislamiento Determinista en RAM:** Con `DETERMINISTIC_TREND_POLICY = True`, la selección, ranking y `estimate_bayesian_win_rate` usan la semilla simétrica limpia inmutable en memoria (sin sesgo direccional, `lcb_penalty = 0.0`, sin CQL/boost/shadow-CB), ignorando `Q` y `state_visits` legados en disco sin romper `strict_sha=True`.
3. **Contabilidad Real Inversa (`BTCUSD`):** Usar `inverse_contract_math=True`; cero doble descuento de comisiones/funding cuando `real_balance_delta_sats` o `pl_includes_fees=True` están presentes; Bellman terminal ($\gamma_{\text{eff}}=0$) en cierres; toda pérdida neta debe reducir $Q(s, a)$.
4. **Invariantes de Capital y Módulos Vigentes (`C1_STREAK3_24H_INV` + Modo B):**
   - `HardFloorGuardian` (`guardian_execution_core.py`) manda en jerarquía P0 (`MAX_CONCURRENT_TRADES = 5` en `mainnet` y `testnet`).
   - Arranque efectivo: `ENABLE_HYBRID_HEDGE = False`, `FE_OFF_TREND = True`, `limit_execution.enabled = 0`, `liquidity_radar` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`).
   - Escala de riesgo y protecciones: `BASE_RISK_PCT_STD = 0.0075`, `RECOVERY_ENTER_DD = 0.035`, `DD_THROTTLE_HALT_PCT = 0.045`, `MAX_DD_GUARD_PCT = 0.052`, `TREND_SL_FLOOR_PCT = 0.0065`, `SHORT_EXP_MIN_DSL = 0.006505`, freno `C1_STREAK3_24H` (`SideStreakBrakeStore`).
5. **Esquema y Retrocompatibilidad:** `0` claves huérfanas entre `config/learning_thresholds.json`, `config/learning_thresholds_testnet.json`, `learning_thresholds_manager.py` y `sniper_cli_catalog.py` (`100` umbrales en `12` categorías, `28` parámetros runtime); `MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` deben retornar siempre la 3-tupla `(h, l, c)`.
6. **Persistencia Atómica, Concurrencia y Termux:** Usar `tmp + os.fsync + os.replace` + `FileLockContext` (`fcntl`) + `strict_sha=True` en los `4` JSON de estado (`q_brain_advanced_lnm.json`, `q_brain_testnet_lnm.json`, `hedge_state_mainnet.json`, `hedge_state_testnet.json`) y `$HOME/.sniper_state/` para el freno por lado. Cero I/O bloqueante en el bucle de `5s` (`is_side_paused` en RAM $O(1)$), `TokenBucket(20/s, burst 40)` + `clientId` único en `open_trade()`, solo stdlib + `requests` + `numpy`.
7. **Seguridad y Cachés:** Prohibido tocar credenciales reales `.env` o conectar a APIs en vivo en auditorías/tests; prohibido alterar `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl` cada uno).

## 3. Precisión Quirúrgica, Sintaxis y Disciplina Git
- **Lectura mínima por AST:** Usa el Mapa AST de [CONTEXT_LNM_SNIPER.md](./CONTEXT_LNM_SNIPER.md) o `grep`/`rg -n` antes de leer un archivo; lee bloques acotados (`<= 150` líneas). Nunca leas completo `lnm_sniper_v6_advanced.py` (`4806L`).
- **Estabilidad sintáctica:** Tras cada edición corre `python3 -B -m py_compile` sobre los archivos tocados; usa `-B` y purga `__pycache__` antes de commits.
- **Concurrencia de pruebas:** Prohibido correr en paralelo suites que toquen disco o estado. Ejecuta `python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode full` secuencialmente y verifica que `git status --short` no marque mutaciones en JSONs trackeados.
- **Commits atómicos:** Stagea rutas explícitas (`git add -- <rutas>`); **NUNCA** uses `git add -A`.

## 4. Skills Especializados y Subagentes
Carga el skill puntual bajo demanda para mantener el contexto ligero:
- **`wsl-verify`** (`.agents/skills/wsl-verify/SKILL.md`): Verificación rápida/completa en WSL, `11` suites secuenciales, `strict_sha=True` y OOS Fold 1.
- **`quant-math-audit`** (`.agents/skills/quant-math-audit/SKILL.md`): Auditoría de matemática inversa, expectancy, Kelly/sizing, paridad sim vs prod y esquema de `100` umbrales.
- **`surgical-python-edit`** (`.agents/skills/surgical-python-edit/SKILL.md`): Localización AST, edición por rangos mínimos, atomicidad Termux, `py_compile` y commits atómicos.
- **`sniper-subagents`** (`.agents/skills/sniper-subagents/SKILL.md`): Orquestación Coordinador + `research` + `quant-auditor` + `implementer` (`Workspace: "share"` si editan en paralelo) + `wsl-verifier`.
