# CONTEXTO OPERATIVO Y MEMORIA PERSISTENTE DEL PROYECTO
## LNM SNIPER v7.3+ (`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`)

> **Memoria Técnica Estructural para Agentes en Antigravity**  
> **Última Actualización:** `2026-11-27`  
> **HEAD Auditado Base:** `89036e0` (`origin/master` + Fase 4B/4B.1 JSONL `shadow_only` desplegado en Termux)  
> **Gobernanza y Hoja de Ruta:** [`AGENTS.md`](AGENTS.md) · [`ROADMAP_LNM_SNIPER.md`](ROADMAP_LNM_SNIPER.md) · [`simulation/AGENTS.md`](simulation/AGENTS.md) · [`AGENT_MAINTENANCE_DIRECTIVE.md`](AGENT_MAINTENANCE_DIRECTIVE.md)

---

## 1. ESTADO ACTUAL DEL SISTEMA (EXECUTIVE SNAPSHOT)

| Componente | Estado Vigente (`2026-11-27`) | Referencia / Evidencia |
| :--- | :--- | :--- |
| **Rama y Remoto** | `master` (`d0ea3fd` Fase 1B -> `5196b85` API v3 camelCase -> `cef38c0`/`10a87c9`/`249ac63` Fase 4 Sidecar `/market/stream` -> `d38f977`/`89036e0` Fase 4B/4B.1 JSONL `OPEN`/`CLOSE` + volúmenes crudos + desglose FER) | [`binance_micro_sidecar.py`](binance_micro_sidecar.py), [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py), [`test_binance_micro_sidecar.py`](test_binance_micro_sidecar.py) |
| **Edición / Política** | **`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`**: política determinista simétrica inmutable en RAM, `E2_FE_OFF`, `C1_SHORT_EXP_PHYS`, freno `C1_STREAK3_24H`, `STRUCTURAL_TD9_TRAILING_ENABLED=False` | [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py), [`sniper_perception_sats_core.py`](sniper_perception_sats_core.py) |
| **Límite de Posiciones** | Máximo **`5`** posiciones concurrentes en `mainnet` y `testnet` (`Config.MAX_CONCURRENT_TRADES = 5`) | [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Guardián P0 & Riesgo** | `HardFloorGuardian` (`75%` -> `88%`), `BASE_RISK_PCT_STD=0.0075`, `RECOVERY_ENTER_DD=0.035`, `DD_THROTTLE_HALT_PCT=0.045`, `MAX_DD_GUARD_PCT=0.052`, `TREND_SL_FLOOR_PCT=0.0065`, `SHORT_EXP_MIN_DSL=0.006505` | [`guardian_execution_core.py`](guardian_execution_core.py), [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Módulos Apagados / Modo B** | `ENABLE_HYBRID_HEDGE=False`, `FE_OFF_TREND=True`, `limit_execution.enabled=0`, `liquidity_radar.enabled=1`, `liquidity_radar.shadow_only=0`, `liquidity_radar.opposite_wall_enabled=0` | [`config/learning_thresholds.json`](config/learning_thresholds.json), [`liquidity_radar.py`](liquidity_radar.py) |
| **Sidecar Binance (`shadow_only`)** | `binance_micro_sidecar.py` (`100%` stdlib RFC 6455 WS `wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder`, `read_timeout_sec=120.0`, REST `30s`, logs `[YYYY-MM-DD HH:MM:SS]`, lectura `P95=0.0056 ms` en Termux, JSONL `logs/binance_micro_events*.jsonl` en `OPEN`/`CLOSE` con 14 campos micro + `opening_fee`/`closing_fee`/`funding`/`net_pnl_sats`) | [`binance_micro_sidecar.py`](binance_micro_sidecar.py), [`test_binance_micro_sidecar.py`](test_binance_micro_sidecar.py) (`9/9`) |
| **API v3 & Cierre Limpio** | `TokenBucket(rate=20.0/s, capacity=40.0)`, `generate_client_id` único en `open_trade()`, parseo camelCase `openingFee`/`closingFee`/`sumFundingFees`, auto-sync reloj `HTTP 401`, `_handle_shutdown_signal` lock-free (`0.25s` + `try/finally`), `--help`/`--dry-check` sin credenciales | [`guardian_execution_core.py`](guardian_execution_core.py), [`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) |
| **Cerebros & Hedge SHA-256** | `strict_sha=True` (`AtomicBrainStore`): [`q_brain_advanced_lnm.json`](q_brain_advanced_lnm.json) (`360` estados), [`q_brain_testnet_lnm.json`](q_brain_testnet_lnm.json) (`360` estados), [`hedge_state_mainnet.json`](hedge_state_mainnet.json), [`hedge_state_testnet.json`](hedge_state_testnet.json) | Certificados en `verify_repo.py --mode full` (`12/12` suites) |
| **Estado Efímero / Termux** | `telemetry_live*.json` y `logs/*.jsonl` gitignored; `side_brake_state_*.json*` y `binance_micro_snapshot.json*` en `$HOME/.sniper_state/` (lecturas $O(1)$ sin I/O bloqueante) | [`.gitignore`](.gitignore), [`side_streak_brake.py`](side_streak_brake.py), [`binance_micro_sidecar.py`](binance_micro_sidecar.py) |
| **Esquema y Catálogo CLI** | **`100` umbrales** en **`12` categorías** (`0` huérfanas), **`28` parámetros runtime** y **`5` presets** (`ENABLE_HYBRID_HEDGE=False` en todos) | [`learning_thresholds_manager.py`](learning_thresholds_manager.py), [`sniper_cli_catalog.py`](sniper_cli_catalog.py) |
| **Cachés OOS WSL** | `/home/akapana/.cache/sniper_wf_s2_cache` (`58` `.pkl`) y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl`) intactos | WSL Ubuntu (`akapana`) |

---

## 2. INVARIANTES MATEMÁTICOS, CONTRATOS Y MÉTRICAS CERTIFICADAS

### A. Contratos Cuantitativos Críticos (P0)
1. **Contrato de Acciones y Estado:** Vector de `9` acciones (`0: HOLD`, `1..4: LONG [TREND, REVERSION, SNIPER, DEF_SCALP]`, `5..8: SHORT [TREND, REVERSION, SNIPER, DEF_SCALP]`) y estado de `5` segmentos `REGIME|OSC|CONC|MACRO|EMA`.
2. **Aislamiento Determinista en Memoria (`DETERMINISTIC_TREND_POLICY = True`):**
   - [`choose_action`](lnm_sniper_v6_advanced.py) y [`evaluate_and_dispatch_entries`](lnm_sniper_v6_advanced.py) evalúan la semilla simétrica limpia inmutable en RAM, ignorando `Q` negativos, `state_visits > 0` y `style_metrics` de cerebros legados en disco.
   - [`compute_live_edge`](lnm_sniper_v6_advanced.py) omite penalizaciones CQL y filtros shadow/expectancy_boost bajo cerebro congelado determinista.
   - [`estimate_bayesian_win_rate`](sniper_perception_sats_core.py) emplea prior simétrico Beta-Binomial $(11.0, 11.0)$ para `TREND` cuando `q_val is not None`, preservando `Test 7a` cuando `q_val is None`.
3. **Matemática Inversa Exacta `BTCUSD` y Contabilidad Real Net-Sats:**
   - PnL bruto inverso exacto: $\Delta_{\text{sats}} = M_{\text{sats}} \cdot L \cdot \left(1 - \frac{P_0}{P_1}\right) \cdot d$, con $d \in \{+1\ (\text{LONG}), -1\ (\text{SHORT})\}$.
   - Recompensa: $R_{\text{sats}} = \Delta \text{Balance} - \lambda(\text{MAE}) - \mu(\Delta t)$, Bellman terminal ($\gamma_{\text{eff}} = 0$) en `_update_q`, desacople flotante (`decouple_floating_pnl == 1`), cero doble descuento de comisiones y reducción obligatoria de $Q(s,a)$ ante pérdida neta.
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

- **Paridad Directa Fold 1 OOS (`verify_repo.py --mode oos-fold1`):**
  `M13: N=14 Net=-3687 DD=3.69%` \| `M14: N=15 Net=+193 DD=3.72%` \| `M15: N=9 Net=-3049 DD=3.53%` \| `M16: N=52 Net=+31746 DD=3.87%` \| `M17: N=22 Net=+2788 DD=4.10%` \| `M18: N=18 Net=-1058 DD=4.02%`.
- **Tarifa Real API v3 (`5196b85` @ `0.16%` RT):** `18M Net = +7,301 sats`, `12M OOS = +23,992 sats`, `MaxDD = 4.62%`, `DDG = 0`.

---

## 3. MAPA QUIRÚRGICO AST DE SÍMBOLOS Y LÍNEAS (`HEAD 89036e0`, LECTURA `<= 150 L`)

Consulta este índice verificado por AST antes de abrir cualquier módulo con `read_file`:

- **[`lnm_sniper_v6_advanced.py`](lnm_sniper_v6_advanced.py) (`5199L`):** `_build_binance_micro_telemetry:L79-125`, `_resolve_binance_micro_events_path:L128-134`, `_evaluate_liquidity_from_mtf:L137-170`, `Config:L176-366`, `load_saved_credentials:L379-428`, `APIHealthMonitor:L434-476`, `MultiTimeframeCache:L482-769`, `calc_ema:L775-785`, `_normalize_ohlc_basis:L788-802`, `compute_indicators:L805-911`, `_build_indicator_prefix_state:L914-1023`, `compute_indicators_from_prefix:L1026-1135`, `AutonomousGoalBrain:L1141-2245`, `_clean_numbers:L2251-2258`, `LNMarketsClient:L2261-2559`, `extract_trade_funding_sats:L2565-2575`, `extract_trade_fees_v3:L2578-2598`, `compute_account_equity:L2601-2610`, `compute_effective_risk:L2613-2625`, `infer_adopted_trade:L2628-2677`, `calculate_strategy_order_params:L2680-2691`, `confirm_and_close_trade:L2694-2714`, `confirm_and_update_stoploss:L2717-2731`, `enforce_concurrency_cap:L2734-2751`, `compute_live_edge:L2754-2866`, `execute_strategy_guardian:L2869-3132`, `evaluate_open_trades_online:L3135-3203`, `compute_situational_order_params:L3206-3311`, `evaluate_system_mode:L3314-3365`, `_is_hybrid_hedge_active:L3368-3374`, `reconcile_portfolio_with_hedges:L3377-3442`, `run_integrated_guardian_and_hedge_cycle:L3445-3551`, `record_shadow_order:L3554-3563`, `update_shadow_trades:L3566-3708`, `process_settlement_queue:L3711-3939`, `manage_open_positions_and_hedges:L3942-3984`, `evaluate_and_dispatch_entries:L3987-4538`, `_parse_cli_env:L4541-4550`, `_print_cli_usage:L4553-4562`, `main:L4565-5195`.
- **[`binance_micro_sidecar.py`](binance_micro_sidecar.py) (`1325L`):** `WSProtocolError:L76-77`, `WSFrame:L80-86`, `_safe_float:L89-94`, `_safe_int:L97-101`, `_format_sidecar_log:L104-108`, `compute_websocket_accept:L111-114`, `_xor_mask:L117-127`, `encode_ws_frame:L130-170`, `build_masked_pong_frame:L173-185`, `build_masked_close_frame:L188-202`, `decode_ws_frame_bytes:L205-258`, `read_ws_frame:L261-290`, `consume_ws_frames:L293-372`, `_parse_ws_endpoint:L375-390`, `perform_ws_handshake:L393-435`, `BinanceMicroAggregator:L438-722`, `resolve_binance_snapshot_path:L725-735`, `write_binance_micro_snapshot:L738-789`, `_empty_snapshot_view:L792-814`, `read_binance_micro_snapshot:L817-902`, `append_binance_micro_trade_event:L905-935`, `fetch_binance_rest_metrics_sync:L954-988`, `BinanceMicroSidecarService:L991-1195`, `_run_dry_check:L1252-1274`, `main:L1277-1321`.
- **[`guardian_execution_core.py`](guardian_execution_core.py) (`733L`):** `FileLockContext:L39-107`, `AtomicBrainStore:L110-217`, `LatencyTelemetry:L220-295`, `InFlightOrderGuard:L298-380`, `HardFloorGuardian:L383-419`, `TokenBucket:L422-449`, `ResilientLNMClient:L452-544`, `rotate_log_file_if_needed:L547-564`, `_CompatNumpy:L673-729`.
- **[`sniper_perception_sats_core.py`](sniper_perception_sats_core.py) (`825L`):** `MarketRegimeClassifier:L16-138`, `ShortTermPerceptionEngine:L141-324`, `SatsExpectancyGate:L327-797`, `sync_brain_floor_with_reality:L800-816`.
- **[`side_streak_brake.py`](side_streak_brake.py) (`345L`):** `SideStreakBrakeStore:L29-345`.
- **[`liquidity_radar.py`](liquidity_radar.py) (`1472L`):** `LiquidityConfig:L41-95`, `LiquidityPool:L99-120`, `SuperPool:L124-134`, `fuse_super_pools:L145-274`, `TD9PerfectionTracker:L277-391`, `SessionVWAPAccumulator:L394-456`, `DeterministicMTFAggregator:L459-707`, `detect_causal_swings:L770-824`, `cluster_swings_to_pools:L827-862`, `filter_unmitigated_pools:L865-903`, `compute_opposite_pool_room:L906-962`, `compute_liquidity_bias:L965-993`, `LiquidityRadar:L996-1418`, `compute_structural_td9_trailing_stop:L1421-1472`.
- **[`limit_execution_manager.py`](limit_execution_manager.py) (`623L`):** `LimitExecutionConfig:L32-66`, `round_to_tick:L69-83`, `should_route_limit:L86-139`, `compute_limit_entry_plan:L142-262`, `LimitExecutionManager:L265-623`.
- **[`learning_thresholds_manager.py`](learning_thresholds_manager.py) (`1227L`):** `resolve_thresholds_path_for_env:L42-54`, `LearningThresholdsManager:L933-1204`, `get_thresholds_manager:L1212-1227`.
- **[`learning_quality_supervisor.py`](learning_quality_supervisor.py) (`1137L`):** `LearningQualitySupervisor:L104-1051`, `AtomicTelemetryDispatcher:L1054-1137`.
- **[`simulation/sim_training_environment.py`](simulation/sim_training_environment.py) (`1478L`):** `PrecomputedMarketFeed:L49-140`, `SimTrainingEnvironment:L143-1478`.
- **[`simulation/sim_market_feed.py`](simulation/sim_market_feed.py) (`401L`):** `Candle:L26-55`, `MTFAggregator:L58-189`, `SimMarketFeed:L192-401`.
- **[`simulation/sim_lnm_exchange.py`](simulation/sim_lnm_exchange.py) (`600L`):** `MicrostructureStressConfig:L22-72`, `SimLNMExchange:L75-600`.
- **[`simulation/run_phase5_stress.py`](simulation/run_phase5_stress.py) (`543L`):** `_parse_scenarios:L30-41`, `_parse_fee_rt_bps:L44-54`, `_atomic_write_json:L57-75`, `_load_months_meta:L78-85`, `_required_pkl_paths:L88-95`, `_run_dry_check:L98-132`, `_load_scope_blocks:L135-161`, `_evaluate_cell:L164-320`, `_evaluate_verdict:L323-399`, `_format_table:L402-460`, `main:L463-539`.

---

## 4. HISTORIAL CONSOLIDADO DE ETAPAS (ETAPAS 1 A 34)

### Resumen Compacto de Etapas Base (1–29)
| Rango | Hito Arquitectónico y Cuantitativo |
| :--- | :--- |
| **Etapas 1–10** | Diagnóstico anti-inflación Q, `learning_thresholds.json` Hot-Reload, fórmula real net-sats, `LearningQualitySupervisor`, remediación `F0->F3` (Bellman terminal), Cluster Alpha/Beta y JSONL streaming. |
| **Etapas 11–25** | Motor `simulation/` completo (`SimLNMExchange`, `PrecomputedMarketFeed`, Monte Carlo `0` mutaciones), remediación `F-01..F-25`, `sniper_cli.py`, aislamiento Mainnet/Testnet, auto-sync reloj `HTTP 401` y purga sintética. |
| **Etapas 26–29** | Paridad simulador, consolidación `C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY` (`20b205d`/`6578e6d`/`876c3c6`), `README.md` (`c6d5690`) y sistema modular de agentes Antigravity + `verify_repo.py` (`d0d86ee`/`2e67c38`). |

### Etapas Recientes (30–34)
- **Etapa 30 — Fase 1B (`d0ea3fd`):** `DeterministicMTFAggregator` en RAM (`1m -> 15m/30m/45m/1h/4h`), `fuse_super_pools`, `TD9PerfectionTracker`, `SessionVWAPAccumulator` y trailing estructural detrás de `structural_td9_trailing_enabled=False`.
- **Etapa 31 — Fees/Funding API v3 camelCase (`5196b85`):** `extract_trade_fees_v3` y `extract_trade_funding_sats` leyendo `openingFee`/`closingFee`/`sumFundingFees` exactos (`0.1595% RT`, `18M Net = +7,301 sats`, `12M OOS = +23,992 sats`).
- **Etapa 32 — Fase 4 Sidecar Binance (`cef38c0`, `10a87c9`, `249ac63`):** `binance_micro_sidecar.py` stdlib RFC 6455 sobre `/market/stream`, agregador causal `CVD 1m/5m` + `forceOrder`, REST `30s`, `read_timeout_sec=120.0`, snapshot atómico en `$HOME/.sniper_state/`.
- **Etapa 33 — Fase 4B / 4B.1 + Optimización Agentes (`d38f977`, `89036e0`):**
  - `d38f977`: `_format_sidecar_log`, `append_binance_micro_trade_event` tolerante a fallos y persistencia JSONL `OPEN`/`CLOSE` en `logs/binance_micro_events*.jsonl`.
  - `89036e0`: adición en `_build_binance_micro_telemetry` y `CLOSE` JSONL de `connected`, `trades_1m`, `buy_vol_1m_usd`, `sell_vol_1m_usd`, `net_pnl_sats`, `opening_fee`, `closing_fee` y `funding` (`9/9` tests en Termux, `P95 = 0.0056 ms`, desplegado en vivo en `sniper_testnet`).
  - Optimización de agentes: `verify_repo.py` ampliado a `12` suites (incluye `test_binance_micro_sidecar.py`), incorporación de `ROADMAP_LNM_SNIPER.md`, `MODO SOLO-EDICIÓN` para ahorro de cuota, corrección dimensional de fórmula inversa y regeneración AST de `HEAD 89036e0`.

- **Etapa 34 — Fase 5 Estrés de Microestructura (`GO` 18M + Fold 1) y Descarte Fase 5B (`NO-GO`):**
  - `simulation/sim_lnm_exchange.py`, `simulation/sim_training_environment.py`, `simulation/run_phase5_stress.py` y `test_simulation_engine.py` (`12/12`): modelo conservador opt-in `MicrostructureStressConfig` (`S0` default neutro con paridad exacta bit a bit, `S1` Termux realista y `S2` pico volatilidad).
  - Matriz `18M` (`58` bloques) certificada **`GO`** en `20 bps` (`S1 OOS 12M = +14,279`, `PF = 1.116`, `MaxDD = 4.34%`, `DDG = 0`, `HFViol = 0`) y `16 bps` (`S1 OOS 12M = +16,477`, `PF = 1.134`, `MaxDD = 4.62%`; `S2 OOS 12M = +6,295`, `PF = 1.050`, `HFViol = 0`). `verify_repo.py --mode full` (`12/12`) y `--mode oos-fold1` PASS exacto.
  - Ablación offline **Fase 5B** (`Dual-Track` Carril B `SNIPER` + $\text{ER}_8$ sin flujo real) cerrada como **`NO-GO`** (`B2` negativo `6/6` meses en Fold 1 @ `16 bps` sin BE: `N=95`, `WR=26.3%`, `Net=-11,209 sats`, `PF=0.488`, `MaxDD=4.98%`; `B1` `4–5` entradas/mes con `0%–20%` WR) y retirada del árbol de trabajo.

---

## 5. PROTOCOLO DE ARRANQUE PARA EL PRÓXIMO AGENTE

1. Lee [`AGENTS.md`](AGENTS.md) y [`ROADMAP_LNM_SNIPER.md`](ROADMAP_LNM_SNIPER.md); si editas `simulation/`, consulta [`simulation/AGENTS.md`](simulation/AGENTS.md).
2. Localiza símbolos en la Sección 3 (**Mapa AST**) antes de llamar `read_file` (`<= 150` líneas, máximo `6` lecturas, solo archivos autorizados).
3. Carga bajo demanda el skill puntual (`surgical-python-edit`, `quant-math-audit`, `wsl-verify`, `sniper-subagents`).
4. Si el usuario indica **`MODO SOLO-EDICIÓN`**, detente tras `python3 -B -m py_compile` sobre los archivos tocados y entrega los comandos WSL al usuario. Si pide validación completa:
   - `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode full` (`12` suites)
   - `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1` (si toca `simulation/` o decisión)
5. Mantén actualizadas las Secciones 1, 3 y 4 al cerrar cada fase.
