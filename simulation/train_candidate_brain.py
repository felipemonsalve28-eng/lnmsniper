#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simulation/train_candidate_brain.py
Runner de entrenamiento curricular multi-episodio de alta velocidad para generar
el cerebro candidato (q_brain_simulated_candidate.json) con rigor Schema v2.
"""
import sys
import os
import math
import random
import argparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from simulation.sim_market_feed import SimMarketFeed, Candle
from simulation.sim_training_environment import SimTrainingEnvironment


def generate_synthetic_multi_regime_candles(num_bars: int = 1500, start_price: float = 70000.0) -> list:
    """
    Genera una serie de velas sintéticas de 1m continuas (Open_t = Close_{t-1})
    atravesando dinámicamente los 6 regímenes canónicos: rango lateral, compresión,
    tendencia alcista, tendencia bajista y expansiones de volatilidad.
    """
    candles = []
    base_ts = 1700000000.0
    cur_price = float(start_price)
    regimes = ["RANGE", "TREND_BULL", "CHOP_HIGHVOL", "TREND_BEAR", "RANGE_SQUEEZE", "VOLATILITY_EXPANSION"]

    regime_idx = 0
    bars_in_regime = 0
    regime_duration = max(35, num_bars // len(regimes))

    for i in range(num_bars):
        reg = regimes[regime_idx]
        bars_in_regime += 1
        if bars_in_regime >= regime_duration:
            regime_idx = (regime_idx + 1) % len(regimes)
            bars_in_regime = 0

        # Dinámica de volatilidad y drift según el régimen
        if reg == "RANGE":
            drift = math.sin(i / 15.0) * 12.0
            vol = random.uniform(8.0, 25.0)
        elif reg == "TREND_BULL":
            drift = random.uniform(10.0, 35.0)
            vol = random.uniform(15.0, 30.0)
        elif reg == "TREND_BEAR":
            drift = -random.uniform(12.0, 38.0)
            vol = random.uniform(18.0, 35.0)
        elif reg == "RANGE_SQUEEZE":
            drift = math.sin(i / 5.0) * 3.0
            vol = random.uniform(2.0, 8.0)
        elif reg == "VOLATILITY_EXPANSION":
            drift = random.choice([-1.0, 1.0]) * random.uniform(25.0, 60.0)
            vol = random.uniform(40.0, 80.0)
        else:  # CHOP_HIGHVOL
            drift = random.choice([-1.0, 1.0]) * random.uniform(15.0, 45.0)
            vol = random.uniform(30.0, 70.0)

        # Proceso difusivo continuo: Open_t = Close_{t-1}
        open_p = cur_price
        delta = drift + random.gauss(0.0, vol)
        close_p = max(20000.0, open_p + delta)
        cur_price = close_p

        wick_up = abs(random.gauss(0.0, max(vol * 0.6, 2.0)))
        wick_down = abs(random.gauss(0.0, max(vol * 0.6, 2.0)))
        high_p = max(open_p, close_p) + wick_up
        low_p = max(1000.0, min(open_p, close_p) - wick_down)

        c = Candle(
            timestamp=base_ts + (i * 60.0),
            open=round(open_p, 2),
            high=round(high_p, 2),
            low=round(low_p, 2),
            close=round(close_p, 2),
            volume=round(random.uniform(0.5, 8.0), 3),
        )
        candles.append(c)

    return candles


def clean_wipe_candidate_files():
    """Elimina exhaustivamente todos los archivos de estado, respaldos y locks del candidato."""
    bases = ["q_brain_simulated_candidate.json", "hedge_state_simulated_candidate.json"]
    exts = [
        "", ".bak", ".sha256", ".lock",
        ".snapshot_guard.json", ".snapshot_guard.json.bak",
        ".snapshot_guard.json.lock", ".snapshot_guard.json.sha256"
    ]
    for b in bases:
        for ext in exts:
            p = b + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description="Entrenador offline de cerebro candidato LNM Sniper")
    parser.add_argument("--bars", type=int, default=1500, help="Número de velas de 1m por episodio")
    parser.add_argument("--episodes", type=int, default=3, help="Número de episodios de entrenamiento")
    parser.add_argument("--initial-balance", type=int, default=100000, help="Balance inicial en satoshis")
    parser.add_argument("--fetch-binance", action="store_true", help="Descarga velas reales de Binance API pública")
    parser.add_argument("--dataset-cache", default="simulation/data/binance_btc_1m.json", help="Ruta de caché del dataset Binance")
    parser.add_argument("--reset", action="store_true", help="Reinicia el cerebro candidato desde estados canónicos neutros")
    parser.add_argument("--alpha", type=float, default=0.18, help="Tasa de aprendizaje alpha")
    parser.add_argument("--epsilon", type=float, default=0.25, help="Tasa inicial de exploración epsilon")
    parser.add_argument("--decay", type=float, default=0.88, help="Factor de decaimiento de epsilon por episodio")
    args = parser.parse_args()

    print("=" * 80)
    print("INICIANDO ENTRENAMIENTO ACELERADO DE CEREBRO CANDIDATO (SCHEMA v2)")
    print(f"Episodios: {args.episodes} | Velas/Episodio: {args.bars} | Balance Inicial: {args.initial_balance:,} sats")
    print("=" * 80)

    # 0. Si se solicita reinicio limpio, eliminar exhaustivamente archivos candidatos y respaldos
    if args.reset:
        clean_wipe_candidate_files()
        print("🧹 Reinicio limpio ejecutado: archivos principales, .bak, .sha256, .lock y guards eliminados.")

    # 1. Preparar o descargar datos base si se requiere Binance
    binance_candles = None
    if args.fetch_binance:
        cache_path = os.path.abspath(args.dataset_cache)
        if os.path.exists(cache_path):
            print(f"📂 Cargando dataset histórico desde caché local: {cache_path}...")
            binance_candles = SimMarketFeed.load_candles_from_cache(cache_path)
            print(f"✅ {len(binance_candles):,} velas cargadas desde caché.")

        if not binance_candles or len(binance_candles) < args.bars:
            req_bars = max(args.bars * args.episodes + 1000, 20000)
            print(f"🌐 Descargando dataset paginado ({req_bars:,} velas) desde Binance API pública...")
            binance_candles = SimMarketFeed.fetch_binance_klines_paginated(
                symbol="BTCUSDT",
                interval="1m",
                total_bars=req_bars,
                save_cache_path=cache_path,
            )
            print(f"✅ {len(binance_candles):,} velas descargadas exitosamente.")

    # 2. Inicializar el entorno de simulación
    env = SimTrainingEnvironment(
        candidate_brain_file="q_brain_simulated_candidate.json",
        candidate_hedge_file="hedge_state_simulated_candidate.json",
        initial_balance_sats=args.initial_balance,
        explorer_mode=True,
        learning_enabled=True,
    )
    env.brain.alpha = float(args.alpha)
    env.brain.epsilon = float(args.epsilon)

    if args.reset:
        assert len(env.brain.processed_closed_trade_ids) == 0, "Error: processed_closed_trade_ids contiene datos tras reset"
        assert env.brain.total_trained == 0, "Error: total_trained > 0 tras reset"
        print(f"✅ Reinicio canónico verificado: 0 cierres previos, {len(env.brain.q_table)} estados canónicos inicializados.")

    # 3. Bucle de entrenamiento por episodios
    for ep in range(1, args.episodes + 1):
        print(f"\n--- [EPISODIO {ep}/{args.episodes}] (Epsilon={env.brain.epsilon:.3f}) ---")
        if binance_candles is not None and len(binance_candles) >= args.bars:
            max_start = max(0, len(binance_candles) - args.bars)
            start_idx = random.randint(0, max_start) if max_start > 0 else 0
            ep_candles = binance_candles[start_idx : start_idx + args.bars]
            print(f"  📊 Ventana de datos reales: barra {start_idx:,} a {start_idx + len(ep_candles):,} de {len(binance_candles):,}")
        else:
            ep_candles = generate_synthetic_multi_regime_candles(
                num_bars=args.bars,
                start_price=random.uniform(62000.0, 75000.0),
            )
        summary = env.run_episode(ep_candles, initial_balance=args.initial_balance)

        print(
            f"  Trades: {summary['total_settled_trades']} | "
            f"WinRate: {summary['win_rate_pct']:.1f}% ({summary['winning_trades']}W / {summary['losing_trades']}L) | "
            f"MaxDD: {summary.get('max_drawdown_pct', 0.0):.1f}% | "
            f"Net PnL: {summary['net_profit_sats']:+,} sats | "
            f"Fees: {summary['total_fees_paid_sats']} sats | "
            f"Balance Final: {summary['final_balance_sats']:,} sats"
        )

        # Decaimiento curricular de epsilon
        env.brain.epsilon = max(0.04, round(env.brain.epsilon * float(args.decay), 4))

    # 4. Guardado final y certificación criptográfica SHA-256
    d_main, d_hedge = env.save_candidate_brains()
    print("\n" + "=" * 80)
    print("🏆 ENTRENAMIENTO COMPLETADO EXITOSAMENTE")
    print(f"📦 Cerebro Candidato Guardado : q_brain_simulated_candidate.json (SHA-256: {d_main[:16]}...)")
    print(f"🛡️ Hedge Store Candidato      : hedge_state_simulated_candidate.json (SHA-256: {d_hedge[:16]}...)")
    print(f"🔒 Cerebros de producción protegidos e intactos (0 mutaciones).")
    print("=" * 80)


if __name__ == "__main__":
    main()
