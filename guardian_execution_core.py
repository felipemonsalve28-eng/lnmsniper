#!/usr/bin/env python3
import os, time, json, hmac, base64, shutil, hashlib, threading, requests

class AtomicBrainStore:
    def __init__(self, brain_file="q_brain_advanced_lnm.json"):
        self.brain_file = brain_file
        self.tmp_file = f"{brain_file}.tmp"
        self.bak_file = f"{brain_file}.bak"
        self.sha_file = f"{brain_file}.sha256"
        self.lock = threading.RLock()

    @staticmethod
    def compute_sha256(raw_bytes):
        return hashlib.sha256(raw_bytes).hexdigest()

    def save(self, data_dict):
        with self.lock:
            raw = json.dumps(data_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
            digest = self.compute_sha256(raw)
            with open(self.tmp_file, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(self.brain_file):
                try:
                    shutil.copy2(self.brain_file, self.bak_file)
                except Exception:
                    pass
            os.replace(self.tmp_file, self.brain_file)
            tmp_s = f"{self.sha_file}.tmp"
            with open(tmp_s, "w", encoding="utf-8") as sf:
                sf.write(digest)
                sf.flush()
                os.fsync(sf.fileno())
            os.replace(tmp_s, self.sha_file)
            return digest

    def load(self):
        with self.lock:
            for cand, chk in ((self.brain_file, True), (self.bak_file, False)):
                if not os.path.exists(cand):
                    continue
                try:
                    with open(cand, "rb") as f:
                        raw = f.read()
                    if chk and os.path.exists(self.sha_file):
                        with open(self.sha_file, "r", encoding="utf-8") as sf:
                            exp = sf.read().strip()
                        if exp and self.compute_sha256(raw) != exp:
                            print(f"[ALERTA H-01] Checksum inválido en {cand}. Usando backup...")
                            continue
                    data = json.loads(raw.decode("utf-8"))
                    if cand == self.bak_file:
                        self.save(data)
                    return data
                except Exception:
                    continue
            raise RuntimeError("No se pudo recuperar el cerebro.")

class ResilientLNMClient:
    def __init__(self, api_key, api_secret, passphrase, base_url="https://api.lnmarkets.com"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url
        self.session = requests.Session()
        self.exec_lock = threading.RLock()
        self.cb_failures = 0
        self.cb_open_until = 0.0

    def _sign(self, method, path, body=""):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.lower()}{path}{body}"
        sig = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        return {
            "LNM-ACCESS-KEY": self.api_key,
            "LNM-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "LNM-ACCESS-TIMESTAMP": ts,
            "LNM-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def request(self, method, path, payload_dict=None, is_guardian_priority=False):
        with self.exec_lock:
            if not is_guardian_priority and time.time() < self.cb_open_until:
                return None
            body = json.dumps(payload_dict, separators=(",", ":")) if payload_dict else ""
            for att in range(1, 4):
                try:
                    r = self.session.request(method, f"{self.base_url}{path}", headers=self._sign(method, path, body), data=body, timeout=7.0)
                    if r.status_code in (200, 201):
                        self.cb_failures = 0
                        return r.json()
                    if r.status_code in (429, 500, 502, 503, 504):
                        self.cb_failures += 1
                        bo = min(30.0, (1.8 ** att) + 0.5)
                        if self.cb_failures >= 3:
                            self.cb_open_until = time.time() + max(15.0, bo * 2.0)
                        time.sleep(bo)
                        continue
                    return None
                except requests.exceptions.RequestException:
                    self.cb_failures += 1
                    if method.upper() == "POST" and "/trade" in path:
                        return {"_uncertain_post_timeout": True}
                    time.sleep(min(10.0, 1.5 ** att))
            return None

    def get_running_trades(self):
        res = self.request("GET", "/v3/futures/isolated/trades/running", is_guardian_priority=True)
        return res if isinstance(res, list) else (res.get("trades", []) if isinstance(res, dict) else None)

    def close_trade_market(self, trade_id):
        return self.request("DELETE", "/v3/futures/isolated/trade", {"id": trade_id, "type": "market"}, is_guardian_priority=True)

class HardFloorGuardian:
    def __init__(self, store, client, max_concurrent=5, taker_fee=0.001):
        self.store = store
        self.client = client
        self.max_concurrent = max_concurrent
        self.taker_fee = taker_fee
        self.raw_peak_equity = 0

    def reconcile_orphan_positions(self, brain_data, current_atr, current_state="RECONCILED"):
        rem = self.client.get_running_trades()
        if rem is None:
            return False, 0, 0
        loc = brain_data.setdefault("active_trades", {})
        rmap = {str(p.get("id") or p.get("pid")): p for p in rem if (p.get("id") or p.get("pid"))}
        ad, pu = 0, 0
        for pid, rp in rmap.items():
            if pid not in loc:
                ib = str(rp.get("side", "b")).lower() in ("b", "buy", "long")
                loc[pid] = {"id": pid, "side": "buy" if ib else "sell", "entry_price": float(rp.get("price", 0.0)), "margin": int(rp.get("margin", 1500)), "leverage": int(rp.get("leverage", 20)), "atr_at_entry": max(float(current_atr), 50.0), "action": 3 if ib else 7, "state": current_state, "adopted_orphan": True}
                ad += 1
        for lpid in list(loc.keys()):
            if lpid not in rmap:
                del loc[lpid]
                pu += 1
        if ad > 0 or pu > 0:
            self.store.save(brain_data)
        return True, ad, pu

    def compute_true_equity(self, avail, rem):
        m = sum(int(p.get("margin", 0)) for p in rem)
        pl = sum(int(p.get("pl", 0)) for p in rem)
        f = sum(int(int(p.get("margin", 0)) * int(p.get("leverage", 20)) * self.taker_fee) for p in rem)
        return max(0, int(avail) + m + pl - f)

    def enforce_hard_floor_kill_switch(self, avail, rem, brain_data):
        teq = self.compute_true_equity(avail, rem)
        if teq > self.raw_peak_equity:
            self.raw_peak_equity = teq
        fr = float(brain_data.get("milestone_floor_ratio", 0.88))
        ef = max(int(brain_data.get("locked_floor", 0)), int(round(self.raw_peak_equity * fr)))
        brain_data["locked_floor"] = ef
        dd = (self.raw_peak_equity - teq) / max(self.raw_peak_equity, 1)
        if 0 < teq <= ef:
            for p in rem:
                pid = str(p.get("id") or p.get("pid") or "")
                if pid:
                    self.client.close_trade_market(pid)
            self.store.save(brain_data)
            return False, teq, dd, "HARD_FLOOR_KILL_SWITCH"
        return True, teq, dd, "OK"
