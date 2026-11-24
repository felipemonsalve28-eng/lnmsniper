---
name: wsl-verify
description: >-
  Use this skill whenever syntax checks, P0 invariants, SHA-256 strict checks,
  Fold 1 OOS simulator parity, or the 11 sequential test suites must run for LNM
  Sniper inside WSL (Ubuntu).
---

# Verificación y Certificación Hermética en WSL (`Ubuntu`)

Ejecuta siempre las validaciones en **WSL (`Ubuntu`)** sobre `/mnt/c/Users/mberm/OneDrive/Documentos/sniper` usando `python3 -B` y el verificador unificado [verify_repo.py](./scripts/verify_repo.py).

## 1. Script Canónico de Verificación (`verify_repo.py`)

| Objetivo | Comando |
| :--- | :--- |
| **Chequeo rápido (`py_compile`, `--dry-check`, P0, `100/28`, `strict_sha`, cachés)** | `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode quick` |
| **Certificación completa (`quick` + `11` suites secuenciales + post-check SHA-256 + purga `__pycache__`)** | `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode full` |
| **Paridad directa Fold 1 OOS (`M13..M18`, `N=130`, `Net=+26,933`, `PF=1.73`, `M16=+31,746`)** | `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1` |

## 2. Suites Incluidas en `--mode full` (Orden Secuencial Estricto)
1. `test_anti_whipsaw_hedge.py`
2. `test_c1_streak3_24h_production.py`
3. `test_candidate_certification.py`
4. `test_learning_quality_and_sats.py`
5. `test_limit_execution_manager.py`
6. `test_liquidity_radar.py`
7. `test_protocolo_v_reversal.py`
8. `test_simulation_engine.py`
9. `test_simulation_training_loop.py`
10. `test_sniper_cli.py`
11. `test_terminal_bellman_fix.py`

## 3. Reglas Críticas de Terminal WSL
- **Cero paralelismo de suites:** las suites nunca deben correr en paralelo contra el mismo directorio de trabajo.
- **Anti-CRLF PowerShell -> WSL:** si ejecutas scripts ad-hoc desde Windows PowerShell hacia WSL, usa `@' ... '@ | wsl -d Ubuntu -- python3 -B -` o `(@' ... '@).Replace("`r","") | wsl -d Ubuntu -- bash -s` para evitar errores por `\r` en opciones o comandos.
- **Integridad de Estado y Cachés:** tras `--mode full`, confirma que `git status --short` no muestre cambios inesperados en los 4 JSON de estado (`q_brain_advanced_lnm.json`, `q_brain_testnet_lnm.json`, `hedge_state_mainnet.json`, `hedge_state_testnet.json`), ni alteración de los cachés `/home/akapana/.cache/sniper_wf_s2_cache` y `/home/akapana/.cache/sniper_wf_s2_v2_cache` (`58` `.pkl` cada uno).
