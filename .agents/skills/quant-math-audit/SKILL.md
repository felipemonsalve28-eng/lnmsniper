---
name: quant-math-audit
description: >-
  Use this skill when designing, modifying, or auditing quantitative math,
  inverse BTCUSD accounting, Phase 5 latency/slippage stress, Phase 6 bounded
  fractional Kelly sizing, Phase 7 micro_bias/limit adverse selection, or
  simulator-production parity.
---

# Auditoría Cuantitativa y de Matemática Aplicada (LNM Sniper)

Checklist obligatorio para toda intervención en contabilidad, física de riesgo, simulador, sizing o microestructura.

## 1. Contrato Inverso Exacto (`BTCUSD`) y Comisiones API v3 (`0.1595%` RT)
- **PnL bruto exacto (`inverse_contract_math=True`):**
  $$\text{GrossPnL}(P_0 \to P_1) = M_{\text{sats}} \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right) \cdot d, \quad d \in \{+1\ (\text{LONG}),\ -1\ (\text{SHORT})\}$$
- **Fricción real (`openingFee`/`closingFee`/`sumFundingFees`):**
  $$C_{\text{open}} = M \cdot L \cdot f_{\text{open}}, \quad C_{\text{close}} = M \cdot L \cdot \frac{P_0}{P_1} \cdot f_{\text{close}}, \quad C_{\text{RT}} = C_{\text{open}} + C_{\text{close}} + C_{\text{funding}}, \quad \text{FER} = \frac{\text{NetPnL}}{C_{\text{RT}}}$$
- **Cero doble descuento y Bellman terminal:** Si `real_balance_delta_sats` o `pl_includes_fees=True` existen, prohibido restar fees otra vez; en cierre (`terminal=True`), $\gamma_{\text{eff}}=0$ y pérdida neta siempre reduce $Q(s,a)$; `decouple_floating_pnl=1`.

## 2. Política Determinista (`C1_STREAK3_24H_INV`) e Invariantes P0
- Bajo `DETERMINISTIC_TREND_POLICY = True`, salida bit a bit idéntica con cerebro limpio o contaminado (`Q<0`, `visits>0`); sin cascada `TREND -> SNIPER`, empate $\to$ `HOLD (0)`, `lcb_penalty=0.0`, sin CQL/`expectancy_boost`/`runtime_shadow_cb`; prior Beta-Binomial simétrico preservando `Test 7a`.
- `ENABLE_HYBRID_HEDGE=False`, `limit_execution.enabled=0`, Modo B `LIQ_BIAS_ONLY` (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`), `MAX_CONCURRENT_TRADES=5`, `HardFloorGuardian` (`0.75 -> 0.88`), `TREND_SL_FLOOR_PCT=0.0065`, `SHORT_EXP_MIN_DSL=0.006505`, `C1_STREAK3_24H` (`86,400s` tras `3` pérdidas/lado).

## 3. Contratos Cuantitativos de Hoja de Ruta (Fases 5 a 7)
- **Fase 5 (Estrés WSL, solo `simulation/`):** $\delta_{\text{slip}}(t) = \delta_{\text{base}} + k_{\text{vol}} \max(0, \text{ATR}_{5m}/P_t - \theta_{\text{vol}}) + 0.25 \sqrt{\Delta t_{\text{exec}}/60} \cdot (H_t - L_t)/P_t$; precio adverso $P_{\text{exec}}^{\text{entry}} = P_0(1 + a_{\text{side}} \delta_{\text{slip}})$ y $P_{\text{exec}}^{\text{SL}} = P_{\text{SL}}(1 - a_{\text{side}} \delta_{\text{slip}})$. `S0` debe preservar paridad exacta Fold 1 OOS.
- **Fase 6 (Kelly Fraccional $0.25\times$ bajo P0, tras `30-50` cierres y GO S1):** $p_{\text{eff}} = \max(0, p - \sqrt{p(1-p)/N})$, $r_i \in [0.0050, 0.0125]$ (forzar $r_{\min}$ si `side_loss_streak == 2`), factor de pérdida exacto $\ell_{\text{SL}} = |P_0 - P_{\text{SL}}|/P_{\text{SL}} + f_{\text{open}} + (P_0/P_{\text{SL}})f_{\text{close}}$, margen $M_i = \lfloor r_i E_{\text{avail}} / (L \cdot \ell_{\text{SL}}) \rfloor$ sujeto a $\sum \text{LossAtSL}_j + M_i L \ell_{\text{SL}} \le 0.80 B_{\text{floor}}$.
- **Fase 7A (Micro-bias condicional, $\Delta\text{FER} \ge +0.15$):** Fail-open (`micro_bias = 0.0` si `not available`, `stale` o `age_sec > 5.0`), acotado en $[-0.08, +0.06]$ (jamás anula veto OVR `-0.35` ni macro `-0.12`).
- **Fase 7B (LIMIT + TTL condicional):** Solo habilitar si $\Delta_{\text{imp}} - \Delta_{\text{adv}} > E_{\text{mkt}} \cdot (1 - p_{\text{fill}})/p_{\text{fill}}$ y tarifa maker real verificada `< 0.08%`/lado.

## 4. Paridad 4-Vías (`100/12 + 28`) y Verificación
- Mantener paridad `1:1` entre `config/learning_thresholds*.json`, `learning_thresholds_manager.py` y `sniper_cli_catalog.py`, y validar `verify_repo.py --mode oos-fold1` tras tocar señales, riesgo o `simulation/`.
