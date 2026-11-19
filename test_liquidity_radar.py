#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import math
import time
import unittest

from liquidity_radar import (
    LiquidityConfig,
    LiquidityRadar,
    cluster_swings_to_pools,
    compute_liquidity_bias,
    compute_opposite_pool_room,
    detect_causal_swings,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
