---
name: surgical-python-edit
description: >-
  Use this skill before editing any Python, shell, or JSON config file in LNM
  Sniper to locate targets via AST/grep, preserve stability/syntax, enforce Termux
  portability, and produce clean atomic git commits.
---

# Protocolo de Edición Quirúrgica y Estabilidad de Código

Aplica este flujo en toda modificación sobre código fuente o configuración.

## 1. Localización Mínima Previa (Cero Desperdicio de Contexto)
1. Consulta primero el **Mapa AST** en `CONTEXT_LNM_SNIPER.md` (Sección 3) o localiza símbolos con `grep`/`rg -n`.
2. Llama `read_file` únicamente sobre el bloque necesario (`StartLine`..`EndLine`, idealmente `<= 150` líneas). Nunca cargues completo `lnm_sniper_v6_advanced.py` (`4806L`).
3. Preserva docstrings, comentarios existentes y contratos de retorno P0 (`MultiTimeframeCache.get_candles()` y `_ensure_raw_candles()` -> 3-tupla `(h, l, c)`).

## 2. Estabilidad Sintáctica, Atomicidad y Portabilidad Termux
- **Dependencias:** Prohibido añadir paquetes C/C++ pesados; usar exclusivamente stdlib + `requests` + `numpy`.
- **Rutas y I/O:** Prohibido hardcodear rutas `C:\`, `/mnt/c/`, `/sdcard` o `/storage/emulated` en lógica productiva. Todo estado runtime móvil va en `$HOME/.sniper_state/` o en el directorio de trabajo mediante rutas relativas.
- **Persistencia atómica:** Toda escritura en archivos JSON de estado usa `FileLockContext` (`fcntl`), archivo temporal único por llamada (`tmp` + `os.fsync` + `os.replace`) y limpieza `finally:` de temporales, preservando sidecar `.sha256` cuando corresponda.
- **Camino crítico `5s` y señales:** Cero I/O bloqueante dentro de evaluación por tick; respetar `TokenBucket(20/s, burst 40)` y manejadores de señal lock-free.

## 3. Verificación Sintáctica y Limpieza Inmediata
1. Tras editar archivos `.py`, compila de inmediato sin generar bytecode residual (`cfile` temporal o `-B`):
   `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode quick`
2. Antes de reportar o commitear, corre la batería completa:
   `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode full`
3. Asegura `0` directorios `__pycache__` / `.pyc` y `0` archivos trackeados sucios por pruebas (`git status --short`).

## 4. Commits Atómicos Semánticos en WSL
- Ejecuta `git` en WSL (`wsl -d Ubuntu -- ...`).
- Stagea explícitamente cada ruta (`git add -- archivo1 archivo2`). **Prohibido `git add -A`**.
- Revisa `git diff --cached --name-only` antes de `git commit -m "<tipo>(<ámbito>): <resumen>"`.
- Nunca añadas archivos runtime gitignored (`telemetry_live*.json`, `side_brake_state_*.json*`, `.env`) a `git add`.
