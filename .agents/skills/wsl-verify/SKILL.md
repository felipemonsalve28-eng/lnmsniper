---
name: wsl-verify
description: >-
  Use this skill whenever tests, quantitative certification suites, simulation
  checks, or SHA-256 brain/hedge integrity verifications must be executed for
  LNM Sniper inside the WSL (Ubuntu) runtime.
---

# Verificación y Certificación en WSL (`Ubuntu`)

Ejecuta siempre las validaciones desde WSL (`Ubuntu`) sobre `/mnt/c/Users/mberm/OneDrive/Documentos/sniper` para replicar el entorno real de producción.

## 1. Suite Core (Obligatoria tras cambios en trading, guardian o thresholds)

Ejecutar en un único comando secuencial (nunca en paralelo para evitar colisiones de locks/archivos temporales):

```powershell
wsl -d Ubuntu -- bash -c "python3 test_learning_quality_and_sats.py && python3 test_protocolo_v_reversal.py"
```

Criterio de éxito:
- Ambos scripts terminan con `Exit Code: 0`.
- `test_learning_quality_and_sats.py`: `8/8` escenarios aprobados (incluyendo 0 claves huérfanas en `config/learning_thresholds.json`).
- `test_protocolo_v_reversal.py`: `23/23` escenarios cuantitativos y Walk-Forward OOS aprobados.

## 2. Suites de Simulación y CLI (Cuando se modifiquen `simulation/` o `sniper_cli*.py`)

```powershell
wsl -d Ubuntu -- bash -c "python3 -m unittest test_simulation_engine.py test_terminal_bellman_fix.py test_candidate_certification.py test_simulation_training_loop.py && python3 test_sniper_cli.py"
```

## 3. Verificación de Integridad SHA-256 (`strict_sha=True`)

Comprobar que los archivos de cerebro y estado de cobertura en producción coincidan con sus sidecars `.sha256` y que no hayan quedado mutados por pruebas:

```powershell
wsl -d Ubuntu -- bash -c "sha256sum -c <(paste <(cat q_brain_advanced_lnm.json.sha256) <(echo ' q_brain_advanced_lnm.json')) <(paste <(cat hedge_state_mainnet.json.sha256) <(echo ' hedge_state_mainnet.json'))"
```

O verificar en Python directamente con `AtomicBrainStore`:

```powershell
wsl -d Ubuntu -- python3 -c "from guardian_execution_core import AtomicBrainStore; print('MAIN:', AtomicBrainStore('q_brain_advanced_lnm.json').verify_file('q_brain_advanced_lnm.json', strict_sha=True)); print('HEDGE:', AtomicBrainStore('hedge_state_mainnet.json').verify_file('hedge_state_mainnet.json', strict_sha=True))"
```

## 4. Consideraciones Críticas
- Si una prueba modifica temporalmente `q_brain_advanced_lnm.json`, `telemetry_live.json` o `walk_forward_oos_report.json` sin intención de persistirlo, restaurarlos con `git checkout -- <archivos>` desde WSL antes de hacer commit y resincronizar `.sha256` / `.bak` solo si el archivo fue promovido legítimamente.
- Nunca ejecutar pruebas de certificación en paralelo sobre el mismo directorio de trabajo.
