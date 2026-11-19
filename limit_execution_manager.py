#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
LNM SNIPER v7.3+ — MOTOR DE EJECUCIÓN LÍMITE CON TTL Y FSM (FASE 2)
================================================================================
Diseñado para Termux / Linux y paridad 100% Sim-to-Real:
  - Enrutamiento inteligente MARKET vs. LIMIT por estilo y confluencia de liquidez.
  - Fijación quirúrgica de P_lim al tick de $0.5 USD con garantía non-crossing.
  - Máquina de Estados Finita (FSM) persistida atómicamente con FileLockContext
    (fcntl en $HOME, nunca /sdcard) + os.replace().
  - Cancelación temprana: TTL, Runaway/Chasing Guard, Invalidación estructural
    de mecha, giro de OVR y cancelación total ante Hard-Floor / SAFE / SIGTERM.
  - Invariante P0 de concurrencia: running_trades + open_limit_orders <= MAX_POS.
================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import json
import math
import os
import time
import uuid

from guardian_execution_core import FileLockContext


@dataclass(frozen=True)
class LimitExecutionConfig:
    enabled: bool = False
    tick_size_usd: float = 0.5
    ttl_sec_5m: float = 90.0
    ttl_sec_15m: float = 150.0
    runaway_tp_frac: float = 0.55
    max_entry_offset_atr: float = 0.75
    retest_atr_sniper: float = 0.05
    retest_atr_reversion: float = 0.12
    retest_atr_scalp: float = 0.10
    fallback_pullback_atr: float = 0.18
    invalidation_buffer_atr: float = 0.15
    min_sweep_align_sniper: float = 0.35
    min_sweep_align_reversion: float = 0.40
    min_passive_bps: float = 1.0  # 0.01% mínimo por detrás del spot

    @classmethod
    def from_thresholds(cls, thresholds_mgr: Any = None) -> "LimitExecutionConfig":
        if thresholds_mgr is None:
            return cls()
        get_b = getattr(thresholds_mgr, "get_bool", None)
        get_f = getattr(thresholds_mgr, "get_float", None)
        if not (callable(get_b) and callable(get_f)):
            return cls()
        cat = "limit_execution"
        return cls(
            enabled=bool(get_b(cat, "enabled", cls.enabled)),
            tick_size_usd=max(0.5, float(get_f(cat, "tick_size_usd", cls.tick_size_usd))),
            ttl_sec_5m=float(get_f(cat, "ttl_sec_5m", cls.ttl_sec_5m)),
            ttl_sec_15m=float(get_f(cat, "ttl_sec_15m", cls.ttl_sec_15m)),
            runaway_tp_frac=float(get_f(cat, "runaway_tp_frac", cls.runaway_tp_frac)),
            max_entry_offset_atr=float(get_f(cat, "max_entry_offset_atr", cls.max_entry_offset_atr)),
            retest_atr_sniper=float(get_f(cat, "retest_atr_sniper", cls.retest_atr_sniper)),
            retest_atr_reversion=float(get_f(cat, "retest_atr_reversion", cls.retest_atr_reversion)),
        )


def round_to_tick(price: float, side: str, tick_size: float = 0.5) -> float:
    """
    Redondea al tick del exchange garantizando sesgo pasivo:
      - LONG ('b'/'buy'): floor al múltiplo de tick_size.
      - SHORT ('s'/'sell'): ceil al múltiplo de tick_size.
    """
    tick = max(0.01, float(tick_size))
    p = max(tick, float(price))
    is_long = str(side).lower().strip() in ("b", "buy", "long")
    inv = 1.0 / tick
    if is_long:
        val = math.floor((p * inv) + 1e-9) * tick
    else:
        val = math.ceil((p * inv) - 1e-9) * tick
    return round(val, 2)


def should_route_limit(
    action: int,
    style: str,
    market_regime: str,
    liquidity_signal: Optional[Dict[str, Any]],
    spot_price: float,
    atr_5m: float,
    taker_fee: float = 0.001,
    recovery_mode: bool = False,
    cfg: Optional[LimitExecutionConfig] = None,
) -> Tuple[bool, str]:
    """
    Matriz determinista de enrutamiento MARKET vs LIMIT.
    Evita adverse selection en rupturas de tendencia (TREND) y captura mechas en REVERSION/SNIPER/SCALP.
    """
    conf = cfg or LimitExecutionConfig()
    if not conf.enabled:
        return False, "LIMIT_DISABLED"

    act_idx = int(action) % 9
    if act_idx == 0:
        return False, "ACTION_HOLD"

    is_long = (1 <= act_idx <= 4)
    st_up = str(style or "").upper()
    reg_up = str(market_regime or "").upper()
    score = float((liquidity_signal or {}).get("liquidity_score") or 0.0)
    liq_align = score if is_long else -score

    if "TREND" in st_up:
        return False, "STYLE_TREND_MARKET_PREFERRED"

    if recovery_mode and liq_align < conf.min_sweep_align_sniper:
        return False, "RECOVERY_NO_SWEEP_MARKET"

    if "SNIPER" in st_up:
        if liq_align >= conf.min_sweep_align_sniper:
            return True, f"LIMIT_SNIPER_SWEEP(align={liq_align:.2f})"
        return False, "SNIPER_NO_SWEEP_MARKET"

    if "REVERSION" in st_up:
        if liq_align >= conf.min_sweep_align_reversion:
            return True, f"LIMIT_REVERSION_SWEEP(align={liq_align:.2f})"
        if "RANGE" in reg_up or "CHOP" in reg_up:
            return True, f"LIMIT_REVERSION_REGIME({reg_up})"
        return False, "REVERSION_OUTSIDE_RANGE_MARKET"

    if "SCALP" in st_up:
        fee_price_offset = max(1.0, float(spot_price)) * (2.0 * max(0.0, float(taker_fee)))
        if ("RANGE" in reg_up or "CHOP" in reg_up) and float(atr_5m) > fee_price_offset:
            return True, f"LIMIT_SCALP_PASSIVE({reg_up})"
        return False, "SCALP_LOW_ATR_OR_TREND_MARKET"

    return False, "DEFAULT_MARKET"


def compute_limit_entry_plan(
    side: str,
    style: str,
    spot_price: float,
    atr_5m: float,
    base_sl: float,
    base_tp: float,
    liquidity_signal: Optional[Dict[str, Any]] = None,
    cfg: Optional[LimitExecutionConfig] = None,
) -> Dict[str, Any]:
    """
    Calcula P_lim pasivo al tick ($0.5), verifica distancia máxima (<= 0.75 ATR),
    recalcula TP/SL preservando distancias efectivas y fija el precio de invalidación estructural.
    """
    conf = cfg or LimitExecutionConfig()
    is_long = str(side).lower().strip() in ("b", "buy", "long")
    spot = float(spot_price)
    atr = max(1.0, float(atr_5m))
    tick = conf.tick_size_usd
    buf = max(tick, spot * (conf.min_passive_bps / 10000.0))

    st_up = str(style or "").upper()
    if "SNIPER" in st_up:
        lam = conf.retest_atr_sniper
    elif "REVERSION" in st_up:
        lam = conf.retest_atr_reversion
    else:
        lam = conf.retest_atr_scalp

    sig = liquidity_signal if isinstance(liquidity_signal, dict) else {}
    score = float(sig.get("liquidity_score") or 0.0)
    liq_align = score if is_long else -score
    swept_level = float(sig.get("swept_level") or 0.0)
    wick_ext = float(sig.get("wick_extreme") or 0.0)
    sweep_tf = str(sig.get("timeframe") or "5m")

    has_aligned_sweep = (
        liq_align >= conf.min_sweep_align_sniper
        and swept_level > 0.0
        and wick_ext > 0.0
        and ((is_long and wick_ext < swept_level) or ((not is_long) and wick_ext > swept_level))
    )

    if has_aligned_sweep:
        if is_long:
            p_raw = min(spot - buf, swept_level + (lam * atr))
            p_floor = wick_ext + 0.15 * (swept_level - wick_ext)
            p_clamped = max(p_raw, p_floor)
            # Garantizar non-crossing estricto tras el clamp
            p_clamped = min(p_clamped, spot - tick)
            p_lim = round_to_tick(p_clamped, "b", tick)
            if p_lim >= spot:
                p_lim = round_to_tick(spot - tick, "b", tick)
            invalidation_price = round(wick_ext - (conf.invalidation_buffer_atr * atr), 2)
        else:
            p_raw = max(spot + buf, swept_level - (lam * atr))
            p_ceil = wick_ext - 0.15 * (wick_ext - swept_level)
            p_clamped = min(p_raw, p_ceil)
            p_clamped = max(p_clamped, spot + tick)
            p_lim = round_to_tick(p_clamped, "s", tick)
            if p_lim <= spot:
                p_lim = round_to_tick(spot + tick, "s", tick)
            invalidation_price = round(wick_ext + (conf.invalidation_buffer_atr * atr), 2)
        ttl_sec = conf.ttl_sec_15m if sweep_tf == "15m" else conf.ttl_sec_5m
        anchor_mode = f"SWEEP_{sweep_tf}"
    else:
        pullback = max(buf, conf.fallback_pullback_atr * atr)
        if is_long:
            p_lim = round_to_tick(spot - pullback, "b", tick)
            if p_lim >= spot:
                p_lim = round_to_tick(spot - tick, "b", tick)
            invalidation_price = round(p_lim - (0.65 * atr), 2)
        else:
            p_lim = round_to_tick(spot + pullback, "s", tick)
            if p_lim <= spot:
                p_lim = round_to_tick(spot + tick, "s", tick)
            invalidation_price = round(p_lim + (0.65 * atr), 2)
        ttl_sec = conf.ttl_sec_5m
        anchor_mode = "PULLBACK_ATR"

    offset_atr = abs(spot - p_lim) / atr
    if offset_atr > conf.max_entry_offset_atr:
        return {
            "valid": False,
            "reason": f"LIMIT_OFFSET_TOO_FAR({offset_atr:.2f}ATR>{conf.max_entry_offset_atr:.2f}ATR)",
            "limit_price": p_lim,
            "offset_atr": round(offset_atr, 3),
        }

    # Recalcular TP y SL anclados a P_lim preservando presupuesto de riesgo original
    orig_sl_dist = max(atr * 0.4, abs(spot - float(base_sl)))
    orig_tp_dist = max(atr * 0.6, abs(float(base_tp) - spot))
    if is_long:
        new_sl = p_lim - orig_sl_dist
        if has_aligned_sweep:
            wick_sl = wick_ext - (0.20 * atr)
            # Ajustar SL cerca de la mecha solo si reduce riesgo sin quedar absurdamente pegado (>= 0.40 ATR)
            if new_sl < wick_sl <= (p_lim - 0.40 * atr):
                new_sl = wick_sl
        new_tp = p_lim + orig_tp_dist
        if new_sl >= p_lim or new_tp <= p_lim:
            return {"valid": False, "reason": "INVALID_LONG_BRACKET", "limit_price": p_lim}
    else:
        new_sl = p_lim + orig_sl_dist
        if has_aligned_sweep:
            wick_sl = wick_ext + (0.20 * atr)
            if (p_lim + 0.40 * atr) <= wick_sl < new_sl:
                new_sl = wick_sl
        new_tp = p_lim - orig_tp_dist
        if new_sl <= p_lim or new_tp >= p_lim:
            return {"valid": False, "reason": "INVALID_SHORT_BRACKET", "limit_price": p_lim}

    return {
        "valid": True,
        "reason": "OK",
        "anchor_mode": anchor_mode,
        "limit_price": round(p_lim, 2),
        "stoploss": round(new_sl, 2),
        "takeprofit": round(new_tp, 2),
        "invalidation_price": invalidation_price,
        "ttl_sec": float(ttl_sec),
        "offset_atr": round(offset_atr, 3),
        "improvement_usd": round(abs(spot - p_lim), 2),
    }


class LimitExecutionManager:
    """
    Administra el ciclo de vida completo (FSM) de órdenes límite pendientes:
    persistencia atómica en $HOME con FileLockContext, reserva de cupo P0,
    reconciliación open->running y cancelación temprana.
    """

    def __init__(
        self,
        env_mode: str = "mainnet",
        base_dir: Optional[str] = None,
        max_concurrent_trades: int = 5,
    ):
        self.env_mode = str(env_mode or "mainnet").strip().lower()
        root = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.filepath = os.path.abspath(os.path.join(root, f"pending_orders_{self.env_mode}.json"))
        self.max_concurrent_trades = max(1, int(max_concurrent_trades))
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.stats: Dict[str, int] = {
            "submitted": 0,
            "filled": 0,
            "cancelled_ttl": 0,
            "cancelled_runaway": 0,
            "cancelled_invalidated": 0,
            "cancelled_safety": 0,
            "errors": 0,
        }
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.filepath):
            self.pending = {}
            return
        try:
            with FileLockContext(self.filepath, exclusive=False, timeout=2.0, raise_on_timeout=False):
                with open(self.filepath, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            if isinstance(raw, dict):
                orders = raw.get("pending", {})
                if isinstance(orders, dict):
                    self.pending = {
                        str(k): v for k, v in orders.items()
                        if isinstance(v, dict) and v.get("status") in ("PENDING_SUBMIT", "OPEN_WAITING")
                    }
                st = raw.get("stats")
                if isinstance(st, dict):
                    for k in self.stats:
                        if k in st:
                            self.stats[k] = int(st[k])
        except Exception:
            self.pending = {}

    def save(self) -> bool:
        parent = os.path.dirname(self.filepath)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = f"{self.filepath}.tmp.{os.getpid()}"
        payload = {
            "env_mode": self.env_mode,
            "updated_at": round(time.time(), 3),
            "pending": self.pending,
            "stats": self.stats,
        }
        try:
            with FileLockContext(self.filepath, exclusive=True, timeout=2.0, raise_on_timeout=False):
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, ensure_ascii=False)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp, self.filepath)
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    def active_open_count(self) -> int:
        return sum(1 for v in self.pending.values() if v.get("status") in ("PENDING_SUBMIT", "OPEN_WAITING"))

    def reserved_margin_sats(self) -> int:
        return sum(
            int(v.get("margin_sats") or 0)
            for v in self.pending.values()
            if v.get("status") in ("PENDING_SUBMIT", "OPEN_WAITING")
        )

    def effective_slots_used(self, running_count: int) -> int:
        return max(0, int(running_count)) + self.active_open_count()

    def can_place_limit(
        self,
        side: str,
        limit_price: float,
        atr_5m: float,
        running_count: int,
        max_slots: Optional[int] = None,
    ) -> Tuple[bool, str]:
        cap = int(max_slots if max_slots is not None else self.max_concurrent_trades)
        if self.effective_slots_used(running_count) >= cap:
            return False, f"SLOTS_FULL(running={running_count}+open={self.active_open_count()}>={cap})"

        norm_side = "b" if str(side).lower().strip() in ("b", "buy", "long") else "s"
        atr = max(1.0, float(atr_5m))
        for rec in self.pending.values():
            if rec.get("status") not in ("PENDING_SUBMIT", "OPEN_WAITING"):
                continue
            if rec.get("side") == norm_side:
                return False, f"OPEN_LIMIT_SAME_SIDE_EXISTS(id={str(rec.get('order_id',''))[:8]})"
            if abs(float(rec.get("limit_price") or 0.0) - float(limit_price)) <= (0.40 * atr):
                return False, "OPEN_LIMIT_TOO_CLOSE_PRICE"
        return True, "OK"

    @staticmethod
    def generate_client_id(env_mode: str, now: Optional[float] = None) -> str:
        ts = int(now if now is not None else time.time())
        short_env = "main" if str(env_mode).lower().startswith("main") else "test"
        return f"lnms_{short_env}_lim_{ts}_{uuid.uuid4().hex[:6]}"

    def submit_limit_order(
        self,
        client: Any,
        side: str,
        margin_sats: int,
        leverage: float,
        plan: Dict[str, Any],
        meta: Dict[str, Any],
        running_count: int,
        atr_5m: float,
        max_slots: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        now_ts = float(now if now is not None else time.time())
        norm_side = "b" if str(side).lower().strip() in ("b", "buy", "long") else "s"
        lim_price = float(plan["limit_price"])

        ok_slot, slot_reason = self.can_place_limit(
            norm_side, lim_price, atr_5m, running_count, max_slots=max_slots
        )
        if not ok_slot:
            return False, slot_reason, None

        client_id = self.generate_client_id(self.env_mode, now=now_ts)
        draft = {
            "client_id": client_id,
            "order_id": "",
            "side": norm_side,
            "style": str(meta.get("style", "")),
            "action": int(meta.get("action", 0)),
            "state_key": str(meta.get("state", "")),
            "margin_sats": int(margin_sats),
            "leverage": float(leverage),
            "limit_price": lim_price,
            "tp_price": float(plan["takeprofit"]),
            "sl_price": float(plan["stoploss"]),
            "invalidation_price": float(plan["invalidation_price"]),
            "created_at": now_ts,
            "ttl_sec": float(plan["ttl_sec"]),
            "entry_q": float(meta.get("entry_q", 0.0)),
            "is_exploratory": bool(meta.get("is_exploratory", False)),
            "liq_score": float(meta.get("liq_score", 0.0)),
            "status": "PENDING_SUBMIT",
            "cancel_attempts": 0,
        }
        self.pending[client_id] = draft
        self.save()

        try:
            res = client.open_limit_trade(
                side=norm_side,
                margin=int(margin_sats),
                leverage=int(round(float(leverage))),
                price=lim_price,
                stoploss=float(plan["stoploss"]),
                takeprofit=float(plan["takeprofit"]),
                client_id=client_id,
            )
            oid = str((res or {}).get("id") or (res or {}).get("trade_id") or "")
            if not oid:
                raise RuntimeError(f"Respuesta sin order_id: {res}")
            draft["order_id"] = oid
            draft["status"] = "OPEN_WAITING"
            self.pending[client_id] = draft
            self.stats["submitted"] += 1
            self.save()
            return True, "SUBMITTED", draft
        except Exception as exc:
            self.pending.pop(client_id, None)
            self.stats["errors"] += 1
            self.save()
            return False, f"SUBMIT_ERROR({exc})", None

    def _cancel_reason(
        self,
        rec: Dict[str, Any],
        spot_price: float,
        ovr_side: Optional[str],
        now_ts: float,
        cfg: LimitExecutionConfig,
        force_safety: bool,
    ) -> Optional[str]:
        if force_safety:
            return "SAFETY_OR_SHUTDOWN"

        age = max(0.0, now_ts - float(rec.get("created_at") or now_ts))
        if age >= float(rec.get("ttl_sec") or cfg.ttl_sec_5m):
            return f"TTL_EXPIRED({age:.0f}s)"

        is_long = (rec.get("side") == "b")
        p_lim = float(rec.get("limit_price") or 0.0)
        p_tp = float(rec.get("tp_price") or 0.0)
        p_inv = float(rec.get("invalidation_price") or 0.0)
        spot = float(spot_price or 0.0)

        if spot > 0.0 and p_lim > 0.0:
            # 1. Invalidación estructural por ruptura de la mecha barrida
            if is_long and p_inv > 0.0 and spot < p_inv:
                return f"WICK_INVALIDATED(spot={spot:.1f}<inv={p_inv:.1f})"
            if (not is_long) and p_inv > 0.0 and spot > p_inv:
                return f"WICK_INVALIDATED(spot={spot:.1f}>inv={p_inv:.1f})"

            # 2. Runaway / Chasing Guard (>= 55% recorrido hacia TP sin llenado)
            sign = 1.0 if is_long else -1.0
            tp_span = sign * (p_tp - p_lim)
            if tp_span > 1e-6:
                progress = (sign * (spot - p_lim)) / tp_span
                if progress >= cfg.runaway_tp_frac:
                    return f"RUNAWAY_GUARD(progress={progress*100:.0f}%)"

        # 3. Giro violento de OVR en contra
        if (is_long and ovr_side == "BEAR") or ((not is_long) and ovr_side == "BULL"):
            return f"OVR_FLIP({ovr_side})"

        return None

    def reconcile_open_orders(
        self,
        client: Any,
        running_trades: List[Dict[str, Any]],
        spot_price: float,
        perception: Optional[Dict[str, Any]] = None,
        cfg: Optional[LimitExecutionConfig] = None,
        force_cancel_all: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Reconcilia órdenes pendientes contra el exchange en cada ciclo de 5s.
        Cero llamadas API extra cuando pending está vacío.
        """
        conf = cfg or LimitExecutionConfig()
        now_ts = float(now if now is not None else time.time())
        filled_events: List[Dict[str, Any]] = []
        cancelled_events: List[Dict[str, Any]] = []

        if not self.pending:
            return {"filled": filled_events, "cancelled": cancelled_events, "open_count": 0}

        running_by_id = {str(t.get("id")): t for t in (running_trades or []) if isinstance(t, dict) and t.get("id")}
        running_by_cid = {
            str(t.get("client_id") or t.get("clientId")): t
            for t in (running_trades or [])
            if isinstance(t, dict) and (t.get("client_id") or t.get("clientId"))
        }

        open_by_id: Dict[str, Dict[str, Any]] = {}
        try:
            open_list = client.get_open_orders() if hasattr(client, "get_open_orders") else []
            for o in (open_list or []):
                if isinstance(o, dict) and o.get("id"):
                    open_by_id[str(o["id"])] = o
        except Exception:
            # Si la consulta de open falla transitoriamente, conservamos el mapa local y verificamos running
            open_by_id = {str(v.get("order_id")): v for v in self.pending.values() if v.get("order_id")}

        ovr_side = perception.get("override_side") if isinstance(perception, dict) else None
        changed = False

        for cid in list(self.pending.keys()):
            rec = self.pending[cid]
            oid = str(rec.get("order_id") or "")

            # Caso 1: Ya se llenó y apareció en running_trades
            matched_trade = running_by_id.get(oid) or running_by_cid.get(cid)
            if matched_trade is not None:
                rec["status"] = "FILLED"
                filled_events.append({"trade": matched_trade, "meta": dict(rec)})
                self.pending.pop(cid, None)
                self.stats["filled"] += 1
                changed = True
                continue

            # Caso 2: Desapareció de open_orders del exchange y no está en running -> ya fue cancelada/expirada en servidor
            if oid and oid not in open_by_id:
                rec["status"] = "CANCELLED"
                cancelled_events.append({"order_id": oid, "client_id": cid, "reason": "GONE_FROM_SERVER"})
                self.pending.pop(cid, None)
                changed = True
                continue

            # Caso 3: Evaluar reglas de cancelación temprana
            reason = self._cancel_reason(rec, spot_price, ovr_side, now_ts, conf, force_cancel_all)
            if reason is not None:
                try:
                    if oid and hasattr(client, "cancel_open_order"):
                        client.cancel_open_order(oid)
                    rec["status"] = "CANCELLED"
                    cancelled_events.append({"order_id": oid, "client_id": cid, "reason": reason})
                    self.pending.pop(cid, None)
                    if reason.startswith("TTL"):
                        self.stats["cancelled_ttl"] += 1
                    elif reason.startswith("RUNAWAY"):
                        self.stats["cancelled_runaway"] += 1
                    elif reason.startswith("WICK") or reason.startswith("OVR"):
                        self.stats["cancelled_invalidated"] += 1
                    else:
                        self.stats["cancelled_safety"] += 1
                    changed = True
                except Exception as exc:
                    # Carrera cancel-vs-fill: si el server retorna 404/already running, refrescar running antes de descartar
                    err_low = str(exc).lower()
                    if "404" in err_low or "not found" in err_low or "running" in err_low:
                        try:
                            refreshed = client.get_running_trades(force_refresh=True) if hasattr(client, "get_running_trades") else []
                        except Exception:
                            refreshed = []
                        found = next((t for t in refreshed if str(t.get("id")) == oid), None)
                        if found is not None:
                            rec["status"] = "FILLED"
                            filled_events.append({"trade": found, "meta": dict(rec)})
                            self.pending.pop(cid, None)
                            self.stats["filled"] += 1
                        else:
                            rec["status"] = "CANCELLED"
                            cancelled_events.append({"order_id": oid, "client_id": cid, "reason": f"{reason}|404_CLEANED"})
                            self.pending.pop(cid, None)
                        changed = True
                    else:
                        rec["cancel_attempts"] = int(rec.get("cancel_attempts", 0)) + 1
                        self.stats["errors"] += 1
                        changed = True

        if changed:
            self.save()

        return {
            "filled": filled_events,
            "cancelled": cancelled_events,
            "open_count": self.active_open_count(),
        }

    def cancel_all_for_shutdown(self, client: Any, running_trades: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return self.reconcile_open_orders(
            client=client,
            running_trades=running_trades or [],
            spot_price=0.0,
            perception=None,
            force_cancel_all=True,
        )
