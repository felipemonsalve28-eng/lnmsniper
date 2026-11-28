---
name: sniper-subagents
description: >-
  Use this skill ONLY when a task in LNM Sniper spans 3+ coupled modules,
  massive log forensics, or independent P0 quantitative auditing where
  Coordinator + specialist Hub-and-Spoke subagents with exclusive file
  ownership truly save context.
---

# Orquestación Hub-and-Spoke de Subagentes en LNM Sniper (`v7.4+`)

## 1. Regla por Defecto: Ahorro Estricto de Cuota
- **`MODO SOLO-EDICIÓN` es el modo por defecto:** prohibido lanzar subagentes en `MODO SOLO-EDICIÓN` o en tareas de `1`–`2` archivos con rangos AST conocidos en `CONTEXT_LNM_SNIPER.md` (edita directo con `surgical-python-edit`).
- Usar subagentes **únicamente** en forense extenso (`logs/*.jsonl`), cambios acoplados en `>= 3` módulos de carriles distintos o auditoría cuantitativa P0 independiente previa a promover una fase.

## 2. Patrón Hub-and-Spoke con Propiedad Exclusiva de Archivos (Cero Colisiones)
- **Coordinator (`Hub`):** Único dueño de gobernanza (`AGENTS.md`, `ROADMAP_LNM_SNIPER.md`, `CONTEXT_LNM_SNIPER.md`, `AGENT_MAINTENANCE_DIRECTIVE.md`), arbitraje de contratos y consolidación. Jamás asigna el mismo archivo a dos subagentes simultáneos.

| Perfil (`Spoke`) | Modo / Workspace | Propiedad Exclusiva de Archivos (Cero Colisiones) | Mandato Técnico |
| :--- | :--- | :--- | :--- |
| **1. Carril A & Simulador Base** | `EnableWriteTools: true`, `Workspace: "share"` | `simulation/sim_training_environment.py`, `simulation/run_phase5b_v2_lab.py` | Paridad `1:1` `C1_STREAK3_24H_INV` + Modo B, `exit_asym` (`forced_edge_loss_only`) y runner `18M` streaming `1` `.pkl`. |
| **2. Carril B & Ejecución Pasiva** | `EnableWriteTools: true`, `Workspace: "share"` | `simulation/scalp_sim_engine.py`, `liquidity_radar.py`, `limit_execution_manager.py`, `simulation/lane_budget.py` | Motor causal Scalp (`\$500–\$1,000`, LIMIT `~16–18 bps`), `DEF_SCALP_LOCK` y aislamiento `LaneBudgetController`. |
| **3. Microestructura & Telemetría** | `EnableWriteTools: true`, `Workspace: "share"` | `binance_micro_sidecar.py`, `micro_flow_guard.py`, `tools/replay_micro_tape.py` | Cinta `1m` rotada (`P95 < 0.300 ms`), `micro_flow_guard` fail-open $O(1)$ (`age_sec > 5.0s -> ALLOW`) y calibración offline. |
| **4. Verificación & QA** | `EnableWriteTools: true`, `Workspace: "inherit"` (secuencial) | `test_*.py`, `.agents/skills/wsl-verify/` | Suites unitarias duales, `verify_repo.py --mode full` (`12` suites) y `--mode oos-fold1`. Nunca dos en paralelo. |

- **Auxiliares de solo lectura (`EnableWriteTools: false`):**
  - **`research`** (built-in): localización vía `CONTEXT_LNM_SNIPER.md` o `rg -n`; máximo `150L`/lectura; reporta `archivo:línea`.
  - **`quant-auditor`** (`define_subagent`): aplica `.agents/skills/quant-math-audit/SKILL.md` sin escribir archivos.

## 3. Reglas Obligatorias para Todos los Spokes
1. **Streaming `1` `.pkl` a la vez en WSL:** Todo script/evaluación `18M` debe procesar un `.pkl` por iteración con `del blk; gc.collect()`, consumo `< 500 MB` RAM y `0` Swap.
2. **Memoria Anti-Retrabajo (`18M`, `S1 @ 16 bps`):** Prohibido reabrir `SL=1.15%` (`CD=24h`/`8h`), `BASE 0.65% | CD=8h`, recorte `RR` (`2.7`/`2.3`) o quitar BE/Trailing en Trend, `V2_PB_1H` (`PB_BASE`/`PB_EMA50`) y Fase 5B vieja (`B1`/`B2`).
3. **Escalado limpio al Hub:** Si un Spoke requiere modificar un archivo fuera de su propiedad exclusiva, se detiene y reporta el contrato necesario al Coordinator (`Hub`) en lugar de editarlo.
4. **Protocolo `py_compile` + `MODO SOLO-EDICIÓN`:** En modo solo-edición cada Spoke corre únicamente `python3 -B -m py_compile` sobre sus propios archivos tocados.
