#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_scalp_sim_engine.py
Suite hermética de certificación cuantitativa y funcional para Carril B (Scalp)
en simulation/scalp_sim_engine.py, simulation/lane_budget.py y su integración
con simulation/run_phase5b_v2_lab.py.
"""
from __future__ import annotations

import math
import os
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
    build_arg_parser,
)
from simulation.scalp_sim_engine import (
    SCALP_PROFILES,
    SCALP_PROFILE_ORDER,
    ScalpSimEngine,
    compute_fer,
    compute_scalp_inverse_pnl,
    compute_scalp_loss_at_sl_sats,
    get_scalp_profile,
    run_scalp_cells_on_block,
)
from simulation.sim_lnm_exchange import MicrostructureStressConfig
from simulation.sim_market_feed import Candle


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
        expected = ("SCALP_SWEEP_TIGHT", "SCALP_SWEEP_STD", "SCALP_VWAP_TD9", "SCALP_SWEEP_EXT")
        self.assertEqual(SCALP_PROFILE_ORDER, expected)
        self.assertEqual(set(SCALP_PROFILES.keys()), set(expected))

        for name in expected:
            cfg = get_scalp_profile(name)
            self.assertGreaterEqual(cfg.min_sl_pct, 0.0045 - 1e-12)
            self.assertLessEqual(cfg.max_sl_pct, 0.0065 + 1e-12)
            self.assertLessEqual(cfg.min_sl_pct, cfg.max_sl_pct)

            self.assertGreaterEqual(cfg.min_tp_pct, 0.0095 - 1e-12)
            self.assertLessEqual(cfg.max_tp_pct, 0.0160 + 1e-12)
            self.assertLessEqual(cfg.min_tp_pct, cfg.max_tp_pct)

            self.assertGreaterEqual(cfg.target_rr, 1.9 - 1e-12)
            self.assertLessEqual(cfg.target_rr, 2.5 + 1e-12)

            eng = ScalpSimEngine(profile=name, exec_mode="LIMIT")
            for atr_val in (1.0, 200.0, 5000.0):
                for side in ("b", "s"):
                    sl_px, tp_px = eng._compute_clamped_bracket(side, 70000.0, atr_val)
                    sl_pct = abs(70000.0 - sl_px) / 70000.0
                    tp_pct = abs(tp_px - 70000.0) / 70000.0
                    self.assertGreaterEqual(sl_pct, 0.0045 - 1e-4)
                    self.assertLessEqual(sl_pct, 0.0065 + 1e-4)
                    self.assertGreaterEqual(tp_pct, 0.0095 - 1e-4)
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
        # Barras 67..90: expansión alcista hacia TP (+1.3%)
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
                    )
                final_rows.append(acc.finalize())

            table_str = _format_table(final_rows)
            for col in ("Fill%", "FER", "PF", "OOS_sin_M16", "MaxDD%", "WR%", "Net_18M", "OOS_12M"):
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

        proc = subprocess.run(
            [sys.executable, "-B", "simulation/run_phase5b_v2_lab.py", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        for token in ("scalping", "--mode", "--scalp-profiles", "--exec-modes"):
            self.assertIn(token, proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
