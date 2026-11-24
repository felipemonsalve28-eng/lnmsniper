---
name: quant-math-audit
description: >-
  Use this skill when designing, modifying, or auditing quantitative math,
  inverse BTCUSD accounting, Q-learning/Bayesian gates, risk sizing, drawdown
  guards, liquidity radar, thresholds schema, or simulator-production parity.
---

# Auditoría Cuantitativa y de Matemática Aplicada (LNM Sniper)

Checklist obligatorio para toda intervención que toque dinero real, contabilidad, física de riesgo, señales de mercado, umbrales o simulador.

## 1. Matemática de Contratos Inversos (`BTCUSD`) y Contabilidad
- **Valuación hiperbólica (`inverse_contract_math=True`):**
  $$\Delta_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(\frac{1}{P_0} - \frac{1}{P_1}\right) \cdot d, \quad d \in \{+1\ (\text{buy}),\ -1\ (\text{sell})\}$$
- **Cero doble descuento de fricción:** Si `real_balance_delta_sats` o `pl_includes_fees=True` están presentes, queda prohibido volver a restar comisiones taker o funding del PnL neto.
- **Bellman terminal y desacople flotante:** En cierres (`terminal=True`), validar $\gamma_{\text{eff}} = 0.0 \implies Q \leftarrow Q + \alpha(R - Q)$ y pérdida neta siempre con gradiente negativo sobre $Q(s,a)$; ticks flotantes abiertos (`decouple_floating_pnl=1`) no mutan `q_table`.

## 2. Política Determinista Simétrica (`C1_STREAK3_24H_INV`)
- Bajo `DETERMINISTIC_TREND_POLICY = True`, `choose_action`, ranking, `estimate_bayesian_win_rate` y `evaluate_and_dispatch_entries` deben producir exactamente la **misma salida bit a bit** con cerebro limpio o con `q_brain_*.json` legado contaminado (`Q < 0`, `visits > 0`, `style_metrics`).
- Sin cascada `TREND -> SNIPER`, sin sesgo `BULL > BEAR`, empate inter-lado $\rightarrow$ `HOLD (0)`, `lcb_penalty = 0.0`, CQL desactivado y sin `expectancy_boost` ni `runtime_shadow_cb`.
- En `estimate_bayesian_win_rate` (`sniper_perception_sats_core.py`), preservar el prior Beta-Binomial simétrico para `TREND` cuando `q_val is not None` sin romper `Test 7a` cuando `q_val is None`.

## 3. Física de Riesgo, Drawdown y Módulos Vigentes
- `ENABLE_HYBRID_HEDGE == False` y `limit_execution.enabled == 0` por defecto.
- Escalera P0 exacta: `BASE_RISK_PCT_STD = 0.0075`, `RECOVERY_ENTER_DD = 0.035`, `DD_THROTTLE_HALT_PCT = 0.045`, `MAX_DD_GUARD_PCT = 0.052`, `HardFloorGuardian` (`0.75` inicial $\to$ `0.88` meta) y `MAX_CONCURRENT_TRADES = 5` (`mainnet` y `testnet`).
- `C1_SHORT_EXP_PHYS`: `TREND_SL_FLOOR_PCT = 0.0065` y bloqueo de `S_TREND` cuando `sl_dist_pct <= 0.006505`.
- `C1_STREAK3_24H`: `SideStreakBrakeStore` dispara `86,400 s` de pausa por lado tras `3` pérdidas consecutivas (`net_pnl_sats <= 0`) leyendo en RAM `is_side_paused` en $O(1)$.
- `LiquidityRadar` **Modo B `LIQ_BIAS_ONLY`**: `enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0` (sin look-ahead: solo pivotes pasados $j \le t - 1 - k$ y sin vetos `BLOCKED_OPPOSITE_LIQ_WALL` en `TREND`).

## 4. Paridad 4-Vías de Umbrales (`100` Parámetros) y Simulador
- Cualquier umbral debe mantenerse sincronizado `1:1` entre `config/learning_thresholds.json`, `config/learning_thresholds_testnet.json`, `learning_thresholds_manager.py` y `sniper_cli_catalog.py` (`100` claves, `12` categorías, `0` huérfanas).
- Verificar paridad de `SimTrainingEnvironment` tras cualquier cambio en señales/riesgo con:
  `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1`
