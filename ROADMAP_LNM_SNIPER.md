# Hoja de Ruta Técnica — LNM Sniper v7.4+ (Máquina Dual: Trend + Scalp)
> **Acompañamiento a [`CONTEXT_LNM_SNIPER.md`](./CONTEXT_LNM_SNIPER.md)**  
> **Última Actualización:** `2026-11-27` | **HEAD Base:** `eeadbfd` (sobre `d23bc07`; **Fase 1A `exit_asym` lista en working tree para certificar en WSL**; **Siguiente: Fase 1B `simulation/scalp_sim_engine.py`**)  
> **Regla Antigravity:** Cero scraping web. Lectura estrictamente local y acotada (`<= 150` líneas por bloque, solo archivos autorizados). En `MODO SOLO-EDICIÓN` (por defecto), delegar `verify_repo.py` y `git` a la terminal WSL del usuario.

---

## 1. Línea Base Certificada, Estado Actual e Invariantes P0

| Dimensión | Valor Comprobado | Fuente |
| :--- | :--- | :--- |
| **Cadena de commits** | `5196b85` (camelCase fees) -> `89036e0` (Fase 4B.1 sidecar JSONL) -> `143faea` (Fase 5 estrés `GO`) -> `d23bc07` (cinta `1m` + `micro_flow_guard.py` + `lane_budget.py` + `run_phase5b_v2_lab.py` + `replay_micro_tape.py`) -> `eeadbfd` (`README.md`) | `origin/master` |
| **Working tree actual** | **Fase 0:** alineación de gobernanza y skills Dual. **Fase 1A (`exit_asym` / `forced_edge_loss_only`):** implementada en `simulation/sim_training_environment.py`, `simulation/run_phase5b_v2_lab.py` y `test_simulation_engine.py`, **lista para certificar en WSL** | Working tree local |
| **Carril A (`Trend`) Base** | `BASE 0.65% \| CD=24h \| RR=4.2` (`\$1,600–\$2,500+`). Paridad OOS Fold 1 (`S0`): `N=130` \| `Net=+26,933` \| `PF=1.729` \| `MaxDD=4.10%` \| `DD>=4.5%=0/6` \| `M16=+31,746 (52)` | `verify_repo.py --mode oos-fold1` |
| **Backtest 18M & Estrés (`143faea`)** | `5196b85 @ 16 bps`: `Net=+7,301`, `OOS 12M=+23,992`, `MaxDD=4.62%`. Estrés `S1` (`GO`): `+14,279` (`20 bps`, `MaxDD=4.34%`) / `+16,477` (`16 bps`, `MaxDD=4.62%`); `HFViol=0` en `S1`/`S2` | `simulation/run_phase5_stress.py` |
| **Infraestructura Micro (`d23bc07`)** | Sidecar Binance (`P95=0.0056 ms` en Termux `89036e0`), cinta `1m` rotada, `micro_flow_guard.py` fail-open $O(1)$, `simulation/lane_budget.py`, `tools/replay_micro_tape.py` | `test_binance_micro_sidecar.py`, `test_simulation_engine.py` |

### Invariantes P0 Innegociables
- **Riesgo y ejecución en vivo (hasta Fase 5):** `MAX_CONCURRENT_TRADES = 5`, Modo B `LIQ_BIAS_ONLY` (`liq_en=True`, `liq_shadow=False`, `opp_wall=False`), `100% MARKET` (`lim_en=False`), `HardFloorGuardian`, `C1_STREAK3_24H` (`86,400s` tras 3 pérdidas/lado), `risk = 0.0075`, `sl_floor = 0.0065`, `hedge = False`.
- **Sistema, memoria WSL e IPC:** Cero I/O bloqueante o HTTP extra en el bucle de `5s`; `TokenBucket(20/s, burst 40)`; `fcntl` bajo `$HOME` (jamás `/sdcard`); `strict_sha=True` en los `4` JSON; esquema `100/12 + 28 runtime`; streaming obligatorio **`1` `.pkl` a la vez** (`del blk; gc.collect()`, `< 500 MB` RAM, `0` Swap) en WSL.

### Cementerio de Ideas Descartadas (Memoria Anti-Retrabajo — Prohibido Reabrir sin Evidencia OOS Nueva)
- **Descartadas en `18M` (`58` bloques, `S1 @ 16 bps`):**
  1. `SL=1.15%` (tanto `CD=24h` como `CD=8h`).
  2. `BASE 0.65% | CD=8h`.
  3. Recorte de `RR` (`2.7` / `2.3`) o quitar Breakeven / Trailing en `Trend`.
  4. `V2_PB_1H` (`PB_BASE` / `PB_EMA50`).
- **Descartadas previas:**
  - Fase 5B vieja (`Dual-Track` Carril B `SNIPER` sin flujo real ni aislamiento de presupuesto): `B2` negativo `6/6` meses en Fold 1 (`N=95`, `WR=26.3%`, `Net=-11,209`, `PF=0.488`, `MaxDD=4.98%`); `B1` `4–5` entradas/mes (`0%–20%` WR).
  - Fase 2 (`LIQ_RAID_1M`), Fase 3 (`RUNNER_4H`), `V_EXIT` y `V_ENTRY`: empeoraron `5196b85` entre `-12,140` y `-33,107 sats` en `18M`.
  - Scale-out parcial (`50%`), `StochRSI` sobre cierres y reemplazo de `q_table`.

---

## 2. Contratos Matemáticos de la Máquina Dual (`v7.4+`)

### A. Contrato Inverso Exacto (`BTCUSD`)
$$\text{GrossPnL}_{\text{sats}}(P_0 \to P_1) = M_{\text{sats}} \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right) \cdot d, \qquad d \in \{+1\ (\text{LONG}),\ -1\ (\text{SHORT})\}$$
$$\text{Fees}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(f_{\text{open}} + \frac{P_0}{P_1} \cdot f_{\text{close}}\right) + C_{\text{funding}}, \qquad \text{LossAtSL}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(\frac{|P_0 - P_{\text{SL}}|}{P_{\text{SL}}} + f_{\text{open}} + \frac{P_0}{P_{\text{SL}}} \cdot f_{\text{close}}\right)$$
$$\text{NetPnL}_{\text{sats}} = \text{GrossPnL}_{\text{sats}} - \text{Fees}_{\text{sats}}, \qquad \text{FER} = \frac{\text{NetPnL}_{\text{sats}}}{\text{Fees}_{\text{sats}}}$$

### B. Punto Dulce Carril B (`Scalp`) y Selección Adversa Pasiva
- **Rango objetivo:** Piernas intradía de `\$500–\$1,000` (`0.85%–1.70%`) vía [`liquidity_radar.py`](./liquidity_radar.py), [`limit_execution_manager.py`](./limit_execution_manager.py) y `DEF_SCALP_LOCK`.
- **Geometría y fricción:** Ejecución `LIMIT` pasiva (`~16–18 bps RT`), `SL` `0.45%–0.65%`, `TP` `0.95%–1.60%` (`1.9R–2.5R`), fricción total `<= 16%` del `TP`.
- **Cota de selección adversa LIMIT:** $\Delta_{\text{imp}} - \Delta_{\text{adv}} > E_{\text{mkt}} \cdot \frac{1 - p_{\text{fill}}}{p_{\text{fill}}}$.

### C. Aislamiento `LaneBudgetController` (`simulation/lane_budget.py`, `MAX_CONCURRENT_TRADES = 5`)
- **Cupo de slots:** Carril A tiene `3` slots reservados (hasta `5` totales); Carril B máximo `2` slots (`1`/lado), solo si `N_A + N_B <= 3` y margen total de B `<= 15%`.
- **Aislamiento de DD:** `lane_a_recovery_mode` (`3.50%`) solo lee `DD_A` (cero contagio desde B).
- **Frenos propios y cruzados de B:** B reduce sizing a `0.5x` si `DD_B >= 0.80%`; pausa `24h` si `DD_B >= 1.50%` o tras `2` pérdidas seguidas en el mismo lado; freno cruzado sobre B si `DD_A >= 2.00%` o `DD_global >= 2.20%`.
- **Cota de riesgo abierto al SL en B:** $\sum \text{LossAtSL}_{B} \le (0.0150 - \text{DD}_B) \cdot E_{\text{global}}$.

### D. Microestructura en Vivo (`micro_flow_guard.py`)
- **Fail-open $O(1)$:** Retorna `ALLOW` ante fallo de lectura, snapshot ausente o `age_sec > 5.0s`.
- **Umbrales mínimos de actividad:** `V_tot_1m >= \$250k`, `trades_1m >= 30`, `L_tot_1m >= \$75k`.

---

## 3. Plan Maestro en 5 Fases (Máquina Dual Trend + Scalp)

### Fase 0 — Alineación de Workspace y Agentes Dual (`COMPLETADA`)
- Gobernanza (`AGENTS.md`, `simulation/AGENTS.md`, `AGENT_MAINTENANCE_DIRECTIVE.md`), skills (`.agents/skills/*`), memoria anti-retrabajo `18M`, topología Hub-and-Spoke de 4 perfiles y hoja de ruta unificada sin tocar `.py` ni `.json`.

### Fase 1 — `FORCED_EDGE_EXIT` Asimétrico (`1A`) y Motor Scalp Causal (`1B`) (`EN CURSO`)
- **Fase 1A (`exit_asym` / `forced_edge_loss_only`) — Implementada en working tree, lista para certificar en WSL:**
  - Recorte asimétrico de sangría lateral en Carril A solo sobre posiciones en pérdida detrás de flag opt-in (`forced_edge_loss_only=False` por defecto para paridad `S0` exacta).
  - Validación pendiente en WSL: `run_phase5b_v2_lab.py --mode exit_asym` + `verify_repo.py --mode full` + `oos-fold1`.
- **Fase 1B (`simulation/scalp_sim_engine.py` + `--mode scalping`) — SIGUIENTE HITO:**
  - Implementar motor causal específico de Carril B integrando `liquidity_radar.py`, `limit_execution_manager.py`, `DEF_SCALP_LOCK` y `simulation/lane_budget.py`, ejecutado en streaming `1` `.pkl` a la vez (`< 500 MB` RAM).

### Fase 2 — Validación Standalone Carril B en `18M` (`58` bloques, `S1 @ 16–18 bps`)
- Evaluar Carril B aislado (`simulation/scalp_sim_engine.py`) sobre los `58` bloques en streaming `1` `.pkl`, con modelo conservador de fills `LIMIT` pasivos y selección adversa sin filtración futura.

### Fase 3 — Validación Combinada `Carril A + Carril B` en `18M` (`58` bloques, `LaneBudgetController` activo)
- Correr Máquina Dual completa (`Carril A` + `Carril B`) verificando preservación de `M16` y expansión Trend, `0` contagio de DD hacia `lane_a_recovery_mode`, mejora de PnL/Calmar `18M` y `12M OOS`, y `0` violaciones `HardFloorGuardian`.

### Fase 4 — Calibración con Cinta Real `1m` (`5–7` días Termux + `tools/replay_micro_tape.py`)
- Recolectar `5–7` días continuos de cinta `1m` en Termux (`binance_micro_sidecar.py`) + eventos `OPEN`/`CLOSE`.
- Calibrar distribuciones empíricas (`V_tot_1m >= \$250k`, `trades_1m >= 30`, `L_tot_1m >= \$75k`, `age_sec <= 5.0s`, signos $I_{\text{CVD},1m}$/$I_{\text{liq},1m}$) mediante `tools/replay_micro_tape.py` y confirmar fills/fricción real (`~16–18 bps RT`, fricción `<= 16%` TP).

### Fase 5 — Promoción Atómica a Producción (Dual `A + B` Testnet -> Mainnet) + Kelly `0.25x` Condicional
- Integrar flags opt-in de Máquina Dual en `lnm_sniper_v6_advanced.py` y `config/learning_thresholds*.json` manteniendo paridad `100/12 + 28` y `strict_sha=True`.
- Certificar `verify_repo.py --mode full` + `oos-fold1` + ventana `30–50` cierres limpios en Testnet antes de habilitar en Mainnet; solo tras estabilizar Dual evaluar extensión condicional de Kelly fraccional `0.25x` ($r_i \in [0.0050, 0.0125]$).

---

## 4. Matriz Maestra de Compuertas Go / No-Go

| Fase | Entorno | Criterio GO | Condición NO-GO |
| :--- | :--- | :--- | :--- |
| **Fase 0 (Workspace & Agentes)** | Local / Git | **COMPLETADA**: `9` docs/skills alineados, `0` `.py`/`.json` alterados por Fase 0, Hub-and-Spoke definido | Mutación accidental de `.py`/`.json` o cachés WSL |
| **Fase 1A (`exit_asym`)** | WSL `18M` + Fold 1 | `test_simulation_engine.py` PASS, `oos-fold1` `S0` intacto (`N=130`, `+26,933`), `S1 18M` mejora sangría lateral sin recortar `M16`/ganadores | Regresión en paridad `S0` o recorte de expansiones Trend |
| **Fase 1B (`scalp_sim_engine.py`)** | WSL + Tests | Motor causal 1 `.pkl` (`< 500 MB` RAM, `0` Swap), integración limpia con `LaneBudgetController`, tests unitarios PASS | Look-ahead bias, > `500 MB` RAM o violación de slots/SL |
| **Fase 2 (B Standalone `18M`)** | WSL `58` bloques (`S1 @ 16–18 bps`) | Expectancy positiva (`PF > 1.10`, `FER >= 1.2`), fricción `<= 16%` TP, `DD_B <= 1.50%`, estabilidad entre meses | Expectancy negativa tras fills LIMIT conservadores o selección adversa |
| **Fase 3 (Dual `A + B` `18M`)** | WSL `58` bloques (`S1`) | `Net Dual > Net Carril A`, `MaxDD < 4.65%`, `HFViol = 0`, `0` contagio a `lane_a_recovery_mode`, `M16` preservado | Degradación de Carril A, `MaxDD >= 4.70%` o fuga de slots/margen |
| **Fase 4 (Cinta Real `5–7d`)** | Termux + `replay_micro_tape.py` | `P95 < 0.300 ms`, `stale < 2%`, calibración confirmada de `micro_flow_guard`, fricción `LIMIT <= 16%` TP | Latencia > `0.300 ms`, `stale` elevado o divergencia severa vs `S1` |
| **Fase 5 (Promoción Atómica)** | WSL + Testnet `30–50` -> Mainnet | `12/12` suites + tests duales PASS, `oos-fold1` PASS, `0` desfase sats, `MaxDD < 4.5%`, `DDG = 0` en Testnet | Desfase fees/balance, `strict_sha` roto, órdenes huérfanas o `DDG > 0` |
