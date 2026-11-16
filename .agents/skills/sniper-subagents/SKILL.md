---
name: sniper-subagents
description: >-
  Use this skill when a task in LNM Sniper spans multiple modules, log analysis,
  P0 quantitative auditing, parallel implementation, or multi-stage delivery
  where orchestrating specialized subagents saves tokens and prevents context bloat.
---

# Orquestación de Subagentes para LNM Sniper

Usa este protocolo para mantener el contexto del Coordinador limpio y delegar exploración, auditoría P0, implementación y verificación en WSL.

## 1. Cuándo NO usar subagentes
- Cambios triviales de 1 archivo donde ya conoces el rango de líneas exacto (edita directamente y usa `wsl-verify`).

## 2. Cuándo SÍ usar subagentes
- Diagnóstico de logs grandes (`sniper_mainnet.log`, `sniper_testnet.log`).
- Cambios que afectan múltiples módulos (`lnm_sniper_v6_advanced.py` + `simulation/` + `sniper_cli.py`).
- Auditoría matemática o de Reglas de Oro P0 previa a la entrega.

## 3. Definición de Roles (`define_subagent`)

### A. `quant-auditor` (Solo lectura)
- `EnableWriteTools: false`
- **SystemPrompt:** Audita cambios o planes contra `AGENT_MAINTENANCE_DIRECTIVE.md`: 9 acciones (`0..8`), clave `REGIME|OSC|CONC|MACRO|EMA`, cero doble descuento de comisiones, subordinación a `HardFloorGuardian`, cero claves huérfanas en `config/learning_thresholds.json` y retorno 3-tupla `(h, l, c)` en `MultiTimeframeCache.get_candles()`. Reporta solo hallazgos accionables con archivo y línea.

### B. `implementer` (Escritura quirúrgica)
- `EnableWriteTools: true`
- **SystemPrompt:** Realiza ediciones mínimas por rango de líneas sin leer archivos completos innecesariamente. Preserva comentarios y docstrings existentes. Si trabaja en paralelo con otro `implementer`, invocar con `Workspace: "share"`.

### C. `wsl-verifier` (Ejecución y certificación)
- `EnableWriteTools: true`
- **SystemPrompt:** Ejecuta las suites de pruebas en WSL (`Ubuntu`) siguiendo el skill `wsl-verify`. Nunca corre tests que muten `q_brain_*.json` en paralelo. Reporta exit codes, conteo exacto de escenarios y estado `git status -s` + verificación SHA-256.

## 4. Flujo Recomendado
1. **Etapa 1 (Paralelo):** Lanza `research` (para localizar líneas, símbolos y logs) y, si es un cambio de riesgo, `quant-auditor` sobre el diagnóstico.
2. **Etapa 2:** El Coordinador consolida el plan y solicita aprobación humana (salvo directiva *One-Shot* preaprobada).
3. **Etapa 3:** Lanza uno o más `implementer` (en `Workspace: "share"` si editan archivos distintos en paralelo). Si usas `share`, fusiona sus cambios al workspace principal antes de verificar.
4. **Etapa 4:** Lanza `wsl-verifier` con `Workspace: "inherit"` para correr los tests secuencialmente en WSL y verificar SHA-256.
5. **Etapa 5:** Actualiza `CONTEXT_LNM_SNIPER.md` si corresponde y entrega resumen breve con enlaces `file://`.
