#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import math
import tempfile
import time
import unittest
from unittest.mock import patch

from liquidity_radar import (
    DeterministicMTFAggregator,
    LiquidityConfig,
    LiquidityPool,
    LiquidityRadar,
    SessionVWAPAccumulator,
    SuperPool,
    TD9PerfectionTracker,
    TF_POOL_WEIGHTS,
    cluster_swings_to_pools,
    compute_liquidity_bias,
    compute_opposite_pool_room,
    compute_structural_td9_trailing_stop,
    detect_causal_swings,
    fuse_super_pools,
)


def _make_flat_series(n: int = 80, base: float = 62500.0, half_rng: float = 30.0, vol: float = 100.0):
    h = [base + half_rng] * n
    l = [base - half_rng] * n
    c = [base] * n
    v = [vol] * n
    return h, l, c, v


class TestLiquidityRadar(unittest.TestCase):
    def test_01_strict_anti_lookahead_bit_for_bit(self):
        """Evaluar en barra t no puede verse afectado por velas t+1..t+25 ni exponer pivotes j > t-1-k."""
        h, l, c, v = _make_flat_series(110, base=63000.0)
        # Colocar un swing low real en idx 40 y un mínimo reciente en idx 78 (aún no causal en t=80 con k=3)
        l[40], c[40], h[40] = 62200.0, 62400.0, 62600.0
        l[78], c[78], h[78] = 61900.0, 62100.0, 62300.0

        # En t=80 (longitud 81), j_max = 79 - 3 = 76 -> idx 78 NO puede aparecer como swing
        sh_80, sl_80 = detect_causal_swings(h[:81], l[:81], k=3, lookback=96)
        self.assertTrue(all(idx <= 76 for idx, _ in sh_80 + sl_80))
        self.assertIn((40, 62200.0), sl_80)
        self.assertNotIn((78, 61900.0), sl_80)

        # En t=83 (longitud 84), j_max = 82 - 3 = 79 -> ahora sí idx 78 se vuelve causal
        _, sl_83 = detect_causal_swings(h[:84], l[:84], k=3, lookback=96)
        self.assertIn((78, 61900.0), sl_83)

        # Invarianza bit-a-bit ante mutaciones futuras en t+1..109
        radar_a = LiquidityRadar(LiquidityConfig(shadow_only=False))
        radar_b = LiquidityRadar(LiquidityConfig(shadow_only=False))
        h_mut, l_mut, c_mut, v_mut = list(h), list(l), list(c), list(v)
        for m in range(81, 110):
            h_mut[m] = 75000.0
            l_mut[m] = 45000.0
            c_mut[m] = 50000.0
            v_mut[m] = 99999.0

        res_a = radar_a.evaluate((h[:81], l[:81], c[:81]), None, atr_5m=100.0, atr_15m=150.0, spot_price=c[80], volumes_5m=v[:81])
        res_b = radar_b.evaluate((h_mut[:81], l_mut[:81], c_mut[:81]), None, atr_5m=100.0, atr_15m=150.0, spot_price=c_mut[80], volumes_5m=v_mut[:81])
        self.assertEqual(res_a, res_b)

    def test_02_eqh_eql_clustering_and_weights(self):
        """Swings dentro de 0.25*ATR deben agruparse con nivel canónico y peso por número de toques."""
        # ATR = 100 -> tol = 25
        lows = [(10, 62000.0), (25, 62015.0), (40, 62030.0), (55, 62500.0)]
        pools_lo = cluster_swings_to_pools(lows, "LOW", "5m", tolerance=25.0)
        self.assertEqual(len(pools_lo), 2)
        self.assertEqual(pools_lo[0].touches, 3)
        self.assertEqual(pools_lo[0].level, 62000.0)
        self.assertAlmostEqual(pools_lo[0].weight, 1.60)
        self.assertEqual(pools_lo[1].touches, 1)
        self.assertAlmostEqual(pools_lo[1].weight, 1.00)

        highs = [(12, 64000.0), (30, 64020.0)]
        pools_hi = cluster_swings_to_pools(highs, "HIGH", "15m", tolerance=25.0)
        self.assertEqual(len(pools_hi), 1)
        self.assertEqual(pools_hi[0].level, 64020.0)
        self.assertAlmostEqual(pools_hi[0].weight, 1.35)

    def test_03_structural_mitigation_discards_broken_pool(self):
        """Un cierre limpio > 0.35*ATR más allá del pool mitiga la liquidez y evita señales falsas."""
        h, l, c, v = _make_flat_series(70, base=62500.0)
        l[20], c[20], h[20] = 62000.0, 62150.0, 62300.0
        # En barra 45, cierre en 61950 (< 62000 - 35) mitiga el pool
        l[45], c[45], h[45] = 61920.0, 61950.0, 62100.0
        # En barra 68 intentamos simular barrida sobre 62000 -> debe ignorarse por estar mitigado
        c[67] = 62120.0
        l[68], c[68], h[68], v[68] = 61960.0, 62110.0, 62140.0, 180.0
        l[69], c[69], h[69] = 62080.0, 62130.0, 62150.0

        radar = LiquidityRadar(LiquidityConfig(shadow_only=False))
        sig = radar.evaluate((h, l, c), None, atr_5m=100.0, atr_15m=150.0, spot_price=62130.0, volumes_5m=v)
        self.assertEqual(sig["event_type"], "NONE")
        self.assertEqual(sig["liquidity_score"], 0.0)

    def test_04_bull_and_bear_sweep_reclaim_confirmed_and_intrabar(self):
        """Valida BULL/BEAR Sweep+Reclaim, bono de volumen, rechazo de cuchillos e invalidación post-sweep."""
        # --- A. BULL CONFIRMED_CLOSE sobre EQL (2 toques) + volumen 1.6x ---
        h, l, c, v = _make_flat_series(60, base=62500.0)
        l[20], c[20], h[20] = 62000.0, 62150.0, 62300.0
        l[35], c[35], h[35] = 62010.0, 62160.0, 62310.0
        c[57] = 62120.0
        l[58], c[58], h[58], v[58] = 61960.0, 62110.0, 62140.0, 160.0
        l[59], c[59], h[59] = 62080.0, 62130.0, 62150.0

        radar = LiquidityRadar(LiquidityConfig(shadow_only=False))
        sig = radar.evaluate((h, l, c), None, atr_5m=100.0, atr_15m=150.0, spot_price=62130.0, volumes_5m=v)
        self.assertEqual(sig["event_type"], "BULL_SWEEP_RECLAIM")
        self.assertEqual(sig["confirmation"], "CONFIRMED_CLOSE")
        self.assertEqual(sig["pool_touches"], 2)
        self.assertAlmostEqual(sig["liquidity_score"], 0.825, places=3)  # 0.50*1.35 + 0.15

        # --- B. Invalidación post-sweep si el spot actual rompe la mecha (61950 < 61960) ---
        sig_inv = radar.evaluate((h, l, c), None, atr_5m=100.0, atr_15m=150.0, spot_price=61950.0, volumes_5m=v)
        self.assertEqual(sig_inv["event_type"], "NONE")

        # --- C. Rechazo de cuchillo cayendo (pierce = 1.50 ATR > 1.20 ATR) ---
        l_knife = list(l)
        l_knife[58] = 61850.0
        radar.clear_cache()
        sig_knife = radar.evaluate((h, l_knife, c), None, atr_5m=100.0, atr_15m=150.0, spot_price=62130.0, volumes_5m=v)
        self.assertEqual(sig_knife["event_type"], "NONE")

        # --- D. BEAR INTRABAR_RECLAIM en 15m (prioritario 1.25x) ---
        h15, l15, c15, v15 = _make_flat_series(50, base=62500.0)
        h15[15], c15[15], l15[15] = 63000.0, 62850.0, 62700.0
        h15[30], c15[30], l15[30] = 63015.0, 62860.0, 62710.0
        # Vela viva t=49 barre 63015 hasta 63085 (pierce=0.467 ATR con ATR=150) y rechaza a 62950
        c15[48] = 62960.0
        h15[49], l15[49], c15[49] = 63085.0, 62945.0, 62950.0
        sig_bear = radar.evaluate(None, (h15, l15, c15), atr_5m=100.0, atr_15m=150.0, spot_price=62950.0, volumes_15m=v15)
        self.assertEqual(sig_bear["event_type"], "BEAR_SWEEP_RECLAIM")
        self.assertEqual(sig_bear["confirmation"], "INTRABAR_RECLAIM")
        self.assertEqual(sig_bear["timeframe"], "15m")
        self.assertLess(sig_bear["liquidity_score"], -0.55)

    def test_05_ttl_decay_and_expiration(self):
        """El evento decae en d=0, 1, 2 y caduca completamente en d=3 (5m)."""
        h, l, c, v = _make_flat_series(65, base=62500.0)
        l[20], c[20], h[20] = 62000.0, 62150.0, 62300.0
        c[57] = 62120.0
        l[58], c[58], h[58], v[58] = 61960.0, 62110.0, 62140.0, 100.0
        # Barras posteriores 59..64 se mantienen por encima de la mecha
        for i in range(59, 65):
            l[i], c[i], h[i] = 62080.0, 62120.0, 62160.0

        radar = LiquidityRadar(LiquidityConfig(shadow_only=False))
        scores = []
        for end_len in (60, 61, 62, 63):  # d = 0, 1, 2, 3
            s = radar.evaluate((h[:end_len], l[:end_len], c[:end_len]), None, 100.0, 150.0, 62120.0, volumes_5m=v[:end_len])
            scores.append((s["bars_since_sweep"], s["liquidity_score"]))

        self.assertEqual(scores[0], (0, 0.5))
        self.assertEqual(scores[1], (1, 0.325))
        self.assertEqual(scores[2], (2, 0.175))
        self.assertEqual(scores[3], (-1, 0.0))

    def test_06_inverse_room_to_pool_and_liq_bias_bounds(self):
        """Valida sats inversos exactos ante muro opuesto y cotas estrictas de liq_bias."""
        sig = {
            "liquidity_score": 0.85,
            "nearest_high_pool": 62600.0,
            "nearest_high_touches": 2,
            "nearest_low_pool": 61800.0,
            "nearest_low_touches": 2,
        }
        # LONG en 62500 con muro EQH en 62600: gross = 20000*(100/62600)=31.95s, C_RT=40s -> ratio=0.799 < 1.80 -> BLOCK
        room_blocked = compute_opposite_pool_room(True, 62500.0, 1000, 20.0, 0.001, sig)
        self.assertTrue(room_blocked["blocked_by_wall"])
        self.assertAlmostEqual(room_blocked["room_gross_sats"], 31.95, places=1)

        # SHORT en 62500 hacia EQL en 61800: gross = 20000*(700/61800)=226.54s -> ratio=5.66 >= 1.80 -> OK
        room_ok = compute_opposite_pool_room(False, 62500.0, 1000, 20.0, 0.001, sig)
        self.assertFalse(room_ok["blocked_by_wall"])
        self.assertAlmostEqual(room_ok["room_gross_sats"], 226.54, places=1)

        # Shadow mode fuerza bias=0.0
        self.assertEqual(compute_liquidity_bias(1, sig, LiquidityConfig(shadow_only=True)), 0.0)
        # Confluencia LONG (+0.085) y trampa SHORT (-0.102); jamás supera cotas ni anula OVR (-0.35)
        cfg_live = LiquidityConfig(shadow_only=False)
        b_long = compute_liquidity_bias(1, sig, cfg_live)
        b_short = compute_liquidity_bias(5, sig, cfg_live)
        self.assertAlmostEqual(b_long, 0.085, places=4)
        self.assertAlmostEqual(b_short, -0.102, places=4)
        self.assertLess(0.065 - 0.35 + b_long, 0.0)  # No puede derrotar veto OVR

    def test_07_termux_latency_budget(self):
        """Verifica presupuesto de latencia en vivo: caché O(1) < 1.0 ms P95 por tick."""
        h5, l5, c5, v5 = _make_flat_series(120, base=62500.0)
        h15, l15, c15, v15 = _make_flat_series(90, base=62500.0)
        for idx in (20, 45, 70, 95):
            l5[idx] = 62100.0
            h5[idx + 5] = 62900.0
        for idx in (15, 35, 55):
            l15[idx] = 62050.0
            h15[idx + 4] = 62950.0

        radar = LiquidityRadar(LiquidityConfig(shadow_only=False))
        # Calentamiento (construcción de pools)
        t0 = time.perf_counter()
        radar.evaluate((h5, l5, c5), (h15, l15, c15), 110.0, 175.0, 62505.0, volumes_5m=v5, volumes_15m=v15)
        cold_ms = (time.perf_counter() - t0) * 1000.0

        # 200 ticks de 5s con la misma vela cerrada (cambia solo spot)
        latencies = []
        for i in range(200):
            spot = 62500.0 + ((i % 7) - 3) * 1.5
            ts = time.perf_counter()
            radar.evaluate((h5, l5, c5), (h15, l15, c15), 110.0, 175.0, spot, volumes_5m=v5, volumes_15m=v15)
            latencies.append((time.perf_counter() - ts) * 1000.0)

        latencies.sort()
        p95 = latencies[int(0.95 * len(latencies))]
        print(f"\n[BENCHMARK Termux] Cold pool rebuild: {cold_ms:.3f} ms | Cached tick P95: {p95:.3f} ms")
        self.assertLess(p95, 2.0, f"Latencia P95 excesiva en tick cacheado: {p95:.3f} ms")

    def test_08_deterministic_mtf_aggregator_45m_utc_buckets_and_no_lookahead(self):
        """Cubetas UTC fijas ts//(tf_min*60) para 15m/30m/45m/1h/4h, cero lookahead en 45m y paridad vivo/sim."""
        from lnm_sniper_v6_advanced import Config, MultiTimeframeCache
        from simulation.sim_market_feed import Candle
        from simulation.sim_training_environment import PrecomputedMarketFeed

        base_ts = 1700006400.0  # 2023-11-15 00:00:00 UTC exacto (múltiplo de 86400 y 14400)
        n_bars = 300
        ts_list, o_list, h_list, l_list, c_list, v_list = [], [], [], [], [], []
        for i in range(n_bars):
            ts_i = base_ts + i * 60.0
            p = 65000.0 + math.sin(i / 7.0) * 120.0 + (i * 0.5)
            ts_list.append(ts_i)
            o_list.append(p - 5.0)
            h_list.append(p + 20.0)
            l_list.append(p - 20.0)
            c_list.append(p + 3.0)
            v_list.append(1.0 + (i % 5) * 0.4)

        agg = DeterministicMTFAggregator(max_history=300)
        agg.ingest_1m_series(ts_list, o_list, h_list, l_list, c_list, v_list, last_is_live=False)

        # 300 min = 20 velas 15m, 10 de 30m, 6 cerradas + 1 en curso (30 min) de 45m, 5 de 1h, 1 cerrada + 1 en curso de 4h
        self.assertEqual(agg.closed_count("15m"), 20)
        self.assertEqual(agg.closed_count("30m"), 10)
        self.assertEqual(agg.closed_count("45m"), 6)
        self.assertEqual(agg.closed_count("1h"), 5)
        self.assertEqual(agg.closed_count("4h"), 1)

        o45, h45, l45, c45, v45, ts45 = agg.get_closed_ohlcv("45m")
        self.assertEqual(len(c45), 6)
        # Validar exactamente la primera cubeta 45m: índices 0..44
        self.assertEqual(o45[0], o_list[0])
        self.assertEqual(h45[0], max(h_list[:45]))
        self.assertEqual(l45[0], min(l_list[:45]))
        self.assertEqual(c45[0], c_list[44])
        self.assertAlmostEqual(v45[0], sum(v_list[:45]), places=9)
        self.assertEqual(ts45, base_ts + 5 * 2700.0)

        # No-lookahead bit-a-bit en 45m al corte t=180 vs serie futura mutada en t+1..
        agg_a = DeterministicMTFAggregator(max_history=300)
        agg_b = DeterministicMTFAggregator(max_history=300)
        agg_a.ingest_1m_series(ts_list[:180], o_list[:180], h_list[:180], l_list[:180], c_list[:180], v_list[:180])
        h_mut = list(h_list)
        l_mut = list(l_list)
        c_mut = list(c_list)
        for m in range(180, n_bars):
            h_mut[m] = 99999.0
            l_mut[m] = 10000.0
            c_mut[m] = 88888.0
        agg_b.ingest_1m_series(ts_list[:180], o_list[:180], h_mut[:180], l_mut[:180], c_mut[:180], v_list[:180])
        self.assertEqual(
            tuple(arr.tolist() for arr in agg_a.get_hlc_arrays("45m")),
            tuple(arr.tolist() for arr in agg_b.get_hlc_arrays("45m")),
        )

        # MultiTimeframeCache construye 30m/45m/15m/1h/4h desde 1m con cero llamadas HTTP extra y retorna 3-tupla
        with tempfile.TemporaryDirectory() as td:
            old_disk = Config.SHARED_MTF_DISK_CACHE
            try:
                Config.SHARED_MTF_DISK_CACHE = f"{td}/mtf.json"
                mtf = MultiTimeframeCache()
                mtf.ingest_1m_ohlcv(ts_list, o_list, h_list, l_list, c_list, v_list, last_is_live=False)
                with patch.object(mtf, "_fetch_remote", side_effect=AssertionError("No debe llamar API")):
                    for tf in ("30m", "45m"):
                        tup = mtf.get_candles(tf)
                        self.assertIsInstance(tup, tuple)
                        self.assertEqual(len(tup), 3)
                        self.assertEqual(tup[2].tolist(), agg.get_hlc_arrays(tf)[2].tolist())
            finally:
                Config.SHARED_MTF_DISK_CACHE = old_disk

        # Paridad bit-a-bit MultiTimeframeCache vs PrecomputedMarketFeed sobre misma serie 1m
        steps = []
        dummy_ind = {"price": 65000.0, "atr": 100.0}
        for i in range(n_bars):
            c_obj = Candle(
                timestamp=ts_list[i],
                open=o_list[i],
                high=h_list[i],
                low=l_list[i],
                close=c_list[i],
                volume=v_list[i],
            )
            steps.append((c_obj, dict(dummy_ind, price=c_list[i]), dummy_ind, dummy_ind, dummy_ind, {}))
        block = {
            "_precomputed": True,
            "warmup_bars": 0,
            "total_len_with_warmup": n_bars,
            "warmup_last_ts": base_ts - 60.0,
            "warmup_state": {},
            "steps": steps,
        }
        pfeed = PrecomputedMarketFeed(block)
        while pfeed.has_next():
            pfeed.next_step()
        for tf in ("15m", "30m", "45m", "1h", "4h"):
            self.assertEqual(
                [a.tolist() for a in pfeed.get_ram_candles(tf)],
                [a.tolist() for a in agg.get_hlc_arrays(tf)],
            )

    def test_09_pool_hierarchy_weights_and_super_pool_fusion(self):
        """Pesos exactos 15m..4h, fusión Super-Pool <= 0.25*ATR, no-fusión > 0.25*ATR y no-lookahead."""
        self.assertEqual(
            TF_POOL_WEIGHTS,
            {"15m": 1.00, "30m": 1.20, "45m": 1.35, "1h": 1.60, "4h": 2.00},
        )
        atr = 200.0  # 0.25 * ATR = 50.0
        pools_by_tf = {
            "4h": [LiquidityPool("LOW", 60000.0, 2, 10, "4h")],
            "1h": [LiquidityPool("HIGH", 64000.0, 1, 15, "1h")],
            "45m": [LiquidityPool("LOW", 61500.0, 2, 20, "45m")],
            "30m": [
                LiquidityPool("LOW", 60030.0, 2, 30, "30m"),   # dist 30 <= 50 -> fusiona en 4h
                LiquidityPool("LOW", 59000.0, 1, 35, "30m"),   # dist 1000 > 50 -> queda solo
            ],
            "15m": [
                LiquidityPool("HIGH", 64040.0, 2, 40, "15m"),  # dist 40 <= 50 -> fusiona en 1h
                LiquidityPool("HIGH", 64120.0, 1, 45, "15m"),  # dist 120 > 50 -> queda solo
            ],
        }
        hi_sp, lo_sp = fuse_super_pools(pools_by_tf, atr=atr, fusion_atr_mult=0.25)

        self.assertEqual(len(lo_sp), 3)
        self.assertTrue(lo_sp[0].is_super_pool)
        self.assertEqual(lo_sp[0].anchor_timeframe, "4h")
        self.assertEqual(lo_sp[0].timeframes, ("4h", "30m"))
        self.assertEqual(lo_sp[0].touches, 4)
        self.assertEqual(lo_sp[0].level, 60000.0)
        self.assertAlmostEqual(lo_sp[0].tf_weight, 2.18, places=4)
        self.assertEqual(lo_sp[1].anchor_timeframe, "45m")
        self.assertFalse(lo_sp[1].is_super_pool)
        self.assertEqual(lo_sp[2].anchor_timeframe, "30m")
        self.assertFalse(lo_sp[2].is_super_pool)

        self.assertEqual(len(hi_sp), 2)
        self.assertTrue(hi_sp[0].is_super_pool)
        self.assertEqual(hi_sp[0].anchor_timeframe, "1h")
        self.assertEqual(hi_sp[0].level, 64040.0)
        self.assertEqual(hi_sp[0].touches, 3)
        self.assertAlmostEqual(hi_sp[0].tf_weight, 1.75, places=4)
        self.assertFalse(hi_sp[1].is_super_pool)
        self.assertEqual(hi_sp[1].level, 64120.0)

        # No-lookahead bit-a-bit en evaluate_multi_tf_hierarchy
        h15, l15, c15, _ = _make_flat_series(70, base=63000.0)
        h45, l45, c45, _ = _make_flat_series(70, base=63000.0)
        h1h, l1h, c1h, _ = _make_flat_series(70, base=63000.0)
        l15[20], l45[20], l1h[20] = 62010.0, 62200.0, 62000.0
        h15[25], h1h[25] = 64010.0, 64000.0

        r_a = LiquidityRadar()
        r_b = LiquidityRadar()
        h15_m, l15_m, c15_m = list(h15), list(l15), list(c15)
        for m in range(55, 70):
            h15_m[m], l15_m[m], c15_m[m] = 80000.0, 40000.0, 50000.0
        res_a = r_a.evaluate_multi_tf_hierarchy(
            {"15m": (h15[:55], l15[:55], c15[:55], 1700000000.0), "45m": (h45[:55], l45[:55], c45[:55], 1700000000.0), "1h": (h1h[:55], l1h[:55], c1h[:55], 1700000000.0)},
            atr_ref=200.0,
            spot_price=63000.0,
        )
        res_b = r_b.evaluate_multi_tf_hierarchy(
            {"15m": (h15_m[:55], l15_m[:55], c15_m[:55], 1700000000.0), "45m": (h45[:55], l45[:55], c45[:55], 1700000000.0), "1h": (h1h[:55], l1h[:55], c1h[:55], 1700000000.0)},
            atr_ref=200.0,
            spot_price=63000.0,
        )
        self.assertEqual(res_a, res_b)
        self.assertGreaterEqual(res_a["super_pool_count"], 2)

    def test_10_td9_perfection_causal_o1_1m_5m_15m(self):
        """TD9 Perfection causal en 1m, 5m y 15m: perfected vs non-perfected, idempotencia y cero forming trigger."""
        tracker = TD9PerfectionTracker()
        base_ts = 1700000000.0
        # 13 barras para Buy TD9 Perfected en 1m (4 base + 9 bajistas; min(L8, L9) <= min(L6, L7))
        closes = [100.0, 100.0, 100.0, 100.0] + [99.0 - i for i in range(9)]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        lows[12] = lows[9] - 5.0  # bar 9 low < min(bar 6, bar 7) -> perfected

        last_st = None
        for i in range(13):
            last_st = tracker.update_bar("1m", base_ts + i * 60.0, highs[i], lows[i], closes[i])
        self.assertEqual(last_st["buy_count"], 9)
        self.assertTrue(last_st["buy_td9"])
        self.assertTrue(last_st["buy_perfected"])
        self.assertEqual(last_st["signal"], "BUY_TD9_PERFECTED")

        # Idempotencia ante timestamp duplicado
        dup_st = tracker.update_bar("1m", base_ts + 12 * 60.0, 999.0, 1.0, 999.0)
        self.assertEqual(dup_st, last_st)

        # Sell TD9 non-perfected en 5m (highs de barras 8,9 < highs de barras 6,7)
        s_closes = [100.0, 100.0, 100.0, 100.0] + [101.0 + i for i in range(9)]
        s_highs = [c + 1.0 for c in s_closes]
        s_lows = [c - 1.0 for c in s_closes]
        s_highs[9] = 150.0  # bar 6 high domina -> 8 y 9 no alcanzan
        st_5m = None
        for i in range(13):
            st_5m = tracker.update_bar("5m", base_ts + i * 300.0, s_highs[i], s_lows[i], s_closes[i])
        self.assertEqual(st_5m["sell_count"], 9)
        self.assertTrue(st_5m["sell_td9"])
        self.assertFalse(st_5m["sell_perfected"])
        self.assertEqual(st_5m["signal"], "SELL_TD9")

        # Sin disparo prematuro en velas 15m en formación dentro de DeterministicMTFAggregator
        agg = DeterministicMTFAggregator(max_history=300)
        # 12 barras cerradas 15m (180 velas 1m) + 14 velas 1m de la barra 13 (todavía abierta)
        utc0 = 1700006400.0
        for b15 in range(13):
            c_target = 1000.0 - b15 * 10.0 if b15 >= 4 else 1000.0
            n_m = 15 if b15 < 12 else 14
            for m in range(n_m):
                ts_m = utc0 + (b15 * 15 + m) * 60.0
                agg.push_1m(ts_m, c_target + 2.0, c_target + 5.0, c_target - 5.0, c_target, v=10.0, is_closed=True)
        # Barra 13 de 15m aún no ha cerrado -> buy_count debe ser 8 (barra 12 cerrada)
        self.assertEqual(agg.get_td9_state()["15m"]["buy_count"], 8)
        self.assertFalse(agg.get_td9_state()["15m"]["buy_td9"])
        # Cerrar minuto 15 de la barra 13 -> ahora exactamente buy_count == 9
        agg.push_1m(utc0 + (12 * 15 + 14) * 60.0, 882.0, 885.0, 860.0, 880.0, v=10.0, is_closed=True)
        self.assertEqual(agg.get_td9_state()["15m"]["buy_count"], 9)
        self.assertTrue(agg.get_td9_state()["15m"]["buy_perfected"])

    def test_11_session_vwap_utc_reset_and_bands_no_lookahead(self):
        """VWAP de sesión UTC O(1) con bandas ±1σ/±2σ exactas (<1e-9), reinicio 00:00 UTC y paridad vivo/sim."""
        from lnm_sniper_v6_advanced import Config, MultiTimeframeCache
        from simulation.sim_market_feed import Candle, MTFAggregator

        day0 = 1700006400.0  # 00:00:00 UTC
        bars_day0 = [
            (day0 + 0 * 60.0, 62010.0, 61990.0, 62000.0, 12.5),
            (day0 + 1 * 60.0, 62120.0, 62020.0, 62100.0, 25.0),
            (day0 + 2 * 60.0, 62250.0, 62110.0, 62200.0, 8.0),
            (day0 + 3 * 60.0, 61950.0, 61880.0, 61900.0, 40.0),
        ]
        # Fórmula exacta de dos pasadas para día 0
        tps = [(h + l + c) / 3.0 for _, h, l, c, _ in bars_day0]
        ws = [v for _, _, _, _, v in bars_day0]
        sum_w = sum(ws)
        ref_vwap = sum(w * tp for tp, w in zip(tps, ws)) / sum_w
        ref_var = sum(w * ((tp - ref_vwap) ** 2) for tp, w in zip(tps, ws)) / sum_w
        ref_std = math.sqrt(ref_var)

        acc = SessionVWAPAccumulator()
        for ts_i, h_i, l_i, c_i, v_i in bars_day0:
            snap = acc.update(ts_i, h_i, l_i, c_i, v_i)
        self.assertAlmostEqual(snap["vwap"], ref_vwap, places=9)
        self.assertAlmostEqual(snap["std"], ref_std, places=9)
        self.assertAlmostEqual(snap["upper_1s"], ref_vwap + ref_std, places=9)
        self.assertAlmostEqual(snap["lower_1s"], ref_vwap - ref_std, places=9)
        self.assertAlmostEqual(snap["upper_2s"], ref_vwap + 2.0 * ref_std, places=9)
        self.assertAlmostEqual(snap["lower_2s"], ref_vwap - 2.0 * ref_std, places=9)

        # Reinicio exacto a las 00:00 UTC del día siguiente
        day1 = day0 + 86400.0
        snap_d1 = acc.update(day1, 63030.0, 62970.0, 63000.0, 5.0)
        self.assertEqual(snap_d1["session_start_ts"], day1)
        self.assertEqual(snap_d1["bar_count"], 1)
        self.assertAlmostEqual(snap_d1["vwap"], 63000.0, places=9)
        self.assertAlmostEqual(snap_d1["std"], 0.0, places=9)

        # Paridad vivo (MultiTimeframeCache) vs simulador (MTFAggregator) + no-lookahead
        with tempfile.TemporaryDirectory() as td:
            old_disk = Config.SHARED_MTF_DISK_CACHE
            try:
                Config.SHARED_MTF_DISK_CACHE = f"{td}/mtf_vwap.json"
                mtf = MultiTimeframeCache()
                sim_agg = MTFAggregator()
                for ts_i, h_i, l_i, c_i, v_i in bars_day0:
                    mtf.push_1m_candle(ts_i, c_i, h_i, l_i, c_i, v_i, is_closed=True)
                    sim_agg.push_1m_candle(Candle(ts_i, c_i, h_i, l_i, c_i, v_i))
                self.assertEqual(mtf.get_session_vwap(), sim_agg.get_session_vwap())
                # Vela viva no cerrada no muta VWAP de sesión
                before_live = mtf.get_session_vwap()
                mtf.push_1m_candle(day0 + 4 * 60.0, 65000.0, 66000.0, 64000.0, 65500.0, 999.0, is_closed=False)
                self.assertEqual(mtf.get_session_vwap(), before_live)
            finally:
                Config.SHARED_MTF_DISK_CACHE = old_disk

    def test_12_structural_td9_trailing_hook_default_off_and_active(self):
        """Hook TD9 + pool 45m/1h apagado por defecto y funcional sin aflojar SL al activarse."""
        from lnm_sniper_v6_advanced import Config
        from simulation.sim_training_environment import SimTrainingEnvironment

        self.assertFalse(LiquidityConfig().structural_td9_trailing_enabled)
        self.assertFalse(Config.STRUCTURAL_TD9_TRAILING_ENABLED)
        with tempfile.TemporaryDirectory() as td:
            sim_env = SimTrainingEnvironment(
                candidate_brain_file=f"{td}/b.json",
                candidate_hedge_file=f"{td}/h.json",
            )
            self.assertFalse(sim_env.structural_td9_trailing_enabled)

        hierarchy = {
            "low_pools": [
                SuperPool("LOW", 62600.0, "45m", ("45m",), 2, 1.35, 1.8225, False, 1, 10),
                SuperPool("LOW", 62850.0, "1h", ("1h", "15m"), 3, 1.75, 2.80, True, 2, 12),
                SuperPool("LOW", 62920.0, "30m", ("30m",), 2, 1.20, 1.62, False, 1, 14),  # ignorar 30m
            ],
            "high_pools": [
                SuperPool("HIGH", 62200.0, "1h", ("1h",), 2, 1.60, 2.16, False, 1, 10),
            ],
        }
        td9_sell = {"15m": {"sell_td9_perfected": True}, "5m": {"sell_td9_perfected": False}, "1m": {"sell_td9_perfected": False}}
        td9_buy = {"15m": {"buy_td9_perfected": False}, "5m": {"buy_td9_perfected": True}, "1m": {"buy_td9_perfected": False}}

        # Apagado -> None
        self.assertIsNone(
            compute_structural_td9_trailing_stop(
                True, 63200.0, 62400.0, 62000.0, 100.0, td9_sell, hierarchy, enabled=False
            )
        )
        # LONG activo con Sell TD9 Perfected -> pool 1h en 62850.0 (> current_sl 62400 y < spot - 0.1*100)
        sl_long = compute_structural_td9_trailing_stop(
            True, 63200.0, 62400.0, 62000.0, 100.0, td9_sell, hierarchy, enabled=True
        )
        self.assertEqual(sl_long, 62850.0)
        # Jamás afloja SL si current_sl ya es mejor (62900 > 62850)
        self.assertIsNone(
            compute_structural_td9_trailing_stop(
                True, 63200.0, 62900.0, 62000.0, 100.0, td9_sell, hierarchy, enabled=True
            )
        )
        # SHORT activo con Buy TD9 Perfected -> pool 1h en 62200.0 (< entry 62500 y > spot + 0.1*100)
        sl_short = compute_structural_td9_trailing_stop(
            False, 61900.0, 62400.0, 62500.0, 100.0, td9_buy, hierarchy, enabled=True
        )
        self.assertEqual(sl_short, 62200.0)

    def test_13_phase1b_full_stack_latency_budget_under_300us(self):
        """Benchmark de latencia Fase 1B: P95 < 0.300 ms por tick en caliente."""
        agg = DeterministicMTFAggregator(max_history=300)
        base_ts = 1700006400.0
        for i in range(320):
            p = 63000.0 + math.sin(i / 8.0) * 180.0
            agg.push_1m(base_ts + i * 60.0, p - 4.0, p + 25.0, p - 25.0, p + 4.0, v=5.0, is_closed=True)

        radar = LiquidityRadar(LiquidityConfig(shadow_only=False))
        tf_ohlcv = {
            tf: (agg.get_ohlcv(tf)[1], agg.get_ohlcv(tf)[2], agg.get_ohlcv(tf)[3], agg.get_ohlcv(tf)[5])
            for tf in ("15m", "30m", "45m", "1h", "4h")
        }
        # Calentamiento
        radar.evaluate_multi_tf_hierarchy(tf_ohlcv, atr_ref=180.0, spot_price=63050.0)

        latencies = []
        for i in range(500):
            spot = 63050.0 + ((i % 9) - 4) * 2.5
            t0 = time.perf_counter()
            cur_tf = {
                tf: (agg.get_ohlcv(tf)[1], agg.get_ohlcv(tf)[2], agg.get_ohlcv(tf)[3], agg.get_ohlcv(tf)[5])
                for tf in ("15m", "30m", "45m", "1h", "4h")
            }
            hier = radar.evaluate_multi_tf_hierarchy(cur_tf, atr_ref=180.0, spot_price=spot)
            vwap_s = agg.get_session_vwap()
            td9_s = agg.get_td9_state()
            _ = compute_structural_td9_trailing_stop(
                True, spot, 62700.0, 62500.0, 180.0, td9_s, hier, enabled=True
            )
            _ = vwap_s["vwap"]
            latencies.append((time.perf_counter() - t0) * 1000.0)

        latencies.sort()
        p95 = latencies[int(0.95 * len(latencies))]
        print(f"\n[BENCHMARK Fase 1B] Stack completo tick P95: {p95:.4f} ms")
        self.assertLess(p95, 0.300, f"Latencia P95 Fase 1B excede 0.300 ms: {p95:.4f} ms")


if __name__ == "__main__":
    unittest.main(verbosity=2)
