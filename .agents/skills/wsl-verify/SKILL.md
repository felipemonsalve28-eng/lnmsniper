---
name: wsl-verify
description: >-
  Use this skill whenever syntax checks, P0 invariants, SHA-256 strict checks,
  Fold 1 OOS simulator parity, or the 12 sequential test suites must run for LNM
  Sniper inside WSL (Ubuntu).
---

# Verificación y Certificación Hermética en WSL (`Ubuntu`)

Ejecuta siempre las validaciones en **WSL (`Ubuntu`)** sobre `/mnt/c/Users/mberm/OneDrive/Documentos/sniper` usando `python3 -B` y [verify_repo.py](./scripts/verify_repo.py).

## 0. Regla por Defecto: Ahorro de Cuota (`MODO SOLO-EDICIÓN`)
- **`MODO SOLO-EDICIÓN` es el modo operativo por defecto:** salvo instrucción explícita de correr pruebas completas en WSL, ejecuta **únicamente** `python3 -B -m py_compile <archivos_editados>` y entrega al usuario el comando exacto de `verify_repo.py` o runner WSL para ejecutarlo en su terminal.

## 1. Script Canónico de Verificación (`verify_repo.py`)

| Objetivo | Comando |
| :--- | :--- |
| **Chequeo rápido (`py_compile`, `--dry-check`, P0, `100/28`, `strict_sha`, cachés)** | `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode quick` |
| **Certificación completa (`quick` + `12` suites secuenciales + post-check SHA-256 + purga `__pycache__`)** | `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode full` |
| **Paridad directa Fold 1 OOS (`M13..M18`, `N=130`, `Net=+26,933`, `PF=1.729`, `M16=+31,746`)** | `wsl -d Ubuntu -- python3 -B .agents/skills/wsl-verify/scripts/verify_repo.py --mode oos-fold1` |

## 2. Suites Incluidas en `--mode full` (Orden Secuencial Estricto: `12`)
1. `test_anti_whipsaw_hedge.py`
2. `test_binance_micro_sidecar.py`
3. `test_c1_streak3_24h_production.py`
4. `test_candidate_certification.py`
5. `test_learning_quality_and_sats.py`
6. `test_limit_execution_manager.py`
7. `test_liquidity_radar.py`
8. `test_protocolo_v_reversal.py`
9. `test_simulation_engine.py`
10. `test_simulation_training_loop.py`
11. `test_sniper_cli.py`
12. `test_terminal_bellman_fix.py`

## 3. Reglas Críticas de Terminal WSL y Presupuesto de RAM
- **Cero paralelismo de suites o labs pesados:** nunca correr dos suites ni dos corridas `18M` simultáneas sobre el mismo workspace.
- **Streaming `1` `.pkl` a la vez (`< 500 MB` RAM, `0` Swap):** toda corrida `18M` (`run_phase5b_v2_lab.py --mode exit_asym`, futuro `--mode scalping`, `run_phase5_stress.py`) debe iterar bloque a bloque liberando memoria con `del blk; gc.collect()`.
- **Anti-CRLF PowerShell -> WSL:** usar `@' ... '@ | wsl -d Ubuntu -- python3 -B -` o `(@' ... '@).Replace("`r","") | wsl -d Ubuntu -- bash -s`.
- **Integridad de Estado y Cachés:** confirmar `git status --short` sin mutaciones en los `4` JSON de cerebro/cobertura y `58 + 58` `.pkl` intactos en `/home/akapana/.cache/sniper_wf_s2*_cache`.
