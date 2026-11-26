#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_binance_micro_sidecar.py
Suite determinista 100% offline (sin red ni credenciales, reloj inyectado) para Fase 4:
Sidecar Desacoplado Binance Futures (shadow_only) + integración de telemetría en LNM Sniper.
"""
import asyncio
import inspect
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import binance_micro_sidecar as sidecar
from binance_micro_sidecar import (
    BinanceMicroAggregator,
    WS_OP_CLOSE,
    WS_OP_CONT,
    WS_OP_PING,
    WS_OP_PONG,
    WS_OP_TEXT,
    build_masked_pong_frame,
    compute_websocket_accept,
    consume_ws_frames,
    decode_ws_frame_bytes,
    encode_ws_frame,
    read_binance_micro_snapshot,
    resolve_binance_snapshot_path,
    write_binance_micro_snapshot,
)


class FakeClock:
    """Reloj determinista inyectable sin dependencia de time.sleep."""

    def __init__(self, initial_ts: float = 1_700_000_000.0) -> None:
        self.now: float = float(initial_ts)

    def __call__(self) -> float:
        return self.now

    def advance(self, delta_sec: float) -> float:
        self.now += float(delta_sec)
        return self.now


class TestBinanceMicroSidecar(unittest.TestCase):
    def test_01_cvd_1m_5m_rolling_windows_and_force_order_liquidations(self) -> None:
        """Valida cálculo exacto de CVD 1m/5m (m=False vs m=True), forceOrder y expiración causal."""
        clock = FakeClock(1_700_100_000.0)
        agg = BinanceMicroAggregator(symbol="BTCUSDT", clock_fn=clock)

        # t = 0.0s: compra agresiva (m=False) 0.20 BTC @ $65,000 = +$13,000
        ok1 = agg.on_ws_message(
            json.dumps(
                {
                    "stream": "btcusdt@aggTrade",
                    "data": {
                        "e": "aggTrade",
                        "s": "BTCUSDT",
                        "p": "65000.00",
                        "q": "0.200",
                        "m": False,
                    },
                }
            )
        )
        self.assertTrue(ok1)

        # t = +10.0s: venta agresiva (m=True) 0.10 BTC @ $65,100 = -$6,510
        clock.advance(10.0)
        ok2 = agg.on_ws_message(
            json.dumps(
                {
                    "stream": "btcusdt@aggTrade",
                    "data": {
                        "e": "aggTrade",
                        "s": "BTCUSDT",
                        "p": "65100.00",
                        "q": "0.100",
                        "m": True,
                    },
                }
            )
        )
        self.assertTrue(ok2)

        # t = +15.0s: liquidaciones forceOrder BUY (ap=65250 * 0.50 = $32,625) y SELL (p=64800 * 0.25 = $16,200)
        clock.advance(5.0)
        self.assertTrue(
            agg.on_ws_message(
                json.dumps(
                    {
                        "stream": "btcusdt@forceOrder",
                        "data": {
                            "e": "forceOrder",
                            "o": {
                                "s": "BTCUSDT",
                                "S": "BUY",
                                "p": "65200.00",
                                "ap": "65250.00",
                                "q": "0.500",
                            },
                        },
                    }
                )
            )
        )
        self.assertTrue(
            agg.on_ws_message(
                json.dumps(
                    {
                        "e": "forceOrder",
                        "o": {
                            "s": "BTCUSDT",
                            "S": "SELL",
                            "p": "64800.00",
                            "ap": "0.00",
                            "q": "0.250",
                        },
                    }
                )
            )
        )

        # Símbolo ajeno debe ignorarse por completo
        self.assertFalse(
            agg.on_agg_trade({"s": "ETHUSDT", "p": "3500.0", "q": "10.0", "m": False})
        )

        snap_t15 = agg.build_snapshot(connected=True)
        self.assertAlmostEqual(snap_t15["buy_vol_1m_usd"], 13000.0, places=4)
        self.assertAlmostEqual(snap_t15["sell_vol_1m_usd"], 6510.0, places=4)
        self.assertAlmostEqual(snap_t15["cvd_1m_usd"], 6490.0, places=4)
        self.assertAlmostEqual(snap_t15["cvd_5m_usd"], 6490.0, places=4)
        self.assertAlmostEqual(snap_t15["last_price"], 65100.0, places=2)
        self.assertEqual(snap_t15["trades_1m"], 2)
        self.assertAlmostEqual(snap_t15["liq_buy_1m_usd"], 32625.0, places=4)
        self.assertAlmostEqual(snap_t15["liq_sell_1m_usd"], 16200.0, places=4)
        self.assertAlmostEqual(snap_t15["liq_buy_5m_usd"], 32625.0, places=4)
        self.assertAlmostEqual(snap_t15["liq_sell_5m_usd"], 16200.0, places=4)
        self.assertEqual(snap_t15["liq_count_1m"], 2)

        # Avanzar a t = +80.0s (> 60s desde t=0..15s, pero < 300s): 1m expira, 5m conserva
        clock.advance(65.0)
        agg.on_agg_trade({"s": "BTCUSDT", "p": "65000.0", "q": "0.050", "m": True})  # -$3,250
        snap_t80 = agg.build_snapshot(connected=True)
        self.assertAlmostEqual(snap_t80["buy_vol_1m_usd"], 0.0, places=4)
        self.assertAlmostEqual(snap_t80["sell_vol_1m_usd"], 3250.0, places=4)
        self.assertAlmostEqual(snap_t80["cvd_1m_usd"], -3250.0, places=4)
        self.assertEqual(snap_t80["trades_1m"], 1)
        self.assertAlmostEqual(snap_t80["liq_buy_1m_usd"], 0.0, places=4)
        self.assertAlmostEqual(snap_t80["liq_sell_1m_usd"], 0.0, places=4)
        self.assertEqual(snap_t80["liq_count_1m"], 0)

        # Ventana 5m mantiene todo: +13000 - 6510 - 3250 = +3240
        self.assertAlmostEqual(snap_t80["cvd_5m_usd"], 3240.0, places=4)
        self.assertAlmostEqual(snap_t80["liq_buy_5m_usd"], 32625.0, places=4)
        self.assertAlmostEqual(snap_t80["liq_sell_5m_usd"], 16200.0, places=4)

        # Avanzar +305s adicionales (> 300s para todos): vaciado exacto sin deriva flotante
        clock.advance(305.0)
        snap_empty = agg.build_snapshot(connected=True)
        self.assertEqual(snap_empty["cvd_1m_usd"], 0.0)
        self.assertEqual(snap_empty["cvd_5m_usd"], 0.0)
        self.assertEqual(snap_empty["buy_vol_1m_usd"], 0.0)
        self.assertEqual(snap_empty["sell_vol_1m_usd"], 0.0)
        self.assertEqual(snap_empty["liq_buy_5m_usd"], 0.0)
        self.assertEqual(snap_empty["liq_sell_5m_usd"], 0.0)
        self.assertEqual(snap_empty["trades_1m"], 0)
        self.assertEqual(snap_empty["liq_count_1m"], 0)
        self.assertAlmostEqual(snap_empty["last_price"], 65000.0, places=2)

    def test_02_rfc6455_ws_frames_text_masked_pong_and_close_offline(self) -> None:
        """Verifica handshake key RFC 6455, texto (corto/16-bit/fragmentado), ping->pong enmascarado y close."""
        # Vector oficial RFC 6455 Sección 1.3
        self.assertEqual(
            compute_websocket_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

        # Frame texto extendido (> 125 bytes -> longitud 16-bit) servidor -> cliente (sin máscara)
        big_json = json.dumps({"stream": "btcusdt@aggTrade", "pad": "X" * 220}).encode("utf-8")
        srv_text_frame = encode_ws_frame(WS_OP_TEXT, big_json, fin=True, masked=False)
        parsed_txt, consumed = decode_ws_frame_bytes(srv_text_frame)
        self.assertEqual(consumed, len(srv_text_frame))
        self.assertTrue(parsed_txt.fin)
        self.assertFalse(parsed_txt.masked)
        self.assertEqual(parsed_txt.opcode, WS_OP_TEXT)
        self.assertEqual(parsed_txt.payload, big_json)

        # Verificación directa del bit MASK (0x80) en pong cliente -> servidor
        fixed_mask = b"\xaa\xbb\xcc\xdd"
        ping_payload = b"binance-ping-99"
        raw_pong = build_masked_pong_frame(ping_payload, mask_key=fixed_mask)
        self.assertEqual(raw_pong[0], 0x80 | WS_OP_PONG)
        self.assertEqual(raw_pong[1] & 0x80, 0x80)  # bit MASK activo obligatoriamente
        self.assertEqual(raw_pong[2:6], fixed_mask)
        self.assertNotEqual(raw_pong[6:], ping_payload)  # bytes en cable están enmascarados
        decoded_pong, _ = decode_ws_frame_bytes(raw_pong)
        self.assertTrue(decoded_pong.masked)
        self.assertEqual(decoded_pong.opcode, WS_OP_PONG)
        self.assertEqual(decoded_pong.payload, ping_payload)

        # Simulación completa en memoria con StreamReader: texto + fragmentación + ping intercalado + close
        async def _drive_stream() -> tuple[list, list, tuple[str, int]]:
            reader = asyncio.StreamReader()
            sent_raw = []
            received_texts = []

            msg1 = b'{"e":"aggTrade","p":"64900","q":"0.1","m":false}'
            frag_full = b'{"e":"forceOrder","o":{"s":"BTCUSDT","S":"BUY","p":"65000","q":"0.2"}}'
            close_payload = struct.pack("!H", 1001) + b"going-away"

            wire = b"".join(
                [
                    encode_ws_frame(WS_OP_TEXT, msg1, fin=True, masked=False),
                    encode_ws_frame(WS_OP_TEXT, frag_full[:24], fin=False, masked=False),
                    encode_ws_frame(WS_OP_PING, b"hb-payload", fin=True, masked=False),
                    encode_ws_frame(WS_OP_CONT, frag_full[24:], fin=True, masked=False),
                    encode_ws_frame(WS_OP_CLOSE, close_payload, fin=True, masked=False),
                ]
            )
            reader.feed_data(wire)
            reader.feed_eof()

            res = await consume_ws_frames(
                reader=reader,
                send_raw_fn=sent_raw.append,
                on_text_fn=received_texts.append,
                mask_key=b"\x11\x22\x33\x44",
            )
            return sent_raw, received_texts, res

        sent_raw, received_texts, end_status = asyncio.run(_drive_stream())
        self.assertEqual(end_status, ("CLOSE", 1001))
        self.assertEqual(len(received_texts), 2)
        self.assertEqual(json.loads(received_texts[0])["p"], "64900")
        self.assertEqual(json.loads(received_texts[1])["o"]["S"], "BUY")

        # Deben haberse emitido exactamente 2 frames cliente->servidor enmascarados: PONG y CLOSE
        self.assertEqual(len(sent_raw), 2)
        pong_out, _ = decode_ws_frame_bytes(sent_raw[0])
        close_out, _ = decode_ws_frame_bytes(sent_raw[1])
        self.assertTrue(pong_out.masked)
        self.assertEqual(pong_out.opcode, WS_OP_PONG)
        self.assertEqual(pong_out.payload, b"hb-payload")
        self.assertTrue(close_out.masked)
        self.assertEqual(close_out.opcode, WS_OP_CLOSE)
        self.assertEqual(struct.unpack("!H", close_out.payload[:2])[0], 1001)

    def test_03_atomic_write_mtime_cache_ttl_expiration_and_corrupt_tolerance(self) -> None:
        """Valida FileLockContext, caché por mtime (0 re-parseos), TTL > 15s y JSON ausente/corrupto."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = resolve_binance_snapshot_path(state_dir=tmpdir)

            # 1. Archivo inexistente -> available=False, stale=True
            missing = read_binance_micro_snapshot(state_path=snap_path, now_ts=1_700_200_000.0)
            self.assertFalse(missing["available"])
            self.assertTrue(missing["stale"])
            self.assertIsNone(missing["age_sec"])

            # 2. Escritura atómica con FileLockContext
            t0 = 1_700_200_000.0
            clock = FakeClock(t0)
            agg = BinanceMicroAggregator(clock_fn=clock)
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.10", "m": False})
            agg.update_rest_metrics(funding_rate=0.000125, mark_price=65060.0, open_interest_btc=84512.34)
            payload = agg.build_snapshot(connected=True, now_ts=t0, ttl_sec=15.0)
            written = write_binance_micro_snapshot(payload, state_path=snap_path)
            self.assertEqual(written, snap_path)
            self.assertTrue(os.path.exists(f"{snap_path}.lock"))
            leftovers = [f for f in os.listdir(tmpdir) if ".tmp." in f]
            self.assertEqual(leftovers, [])

            # 3. Caché por mtime: primera lectura parsea JSON; lecturas repetidas NO llaman json.loads
            with sidecar._SNAPSHOT_CACHE_LOCK:
                sidecar._SNAPSHOT_READ_CACHE.pop(snap_path, None)

            with patch.object(sidecar.json, "loads", wraps=json.loads) as spy_loads:
                r1 = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 2.0, ttl_sec=15.0)
                self.assertEqual(spy_loads.call_count, 1)
                self.assertTrue(r1["available"])
                self.assertFalse(r1["stale"])
                self.assertAlmostEqual(r1["age_sec"], 2.0, places=3)
                self.assertAlmostEqual(r1["cvd_1m_usd"], 6506.5, places=2)

                for step in range(10):
                    r_hit = read_binance_micro_snapshot(
                        state_path=snap_path,
                        now_ts=t0 + 3.0 + step * 0.5,
                        ttl_sec=15.0,
                    )
                    self.assertTrue(r_hit["available"])
                    self.assertFalse(r_hit["stale"])
                self.assertEqual(spy_loads.call_count, 1, "No debe re-parsear JSON cuando mtime no cambia")

                # 4. Frontera exacta TTL = 15.0 s (válido) vs TTL > 15.0 s (stale=True, incluso desde caché mtime)
                r_exact_15 = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 15.0, ttl_sec=15.0)
                self.assertFalse(r_exact_15["stale"])
                self.assertTrue(r_exact_15["available"])

                r_expired = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 15.001, ttl_sec=15.0)
                self.assertTrue(r_expired["stale"])
                self.assertFalse(r_expired["available"])
                self.assertEqual(spy_loads.call_count, 1)

            # 5. Nueva escritura atómica actualiza mtime e invalida caché limpiamente
            clock.advance(5.0)
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65200.0", "q": "0.10", "m": False})
            write_binance_micro_snapshot(agg.build_snapshot(connected=True, ttl_sec=15.0), state_path=snap_path)
            r_updated = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 6.0, ttl_sec=15.0)
            self.assertTrue(r_updated["available"])
            self.assertAlmostEqual(r_updated["last_price"], 65200.0, places=2)
            self.assertAlmostEqual(r_updated["cvd_1m_usd"], 13026.5, places=2)

            # 6. Manejo seguro ante JSON truncado/corrupto o tipo inválido
            for bad_raw in (b'{"schema_version": 1, "updated_at_ts":', b'"not-an-object"', b""):
                with open(snap_path, "wb") as bfh:
                    bfh.write(bad_raw)
                st_cur = os.stat(snap_path)
                os.utime(snap_path, ns=(st_cur.st_atime_ns, st_cur.st_mtime_ns + 5_000_000))
                corrupt_res = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 7.0, ttl_sec=15.0)
                self.assertFalse(corrupt_res["available"])
                self.assertTrue(corrupt_res["stale"])

    def test_04_shadow_only_telemetry_isolation_and_basis_bps(self) -> None:
        """Verifica bloque `binance_micro` en `lnm_sniper_v6_advanced.py` y aislamiento total de decisiones."""
        import lnm_sniper_v6_advanced as v6

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = resolve_binance_snapshot_path(state_dir=tmpdir)
            t0 = 1_700_300_000.0
            agg = BinanceMicroAggregator(clock_fn=lambda: t0)
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.2", "m": False})
            agg.on_force_order({"o": {"s": "BTCUSDT", "S": "SELL", "p": "64900.0", "q": "0.1"}})
            agg.update_rest_metrics(funding_rate=-0.00005, mark_price=65060.0, open_interest_btc=79123.5)
            write_binance_micro_snapshot(agg.build_snapshot(connected=True, now_ts=t0), state_path=snap_path)

            tel = v6._build_binance_micro_telemetry(65000.0, now=t0 + 1.25, state_path=snap_path)
            expected_keys = {
                "available",
                "stale",
                "age_sec",
                "basis_bps",
                "cvd_1m_usd",
                "cvd_5m_usd",
                "liq_buy_1m_usd",
                "liq_sell_1m_usd",
                "funding_rate",
                "open_interest_btc",
            }
            self.assertEqual(set(tel.keys()), expected_keys)
            self.assertTrue(tel["available"])
            self.assertFalse(tel["stale"])
            self.assertAlmostEqual(tel["age_sec"], 1.25, places=3)
            # (65065 - 65000) / 65000 * 10000 = +10.0 bps
            self.assertAlmostEqual(tel["basis_bps"], 10.0, places=2)
            self.assertAlmostEqual(tel["cvd_1m_usd"], 13013.0, places=2)
            self.assertAlmostEqual(tel["liq_sell_1m_usd"], 6490.0, places=2)
            self.assertAlmostEqual(tel["funding_rate"], -0.00005, places=8)
            self.assertAlmostEqual(tel["open_interest_btc"], 79123.5, places=4)

        # Garantía estática P0: jamás invocar lector Binance dentro de ruta crítica de trading
        for critical_fn in (
            v6.AutonomousGoalBrain.choose_action,
            v6.execute_strategy_guardian,
            v6.evaluate_and_dispatch_entries,
            v6.compute_live_edge,
        ):
            src = inspect.getsource(critical_fn)
            self.assertNotIn("read_binance_micro_snapshot", src)
            self.assertNotIn("_build_binance_micro_telemetry", src)

    def test_05_latency_benchmark_read_snapshot_under_300us(self) -> None:
        """Benchmark de latencia de `read_binance_micro_snapshot()`: P95 < 0.300 ms."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = resolve_binance_snapshot_path(state_dir=tmpdir)
            t0 = 1_700_400_000.0
            agg = BinanceMicroAggregator(clock_fn=lambda: t0)
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65000.0", "q": "0.25", "m": False})
            write_binance_micro_snapshot(agg.build_snapshot(connected=True, now_ts=t0), state_path=snap_path)

            for i in range(30):
                _ = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 0.01 * i, ttl_sec=15.0)

            samples_ms = []
            for i in range(1000):
                ts_q = t0 + (i % 100) * 0.1
                t_start = time.perf_counter()
                res = read_binance_micro_snapshot(state_path=snap_path, now_ts=ts_q, ttl_sec=15.0)
                samples_ms.append((time.perf_counter() - t_start) * 1000.0)
                if not res["available"]:
                    self.fail("Snapshot válido dentro de TTL reportado no disponible en benchmark")

            samples_ms.sort()
            p50_ms = samples_ms[int(0.50 * len(samples_ms))]
            p95_ms = samples_ms[int(0.95 * len(samples_ms))]
            p99_ms = samples_ms[int(0.99 * len(samples_ms))]
            print(
                f"\n[BENCHMARK Fase 4] read_binance_micro_snapshot "
                f"P50={p50_ms:.4f} ms | P95={p95_ms:.4f} ms | P99={p99_ms:.4f} ms"
            )
            self.assertLess(
                p95_ms,
                0.300,
                f"Latencia P95 de read_binance_micro_snapshot excede 0.300 ms: {p95_ms:.4f} ms",
            )

    def test_06_cli_help_and_dry_check_offline(self) -> None:
        """Ejecuta `--help` y `--dry-check` de `binance_micro_sidecar.py` sin red ni credenciales."""
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

        res_help = subprocess.run(
            [sys.executable, "-B", "binance_micro_sidecar.py", "--help"],
            cwd=repo_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=10.0,
        )
        self.assertEqual(res_help.returncode, 0, msg=res_help.stderr)
        self.assertIn("--dry-check", res_help.stdout)
        self.assertIn("--state-dir", res_help.stdout)

        res_dry = subprocess.run(
            [sys.executable, "-B", "binance_micro_sidecar.py", "--dry-check"],
            cwd=repo_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=10.0,
        )
        self.assertEqual(res_dry.returncode, 0, msg=res_dry.stderr)
        self.assertIn("[DRY-CHECK OK]", res_dry.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
