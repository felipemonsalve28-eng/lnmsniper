#!/bin/bash
echo "[1/3] Deteniendo instancia actual de forma segura (SIGTERM)..."
pkill -TERM -f "python3 lnm_sniper_v6_advanced.py"
sleep 3

echo "[2/3] Levantando LNM Sniper v7.3 optimizado en segundo plano..."
nohup python3 lnm_sniper_v6_advanced.py >> sniper_advanced.log 2>&1 &
sleep 2

echo "[3/3] Reinicio exitoso. Mostrando log en vivo (Presiona Ctrl+C para salir del log, el bot seguirá corriendo):"
tail -f sniper_advanced.log
