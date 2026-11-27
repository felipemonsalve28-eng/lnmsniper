#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
binance_micro_sidecar.py
Proceso sidecar independiente y desacoplado de microestructura Binance USD-M Futures
para LNM Sniper v7.3+ (Termux aarch64 / Linux / WSL, 100% Python stdlib, shadow_only).

Características principales:
  - Cliente WebSocket RFC 6455 minimalista sobre stdlib (asyncio + ssl) hacia
    wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder
    con handshake HTTP/1.1 Upgrade, decodificación de frames texto/continuación/ping/close,
    respuesta automática pong enmascarada (client->server), reconexión con backoff
    exponencial y parada limpia lock-free ante SIGINT/SIGTERM.
  - Agregador causal O(1) amortizado en RAM mediante collections.deque acotados por
    timestamp y aritmética entera en micro-USD (cero deriva numérica acumulada):
      * aggTrade (m=False compra agresiva, m=True venta agresiva):
        cvd_1m_usd, cvd_5m_usd, buy_vol_1m_usd, sell_vol_1m_usd, last_price, trades_1m.
      * forceOrder:
        liq_buy_1m_usd, liq_sell_1m_usd, liq_buy_5m_usd, liq_sell_5m_usd, liq_count_1m.
  - Tarea asíncrona secundaria cada 30 s hacia REST público (openInterest + premiumIndex)
    con timeout corto en hilo stdlib (urllib.request) sin bloquear el stream WebSocket.
  - Publicación atómica cada 1 s en $HOME/.sniper_state/binance_micro_snapshot.json
    (override por --state-dir o SNIPER_STATE_DIR) bajo FileLockContext + tmp + os.replace.
  - Lector O(1) read_binance_micro_snapshot() con caché por mtime_ns, tolerancia total
    ante archivo inexistente/parcial/corrupto y expiración estricta TTL > 15.0 s.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import random
import signal
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Callable, Deque, Dict, NamedTuple, Optional, Tuple

from guardian_execution_core import FileLockContext

SCHEMA_VERSION = 1
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_TTL_SEC = 15.0
DEFAULT_PUBLISH_INTERVAL_SEC = 1.0
DEFAULT_REST_INTERVAL_SEC = 30.0
DEFAULT_WS_URL = (
    "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@forceOrder"
)
DEFAULT_REST_BASE_URL = "https://fapi.binance.com"
DEFAULT_SNAPSHOT_FILENAME = "binance_micro_snapshot.json"

_WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_OP_CONT = 0x0
WS_OP_TEXT = 0x1
WS_OP_BINARY = 0x2
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA

_MICRO_USD_SCALE = 1_000_000
_SNAPSHOT_WRITE_SEQ = 0
_SNAPSHOT_CACHE_LOCK = threading.Lock()
# Mapa: abs_path -> (st_mtime_ns, st_size, parsed_dict)
_SNAPSHOT_READ_CACHE: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}


class WSProtocolError(RuntimeError):
    """Error de protocolo RFC 6455 o handshake HTTP Upgrade."""


class WSFrame(NamedTuple):
    """Representación inmutable de un frame WebSocket RFC 6455."""

    fin: bool
    opcode: int
    masked: bool
    payload: bytes


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        out = float(val)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _format_sidecar_log(msg: str, now_ts: Optional[float] = None) -> str:
    """Formatea un mensaje de log del sidecar con prefijo de timestamp `[YYYY-MM-DD HH:MM:SS]`."""
    ts = time.time() if now_ts is None else _safe_float(now_ts, 0.0)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return f"[{stamp}] {msg}"


def compute_websocket_accept(sec_key: str) -> str:
    """Calcula el valor exacto Sec-WebSocket-Accept (RFC 6455 Sección 4.2.2)."""
    digest = hashlib.sha1((str(sec_key) + _WS_MAGIC_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _xor_mask(data: bytes, mask_key: bytes) -> bytes:
    """Aplica o remueve máscara XOR de 4 bytes en tiempo lineal C-level."""
    plen = len(data)
    if plen == 0:
        return b""
    if len(mask_key) != 4:
        raise ValueError("mask_key debe tener exactamente 4 bytes")
    mask_rep = (mask_key * ((plen // 4) + 1))[:plen]
    return (
        int.from_bytes(data, "big") ^ int.from_bytes(mask_rep, "big")
    ).to_bytes(plen, "big")


def encode_ws_frame(
    opcode: int,
    payload: bytes = b"",
    fin: bool = True,
    masked: bool = True,
    mask_key: Optional[bytes] = None,
) -> bytes:
    """
    Codifica un frame WebSocket RFC 6455.
    Por defecto `masked=True` (obligatorio para todo frame cliente -> servidor).
    """
    op = int(opcode) & 0x0F
    body = bytes(payload or b"")
    if op >= 0x8:
        fin = True
        if len(body) > 125:
            raise WSProtocolError("Los frames de control RFC 6455 no pueden superar 125 bytes")

    b0 = (0x80 if fin else 0x00) | op
    plen = len(body)
    mask_bit = 0x80 if masked else 0x00

    header = bytearray([b0])
    if plen < 126:
        header.append(mask_bit | plen)
    elif plen < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", plen))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", plen))

    if masked:
        mkey = bytes(mask_key) if mask_key is not None else os.urandom(4)
        if len(mkey) != 4:
            raise ValueError("mask_key debe tener exactamente 4 bytes")
        header.extend(mkey)
        header.extend(_xor_mask(body, mkey))
    else:
        header.extend(body)
    return bytes(header)


def build_masked_pong_frame(
    ping_payload: bytes = b"",
    mask_key: Optional[bytes] = None,
) -> bytes:
    """Construye un frame PONG (0xA) enmascarado cliente->servidor replicando el payload del PING."""
    body = bytes(ping_payload or b"")[:125]
    return encode_ws_frame(
        WS_OP_PONG,
        payload=body,
        fin=True,
        masked=True,
        mask_key=mask_key,
    )


def build_masked_close_frame(
    code: int = 1000,
    reason: str = "",
    mask_key: Optional[bytes] = None,
) -> bytes:
    """Construye un frame CLOSE (0x8) enmascarado cliente->servidor."""
    reason_bytes = str(reason or "").encode("utf-8")[:123]
    payload = struct.pack("!H", int(code)) + reason_bytes
    return encode_ws_frame(
        WS_OP_CLOSE,
        payload=payload,
        fin=True,
        masked=True,
        mask_key=mask_key,
    )


def decode_ws_frame_bytes(
    buf: bytes,
    max_frame_bytes: int = 4 * 1024 * 1024,
) -> Tuple[WSFrame, int]:
    """
    Decodifica un frame RFC 6455 desde un buffer de bytes en memoria.
    Retorna `(WSFrame, bytes_consumidos)`. Lanza `EOFError` si el buffer está incompleto.
    """
    total = len(buf)
    if total < 2:
        raise EOFError("Buffer insuficiente para cabecera WS (2 bytes)")
    b0, b1 = buf[0], buf[1]
    fin = bool((b0 >> 7) & 1)
    rsv = (b0 >> 4) & 0x07
    if rsv != 0:
        raise WSProtocolError(f"Bits RSV no soportados sin extensiones: {rsv}")
    opcode = b0 & 0x0F
    is_masked = bool((b1 >> 7) & 1)
    plen = b1 & 0x7F
    idx = 2

    if op_is_ctrl := (opcode >= 0x8):
        if not fin or plen > 125:
            raise WSProtocolError("Frame de control inválido (fragmentado o > 125 bytes)")

    if plen == 126:
        if total < idx + 2:
            raise EOFError("Buffer insuficiente para longitud extendida 16-bit")
        plen = struct.unpack("!H", buf[idx : idx + 2])[0]
        idx += 2
    elif plen == 127:
        if total < idx + 8:
            raise EOFError("Buffer insuficiente para longitud extendida 64-bit")
        plen = struct.unpack("!Q", buf[idx : idx + 8])[0]
        idx += 8

    if plen > max_frame_bytes:
        raise WSProtocolError(f"Payload WS excede límite seguro ({plen} > {max_frame_bytes})")

    mask_key = b""
    if is_masked:
        if total < idx + 4:
            raise EOFError("Buffer insuficiente para mask_key de 4 bytes")
        mask_key = bytes(buf[idx : idx + 4])
        idx += 4

    if total < idx + plen:
        raise EOFError("Buffer insuficiente para payload WS completo")

    raw_payload = bytes(buf[idx : idx + plen])
    idx += plen
    payload = _xor_mask(raw_payload, mask_key) if (is_masked and plen > 0) else raw_payload
    _ = op_is_ctrl
    return WSFrame(fin=fin, opcode=opcode, masked=is_masked, payload=payload), idx


async def read_ws_frame(
    reader: asyncio.StreamReader,
    max_frame_bytes: int = 4 * 1024 * 1024,
) -> WSFrame:
    """Lee y decodifica un frame RFC 6455 completo desde un `asyncio.StreamReader`."""
    hdr = await reader.readexactly(2)
    b0, b1 = hdr[0], hdr[1]
    fin = bool((b0 >> 7) & 1)
    rsv = (b0 >> 4) & 0x07
    if rsv != 0:
        raise WSProtocolError(f"Bits RSV no soportados: {rsv}")
    opcode = b0 & 0x0F
    is_masked = bool((b1 >> 7) & 1)
    plen = b1 & 0x7F

    if opcode >= 0x8 and (not fin or plen > 125):
        raise WSProtocolError("Frame de control RFC 6455 inválido")

    if plen == 126:
        plen = struct.unpack("!H", await reader.readexactly(2))[0]
    elif plen == 127:
        plen = struct.unpack("!Q", await reader.readexactly(8))[0]

    if plen > max_frame_bytes:
        raise WSProtocolError(f"Frame excede tamaño máximo permitido: {plen}")

    mkey = await reader.readexactly(4) if is_masked else b""
    raw_payload = await reader.readexactly(plen) if plen > 0 else b""
    payload = _xor_mask(raw_payload, mkey) if (is_masked and plen > 0) else raw_payload
    return WSFrame(fin=fin, opcode=opcode, masked=is_masked, payload=payload)


async def consume_ws_frames(
    reader: asyncio.StreamReader,
    send_raw_fn: Callable[[bytes], Any],
    on_text_fn: Callable[[str], None],
    stop_check_fn: Optional[Callable[[], bool]] = None,
    read_timeout_sec: float = 120.0,
    mask_key: Optional[bytes] = None,
) -> Tuple[str, int]:
    """
    Bucle lector RFC 6455 desacoplado de sockets reales (permite test offline determinista).
    Maneja frames de texto (incluyendo fragmentación), responde PING con PONG enmascarado
    y procesa CLOSE enviando acuse CLOSE enmascarado.
    """
    frag_opcode: Optional[int] = None
    frag_buffer = bytearray()

    while True:
        if stop_check_fn is not None and stop_check_fn():
            return ("STOPPED", 1000)

        try:
            frame = await asyncio.wait_for(read_ws_frame(reader), timeout=read_timeout_sec)
        except (TimeoutError, asyncio.TimeoutError):
            if stop_check_fn is not None and stop_check_fn():
                return ("STOPPED", 1000)
            continue
        except asyncio.IncompleteReadError:
            return ("EOF", 1006)

        if frame.opcode == WS_OP_PING:
            pong = build_masked_pong_frame(frame.payload, mask_key=mask_key)
            maybe_coro = send_raw_fn(pong)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            continue

        if frame.opcode == WS_OP_PONG:
            continue

        if frame.opcode == WS_OP_CLOSE:
            close_code = 1000
            if len(frame.payload) >= 2:
                close_code = struct.unpack("!H", frame.payload[:2])[0]
            try:
                close_reply = build_masked_close_frame(code=close_code, mask_key=mask_key)
                maybe_coro = send_raw_fn(close_reply)
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception:
                pass
            return ("CLOSE", close_code)

        if frame.opcode in (WS_OP_TEXT, WS_OP_BINARY):
            if frag_opcode is not None:
                raise WSProtocolError("Nuevo mensaje de datos recibido antes de cerrar fragmento previo")
            if frame.fin:
                if frame.opcode == WS_OP_TEXT:
                    on_text_fn(frame.payload.decode("utf-8", errors="replace"))
            else:
                frag_opcode = frame.opcode
                frag_buffer.clear()
                frag_buffer.extend(frame.payload)
            continue

        if frame.opcode == WS_OP_CONT:
            if frag_opcode is None:
                raise WSProtocolError("Frame de continuación inesperado sin inicio previo")
            frag_buffer.extend(frame.payload)
            if len(frag_buffer) > 4 * 1024 * 1024:
                raise WSProtocolError("Mensaje fragmentado supera 4 MiB")
            if frame.fin:
                done_op = frag_opcode
                raw_msg = bytes(frag_buffer)
                frag_opcode = None
                frag_buffer.clear()
                if done_op == WS_OP_TEXT:
                    on_text_fn(raw_msg.decode("utf-8", errors="replace"))
            continue

        raise WSProtocolError(f"Opcode WebSocket desconocido: {frame.opcode}")


def _parse_ws_endpoint(ws_url: str) -> Tuple[bool, str, int, str, str]:
    parsed = urllib.parse.urlsplit(str(ws_url or DEFAULT_WS_URL).strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("wss", "ws"):
        raise ValueError(f"Esquema WebSocket no soportado: {scheme!r}")
    use_ssl = scheme == "wss"
    host = parsed.hostname
    if not host:
        raise ValueError(f"Host vacío en URL WebSocket: {ws_url!r}")
    port = int(parsed.port or (443 if use_ssl else 80))
    path_query = parsed.path or "/"
    if parsed.query:
        path_query = f"{path_query}?{parsed.query}"
    default_port = (use_ssl and port == 443) or ((not use_ssl) and port == 80)
    host_header = host if default_port else f"{host}:{port}"
    return use_ssl, host, port, path_query, host_header


async def perform_ws_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host_header: str,
    path_query: str,
    timeout_sec: float = 10.0,
) -> None:
    """Ejecuta el handshake cliente HTTP/1.1 Upgrade a WebSocket (RFC 6455 Sección 4.1)."""
    sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = compute_websocket_accept(sec_key)
    req_lines = (
        f"GET {path_query} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {sec_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: lnm-sniper-sidecar/1.0\r\n"
        "\r\n"
    )
    writer.write(req_lines.encode("ascii"))
    await asyncio.wait_for(writer.drain(), timeout=timeout_sec)

    raw_hdr = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout_sec)
    if len(raw_hdr) > 16384:
        raise WSProtocolError("Cabecera HTTP Upgrade demasiado extensa")

    lines = raw_hdr.decode("latin-1").split("\r\n")
    status_line = lines[0].strip() if lines else ""
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[1] != "101":
        raise WSProtocolError(f"Respuesta HTTP Upgrade inválida: {status_line!r}")

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    if headers.get("upgrade", "").lower() != "websocket":
        raise WSProtocolError("Falta cabecera 'Upgrade: websocket' en respuesta 101")
    if headers.get("sec-websocket-accept", "") != expected_accept:
        raise WSProtocolError("Sec-WebSocket-Accept inválido en handshake RFC 6455")


class BinanceMicroAggregator:
    """
    Agregador causal O(1) amortizado en RAM para flujo de órdenes y liquidaciones
    de Binance Futures (`aggTrade` y `forceOrder`).
    """

    WINDOW_1M_SEC = 60.0
    WINDOW_5M_SEC = 300.0

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        clock_fn: Optional[Callable[[], float]] = None,
        max_events: int = 250_000,
    ) -> None:
        self.symbol = str(symbol or DEFAULT_SYMBOL).strip().upper()
        self._clock: Callable[[], float] = clock_fn if clock_fn is not None else time.time
        self.max_events = max(1000, int(max_events))

        # Tuplas (ts_sec, is_buy, notional_uusd)
        self._trades_1m: Deque[Tuple[float, bool, int]] = deque()
        self._trades_5m: Deque[Tuple[float, bool, int]] = deque()
        self._liqs_1m: Deque[Tuple[float, bool, int]] = deque()
        self._liqs_5m: Deque[Tuple[float, bool, int]] = deque()

        self._buy_1m_uusd: int = 0
        self._sell_1m_uusd: int = 0
        self._buy_5m_uusd: int = 0
        self._sell_5m_uusd: int = 0

        self._liq_buy_1m_uusd: int = 0
        self._liq_sell_1m_uusd: int = 0
        self._liq_buy_5m_uusd: int = 0
        self._liq_sell_5m_uusd: int = 0

        self.last_price: float = 0.0
        self.last_event_ts: float = 0.0
        self.funding_rate: float = 0.0
        self.mark_price: float = 0.0
        self.open_interest_btc: float = 0.0
        self.rest_updated_at_ts: float = 0.0

    def _resolve_ts(self, now_ts: Optional[float]) -> float:
        return float(now_ts) if now_ts is not None else float(self._clock())

    def evict_expired(self, now_ts: Optional[float] = None) -> None:
        """Purga en O(1) amortizado todos los eventos fuera de las ventanas de 1m y 5m."""
        now = self._resolve_ts(now_ts)
        cut_1m = now - self.WINDOW_1M_SEC
        cut_5m = now - self.WINDOW_5M_SEC

        while self._trades_1m and self._trades_1m[0][0] < cut_1m:
            _, is_buy, uusd = self._trades_1m.popleft()
            if is_buy:
                self._buy_1m_uusd -= uusd
            else:
                self._sell_1m_uusd -= uusd
        if not self._trades_1m:
            self._buy_1m_uusd = 0
            self._sell_1m_uusd = 0

        while self._trades_5m and self._trades_5m[0][0] < cut_5m:
            _, is_buy, uusd = self._trades_5m.popleft()
            if is_buy:
                self._buy_5m_uusd -= uusd
            else:
                self._sell_5m_uusd -= uusd
        if not self._trades_5m:
            self._buy_5m_uusd = 0
            self._sell_5m_uusd = 0

        while self._liqs_1m and self._liqs_1m[0][0] < cut_1m:
            _, is_buy, uusd = self._liqs_1m.popleft()
            if is_buy:
                self._liq_buy_1m_uusd -= uusd
            else:
                self._liq_sell_1m_uusd -= uusd
        if not self._liqs_1m:
            self._liq_buy_1m_uusd = 0
            self._liq_sell_1m_uusd = 0

        while self._liqs_5m and self._liqs_5m[0][0] < cut_5m:
            _, is_buy, uusd = self._liqs_5m.popleft()
            if is_buy:
                self._liq_buy_5m_uusd -= uusd
            else:
                self._liq_sell_5m_uusd -= uusd
        if not self._liqs_5m:
            self._liq_buy_5m_uusd = 0
            self._liq_sell_5m_uusd = 0

    def on_agg_trade(self, data: Dict[str, Any], now_ts: Optional[float] = None) -> bool:
        """
        Registra un evento `aggTrade` de Binance Futures:
          - `m=False`: comprador agresivo (taker buy) -> suma a buy_vol y +CVD.
          - `m=True`: vendedor agresivo (taker sell) -> suma a sell_vol y -CVD.
        """
        if not isinstance(data, dict):
            return False
        sym = str(data.get("s") or self.symbol).strip().upper()
        if sym and sym != self.symbol:
            return False

        price = _safe_float(data.get("p"), 0.0)
        qty = _safe_float(data.get("q"), 0.0)
        if price <= 0.0 or qty <= 0.0:
            return False

        raw_m = data.get("m")
        if isinstance(raw_m, bool):
            is_buyer_maker = raw_m
        elif isinstance(raw_m, str):
            is_buyer_maker = raw_m.strip().lower() in ("true", "1")
        else:
            is_buyer_maker = bool(raw_m)

        is_aggressive_buy = not is_buyer_maker
        uusd = int(round(price * qty * _MICRO_USD_SCALE))
        if uusd <= 0:
            return False

        ts = self._resolve_ts(now_ts)
        self.last_price = price
        self.last_event_ts = ts

        if len(self._trades_1m) >= self.max_events:
            _, old_buy, old_uusd = self._trades_1m.popleft()
            if old_buy:
                self._buy_1m_uusd -= old_uusd
            else:
                self._sell_1m_uusd -= old_uusd
        if len(self._trades_5m) >= self.max_events:
            _, old_buy5, old_uusd5 = self._trades_5m.popleft()
            if old_buy5:
                self._buy_5m_uusd -= old_uusd5
            else:
                self._sell_5m_uusd -= old_uusd5

        entry = (ts, is_aggressive_buy, uusd)
        self._trades_1m.append(entry)
        self._trades_5m.append(entry)
        if is_aggressive_buy:
            self._buy_1m_uusd += uusd
            self._buy_5m_uusd += uusd
        else:
            self._sell_1m_uusd += uusd
            self._sell_5m_uusd += uusd

        self.evict_expired(ts)
        return True

    def on_force_order(self, data: Dict[str, Any], now_ts: Optional[float] = None) -> bool:
        """Registra una liquidación `forceOrder` (`S="BUY"` o `S="SELL"`)."""
        if not isinstance(data, dict):
            return False
        order = data.get("o") if isinstance(data.get("o"), dict) else data
        sym = str(order.get("s") or data.get("s") or self.symbol).strip().upper()
        if sym and sym != self.symbol:
            return False

        side = str(order.get("S") or order.get("side") or "").strip().upper()
        if side not in ("BUY", "SELL"):
            return False

        avg_px = _safe_float(order.get("ap"), 0.0)
        ord_px = _safe_float(order.get("p"), 0.0)
        eff_px = avg_px if avg_px > 0.0 else ord_px
        qty = _safe_float(order.get("q") or order.get("z") or order.get("l"), 0.0)
        if eff_px <= 0.0 or qty <= 0.0:
            return False

        uusd = int(round(eff_px * qty * _MICRO_USD_SCALE))
        if uusd <= 0:
            return False

        is_buy_liq = side == "BUY"
        ts = self._resolve_ts(now_ts)
        self.last_event_ts = ts

        if len(self._liqs_1m) >= self.max_events:
            _, old_b1, old_u1 = self._liqs_1m.popleft()
            if old_b1:
                self._liq_buy_1m_uusd -= old_u1
            else:
                self._liq_sell_1m_uusd -= old_u1
        if len(self._liqs_5m) >= self.max_events:
            _, old_b5, old_u5 = self._liqs_5m.popleft()
            if old_b5:
                self._liq_buy_5m_uusd -= old_u5
            else:
                self._liq_sell_5m_uusd -= old_u5

        entry = (ts, is_buy_liq, uusd)
        self._liqs_1m.append(entry)
        self._liqs_5m.append(entry)
        if is_buy_liq:
            self._liq_buy_1m_uusd += uusd
            self._liq_buy_5m_uusd += uusd
        else:
            self._liq_sell_1m_uusd += uusd
            self._liq_sell_5m_uusd += uusd

        self.evict_expired(ts)
        return True

    def on_ws_message(self, raw_text: str, now_ts: Optional[float] = None) -> bool:
        """Procesa un mensaje JSON crudo del stream combinado o directo de Binance Futures."""
        try:
            doc = json.loads(raw_text)
        except (TypeError, ValueError):
            return False
        if not isinstance(doc, dict):
            return False

        stream_name = str(doc.get("stream") or "").lower()
        payload = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        ev_type = str(payload.get("e") or "").strip()

        if ev_type == "aggTrade" or stream_name.endswith("@aggtrade"):
            return self.on_agg_trade(payload, now_ts=now_ts)
        if ev_type == "forceOrder" or stream_name.endswith("@forceorder"):
            return self.on_force_order(payload, now_ts=now_ts)
        return False

    def update_rest_metrics(
        self,
        funding_rate: Optional[float] = None,
        mark_price: Optional[float] = None,
        open_interest_btc: Optional[float] = None,
        now_ts: Optional[float] = None,
    ) -> None:
        if funding_rate is not None and math.isfinite(float(funding_rate)):
            self.funding_rate = float(funding_rate)
        if mark_price is not None and float(mark_price) > 0.0 and math.isfinite(float(mark_price)):
            self.mark_price = float(mark_price)
        if (
            open_interest_btc is not None
            and float(open_interest_btc) >= 0.0
            and math.isfinite(float(open_interest_btc))
        ):
            self.open_interest_btc = float(open_interest_btc)
        self.rest_updated_at_ts = self._resolve_ts(now_ts)

    def build_snapshot(
        self,
        connected: bool = True,
        now_ts: Optional[float] = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> Dict[str, Any]:
        """Genera el diccionario atómico listo para serializar en `binance_micro_snapshot.json`."""
        now = self._resolve_ts(now_ts)
        self.evict_expired(now)

        buy_1m = round(self._buy_1m_uusd / _MICRO_USD_SCALE, 4)
        sell_1m = round(self._sell_1m_uusd / _MICRO_USD_SCALE, 4)
        cvd_1m = round((self._buy_1m_uusd - self._sell_1m_uusd) / _MICRO_USD_SCALE, 4)
        cvd_5m = round((self._buy_5m_uusd - self._sell_5m_uusd) / _MICRO_USD_SCALE, 4)

        liq_buy_1m = round(self._liq_buy_1m_uusd / _MICRO_USD_SCALE, 4)
        liq_sell_1m = round(self._liq_sell_1m_uusd / _MICRO_USD_SCALE, 4)
        liq_buy_5m = round(self._liq_buy_5m_uusd / _MICRO_USD_SCALE, 4)
        liq_sell_5m = round(self._liq_sell_5m_uusd / _MICRO_USD_SCALE, 4)

        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": self.symbol,
            "updated_at_ts": round(now, 6),
            "ttl_sec": float(ttl_sec),
            "connected": bool(connected),
            "last_price": round(self.last_price, 2),
            "mark_price": round(self.mark_price, 2),
            "cvd_1m_usd": cvd_1m,
            "cvd_5m_usd": cvd_5m,
            "buy_vol_1m_usd": buy_1m,
            "sell_vol_1m_usd": sell_1m,
            "trades_1m": int(len(self._trades_1m)),
            "liq_buy_1m_usd": liq_buy_1m,
            "liq_sell_1m_usd": liq_sell_1m,
            "liq_buy_5m_usd": liq_buy_5m,
            "liq_sell_5m_usd": liq_sell_5m,
            "liq_count_1m": int(len(self._liqs_1m)),
            "funding_rate": round(self.funding_rate, 8),
            "open_interest_btc": round(self.open_interest_btc, 4),
            "rest_updated_at_ts": round(self.rest_updated_at_ts, 3),
        }


def resolve_binance_snapshot_path(
    state_path: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> str:
    """Resuelve la ruta canónica de `binance_micro_snapshot.json` en `$HOME/.sniper_state/`."""
    if state_path:
        return os.path.abspath(str(state_path))
    eff_dir = state_dir or os.getenv("SNIPER_STATE_DIR")
    if not eff_dir:
        eff_dir = os.path.join(os.path.expanduser("~"), ".sniper_state")
    return os.path.abspath(os.path.join(str(eff_dir), DEFAULT_SNAPSHOT_FILENAME))


def write_binance_micro_snapshot(
    payload: Dict[str, Any],
    state_path: Optional[str] = None,
    state_dir: Optional[str] = None,
    lock_timeout: float = 1.5,
) -> str:
    """
    Escribe atómicamente `binance_micro_snapshot.json` usando `FileLockContext` +
    archivo temporal en el mismo directorio + `os.fsync` + `os.replace`.
    """
    global _SNAPSHOT_WRITE_SEQ
    target = resolve_binance_snapshot_path(state_path=state_path, state_dir=state_dir)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    _SNAPSHOT_WRITE_SEQ = (_SNAPSHOT_WRITE_SEQ + 1) & 0x7FFFFFFF
    tmp_path = f"{target}.tmp.{os.getpid()}.{threading.get_ident()}.{_SNAPSHOT_WRITE_SEQ}"
    raw_bytes = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

    try:
        with FileLockContext(target, exclusive=True, timeout=lock_timeout, raise_on_timeout=True):
            prev_mtime_ns = 0
            try:
                prev_mtime_ns = os.stat(target).st_mtime_ns
            except OSError:
                prev_mtime_ns = 0

            with open(tmp_path, "wb") as fh:
                fh.write(raw_bytes)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, target)

            # Garantiza avance monótono de st_mtime_ns en sistemas de archivos con resolución gruesa
            try:
                cur_st = os.stat(target)
                if prev_mtime_ns > 0 and cur_st.st_mtime_ns <= prev_mtime_ns:
                    bumped_ns = prev_mtime_ns + 1_000_000
                    os.utime(target, ns=(cur_st.st_atime_ns, bumped_ns))
            except OSError:
                pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return target


def _empty_snapshot_view(ttl_sec: float = DEFAULT_TTL_SEC) -> Dict[str, Any]:
    return {
        "available": False,
        "stale": True,
        "connected": False,
        "age_sec": None,
        "ttl_sec": float(ttl_sec),
        "updated_at_ts": 0.0,
        "last_price": 0.0,
        "mark_price": 0.0,
        "cvd_1m_usd": 0.0,
        "cvd_5m_usd": 0.0,
        "buy_vol_1m_usd": 0.0,
        "sell_vol_1m_usd": 0.0,
        "trades_1m": 0,
        "liq_buy_1m_usd": 0.0,
        "liq_sell_1m_usd": 0.0,
        "liq_buy_5m_usd": 0.0,
        "liq_sell_5m_usd": 0.0,
        "liq_count_1m": 0,
        "funding_rate": 0.0,
        "open_interest_btc": 0.0,
    }


def read_binance_micro_snapshot(
    state_path: Optional[str] = None,
    now_ts: Optional[float] = None,
    ttl_sec: float = DEFAULT_TTL_SEC,
) -> Dict[str, Any]:
    """
    Lee `binance_micro_snapshot.json` con caché O(1) por `(st_mtime_ns, st_size)`.
    Nunca lanza excepción ante archivo inexistente, parcial o corrupto; retorna
    `{"available": False, "stale": True, ...}` cuando no existe o `age_sec > ttl_sec`.
    """
    eff_ttl = max(0.001, _safe_float(ttl_sec, DEFAULT_TTL_SEC))
    target = (
        os.path.abspath(state_path)
        if state_path
        else resolve_binance_snapshot_path()
    )

    try:
        st = os.stat(target)
    except OSError:
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_READ_CACHE.pop(target, None)
        return _empty_snapshot_view(ttl_sec=eff_ttl)

    if st.st_size <= 0 or st.st_size > 1_048_576:
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_READ_CACHE.pop(target, None)
        return _empty_snapshot_view(ttl_sec=eff_ttl)

    doc: Optional[Dict[str, Any]] = None
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_READ_CACHE.get(target)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            doc = cached[2]

    if doc is None:
        try:
            with open(target, "rb") as fh:
                raw = fh.read()
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            with _SNAPSHOT_CACHE_LOCK:
                _SNAPSHOT_READ_CACHE.pop(target, None)
            return _empty_snapshot_view(ttl_sec=eff_ttl)

        if not isinstance(parsed, dict):
            with _SNAPSHOT_CACHE_LOCK:
                _SNAPSHOT_READ_CACHE.pop(target, None)
            return _empty_snapshot_view(ttl_sec=eff_ttl)

        doc = parsed
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_READ_CACHE[target] = (st.st_mtime_ns, st.st_size, doc)

    updated_at = _safe_float(doc.get("updated_at_ts"), 0.0)
    if updated_at <= 0.0:
        return _empty_snapshot_view(ttl_sec=eff_ttl)

    now = float(now_ts) if now_ts is not None else time.time()
    age_sec = max(0.0, now - updated_at)
    is_stale = age_sec > eff_ttl
    connected = bool(doc.get("connected", False))
    is_available = (not is_stale) and connected

    return {
        "available": is_available,
        "stale": is_stale,
        "connected": connected,
        "age_sec": round(age_sec, 4),
        "ttl_sec": eff_ttl,
        "updated_at_ts": updated_at,
        "last_price": _safe_float(doc.get("last_price"), 0.0),
        "mark_price": _safe_float(doc.get("mark_price"), 0.0),
        "cvd_1m_usd": _safe_float(doc.get("cvd_1m_usd"), 0.0),
        "cvd_5m_usd": _safe_float(doc.get("cvd_5m_usd"), 0.0),
        "buy_vol_1m_usd": _safe_float(doc.get("buy_vol_1m_usd"), 0.0),
        "sell_vol_1m_usd": _safe_float(doc.get("sell_vol_1m_usd"), 0.0),
        "trades_1m": max(0, _safe_int(doc.get("trades_1m"), 0)),
        "liq_buy_1m_usd": _safe_float(doc.get("liq_buy_1m_usd"), 0.0),
        "liq_sell_1m_usd": _safe_float(doc.get("liq_sell_1m_usd"), 0.0),
        "liq_buy_5m_usd": _safe_float(doc.get("liq_buy_5m_usd"), 0.0),
        "liq_sell_5m_usd": _safe_float(doc.get("liq_sell_5m_usd"), 0.0),
        "liq_count_1m": max(0, _safe_int(doc.get("liq_count_1m"), 0)),
        "funding_rate": _safe_float(doc.get("funding_rate"), 0.0),
        "open_interest_btc": _safe_float(doc.get("open_interest_btc"), 0.0),
    }


def append_binance_micro_trade_event(
    log_path: str,
    event_record: Dict[str, Any],
) -> bool:
    """
    Agrega una línea JSONL UTF-8 compacta (`ensure_ascii=False`, `separators=(",", ":")`)
    para auditoría `shadow_only` de microestructura Binance por trade (`OPEN`/`CLOSE`).
    Crea el directorio padre si falta y nunca lanza excepciones hacia el llamador.
    """
    try:
        if not isinstance(log_path, (str, os.PathLike)):
            return False
        target = os.fspath(log_path)
        if not isinstance(target, str) or not target.strip():
            return False
        if not isinstance(event_record, dict):
            return False
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = json.dumps(
            event_record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False


from typing import Mapping  # noqa: E402

DEFAULT_TAPE_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BINANCE_TAPE_PATH = os.path.join("logs", "binance_micro_tape.jsonl")


def rotate_tape_file_if_needed(
    tape_path: str,
    max_bytes: int = DEFAULT_TAPE_MAX_BYTES,
    incoming_bytes: int = 0,
) -> bool:
    """Rota `<tape_path>` -> `<tape_path>.1` en O(1) si supera `max_bytes`; nunca lanza excepción."""
    try:
        if not isinstance(tape_path, (str, os.PathLike)):
            return False
        target = os.fspath(tape_path)
        if not isinstance(target, str) or not target.strip():
            return False
        eff_max = int(max_bytes)
        if eff_max <= 0:
            return False
        eff_incoming = max(0, int(incoming_bytes))
        if not os.path.exists(target):
            return False
        if os.path.getsize(target) + eff_incoming > eff_max:
            os.replace(target, f"{target}.1")
            return True
        return False
    except Exception:
        return False


def append_binance_micro_tape_1m(
    tape_path: Optional[str],
    snapshot: Mapping[str, Any],
    *,
    ts: Optional[float] = None,
    max_bytes: int = DEFAULT_TAPE_MAX_BYTES,
) -> bool:
    """
    Graba una línea JSONL de 12 campos por minuto cerrado con rotación O(1) segura.
    Nunca lanza excepción ante errores de disco o permisos.
    """
    try:
        if tape_path is None or not isinstance(tape_path, (str, os.PathLike)):
            return False
        target = os.fspath(tape_path)
        if not isinstance(target, str) or not target.strip():
            return False
        if not isinstance(snapshot, Mapping):
            return False

        abs_target = os.path.abspath(target)
        parent = os.path.dirname(abs_target)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if ts is not None:
            eff_ts = _safe_float(ts, time.time())
        else:
            raw_ts = snapshot.get("ts")
            if raw_ts is None:
                raw_ts = snapshot.get("updated_at_ts")
            eff_ts = _safe_float(raw_ts, time.time())

        raw_price = snapshot.get("price")
        if raw_price is None:
            raw_price = snapshot.get("last_price")
        price = _safe_float(raw_price, 0.0)
        if price <= 0.0 and snapshot.get("mark_price") is not None:
            price = _safe_float(snapshot.get("mark_price"), 0.0)

        record = {
            "ts": eff_ts,
            "price": price,
            "connected": bool(snapshot.get("connected", False)),
            "stale": bool(snapshot.get("stale", False)),
            "trades_1m": max(0, _safe_int(snapshot.get("trades_1m"), 0)),
            "buy_vol_1m_usd": _safe_float(snapshot.get("buy_vol_1m_usd"), 0.0),
            "sell_vol_1m_usd": _safe_float(snapshot.get("sell_vol_1m_usd"), 0.0),
            "cvd_1m_usd": _safe_float(snapshot.get("cvd_1m_usd"), 0.0),
            "cvd_5m_usd": _safe_float(snapshot.get("cvd_5m_usd"), 0.0),
            "liq_buy_1m_usd": _safe_float(snapshot.get("liq_buy_1m_usd"), 0.0),
            "liq_sell_1m_usd": _safe_float(snapshot.get("liq_sell_1m_usd"), 0.0),
            "basis_bps": _safe_float(snapshot.get("basis_bps"), 0.0),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        line_bytes = line.encode("utf-8")
        rotate_tape_file_if_needed(
            abs_target,
            max_bytes=max_bytes,
            incoming_bytes=len(line_bytes),
        )
        with open(abs_target, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception:
        return False


def _fetch_json_urllib(url: str, timeout_sec: float = 4.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "lnm-sniper-sidecar/1.0",
        },
        method="GET",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
        raw = resp.read(262_144)
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def fetch_binance_rest_metrics_sync(
    rest_base_url: str = DEFAULT_REST_BASE_URL,
    symbol: str = DEFAULT_SYMBOL,
    timeout_sec: float = 4.0,
) -> Dict[str, Optional[float]]:
    """Consulta sincrónica stdlib de `openInterest` y `premiumIndex` (para ejecutar vía `to_thread`)."""
    base = str(rest_base_url or DEFAULT_REST_BASE_URL).rstrip("/")
    sym = urllib.parse.quote(str(symbol or DEFAULT_SYMBOL).strip().upper())
    out: Dict[str, Optional[float]] = {
        "funding_rate": None,
        "mark_price": None,
        "open_interest_btc": None,
    }
    try:
        oi_doc = _fetch_json_urllib(
            f"{base}/fapi/v1/openInterest?symbol={sym}",
            timeout_sec=timeout_sec,
        )
        if "openInterest" in oi_doc:
            out["open_interest_btc"] = _safe_float(oi_doc.get("openInterest"), 0.0)
    except Exception:
        pass

    try:
        pi_doc = _fetch_json_urllib(
            f"{base}/fapi/v1/premiumIndex?symbol={sym}",
            timeout_sec=timeout_sec,
        )
        if "lastFundingRate" in pi_doc:
            out["funding_rate"] = _safe_float(pi_doc.get("lastFundingRate"), 0.0)
        if "markPrice" in pi_doc:
            out["mark_price"] = _safe_float(pi_doc.get("markPrice"), 0.0)
    except Exception:
        pass
    return out


class BinanceMicroSidecarService:
    """Orquestador asíncrono de stream WS, polling REST 30s y publicación atómica 1s."""

    def __init__(
        self,
        state_path: str,
        ws_url: str = DEFAULT_WS_URL,
        rest_base_url: str = DEFAULT_REST_BASE_URL,
        symbol: str = DEFAULT_SYMBOL,
        publish_interval_sec: float = DEFAULT_PUBLISH_INTERVAL_SEC,
        rest_interval_sec: float = DEFAULT_REST_INTERVAL_SEC,
        ttl_sec: float = DEFAULT_TTL_SEC,
        tape_path: Optional[str] = None,
        tape_max_bytes: int = DEFAULT_TAPE_MAX_BYTES,
    ) -> None:
        self.state_path = os.path.abspath(state_path)
        self.ws_url = str(ws_url or DEFAULT_WS_URL)
        self.rest_base_url = str(rest_base_url or DEFAULT_REST_BASE_URL)
        self.symbol = str(symbol or DEFAULT_SYMBOL).strip().upper()
        self.publish_interval_sec = max(0.2, float(publish_interval_sec))
        self.rest_interval_sec = max(5.0, float(rest_interval_sec))
        self.ttl_sec = max(1.0, float(ttl_sec))
        self.tape_path: Optional[str] = (
            os.path.abspath(str(tape_path)) if tape_path else None
        )
        self.tape_max_bytes: int = max(1, int(tape_max_bytes))
        self._last_tape_minute_bucket: Optional[int] = None

        self.aggregator = BinanceMicroAggregator(symbol=self.symbol)
        self.connected: bool = False
        self.stop_state: Dict[str, Any] = {"requested": False, "sig": None}

    def request_stop(self, signum: Optional[int] = None) -> None:
        """Manejador lock-free apto para invocación directa desde `signal.signal`."""
        self.stop_state["requested"] = True
        self.stop_state["sig"] = signum

    def is_stopped(self) -> bool:
        return bool(self.stop_state["requested"])

    async def _sleep_interruptible(self, total_sec: float) -> None:
        rem = max(0.0, float(total_sec))
        while rem > 0.0 and not self.is_stopped():
            step = min(0.25, rem)
            await asyncio.sleep(step)
            rem -= step

    async def _ws_loop(self) -> None:
        use_ssl, host, port, path_query, host_header = _parse_ws_endpoint(self.ws_url)
        ssl_ctx = ssl.create_default_context() if use_ssl else None
        backoff_sec = 1.0
        max_backoff_sec = 30.0
        ws_connect_count = 0

        while not self.is_stopped():
            writer: Optional[asyncio.StreamWriter] = None
            conn_t0 = time.monotonic()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=host,
                        port=port,
                        ssl=ssl_ctx,
                        server_hostname=host if use_ssl else None,
                    ),
                    timeout=10.0,
                )
                await perform_ws_handshake(
                    reader=reader,
                    writer=writer,
                    host_header=host_header,
                    path_query=path_query,
                    timeout_sec=10.0,
                )
                self.connected = True
                ws_connect_count += 1
                ws_evt = "Reconectado" if ws_connect_count > 1 else "Conectado"
                print(
                    _format_sidecar_log(
                        f"[BINANCE-SIDECAR] {ws_evt} WS ({host}:{port}{path_query})"
                    )
                )
                sys.stdout.flush()

                async def _send_raw(raw_frame: bytes) -> None:
                    if writer is not None:
                        writer.write(raw_frame)
                        await asyncio.wait_for(writer.drain(), timeout=5.0)

                ws_status, ws_code = await consume_ws_frames(
                    reader=reader,
                    send_raw_fn=_send_raw,
                    on_text_fn=self.aggregator.on_ws_message,
                    stop_check_fn=self.is_stopped,
                    read_timeout_sec=120.0,
                )
                if (time.monotonic() - conn_t0) >= 15.0:
                    backoff_sec = 1.0
                if not self.is_stopped():
                    sys.stderr.write(
                        _format_sidecar_log(
                            f"[BINANCE-SIDECAR] Desconexión WS ({ws_status}:{ws_code}). "
                            f"Reconectando en {backoff_sec:.1f}s..."
                        )
                        + "\n"
                    )
                    sys.stderr.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self.is_stopped():
                    sys.stderr.write(
                        _format_sidecar_log(
                            f"[BINANCE-SIDECAR] Desconexión/error WS ({type(exc).__name__}: {exc}). "
                            f"Reintentando en {backoff_sec:.1f}s..."
                        )
                        + "\n"
                    )
                    sys.stderr.flush()
            finally:
                self.connected = False
                if writer is not None:
                    try:
                        if not self.is_stopped():
                            writer.write(build_masked_close_frame(code=1000))
                            await asyncio.wait_for(writer.drain(), timeout=1.0)
                    except Exception:
                        pass
                    try:
                        writer.close()
                        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                    except Exception:
                        pass

            if self.is_stopped():
                break
            jitter = random.uniform(0.0, min(1.0, backoff_sec * 0.2))
            await self._sleep_interruptible(backoff_sec + jitter)
            backoff_sec = min(max_backoff_sec, backoff_sec * 2.0)

    async def _rest_loop(self) -> None:
        while not self.is_stopped():
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(
                        fetch_binance_rest_metrics_sync,
                        self.rest_base_url,
                        self.symbol,
                        4.0,
                    ),
                    timeout=6.0,
                )
                if any(v is not None for v in res.values()):
                    self.aggregator.update_rest_metrics(
                        funding_rate=res.get("funding_rate"),
                        mark_price=res.get("mark_price"),
                        open_interest_btc=res.get("open_interest_btc"),
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await self._sleep_interruptible(self.rest_interval_sec)

    async def _publish_loop(self) -> None:
        while not self.is_stopped():
            try:
                snap = self.aggregator.build_snapshot(
                    connected=self.connected,
                    ttl_sec=self.ttl_sec,
                )
                await asyncio.to_thread(
                    write_binance_micro_snapshot,
                    snap,
                    self.state_path,
                )
                now_ts = _safe_float(snap.get("updated_at_ts"), 0.0)
                if now_ts <= 0.0:
                    now_ts = time.time()
                cur_bucket = int(now_ts // 60)
                if self._last_tape_minute_bucket is None:
                    self._last_tape_minute_bucket = cur_bucket
                elif cur_bucket > self._last_tape_minute_bucket:
                    self._last_tape_minute_bucket = cur_bucket
                    if self.tape_path:
                        await asyncio.to_thread(
                            append_binance_micro_tape_1m,
                            self.tape_path,
                            snap,
                            ts=float(cur_bucket * 60),
                            max_bytes=self.tape_max_bytes,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                sys.stderr.write(
                    _format_sidecar_log(f"[BINANCE-SIDECAR] Error publicando snapshot: {exc}")
                    + "\n"
                )
                sys.stderr.flush()
            await self._sleep_interruptible(self.publish_interval_sec)

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._ws_loop(), name="binance_ws"),
            asyncio.create_task(self._rest_loop(), name="binance_rest"),
            asyncio.create_task(self._publish_loop(), name="binance_pub"),
        ]
        try:
            while not self.is_stopped():
                done = [t for t in tasks if t.done()]
                if done:
                    break
                await asyncio.sleep(0.25)
        finally:
            self.request_stop(self.stop_state.get("sig"))
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                final_snap = self.aggregator.build_snapshot(
                    connected=False,
                    ttl_sec=self.ttl_sec,
                )
                write_binance_micro_snapshot(final_snap, state_path=self.state_path)
            except Exception:
                pass


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="binance_micro_sidecar.py",
        description=(
            "Sidecar independiente de microestructura Binance Futures (shadow_only, "
            "100% Python stdlib) para LNM Sniper."
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Directorio de salida del snapshot (override de SNIPER_STATE_DIR / ~/.sniper_state).",
    )
    parser.add_argument(
        "--ws-url",
        default=DEFAULT_WS_URL,
        help="Endpoint WebSocket combinado de Binance Futures.",
    )
    parser.add_argument(
        "--rest-base-url",
        default=DEFAULT_REST_BASE_URL,
        help="Base URL REST pública de Binance Futures.",
    )
    parser.add_argument(
        "--symbol",
        default=DEFAULT_SYMBOL,
        help="Símbolo perpetuo USD-M a monitorear (default: BTCUSDT).",
    )
    parser.add_argument(
        "--publish-interval",
        type=float,
        default=DEFAULT_PUBLISH_INTERVAL_SEC,
        help="Intervalo de escritura atómica del snapshot en segundos (default: 1.0).",
    )
    parser.add_argument(
        "--rest-interval",
        type=float,
        default=DEFAULT_REST_INTERVAL_SEC,
        help="Intervalo de consulta REST openInterest/premiumIndex en segundos (default: 30.0).",
    )
    parser.add_argument(
        "--ttl-sec",
        type=float,
        default=DEFAULT_TTL_SEC,
        help="TTL máximo de validez del snapshot en segundos (default: 15.0).",
    )
    parser.add_argument(
        "--tape-path",
        default=None,
        help="Ruta JSONL para grabar el tape histórico 1m por minuto cerrado (default: desactivado).",
    )
    parser.add_argument(
        "--enable-tape",
        action="store_true",
        help=f"Activa la grabación de tape 1m en {DEFAULT_BINANCE_TAPE_PATH} si no se pasa --tape-path.",
    )
    parser.add_argument(
        "--tape-max-bytes",
        type=int,
        default=DEFAULT_TAPE_MAX_BYTES,
        help=f"Tamaño máximo en bytes antes de rotar el tape 1m a .1 (default: {DEFAULT_TAPE_MAX_BYTES}).",
    )
    parser.add_argument(
        "--dry-check",
        action="store_true",
        help="Valida configuración, framing RFC 6455 y agregador en RAM sin red ni credenciales.",
    )
    return parser


def _run_dry_check(args: argparse.Namespace) -> int:
    target_path = resolve_binance_snapshot_path(state_dir=args.state_dir)
    use_ssl, host, port, path_q, _ = _parse_ws_endpoint(args.ws_url)
    agg = BinanceMicroAggregator(symbol=args.symbol, clock_fn=lambda: 1700000000.0)
    agg.on_agg_trade({"s": args.symbol, "p": "65000.0", "q": "0.1", "m": False}, now_ts=1700000000.0)
    snap = agg.build_snapshot(connected=True, now_ts=1700000000.0, ttl_sec=args.ttl_sec)
    pong = build_masked_pong_frame(b"dry-check", mask_key=b"\x01\x02\x03\x04")
    decoded, _ = decode_ws_frame_bytes(pong)
    if not (decoded.opcode == WS_OP_PONG and decoded.masked and decoded.payload == b"dry-check"):
        raise RuntimeError("Fallo autochequeo RFC 6455 PONG enmascarado")
    with tempfile.TemporaryDirectory() as tmpdir:
        missing_view = read_binance_micro_snapshot(
            state_path=os.path.join(tmpdir, "missing.json"),
            now_ts=1700000000.0,
            ttl_sec=args.ttl_sec,
        )
        if missing_view["available"] is not False or missing_view["stale"] is not True:
            raise RuntimeError("Fallo autochequeo lector snapshot ausente")
    print(
        f"[DRY-CHECK OK] symbol={snap['symbol']} | ssl={use_ssl} | endpoint={host}:{port}{path_q} | "
        f"state_path={target_path} | ttl_sec={float(args.ttl_sec):.1f} | cvd_1m={snap['cvd_1m_usd']:.2f}"
    )
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.dry_check:
        return _run_dry_check(args)

    state_path = resolve_binance_snapshot_path(state_dir=args.state_dir)
    tape_path = args.tape_path
    if not tape_path and getattr(args, "enable_tape", False):
        tape_path = DEFAULT_BINANCE_TAPE_PATH
    service = BinanceMicroSidecarService(
        state_path=state_path,
        ws_url=args.ws_url,
        rest_base_url=args.rest_base_url,
        symbol=args.symbol,
        publish_interval_sec=args.publish_interval,
        rest_interval_sec=args.rest_interval,
        ttl_sec=args.ttl_sec,
        tape_path=tape_path,
        tape_max_bytes=args.tape_max_bytes,
    )

    def _on_signal(signum: int, _frame: Any) -> None:
        service.request_stop(signum)

    for sig_name in ("SIGINT", "SIGTERM"):
        sig_obj = getattr(signal, sig_name, None)
        if sig_obj is not None:
            try:
                signal.signal(sig_obj, _on_signal)
            except Exception:
                pass

    print(
        _format_sidecar_log(
            f"[BINANCE-SIDECAR] Iniciando stream shadow_only symbol={service.symbol} -> {service.state_path} "
            f"(TTL={service.ttl_sec:.1f}s)"
        )
    )
    sys.stdout.flush()
    asyncio.run(service.run())
    stop_sig = service.stop_state.get("sig")
    sig_info = f" (sig={stop_sig})" if stop_sig is not None else ""
    print(
        _format_sidecar_log(
            f"[BINANCE-SIDECAR] Stream detenido limpiamente{sig_info} -> {service.state_path}"
        )
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
