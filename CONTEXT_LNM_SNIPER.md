# CONTEXTO OPERATIVO Y MEMORIA PERSISTENTE DEL PROYECTO
## LNM SNIPER v7.3+ (`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`)

> **Memoria Técnica Estructural para Agentes en Antigravity**  
> **Última Actualización:** `2026-11-26`  
> **HEAD Auditado Base:** `10a87c9` (`master` + fix ruta `/market/stream` en sidecar Binance Futures)  
> **Gobernanza y Reglas:** [`AGENTS.md`](AGENTS.md) · [`simulation/AGENTS.md`](simulation/AGENTS.md) · [`AGENT_MAINTENANCE_DIRECTIVE.md`](AGENT_MAINTENANCE_DIRECTIVE.md)

---

## 1. ESTADO ACTUAL DEL SISTEMA (EXECUTIVE SNAPSHOT)

| Componente | Estado Vigente (`2026-11-26`) | Referencia / Evidencia |
| :--- | :--- | :--- |
| **Rama y Remoto** | `master` (`d0ea3fd` Fase 1B + `5196b85` API v3 camelCase + `cef38c0`/`10a87c9` + fix `/market/stream` Fase 4 Sidecar Binance `shadow_only`) | [`binance_micro_sidecar.py`](binance_micro_sidecar.py), [`liquidity_radar.py`](liquidity_radar.py), [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Edición / Política** | **`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`**: política determinista simétrica inmutable en RAM, `E2_FE_OFF`, `C1_SHORT_EXP_PHYS`, freno `C1_STREAK3_24H`, `STRUCTURAL_TD9_TRAILING_ENABLED=False` | [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py), [`sniper_perception_sats_core.py`](sniper_perception_sats_core.py) |
| **Límite de Posiciones** | Máximo **`5`** posiciones concurrentes tanto en `mainnet` como en `testnet` (`Config.MAX_CONCURRENT_TRADES = 5`) | [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Guardián P0 & Riesgo** | `HardFloorGuardian` (`75%` -> `88%`), `BASE_RISK_PCT_STD=0.0075`, `RECOVERY_ENTER_DD=0.035`, `DD_THROTTLE_HALT_PCT=0.045`, `MAX_DD_GUARD_PCT=0.052`, `TREND_SL_FLOOR_PCT=0.0065`, `SHORT_EXP_MIN_DSL=0.006505` | [`guardian_execution_core.py`](guardian_execution_core.py), [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Módulos Apagados / Modo B** | `ENABLE_HYBRID_HEDGE=False`, `FE_OFF_TREND=True`, `limit_execution.enabled=0`, `liquidity_radar.enabled=1`, `liquidity_radar.shadow_only=0`, `liquidity_radar.opposite_wall_enabled=0` | [`config/learning_thresholds.json`](config/learning_thresholds.json), [`liquidity_radar.py`](liquidity_radar.py) |
| **Sidecar Binance (`shadow_only`)** | `binance_micro_sidecar.py` (`100%` stdlib RFC 6455 WS hacia `wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder`, `read_timeout_sec=120.0` tolerante a `TimeoutError`, REST cada `30s`, `connected` default `False`, lectura O(1) `P95 < 0.02 ms` solo para telemetría `binance_micro`) | [`binance_micro_sidecar.py`](binance_micro_sidecar.py), [`test_binance_micro_sidecar.py`](test_binance_micro_sidecar.py) |
| **API v3 & Cierre Limpio** | `TokenBucket(rate=20.0/s, capacity=40.0)`, `generate_client_id` único en `open_trade()`, parseo camelCase `openingFee`/`closingFee`/`sumFundingFees`, auto-sync reloj `HTTP 401`, `_handle_shutdown_signal` lock-free (`0.25s` + `try/finally`), `--help` y `--dry-check` sin credenciales | [`guardian_execution_core.py`](guardian_execution_core.py), [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Cerebros & Hedge SHA-256** | `strict_sha=True` (`AtomicBrainStore`): [`q_brain_advanced_lnm.json`](q_brain_advanced_lnm.json) (`360` estados, SHA `986eff5ca876...`), [`q_brain_testnet_lnm.json`](q_brain_testnet_lnm.json) (`360` estados, `8b7713e2f705...`), [`hedge_state_mainnet.json`](hedge_state_mainnet.json) (`pairs`, `e98eb4584ac5...`), [`hedge_state_testnet.json`](hedge_state_testnet.json) (`pairs`, `780a54f95c36...`) | Verificados tras commits y `11/11` + sidecar suites |
| **Estado Efímero / Termux** | `telemetry_live.json` y `telemetry_live_testnet.json` desversionados (`git rm --cached` + `.gitignore`); `side_brake_state_*.json*` y `binance_micro_snapshot.json*` en `$HOME/.sniper_state/` (lecturas $O(1)$ sin I/O bloqueante) | [`.gitignore`](.gitignore), [`side_streak_brake.py`](side_streak_brake.py), [`binance_micro_sidecar.py`](binance_micro_sidecar.py) |
| **Esquema y Catálogo CLI** | **`100` umbrales** en **`12` categorías** (`0` claves huérfanas), **`28` parámetros runtime** y **`5` presets** (`ENABLE_HYBRID_HEDGE=False` en todos) | [`learning_thresholds_manager.py`](learning_thresholds_manager.py), [`sniper_cli_catalog.py`](sniper_cli_catalog.py) |
| **Cachés OOS WSL** | `/home/akapana/.cache/sniper_wf_s2_cache` (`58` `.pkl`) y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl`) intactos | WSL Ubuntu (`akapana`) |

---

## 2. INVARIANTES MATEMÁTICOS, CONTRATOS Y MÉTRICAS CERTIFICADAS

### A. Contratos Cuantitativos Críticos (P0)
1. **Contrato de Acciones y Estado:** Vector de `9` acciones (`0: HOLD`, `1..4: LONG [TREND, REVERSION, SNIPER, DEF_SCALP]`, `5..8: SHORT [TREND, REVERSION, SNIPER, DEF_SCALP]`) y estado de `5` segmentos `REGIME|OSC|CONC|MACRO|EMA`.
2. **Aislamiento Determinista en Memoria (`DETERMINISTIC_TREND_POLICY = True`):**
   - [`choose_action`](lnm_sniper_v6_advanced.py) y [`evaluate_and_dispatch_entries`](lnm_sniper_v6_advanced.py) evalúan la semilla simétrica limpia inmutable en RAM, ignorando `Q` negativos, `state_visits > 0` y `style_metrics` de cerebros legados en disco.
   - [`compute_live_edge`](lnm_sniper_v6_advanced.py) omite penalizaciones CQL y filtros shadow/expectancy_boost bajo cerebro congelado determinista.
   - [`estimate_bayesian_win_rate`](sniper_perception_sats_core.py) emplea prior simétrico Beta-Binomial $(11.0, 11.0)$ para `TREND` cuando `q_val is not None`, preservando `Test 7a` cuando `q_val is None`.
3. **Matemática Inversa `BTCUSD` y Contabilidad Real Net-Sats:**
   - PnL inverso exacto: $\Delta_{\text{sats}} = M_{\text{sats}} \cdot L \cdot (1/P_0 - 1/P_1) \cdot d$.
   - Recompensa: $R_{\text{sats}} = \Delta \text{Balance} - \lambda(\text{MAE}) - \mu(\Delta t)$, Bellman terminal ($\gamma_{\text{eff}} = 0$) en [`_update_q`](lnm_sniper_v6_advanced.py), desacople flotante (`decouple_floating_pnl == 1`), cero doble descuento de comisiones y reducción obligatoria de $Q(s,a)$ ante pérdida neta.
4. **Filtros Físicos de Entrada y Riesgo (`C1_STREAK3_24H_INV`):**
   - `C1_SHORT_EXP_PHYS`: `S_TREND (5)` requiere `sl_dist_pct > 0.006505`; si $\le 0.006505$ bloquea a `HOLD (0)`.
   - `C1_STREAK3_24H`: `3` pérdidas netas seguidas (`net_pnl_sats <= 0`) en un lado (`b` o `s`) disparan pausa de `86,400 s` (`24 h`) solo en ese lado; lectura `is_side_paused` en RAM sin tocar disco.
   - **Paridad 1:1 en Simulación:** [`SimTrainingEnvironment`](simulation/sim_training_environment.py) replica `PrecomputedMarketFeed`, dos etapas de guardián/BE en política determinista, salto del subsistema hedge cuando `deterministic_trend_policy=True`, `opposite_wall_enabled=False` y `compute_liquidity_bias` activo (**Modo B `LIQ_BIAS_ONLY`**).

### B. Benchmark A/B 18M y Paridad Directa Fold 1 OOS (`inverse_contract_math = True`, `0.20% RT`)

| Escenario | Modo A (`SHADOW_ON`) | Modo B (`LIQ_BIAS_ONLY`, **Vigente**) | Criterio / Observación |
| :--- | :---: | :---: | :--- |
| **`M01..M06` (Fold 2 OOS)** | `N=247` \| `Net=-8,851` \| `PF=0.899` \| `DD=4.40%` | `N=247` \| **`Net=-8,841`** \| `PF=0.899` \| `DD=4.40%` | `DD>=4.5%=0/6`, `DDG=0`, `FE=0`, `M02 BULL=+12,750` |
| **`M07..M12` (Semestre 2)** | `N=294` \| `Net=-26,343` \| `PF=0.744` \| `DD=4.66%` | `N=295` \| **`Net=-26,288`** \| `PF=0.745` \| `DD=4.66%` | `DD>=4.5%=1/6`, `DDG=0`, `FE=0`, `M09 BEAR=+8,392` |
| **`M13..M18` (Fold 1 OOS)** | `N=114` \| `Net=+26,814` \| `PF=1.818` \| `DD=4.10%` | `N=130` \| **`Net=+26,933`** \| `PF=1.729` \| `DD=4.10%` | `DD>=4.5%=0/6`, `DDG=0`, `OTR=0`, `FE=0`, `M16=+31,746 (N=52)` |
| **`12M OOS` (`F1 + F2`)** | `N=361` \| `WR=48.20%` \| `Net=+17,963` \| `PF=1.149` | `N=377` \| `WR=48.54%` \| **`Net=+18,092`** \| `PF=1.145` | Ganador determinista Regla 2 (`+129 sats`, `PosMo=5/12`, `DDG=0`) |
| **`Total 18M` (`M01..M18`)** | `N=655` \| `WR=45.95%` \| `Net=-8,380` \| `PF=0.963` | `N=672` \| `WR=46.28%` \| **`Net=-8,196`** \| `PF=0.964` | `+184 sats` vs Modo A, `MaxDD=4.66% <= 4.70%`, `DDG=0` |

- **Paridad Directa Fold 1 OOS Mes a Mes (`verify_repo.py --mode oos-fold1`):**
  `M13: N=14 Net=-3687 DD=3.69%` \| `M14: N=15 Net=+193 DD=3.72%` \| `M15: N=9 Net=-3049 DD=3.53%` \| `M16: N=52 Net=+31746 DD=3.87%` \| `M17: N=22 Net=+2788 DD=4.10%` \| `M18: N=18 Net=-1058 DD=4.02%`.
- **Estrés & Monte Carlo (`C1_STREAK3_24H_INV`):** Sensibilidad de comisiones `12M OOS`: `+31,603 sats` (`PF=1.27` @ `0.15% RT`) y `+45,104 sats` (`PF=1.41` @ `0.10% RT`); estrés `+50%` slippage + `1-bar` latencia: `+13,488 sats` (`PF=1.11`, `MaxDD=4.63%`, `DDG=0`); Monte Carlo `1,000` caminos *block-bootstrap* (`seed=20260331`): `P(Net>0)=62.10%`, `P50=+9,790 sats`, `P(DDG>=5.2%)=0.00%`, `P(HardFloor)=0.00%`.

---

## 3. MAPA QUIRÚRGICO AST DE SÍMBOLOS Y LÍNEAS (LECTURA ACOTADA `<= 150 L`)

Consulta este índice verificado por AST antes de abrir cualquier módulo con `read_file`:

- **[`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) (`5024L`):** `_build_binance_micro_telemetry:L76-114`, `_evaluate_liquidity_from_mtf:L117-150`, `Config:L156-341`, `load_saved_credentials:L354-403`, `APIHealthMonitor:L409-451`, `MultiTimeframeCache:L457-744`, `calc_ema:L750-760`, `_normalize_ohlc_basis:L763-777`, `compute_indicators:L780-886`, `_build_indicator_prefix_state:L889-998`, `compute_indicators_from_prefix:L1001-1110`, `AutonomousGoalBrain:L1116-2220`, `_clean_numbers:L2226-2233`, `LNMarketsClient:L2236-2534`, `extract_trade_funding_sats:L2540-2550`, `extract_trade_fees_v3:L2553-2573`, `compute_account_equity:L2576-2585`, `compute_effective_risk:L2588-2600`, `infer_adopted_trade:L2603-2652`, `calculate_strategy_order_params:L2655-2666`, `confirm_and_close_trade:L2669-2689`, `confirm_and_update_stoploss:L2692-2706`, `enforce_concurrency_cap:L2709-2726`, `compute_live_edge:L2729-2841`, `execute_strategy_guardian:L2844-3107`, `evaluate_open_trades_online:L3110-3178`, `compute_situational_order_params:L3181-3286`, `evaluate_system_mode:L3289-3340`, `_is_hybrid_hedge_active:L3343-3349`, `reconcile_portfolio_with_hedges:L3352-3417`, `run_integrated_guardian_and_hedge_cycle:L3420-3526`, `record_shadow_order:L3529-3538`, `update_shadow_trades:L3541-3683`, `process_settlement_queue:L3686-3815`, `manage_open_positions_and_hedges:L3818-3860`, `evaluate_and_dispatch_entries:L3863-4414`, `_parse_cli_env:L4417-4426`, `_print_cli_usage:L4429-4438`, `main:L4441-5020`.
- **[`binance_micro_sidecar.py`](binance_micro_sidecar.py) (`1251L`):** `WSProtocolError:L76-78`, `WSFrame:L80-87`, `compute_websocket_accept:L104-108`, `encode_ws_frame:L123-164`, `build_masked_pong_frame:L166-179`, `build_masked_close_frame:L181-196`, `decode_ws_frame_bytes:L198-251`, `read_ws_frame:L254-283`, `consume_ws_frames:L286-365`, `_parse_ws_endpoint:L368-383`, `perform_ws_handshake:L386-429`, `BinanceMicroAggregator:L431-715`, `resolve_binance_snapshot_path:L718-728`, `write_binance_micro_snapshot:L731-782`, `read_binance_micro_snapshot:L810-895`, `fetch_binance_rest_metrics_sync:L914-948`, `BinanceMicroSidecarService:L951-1131`, `_run_dry_check:L1188-1210`, `main:L1213-1248`.
- **[`guardian_execution_core.py`](guardian_execution_core.py) (`733L`):** `FileLockContext:L39-107`, `AtomicBrainStore:L110-217`, `LatencyTelemetry:L220-295`, `InFlightOrderGuard:L298-380`, `HardFloorGuardian:L383-419`, `TokenBucket:L422-449`, `ResilientLNMClient:L452-544`, `rotate_log_file_if_needed:L547-564`, `_CompatNumpy:L673-729`.
- **[`sniper_perception_sats_core.py`](sniper_perception_sats_core.py) (`825L`):** `MarketRegimeClassifier:L16-138`, `ShortTermPerceptionEngine:L141-324`, `SatsExpectancyGate:L327-797`, `sync_brain_floor_with_reality:L800-816`.
- **[`side_streak_brake.py`](side_streak_brake.py) (`345L`):** `SideStreakBrakeStore:L29-345`.
- **[`liquidity_radar.py`](liquidity_radar.py) (`1472L`):** `LiquidityConfig:L41-95`, `LiquidityPool:L99-120`, `SuperPool:L124-134`, `fuse_super_pools:L145-274`, `TD9PerfectionTracker:L277-391`, `SessionVWAPAccumulator:L394-456`, `DeterministicMTFAggregator:L459-707`, `detect_causal_swings:L770-824`, `cluster_swings_to_pools:L827-862`, `filter_unmitigated_pools:L865-903`, `compute_opposite_pool_room:L906-962`, `compute_liquidity_bias:L965-993`, `LiquidityRadar:L996-1418`, `compute_structural_td9_trailing_stop:L1421-1472`.
- **[`limit_execution_manager.py`](limit_execution_manager.py) (`623L`):** `LimitExecutionConfig:L32-66`, `round_to_tick:L69-83`, `should_route_limit:L86-139`, `compute_limit_entry_plan:L142-262`, `LimitExecutionManager:L265-623`.
- **[`learning_thresholds_manager.py`](learning_thresholds_manager.py) (`1227L`):** `resolve_thresholds_path_for_env:L42-54`, `LearningThresholdsManager:L933-1204`, `get_thresholds_manager:L1212-1227`.
- **[`learning_quality_supervisor.py`](learning_quality_supervisor.py) (`1137L`):** `LearningQualitySupervisor:L104-1051`, `AtomicTelemetryDispatcher:L1054-1137`.
- **[`simulation/sim_training_environment.py`](simulation/sim_training_environment.py) (`1468L`):** `PrecomputedMarketFeed:L49-140`, `SimTrainingEnvironment:L143-1468`.
- **[`simulation/sim_market_feed.py`](simulation/sim_market_feed.py) (`401L`):** `Candle:L26-55`, `MTFAggregator:L58-189`, `SimMarketFeed:L192-401`.
- **[`simulation/sim_lnm_exchange.py`](simulation/sim_lnm_exchange.py) (`501L`):** `SimLNMExchange:L19-501`.

---

## 4. HISTORIAL CONSOLIDADO DE ETAPAS (ETAPAS 1 A 32)

### Resumen Compacto de Etapas Base (1–25)
| Rango | Hito Arquitectónico y Cuantitativo |
| :--- | :--- |
| **Etapas 1–6** | Diagnóstico anti-inflación Q, `config/learning_thresholds.json` con Hot-Reload, fórmula real de recompensa líquida en sats, `LearningQualitySupervisor` (5 signos vitales $O(1)$) y telemetría headless atómica. |
| **Etapas 7–10** | Remediación `F0->F3` (Bellman terminal `terminal=True`, `Schema v2`, cero doble descuento), Cluster Alpha (Bayesiano Beta-Binomial, `1/4` Kelly, `mainnet_epsilon=0.0`) y Cluster Beta (modularización de `main()`, debounce 30s, JSONL streaming, Shadow Mode). |
| **Etapas 11–19** | Paridad Bellman terminal en `HedgeEngine`, motor `simulation/` completo (`SimLNMExchange`, `PrecomputedMarketFeed`, certificación Monte Carlo `0` mutaciones), remediación integral de 25 hallazgos (`F-01`..`F-25`) y reentrenamiento 18M con promoción dual certificada. |
| **Etapas 20–25** | `sniper_cli.py` ligero para Termux (`fcntl` + `SIGHUP`), anti-whipsaw `RANGE_SQUEEZE` en coberturas, normalización dimensional `1k sats`, aislamiento por entorno Mainnet/Testnet, viabilidad neta `FORCED_EDGE_EXIT`, auto-sync reloj `HTTP 401` y purga sintética en `transitions_audit.jsonl`. |

### Etapas Recientes (26–32)

- **Etapa 26 — Auditoría y Paridad del Simulador + Promoción Dual Limpia:**
  - Alineación bipolar `"b"`/`"s"` $\leftrightarrow$ `"buy"`/`"sell"`, `learning_enabled=False` sin mutaciones en Monte Carlo, `update_stoploss`/`update_takeprofit` en `SimLNMExchange` y reconciliación completa en `SimTrainingEnvironment` (`normalize_to_canonical_margin`, `EdgeMonitor`, `PortfolioHedgeCoordinator`, `AdaptiveHedgeController`).
  - Limpieza de 26 residuos `.pre_*`/`.bak` temporales y certificación SHA-256 limpia sobre los 4 archivos de estado en `master`.

- **Etapa 27 — Consolidación `C1_STREAK3_24H_INV` (Commits `refactor(core)`, `1298f2b`, `d133d28`, `c2bdb19`):**
  - Política determinista simétrica sin cascada, asimetría `C1_SHORT_EXP_PHYS` (`SHORT_EXP_MIN_DSL = 0.006505`), `FE_OFF_TREND = True`, apagado por defecto de `Hybrid Hedge` y `limit_execution.enabled = 0`, y creación de [`side_streak_brake.py`](side_streak_brake.py) (`3` pérdidas por lado -> `24 h` pausa en `$HOME/.sniper_state/`).
  - *Nota histórica:* en Etapa 27 se dejó temporalmente `liquidity_radar.shadow_only = 1`; esta bandera fue promovida a **Modo B `LIQ_BIAS_ONLY` (`shadow_only = 0`, `opposite_wall_enabled = 0`)** tras el benchmark A/B 18M de la Etapa 28.

- **Etapa 28 — Auditoría Integral Pre-Termux y Certificación 6 Bloques (`20b205d`, `6578e6d`, `876c3c6`):**
  - `20b205d` (`fix(audit)`): Aislamiento determinista total frente a cerebros legados contaminados, promoción de **Modo B `LIQ_BIAS_ONLY`** (`100` umbrales / `12` categorías), `MAX_CONCURRENT_TRADES = 5` en `testnet`, `MAX_DD_GUARD_PCT = 0.052`, `TokenBucket(20/s, burst 40)` + `clientId` único, `finally` en `SideStreakBrakeStore.save()`, `--help`/`--dry-check` sin credenciales y parada `_handle_shutdown_signal` lock-free.
  - `6578e6d` (`chore(repo)`): Catálogo CLI alineado (`100` umbrales / `28` runtime), `.gitignore` endurecido y `git rm --cached` sobre `telemetry_live.json` y `telemetry_live_testnet.json` (preservados en disco sin ensuciar Git en Termux).
  - `876c3c6` (`test(audit)`): `test_07` (inmunidad ante cerebro legado `Q=-0.50`, `visits=500`), `test_08` (TokenBucket/`clientId`/`--help`/`--dry-check`), paridad directa `M16 BULL = +31,746` y `save_report=False` en `test_protocolo_v_reversal.py` para mantener limpio el árbol Git.
  - **Certificación observada:** `11/11` suites PASS (`python3 -B`), `41/41` `.py` compilados, Fold 1 OOS (`N=130`, `Net=+26,933`, `PF=1.73`, `MaxDD=4.10%`, `M16=+31,746`), `4/4` `strict_sha=True`, `0` secretos/`.env`, `0` `__pycache__` y ambos cachés WSL intactos (`58` `.pkl` cada uno).

- **Etapa 29 — Actualización Completa de `README.md`, Contexto y Sistema de Agentes Antigravity:**
  - Commit `c6d5690` (`docs(readme)`): Reemplazo integral de [`README.md`](README.md) acorde a `C1_STREAK3_24H_INV` y publicado en `origin/master`.
  - Optimización completa de [`AGENTS.md`](AGENTS.md), [`simulation/AGENTS.md`](simulation/AGENTS.md), [`AGENT_MAINTENANCE_DIRECTIVE.md`](AGENT_MAINTENANCE_DIRECTIVE.md), [`CONTEXT_LNM_SNIPER.md`](CONTEXT_LNM_SNIPER.md), los `4` skills modulares en `.agents/skills/` (`wsl-verify`, `sniper-subagents`, `quant-math-audit`, `surgical-python-edit`) y el script ejecutable [`.agents/skills/wsl-verify/scripts/verify_repo.py`](.agents/skills/wsl-verify/scripts/verify_repo.py) (`d0d86ee`, `2e67c38`).

- **Etapa 30 — Fase 1B (`d0ea3fd`, `feat(core)`): Capa Pura Multi-TF + Super-Pools + TD9 Perfection + Session VWAP O(1):**
  - [`DeterministicMTFAggregator`](liquidity_radar.py) en [`MultiTimeframeCache`](lnm_sniper_v6_advanced.py), [`MTFAggregator`](simulation/sim_market_feed.py) y [`PrecomputedMarketFeed`](simulation/sim_training_environment.py): fuente única `1m` construyendo `15m`, `30m`, `45m`, `1h` y `4h` en RAM mediante cubetas UTC fijas `ts // (tf_min * 60)` sin llamadas extra a la API.
  - Jerarquía de pools (`15m: 1.00`, `30m: 1.20`, `45m: 1.35`, `1h: 1.60`, `4h: 2.00`), fusión `fuse_super_pools` (`15m`/`30m` dentro de `0.25*ATR` de `1h`/`4h`), `TD9PerfectionTracker` causal O(1) en `1m`/`5m`/`15m`, `SessionVWAPAccumulator` O(1) reiniciado a las `00:00 UTC` (`±1σ`, `±2σ`), y hook de trailing estructural `compute_structural_td9_trailing_stop` detrás del flag `structural_td9_trailing_enabled=False`.
  - **Certificación observada:** `11/11` suites PASS (`test_liquidity_radar.py` `13/13` con `P95 = 0.0233 ms < 0.300 ms`), `verify_repo.py --mode oos-fold1` (`N=130`, `Net=+26,933`, `PF=1.73`, `MaxDD=4.10%`, `M16=+31,746 (52)`), y paridad exacta en `M02`/`M12`/`M16`.

- **Etapa 31 — Parseo Exacto de Fees/Funding API v3 camelCase (`5196b85`, `fix(api)`):**
  - `extract_trade_fees_v3` y `extract_trade_funding_sats` en [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) más alineación en [`sniper_perception_sats_core.py`](sniper_perception_sats_core.py) para leer exactamente `openingFee`, `closingFee` y `sumFundingFees` de LN Markets API v3 sin subconteo ni doble descuento, respaldado con cobertura en [`test_c1_streak3_24h_production.py`](test_c1_streak3_24h_production.py).

- **Etapa 32 — Fase 4 Sidecar de Microestructura Binance Futures (`cef38c0`, `10a87c9` + fix `/market/stream`):**
  - `cef38c0` (`feat(sidecar)`): creación de [`binance_micro_sidecar.py`](binance_micro_sidecar.py) (`100%` stdlib RFC 6455 WS + agregador causal `CVD 1m/5m` y liquidaciones `forceOrder` en micro-USD enteros + REST público `openInterest`/`premiumIndex` cada `30s` + snapshot atómico `$HOME/.sniper_state/binance_micro_snapshot.json`), integración `shadow_only` `_build_binance_micro_telemetry` en [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) (aislado de rutas críticas de decisión) y suite offline [`test_binance_micro_sidecar.py`](test_binance_micro_sidecar.py).
  - `10a87c9` (`fix(sidecar)`): elevación de `read_timeout_sec=120.0`, tolerancia a `TimeoutError`/`asyncio.TimeoutError` transitorios en `consume_ws_frames` y fallback `connected=False` en `read_binance_micro_snapshot`.
  - Fix ruta oficial Binance Futures `/market/stream`: actualización de `DEFAULT_WS_URL` a `wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder` (requerido por la arquitectura segmentada `/public` vs `/market` de Binance USD-M Futures para entregar frames `@aggTrade` y `@forceOrder`) + verificación offline en [`test_binance_micro_sidecar.py`](test_binance_micro_sidecar.py) y sincronización de [`README.md`](README.md) y [`CONTEXT_LNM_SNIPER.md`](CONTEXT_LNM_SNIPER.md).

---

## 5. PROTOCOLO DE ARRANQUE PARA EL PRÓXIMO AGENTE

1. Lee [`AGENTS.md`](AGENTS.md) y [`AGENT_MAINTENANCE_DIRECTIVE.md`](AGENT_MAINTENANCE_DIRECTIVE.md); si editas `simulation/`, consulta también [`simulation/AGENTS.md`](simulation/AGENTS.md).
2. Localiza símbolos y líneas exactas usando la Sección 3 (**Mapa AST**) antes de llamar `read_file` (máximo `150` líneas por lectura).
3. Carga bajo demanda el skill que corresponda (`surgical-python-edit`, `quant-math-audit`, `sniper-subagents`, `wsl-verify`).
4. Verifica siempre en WSL mediante:
   - Chequeo rápido tras ediciones puntuales: `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode quick`
   - Certificación completa previa a commit: `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode full`
   - Paridad OOS Fold 1 al tocar `simulation/` o lógica de decisión: `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1`
5. Actualiza las Secciones 1 y 4 de este archivo al concluir cualquier hito nuevo.
