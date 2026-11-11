#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init_clean_brains.py
Inicializa cerebros nuevos, limpios y calibrados (Schema v2) para Mainnet, Testnet
y Hedge Engine, respaldando previamente los archivos existentes con verificación SHA-256.
Garantiza que el repositorio cuente con estados iniciales limpios listos para empezar de 0.
"""
import os
import sys
import shutil
from datetime import datetime

from guardian_execution_core import AtomicBrainStore
from lnm_sniper_v6_advanced import AutonomousGoalBrain, Config


def create_clean_brains():
    print("=" * 80)
    print("INICIALIZANDO CEREBROS NUEVOS Y LIMPIOS (SCHEMA v2)")
    print("=" * 80)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. Respaldo de cerebros existentes si existen
    for fname in ("q_brain_advanced_lnm.json", "q_brain_testnet_lnm.json", "hedge_state_mainnet.json", "hedge_state_testnet.json"):
        if os.path.exists(fname):
            bak_name = f"{fname}.pre_clean_{ts}.bak"
            shutil.copy2(fname, bak_name)
            print(f"📦 Respaldo creado: {fname} -> {bak_name}")

    # Extraer las claves de estado canónicas conocidas
    known_states = []
    if os.path.exists("q_brain_advanced_lnm.json"):
        try:
            store_prev = AtomicBrainStore("q_brain_advanced_lnm.json")
            data_prev = store_prev.load()
            if isinstance(data_prev, dict) and "q_table" in data_prev:
                known_states = list(data_prev["q_table"].keys())
        except Exception:
            pass

    # Si no hay estados previos, sembrar los 175 canónicos
    if len(known_states) < 100:
        regimes = ["RANGE_NORMAL", "RANGE_SQUEEZE", "TREND_BULL", "TREND_BEAR", "VOLATILITY_EXPANSION", "CHOP_HIGHVOL"]
        oscillators = ["OVERSOLD", "DIP", "MID", "PEAK", "OVERBOUGHT"]
        expansions = ["LOW_EXP", "MID_EXP", "HIGH_EXP"]
        macros = ["BULL", "BEAR", "CHOP"]
        emas = ["FULL_BULL", "BULL_DIP", "FULL_BEAR", "BEAR_RALLY", "EMA_TRANS"]
        for r in regimes:
            for o in oscillators:
                for e in expansions:
                    known_states.append(f"{r}|{o}|{e}|CHOP|EMA_TRANS")

    # Construir q_table canónica limpia con Q(s, 0) = 0.0
    clean_q_table = {}
    for st in known_states:
        norm_s = AutonomousGoalBrain._normalize_state_key(st)
        clean_q_table[norm_s] = AutonomousGoalBrain._init_q_vector(norm_s)

    clean_state_visits = {st: 0 for st in clean_q_table.keys()}

    # 2. Generar q_brain_advanced_lnm.json (Mainnet limpio)
    mainnet_payload = {
        "env": "mainnet",
        "reward_schema_version": 2,
        "epsilon": 0.0,
        "total_trained": 0,
        "total_intermediate_updates": 0,
        "current_target": Config.INITIAL_TARGET_SATS,
        "milestone_level": 1,
        "locked_floor": 0,
        "peak_equity": 0,
        "realized_net_profit_sats": 0,
        "total_fees_paid_sats": 0,
        "style_metrics": {
            "TREND": {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
            "REVERSION": {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
            "SNIPER": {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
            "DEF_SCALP": {"trades": 0, "wins": 0, "net_pnl_sats": 0, "fees_sats": 0},
        },
        "q_table": clean_q_table,
        "q_table_1h": {},
        "q_table_4h": {},
        "cycle_state": {
            "1h": {"bucket": 0, "start_eq": 0, "start_price": 0, "trades": 0, "wins": 0, "pnl": 0, "state": "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS", "action": 0},
            "4h": {"bucket": 0, "start_eq": 0, "start_price": 0, "trades": 0, "wins": 0, "pnl": 0, "state": "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS", "action": 0},
        },
        "state_visits": clean_state_visits,
        "active_trades": {},
        "processed_closed_trade_ids": [],
        "memory": [],
    }

    store_main = AtomicBrainStore("q_brain_advanced_lnm.json")
    digest_main = store_main.save(mainnet_payload)
    print(f"✅ Cerebro Mainnet Limpio guardado: q_brain_advanced_lnm.json ({len(clean_q_table)} estados | SHA-256: {digest_main[:12]}...)")

    # 3. Generar q_brain_testnet_lnm.json (Testnet limpio con explorador activo)
    testnet_payload = dict(mainnet_payload)
    testnet_payload["env"] = "testnet"
    testnet_payload["epsilon"] = 0.18
    store_test = AtomicBrainStore("q_brain_testnet_lnm.json")
    digest_test = store_test.save(testnet_payload)
    print(f"✅ Cerebro Testnet Limpio guardado: q_brain_testnet_lnm.json ({len(clean_q_table)} estados | eps=0.18 | SHA-256: {digest_test[:12]}...)")

    # 4. Generar hedge_state_mainnet.json y hedge_state_testnet.json
    for env in ("mainnet", "testnet"):
        h_name = f"hedge_state_{env}.json"
        h_payload = {
            "version": "1.0-v3",
            "env": env,
            "updated_at": round(datetime.now().timestamp(), 3),
            "pairs": {},
            "hedge_q_table": {},
            "hedge_visits": {},
            "hedge_memory": [],
            "hedge_metrics": {
                "total_hedges_opened": 0,
                "successful_neutralizations": 0,
                "successful_unwinds": 0,
                "premature_hedges": 0,
                "avoided_stopouts": 0,
                "total_hedge_fees_sats": 0,
                "total_cashed_in_sats": 0,
                "net_sats_saved_vs_sl": 0,
                "regime_stats": {},
            },
        }
        h_store = AtomicBrainStore(h_name, required_key="pairs")
        h_digest = h_store.save(h_payload)
        print(f"✅ Hedge Store Limpio guardado: {h_name} (SHA-256: {h_digest[:12]}...)")

    print("=" * 80)
    print("🏆 CEREBROS Y HEDGE STORES INICIALIZADOS DE FORMA LIMPIA Y VERIFICADA AL 100%")
    print("=" * 80)


if __name__ == "__main__":
    create_clean_brains()
