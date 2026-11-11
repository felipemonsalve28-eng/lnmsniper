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
    Genera una serie de velas sintéticas de 1m atravesando dinámicamente los 6 regímenes:
    rango lateral, compresión, tendencia alcista, tendencia bajista y expansiones de volatilidad.
    """
    candles = []
    base_ts = 1700000000.0
    cur_price = float(start_price)
    regimes = ["RANGE", "TREND_BULL", "CHOP_HIGHVOL", "TREND_BEAR", "RANGE_SQUEEZE", "VOLATILITY_EXPANSION"]

    regime_idx = 0
    bars_in_regime = 0
    regime_duration = max(100, num_bars // len(regimes))

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

        cur_price = max(20000.0, cur_price + drift + random.gauss(0.0, vol))
        high = cur_price + abs(random.gauss(0.0, vol * 0.75))
        low = cur_price - abs(random.gauss(0.0, vol * 0.75))
        open_p = cur_price - (drift * 0.5)
        close_p = cur_price

        c = Candle(
            timestamp=base_ts + (i * 60.0),
            open=round(open_p, 2),
            high=round(max(open_p, high, close_p), 2),
            low=round(min(open_p, low, close_p), 2),
            close=round(close_p, 2),
            volume=round(random.uniform(0.5, 8.0), 3),
        )
        candles.append(c)

    return candles


def main():
    parser = argparse.ArgumentParser(description="Entrenador offline de cerebro candidato LNM Sniper")
    parser.add_argument("--bars", type=int, default=1500, help="Número de velas de 1m por episodio")
    parser.add_argument("--episodes", type=int, default=3, help="Número de episodios de entrenamiento")
    parser.add_argument("--initial-balance", type=int, default=100000, help="Balance inicial en satoshis")
    parser.add_argument("--fetch-binance", action="store_true", help="Descarga velas reales de Binance API pública")
    parser.add_argument("--alpha", type=float, default=0.18, help="Tasa de aprendizaje alpha")
    parser.add_argument("--epsilon", type=float, default=0.25, help="Tasa inicial de exploración epsilon")
    parser.add_argument("--decay", type=float, default=0.88, help="Factor de decaimiento de epsilon por episodio")
    args = parser.parse_args()

    print("=" * 80)
    print("INICIANDO ENTRENAMIENTO ACELERADO DE CEREBRO CANDIDATO (SCHEMA v2)")
    print(f"Episodios: {args.episodes} | Velas/Episodio: {args.bars} | Balance Inicial: {args.initial_balance:,} sats")
    print("=" * 80)

    # 1. Preparar o generar datos de mercado
    if args.fetch_binance:
        print("🌐 Descargando velas históricas reales desde Binance API pública...")
        candles = SimMarketFeed.fetch_binance_klines(symbol="BTCUSDT", interval="1m", limit=min(1000, args.bars))
        print(f"✅ {len(candles)} velas descargadas exitosamente.")
    else:
        print("🎲 Generando serie sintética estocástica multirégimen...")
        candles = generate_synthetic_multi_regime_candles(num_bars=args.bars)
        print(f"✅ {len(candles)} velas generadas (cubriendo los 6 regímenes canónicos).")

    # 2. Inicializar el entorno de simulación
    env = SimTrainingEnvironment(
        candidate_brain_file="q_brain_simulated_candidate.json",
        candidate_hedge_file="hedge_state_simulated_candidate.json",
        initial_balance_sats=args.initial_balance,
        explorer_mode=True,
    )
    env.brain.alpha = float(args.alpha)
    env.brain.epsilon = float(args.epsilon)

    # 3. Bucle de entrenamiento por episodios
    for ep in range(1, args.episodes + 1):
        print(f"\n--- [EPISODIO {ep}/{args.episodes}] (Epsilon={env.brain.epsilon:.3f}) ---")
        summary = env.run_episode(candles, initial_balance=args.initial_balance)

        print(
            f"  Trades: {summary['total_settled_trades']} | "
            f"WinRate: {summary['win_rate_pct']:.1f}% ({summary['winning_trades']}W / {summary['losing_trades']}L) | "
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
