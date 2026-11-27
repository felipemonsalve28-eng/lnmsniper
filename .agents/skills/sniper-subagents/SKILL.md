---
name: sniper-subagents
description: >-
  Use this skill ONLY when a task in LNM Sniper spans 3+ coupled modules,
  massive log forensics, or independent P0 quantitative auditing where
  Coordinator + specialist subagents truly save context.
---

# Orquestación de Subagentes en LNM Sniper

## 1. Cuándo NO usar subagentes (Ahorro Estricto de Cuota)
- En **`MODO SOLO-EDICIÓN`** (prohibido lanzar subagentes).
- En tareas de `1` o `2` archivos con rangos AST conocidos en `CONTEXT_LNM_SNIPER.md` (edita directo con `surgical-python-edit`).

## 2. Cuándo SÍ usar subagentes
- Forense de logs extensos (`logs/*.jsonl`, `sniper_testnet.log`, `sniper_mainnet.log`).
- Cambios acoplados en `>= 3` módulos (ej. `lnm_sniper_v6_advanced.py` + `simulation/sim_training_environment.py` + `sniper_cli.py`).
- Auditoría cuantitativa P0 independiente previa a promover Fase 6 o Fase 7.

## 3. Especialistas del Repositorio

| Rol | Modo | Mandato Exacto |
| :--- | :--- | :--- |
| **`research`** | Built-in solo lectura | Localiza vía `CONTEXT_LNM_SNIPER.md` o `grep`; máximo `150L`/lectura; reporta `archivo:línea`. |
| **`quant-auditor`** | `define_subagent` (`EnableWriteTools: false`) | Aplica `.agents/skills/quant-math-audit/SKILL.md`: 9 acciones, aislamiento determinista, matemática inversa exacta $1 - P_0/P_1$, Fases 5–7, Modo B y esquema `100/12 + 28`. |
| **`implementer`** | `define_subagent` (`EnableWriteTools: true`) | Aplica `.agents/skills/surgical-python-edit/SKILL.md`: edición quirúrgica + `py_compile`. Usa `Workspace: "share"` si corren frentes disjuntos en paralelo. |
| **`wsl-verifier`** | `define_subagent` (`EnableWriteTools: true`) | Aplica `.agents/skills/wsl-verify/SKILL.md`: `verify_repo.py --mode full` (`12` suites secuenciales) / `oos-fold1` con `Workspace: "inherit"`. Nunca dos en paralelo. |
