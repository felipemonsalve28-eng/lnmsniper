# AGENTS.md — Orquestación y Gobernanza LNM Sniper

Este repositorio opera sobre **WSL (`Ubuntu`)** en `/mnt/c/Users/mberm/OneDrive/Documentos/sniper`.

## 1. Gobernanza Obligatoria
- Lee [AGENT_MAINTENANCE_DIRECTIVE.md](./AGENT_MAINTENANCE_DIRECTIVE.md) y [CONTEXT_LNM_SNIPER.md](./CONTEXT_LNM_SNIPER.md) antes de cambios estructurales.
- Trabaja por etapas secuenciales (Etapa 1 Diagnóstico -> Etapa 2 Plan -> Etapa 3 Código -> Etapa 4 Pruebas -> Etapa 5 Contexto) esperando aprobación humana entre etapas, **salvo** cuando el usuario entregue explícitamente una directiva *One-Shot* preaprobada.
- En la terminal, ejecuta `python3` y operaciones `git` de escritura (`pull`, `commit`, `checkout`) dentro de **WSL (`Ubuntu`)** (`wsl -d Ubuntu -- ...`).

## 2. Reglas de Oro (P0 — Intocables sin autorización explícita)
1. **Q-Table y Acciones:** Mantener estrictamente las 9 acciones discretas (`0..8`) y el formato de 5 segmentos `REGIME|OSC|CONC|MACRO|EMA`.
2. **Contabilidad Real en Satoshis:** Cero doble descuento de comisiones/funding cuando `real_balance_delta_sats` o `pl_includes_fees=True` estén presentes; las pérdidas netas deben reducir $Q(s, a)$.
3. **Subordinación P0:** Preservar la jerarquía de `HardFloorGuardian` (`guardian_execution_core.py`) y el límite máximo de 5 posiciones concurrentes en `mainnet`.
4. **Umbrales y Retrocompatibilidad:**
   - No añadir claves huérfanas ni sin esquema en `config/learning_thresholds.json`.
   - `MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` deben seguir retornando una 3-tupla `(h, l, c)`.
5. **Integridad Criptográfica:** Respetar `AtomicBrainStore` con `strict_sha=True` en `q_brain_advanced_lnm.json`, `q_brain_testnet_lnm.json`, `hedge_state_mainnet.json` y `hedge_state_testnet.json`.

## 3. Arquitectura de Subagentes (Coordinador + Especialistas)
Para tareas medianas o complejas, el agente principal actúa como **Coordinador** (mantiene su contexto ligero y no lee archivos completos innecesariamente) y delega en especialistas:

- **`research` (Solo lectura):** Explora código por búsqueda (`grep`/`Select-String`), lee rangos acotados (<= 150 líneas) y analiza logs (`sniper_mainnet.log`, `sniper_testnet.log`, `telemetry_live*.json`).
- **`quant-auditor` (Solo lectura):** Verifica el cumplimiento de las Reglas de Oro P0, invariantes matemáticos y compatibilidad con `learning_thresholds.json` antes y después de editar.
- **`implementer` (Escritura):** Realiza ediciones quirúrgicas en módulos asignados. Si hay múltiples frentes independientes (ej. producción vs. `simulation/`), usar `Workspace: "share"` para aislar cambios.
- **`wsl-verifier` (Ejecución en WSL):** Corre la batería de tests en WSL (`Ubuntu`) y valida la integridad SHA-256 de los cerebros.

Usa el skill `sniper-subagents` cuando necesites orquestar varios roles en paralelo, y el skill `wsl-verify` para ejecutar y certificar las pruebas en WSL.

## 4. Seguridad de Concurrencia y Lectura Económica
- **Prohibido** ejecutar en paralelo dos scripts o tests que escriban sobre los mismos archivos `q_brain_*.json`, `hedge_state_*.json` o `telemetry_live*.json`. Las suites que tocan estado deben correr secuencialmente o en directorios temporales aislados.
- **No** editar código sin antes localizar funciones/líneas exactas y leer solo el bloque relevante.
