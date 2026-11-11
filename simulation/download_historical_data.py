#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simulation/download_historical_data.py
Herramienta CLI para descargar y cachear datasets históricos de Binance API pública
para entrenamiento y certificación de alta fidelidad.
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from simulation.sim_market_feed import SimMarketFeed


def main():
    parser = argparse.ArgumentParser(description="Descargador de datasets históricos de Binance")
    parser.add_argument("--symbol", default="BTCUSDT", help="Par a descargar")
    parser.add_argument("--interval", default="1m", help="Temporalidad de velas (1m)")
    parser.add_argument("--bars", type=int, default=25000, help="Total de velas a descargar")
    parser.add_argument("--output", default="simulation/data/binance_btc_1m.json", help="Ruta del archivo de salida")
    args = parser.parse_args()

    out_path = os.path.abspath(args.output)
    candles = SimMarketFeed.fetch_binance_klines_paginated(
        symbol=args.symbol,
        interval=args.interval,
        total_bars=args.bars,
        save_cache_path=out_path,
    )
    print(f"✅ Descarga finalizada: {len(candles):,} velas almacenadas en {out_path}.")


if __name__ == "__main__":
    main()
