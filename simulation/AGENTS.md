# simulation/AGENTS.md — Reglas Estrictas del Simulador y OOS

Este directorio opera bajo [AGENTS.md](../AGENTS.md) más los invariantes de paridad de simulación, estrés Fase 5 y validación OOS.

## Invariantes Obligatorios en `simulation/`
1. **Paridad 1:1 con Producción (`C1_STREAK3_24H_INV` + Modo B):**
   - `SimTrainingEnvironment` (`sim_training_environment.py:L143-1468`) y `SimLNMExchange` (`sim_lnm_exchange.py:L19-501`) mantienen `inverse_contract_math=True` por defecto, incluso tras `reset()`.
   - Defaults idénticos a producción: `deterministic_trend_policy=True`, `fe_off_trend=True`, `short_exp_phys_hold=True`, `streak3_24h=True` y `LiquidityConfig` en **Modo B `LIQ_BIAS_ONLY`** (`enabled=1`, `shadow_only=0`, `opposite_wall_enabled=0`).
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
