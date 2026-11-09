#!/bin/bash
# =====================================================================
# setup_dual_sniper.sh - Orquestador Termux para LNM Sniper v7.3+ Dual
# Soporta: setup | start-main | start-test | start-both | sync | status | stop | rotate-logs
# Incluye aislamiento de procesos, rotación automática de logs y parada limpia SIGTERM.
# =====================================================================
set -e

MAIN_BRAIN="q_brain_advanced_lnm.json"
TEST_BRAIN="q_brain_testnet_lnm.json"
MAIN_LOG="sniper_mainnet.log"
TEST_LOG="sniper_testnet.log"
SYNC_LOG="sniper_brain_sync.log"
MAX_LOG_BYTES=5242880

rotate_log_if_needed() {
    local log_file="$1"
    if [ -f "$log_file" ]; then
        local size
        size=$(wc -c < "$log_file" 2>/dev/null || echo 0)
        if [ "$size" -ge "$MAX_LOG_BYTES" ]; then
            [ -f "${log_file}.2" ] && mv -f "${log_file}.2" "${log_file}.3"
            [ -f "${log_file}.1" ] && mv -f "${log_file}.1" "${log_file}.2"
            cp -f "$log_file" "${log_file}.1"
            printf "[LOG ROTATED AT %s]\n" "$(date '+%Y-%m-%d %H:%M:%S')" > "$log_file"
            echo "♻️ Log rotado: $log_file (${size} bytes -> ${log_file}.1)"
        fi
    fi
}

wait_for_graceful_stop() {
    local pattern="$1"
    local max_wait=8
    local count=0
    while pgrep -f "$pattern" >/dev/null 2>&1 && [ "$count" -lt "$max_wait" ]; do
        sleep 0.5
        count=$((count + 1))
    done
}

ensure_deps() {
    echo "[1/4] Verificando dependencias ligeras en Termux..."
    python3 -c "import requests, numpy" 2>/dev/null || pip install --quiet requests numpy
}

ensure_env_template() {
    if [ ! -f ".env" ]; then
        cat << 'EOT' > .env
# Credenciales Producción (Mainnet)
LNM_API_KEY=TU_API_KEY_MAINNET
LNM_API_SECRET=TU_API_SECRET_MAINNET
LNM_PASSPHRASE=TU_PASSPHRASE_MAINNET

# Credenciales Explorador (Testnet)
LNM_TESTNET_API_KEY=TU_API_KEY_TESTNET
LNM_TESTNET_API_SECRET=TU_API_SECRET_TESTNET
LNM_TESTNET_PASSPHRASE=TU_PASSPHRASE_TESTNET
EOT
        chmod 600 .env
        echo "⚠️ Archivo .env creado con plantilla. Edita tus credenciales antes de operar en vivo."
    fi
}

seed_brains() {
    echo "[2/4] Inicializando y verificando cerebros independientes (Mainnet & Testnet)..."
    python3 sync_brains_dual.py || true
}

start_mainnet() {
    pkill -TERM -f "lnm_sniper_v6_advanced.py --env mainnet" 2>/dev/null || true
    wait_for_graceful_stop "lnm_sniper_v6_advanced.py --env mainnet"
    rotate_log_if_needed "$MAIN_LOG"
    echo "🚀 Iniciando Bot MAINNET (Máx 5 pos | Protecciones P0 estrictas | API v3)..."
    SNIPER_LOG_FILE="$MAIN_LOG" nohup python3 -u lnm_sniper_v6_advanced.py --env mainnet >> "$MAIN_LOG" 2>&1 &
    sleep 1
}

start_testnet() {
    pkill -TERM -f "lnm_sniper_v6_advanced.py --env testnet" 2>/dev/null || true
    wait_for_graceful_stop "lnm_sniper_v6_advanced.py --env testnet"
    rotate_log_if_needed "$TEST_LOG"
    echo "🧪 Iniciando Bot TESTNET Explorador Libre (Máx 10 pos | Alta Exploración | API v3)..."
    SNIPER_LOG_FILE="$TEST_LOG" nohup python3 -u lnm_sniper_v6_advanced.py --env testnet >> "$TEST_LOG" 2>&1 &
    sleep 1
}

start_sync_daemon() {
    pkill -TERM -f "sniper_sync_loop" 2>/dev/null || true
    wait_for_graceful_stop "sniper_sync_loop"
    rotate_log_if_needed "$SYNC_LOG"
    echo "🔄 Iniciando Demonio de Nutrición Atómica (cada 30 min)..."
    nohup bash -c 'exec -a sniper_sync_loop bash -c "while true; do sleep 1800; python3 sync_brains_dual.py >> '"$SYNC_LOG"' 2>&1; done"' >/dev/null 2>&1 &
}

show_status() {
    echo "===================================================================="
    echo "📊 ESTADO DE PROCESOS LNM SNIPER v7.3+ DUAL (TERMUX)"
    echo "===================================================================="
    pgrep -af "lnm_sniper_v6_advanced.py" || echo "• Ningún bot corriendo actualmente."
    pgrep -af "sniper_sync_loop" || echo "• Loop de nutrición inactivo."
    echo "--------------------------------------------------------------------"
    echo "• Log Mainnet: tail -f $MAIN_LOG"
    echo "• Log Testnet: tail -f $TEST_LOG"
    echo "• Log Fusión : tail -f $SYNC_LOG"
    echo "===================================================================="
}

MODE="${1:-setup}"

case "$MODE" in
    setup)
        ensure_deps
        ensure_env_template
        seed_brains
        echo "[3/4] Ejecutando protocolo de auditoría cuantitativa..."
        python3 test_protocolo_v_reversal.py
        echo "[4/4] Entorno listo. Usa './setup_dual_sniper.sh start-both' para arrancar ambos bots."
        ;;
    start-main)
        ensure_deps
        start_mainnet
        show_status
        ;;
    start-test)
        ensure_deps
        seed_brains
        start_testnet
        show_status
        ;;
    start-both)
        ensure_deps
        seed_brains
        start_mainnet
        start_testnet
        start_sync_daemon
        show_status
        ;;
    sync)
        python3 sync_brains_dual.py
        ;;
    rotate-logs)
        rotate_log_if_needed "$MAIN_LOG"
        rotate_log_if_needed "$TEST_LOG"
        rotate_log_if_needed "$SYNC_LOG"
        ;;
    stop)
        echo "🛑 Deteniendo bots y demonio de sincronización con SIGTERM..."
        pkill -TERM -f "lnm_sniper_v6_advanced.py" 2>/dev/null || true
        pkill -TERM -f "sniper_sync_loop" 2>/dev/null || true
        wait_for_graceful_stop "lnm_sniper_v6_advanced.py"
        show_status
        ;;
    status)
        show_status
        ;;
    *)
        echo "Uso: ./setup_dual_sniper.sh {setup|start-main|start-test|start-both|sync|rotate-logs|status|stop}"
        ;;
esac
chmod +x setup_dual_sniper.sh
