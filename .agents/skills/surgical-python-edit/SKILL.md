---
name: surgical-python-edit
description: >-
  Use this skill before editing any Python, shell, or JSON config file in LNM
  Sniper to enforce strict AST read budgets, syntax stability, Termux
  portability, zero-quota delegation, and clean atomic git commits.
---

# Protocolo de Edición Quirúrgica y Ahorro de Cuota (`v7.4+`)

## 1. Localización Mínima y Presupuesto Estricto de Lectura
1. Consulta primero el **Mapa AST** en `CONTEXT_LNM_SNIPER.md` (Sección 3) o localiza con `grep`/`rg -n`.
2. Lee **únicamente** los archivos autorizados en el prompt en rangos `<= 150` líneas (máximo `6` lecturas antes de editar). Nunca cargues completo `lnm_sniper_v6_advanced.py` (`5199L`) ni inspecciones módulos no solicitados.
3. Preserva docstrings, comentarios existentes y contratos P0 (`get_candles()` y `_ensure_raw_candles()` -> 3-tupla `(h, l, c)`).
4. **Propiedad exclusiva de archivos (Hub-and-Spoke):** Edita solo los archivos del frente asignado; no toques cambios previos en el working tree fuera de tu alcance.

## 2. Estabilidad Sintáctica, Streaming WSL y Portabilidad Termux
- **Dependencias:** Solo Python stdlib + `requests` + `numpy`.
- **Rutas y I/O:** Prohibido hardcodear `C:\`, `/mnt/c/`, `/sdcard` o `/storage/emulated` en código productivo. Estado móvil bajo `$HOME/.sniper_state/` o rutas relativas.
- **Persistencia atómica:** `FileLockContext` (`fcntl`), temporal único (`tmp + os.fsync + os.replace`) con `finally:` de limpieza y `.sha256` cuando corresponda.
- **Camino crítico `5s`:** Cero I/O bloqueante o HTTP extra por tick; respetar `TokenBucket(20/s, burst 40)` y señales lock-free; `micro_flow_guard.py` siempre fail-open $O(1)$.
- **Streaming obligatorio en `simulation/` (`1` `.pkl` a la vez):** Todo runner `18M` (`58` bloques) debe cargar 1 bloque por iteración y ejecutar `del blk; gc.collect()` (`< 500 MB` RAM, `0` Swap).

## 3. Verificación Sintáctica y `MODO SOLO-EDICIÓN` (Por Defecto)
1. Tras cada edición corre inmediatamente:
   `wsl -d Ubuntu -- python3 -B -m py_compile <archivos_editados>`
2. **`MODO SOLO-EDICIÓN` es el modo por defecto** para ahorro de cuota: detente tras `py_compile`, entrega diff resumido (`archivo:líneas`) y lista los comandos exactos de `verify_repo.py` para que el usuario los ejecute en su terminal WSL.
3. Solo si el usuario solicita validación completa explícita, ejecuta `verify_repo.py --mode full` (`12` suites) y `oos-fold1` (si toca decisión o `simulation/`), garantizando `0` `__pycache__` y `git status --short` limpio en JSON trackeados.

## 4. Commits Atómicos Semánticos en WSL
- Stagea solo rutas explícitas (`git add -- archivo1 archivo2`). **Prohibido `git add -A`**.
- Jamás añadir `telemetry_live*.json`, `logs/*.jsonl`, `side_brake_state_*.json*` ni `.env`.
