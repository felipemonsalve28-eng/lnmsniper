---
name: quant-math-audit
description: >-
  Use this skill when designing, modifying, or auditing quantitative math,
  Dual Machine (Trend + Scalp) contracts, LaneBudgetController isolation,
  micro_flow_guard fail-open thresholds, inverse BTCUSD accounting, Phase 5
  stress, or simulator-production parity.
---

# Auditoría Cuantitativa y de Matemática Aplicada (LNM Sniper v7.4+ Dual)

Checklist obligatorio para toda intervención en contabilidad, física de riesgo, simulador, Máquina Dual (`Carril A + Carril B`) o microestructura.

## 1. Contrato Inverso Exacto (`BTCUSD`) y Comisiones API v3
- **`GrossPnL_sats` exacto (`inverse_contract_math=True`):**
  $$\text{GrossPnL}_{\text{sats}}(P_0 \to P_1) = M_{\text{sats}} \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right) \cdot d, \quad d \in \{+1\ (\text{LONG}),\ -1\ (\text{SHORT})\}$$
- **`Fees_sats`, `LossAtSL_sats` y `FER` reales (`openingFee`/`closingFee`/`sumFundingFees`):**
  $$\text{Fees}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(f_{\text{open}} + \frac{P_0}{P_1} \cdot f_{\text{close}}\right) + C_{\text{funding}}$$
  $$\text{LossAtSL}_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(\frac{|P_0 - P_{\text{SL}}|}{P_{\text{SL}}} + f_{\text{open}} + \frac{P_0}{P_{\text{SL}}} \cdot f_{\text{close}}\right), \qquad \text{FER} = \frac{\text{NetPnL}_{\text{sats}}}{\text{Fees}_{\text{sats}}}$$
- **Cero doble descuento y Bellman terminal:** Si `real_balance_delta_sats` o `pl_includes_fees=True` existen, prohibido restar fees otra vez; en cierre (`terminal=True`), $\gamma_{\text{eff}}=0$ y pérdida neta siempre reduce $Q(s,a)$; `decouple_floating_pnl=1`.

## 2. Política Determinista (`C1_STREAK3_24H_INV`), P0 y Memoria Anti-Retrabajo
- Bajo `DETERMINISTIC_TREND_POLICY = True`, salida bit a bit idéntica con cerebro limpio o contaminado (`Q<0`, `visits>0`); sin cascada `TREND -> SNIPER`, empate $\to$ `HOLD (0)`, `lcb_penalty=0.0`, sin CQL/`expectancy_boost`/`runtime_shadow_cb`; prior Beta-Binomial simétrico preservando `Test 7a`.
- `ENABLE_HYBRID_HEDGE=False`, `limit_execution.enabled=0`, Modo B `LIQ_BIAS_ONLY` (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`), `MAX_CONCURRENT_TRADES=5`, `HardFloorGuardian` (`0.75 -> 0.88`), `TREND_SL_FLOOR_PCT=0.0065`, `SHORT_EXP_MIN_DSL=0.006505`, `C1_STREAK3_24H` (`86,400s` tras `3` pérdidas/lado).
- **Memoria Anti-Retrabajo (`18M`, `58` bloques, `S1 @ 16 bps` — Prohibido reabrir sin evidencia OOS nueva):**
  - `SL=1.15%` (`CD=24h` y `CD=8h`), `BASE 0.65% | CD=8h`, recorte de `RR` (`2.7`/`2.3`) o quitar BE/Trailing en Trend, `V2_PB_1H` (`PB_BASE`/`PB_EMA50`), y Fase 5B vieja (`B1`/`B2` sin flujo real).

## 3. Contratos Cuantitativos de la Máquina Dual (`v7.4+`)
- **Carril A (`Trend` + `exit_asym`):** Línea base certificada `BASE 0.65% | CD=24h | RR=4.2` (`\$1,600–\$2,500+`); `FORCED_EDGE_EXIT` asimétrico solo en pérdidas (`forced_edge_loss_only`) detrás de flag opt-in que preserva paridad exacta `S0` por defecto.
- **Carril B (`Scalp` Punto Dulce):**
  - Piernas intradía de `\$500–\$1,000` (`0.85%–1.70%`), ejecución `LIMIT` pasiva (`~16–18 bps RT`), `SL` `0.45%–0.65%`, `TP` `0.95%–1.60%` (`1.9R–2.5R`), fricción total `<= 16%` del `TP`.
  - Módulos: `liquidity_radar.py`, `limit_execution_manager.py`, `DEF_SCALP_LOCK`. Cota de selección adversa LIMIT: $\Delta_{\text{imp}} - \Delta_{\text{adv}} > E_{\text{mkt}} \cdot (1 - p_{\text{fill}})/p_{\text{fill}}$.
- **Aislamiento `LaneBudgetController` (`simulation/lane_budget.py`) bajo `MAX_CONCURRENT_TRADES = 5`:**
  - Carril A: `3` slots reservados (hasta `5` totales).
  - Carril B: máx `2` slots (`1`/lado), solo si `N_A + N_B <= 3` y margen B `<= 15%`.
  - `lane_a_recovery_mode` (`3.50%`) solo lee `DD_A`.
  - B reduce sizing a `0.5x` si `DD_B >= 0.80%`; pausa `24h` si `DD_B >= 1.50%` o `2` pérdidas seguidas mismo lado; freno cruzado sobre B si `DD_A >= 2.00%` o `DD_global >= 2.20%`.
  - Cota de riesgo abierto al SL en B: $\sum \text{LossAtSL}_{B} \le (0.0150 - \text{DD}_B) \cdot E_{\text{global}}$.
- **Microestructura (`micro_flow_guard.py`):**
  - Fail-open $O(1)$ (`ALLOW` ante fallo o `age_sec > 5.0s`); umbrales `V_tot_1m >= \$250k`, `trades_1m >= 30`, `L_tot_1m >= \$75k`.
- **Referencias heredadas certificadas/condicionales:**
  - **Estrés `S0`/`S1`/`S2` (solo `simulation/`):** $\delta_{\text{slip}}(t) = \delta_{\text{base}} + k_{\text{vol}} \max(0, \text{ATR}_{5m}/P_t - \theta_{\text{vol}}) + 0.25 \sqrt{\Delta t_{\text{exec}}/60} \cdot (H_t - L_t)/P_t$; `S0` preserva paridad Fold 1 OOS (`N=130`, `+26,933`, `PF=1.729`).
  - **Kelly Fraccional `0.25x` (extensión Fase 5):** $p_{\text{eff}} = \max(0, p - \sqrt{p(1-p)/N})$, $r_i \in [0.0050, 0.0125]$, $M_i = \lfloor r_i E_{\text{avail}} / (L \cdot \ell_{\text{SL}}) \rfloor$ sujeto a $\sum \text{LossAtSL}_j + M_i L \ell_{\text{SL}} \le 0.80 B_{\text{floor}}$.

## 4. Paridad 4-Vías (`100/12 + 28`), Streaming y Verificación
- Mantener paridad `1:1` entre `config/learning_thresholds*.json`, `learning_thresholds_manager.py` y `sniper_cli_catalog.py`.
- Todo runner `18M` debe usar streaming `1` `.pkl` a la vez (`del blk; gc.collect()`, `< 500 MB` RAM, `0` Swap) y validar `verify_repo.py --mode oos-fold1` tras tocar señales, riesgo o `simulation/`.
