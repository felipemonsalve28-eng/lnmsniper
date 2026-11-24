---
name: sniper-subagents
description: >-
  Use this skill when a task in LNM Sniper spans multiple modules, large log
  diagnostics, parallel implementation, or independent P0 quantitative auditing
  where Coordinator + specialist subagents save tokens and prevent context bloat.
---

# Orquestación de Subagentes en LNM Sniper

Protocolo Coordinador + Especialistas alineado con `AGENTS.md` y la arquitectura vigente (`C1_STREAK3_24H_INV` + Modo B `LIQ_BIAS_ONLY`).

## 1. Cuándo NO usar subagentes
- Ediciones localizadas en 1 archivo donde ya conoces el rango AST en `CONTEXT_LNM_SNIPER.md` (carga `surgical-python-edit`, edita y ejecuta `wsl-verify`).

## 2. Cuándo SÍ usar subagentes
- Análisis de logs extensos (`sniper_mainnet.log`, `sniper_testnet.log`, `sniper_brain_sync.log`).
- Cambios acoplados entre múltiples módulos (ej. `lnm_sniper_v6_advanced.py` + `simulation/sim_training_environment.py` + `sniper_cli.py`).
- Auditoría matemática o de Reglas P0 previa/posterior a refactorizaciones grandes.

## 3. Especialistas del Repositorio

| Rol | Modo | Mandato Exacto |
| :--- | :--- | :--- |
| **`research`** | Built-in solo lectura | Localiza símbolos/líneas vía `CONTEXT_LNM_SNIPER.md` o `grep`; lee máximo `150` líneas por bloque; resume hallazgos con `archivo:línea`. |
| **`quant-auditor`** | `define_subagent` (`EnableWriteTools: false`) | Aplica los checklists de `.agents/skills/quant-math-audit/SKILL.md`: 9 acciones (`0..8`), 5 segmentos, aislamiento determinista, matemática inversa `1/P`, cero doble fee, `C1_SHORT_EXP_PHYS`, `C1_STREAK3_24H`, Modo B `LIQ_BIAS_ONLY` y esquema `100/12` + `28` runtime. |
| **`implementer`** | `define_subagent` (`EnableWriteTools: true`) | Sigue `.agents/skills/surgical-python-edit/SKILL.md`: ediciones quirúrgicas acotadas preservando comentarios/docstrings y `py_compile`. Si hay frentes paralelos disjuntos, invocar con `Workspace: "share"` y fusionar al principal antes de verificar. |
| **`wsl-verifier`** | `define_subagent` (`EnableWriteTools: true`) | Sigue `.agents/skills/wsl-verify/SKILL.md`: ejecuta `verify_repo.py --mode full` / `oos-fold1` secuencialmente en WSL (`Workspace: "inherit"`), reporta conteos reales, SHA-256 `strict_sha=True`, cachés `58` `.pkl` y `git status --short`. |

## 4. Flujo por Etapas
1. **Etapa 1 (Paralelo opcional):** `research` (+ `quant-auditor` si toca riesgo/matemática/sizing).
2. **Etapa 2:** Consolidar plan con riesgos y solicitar aprobación humana (salvo instrucción *One-Shot* preaprobada).
3. **Etapa 3:** Uno o más `implementer` sobre archivos disjuntos (`Workspace: "share"` si corren en paralelo).
4. **Etapa 4:** `wsl-verifier` único en secuencia (`Workspace: "inherit"`). Nunca dos verifiers en paralelo.
5. **Etapa 5:** Actualizar `CONTEXT_LNM_SNIPER.md` y entregar resumen con enlaces `file://`.
