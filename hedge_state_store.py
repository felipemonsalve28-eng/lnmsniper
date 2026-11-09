# -*- coding: utf-8 -*-
"""
hedge_state_store.py
Persistencia atómica con bloqueo FileLockContext, verificación SHA-256,
recuperación automática desde .bak y aprendizaje Q-learning tabular independiente
para el Hybrid Edge Hedging Engine (Fase 5 - LN Markets API v3).
Preserva intacto el formato y las 9 acciones de q_brain_advanced_lnm.json.
"""
import os
import time
import math
from collections import deque
from guardian_execution_core import AtomicBrainStore


HEDGE_STATES = (
    "NORMAL",
    "WARNING",
    "HEDGE_CANDIDATE",
    "HEDGE_ACTIVE",
    "HEDGE_ADJUST",
    "RECOVERY",
    "HEDGE_EXIT",
)

HEDGE_ACTIONS = {
    0: "NO_HEDGE",
    1: "PARTIAL_HEDGE",
    2: "INCREASE_HEDGE",
    3: "REDUCE_HEDGE",
    4: "CLOSE_HEDGE",
    5: "REDUCE_ORIGINAL_POSITION",
    6: "CLOSE_ORIGINAL_POSITION",
}

NUM_HEDGE_ACTIONS = 7


class HedgeStateStore:
    """
    Almacén persistente y atómico del estado de coberturas y de la tabla Q
    independiente de 7 acciones del controlador de cobertura.
    """

    def __init__(self, env_mode="mainnet", filename=None):
        self.env_mode = str(env_mode or "mainnet").strip().lower()
        if filename:
            self.filename = filename
        else:
            self.filename = (
                "hedge_state_testnet.json"
                if self.env_mode in ("testnet", "test", "explorer")
                else "hedge_state_mainnet.json"
            )
        self.store = AtomicBrainStore(self.filename, required_key="pairs")
        self.pairs = {}
        self.hedge_q_table = {}
        self.hedge_visits = {}
        self.hedge_memory = deque(maxlen=1200)
        self.hedge_metrics = {
            "total_hedges_opened": 0,
            "successful_neutralizations": 0,
            "successful_unwinds": 0,
            "premature_hedges": 0,
            "avoided_stopouts": 0,
            "total_hedge_fees_sats": 0,
            "total_cashed_in_sats": 0,
            "net_sats_saved_vs_sl": 0,
            "regime_stats": {},
        }
        self.dirty = False
        self.last_save_ts = 0.0
        self.load()

    @staticmethod
    def default_q_vector(hedge_state_key=""):
        # Inicialización conservadora que favorece NO_HEDGE salvo señal clara
        q = [0.020, 0.005, 0.000, 0.005, 0.010, 0.005, 0.005]
        if "HEDGE_CANDIDATE" in hedge_state_key:
            q[1] = 0.035  # PARTIAL_HEDGE
            q[6] = 0.018  # CLOSE_ORIGINAL_POSITION
        elif "HEDGE_ACTIVE" in hedge_state_key and "SEVERE_LOSS" in hedge_state_key:
            q[2] = 0.030  # INCREASE_HEDGE
            q[6] = 0.025  # CLOSE_ORIGINAL_POSITION
        elif "RECOVERY" in hedge_state_key:
            q[4] = 0.045  # CLOSE_HEDGE
            q[3] = 0.030  # REDUCE_HEDGE
        elif "HEDGE_EXIT" in hedge_state_key:
            q[4] = 0.050  # CLOSE_HEDGE
        return [round(float(x), 4) for x in q]

    def load(self):
        data = None
        try:
            data = self.store.load()
        except Exception:
            data = None
        if not isinstance(data, dict):
            return False

        raw_pairs = data.get("pairs", {})
        if isinstance(raw_pairs, dict):
            self.pairs = raw_pairs

        raw_q = data.get("hedge_q_table", {})
        if isinstance(raw_q, dict):
            clean_q = {}
            for k, vec in raw_q.items():
                if isinstance(vec, list) and len(vec) == NUM_HEDGE_ACTIONS:
                    if all(isinstance(v, (int, float)) and not math.isnan(float(v)) and not math.isinf(float(v)) for v in vec):
                        clean_q[str(k)] = [round(float(v), 4) for v in vec]
            self.hedge_q_table = clean_q

        raw_v = data.get("hedge_visits", {})
        if isinstance(raw_v, dict):
            self.hedge_visits = {str(k): int(v) for k, v in raw_v.items() if isinstance(v, (int, float))}

        raw_mem = data.get("hedge_memory", [])
        if isinstance(raw_mem, list):
            self.hedge_memory.clear()
            for item in raw_mem[-1200:]:
                if isinstance(item, (list, tuple)) and len(item) == 5:
                    self.hedge_memory.append((
                        float(item[0]),
                        str(item[1]),
                        int(item[2]) % NUM_HEDGE_ACTIONS,
                        float(item[3]),
                        str(item[4]),
                    ))

        raw_metrics = data.get("hedge_metrics", {})
        if isinstance(raw_metrics, dict):
            for mk, mv in raw_metrics.items():
                if mk in self.hedge_metrics:
                    self.hedge_metrics[mk] = mv
        return True

    def save(self, force=False):
        now = time.time()
        if not force and not self.dirty:
            return None
        if not force and (now - self.last_save_ts) < 30.0:
            return None
        payload = {
            "version": "1.0-v3",
            "env": self.env_mode,
            "updated_at": round(now, 3),
            "pairs": self.pairs,
            "hedge_q_table": self.hedge_q_table,
            "hedge_visits": self.hedge_visits,
            "hedge_memory": list(self.hedge_memory)[-800:],
            "hedge_metrics": self.hedge_metrics,
        }
        digest = self.store.save(payload)
        self.dirty = False
        self.last_save_ts = now
        return digest

    def get_or_create_pair(self, orig_trade, orig_meta=None, now=None):
        now = float(now if now is not None else time.time())
        tid = str(orig_trade["id"])
        margin_sats = int(orig_trade.get("margin") or 1000)
        lev = float(orig_trade.get("leverage") or 20.0)
        entry_p = float(orig_trade.get("price") or orig_trade.get("entryPrice") or 0.0)
        side = str(orig_trade.get("side") or "buy").lower()
        style = str((orig_meta or {}).get("style") or "REVERSION")

        if tid not in self.pairs or not isinstance(self.pairs[tid], dict):
            self.pairs[tid] = {
                "orig_trade_id": tid,
                "orig_side": side,
                "orig_margin_sats": margin_sats,
                "orig_leverage": lev,
                "orig_notional_sats": int(round(margin_sats * lev)),
                "orig_entry_price": entry_p,
                "orig_strategy": style,
                "state": "NORMAL",
                "state_entered_at": now,
                "state_cycles": 0,
                "last_transition_ts": now,
                "cooldown_until": 0.0,
                "warning_streak": 0,
                "candidate_streak": 0,
                "adverse_streak": 0,
                "recovery_streak": 0,
                "hedge_trade_id": None,
                "hedge_side": None,
                "hedge_margin_sats": 0,
                "hedge_leverage": 0.0,
                "hedge_notional_sats": 0,
                "hedge_entry_price": 0.0,
                "hedge_ratio": 0.0,
                "cumulative_hedge_fees_sats": 0,
                "cumulative_cashed_in_sats": 0,
                "cumulative_saved_sats": 0,
                "entry_unhedged_pl_sats": 0,
                "peak_combined_net_sats": int(orig_trade.get("pl") or 0),
                "last_edge_score": 0.0,
                "edge_history": [],
                "last_hedge_state_key": None,
                "last_hedge_action": 0,
            }
            self.dirty = True
        else:
            p = self.pairs[tid]
            p["orig_margin_sats"] = margin_sats
            p["orig_leverage"] = lev
            p["orig_notional_sats"] = int(round(margin_sats * lev))
            if entry_p > 0:
                p["orig_entry_price"] = entry_p
        return self.pairs[tid]

    def get_all_hedge_trade_ids(self):
        ids = set()
        for p in self.pairs.values():
            if isinstance(p, dict) and p.get("hedge_trade_id"):
                ids.add(str(p["hedge_trade_id"]))
        return ids

    def is_hedge_trade(self, trade_id):
        return str(trade_id) in self.get_all_hedge_trade_ids()

    def reconcile_with_live_trades(self, running_trades, now=None):
        """
        Sincroniza el almacén de coberturas con las posiciones reales activas en LN Markets API v3.
        Limpia pares cuyas patas ya cerraron y actualiza el estado si una cobertura se cerró por SL/TP.
        """
        now = float(now if now is not None else time.time())
        live_map = {str(t["id"]): t for t in (running_trades or []) if isinstance(t, dict) and "id" in t}
        removed_pairs = []

        for orig_id, p in list(self.pairs.items()):
            hedge_id = str(p.get("hedge_trade_id")) if p.get("hedge_trade_id") else None
            orig_alive = orig_id in live_map
            hedge_alive = bool(hedge_id and hedge_id in live_map)

            if not orig_alive and not hedge_alive:
                removed_pairs.append(self.pairs.pop(orig_id, None))
                self.dirty = True
                continue

            if orig_alive and hedge_id and not hedge_alive:
                # La pata de cobertura se cerró en el servidor (TP/SL o manual)
                p["hedge_trade_id"] = None
                p["hedge_side"] = None
                p["hedge_margin_sats"] = 0
                p["hedge_leverage"] = 0.0
                p["hedge_notional_sats"] = 0
                p["hedge_entry_price"] = 0.0
                p["hedge_ratio"] = 0.0
                p["state"] = "NORMAL"
                p["state_entered_at"] = now
                p["state_cycles"] = 0
                p["cooldown_until"] = now + 25.0
                self.dirty = True
            elif not orig_alive and hedge_alive:
                # La pata original cerró pero la cobertura sigue abierta -> promover cobertura a primaria
                h_trade = live_map[hedge_id]
                p["hedge_trade_id"] = None
                self.pairs.pop(orig_id, None)
                self.get_or_create_pair(h_trade, orig_meta={"style": "TREND"}, now=now)
                self.dirty = True

        return removed_pairs

    def get_q_values(self, hedge_state_key):
        k = str(hedge_state_key)
        if k not in self.hedge_q_table or len(self.hedge_q_table[k]) != NUM_HEDGE_ACTIONS:
            self.hedge_q_table[k] = self.default_q_vector(k)
            self.hedge_visits[k] = 0
        return self.hedge_q_table[k]

    def update_hedge_q(self, state_key, action_idx, reward, next_state_key, alpha=0.20, gamma=0.88):
        if not state_key or not next_state_key:
            return 0.0
        s = str(state_key)
        ns = str(next_state_key)
        a = int(action_idx) % NUM_HEDGE_ACTIONS
        r = max(-1.0, min(1.0, float(reward)))

        q_s = self.get_q_values(s)
        q_ns = self.get_q_values(ns)
        self.hedge_visits[s] = int(self.hedge_visits.get(s, 0)) + 1

        cur_q = float(q_s[a])
        max_next = max(float(v) for v in q_ns)
        new_q = max(-0.95, min(0.95, cur_q + float(alpha) * (r + float(gamma) * max_next - cur_q)))
        delta_q = abs(new_q - cur_q)
        self.hedge_q_table[s][a] = round(new_q, 4)

        prio = round(abs(r) + 0.30 * delta_q + 0.02, 4)
        if a != 0 or abs(r) > 1e-4:
            self.hedge_memory.append((prio, s, a, round(r, 4), ns))
        self.dirty = True
        return round(delta_q, 4)

    def record_regime_metric(self, regime, field_name, delta=1):
        reg = str(regime or "RANGE")
        reg_map = self.hedge_metrics.setdefault("regime_stats", {})
        entry = reg_map.setdefault(reg, {
            "hedges": 0,
            "saved_sats": 0,
            "fees_sats": 0,
            "unwinds": 0,
        })
        entry[field_name] = int(entry.get(field_name, 0)) + int(delta)
        self.dirty = True
