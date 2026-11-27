#!/usr/bin/env python3
"""
Guardia determinista y pura (O(1), cero I/O) de microestructura Binance Futures
para filtrar entradas adversas / post-clímax sin dependencias externas.
"""

from dataclasses import dataclass
import math
from typing import Any, Dict, Mapping, Optional, Union

MICRO_VETO_ADV_SQUEEZE = "MICRO_VETO_ADV_SQUEEZE"
MICRO_VETO_COUNTER_FLOW = "MICRO_VETO_COUNTER_FLOW"
MICRO_VETO_POST_CLIMAX_LIQ = "MICRO_VETO_POST_CLIMAX_LIQ"
MICRO_VETO_PURE_CLIMAX = "MICRO_VETO_PURE_CLIMAX"


@dataclass(frozen=True)
class MicroFlowGuardConfig:
    enabled: bool = False
    shadow_only: bool = True
    max_age_sec: float = 5.0
    min_trades_1m: int = 30
    min_vol_1m_usd: float = 250_000.0
    min_liq_1m_usd: float = 75_000.0
    adv_squeeze_cvd_1m_max: float = -0.15
    adv_squeeze_liq_dir_max: float = -0.50
    counter_flow_cvd_1m_max: float = -0.30
    counter_flow_cvd_5m_usd_max: float = -25_000_000.0
    post_climax_liq_dir_min: float = 0.60
    post_climax_cvd_1m_min: float = 0.15
    post_climax_cvd_5m_usd_min: float = 35_000_000.0
    pure_climax_cvd_1m_min: float = 0.55
    pure_climax_cvd_5m_usd_min: float = 60_000_000.0


@dataclass(frozen=True)
class MicroFlowDecision:
    allowed: bool
    vetoed: bool
    would_veto: bool
    reason: str
    rule_code: Optional[str]
    shadow_only: bool
    fail_open: bool
    side_sign: int = 0
    dir_cvd_1m: float = 0.0
    dir_cvd_5m_usd: float = 0.0
    l_dir: float = 0.0
    vol_tot_1m_usd: float = 0.0
    liq_tot_1m_usd: float = 0.0
    i_cvd_1m: float = 0.0
    i_liq_1m: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "vetoed": self.vetoed,
            "would_veto": self.would_veto,
            "reason": self.reason,
            "rule_code": self.rule_code,
            "shadow_only": self.shadow_only,
            "fail_open": self.fail_open,
            "side_sign": self.side_sign,
            "dir_cvd_1m": self.dir_cvd_1m,
            "dir_cvd_5m_usd": self.dir_cvd_5m_usd,
            "l_dir": self.l_dir,
            "vol_tot_1m_usd": self.vol_tot_1m_usd,
            "liq_tot_1m_usd": self.liq_tot_1m_usd,
            "i_cvd_1m": self.i_cvd_1m,
            "i_liq_1m": self.i_liq_1m,
        }


def normalize_side_sign(side: Union[str, int]) -> int:
    """Normaliza acción discreta (1..8), signo (-1/1) o texto a +1 (LONG), -1 (SHORT) o 0."""
    if isinstance(side, bool):
        return 0
    if isinstance(side, int):
        if side in (1, 2, 3, 4):
            return 1
        if side == -1 or side in (5, 6, 7, 8):
            return -1
        return 0
    if isinstance(side, str):
        norm = side.strip().lower()
        if norm in ("b", "buy", "long", "+"):
            return 1
        if norm in ("s", "sell", "short", "-"):
            return -1
        return 0
    return 0


def _parse_finite_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _parse_nonneg_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fval) or fval < 0.0:
        return None
    return int(fval)


def evaluate_micro_entry_guard(
    side: Union[str, int],
    snap: Optional[Mapping[str, Any]],
    cfg: Optional[MicroFlowGuardConfig] = None,
    *,
    now_ts: Optional[float] = None,
) -> MicroFlowDecision:
    """Evalúa las 4 reglas simétricas de microestructura con fail-open estricto."""
    eff_cfg = cfg if cfg is not None else MicroFlowGuardConfig()
    if not eff_cfg.enabled:
        return MicroFlowDecision(
            allowed=True,
            vetoed=False,
            would_veto=False,
            reason="DISABLED",
            rule_code=None,
            shadow_only=eff_cfg.shadow_only,
            fail_open=False,
        )

    def _fail_open(reason: str, *, side_sign: int = 0) -> MicroFlowDecision:
        return MicroFlowDecision(
            allowed=True,
            vetoed=False,
            would_veto=False,
            reason=reason,
            rule_code=None,
            shadow_only=eff_cfg.shadow_only,
            fail_open=True,
            side_sign=side_sign,
        )

    s = normalize_side_sign(side)
    if s == 0:
        return _fail_open("FAIL_OPEN_INVALID_SIDE")

    if snap is None or not isinstance(snap, Mapping):
        return _fail_open("FAIL_OPEN_NONE", side_sign=s)

    if snap.get("available") is False or snap.get("connected") is not True:
        return _fail_open("FAIL_OPEN_DISCONNECTED", side_sign=s)

    if bool(snap.get("stale", False)) is True:
        return _fail_open("FAIL_OPEN_STALE", side_sign=s)

    age: Optional[float] = None
    if "age_sec" in snap and snap["age_sec"] is not None:
        age = _parse_finite_float(snap["age_sec"])
    else:
        ts_raw: Any = None
        for ts_key in ("ts", "updated_ts", "updated_at_ts"):
            if ts_key in snap and snap[ts_key] is not None:
                ts_raw = snap[ts_key]
                break
        if ts_raw is not None:
            ts_f = _parse_finite_float(ts_raw)
            if ts_f is not None:
                if now_ts is not None:
                    now_f = _parse_finite_float(now_ts)
                    if now_f is not None:
                        age = now_f - ts_f
                else:
                    age = 0.0

    if age is None or not math.isfinite(age) or age > eff_cfg.max_age_sec:
        return _fail_open("FAIL_OPEN_AGE", side_sign=s)

    trades_1m = _parse_nonneg_int(snap.get("trades_1m"))
    buy_vol = _parse_finite_float(snap.get("buy_vol_1m_usd"))
    sell_vol = _parse_finite_float(snap.get("sell_vol_1m_usd"))
    cvd_5m = _parse_finite_float(snap.get("cvd_5m_usd"))
    liq_buy = _parse_finite_float(snap.get("liq_buy_1m_usd"))
    liq_sell = _parse_finite_float(snap.get("liq_sell_1m_usd"))

    if (
        trades_1m is None
        or buy_vol is None
        or sell_vol is None
        or cvd_5m is None
        or liq_buy is None
        or liq_sell is None
        or buy_vol < 0.0
        or sell_vol < 0.0
        or liq_buy < 0.0
        or liq_sell < 0.0
    ):
        return _fail_open("FAIL_OPEN_PARSE_ERROR", side_sign=s)

    vol_tot = buy_vol + sell_vol
    if trades_1m < eff_cfg.min_trades_1m or vol_tot < eff_cfg.min_vol_1m_usd:
        return _fail_open("FAIL_OPEN_LOW_VOLUME", side_sign=s)

    i_cvd_1m = (buy_vol - sell_vol) / max(vol_tot, 1e-6)
    liq_tot = liq_buy + liq_sell
    i_liq_1m = ((liq_sell - liq_buy) / max(liq_tot, 1e-6)) if liq_tot > 0.0 else 0.0

    dir_cvd_1m = s * i_cvd_1m
    dir_cvd_5m = s * cvd_5m
    l_dir = -s * i_liq_1m

    rule_code: Optional[str] = None
    if (
        liq_tot >= eff_cfg.min_liq_1m_usd
        and dir_cvd_1m <= eff_cfg.adv_squeeze_cvd_1m_max
        and l_dir <= eff_cfg.adv_squeeze_liq_dir_max
    ):
        rule_code = MICRO_VETO_ADV_SQUEEZE
    elif (
        dir_cvd_1m <= eff_cfg.counter_flow_cvd_1m_max
        and dir_cvd_5m <= eff_cfg.counter_flow_cvd_5m_usd_max
    ):
        rule_code = MICRO_VETO_COUNTER_FLOW
    elif (
        liq_tot >= eff_cfg.min_liq_1m_usd
        and l_dir >= eff_cfg.post_climax_liq_dir_min
        and dir_cvd_1m >= eff_cfg.post_climax_cvd_1m_min
        and dir_cvd_5m >= eff_cfg.post_climax_cvd_5m_usd_min
    ):
        rule_code = MICRO_VETO_POST_CLIMAX_LIQ
    elif (
        dir_cvd_1m >= eff_cfg.pure_climax_cvd_1m_min
        and dir_cvd_5m >= eff_cfg.pure_climax_cvd_5m_usd_min
    ):
        rule_code = MICRO_VETO_PURE_CLIMAX

    if rule_code is not None:
        return MicroFlowDecision(
            allowed=bool(eff_cfg.shadow_only),
            vetoed=not eff_cfg.shadow_only,
            would_veto=True,
            reason=rule_code,
            rule_code=rule_code,
            shadow_only=eff_cfg.shadow_only,
            fail_open=False,
            side_sign=s,
            dir_cvd_1m=dir_cvd_1m,
            dir_cvd_5m_usd=dir_cvd_5m,
            l_dir=l_dir,
            vol_tot_1m_usd=vol_tot,
            liq_tot_1m_usd=liq_tot,
            i_cvd_1m=i_cvd_1m,
            i_liq_1m=i_liq_1m,
        )

    return MicroFlowDecision(
        allowed=True,
        vetoed=False,
        would_veto=False,
        reason="PASS",
        rule_code=None,
        shadow_only=eff_cfg.shadow_only,
        fail_open=False,
        side_sign=s,
        dir_cvd_1m=dir_cvd_1m,
        dir_cvd_5m_usd=dir_cvd_5m,
        l_dir=l_dir,
        vol_tot_1m_usd=vol_tot,
        liq_tot_1m_usd=liq_tot,
        i_cvd_1m=i_cvd_1m,
        i_liq_1m=i_liq_1m,
    )
