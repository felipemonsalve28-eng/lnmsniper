# simulation/AGENTS.md — Reglas Estrictas del Simulador y OOS

Este directorio opera bajo las reglas raíz de [AGENTS.md](../AGENTS.md) más los invariantes propios de paridad de simulación y entrenamiento offline.

## Invariantes Obligatorios en `simulation/`
1. **Paridad 1:1 con Producción (`C1_STREAK3_24H_INV` + Modo B):**
   - `SimTrainingEnvironment` (`sim_training_environment.py:L95-1398`) y `SimLNMExchange` (`sim_lnm_exchange.py:L19-501`) deben mantener `inverse_contract_math=True` por defecto, incluso tras llamadas a `reset()`.
   - Las banderas por defecto deben replicar producción: `deterministic_trend_policy=True`, `fe_off_trend=True`, `short_exp_phys_hold=True`, `streak3_24h=True` y `LiquidityConfig` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`).
   - En evaluaciones deterministic/Monte Carlo (`learning_enabled=False`), la Q-table, `state_visits` y `AdaptiveHedgeController` deben tener **0 mutaciones** (`mutations_detected == 0`).
2. **Protección de Cerebros y Cachés WSL:**
   - **Prohibido** escribir o mutar `q_brain_advanced_lnm.json` o `q_brain_testnet_lnm.json` desde simulaciones o tests. Copia siempre semillas a `tempfile.TemporaryDirectory()` o escribe en `q_brain_simulated_candidate.json` / `hedge_state_simulated_candidate.json`.
   - **Intocables:** Preservar intactos `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` archivos `.pkl` cada uno).
3. **Referencia de Paridad Directa Fold 1 OOS (`M13..M18`):**
   - Con los defaults del repositorio sobre `/home/akapana/.cache/sniper_wf_s2_cache/oos_m12.pkl` .. `oos_m17.pkl` y semilla `q_brain_s2_clean_seed.json`, la salida esperada exacta es:
     `N=130`, `Net=+26,933 sats`, `PF=1.73` (`1.729`), `MaxDD=4.10%`, `DD>=4.5%=0/6`, `DDG=0`, `OTR=0`, `FE=0`, `M16=+31,746 sats (52)`.
   - Verifica la paridad tras tocar `simulation/` con:
     `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1`
