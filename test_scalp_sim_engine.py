#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_scalp_sim_engine.py
Suite hermética de certificación cuantitativa y funcional para Carril B (Scalp)
en simulation/scalp_sim_engine.py, simulation/lane_budget.py y su integración
con simulation/run_phase5b_v2_lab.py.
"""
from __future__ import annotations

import inspect
import math
import os
import pickle
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from simulation.lane_budget import LaneBudgetConfig, LaneBudgetController
from simulation.run_phase5b_v2_lab import (
    BlockDesc,
    CellAccumulator,
    _format_table,
    _run_scalping_mode,
    build_arg_parser,
)
from simulation.scalp_sim_engine import (
    MACRO_REGIME_KEYS,
    SCALP_PROFILES,
    SCALP_PROFILE_ORDER,
    CausalScalpContextBuilder,
    ScalpSimEngine,
    compute_fer,
    compute_scalp_inverse_pnl,
    compute_scalp_loss_at_sl_sats,
    get_scalp_profile,
    run_scalp_cells_on_block,
    scale_macro_ind_for_regime,
)
from simulation.sim_lnm_exchange import MicrostructureStressConfig
from simulation.sim_market_feed import Candle
from sniper_perception_sats_core import MarketRegimeClassifier


def _neutral_ctx(spot: float = 70000.0, atr_5m: float = 150.0) -> dict:
    return {
        "spot_price": float(spot),
        "atr_5m": float(atr_5m),
        "atr_15m": float(atr_5m) * 1.4,
        "liquidity_signal": {"liquidity_score": 0.0},
        "liquidity_signal_15m": {"liquidity_score": 0.0},
        "hierarchy": {},
        "vwap": {},
        "td9": {},
        "htf_sweeps": {},
        "perception": {},
    }


def _sweep_ctx(
    side: str,
    spot: float = 70000.0,
    atr_5m: float = 150.0,
    swept_level: float = 69960.0,
    wick_extreme: float = 69900.0,
) -> dict:
    is_long = side == "b"
    liq_sig = {
        "liquidity_score": 0.55 if is_long else -0.55,
        "event_type": "BULL_SWEEP_RECLAIM" if is_long else "BEAR_SWEEP_RECLAIM",
        "confirmation": "CONFIRMED_CLOSE",
        "timeframe": "5m",
        "swept_level": float(swept_level),
        "wick_extreme": float(wick_extreme),
        "nearest_high_pool": float(spot + 2500.0),
        "nearest_high_touches": 2,
        "nearest_low_pool": float(spot - 2500.0),
        "nearest_low_touches": 2,
    }
    return {
        "spot_price": float(spot),
        "atr_5m": float(atr_5m),
        "atr_15m": float(atr_5m) * 1.4,
        "liquidity_signal": liq_sig,
        "liquidity_signal_15m": liq_sig,
        "hierarchy": {},
        "vwap": {},
        "td9": {},
        "htf_sweeps": {},
        "perception": {},
    }


def _make_precomputed_scalp_block(
    candles: list[Candle],
    *,
    atr_5m: float = 200.0,
    regime_5m: str = "RANGING",
    regime_15m: str = "RANGING",
) -> dict:
    """Patrón compatible con _make_precomputed_block de test_simulation_engine.py."""
    steps = []
    for c in candles:
        px = float(c.close)
        ind_5m = {
            "price": px,
            "atr": float(atr_5m),
            "rsi": 50.0,
            "bb_pos": 0.50,
            "regime": regime_5m,
            "ema9": px,
            "ema21": px,
            "ema50": px,
            "ema_score": 0.0,
        }
        ind_15m = {
            "price": px,
            "atr": float(atr_5m) * 1.4,
            "rsi": 50.0,
            "bb_pos": 0.50,
            "regime": regime_15m,
            "ema9": px,
            "ema21": px,
            "ema50": px,
            "ema_score": 0.0,
        }
        ind_1h = {
            "price": px,
            "atr": float(atr_5m) * 2.5,
            "rsi": 50.0,
            "bb_pos": 0.50,
            "regime": regime_15m,
            "ema9": px,
            "ema21": px,
            "ema50": px,
            "ema_score": 0.0,
        }
        ind_4h = {
            "price": px,
            "atr": float(atr_5m) * 4.0,
            "rsi": 50.0,
            "bb_pos": 0.50,
            "regime": regime_15m,
            "ema9": px,
            "ema21": px,
            "ema50": px,
            "ema_score": 0.0,
        }
        perception = {
            "price": px,
            "atr": float(atr_5m),
            "market_regime": regime_5m,
            "rsi_5m": 50.0,
            "vel_ema_atr": 0.0,
            "override_side": None,
        }
        steps.append((c, ind_5m, ind_15m, ind_1h, ind_4h, perception))

    return {
        "_precomputed": True,
        "warmup_bars": 0,
        "total_len_with_warmup": max(60, len(steps)),
        "warmup_last_ts": float(candles[0].timestamp) - 60.0,
        "warmup_state": {
            "price": float(candles[0].close),
            "ind_5m": steps[0][1],
            "ind_15m": steps[0][2],
            "ind_1h": steps[0][3],
            "ind_4h": steps[0][4],
            "perception": steps[0][5],
            "timestamp": float(candles[0].timestamp) - 60.0,
        },
        "steps": steps,
    }


class TestAntiLookahead(unittest.TestCase):
    """1. Anti-lookahead estricto para modos LIMIT y MARKET."""

    def test_limit_signal_at_bar_t_never_fills_on_bar_t_long_and_short(self):
        t0 = 1700000000.0

        # LONG: señal generada al cierre de barra 0 aunque low[0] esté muy debajo de limit_price
        eng_l = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        ctx_l = _sweep_ctx("b", spot=70000.0, atr_5m=150.0, swept_level=69960.0, wick_extreme=69850.0)
        eng_l.process_bar(0, t0, 70010.0, 70040.0, 69800.0, 70000.0, context=ctx_l)
        self.assertEqual(eng_l.orders_submitted, 1)
        self.assertEqual(eng_l.orders_filled, 0)
        self.assertEqual(len(eng_l.pending_orders), 1)
        self.assertEqual(len(eng_l.running_trades), 0)

        p_lim_l = eng_l.pending_orders[0]["limit_price"]
        # También verificar vía submit_signal directo en bar_idx=0 antes de cerrar barra 0
        eng_l_direct = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        ok, _, _ = eng_l_direct.submit_signal(
            side="b",
            spot_price=70000.0,
            limit_price=69950.0,
            sl_price=69600.0,
            tp_price=70750.0,
            invalidation_price=69800.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok)
        eng_l_direct.process_bar(0, t0, 70000.0, 70020.0, 69900.0, 70000.0, context=_neutral_ctx())
        self.assertEqual(eng_l_direct.orders_filled, 0)
        self.assertEqual(len(eng_l_direct.running_trades), 0)

        # Elegible desde barra t+1
        eng_l.process_bar(1, t0 + 60.0, 70000.0, 70020.0, p_lim_l - 0.5, 70005.0, context=_neutral_ctx())
        self.assertEqual(eng_l.orders_filled, 1)
        self.assertEqual(len(eng_l.running_trades), 1)
        self.assertEqual(eng_l.running_trades[0]["entry_price"], p_lim_l)

        # SHORT: señal al cierre de barra 0 aunque high[0] esté muy arriba de limit_price
        eng_s = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        ctx_s = _sweep_ctx("s", spot=70000.0, atr_5m=150.0, swept_level=70040.0, wick_extreme=70150.0)
        eng_s.process_bar(0, t0, 69990.0, 70250.0, 69960.0, 70000.0, context=ctx_s)
        self.assertEqual(eng_s.orders_submitted, 1)
        self.assertEqual(eng_s.orders_filled, 0)
        self.assertEqual(len(eng_s.pending_orders), 1)
        self.assertEqual(len(eng_s.running_trades), 0)

        p_lim_s = eng_s.pending_orders[0]["limit_price"]
        eng_s.process_bar(1, t0 + 60.0, 70000.0, p_lim_s + 0.5, 69980.0, 69995.0, context=_neutral_ctx())
        self.assertEqual(eng_s.orders_filled, 1)
        self.assertEqual(len(eng_s.running_trades), 1)
        self.assertEqual(eng_s.running_trades[0]["entry_price"], p_lim_s)

    def test_market_signal_at_bar_t_never_fills_on_bar_t_and_opens_at_t_plus_1_open(self):
        t0 = 1700000000.0
        for side in ("b", "s"):
            eng = ScalpSimEngine(
                profile="SCALP_SWEEP_TIGHT",
                exec_mode="MARKET",
                stress_config=MicrostructureStressConfig.s0_nominal(),
            )
            swept = 69960.0 if side == "b" else 70040.0
            wick = 69850.0 if side == "b" else 70150.0
            ctx = _sweep_ctx(side, spot=70000.0, atr_5m=150.0, swept_level=swept, wick_extreme=wick)
            eng.process_bar(0, t0, 70000.0, 70250.0, 69750.0, 70000.0, context=ctx)
            self.assertEqual(eng.orders_submitted, 1)
            self.assertEqual(eng.orders_filled, 0)
            self.assertEqual(len(eng.pending_orders), 1)
            self.assertEqual(len(eng.running_trades), 0)

            open_t1 = 70015.0 if side == "b" else 69985.0
            eng.process_bar(1, t0 + 60.0, open_t1, open_t1 + 20.0, open_t1 - 20.0, open_t1, context=_neutral_ctx())
            self.assertEqual(eng.orders_filled, 1)
            self.assertEqual(len(eng.running_trades), 1)
            self.assertEqual(eng.running_trades[0]["entry_price"], open_t1)


class TestLimitFillRealism(unittest.TestCase):
    """2. Realismo de fill LIMIT: cruce estricto >= $0.5 vs toque exacto."""

    def test_long_limit_requires_half_dollar_cross(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT")
        ok, _, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69620.0,
            tp_price=70800.0,
            invalidation_price=69500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok)

        # t+1: toque exacto (low=70000.0) -> NO fill
        eng.process_bar(1, t0 + 60.0, 70040.0, 70060.0, 70000.0, 70030.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.orders_filled, 0)

        # t+2: cruce menor a $0.5 (low=69999.6) -> NO fill
        eng.process_bar(2, t0 + 120.0, 70030.0, 70040.0, 69999.6, 70020.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.orders_filled, 0)

        # t+3: cruce exacto de $0.5 (low=69999.5) -> fill a 70000.0
        eng.process_bar(3, t0 + 180.0, 70020.0, 70050.0, 69999.5, 70040.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.orders_filled, 1)
        self.assertEqual(eng.limit_filled, 1)
        self.assertEqual(len(eng.running_trades), 1)
        self.assertEqual(eng.running_trades[0]["entry_price"], 70000.0)

    def test_short_limit_requires_half_dollar_cross(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT")
        ok, _, _ = eng.submit_signal(
            side="s",
            spot_price=69950.0,
            limit_price=70000.0,
            sl_price=70380.0,
            tp_price=69200.0,
            invalidation_price=70500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok)

        # t+1: toque exacto (high=70000.0) -> NO fill
        eng.process_bar(1, t0 + 60.0, 69960.0, 70000.0, 69940.0, 69970.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.orders_filled, 0)

        # t+2: cruce menor a $0.5 (high=70000.4) -> NO fill
        eng.process_bar(2, t0 + 120.0, 69970.0, 70000.4, 69950.0, 69980.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.orders_filled, 0)

        # t+3: cruce exacto de $0.5 (high=70000.5) -> fill a 70000.0
        eng.process_bar(3, t0 + 180.0, 69980.0, 70000.5, 69950.0, 69960.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.orders_filled, 1)
        self.assertEqual(eng.limit_filled, 1)
        self.assertEqual(len(eng.running_trades), 1)
        self.assertEqual(eng.running_trades[0]["entry_price"], 70000.0)


class TestOrderCancellationLifecycle(unittest.TestCase):
    """3. Ciclo de cancelación: TTL, Runaway Guard y Wick Invalidation."""

    def test_ttl_expiration_prevents_fill_on_same_bar(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        ok, _, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69650.0,
            tp_price=70700.0,
            invalidation_price=69500.0,
            ttl_sec=240.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok)

        # En age == ttl_sec (240s), aunque low <= limit_price - 0.5, cancela por TTL_EXPIRED
        eng.process_bar(1, t0 + 240.0, 70020.0, 70030.0, 69900.0, 70010.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng.cancelled_ttl, 1)
        self.assertEqual(eng.rejected_reasons["TTL_EXPIRED"], 1)
        self.assertEqual(eng.orders_filled, 0)
        self.assertEqual(len(eng.pending_orders), 0)
        self.assertEqual(len(eng.running_trades), 0)

    def test_runaway_guard_cancels_at_55pct_and_pullback_does_not_open_trade(self):
        t0 = 1700000000.0
        # LONG
        eng_l = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT")
        ok, _, _ = eng_l.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok)
        # Avanza >= 55% hacia TP (70550.0) sin cruzar hacia abajo 69999.5
        eng_l.process_bar(1, t0 + 60.0, 70150.0, 70550.0, 70100.0, 70500.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_l.cancelled_runaway, 1)
        self.assertEqual(eng_l.rejected_reasons["RUNAWAY_GUARD"], 1)
        self.assertEqual(len(eng_l.pending_orders), 0)

        # Pullback posterior hasta limit_price - 5.0 no debe abrir trade
        eng_l.process_bar(2, t0 + 120.0, 70200.0, 70210.0, 69995.0, 70010.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_l.orders_filled, 0)
        self.assertEqual(len(eng_l.running_trades), 0)

        # SHORT simétrico
        eng_s = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT")
        ok_s, _, _ = eng_s.submit_signal(
            side="s",
            spot_price=69950.0,
            limit_price=70000.0,
            sl_price=70400.0,
            tp_price=69000.0,
            invalidation_price=70500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok_s)
        eng_s.process_bar(1, t0 + 60.0, 69850.0, 69900.0, 69450.0, 69500.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_s.cancelled_runaway, 1)
        self.assertEqual(eng_s.rejected_reasons["RUNAWAY_GUARD"], 1)
        eng_s.process_bar(2, t0 + 120.0, 69800.0, 70005.0, 69790.0, 69990.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_s.orders_filled, 0)

    def test_wick_invalidation_gap_open_and_structural_breach(self):
        t0 = 1700000000.0
        # 1) Gap-open más allá de invalidation_price (LONG y SHORT)
        eng_gap_l = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        eng_gap_l.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69650.0,
            tp_price=70750.0,
            invalidation_price=69800.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        eng_gap_l.process_bar(1, t0 + 60.0, 69750.0, 70050.0, 69700.0, 70020.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_gap_l.cancelled_invalidated, 1)
        self.assertEqual(eng_gap_l.rejected_reasons["WICK_INVALIDATED"], 1)
        self.assertEqual(eng_gap_l.orders_filled, 0)

        eng_gap_s = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        eng_gap_s.submit_signal(
            side="s",
            spot_price=69950.0,
            limit_price=70000.0,
            sl_price=70350.0,
            tp_price=69250.0,
            invalidation_price=70200.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        eng_gap_s.process_bar(1, t0 + 60.0, 70250.0, 70300.0, 69950.0, 69980.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_gap_s.cancelled_invalidated, 1)
        self.assertEqual(eng_gap_s.rejected_reasons["WICK_INVALIDATED"], 1)
        self.assertEqual(eng_gap_s.orders_filled, 0)

        # 2) Ruptura estructural pre-fill (sin cruzar tick completo $0.5 de fill)
        eng_struct_l = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT")
        eng_struct_l.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69650.0,
            tp_price=70750.0,
            invalidation_price=69999.8,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        eng_struct_l.process_bar(1, t0 + 60.0, 70020.0, 70030.0, 69999.6, 70010.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_struct_l.cancelled_invalidated, 1)
        self.assertEqual(eng_struct_l.rejected_reasons["WICK_INVALIDATED"], 1)
        self.assertEqual(eng_struct_l.orders_filled, 0)


class TestConservativeTieBreaking(unittest.TestCase):
    """4. Desempate conservador: STOP_LOSS antes que TAKE_PROFIT."""

    def test_sl_beats_tp_on_already_open_trade_long_and_short(self):
        t0 = 1700000000.0
        # LONG abierto en barra 1 y colisión SL+TP en barra 2
        eng_l = ScalpSimEngine(
            profile="SCALP_SWEEP_TIGHT",
            exec_mode="LIMIT",
            stress_config=MicrostructureStressConfig.s0_nominal(),
        )
        eng_l.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69650.0,
            tp_price=70700.0,
            invalidation_price=69500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        eng_l.process_bar(1, t0 + 60.0, 70020.0, 70030.0, 69999.5, 70010.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(len(eng_l.running_trades), 1)
        eng_l.process_bar(2, t0 + 120.0, 70010.0, 70800.0, 69600.0, 70750.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(len(eng_l.closed_trades), 1)
        self.assertEqual(eng_l.closed_trades[0]["close_reason"], "STOP_LOSS")
        self.assertEqual(eng_l.closed_trades[0]["exit_price"], 69650.0)

        # SHORT abierto en barra 1 y colisión SL+TP en barra 2
        eng_s = ScalpSimEngine(
            profile="SCALP_SWEEP_TIGHT",
            exec_mode="LIMIT",
            stress_config=MicrostructureStressConfig.s0_nominal(),
        )
        eng_s.submit_signal(
            side="s",
            spot_price=69950.0,
            limit_price=70000.0,
            sl_price=70350.0,
            tp_price=69300.0,
            invalidation_price=70500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        eng_s.process_bar(1, t0 + 60.0, 69980.0, 70000.5, 69970.0, 69990.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(len(eng_s.running_trades), 1)
        eng_s.process_bar(2, t0 + 120.0, 69990.0, 70400.0, 69200.0, 69250.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(len(eng_s.closed_trades), 1)
        self.assertEqual(eng_s.closed_trades[0]["close_reason"], "STOP_LOSS")
        self.assertEqual(eng_s.closed_trades[0]["exit_price"], 70350.0)

    def test_sl_beats_tp_on_same_fill_bar_limit_and_market(self):
        t0 = 1700000000.0
        # LIMIT LONG fill + colisión SL/TP en la misma barra
        eng_lim = ScalpSimEngine(
            profile="SCALP_SWEEP_TIGHT",
            exec_mode="LIMIT",
            stress_config=MicrostructureStressConfig.s0_nominal(),
        )
        eng_lim.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69650.0,
            tp_price=70700.0,
            invalidation_price=69500.0,
            ttl_sec=600.0,
            bar_idx=0,
            ts=t0,
        )
        eng_lim.process_bar(1, t0 + 60.0, 70040.0, 70800.0, 69600.0, 70750.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_lim.orders_filled, 1)
        self.assertEqual(len(eng_lim.running_trades), 0)
        self.assertEqual(len(eng_lim.closed_trades), 1)
        self.assertEqual(eng_lim.closed_trades[0]["close_reason"], "STOP_LOSS")
        self.assertEqual(eng_lim.closed_trades[0]["exit_price"], 69650.0)

        # MARKET SHORT fill + colisión SL/TP en la misma barra
        eng_mkt = ScalpSimEngine(
            profile="SCALP_SWEEP_TIGHT",
            exec_mode="MARKET",
            stress_config=MicrostructureStressConfig.s0_nominal(),
        )
        eng_mkt.submit_signal(
            side="s",
            spot_price=70000.0,
            sl_price=70350.0,
            tp_price=69300.0,
            bar_idx=0,
            ts=t0,
        )
        eng_mkt.process_bar(1, t0 + 60.0, 70000.0, 70400.0, 69200.0, 69250.0, context=_neutral_ctx(), has_more_bars=False)
        self.assertEqual(eng_mkt.orders_filled, 1)
        self.assertEqual(len(eng_mkt.running_trades), 0)
        self.assertEqual(len(eng_mkt.closed_trades), 1)
        self.assertEqual(eng_mkt.closed_trades[0]["close_reason"], "STOP_LOSS")
        self.assertEqual(eng_mkt.closed_trades[0]["exit_price"], 70350.0)


class TestInverseBTCUSDMath(unittest.TestCase):
    """5. Matemática inversa exacta BTCUSD, comisiones 16/20 bps, S0/S1/S2 y FER."""

    def test_hand_calculated_formulas_and_fer_16_20_bps_and_stress(self):
        margin = 1000.0
        lev = 25.0
        notional = margin * lev  # 25,000 sats
        p_entry = 70000.0
        atr_5m = 420.0  # atr/px = 0.006 > theta_vol(0.004) para activar término vol en S1/S2

        for fee_bps in (16.0, 20.0):
            f_rate = fee_bps / 20000.0
            for side, d, p_tp, p_sl in (
                ("b", 1.0, 70700.0, 69650.0),
                ("s", -1.0, 69300.0, 70350.0),
            ):
                # Verificación directa de helpers TP
                res_tp = compute_scalp_inverse_pnl(side, margin, lev, p_entry, p_tp, f_rate, f_rate, 0.0)
                exp_gross_tp = notional * (1.0 - p_entry / p_tp) * d
                exp_fees_tp = notional * (f_rate + (p_entry / p_tp) * f_rate)
                self.assertAlmostEqual(res_tp["gross_pnl_sats"], exp_gross_tp, places=6)
                self.assertAlmostEqual(res_tp["fees_sats"], exp_fees_tp, places=6)
                self.assertAlmostEqual(res_tp["net_pnl_sats"], exp_gross_tp - exp_fees_tp, places=6)

                # Verificación de LossAtSL_sats y ejecución S0 / S1 / S2
                for preset in ("S0", "S1", "S2"):
                    sc_cfg = MicrostructureStressConfig.from_preset(preset)
                    t0 = 1700000000.0
                    eng = ScalpSimEngine(
                        profile="SCALP_SWEEP_TIGHT",
                        exec_mode="LIMIT",
                        fee_rt_bps=fee_bps,
                        stress_config=sc_cfg,
                        initial_balance_sats=100_000,
                    )
                    eng.submit_signal(
                        side=side,
                        spot_price=p_entry + (20.0 if side == "b" else -20.0),
                        limit_price=p_entry,
                        sl_price=p_sl,
                        tp_price=p_tp,
                        invalidation_price=69000.0 if side == "b" else 71000.0,
                        ttl_sec=600.0,
                        margin_sats=margin,
                        leverage=int(lev),
                        bar_idx=0,
                        ts=t0,
                    )
                    ctx_bar = _neutral_ctx(spot=p_entry, atr_5m=atr_5m)
                    # Barra 1 llena el limit sin tocar SL ni TP
                    if side == "b":
                        eng.process_bar(1, t0 + 60.0, 70010.0, 70020.0, 69999.5, 70010.0, context=ctx_bar, has_more_bars=False)
                        o2, h2, l2, c2 = 70010.0, 70020.0, p_sl - 50.0, p_sl - 40.0
                    else:
                        eng.process_bar(1, t0 + 60.0, 69990.0, 70000.5, 69980.0, 69990.0, context=ctx_bar, has_more_bars=False)
                        o2, h2, l2, c2 = 69990.0, p_sl + 50.0, 69980.0, p_sl + 40.0

                    range_pct = (h2 - l2) / c2
                    eng.process_bar(2, t0 + 120.0, o2, h2, l2, c2, context=ctx_bar, has_more_bars=False)
                    self.assertEqual(len(eng.closed_trades), 1)
                    tr = eng.closed_trades[0]

                    slip = sc_cfg.compute_slip_ratio(p_sl, atr_5m=atr_5m, candle_range_pct=range_pct)
                    p_sl_exec = p_sl * (1.0 - d * slip)
                    exp_loss_sl = notional * (
                        abs(p_entry - p_sl_exec) / p_sl_exec + f_rate + (p_entry / p_sl_exec) * f_rate
                    )
                    got_loss_sl = compute_scalp_loss_at_sl_sats(margin, lev, p_entry, p_sl_exec, f_rate, f_rate)
                    self.assertAlmostEqual(got_loss_sl, exp_loss_sl, places=6)

                    exp_gross_sl = notional * (1.0 - p_entry / p_sl_exec) * d
                    exp_fees_sl = notional * (f_rate + (p_entry / p_sl_exec) * f_rate)
                    self.assertAlmostEqual(tr["exit_price"], p_sl_exec, places=6)
                    self.assertAlmostEqual(tr["gross_pnl_sats"], exp_gross_sl, places=6)
                    self.assertAlmostEqual(tr["fees_sats"], exp_fees_sl, places=6)
                    self.assertAlmostEqual(tr["net_pnl_sats"], exp_gross_sl - exp_fees_sl, places=6)
                    self.assertAlmostEqual(-tr["net_pnl_sats"], exp_loss_sl, places=6)

        # Verificación end-to-end de FER = sum(NetPnL_sats) / sum(C_rt_sats) con 1 win + 1 loss
        eng_fer = ScalpSimEngine(
            profile="SCALP_SWEEP_TIGHT",
            exec_mode="LIMIT",
            fee_rt_bps=16.0,
            stress_config=MicrostructureStressConfig.s0_nominal(),
            initial_balance_sats=100_000,
        )
        t0 = 1700000000.0
        eng_fer.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, ttl_sec=600.0, margin_sats=1000.0, leverage=25, bar_idx=0, ts=t0,
        )
        eng_fer.process_bar(1, t0 + 60.0, 70020.0, 70030.0, 69999.5, 70010.0, context=_neutral_ctx())
        eng_fer.process_bar(2, t0 + 120.0, 70010.0, 70750.0, 70000.0, 70720.0, context=_neutral_ctx())
        eng_fer.submit_signal(
            side="s", spot_price=70680.0, limit_price=70700.0, sl_price=71050.0, tp_price=70000.0,
            invalidation_price=71200.0, ttl_sec=600.0, margin_sats=1000.0, leverage=25, bar_idx=2, ts=t0 + 120.0,
        )
        eng_fer.process_bar(3, t0 + 180.0, 70690.0, 70700.5, 70680.0, 70690.0, context=_neutral_ctx())
        eng_fer.process_bar(4, t0 + 240.0, 70690.0, 71060.0, 70650.0, 71050.0, context=_neutral_ctx(), has_more_bars=False)
        ep = eng_fer.finalize_episode()
        sum_net = sum(t["net_pnl_sats"] for t in ep["closed_trades"])
        sum_crt = sum(t["c_rt_sats"] for t in ep["closed_trades"])
        self.assertGreater(sum_crt, 0.0)
        self.assertAlmostEqual(ep["fer"], compute_fer(sum_net, sum_crt), places=9)
        self.assertAlmostEqual(ep["fer"], sum_net / sum_crt, places=9)


class TestLaneBudgetIsolationAndCaps(unittest.TestCase):
    """6. Aislamiento, caps, recorte 0.5x único y pausas 24h de LaneBudgetController."""

    def test_slots_total_cap_and_15pct_margin_cap_including_pending_limit(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(
            profile="SCALP_SWEEP_TIGHT",
            exec_mode="LIMIT",
            initial_balance_sats=100_000,
        )
        # 1) Máximo 1 por lado (b) contando órdenes LIMIT pendientes
        ok1, r1, _ = eng.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, margin_sats=10_000, leverage=10, bar_idx=0, ts=t0,
        )
        self.assertTrue(ok1, msg=r1)

        ok_dup_b, r_dup_b, _ = eng.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, margin_sats=1_000, leverage=10, bar_idx=0, ts=t0,
        )
        self.assertFalse(ok_dup_b)
        self.assertEqual(r_dup_b, "LANE_B_SLOT_CAP")

        # 2) Margin cap 15% incluyendo margen reservado por LIMIT pendiente
        # open_margin_b=10_000 -> avail=90_000 -> 15% avail=13_500 -> +6_000=16_000 excede 13_500
        ok_mcap, r_mcap, _ = eng.submit_signal(
            side="s", spot_price=69950.0, limit_price=70000.0, sl_price=70350.0, tp_price=69300.0,
            invalidation_price=70500.0, margin_sats=6_000, leverage=10, bar_idx=0, ts=t0,
        )
        self.assertFalse(ok_mcap)
        self.assertEqual(r_mcap, "LANE_B_MARGIN_CAP_15PCT")

        # +3_500 -> total 13_500 <= 13_500 pasa y llena 2do slot de B (1b + 1s)
        ok_s, r_s, _ = eng.submit_signal(
            side="s", spot_price=69950.0, limit_price=70000.0, sl_price=70350.0, tp_price=69300.0,
            invalidation_price=70500.0, margin_sats=3_500, leverage=10, bar_idx=0, ts=t0,
        )
        self.assertTrue(ok_s, msg=r_s)
        self.assertEqual(len(eng.pending_orders), 2)

        # 3) Tope N_A + N_B <= 3 antes de entrar y máximo 2 en B
        ctrl = LaneBudgetController(100_000.0, LaneBudgetConfig(enabled=True))
        dec_na3_nb0 = ctrl.evaluate_lane_b_entry(
            side="b", now_ts=t0, n_open_a=3, n_open_b_buy=0, n_open_b_sell=0,
            avail_balance_sats=100_000.0, open_margin_b_sats=0.0, margin_new_sats=500.0,
            open_loss_at_sl_b_sats=0.0, loss_at_sl_new_sats=100.0,
        )
        self.assertTrue(dec_na3_nb0.allowed)

        dec_na3_nb1 = ctrl.evaluate_lane_b_entry(
            side="s", now_ts=t0, n_open_a=3, n_open_b_buy=1, n_open_b_sell=0,
            avail_balance_sats=100_000.0, open_margin_b_sats=500.0, margin_new_sats=500.0,
            open_loss_at_sl_b_sats=100.0, loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_na3_nb1.allowed)
        self.assertEqual(dec_na3_nb1.reason, "LANE_B_SLOT_CAP")

        dec_nb2 = ctrl.evaluate_lane_b_entry(
            side="b", now_ts=t0, n_open_a=0, n_open_b_buy=1, n_open_b_sell=1,
            avail_balance_sats=100_000.0, open_margin_b_sats=1_000.0, margin_new_sats=500.0,
            open_loss_at_sl_b_sats=200.0, loss_at_sl_new_sats=100.0,
        )
        self.assertFalse(dec_nb2.allowed)
        self.assertEqual(dec_nb2.reason, "LANE_B_SLOT_CAP")

    def test_soft_0_5x_sizing_applied_once_at_0_80_pct_dd(self):
        t0 = 1700000000.0
        # Motor limpio (DD_B = 0%)
        eng_base = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", initial_balance_sats=100_000)
        ok_b, _, ord_base = eng_base.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, bar_idx=0, ts=t0,
        )
        self.assertTrue(ok_b)

        # Motor con DD_B = 0.90% (>= 0.80%, < 1.50%) -> sizing 0.5x aplicado exactamente una vez
        eng_soft = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", initial_balance_sats=100_000)
        eng_soft.lane_budget.record_lane_b_pnl(-900.0, now_ts=t0 - 10.0, side="b")
        self.assertAlmostEqual(eng_soft.lane_budget.dd_b, 0.0090, places=6)

        dec = eng_soft.lane_budget.evaluate_lane_b_entry(
            side="s", now_ts=t0, n_open_a=0, n_open_b_buy=0, n_open_b_sell=0,
            avail_balance_sats=99_100.0, open_margin_b_sats=0.0, margin_new_sats=500.0,
            open_loss_at_sl_b_sats=0.0, loss_at_sl_new_sats=100.0,
        )
        self.assertTrue(dec.allowed)
        self.assertEqual(dec.risk_multiplier, 0.5)

        ok_s, _, ord_soft = eng_soft.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, bar_idx=0, ts=t0,
        )
        self.assertTrue(ok_s)
        # Base margin usa 100_000; soft usa e_global=99_100 y 0.5x UNA sola vez (~0.4955x, jamás ~0.25x)
        ratio = ord_soft["margin_sats"] / ord_base["margin_sats"]
        self.assertGreater(ratio, 0.45)
        self.assertLess(ratio, 0.55)

    def test_24h_pause_on_two_consecutive_losses_same_side_and_hard_dd(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", initial_balance_sats=100_000)
        # 2 pérdidas consecutivas pequeñas en lado 'b' (DD_B < 1.50%)
        eng.lane_budget.record_lane_b_trade_close("b", -200.0, now_ts=t0)
        eng.lane_budget.record_lane_b_trade_close("b", -200.0, now_ts=t0 + 100.0)
        self.assertLess(eng.lane_budget.dd_b, 0.0150)

        # Lado 'b' pausado por 24h; lado 's' aún permitido
        ok_b, r_b, _ = eng.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, margin_sats=1_000, bar_idx=1, ts=t0 + 101.0,
        )
        self.assertFalse(ok_b)
        self.assertEqual(r_b, "LANE_B_PAUSED_24H")

        ok_s, r_s, _ = eng.submit_signal(
            side="s", spot_price=69950.0, limit_price=70000.0, sl_price=70350.0, tp_price=69300.0,
            invalidation_price=70500.0, margin_sats=1_000, bar_idx=1, ts=t0 + 101.0,
        )
        self.assertTrue(ok_s, msg=r_s)

        # A t + 86,399 sigue pausado; a t + 86,401 se desbloquea 'b'
        ok_b_before, r_before, _ = eng.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, margin_sats=1_000, bar_idx=2, ts=t0 + 100.0 + 86399.0,
        )
        self.assertFalse(ok_b_before)
        self.assertEqual(r_before, "LANE_B_PAUSED_24H")

        ok_b_after, r_after, _ = eng.submit_signal(
            side="b", spot_price=70050.0, limit_price=70000.0, sl_price=69650.0, tp_price=70700.0,
            invalidation_price=69500.0, margin_sats=1_000, bar_idx=3, ts=t0 + 100.0 + 86401.0,
        )
        self.assertTrue(ok_b_after, msg=r_after)

        # Hard DD >= 1.50% pausa ambos lados
        ctrl_hard = LaneBudgetController(100_000.0, LaneBudgetConfig(enabled=True))
        ctrl_hard.record_lane_b_pnl(-1_500.0, now_ts=t0, side="b")
        self.assertGreaterEqual(ctrl_hard.dd_b, 0.0150)
        for s_check in ("b", "s"):
            dec = ctrl_hard.evaluate_lane_b_entry(
                side=s_check, now_ts=t0 + 60.0, n_open_a=0, n_open_b_buy=0, n_open_b_sell=0,
                avail_balance_sats=98_500.0, open_margin_b_sats=0.0, margin_new_sats=500.0,
                open_loss_at_sl_b_sats=0.0, loss_at_sl_new_sats=100.0,
            )
            self.assertFalse(dec.allowed)
            self.assertEqual(dec.reason, "LANE_B_PAUSED_24H")

    def test_open_sl_risk_cap_and_zero_contagion_to_lane_a(self):
        t0 = 1700000000.0
        ctrl = LaneBudgetController(100_000.0, LaneBudgetConfig(enabled=True))
        # Pérdida de 1,000 sats en B -> dd_b = 1.00%, e_global = 99,000
        # Cap de riesgo abierto SL = (0.0150 - 0.0100) * 99,000 = 495.0 sats
        ctrl.record_lane_b_pnl(-1_000.0, now_ts=t0, side="b")
        self.assertAlmostEqual(ctrl.dd_b, 0.0100, places=6)

        dec_ok = ctrl.evaluate_lane_b_entry(
            side="s", now_ts=t0 + 10.0, n_open_a=0, n_open_b_buy=0, n_open_b_sell=0,
            avail_balance_sats=99_000.0, open_margin_b_sats=0.0, margin_new_sats=1_000.0,
            open_loss_at_sl_b_sats=0.0, loss_at_sl_new_sats=495.0,
        )
        self.assertTrue(dec_ok.allowed)

        dec_over = ctrl.evaluate_lane_b_entry(
            side="s", now_ts=t0 + 10.0, n_open_a=0, n_open_b_buy=0, n_open_b_sell=0,
            avail_balance_sats=99_000.0, open_margin_b_sats=0.0, margin_new_sats=1_000.0,
            open_loss_at_sl_b_sats=0.0, loss_at_sl_new_sats=496.0,
        )
        self.assertFalse(dec_over.allowed)
        self.assertEqual(dec_over.reason, "LANE_B_OPEN_SL_RISK_CAP")

        # Cero contagio de DD hacia Carril A incluso si Carril B pierde 5%
        ctrl.record_lane_b_pnl(-4_000.0, now_ts=t0 + 20.0, side="s")
        self.assertGreater(ctrl.dd_b, 0.045)
        self.assertEqual(ctrl.dd_a, 0.0)
        self.assertFalse(ctrl.lane_a_recovery_mode)
        self.assertEqual(ctrl.e_a, 100_000.0)


class TestProfilesAndCLIIntegration(unittest.TestCase):
    """7. Perfiles congelados, integración run_scalp_cells_on_block, _format_table y CLI."""

    def test_all_four_profiles_strictly_inside_bounds(self):
        expected = (
            "SCALP_SWEEP_TIGHT",
            "SCALP_SWEEP_STD",
            "SCALP_VWAP_TD9",
            "SCALP_SWEEP_WIDE",
            "SCALP_SWEEP_EXT",
        )
        self.assertEqual(SCALP_PROFILE_ORDER, expected)
        self.assertEqual(set(SCALP_PROFILES.keys()), set(expected))

        for name in expected:
            cfg = get_scalp_profile(name)
            self.assertGreaterEqual(cfg.min_sl_pct, 0.0055 - 1e-12)
            self.assertLessEqual(cfg.max_sl_pct, 0.0065 + 1e-12)
            self.assertLessEqual(cfg.min_sl_pct, cfg.max_sl_pct)

            self.assertGreaterEqual(cfg.min_tp_pct, 0.0125 - 1e-12)
            self.assertLessEqual(cfg.max_tp_pct, 0.0160 + 1e-12)
            self.assertLessEqual(cfg.min_tp_pct, cfg.max_tp_pct)

            self.assertGreaterEqual(cfg.target_rr, 2.1 - 1e-12)
            self.assertLessEqual(cfg.target_rr, 2.5 + 1e-12)

            # Fricción relativa c / TP <= 13% @ 16 bps (0.0016)
            self.assertLessEqual(0.0016 / cfg.min_tp_pct, 0.13 + 1e-9)

            eng = ScalpSimEngine(profile=name, exec_mode="LIMIT")
            for atr_val in (1.0, 200.0, 5000.0):
                for side in ("b", "s"):
                    sl_px, tp_px = eng._compute_clamped_bracket(side, 70000.0, atr_val)
                    sl_pct = abs(70000.0 - sl_px) / 70000.0
                    tp_pct = abs(tp_px - 70000.0) / 70000.0
                    self.assertGreaterEqual(sl_pct, 0.0055 - 1e-4)
                    self.assertLessEqual(sl_pct, 0.0065 + 1e-4)
                    self.assertGreaterEqual(tp_pct, 0.0125 - 1e-4)
                    self.assertLessEqual(tp_pct, 0.0160 + 1e-4)

    def test_run_scalp_cells_on_block_and_format_table(self):
        # Construir bloque sintético con pool en 69,900, barrido en 5m y pullback tras la señal en barra 65
        t0 = 1700000100.0  # múltiplo limpio de 300s (5m)
        candles = []
        for i in range(60):
            ts = t0 + i * 60.0
            base = 70000.0 + (20.0 * math.sin(i / 4.0))
            low = 69900.0 if (i in (12, 27, 42)) else base - 30.0
            high = 70120.0 if (i in (18, 33, 48)) else base + 30.0
            candles.append(Candle(timestamp=ts, open=base, high=high, low=low, close=base, volume=10.0))

        # Barras 60..64 (vela 5m): barrido por debajo de 69,900 hasta 69,850 y reclaim sobre 70,000
        candles.append(Candle(timestamp=t0 + 60 * 60.0, open=69980.0, high=69990.0, low=69860.0, close=69940.0, volume=25.0))
        candles.append(Candle(timestamp=t0 + 61 * 60.0, open=69940.0, high=69980.0, low=69850.0, close=69960.0, volume=30.0))
        candles.append(Candle(timestamp=t0 + 62 * 60.0, open=69960.0, high=70010.0, low=69950.0, close=70005.0, volume=25.0))
        candles.append(Candle(timestamp=t0 + 63 * 60.0, open=70005.0, high=70030.0, low=69990.0, close=70020.0, volume=20.0))
        candles.append(Candle(timestamp=t0 + 64 * 60.0, open=70020.0, high=70040.0, low=70000.0, close=70030.0, volume=20.0))

        # Barra 65 abre nuevo bucket 5m y confirma la señal al cierre (close=69,990)
        candles.append(Candle(timestamp=t0 + 65 * 60.0, open=70030.0, high=70035.0, low=69950.0, close=69990.0, volume=18.0))
        # Barra 66 (t+1) hace pullback por encima de invalidación (69,823) para llenar LIMIT (~69,914)
        candles.append(Candle(timestamp=t0 + 66 * 60.0, open=69990.0, high=70000.0, low=69895.0, close=69985.0, volume=18.0))
        # Barras 67..90: expansión alcista hacia TP (+1.3%..+1.7%)
        for j in range(67, 91):
            px = 70000.0 + (j - 66) * 50.0
            candles.append(
                Candle(timestamp=t0 + j * 60.0, open=px - 20.0, high=px + 30.0, low=px - 30.0, close=px, volume=15.0)
            )

        blk = _make_precomputed_scalp_block(candles, atr_5m=180.0)
        engines = {
            "scalp|S0|16bps|SCALP_SWEEP_TIGHT|LIMIT": ScalpSimEngine(
                profile="SCALP_SWEEP_TIGHT",
                exec_mode="LIMIT",
                fee_rt_bps=16.0,
                stress_config=MicrostructureStressConfig.s0_nominal(),
            ),
            "scalp|S0|16bps|SCALP_SWEEP_TIGHT|MARKET": ScalpSimEngine(
                profile="SCALP_SWEEP_TIGHT",
                exec_mode="MARKET",
                fee_rt_bps=16.0,
                stress_config=MicrostructureStressConfig.s0_nominal(),
            ),
        }
        results = run_scalp_cells_on_block(blk, engines, initial_balance=100_000)
        self.assertEqual(set(results.keys()), set(engines.keys()))
        for cid, res in results.items():
            self.assertGreater(res["signals_total"], 0, msg=cid)
            self.assertGreater(res["orders_submitted"], 0, msg=cid)
            self.assertGreater(res["orders_filled"], 0, msg=cid)
            self.assertGreater(res["n_trades"], 0, msg=cid)
            self.assertIn("fer", res)
            self.assertIn("fill_pct", res)
            self.assertIn("total_cost_sats", res)
            self.assertIn("gross_pnl_sats", res)
            self.assertIn("exit_reasons", res)

        # Alimentar CellAccumulator en directorio temporal para verificar _format_table
        with tempfile.TemporaryDirectory() as tmpdir:
            desc_oos = BlockDesc(
                index=0,
                block_key="B_OOS_01",
                label="OOS_01",
                pkl_path=os.path.join(tmpdir, "oos_01.pkl"),
                is_oos_12m=True,
                is_m16=False,
                oos_month_bucket="2025-01",
            )
            desc_m16 = BlockDesc(
                index=1,
                block_key="B_M16",
                label="M16",
                pkl_path=os.path.join(tmpdir, "m16.pkl"),
                is_oos_12m=True,
                is_m16=True,
                oos_month_bucket="2025-02",
            )
            rec_params = inspect.signature(CellAccumulator.record_block).parameters
            final_rows = []
            for cid, res in results.items():
                exec_m = cid.split("|")[-1]
                acc = CellAccumulator(
                    cell_id=cid,
                    mode="scalping",
                    scope="fold1",
                    scenario="S0",
                    fee_rt_bps=16.0,
                    taker_fee_rate=0.0008,
                    initial_balance=100_000,
                    params={"profile": "SCALP_SWEEP_TIGHT", "exec_mode": exec_m},
                )
                extra_kw = {}
                if "gross_pnl_sats" in rec_params:
                    extra_kw["gross_pnl_sats"] = float(res["gross_pnl_sats"])
                if "exit_reasons" in rec_params:
                    extra_kw["exit_reasons"] = dict(res["exit_reasons"])
                for d_blk in (desc_oos, desc_m16):
                    acc.record_block(
                        desc=d_blk,
                        n_trades=int(res["n_trades"]),
                        wins=int(res["wins"]),
                        net_sats=int(round(res["net_profit_sats"])),
                        b_win=int(round(res["gross_win_sats"])),
                        b_loss=int(round(res["gross_loss_sats"])),
                        dd_pct=float(res["max_drawdown_pct"]),
                        blk_ddg=int(res["dd_guard_events"]),
                        floor_violated=bool(res["hard_floor_violated"]),
                        cost_sats=float(res["total_cost_sats"]),
                        submitted=int(res["orders_submitted"]),
                        filled=int(res["orders_filled"]),
                        **extra_kw,
                    )
                final_rows.append(acc.finalize())

            table_str = _format_table(final_rows)
            for col in (
                "Fill%",
                "FER",
                "PF",
                "OOS_sin_M16",
                "MaxDD%",
                "WR%",
                "Net_18M",
                "OOS_12M",
                "GrossPnL",
                "AvgW",
                "AvgL",
                "RR_net",
                "ExitSplit",
            ):
                self.assertIn(col, table_str)
            for cid in results:
                self.assertIn(cid, table_str)

    def test_build_arg_parser_and_cli_help_lists_scalping(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--mode", "scalping", "--scope", "fold1"])
        self.assertEqual(args.mode, "scalping")
        self.assertEqual(args.scope, "fold1")
        self.assertEqual(args.exec_modes, "LIMIT,MARKET")
        self.assertEqual(args.scalp_profiles, ",".join(SCALP_PROFILE_ORDER))
        self.assertEqual(args.max_hold_hours, "0")

        args_ablation = parser.parse_args(
            [
                "--mode",
                "scalping",
                "--scope",
                "fold1",
                "--lock-modes",
                "pure_sl_tp,delayed_75",
                "--macro-filters",
                "off,counter_block,range_only",
                "--no-rolling-pause-reset",
            ]
        )
        self.assertEqual(args_ablation.lock_modes, "pure_sl_tp,delayed_75")
        self.assertEqual(args_ablation.macro_filters, "off,counter_block,range_only")
        self.assertFalse(args_ablation.rolling_pause_reset)

        args_rpr = parser.parse_args(["--mode", "scalping", "--rolling-pause-reset"])
        self.assertTrue(args_rpr.rolling_pause_reset)

        args_mh = parser.parse_args(["--mode", "scalping", "--max-hold-hours", "0,16"])
        self.assertEqual(args_mh.max_hold_hours, "0,16")

        proc = subprocess.run(
            [sys.executable, "-B", "simulation/run_phase5b_v2_lab.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        for token in (
            "scalping",
            "--mode",
            "--scalp-profiles",
            "--exec-modes",
            "--lock-modes",
            "--macro-filters",
            "--max-hold-hours",
            "--rolling-pause-reset",
            "--no-rolling-pause-reset",
        ):
            self.assertIn(token, proc.stdout)


class TestPhase1B2AblationAndFixes(unittest.TestCase):
    """8. Certificación Fase 1B.2: modos de lock, clamping 0-deadlock, re-anchor, filtro macro causal, flancos y contabilidad."""

    def test_lock_modes_pure_sl_tp_vs_delayed_75_vs_legacy(self):
        t0 = 1700000000.0

        def _open_long_trade(lock_mode: str) -> ScalpSimEngine:
            eng = ScalpSimEngine(
                profile="SCALP_SWEEP_STD",
                exec_mode="LIMIT",
                lock_mode=lock_mode,
                fee_rt_bps=16.0,
                stress_config=MicrostructureStressConfig.s0_nominal(),
                initial_balance_sats=100_000,
            )
            ok, reason, _ = eng.submit_signal(
                side="b",
                spot_price=70050.0,
                limit_price=70000.0,
                sl_price=69600.0,
                tp_price=71000.0,
                invalidation_price=69400.0,
                ttl_sec=600.0,
                bar_idx=0,
                ts=t0,
            )
            self.assertTrue(ok, msg=reason)
            eng.process_bar(1, t0 + 60.0, 70020.0, 70030.0, 69999.5, 70010.0, context=_neutral_ctx())
            self.assertEqual(len(eng.running_trades), 1)
            return eng

        # 1) pure_sl_tp: jamás mueve el stop aunque alcance 80% de TP (2.0R) y retroceda a 69,900
        eng_pure = _open_long_trade("pure_sl_tp")
        eng_pure.process_bar(2, t0 + 120.0, 70010.0, 70820.0, 70000.0, 70800.0, context=_neutral_ctx())
        self.assertEqual(len(eng_pure.running_trades), 1)
        self.assertEqual(eng_pure.running_trades[0]["sl_price"], 69600.0)
        self.assertFalse(eng_pure.running_trades[0].get("lock_active", False))

        eng_pure.process_bar(3, t0 + 180.0, 70800.0, 70810.0, 69900.0, 69920.0, context=_neutral_ctx())
        self.assertEqual(len(eng_pure.running_trades), 1)
        self.assertEqual(len(eng_pure.closed_trades), 0)
        self.assertEqual(eng_pure.running_trades[0]["sl_price"], 69600.0)

        eng_pure.process_bar(
            4, t0 + 240.0, 69920.0, 71050.0, 69910.0, 71020.0, context=_neutral_ctx(), has_more_bars=False
        )
        self.assertEqual(len(eng_pure.closed_trades), 1)
        self.assertEqual(eng_pure.closed_trades[0]["close_reason"], "TAKE_PROFIT")
        ep_pure = eng_pure.finalize_episode()
        self.assertEqual(ep_pure["exit_reasons"]["TP"], 1)
        self.assertEqual(ep_pure["exit_reasons"]["DEF_SCALP_LOCK"], 0)

        # 2) delayed_75: inactivo en 70% de TP (70,700), activo en >= 75% de TP (70,760) asegurando > +0.40*TP neto sobre fees
        eng_d75 = _open_long_trade("delayed_75")
        eng_d75.process_bar(2, t0 + 120.0, 70010.0, 70710.0, 70000.0, 70700.0, context=_neutral_ctx())
        tr_d75 = eng_d75.running_trades[0]
        self.assertEqual(tr_d75["sl_price"], 69600.0)
        self.assertFalse(tr_d75["lock_active"])

        eng_d75.process_bar(3, t0 + 180.0, 70700.0, 70780.0, 70690.0, 70760.0, context=_neutral_ctx())
        self.assertTrue(tr_d75["lock_active"])
        locked_sl = float(tr_d75["sl_price"])
        self.assertGreater(locked_sl, 70400.0)  # > entry + 0.40 * tp_dist (jamás BE 0%)

        f_rate = 16.0 / 20000.0
        pnl_at_lock = compute_scalp_inverse_pnl(
            "b", tr_d75["margin_sats"], tr_d75["leverage"], 70000.0, locked_sl, f_rate, f_rate, 0.0
        )
        tp_gross = compute_scalp_inverse_pnl(
            "b", tr_d75["margin_sats"], tr_d75["leverage"], 70000.0, 71000.0, 0.0, 0.0, 0.0
        )["gross_pnl_sats"]
        self.assertGreater(pnl_at_lock["net_pnl_sats"], 0.40 * tp_gross)

        eng_d75.process_bar(
            4,
            t0 + 240.0,
            70750.0,
            70760.0,
            locked_sl - 10.0,
            locked_sl - 5.0,
            context=_neutral_ctx(),
            has_more_bars=False,
        )
        self.assertEqual(len(eng_d75.closed_trades), 1)
        self.assertEqual(eng_d75.closed_trades[0]["close_reason"], "DEF_SCALP_LOCK")
        self.assertGreater(eng_d75.closed_trades[0]["net_pnl_sats"], 0.0)
        ep_d75 = eng_d75.finalize_episode()
        self.assertEqual(ep_d75["exit_reasons"]["DEF_SCALP_LOCK"], 1)

        # 3) legacy: avance temprano de 1.2R (70,480 < 0.75 * TP) sí activa lock_active
        eng_leg = _open_long_trade("legacy")
        eng_leg.process_bar(2, t0 + 120.0, 70010.0, 70500.0, 70000.0, 70480.0, context=_neutral_ctx())
        self.assertTrue(eng_leg.running_trades[0]["lock_active"])
        self.assertGreater(eng_leg.running_trades[0]["sl_price"], 69600.0)

    def test_zero_deadlock_dynamic_risk_clamping_at_1_40pct_dd(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            initial_balance_sats=100_000,
            dynamic_risk_clamp=True,
        )
        eng.lane_budget.record_lane_b_pnl(-1_400.0, now_ts=t0 - 60.0, side="s")
        self.assertAlmostEqual(eng.lane_budget.dd_b, 0.0140, places=6)
        self.assertAlmostEqual(eng.lane_budget.e_global, 98_600.0, places=6)

        cap = (0.0150 - 0.0140) * 98_600.0  # 98.60 sats (0.10% * E_global)
        self.assertAlmostEqual(cap, 98.60, places=6)

        ok, reason, order = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok, msg=reason)
        self.assertEqual(reason, "SUBMITTED")
        self.assertIsNotNone(order)
        self.assertLessEqual(order["loss_at_sl_sats"], cap + 1e-9)
        self.assertGreater(order["loss_at_sl_sats"], 0.80 * cap)
        self.assertEqual(eng.clamped_entries, 1)

        # Contraste directo: sin clamping dinámico, el mismo estado rechaza con LANE_B_OPEN_SL_RISK_CAP
        eng_noclamp = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            initial_balance_sats=100_000,
            dynamic_risk_clamp=False,
        )
        eng_noclamp.lane_budget.record_lane_b_pnl(-1_400.0, now_ts=t0 - 60.0, side="s")
        ok_nc, reason_nc, _ = eng_noclamp.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertFalse(ok_nc)
        self.assertEqual(reason_nc, "LANE_B_OPEN_SL_RISK_CAP")

    def test_post_pause_window_reanchor_0_5x_and_hard_caps(self):
        t0 = 1700000000.0

        eng_clean = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            initial_balance_sats=100_000,
        )
        ok_c, _, ord_clean = eng_clean.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok_c)
        base_margin = ord_clean["margin_sats"]

        eng = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            initial_balance_sats=100_000,
            rolling_pause_reset=True,
        )
        eng.lane_budget.record_lane_b_pnl(-1_600.0, now_ts=t0, side="b")
        self.assertGreaterEqual(eng.lane_budget.dd_b, 0.0160 - 1e-9)

        # Durante las 24h está pausado
        ok_p, r_p, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=1,
            ts=t0 + 3600.0,
        )
        self.assertFalse(ok_p)
        self.assertEqual(r_p, "LANE_B_PAUSED_24H")

        # Tras expirar las 24h: re-anchor a 98,400, dd_b == 0.0, reanchor_count_b == 1 y sizing 0.5x
        t1 = t0 + 86_401.0
        ok_1, r_1, order = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=2,
            ts=t1,
        )
        self.assertTrue(ok_1, msg=r_1)
        self.assertAlmostEqual(eng.lane_budget.dd_b, 0.0, places=9)
        self.assertAlmostEqual(eng.lane_budget.peak_b, 98_400.0, places=6)
        self.assertEqual(eng.lane_budget.reanchor_count_b, 1)
        ratio = order["margin_sats"] / base_margin
        self.assertGreater(ratio, 0.44)
        self.assertLess(ratio, 0.55)

        # Expirar orden pendiente por TTL antes de probar el techo duro de episodio
        eng.process_bar(3, t1 + 1000.0, 70050.0, 70060.0, 70040.0, 70050.0, context=_neutral_ctx())
        self.assertEqual(len(eng.pending_orders), 0)

        # Techo duro DD_B_episode_hard_cap = 3.00% incluso si A ganó +2,500 (dd_global < 2.20%)
        eng.lane_budget.record_lane_a_pnl(2_500.0)
        eng.lane_budget.record_lane_b_pnl(-1_500.0, now_ts=t1 + 1200.0, side="s")
        self.assertAlmostEqual(eng.lane_budget.e_b, 96_900.0, places=6)

        t2 = t1 + 90_000.0
        ok_ep, r_ep, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=4,
            ts=t2,
        )
        self.assertFalse(ok_ep)
        self.assertEqual(r_ep, "LANE_B_EPISODE_HARD_CAP")

        # Freno global DD_global >= 2.20% cuando dd_b_episode = 1.60% < 3.00% pero A pierde -700 sats
        eng_glob = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            initial_balance_sats=100_000,
            rolling_pause_reset=True,
        )
        eng_glob.lane_budget.record_lane_b_pnl(-1_600.0, now_ts=t0, side="b")
        eng_glob.lane_budget.record_lane_a_pnl(-700.0)
        ok_g, r_g, _ = eng_glob.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            bar_idx=1,
            ts=t0 + 86_401.0,
        )
        self.assertFalse(ok_g)
        self.assertEqual(r_g, "LANE_B_PROTECTIVE_DD_BRAKE")

    def test_causal_macro_filter_counter_block_and_range_only_no_lookahead(self):
        t0 = 1700006400.0  # múltiplo exacto de 14,400s (4h), 3,600s (1h) y 300s (5m)

        def _eval_macro(macro_filter: str, side: str, ind_1h: dict, ind_4h: dict, perception: dict | None = None):
            eng = ScalpSimEngine(
                profile="SCALP_SWEEP_STD",
                exec_mode="LIMIT",
                macro_filter=macro_filter,
            )
            swept = 69960.0 if side == "b" else 70040.0
            wick = 69850.0 if side == "b" else 70150.0
            ctx = _sweep_ctx(side, spot=70000.0, atr_5m=150.0, swept_level=swept, wick_extreme=wick)
            ctx["liquidity_signal"] = dict(ctx["liquidity_signal"], timeframe="15m")
            ctx["ind_1h"] = dict(ind_1h)
            ctx["ind_4h"] = dict(ind_4h)
            if perception is not None:
                ctx["perception"] = dict(perception)
            eng.process_bar(0, t0, 70000.0, 70040.0, 69960.0, 70000.0, context=ctx)
            if eng.orders_submitted == 1:
                return True, "SUBMITTED", eng
            reason = next(iter(eng.rejected_reasons.keys()), "")
            return False, reason, eng

        bull_1h = {
            "regime": "TREND_BULL",
            "market_regime": "VOLATILITY_EXPANSION",
            "ema_score": 1.0,
            "ema9": 70500.0,
            "ema21": 70000.0,
            "adx": 30.0,
        }
        bull_4h = {
            "regime": "TREND_BULL",
            "ema_score": 1.0,
            "ema9": 70400.0,
            "ema21": 69800.0,
        }

        # Contexto alcista fuerte 1h/4h
        ok_cb_s, r_cb_s, _ = _eval_macro("counter_block", "s", bull_1h, bull_4h)
        self.assertFalse(ok_cb_s)
        self.assertEqual(r_cb_s, "MACRO_COUNTER_BLOCK")

        ok_cb_b, r_cb_b, _ = _eval_macro("counter_block", "b", bull_1h, bull_4h)
        self.assertTrue(ok_cb_b, msg=r_cb_b)

        for s_side in ("b", "s"):
            ok_ro, r_ro, _ = _eval_macro("range_only", s_side, bull_1h, bull_4h)
            self.assertFalse(ok_ro)
            self.assertEqual(r_ro, "MACRO_NOT_RANGE")

            ok_off, r_off, _ = _eval_macro("off", s_side, bull_1h, bull_4h)
            self.assertTrue(ok_off, msg=r_off)

        # Contexto bajista fuerte 1h/4h
        bear_1h = {
            "regime": "TREND_BEAR",
            "market_regime": "VOLATILITY_EXPANSION",
            "ema_score": -1.0,
            "ema9": 69500.0,
            "ema21": 70000.0,
            "adx": 30.0,
        }
        bear_4h = {
            "regime": "TREND_BEAR",
            "ema_score": -1.0,
            "ema9": 69600.0,
            "ema21": 70200.0,
        }
        ok_bear_b, r_bear_b, _ = _eval_macro("counter_block", "b", bear_1h, bear_4h)
        self.assertFalse(ok_bear_b)
        self.assertEqual(r_bear_b, "MACRO_COUNTER_BLOCK")

        ok_bear_s, r_bear_s, _ = _eval_macro("counter_block", "s", bear_1h, bear_4h)
        self.assertTrue(ok_bear_s, msg=r_bear_s)

        # EXTREME_VOLATILITY bloquea en ambos filtros
        ext_1h = {
            "regime": "EXTREME_VOLATILITY",
            "market_regime": "EXTREME_VOLATILITY",
            "ema_score": 0.0,
            "ema9": 70000.0,
            "ema21": 70000.0,
            "adx": 45.0,
        }
        ext_4h = {"regime": "EXTREME_VOLATILITY", "ema_score": 0.0, "ema9": 70000.0, "ema21": 70000.0}
        ext_perc = {"market_regime": "EXTREME_VOLATILITY"}
        for filt in ("counter_block", "range_only"):
            for s_side in ("b", "s"):
                ok_ev, r_ev, _ = _eval_macro(filt, s_side, ext_1h, ext_4h, perception=ext_perc)
                self.assertFalse(ok_ev)
                self.assertEqual(r_ev, "MACRO_EXTREME_VOL")

        # Cero lookahead en secuencia cruda Candle: 3h planas en rango (0..179) seguidas de expansión alcista futura (180..299)
        candles_180: list[Candle] = []
        for i in range(170):
            ts = t0 + i * 60.0
            base = 70000.0 + 15.0 * math.sin(i / 4.0)
            high = 70120.0 if (i in (12, 27, 42, 72, 102, 132)) else base + 55.0
            low = 69880.0 if (i in (18, 33, 48, 78, 108, 138)) else base - 55.0
            candles_180.append(Candle(timestamp=ts, open=base, high=high, low=low, close=base, volume=12.0))

        # Minutos 170..174: barrido bajista sobre el pool 70,120 hasta 70,165 y reclaim en 70,065
        candles_180.append(Candle(timestamp=t0 + 170 * 60.0, open=70040.0, high=70140.0, low=70010.0, close=70070.0, volume=25.0))
        candles_180.append(Candle(timestamp=t0 + 171 * 60.0, open=70070.0, high=70165.0, low=70030.0, close=70060.0, volume=30.0))
        candles_180.append(Candle(timestamp=t0 + 172 * 60.0, open=70060.0, high=70080.0, low=70020.0, close=70055.0, volume=25.0))
        candles_180.append(Candle(timestamp=t0 + 173 * 60.0, open=70055.0, high=70075.0, low=70025.0, close=70060.0, volume=20.0))
        candles_180.append(Candle(timestamp=t0 + 174 * 60.0, open=70060.0, high=70080.0, low=70035.0, close=70065.0, volume=20.0))
        # Minutos 175..179: sigue en rango plano ~70,060 justo hasta minuto 179
        candles_180.append(Candle(timestamp=t0 + 175 * 60.0, open=70065.0, high=70085.0, low=70030.0, close=70060.0, volume=18.0))
        for i in range(176, 180):
            ts = t0 + i * 60.0
            candles_180.append(Candle(timestamp=ts, open=70060.0, high=70080.0, low=70030.0, close=70055.0, volume=15.0))
        self.assertEqual(len(candles_180), 180)

        # Barras futuras > 179 (180..299): tendencia alcista limpia (+12 USD/min hasta ~71,500 -> TREND_BULL 1h)
        candles_with_future_bull = list(candles_180)
        for j in range(180, 300):
            ts = t0 + j * 60.0
            px = 70055.0 + (j - 179) * 12.0
            candles_with_future_bull.append(
                Candle(timestamp=ts, open=px - 10.0, high=px + 15.0, low=px - 15.0, close=px, volume=40.0)
            )

        # 1) Verificación directa al minuto 179: el builder causal solo vio 0..179 (RANGE) sin contaminarse por 180..299
        from simulation.scalp_sim_engine import CausalScalpContextBuilder
        builder_causal = CausalScalpContextBuilder()
        live_ctx_179 = {}
        for idx in range(180):
            c_bar = candles_with_future_bull[idx]
            live_ctx_179 = builder_causal.step(
                c_bar.timestamp, c_bar.open, c_bar.high, c_bar.low, c_bar.close, volume=c_bar.volume
            )
        self.assertEqual(live_ctx_179["ind_1h"]["regime"], "RANGE")
        self.assertEqual(live_ctx_179["ind_4h"]["regime"], "RANGE")

        c_179 = candles_with_future_bull[179]
        for filt in ("range_only", "counter_block"):
            eng_causal = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT", macro_filter=filt)
            ctx_179 = _sweep_ctx("s", spot=70000.0, atr_5m=150.0, swept_level=70040.0, wick_extreme=70095.0)
            ctx_179["liquidity_signal"] = dict(ctx_179["liquidity_signal"], timeframe="15m")
            ctx_179["ind_1h"] = live_ctx_179["ind_1h"]
            ctx_179["ind_4h"] = live_ctx_179["ind_4h"]
            eng_causal.process_bar(179, c_179.timestamp, c_179.open, c_179.high, c_179.low, c_179.close, context=ctx_179)
            self.assertEqual(eng_causal.orders_submitted, 1, msg=f"Blocked at min 179 for {filt}: {eng_causal.rejected_reasons}")

        # Y tras cerrar las horas futuras alcistas (180..299), el mismo builder causal pasa a TREND_BULL y bloquea SHORT
        live_ctx_299 = {}
        for idx in range(180, 300):
            c_bar = candles_with_future_bull[idx]
            live_ctx_299 = builder_causal.step(
                c_bar.timestamp, c_bar.open, c_bar.high, c_bar.low, c_bar.close, volume=c_bar.volume
            )
        self.assertEqual(live_ctx_299["ind_1h"]["regime"], "TREND_BULL")
        eng_after_bull = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT", macro_filter="counter_block")
        ctx_299 = _sweep_ctx("s", spot=71495.0, atr_5m=150.0, swept_level=71535.0, wick_extreme=71590.0)
        ctx_299["liquidity_signal"] = dict(ctx_299["liquidity_signal"], timeframe="15m")
        ctx_299["ind_1h"] = live_ctx_299["ind_1h"]
        ctx_299["ind_4h"] = live_ctx_299["ind_4h"]
        c_299 = candles_with_future_bull[299]
        eng_after_bull.process_bar(299, c_299.timestamp, c_299.open, c_299.high, c_299.low, c_299.close, context=ctx_299)
        self.assertEqual(eng_after_bull.orders_submitted, 0)
        self.assertEqual(eng_after_bull.rejected_reasons["MACRO_COUNTER_BLOCK"], 1)

        # 2) Paridad exacta de decisiones hasta t=179 en run_scalp_cells_on_block con vs sin barras futuras
        res_180 = run_scalp_cells_on_block(
            candles_180,
            {
                "ro": ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", macro_filter="range_only"),
                "cb": ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", macro_filter="counter_block"),
            },
            initial_balance=100_000,
        )
        res_300 = run_scalp_cells_on_block(
            candles_with_future_bull,
            {
                "ro": ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", macro_filter="range_only"),
                "cb": ScalpSimEngine(profile="SCALP_SWEEP_TIGHT", exec_mode="LIMIT", macro_filter="counter_block"),
            },
            initial_balance=100_000,
        )
        for k in ("ro", "cb"):
            self.assertGreaterEqual(res_180[k]["orders_submitted"], 1, msg=res_180[k])
            self.assertGreaterEqual(res_300[k]["orders_submitted"], res_180[k]["orders_submitted"])

    def test_edge_triggering_single_signal_and_clean_limit_offset_counter(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT")
        ctx_b = _sweep_ctx("b", spot=70000.0, atr_5m=150.0, swept_level=69960.0, wick_extreme=69850.0)
        ctx_b["liquidity_signal"] = dict(ctx_b["liquidity_signal"], timeframe="15m")

        # 10 velas consecutivas con condición de barrido idéntica -> exactamente 1 señal en el flanco
        for i in range(10):
            eng.process_bar(i, t0 + i * 60.0, 70005.0, 70020.0, 70000.0, 70000.0, context=ctx_b)
        self.assertEqual(eng.signals_total, 1)

        # 2 velas neutrales rearman el detector y un barrido en el lado opuesto dispara la 2da señal
        eng.process_bar(10, t0 + 10 * 60.0, 70000.0, 70010.0, 69990.0, 70000.0, context=_neutral_ctx())
        eng.process_bar(11, t0 + 11 * 60.0, 70000.0, 70010.0, 69990.0, 70000.0, context=_neutral_ctx())
        self.assertEqual(eng.signals_total, 1)

        ctx_s = _sweep_ctx("s", spot=70000.0, atr_5m=150.0, swept_level=70040.0, wick_extreme=70150.0)
        ctx_s["liquidity_signal"] = dict(ctx_s["liquidity_signal"], timeframe="15m")
        eng.process_bar(12, t0 + 12 * 60.0, 69995.0, 70000.0, 69980.0, 70000.0, context=ctx_s)
        self.assertEqual(eng.signals_total, 2)

        # Conteo limpio de LIMIT_OFFSET_TOO_FAR sin floats interpolados en las claves
        eng_off = ScalpSimEngine(profile="SCALP_SWEEP_STD", exec_mode="LIMIT")
        liq_far = {
            "liquidity_score": 0.65,
            "swept_level": 70000.0,
            "wick_extreme": 69900.0,
            "timeframe": "5m",
        }
        for idx, spot in enumerate((71000.0, 71050.0, 71120.0)):
            ok, reason, _ = eng_off.submit_signal(
                side="b",
                spot_price=spot,
                atr_5m=100.0,
                liquidity_signal=liq_far,
                bar_idx=idx,
                ts=t0 + idx * 60.0,
            )
            self.assertFalse(ok)
            self.assertEqual(reason, "LIMIT_OFFSET_TOO_FAR")

        self.assertEqual(dict(eng_off.rejected_reasons), {"LIMIT_OFFSET_TOO_FAR": 3})
        for key in eng_off.rejected_reasons:
            self.assertNotIn("(", key)
            self.assertNotIn(".", key)

    def test_accounting_identity_gross_pnl_and_exit_reasons(self):
        t0 = 1700000000.0
        eng = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            lock_mode="pure_sl_tp",
            fee_rt_bps=16.0,
            stress_config=MicrostructureStressConfig.s0_nominal(),
            initial_balance_sats=100_000,
        )

        # Trade 1: LONG cerrado por TAKE_PROFIT
        ok1, r1, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            ttl_sec=600.0,
            margin_sats=1000.0,
            leverage=25,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok1, msg=r1)
        eng.process_bar(1, t0 + 60.0, 70020.0, 70030.0, 69999.5, 70010.0, context=_neutral_ctx())
        eng.process_bar(2, t0 + 120.0, 70010.0, 71050.0, 70000.0, 71020.0, context=_neutral_ctx())
        self.assertEqual(len(eng.closed_trades), 1)
        self.assertEqual(eng.closed_trades[0]["close_reason"], "TAKE_PROFIT")

        # Trade 2: SHORT cerrado por STOP_LOSS
        ok2, r2, _ = eng.submit_signal(
            side="s",
            spot_price=70950.0,
            limit_price=71000.0,
            sl_price=71400.0,
            tp_price=70000.0,
            invalidation_price=71600.0,
            ttl_sec=600.0,
            margin_sats=1000.0,
            leverage=25,
            bar_idx=2,
            ts=t0 + 120.0,
        )
        self.assertTrue(ok2, msg=r2)
        eng.process_bar(3, t0 + 180.0, 70980.0, 71000.5, 70970.0, 70990.0, context=_neutral_ctx())
        eng.process_bar(4, t0 + 240.0, 70990.0, 71450.0, 70980.0, 71420.0, context=_neutral_ctx())
        self.assertEqual(len(eng.closed_trades), 2)
        self.assertEqual(eng.closed_trades[1]["close_reason"], "STOP_LOSS")

        # Trade 3: LONG abierto que cierra por EPISODE_END al llamar finalize_episode()
        ok3, r3, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69600.0,
            tp_price=71000.0,
            invalidation_price=69400.0,
            ttl_sec=600.0,
            margin_sats=1000.0,
            leverage=25,
            bar_idx=4,
            ts=t0 + 240.0,
        )
        self.assertTrue(ok3, msg=r3)
        eng.process_bar(5, t0 + 300.0, 70020.0, 70280.0, 69999.5, 70250.0, context=_neutral_ctx())
        self.assertEqual(len(eng.running_trades), 1)

        summary = eng.finalize_episode()
        self.assertEqual(summary["n_trades"], 3)
        self.assertEqual(eng.closed_trades[2]["close_reason"], "EPISODE_END")
        self.assertLess(
            abs(summary["gross_pnl_sats"] - (summary["net_profit_sats"] + summary["total_cost_sats"])),
            1e-6,
        )
        self.assertEqual(summary["exit_reasons"], {"TP": 1, "SL": 1, "DEF_SCALP_LOCK": 0, "TIME_STOP": 0, "EOD": 1})
        self.assertEqual(sum(summary["exit_reasons"].values()), summary["n_trades"])
        self.assertGreater(summary["avg_win_net_sats"], 0.0)
        self.assertLess(summary["avg_loss_net_sats"], 0.0)
        self.assertGreater(summary["avg_win_gross_pct"], 0.0)
        self.assertLess(summary["avg_loss_gross_pct"], 0.0)


class TestPhase1B3TimeStopAndMacroParity(unittest.TestCase):
    """9. Certificación Fase 1B.3: max_hold_hours / TIME_STOP y paridad macro TF."""

    def test_max_hold_zero_crosses_utc_midnight_gt_24h_without_time_stop_or_eod(self):
        t0 = 1700002800.0  # 2023-11-14 23:00:00 UTC (1h antes de medianoche UTC 1700006400)

        def _setup_gt_24h_hold() -> ScalpSimEngine:
            eng = ScalpSimEngine(
                profile="SCALP_SWEEP_STD",
                exec_mode="LIMIT",
                lock_mode="pure_sl_tp",
                max_hold_hours=0.0,
                initial_balance_sats=100_000,
            )
            ok, reason, _ = eng.submit_signal(
                side="b",
                spot_price=70050.0,
                limit_price=70000.0,
                sl_price=69500.0,
                tp_price=71000.0,
                invalidation_price=69300.0,
                ttl_sec=600.0,
                margin_sats=1000.0,
                leverage=25,
                bar_idx=0,
                ts=t0,
            )
            self.assertTrue(ok, msg=reason)
            eng.process_bar(
                1,
                t0 + 60.0,
                70010.0,
                70020.0,
                69999.5,
                70010.0,
                context=_neutral_ctx(spot=70010.0),
            )
            for idx in range(2, 1602):
                px = 70000.0 + 150.0 * math.sin(idx / 20.0)
                eng.process_bar(
                    idx,
                    t0 + idx * 60.0,
                    px,
                    px + 40.0,
                    px - 40.0,
                    px,
                    context=_neutral_ctx(spot=px),
                )
            self.assertEqual(len(eng.running_trades), 1)
            self.assertEqual(len(eng.closed_trades), 0)
            return eng

        # Variante A: tras > 26.6h cruzando medianoche UTC, cierra en TAKE_PROFIT sin TIME_STOP ni EOD
        eng_a = _setup_gt_24h_hold()
        eng_a.process_bar(
            1602,
            t0 + 1602 * 60.0,
            70200.0,
            71050.0,
            70100.0,
            71020.0,
            context=_neutral_ctx(spot=71020.0),
        )
        ep_a = eng_a.finalize_episode()
        self.assertEqual(len(eng_a.closed_trades), 1)
        self.assertEqual(eng_a.closed_trades[0]["close_reason"], "TAKE_PROFIT")
        self.assertEqual(
            ep_a["exit_reasons"],
            {"TP": 1, "SL": 0, "DEF_SCALP_LOCK": 0, "TIME_STOP": 0, "EOD": 0},
        )

        # Variante B: tras > 26.6h sin tocar SL/TP, finalize_episode cierra como EPISODE_END (EOD)
        eng_b = _setup_gt_24h_hold()
        ep_b = eng_b.finalize_episode()
        self.assertEqual(len(eng_b.closed_trades), 1)
        self.assertEqual(eng_b.closed_trades[0]["close_reason"], "EPISODE_END")
        self.assertEqual(
            ep_b["exit_reasons"],
            {"TP": 0, "SL": 0, "DEF_SCALP_LOCK": 0, "TIME_STOP": 0, "EOD": 1},
        )

    def test_max_hold_hours_4_closes_as_time_stop_not_eod_and_cli_exit_split(self):
        t0 = 1700002800.0
        eng = ScalpSimEngine(
            profile="SCALP_SWEEP_STD",
            exec_mode="LIMIT",
            lock_mode="pure_sl_tp",
            max_hold_hours=4.0,
            initial_balance_sats=100_000,
        )
        ok, reason, _ = eng.submit_signal(
            side="b",
            spot_price=70050.0,
            limit_price=70000.0,
            sl_price=69500.0,
            tp_price=71000.0,
            invalidation_price=69300.0,
            ttl_sec=600.0,
            margin_sats=1000.0,
            leverage=25,
            bar_idx=0,
            ts=t0,
        )
        self.assertTrue(ok, msg=reason)

        open_ts = t0 + 60.0
        eng.process_bar(1, open_ts, 70010.0, 70020.0, 69999.5, 70010.0, context=_neutral_ctx(spot=70010.0))
        self.assertEqual(len(eng.running_trades), 1)

        # Hasta open_ts + 14340.0 (3h59m) sigue abierta
        for idx in range(2, 241):
            ts_bar = t0 + idx * 60.0
            eng.process_bar(idx, ts_bar, 70010.0, 70040.0, 69990.0, 70020.0, context=_neutral_ctx(spot=70020.0))
        self.assertEqual(eng.current_ts, open_ts + 14340.0)
        self.assertEqual(len(eng.running_trades), 1)
        self.assertEqual(len(eng.closed_trades), 0)

        # Barra a open_ts + 14400.0 (4h00m) dispara TIME_STOP inmediatamente
        eng.process_bar(
            241,
            open_ts + 14400.0,
            70020.0,
            70050.0,
            69990.0,
            70030.0,
            context=_neutral_ctx(spot=70030.0),
        )
        self.assertEqual(len(eng.running_trades), 0)
        self.assertEqual(len(eng.closed_trades), 1)
        self.assertEqual(eng.closed_trades[0]["close_reason"], "TIME_STOP")

        ep = eng.finalize_episode()
        self.assertEqual(
            ep["exit_reasons"],
            {"TP": 0, "SL": 0, "DEF_SCALP_LOCK": 0, "TIME_STOP": 1, "EOD": 0},
        )

        acc = CellAccumulator(
            cell_id="scalp|S0|16bps|SCALP_SWEEP_STD|LIMIT|pure_sl_tp|off|hold4h",
            mode="scalping",
            scope="fold1",
            scenario="S0",
            fee_rt_bps=16.0,
            taker_fee_rate=0.0008,
            initial_balance=100_000,
            params={"profile": "SCALP_SWEEP_STD", "exec_mode": "LIMIT", "max_hold_hours": 4.0},
        )
        desc_mem = BlockDesc(
            index=0,
            block_key="B_OOS_01",
            label="OOS_01",
            pkl_path="unused.pkl",
            is_oos_12m=True,
            is_m16=False,
            oos_month_bucket="2025-01",
        )
        acc.record_block(
            desc=desc_mem,
            n_trades=int(ep["n_trades"]),
            wins=int(ep["wins"]),
            net_sats=int(round(ep["net_profit_sats"])),
            b_win=int(round(ep["gross_win_sats"])),
            b_loss=int(round(ep["gross_loss_sats"])),
            dd_pct=float(ep["max_drawdown_pct"]),
            blk_ddg=int(ep["dd_guard_events"]),
            floor_violated=bool(ep["hard_floor_violated"]),
            cost_sats=float(ep["total_cost_sats"]),
            submitted=int(ep["orders_submitted"]),
            filled=int(ep["orders_filled"]),
            gross_pnl_sats=float(ep["gross_pnl_sats"]),
            exit_reasons=dict(ep["exit_reasons"]),
            macro_regime_counts=dict(ep["macro_regime_counts"]),
        )
        final_acc = acc.finalize()
        self.assertEqual(final_acc["ExitSplit"], "0/0/0/1/0")
        self.assertEqual(set(final_acc["macro_regime_counts"].keys()), set(MACRO_REGIME_KEYS))

        # Verificar grid CLI _run_scalping_mode con max_hold_hours_list=[0.0, 16.0] sobre 1 bloque .pkl temporal
        tiny_candles = [
            Candle(
                timestamp=t0 + i * 60.0,
                open=70000.0,
                high=70020.0,
                low=69980.0,
                close=70000.0,
                volume=10.0,
            )
            for i in range(15)
        ]
        blk = _make_precomputed_scalp_block(tiny_candles, atr_5m=180.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            pkl_path = os.path.join(tmpdir, "tiny_scalp.pkl")
            with open(pkl_path, "wb") as fh:
                pickle.dump(blk, fh)
            desc_tmp = BlockDesc(
                index=0,
                block_key="B_TMP",
                label="TMP",
                pkl_path=pkl_path,
                is_oos_12m=True,
                is_m16=False,
                oos_month_bucket="2025-01",
            )
            rows = _run_scalping_mode(
                descs=[desc_tmp],
                scope="fold1",
                scenarios=[("S0", MicrostructureStressConfig.s0_nominal())],
                fee_rt_list=[16.0],
                profiles=["SCALP_SWEEP_STD"],
                exec_modes=["LIMIT"],
                initial_balance=100_000,
                quiet=True,
                lock_modes=["pure_sl_tp"],
                macro_filters=["off"],
                rolling_pause_reset=True,
                max_hold_hours_list=[0.0, 16.0],
            )
        self.assertEqual(len(rows), 2)
        by_hold = {r["params"]["max_hold_hours"]: r for r in rows}
        self.assertEqual(set(by_hold.keys()), {0.0, 16.0})
        self.assertIn("|hold0h", by_hold[0.0]["cell_id"])
        self.assertIn("|hold16h", by_hold[16.0]["cell_id"])

    def test_macro_classifier_parity_normal_vol_and_pkl_unscaled_dict_sanitization(self):
        t0 = 1700006400.0  # frontera limpia UTC 00:00 / 4h / 1h
        builder = CausalScalpContextBuilder()
        check_bars = {180, 360, 720, 1080, 1440}
        checked = 0
        checked_4h = 0
        for i in range(1441):
            c_px = 70000.0 + 180.0 * math.sin(i / 25.0)
            ctx = builder.step(
                t0 + i * 60.0,
                c_px,
                c_px + 35.0,
                c_px - 35.0,
                c_px,
                volume=12.0,
            )
            if i in check_bars:
                checked += 1
                self.assertNotEqual(ctx["ind_1h"]["market_regime"], "EXTREME_VOLATILITY")
                self.assertNotEqual(ctx["ind_4h"]["market_regime"], "EXTREME_VOLATILITY")
                self.assertEqual(
                    MarketRegimeClassifier.classify(ctx["ind_5m"], ctx["ind_15m"], ctx["ind_1h"])["market_regime"],
                    ctx["perception"]["market_regime"],
                )
                self.assertEqual(
                    MarketRegimeClassifier.classify(scale_macro_ind_for_regime(ctx["ind_1h"], "1h"))["market_regime"],
                    ctx["ind_1h"]["market_regime"],
                )
                if "atr_pct" in ctx["ind_4h"]:
                    checked_4h += 1
                    self.assertEqual(
                        MarketRegimeClassifier.classify(scale_macro_ind_for_regime(ctx["ind_4h"], "4h"))["market_regime"],
                        ctx["ind_4h"]["market_regime"],
                    )
        self.assertEqual(checked, len(check_bars))
        self.assertEqual(checked_4h, 3)

        # Caso exacto de .pkl con umbrales 5m sin escalar estampados en ind_1h/ind_4h
        pkl_ind_1h = {
            "price": 70000.0,
            "atr": 455.0,
            "atr_pct": 0.0065,
            "atr_rel_ratio": 1.05,
            "bb_width_pct": 0.014,
            "bb_expansion_rate": 1.0,
            "ema9": 70030.0,
            "ema21": 70000.0,
            "ema_score": 0.0,
            "regime": "RANGE_NORMAL",
            "market_regime": "RANGE",
        }
        pkl_ind_4h = {
            "price": 70000.0,
            "atr": 910.0,
            "atr_pct": 0.0130,
            "atr_rel_ratio": 1.05,
            "bb_width_pct": 0.025,
            "bb_expansion_rate": 1.0,
            "ema9": 70040.0,
            "ema21": 70000.0,
            "ema_score": 0.0,
            "regime": "CHOP_HIGHVOL",
            "market_regime": "EXTREME_VOLATILITY",
        }
        orig_1h = dict(pkl_ind_1h)
        orig_4h = dict(pkl_ind_4h)

        san_builder = CausalScalpContextBuilder()
        ctx_san = san_builder.step(
            t0,
            70000.0,
            70030.0,
            69970.0,
            70000.0,
            volume=10.0,
            ind_1h=pkl_ind_1h,
            ind_4h=pkl_ind_4h,
        )
        self.assertEqual(pkl_ind_1h, orig_1h)
        self.assertEqual(pkl_ind_4h, orig_4h)
        self.assertNotEqual(ctx_san["ind_4h"]["market_regime"], "EXTREME_VOLATILITY")

        for mf in ("counter_block", "range_only"):
            eng = ScalpSimEngine(
                profile="SCALP_SWEEP_STD",
                exec_mode="LIMIT",
                macro_filter=mf,
                initial_balance_sats=100_000,
            )
            ok, reason, _ = eng.submit_signal(
                side="b",
                spot_price=70000.0,
                atr_5m=150.0,
                limit_price=69980.0,
                sl_price=69500.0,
                tp_price=71000.0,
                invalidation_price=69300.0,
                ttl_sec=600.0,
                margin_sats=1000.0,
                leverage=25,
                bar_idx=0,
                ts=t0,
                context=ctx_san,
            )
            self.assertTrue(ok, msg=f"{mf}: {reason}")
            self.assertEqual(eng.orders_submitted, 1)
            self.assertEqual(eng.rejected_reasons.get("MACRO_EXTREME_VOL", 0), 0)
            ep = eng.finalize_episode()
            self.assertEqual(ep["macro_regime_counts"]["EXTREME_VOLATILITY"], 0)
            self.assertEqual(sum(ep["macro_regime_counts"].values()), eng.signals_total)

        self.assertEqual(pkl_ind_1h, orig_1h)
        self.assertEqual(pkl_ind_4h, orig_4h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
