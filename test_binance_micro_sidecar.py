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
    _format_sidecar_log,
    append_binance_micro_trade_event,
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
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.05", "m": True})
            agg.on_force_order({"o": {"s": "BTCUSDT", "S": "SELL", "p": "64900.0", "q": "0.1"}})
            agg.update_rest_metrics(funding_rate=-0.00005, mark_price=65060.0, open_interest_btc=79123.5)
            write_binance_micro_snapshot(agg.build_snapshot(connected=True, now_ts=t0), state_path=snap_path)

            tel = v6._build_binance_micro_telemetry(65000.0, now=t0 + 1.25, state_path=snap_path)
            expected_keys = {
                "available",
                "connected",
                "stale",
                "age_sec",
                "basis_bps",
                "trades_1m",
                "buy_vol_1m_usd",
                "sell_vol_1m_usd",
                "cvd_1m_usd",
                "cvd_5m_usd",
                "liq_buy_1m_usd",
                "liq_sell_1m_usd",
                "funding_rate",
                "open_interest_btc",
            }
            self.assertEqual(set(tel.keys()), expected_keys)
            self.assertTrue(tel["available"])
            self.assertTrue(tel["connected"])
            self.assertFalse(tel["stale"])
            self.assertAlmostEqual(tel["age_sec"], 1.25, places=3)
            # (65065 - 65000) / 65000 * 10000 = +10.0 bps
            self.assertAlmostEqual(tel["basis_bps"], 10.0, places=2)
            self.assertEqual(tel["trades_1m"], 2)
            self.assertAlmostEqual(tel["buy_vol_1m_usd"], 13013.0, places=2)
            self.assertAlmostEqual(tel["sell_vol_1m_usd"], 3253.25, places=2)
            self.assertAlmostEqual(tel["cvd_1m_usd"], 9759.75, places=2)
            self.assertAlmostEqual(tel["liq_sell_1m_usd"], 6490.0, places=2)
            self.assertAlmostEqual(tel["funding_rate"], -0.00005, places=8)
            self.assertAlmostEqual(tel["open_interest_btc"], 79123.5, places=4)

            # Fallback por snapshot inexistente
            missing_tel = v6._build_binance_micro_telemetry(
                65000.0,
                now=t0 + 1.25,
                state_path=os.path.join(tmpdir, "missing_snapshot.json"),
            )
            self.assertEqual(set(missing_tel.keys()), expected_keys)
            self.assertFalse(missing_tel["available"])
            self.assertFalse(missing_tel["connected"])
            self.assertEqual(missing_tel["trades_1m"], 0)
            self.assertEqual(missing_tel["buy_vol_1m_usd"], 0.0)
            self.assertEqual(missing_tel["sell_vol_1m_usd"], 0.0)

            # Fallback ante excepción en read_binance_micro_snapshot
            with patch.object(v6, "read_binance_micro_snapshot", side_effect=RuntimeError("boom")):
                err_tel = v6._build_binance_micro_telemetry(65000.0, now=t0 + 1.25, state_path=snap_path)
            self.assertEqual(set(err_tel.keys()), expected_keys)
            self.assertFalse(err_tel["available"])
            self.assertFalse(err_tel["connected"])
            self.assertEqual(err_tel["trades_1m"], 0)
            self.assertEqual(err_tel["buy_vol_1m_usd"], 0.0)
            self.assertEqual(err_tel["sell_vol_1m_usd"], 0.0)

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
            self.assertNotIn("append_binance_micro_trade_event", src)

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

    def test_07_ws_transient_timeout_continues_and_missing_connected_defaults_false(self) -> None:
        """Verifica ruta /market/stream, que TimeoutError transitorio no corta consume_ws_frames y connected ausente=False."""
        self.assertEqual(
            sidecar.DEFAULT_WS_URL,
            "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder",
        )
        use_ssl, host, port, path_query, host_header = sidecar._parse_ws_endpoint(sidecar.DEFAULT_WS_URL)
        self.assertTrue(use_ssl)
        self.assertEqual(host, "fstream.binance.com")
        self.assertEqual(port, 443)
        self.assertEqual(host_header, "fstream.binance.com")
        self.assertEqual(path_query, "/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder")

        sig = inspect.signature(consume_ws_frames)
        self.assertEqual(sig.parameters["read_timeout_sec"].default, 120.0)

        # 1. TimeoutError transitorio en read_ws_frame no aborta la sesión y lee el siguiente frame válido
        orig_read_ws_frame = sidecar.read_ws_frame

        async def _drive_after_transient_timeout() -> tuple[list, tuple[str, int], int]:
            reader = asyncio.StreamReader()
            received_texts = []
            sent_raw = []
            msg_ok = b'{"e":"aggTrade","p":"65125.5","q":"0.25","m":false}'
            close_payload = struct.pack("!H", 1000)
            reader.feed_data(
                encode_ws_frame(WS_OP_TEXT, msg_ok, fin=True, masked=False)
                + encode_ws_frame(WS_OP_CLOSE, close_payload, fin=True, masked=False)
            )
            reader.feed_eof()

            calls = 0

            async def _flaky_read_ws_frame(r: asyncio.StreamReader):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise TimeoutError("simulated transient socket idle")
                if calls == 2:
                    raise asyncio.TimeoutError("simulated asyncio wait_for timeout")
                return await orig_read_ws_frame(r)

            with patch.object(sidecar, "read_ws_frame", side_effect=_flaky_read_ws_frame):
                status = await consume_ws_frames(
                    reader=reader,
                    send_raw_fn=sent_raw.append,
                    on_text_fn=received_texts.append,
                    mask_key=b"\x01\x02\x03\x04",
                )
            return received_texts, status, calls

        texts, status, total_calls = asyncio.run(_drive_after_transient_timeout())
        self.assertEqual(status, ("CLOSE", 1000))
        self.assertEqual(total_calls, 4)
        self.assertEqual(len(texts), 1)
        self.assertEqual(json.loads(texts[0])["p"], "65125.5")

        # 1b. Si stop_check_fn se activa durante un TimeoutError, retorna ("STOPPED", 1000) limpiamente
        async def _drive_stop_on_timeout() -> tuple[str, int]:
            reader = asyncio.StreamReader()
            stopped = False

            async def _timeout_then_stop(_r: asyncio.StreamReader):
                nonlocal stopped
                stopped = True
                raise TimeoutError("idle before stop")

            with patch.object(sidecar, "read_ws_frame", side_effect=_timeout_then_stop):
                return await consume_ws_frames(
                    reader=reader,
                    send_raw_fn=lambda _b: None,
                    on_text_fn=lambda _t: None,
                    stop_check_fn=lambda: stopped,
                )

        self.assertEqual(asyncio.run(_drive_stop_on_timeout()), ("STOPPED", 1000))

        # 2. Snapshot válido sin clave "connected" debe reportar connected=False y available=False
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = resolve_binance_snapshot_path(state_dir=tmpdir)
            t0 = 1_700_400_000.0
            doc_without_connected = {
                "schema_version": 1,
                "updated_at_ts": t0,
                "ttl_sec": 15.0,
                "last_price": 65125.5,
                "cvd_1m_usd": 1250.0,
            }
            with open(snap_path, "wb") as fh:
                fh.write(json.dumps(doc_without_connected).encode("utf-8"))

            res = read_binance_micro_snapshot(state_path=snap_path, now_ts=t0 + 1.0, ttl_sec=15.0)
            self.assertFalse(res["stale"])
            self.assertFalse(res["connected"])
            self.assertFalse(res["available"])

    def test_08_format_sidecar_log_and_jsonl_trade_events_persistence(self) -> None:
        """Valida _format_sidecar_log determinista, persistencia JSONL OPEN/CLOSE, tolerancia a errores y rutas por entorno."""
        import lnm_sniper_v6_advanced as v6

        # 1. Prefijo determinista [YYYY-MM-DD HH:MM:SS] con now_ts fijo
        fixed_ts_1 = 1_700_500_123.0
        expected_stamp_1 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fixed_ts_1))
        formatted_1 = _format_sidecar_log("[BINANCE-SIDECAR] Inicio de prueba", now_ts=fixed_ts_1)
        self.assertEqual(formatted_1, f"[{expected_stamp_1}] [BINANCE-SIDECAR] Inicio de prueba")
        self.assertRegex(formatted_1, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")

        fixed_ts_2 = 1_700_500_456.0
        expected_stamp_2 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fixed_ts_2))
        formatted_2 = _format_sidecar_log("[BINANCE-SIDECAR] Desconexión WS", now_ts=fixed_ts_2)
        self.assertEqual(formatted_2, f"[{expected_stamp_2}] [BINANCE-SIDECAR] Desconexión WS")
        self.assertNotEqual(expected_stamp_1, expected_stamp_2)

        # 2. Persistencia JSONL OPEN y CLOSE en directorio temporal (creación de directorio padre + formato compacto UTF-8)
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = resolve_binance_snapshot_path(state_dir=tmpdir)
            t0 = 1_700_500_100.0
            agg = BinanceMicroAggregator(clock_fn=lambda: t0)
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.2", "m": False})
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.05", "m": True})
            agg.on_force_order({"o": {"s": "BTCUSDT", "S": "BUY", "p": "65100.0", "q": "0.1"}})
            agg.update_rest_metrics(funding_rate=0.0001, mark_price=65060.0, open_interest_btc=81234.5)
            write_binance_micro_snapshot(agg.build_snapshot(connected=True, now_ts=t0), state_path=snap_path)

            micro_open = v6._build_binance_micro_telemetry(65000.0, now=t0 + 1.0, state_path=snap_path)
            micro_close = v6._build_binance_micro_telemetry(65130.0, now=t0 + 5.0, state_path=snap_path)

            events_path = os.path.join(tmpdir, "nested_logs", "binance_micro_events_testnet.jsonl")
            open_record = {
                "ts": round(t0 + 1.0, 3),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 1.0)),
                "env": "testnet",
                "event": "OPEN",
                "trade_id": "uuid-open-close-001",
                "side": "buy",
                "action_id": 1,
                "action_name": "LONG_TREND",
                "state": "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL",
                "regime": "TREND_BULL",
                "lnm_price": 65000.0,
                "margin_sats": 2500,
                "leverage": 35.0,
                "binance_micro": micro_open,
            }
            close_record = {
                "ts": round(t0 + 5.0, 3),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 5.0)),
                "env": "testnet",
                "event": "CLOSE",
                "trade_id": "uuid-open-close-001",
                "side": "buy",
                "action_id": 1,
                "action_name": "LONG_TREND",
                "state": "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL",
                "regime": "TREND_BULL",
                "lnm_price": 65130.0,
                "net_realized_sats": 320,
                "net_pnl_sats": 320,
                "fee_sats": 88,
                "opening_fee": 42,
                "closing_fee": 46,
                "funding": -6,
                "mfe_sats": 410.0,
                "mae_sats": 45.5,
                "duration_ticks": 12.0,
                "exit_reason": "TAKE_PROFIT_ÓPTIMO",
                "binance_micro": micro_close,
            }

            self.assertTrue(append_binance_micro_trade_event(events_path, open_record))
            self.assertTrue(append_binance_micro_trade_event(events_path, close_record))
            self.assertTrue(os.path.isfile(events_path))

            with open(events_path, "r", encoding="utf-8") as fh:
                raw_lines = fh.readlines()
            self.assertEqual(len(raw_lines), 2)
            # Verifica separadores compactos (",", ":") y ensure_ascii=False
            self.assertNotIn(": ", raw_lines[0])
            self.assertNotIn(", ", raw_lines[0])
            self.assertIn("TAKE_PROFIT_ÓPTIMO", raw_lines[1])

            parsed_open = json.loads(raw_lines[0])
            parsed_close = json.loads(raw_lines[1])
            self.assertEqual(parsed_open["event"], "OPEN")
            self.assertEqual(parsed_open["trade_id"], "uuid-open-close-001")
            self.assertEqual(parsed_open["margin_sats"], 2500)
            self.assertAlmostEqual(parsed_open["leverage"], 35.0, places=2)
            self.assertTrue(parsed_open["binance_micro"]["available"])
            self.assertTrue(parsed_open["binance_micro"]["connected"])
            self.assertEqual(parsed_open["binance_micro"]["trades_1m"], 2)
            self.assertAlmostEqual(parsed_open["binance_micro"]["buy_vol_1m_usd"], 13013.0, places=2)
            self.assertAlmostEqual(parsed_open["binance_micro"]["sell_vol_1m_usd"], 3253.25, places=2)
            self.assertAlmostEqual(parsed_open["binance_micro"]["basis_bps"], 10.0, places=2)

            self.assertEqual(parsed_close["event"], "CLOSE")
            self.assertEqual(parsed_close["trade_id"], "uuid-open-close-001")
            self.assertEqual(parsed_close["net_realized_sats"], 320)
            self.assertEqual(parsed_close["net_pnl_sats"], 320)
            self.assertEqual(parsed_close["fee_sats"], 88)
            self.assertEqual(parsed_close["opening_fee"], 42)
            self.assertEqual(parsed_close["closing_fee"], 46)
            self.assertEqual(parsed_close["funding"], -6)
            self.assertEqual(parsed_close["exit_reason"], "TAKE_PROFIT_ÓPTIMO")
            self.assertTrue(parsed_close["binance_micro"]["available"])
            self.assertTrue(parsed_close["binance_micro"]["connected"])
            self.assertEqual(parsed_close["binance_micro"]["trades_1m"], 2)
            self.assertAlmostEqual(parsed_close["binance_micro"]["buy_vol_1m_usd"], 13013.0, places=2)
            self.assertAlmostEqual(parsed_close["binance_micro"]["sell_vol_1m_usd"], 3253.25, places=2)

            # 3. Tolerancia total a fallos en rutas inválidas sin lanzar excepción
            blocker_file = os.path.join(tmpdir, "not_a_directory")
            with open(blocker_file, "wb") as bfh:
                bfh.write(b"x")
            self.assertFalse(
                append_binance_micro_trade_event(os.path.join(blocker_file, "events.jsonl"), open_record)
            )
            self.assertFalse(append_binance_micro_trade_event(tmpdir, open_record))
            self.assertFalse(append_binance_micro_trade_event("", open_record))
            self.assertFalse(append_binance_micro_trade_event(os.path.join(tmpdir, "bad\x00.jsonl"), open_record))
            self.assertFalse(append_binance_micro_trade_event(events_path, "not-a-dict"))  # type: ignore[arg-type]

        # 4. Verificación de rutas separadas por entorno (testnet vs mainnet)
        prev_env = v6.Config.ENV_MODE
        try:
            v6.Config.configure_environment("testnet")
            tn_path = v6._resolve_binance_micro_events_path()
            self.assertTrue(
                tn_path.endswith(os.path.join("logs", "binance_micro_events_testnet.jsonl")),
                msg=f"Ruta testnet inesperada: {tn_path}",
            )
            self.assertEqual(v6.Config.BINANCE_MICRO_EVENTS_FILE, tn_path)

            v6.Config.configure_environment("mainnet")
            mn_path = v6._resolve_binance_micro_events_path()
            self.assertTrue(
                mn_path.endswith(os.path.join("logs", "binance_micro_events.jsonl")),
                msg=f"Ruta mainnet inesperada: {mn_path}",
            )
            self.assertEqual(v6.Config.BINANCE_MICRO_EVENTS_FILE, mn_path)
        finally:
            v6.Config.configure_environment(prev_env)

    def test_09_process_settlement_queue_close_jsonl_fer_and_micro_fields(self) -> None:
        """Verifica que process_settlement_queue emita CLOSE JSONL con FER (fees/funding/net_pnl_sats) y 4 campos micro."""
        import types
        import lnm_sniper_v6_advanced as v6

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_path = resolve_binance_snapshot_path(state_dir=tmpdir)
            events_path = os.path.join(tmpdir, "close_events.jsonl")
            t0 = 1_700_600_000.0
            agg = BinanceMicroAggregator(clock_fn=lambda: t0)
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.2", "m": False})
            agg.on_agg_trade({"s": "BTCUSDT", "p": "65065.0", "q": "0.05", "m": True})
            agg.update_rest_metrics(funding_rate=0.0001, mark_price=65060.0, open_interest_btc=81234.5)
            write_binance_micro_snapshot(agg.build_snapshot(connected=True, now_ts=t0), state_path=snap_path)

            closed_trade = {
                "id": "uuid-close-fer-001",
                "side": "buy",
                "pl": 410,
                "margin": 2500,
                "leverage": 35.0,
                "openingFee": 44,
                "closingFee": 46,
                "sumFundingFees": -12,
                "entryPrice": 65000.0,
                "exitPrice": 65130.0,
                "takeprofit_wi": True,
            }
            fake_client = types.SimpleNamespace(
                get_closed_trades=lambda limit=50: [closed_trade],
                get_user=lambda force_refresh=False: {"balance": 100000},
            )
            fake_brain = types.SimpleNamespace(
                active_trades={
                    "uuid-close-fer-001": {
                        "side": "buy",
                        "margin": 2500,
                        "leverage": 35.0,
                        "decision_price": 65000.0,
                        "state": "TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL",
                        "action": 1,
                        "entry_time": t0,
                        "mfe_sats": 450.0,
                        "mae_sats": 25.0,
                    }
                },
                dirty=False,
                strategies={1: {"name": "LONG_TREND"}},
                transition_audit_log=[],
                _processed_closed_trade_ids_set=set(),
                _normalize_state_key=lambda s: str(s),
                remember_and_train=lambda *a, **kw: None,
            )

            def _read_tmp_snap(state_path=None, now_ts=None, ttl_sec=15.0):
                return read_binance_micro_snapshot(state_path=snap_path, now_ts=now_ts, ttl_sec=ttl_sec)

            with patch.object(v6, "read_binance_micro_snapshot", side_effect=_read_tmp_snap):
                settled, con_losses, risk_mult, pause_until = v6.process_settlement_queue(
                    client=fake_client,
                    brain=fake_brain,
                    closed_ids=["uuid-close-fer-001"],
                    state="TREND_BULL|MID|LOW_EXP|BULL|FULL_BULL",
                    now=t0 + 2.0,
                    recovery_anchor_equity=None,
                    current_mode="STANDARD",
                    consecutive_losses=0,
                    risk_multiplier=1.0,
                    pause_until=0.0,
                    lnm_price=65130.0,
                    micro_events_log_path=events_path,
                )

            self.assertEqual(settled, 1)
            self.assertEqual(con_losses, 0)
            self.assertAlmostEqual(risk_mult, 1.0)
            self.assertEqual(pause_until, 0.0)
            self.assertTrue(os.path.isfile(events_path))

            with open(events_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertEqual(len(lines), 1)
            ev = json.loads(lines[0])
            self.assertEqual(ev["event"], "CLOSE")
            self.assertEqual(ev["trade_id"], "uuid-close-fer-001")
            self.assertEqual(ev["side"], "buy")
            # net_pnl_sats = pl(410) - (openingFee(44) + closingFee(46) + sumFundingFees(-12)) = 332
            self.assertEqual(ev["net_pnl_sats"], 332)
            self.assertEqual(ev["net_realized_sats"], 332)
            self.assertEqual(ev["opening_fee"], 44)
            self.assertEqual(ev["closing_fee"], 46)
            self.assertEqual(ev["funding"], -12)
            self.assertEqual(ev["fee_sats"], 78)

            micro = ev["binance_micro"]
            self.assertTrue(micro["available"])
            self.assertTrue(micro["connected"])
            self.assertEqual(micro["trades_1m"], 2)
            self.assertAlmostEqual(micro["buy_vol_1m_usd"], 13013.0, places=2)
            self.assertAlmostEqual(micro["sell_vol_1m_usd"], 3253.25, places=2)
            self.assertAlmostEqual(micro["basis_bps"], -9.98, places=2)


from binance_micro_sidecar import (
    DEFAULT_BINANCE_TAPE_PATH,
    DEFAULT_TAPE_MAX_BYTES,
    BinanceMicroSidecarService,
    _build_arg_parser as _sidecar_build_arg_parser,
    append_binance_micro_tape_1m,
    rotate_tape_file_if_needed,
)
from micro_flow_guard import (
    MICRO_VETO_ADV_SQUEEZE,
    MICRO_VETO_COUNTER_FLOW,
    MICRO_VETO_POST_CLIMAX_LIQ,
    MICRO_VETO_PURE_CLIMAX,
    MicroFlowGuardConfig,
    evaluate_micro_entry_guard,
    normalize_side_sign,
)
from tools import replay_micro_tape


class TestMicroFlowGuardAndTape(unittest.TestCase):
    @staticmethod
    def _base_snap(**overrides) -> dict:
        snap = {
            "available": True,
            "connected": True,
            "stale": False,
            "age_sec": 0.5,
            "trades_1m": 120,
            "buy_vol_1m_usd": 500_000.0,
            "sell_vol_1m_usd": 500_000.0,
            "cvd_5m_usd": 0.0,
            "liq_buy_1m_usd": 0.0,
            "liq_sell_1m_usd": 0.0,
        }
        snap.update(overrides)
        return snap

    @staticmethod
    def _reflect_snap(snap: dict) -> dict:
        out = dict(snap)
        out["buy_vol_1m_usd"] = snap["sell_vol_1m_usd"]
        out["sell_vol_1m_usd"] = snap["buy_vol_1m_usd"]
        out["liq_buy_1m_usd"] = snap["liq_sell_1m_usd"]
        out["liq_sell_1m_usd"] = snap["liq_buy_1m_usd"]
        out["cvd_5m_usd"] = -float(snap["cvd_5m_usd"])
        return out

    def test_01_fail_open_exhaustive_and_disabled(self) -> None:
        """Valida fail-open estricto en todas las fallas de señal y disabled."""
        self.assertEqual(normalize_side_sign(1), 1)
        self.assertEqual(normalize_side_sign(4), 1)
        self.assertEqual(normalize_side_sign(5), -1)
        self.assertEqual(normalize_side_sign(8), -1)
        self.assertEqual(normalize_side_sign(-1), -1)
        self.assertEqual(normalize_side_sign(" BUY "), 1)
        self.assertEqual(normalize_side_sign("Long"), 1)
        self.assertEqual(normalize_side_sign("sell"), -1)
        self.assertEqual(normalize_side_sign("-"), -1)
        self.assertEqual(normalize_side_sign(True), 0)
        self.assertEqual(normalize_side_sign(False), 0)
        self.assertEqual(normalize_side_sign(0), 0)
        self.assertEqual(normalize_side_sign("hold"), 0)

        dec_dis = evaluate_micro_entry_guard(
            "buy", self._base_snap(), MicroFlowGuardConfig(enabled=False)
        )
        self.assertTrue(dec_dis.allowed)
        self.assertFalse(dec_dis.vetoed)
        self.assertFalse(dec_dis.fail_open)
        self.assertEqual(dec_dis.reason, "DISABLED")

        cfg = MicroFlowGuardConfig(enabled=True, shadow_only=False)
        cases = [
            (0, self._base_snap(), "FAIL_OPEN_INVALID_SIDE"),
            (True, self._base_snap(), "FAIL_OPEN_INVALID_SIDE"),
            ("buy", None, "FAIL_OPEN_NONE"),
            ("buy", self._base_snap(connected=False), "FAIL_OPEN_DISCONNECTED"),
            ("buy", self._base_snap(available=False), "FAIL_OPEN_DISCONNECTED"),
            ("buy", self._base_snap(stale=True), "FAIL_OPEN_STALE"),
            ("buy", self._base_snap(age_sec=5.01), "FAIL_OPEN_AGE"),
            ("buy", {"connected": True, "stale": False, "trades_1m": 50}, "FAIL_OPEN_AGE"),
            ("buy", self._base_snap(trades_1m=29), "FAIL_OPEN_LOW_VOLUME"),
            (
                "buy",
                self._base_snap(buy_vol_1m_usd=100_000.0, sell_vol_1m_usd=149_999.0),
                "FAIL_OPEN_LOW_VOLUME",
            ),
            ("buy", self._base_snap(buy_vol_1m_usd=float("nan")), "FAIL_OPEN_PARSE_ERROR"),
            ("buy", self._base_snap(cvd_5m_usd=float("inf")), "FAIL_OPEN_PARSE_ERROR"),
            ("buy", self._base_snap(trades_1m=True), "FAIL_OPEN_PARSE_ERROR"),
            ("buy", self._base_snap(sell_vol_1m_usd=-1.0), "FAIL_OPEN_PARSE_ERROR"),
        ]
        for side, snap, exp_reason in cases:
            dec = evaluate_micro_entry_guard(side, snap, cfg)
            self.assertTrue(dec.allowed, msg=exp_reason)
            self.assertFalse(dec.vetoed, msg=exp_reason)
            self.assertFalse(dec.would_veto, msg=exp_reason)
            self.assertTrue(dec.fail_open, msg=exp_reason)
            self.assertEqual(dec.reason, exp_reason)
            self.assertIsNone(dec.rule_code)

        # Edad por timestamp con now_ts
        snap_ts = self._base_snap(ts=1000.0)
        snap_ts.pop("age_sec")
        dec_age = evaluate_micro_entry_guard("buy", snap_ts, cfg, now_ts=1005.5)
        self.assertTrue(dec_age.fail_open)
        self.assertEqual(dec_age.reason, "FAIL_OPEN_AGE")

    def test_02_small_liquidations_immunity_and_four_rules_symmetry(self) -> None:
        """Valida inmunidad con liq_tot < 75k USD y simetría exacta LONG vs SHORT en 4 reglas."""
        cfg = MicroFlowGuardConfig(enabled=True, shadow_only=False)

        # Inmunidad a liquidaciones pequeñas (< 75_000)
        small_liq = self._base_snap(
            buy_vol_1m_usd=200_000.0,
            sell_vol_1m_usd=600_000.0,  # i_cvd = -0.50 -> dir_cvd_1m = -0.50
            liq_buy_1m_usd=0.0,
            liq_sell_1m_usd=74_999.0,  # l_dir = -1.0 pero liq_tot < 75k
            cvd_5m_usd=-1_000_000.0,
        )
        dec_small = evaluate_micro_entry_guard("buy", small_liq, cfg)
        self.assertTrue(dec_small.allowed)
        self.assertFalse(dec_small.would_veto)
        self.assertEqual(dec_small.reason, "PASS")

        rule_snaps = [
            (
                MICRO_VETO_ADV_SQUEEZE,
                self._base_snap(
                    buy_vol_1m_usd=300_000.0,
                    sell_vol_1m_usd=700_000.0,  # dir_cvd_1m = -0.40 <= -0.15
                    liq_buy_1m_usd=10_000.0,
                    liq_sell_1m_usd=90_000.0,  # liq_tot=100k, l_dir=-0.80 <= -0.50
                    cvd_5m_usd=0.0,
                ),
            ),
            (
                MICRO_VETO_COUNTER_FLOW,
                self._base_snap(
                    buy_vol_1m_usd=300_000.0,
                    sell_vol_1m_usd=700_000.0,  # dir_cvd_1m = -0.40 <= -0.30
                    cvd_5m_usd=-30_000_000.0,  # <= -25M
                ),
            ),
            (
                MICRO_VETO_POST_CLIMAX_LIQ,
                self._base_snap(
                    buy_vol_1m_usd=650_000.0,
                    sell_vol_1m_usd=350_000.0,  # dir_cvd_1m = +0.30 >= 0.15
                    liq_buy_1m_usd=85_000.0,
                    liq_sell_1m_usd=15_000.0,  # liq_tot=100k, l_dir=+0.70 >= 0.60
                    cvd_5m_usd=40_000_000.0,  # >= 35M
                ),
            ),
            (
                MICRO_VETO_PURE_CLIMAX,
                self._base_snap(
                    buy_vol_1m_usd=800_000.0,
                    sell_vol_1m_usd=200_000.0,  # dir_cvd_1m = +0.60 >= 0.55
                    cvd_5m_usd=65_000_000.0,  # >= 60M
                ),
            ),
            (
                None,
                self._base_snap(
                    buy_vol_1m_usd=550_000.0,
                    sell_vol_1m_usd=450_000.0,
                    cvd_5m_usd=5_000_000.0,
                ),
            ),
        ]

        for exp_rule, snap in rule_snaps:
            dec_l = evaluate_micro_entry_guard("buy", snap, cfg)
            dec_s = evaluate_micro_entry_guard("sell", self._reflect_snap(snap), cfg)
            self.assertEqual(dec_l.rule_code, exp_rule)
            self.assertEqual(dec_s.rule_code, exp_rule)
            self.assertEqual(dec_l.allowed, dec_s.allowed)
            self.assertEqual(dec_l.vetoed, dec_s.vetoed)
            self.assertEqual(dec_l.would_veto, dec_s.would_veto)
            self.assertAlmostEqual(dec_l.dir_cvd_1m, dec_s.dir_cvd_1m, places=12)
            self.assertAlmostEqual(dec_l.dir_cvd_5m_usd, dec_s.dir_cvd_5m_usd, places=6)
            self.assertAlmostEqual(dec_l.l_dir, dec_s.l_dir, places=12)
            self.assertAlmostEqual(dec_l.i_cvd_1m, -dec_s.i_cvd_1m, places=12)
            self.assertAlmostEqual(dec_l.i_liq_1m, -dec_s.i_liq_1m, places=12)
            self.assertEqual(dec_l.to_dict()["rule_code"], exp_rule)

    def test_03_shadow_only_vs_active_veto(self) -> None:
        """Verifica modo sombra (allowed=True, would_veto=True) vs veto activo (allowed=False, vetoed=True)."""
        snap = self._base_snap(
            buy_vol_1m_usd=250_000.0,
            sell_vol_1m_usd=750_000.0,
            cvd_5m_usd=-30_000_000.0,
        )
        dec_shadow = evaluate_micro_entry_guard(
            1, snap, MicroFlowGuardConfig(enabled=True, shadow_only=True)
        )
        self.assertTrue(dec_shadow.allowed)
        self.assertFalse(dec_shadow.vetoed)
        self.assertTrue(dec_shadow.would_veto)
        self.assertEqual(dec_shadow.rule_code, MICRO_VETO_COUNTER_FLOW)

        dec_active = evaluate_micro_entry_guard(
            1, snap, MicroFlowGuardConfig(enabled=True, shadow_only=False)
        )
        self.assertFalse(dec_active.allowed)
        self.assertTrue(dec_active.vetoed)
        self.assertTrue(dec_active.would_veto)
        self.assertEqual(dec_active.rule_code, MICRO_VETO_COUNTER_FLOW)

    def test_04_tape_1m_writer_rotation_schema_and_oserror_tolerance(self) -> None:
        """Valida esquema exacto de 12 campos, rotación <= max_bytes (.1), tolerancia OSError y CLI."""
        expected_keys = {
            "ts",
            "price",
            "connected",
            "stale",
            "trades_1m",
            "buy_vol_1m_usd",
            "sell_vol_1m_usd",
            "cvd_1m_usd",
            "cvd_5m_usd",
            "liq_buy_1m_usd",
            "liq_sell_1m_usd",
            "basis_bps",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tape_path = os.path.join(tmpdir, "sub", "binance_micro_tape.jsonl")
            snap = {
                "last_price": 65123.5,
                "connected": True,
                "stale": False,
                "trades_1m": 45,
                "buy_vol_1m_usd": 300_000.0,
                "sell_vol_1m_usd": 200_000.0,
                "cvd_1m_usd": 100_000.0,
                "cvd_5m_usd": 250_000.0,
                "liq_buy_1m_usd": 10_000.0,
                "liq_sell_1m_usd": 5_000.0,
                "basis_bps": None,
            }
            max_b = 450
            for idx in range(5):
                self.assertTrue(
                    append_binance_micro_tape_1m(
                        tape_path,
                        snap,
                        ts=1_700_700_000 + idx * 60,
                        max_bytes=max_b,
                    )
                )
            self.assertTrue(os.path.isfile(tape_path))
            self.assertTrue(os.path.isfile(f"{tape_path}.1"))
            self.assertLessEqual(os.path.getsize(tape_path), max_b)
            self.assertLessEqual(os.path.getsize(f"{tape_path}.1"), max_b)

            with open(tape_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            self.assertGreaterEqual(len(lines), 1)
            self.assertNotIn(": ", lines[0])
            self.assertNotIn(", ", lines[0])
            row = json.loads(lines[0])
            self.assertEqual(set(row.keys()), expected_keys)
            self.assertEqual(row["basis_bps"], 0.0)
            self.assertEqual(row["price"], 65123.5)

            # Tolerancia a OSError sin lanzar excepción
            blocker = os.path.join(tmpdir, "blocker_file")
            with open(blocker, "wb") as bfh:
                bfh.write(b"x")
            self.assertFalse(
                append_binance_micro_tape_1m(os.path.join(blocker, "tape.jsonl"), snap)
            )
            self.assertFalse(append_binance_micro_tape_1m(None, snap))
            with patch("builtins.open", side_effect=OSError("disk full")):
                self.assertFalse(append_binance_micro_tape_1m(tape_path, snap))
            self.assertFalse(rotate_tape_file_if_needed(os.path.join(tmpdir, "missing.jsonl"), 100, 10))

            # Defaults de servicio y parser CLI sin romper comportamiento previo
            svc_default = BinanceMicroSidecarService(state_path=os.path.join(tmpdir, "s.json"))
            self.assertIsNone(svc_default.tape_path)
            self.assertEqual(svc_default.tape_max_bytes, DEFAULT_TAPE_MAX_BYTES)
            args = _sidecar_build_arg_parser().parse_args(["--enable-tape", "--tape-max-bytes", "1024"])
            self.assertTrue(args.enable_tape)
            self.assertIsNone(args.tape_path)
            self.assertEqual(args.tape_max_bytes, 1024)
            self.assertTrue(DEFAULT_BINANCE_TAPE_PATH.endswith("binance_micro_tape.jsonl"))

    def test_05_replay_micro_tape_cli_events_tape_and_help(self) -> None:
        """Valida CLI --help, --events (OPEN/CLOSE + FER + sats_avoided) y --tape (huecos + percentiles)."""
        repo_root = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(repo_root, "tools", "replay_micro_tape.py")
        help_proc = subprocess.run(
            [sys.executable, "-B", script_path, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(help_proc.returncode, 0, msg=help_proc.stderr)

        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = os.path.join(tmpdir, "events.jsonl")
            events_rows = [
                {
                    "ts": 1000.0,
                    "event": "OPEN",
                    "trade_id": "t1",
                    "side": "buy",
                    "binance_micro": self._base_snap(
                        buy_vol_1m_usd=300_000.0,
                        sell_vol_1m_usd=700_000.0,
                        liq_buy_1m_usd=10_000.0,
                        liq_sell_1m_usd=90_000.0,
                        cvd_5m_usd=-5_000_000.0,
                    ),
                },
                {
                    "ts": 1020.0,
                    "event": "CLOSE",
                    "trade_id": "t1",
                    "side": "buy",
                    "net_pnl_sats": -400,
                    "fee_sats": 80,
                },
                {
                    "ts": 1100.0,
                    "event": "OPEN",
                    "trade_id": "t2",
                    "side": "buy",
                    "binance_micro": self._base_snap(
                        buy_vol_1m_usd=550_000.0,
                        sell_vol_1m_usd=450_000.0,
                        cvd_5m_usd=2_000_000.0,
                    ),
                },
                {
                    "ts": 1130.0,
                    "event": "CLOSE",
                    "trade_id": "t2",
                    "side": "buy",
                    "net_pnl_sats": 320,
                    "fee_sats": 80,
                },
            ]
            with open(events_path, "w", encoding="utf-8") as fh:
                for rec in events_rows:
                    fh.write(json.dumps(rec) + "\n")
                fh.write("{malformed-json\n")

            ev_out = os.path.join(tmpdir, "ev_report.json")
            rc_ev = replay_micro_tape.main(["--events", events_path, "--json-out", ev_out])
            self.assertEqual(rc_ev, 0)
            with open(ev_out, "r", encoding="utf-8") as fh:
                ev_rep = json.load(fh)
            self.assertEqual(ev_rep["paired_trades"], 2)
            self.assertEqual(ev_rep["vetoed_count"], 1)
            self.assertEqual(ev_rep["approved_count"], 1)
            self.assertEqual(ev_rep["veto_by_rule"][MICRO_VETO_ADV_SQUEEZE], 1)
            self.assertAlmostEqual(ev_rep["net_pnl_base_sats"], -80.0)
            self.assertAlmostEqual(ev_rep["net_pnl_approved_sats"], 320.0)
            self.assertAlmostEqual(ev_rep["sats_avoided"], 400.0)
            self.assertAlmostEqual(ev_rep["fer_base"], -0.5)
            self.assertAlmostEqual(ev_rep["fer_approved"], 4.0)
            self.assertAlmostEqual(ev_rep["delta_fer"], 4.5)

            tape_path = os.path.join(tmpdir, "tape.jsonl")
            tape_rows = [
                {
                    "ts": 1700000000.0,
                    "price": 65000.0,
                    "connected": True,
                    "stale": False,
                    "trades_1m": 50,
                    "buy_vol_1m_usd": 800_000.0,
                    "sell_vol_1m_usd": 200_000.0,
                    "cvd_1m_usd": 600_000.0,
                    "cvd_5m_usd": 65_000_000.0,
                    "liq_buy_1m_usd": 20_000.0,
                    "liq_sell_1m_usd": 10_000.0,
                    "basis_bps": 1.0,
                },
                {
                    "ts": 1700000180.0,  # hueco > 90s
                    "price": 64900.0,
                    "connected": True,
                    "stale": False,
                    "trades_1m": 10,  # low_volume
                    "buy_vol_1m_usd": 100_000.0,
                    "sell_vol_1m_usd": 50_000.0,
                    "cvd_1m_usd": 50_000.0,
                    "cvd_5m_usd": -1_000_000.0,
                    "liq_buy_1m_usd": 0.0,
                    "liq_sell_1m_usd": 0.0,
                    "basis_bps": 0.0,
                },
            ]
            with open(tape_path, "w", encoding="utf-8") as fh:
                for row in tape_rows:
                    fh.write(json.dumps(row) + "\n")

            tape_out = os.path.join(tmpdir, "tape_report.json")
            rc_tape = replay_micro_tape.main(["--tape", tape_path, "--json-out", tape_out])
            self.assertEqual(rc_tape, 0)
            with open(tape_out, "r", encoding="utf-8") as fh:
                tape_rep = json.load(fh)
            self.assertEqual(tape_rep["total_minutes"], 2)
            self.assertEqual(tape_rep["gaps_gt_90s"], 1)
            self.assertEqual(tape_rep["low_volume_count"], 1)
            self.assertEqual(tape_rep["long_veto_by_rule"][MICRO_VETO_PURE_CLIMAX], 1)
            self.assertEqual(tape_rep["short_veto_by_rule"][MICRO_VETO_COUNTER_FLOW], 1)

    def test_06_micro_flow_guard_latency_benchmark(self) -> None:
        """Benchmark de >= 5,000 evaluaciones: exige P95 < 0.05 ms (50,000 ns)."""
        cfg = MicroFlowGuardConfig(enabled=True, shadow_only=False)
        snap = self._base_snap(
            buy_vol_1m_usd=600_000.0,
            sell_vol_1m_usd=400_000.0,
            cvd_5m_usd=10_000_000.0,
            liq_buy_1m_usd=50_000.0,
            liq_sell_1m_usd=30_000.0,
        )
        for _ in range(200):
            evaluate_micro_entry_guard("buy", snap, cfg)

        iters = 5_000
        lat_ns = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            evaluate_micro_entry_guard("buy", snap, cfg)
            lat_ns.append(time.perf_counter_ns() - t0)

        lat_ns.sort()
        p95_ns = lat_ns[int(0.95 * iters)]
        self.assertLess(p95_ns, 50_000, msg=f"P95 demasiado alto: {p95_ns} ns")


if __name__ == "__main__":
    unittest.main(verbosity=2)
