# -*- coding: utf-8 -*-
"""
simulation/lane_budget.py
Aislamiento de carriles (Carril A / Carril B) y contabilidad exacta de contrato inverso BTCUSD.
Módulo 100% puro, cero I/O, portable Termux/WSL (solo stdlib).
"""
from dataclasses import dataclass
from typing import Dict, Optional


def _normalize_side(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in ("b", "buy", "long"):
        return "b"
    if s in ("s", "sell", "short"):
        return "s"
    raise ValueError(f"Lado de orden inválido: {side!r}")


def compute_inverse_loss_at_sl_sats(
    margin_sats: float,
    leverage: float,
    entry_price: float,
    sl_price: float,
    fee_rate_open: float = 0.0008,
    fee_rate_close: float = 0.0008,
) -> float:
    """
    Calcula la pérdida máxima estimada al Stop Loss en contrato inverso BTCUSD (sats):
      l_sl = abs(P0 - P_SL) / P_SL + f_open + (P0 / P_SL) * f_close
      LossAtSL = margin_sats * leverage * l_sl
    """
    m = float(margin_sats)
    lev = float(leverage)
    p0 = float(entry_price)
    p_sl = float(sl_price)
    f_open = float(fee_rate_open)
    f_close = float(fee_rate_close)

    if m < 0.0 or lev < 0.0:
        raise ValueError(f"Margen y apalancamiento deben ser >= 0 (margin={m}, leverage={lev}).")
    if p0 <= 0.0 or p_sl <= 0.0:
        raise ValueError(f"entry_price y sl_price deben ser > 0 (P0={p0}, P_SL={p_sl}).")
    if f_open < 0.0 or f_close < 0.0:
        raise ValueError(f"Las tarifas deben ser >= 0 (open={f_open}, close={f_close}).")
    if m == 0.0 or lev == 0.0:
        return 0.0

    l_sl = abs(p0 - p_sl) / p_sl + f_open + (p0 / p_sl) * f_close
    return m * lev * l_sl


def compute_inverse_net_pnl_sats(
    side: str,
    margin_sats: float,
    leverage: float,
    entry_price: float,
    exit_price: float,
    fee_rate_open: float = 0.0008,
    fee_rate_close: float = 0.0008,
    funding_sats: float = 0.0,
) -> Dict[str, float]:
    """
    Contabilidad exacta de contrato inverso BTCUSD (sats):
      gross_pnl = M * L * (1 - P0 / P1) * d
      c_open    = M * L * f_open
      c_close   = M * L * (P0 / P1) * f_close
      net_pnl   = gross_pnl - c_open - c_close - funding_sats
    """
    s_key = _normalize_side(side)
    d = 1.0 if s_key == "b" else -1.0

    m = float(margin_sats)
    lev = float(leverage)
    p0 = float(entry_price)
    p1 = float(exit_price)
    f_open = float(fee_rate_open)
    f_close = float(fee_rate_close)
    fund = float(funding_sats)

    if m < 0.0 or lev < 0.0:
        raise ValueError(f"Margen y apalancamiento deben ser >= 0 (margin={m}, leverage={lev}).")
    if p0 <= 0.0 or p1 <= 0.0:
        raise ValueError(f"entry_price y exit_price deben ser > 0 (P0={p0}, P1={p1}).")
    if f_open < 0.0 or f_close < 0.0:
        raise ValueError(f"Las tarifas deben ser >= 0 (open={f_open}, close={f_close}).")

    notional = m * lev
    gross_pnl = notional * (1.0 - p0 / p1) * d
    c_open = notional * f_open
    c_close = notional * (p0 / p1) * f_close
    total_cost = c_open + c_close + fund
    net_pnl = gross_pnl - total_cost

    return {
        "gross_pnl_sats": gross_pnl,
        "opening_fee_sats": c_open,
        "closing_fee_sats": c_close,
        "funding_sats": fund,
        "total_cost_sats": total_cost,
        "net_pnl_sats": net_pnl,
    }


@dataclass(frozen=True)
class LaneBudgetConfig:
    enabled: bool = False
    max_concurrent_total: int = 5
    lane_a_reserved_slots: int = 3
    lane_a_max_slots: int = 5
    lane_b_max_slots: int = 2
    lane_b_max_per_side: int = 1
    lane_b_max_open_total_before_entry: int = 3
    lane_b_max_margin_share: float = 0.15
    lane_b_soft_dd_cut_pct: float = 0.0080
    lane_b_soft_risk_mult: float = 0.50
    lane_b_hard_dd_pause_pct: float = 0.0150
    lane_b_pause_sec: float = 86_400.0
    lane_b_side_loss_streak_limit: int = 2
    protective_dd_a_max_pct: float = 0.0200
    protective_dd_global_max_pct: float = 0.0220
    lane_a_recovery_enter_dd: float = 0.0350
    lane_a_recovery_exit_dd: float = 0.0250


@dataclass(frozen=True)
class LaneEntryDecision:
    allowed: bool
    reason: str
    risk_multiplier: float
    dd_a: float
    dd_b: float
    dd_global: float
    lane_a_recovery_mode: bool
    lane_b_paused: bool


class LaneBudgetController:
    """
    Controlador de presupuesto y aislamiento estricto entre Carril A y Carril B.
    Mantiene sub-libros virtuales independientes anclados a initial_equity_sats para
    impedir cualquier contagio de drawdown desde Carril B hacia Carril A.
    """

    def __init__(
        self,
        initial_equity_sats: float,
        config: Optional[LaneBudgetConfig] = None,
    ) -> None:
        init_eq = float(initial_equity_sats)
        if init_eq <= 0.0:
            raise ValueError(f"initial_equity_sats debe ser > 0 (recibido={init_eq}).")

        self.initial_equity_sats: float = init_eq
        self.config: LaneBudgetConfig = config or LaneBudgetConfig()

        self.e_a: float = init_eq
        self.peak_a: float = init_eq
        self.dd_a: float = 0.0

        self.e_b: float = init_eq
        self.peak_b: float = init_eq
        self.dd_b: float = 0.0

        self.e_global: float = init_eq
        self.peak_global: float = init_eq
        self.dd_global: float = 0.0

        self.lane_a_recovery_mode: bool = False
        self.paused_until_ts_b: float = -1e18
        self.paused_until_ts_b_side: Dict[str, float] = {"b": -1e18, "s": -1e18}
        self.loss_streak_b: Dict[str, int] = {"b": 0, "s": 0}

    @staticmethod
    def _calc_dd(equity: float, peak: float) -> float:
        if peak <= 0.0:
            return 1.0
        return max(0.0, (peak - equity) / peak)

    def _update_global(self, delta_sats: float) -> None:
        self.e_global += float(delta_sats)
        if self.e_global > self.peak_global:
            self.peak_global = self.e_global
        self.dd_global = self._calc_dd(self.e_global, self.peak_global)

    def is_lane_b_paused(self, now_ts: float = 0.0, side: Optional[str] = None) -> bool:
        ts = float(now_ts)
        paused = (ts < self.paused_until_ts_b) or (self.dd_b >= self.config.lane_b_hard_dd_pause_pct)
        if side is not None:
            s_key = _normalize_side(side)
            paused = paused or (ts < self.paused_until_ts_b_side[s_key])
        return paused

    def record_lane_a_pnl(self, delta_sats: float) -> None:
        """Actualiza exclusivamente el sub-libro de Carril A y el libro global."""
        d = float(delta_sats)
        self.e_a += d
        if self.e_a > self.peak_a:
            self.peak_a = self.e_a
        self.dd_a = self._calc_dd(self.e_a, self.peak_a)

        self._update_global(d)

        # Carril A consulta exclusivamente dd_a para su modo de recuperación (cero contagio de B).
        if not self.lane_a_recovery_mode and self.dd_a >= self.config.lane_a_recovery_enter_dd:
            self.lane_a_recovery_mode = True
        elif self.lane_a_recovery_mode and self.dd_a <= self.config.lane_a_recovery_exit_dd:
            self.lane_a_recovery_mode = False

    def record_lane_b_pnl(
        self,
        delta_sats: float,
        now_ts: float = 0.0,
        side: Optional[str] = None,
    ) -> None:
        """Actualiza el sub-libro de Carril B, su kill-switch y el libro global sin tocar Carril A."""
        d = float(delta_sats)
        ts = float(now_ts)

        self.e_b += d
        if self.e_b > self.peak_b:
            self.peak_b = self.e_b
        self.dd_b = self._calc_dd(self.e_b, self.peak_b)

        self._update_global(d)

        if self.dd_b >= self.config.lane_b_hard_dd_pause_pct:
            self.paused_until_ts_b = max(self.paused_until_ts_b, ts + float(self.config.lane_b_pause_sec))

        if side is not None:
            s_key = _normalize_side(side)
            if d <= 0.0:
                self.loss_streak_b[s_key] += 1
                if self.loss_streak_b[s_key] >= self.config.lane_b_side_loss_streak_limit:
                    self.paused_until_ts_b_side[s_key] = max(
                        self.paused_until_ts_b_side[s_key],
                        ts + float(self.config.lane_b_pause_sec),
                    )
            elif d > 0.0:
                self.loss_streak_b[s_key] = 0

    def record_lane_b_trade_close(self, side: str, net_pnl_sats: float, now_ts: float) -> None:
        self.record_lane_b_pnl(delta_sats=net_pnl_sats, now_ts=now_ts, side=side)

    def _decision(
        self,
        *,
        allowed: bool,
        reason: str,
        risk_multiplier: float,
        lane_b_paused: bool,
    ) -> LaneEntryDecision:
        return LaneEntryDecision(
            allowed=allowed,
            reason=reason,
            risk_multiplier=float(risk_multiplier),
            dd_a=self.dd_a,
            dd_b=self.dd_b,
            dd_global=self.dd_global,
            lane_a_recovery_mode=self.lane_a_recovery_mode,
            lane_b_paused=lane_b_paused,
        )

    def evaluate_lane_a_entry(
        self,
        n_open_a: int,
        n_open_b: int,
        now_ts: Optional[float] = None,
    ) -> LaneEntryDecision:
        cfg = self.config
        na = int(n_open_a)
        nb = int(n_open_b)
        b_paused = (
            self.is_lane_b_paused(now_ts=now_ts)
            if now_ts is not None
            else (self.dd_b >= cfg.lane_b_hard_dd_pause_pct or self.paused_until_ts_b > 0.0)
        )
        allowed = (na + nb < cfg.max_concurrent_total) and (na < cfg.lane_a_max_slots)
        return self._decision(
            allowed=allowed,
            reason="LANE_A_ALLOWED" if allowed else "LANE_A_SLOT_CAP",
            risk_multiplier=1.0 if allowed else 0.0,
            lane_b_paused=b_paused,
        )

    def evaluate_lane_b_entry(
        self,
        *,
        side: str,
        now_ts: float,
        n_open_a: int,
        n_open_b_buy: int,
        n_open_b_sell: int,
        avail_balance_sats: float,
        open_margin_b_sats: float,
        margin_new_sats: float,
        open_loss_at_sl_b_sats: float,
        loss_at_sl_new_sats: float,
    ) -> LaneEntryDecision:
        cfg = self.config
        s_key = _normalize_side(side)
        ts = float(now_ts)
        b_paused = self.is_lane_b_paused(now_ts=ts, side=s_key)

        if not cfg.enabled:
            return self._decision(
                allowed=False,
                reason="LANE_B_DISABLED",
                risk_multiplier=0.0,
                lane_b_paused=b_paused,
            )

        if b_paused:
            return self._decision(
                allowed=False,
                reason="LANE_B_PAUSED_24H",
                risk_multiplier=0.0,
                lane_b_paused=True,
            )

        if self.dd_a >= cfg.protective_dd_a_max_pct or self.dd_global >= cfg.protective_dd_global_max_pct:
            return self._decision(
                allowed=False,
                reason="LANE_B_PROTECTIVE_DD_BRAKE",
                risk_multiplier=0.0,
                lane_b_paused=False,
            )

        na = int(n_open_a)
        nb_buy = int(n_open_b_buy)
        nb_sell = int(n_open_b_sell)
        nb = nb_buy + nb_sell
        n_side = nb_buy if s_key == "b" else nb_sell

        slot_ok = (
            (na + nb < cfg.max_concurrent_total)
            and (nb < cfg.lane_b_max_slots)
            and (n_side < cfg.lane_b_max_per_side)
            and (na + nb <= cfg.lane_b_max_open_total_before_entry)
            and (cfg.max_concurrent_total - (nb + 1) >= cfg.lane_a_reserved_slots)
        )
        if not slot_ok:
            return self._decision(
                allowed=False,
                reason="LANE_B_SLOT_CAP",
                risk_multiplier=0.0,
                lane_b_paused=False,
            )

        avail = max(0.0, float(avail_balance_sats))
        if float(open_margin_b_sats) + float(margin_new_sats) > cfg.lane_b_max_margin_share * avail:
            return self._decision(
                allowed=False,
                reason="LANE_B_MARGIN_CAP_15PCT",
                risk_multiplier=0.0,
                lane_b_paused=False,
            )

        open_sl_cap = max(0.0, cfg.lane_b_hard_dd_pause_pct - self.dd_b) * self.e_global
        tol = 1e-9 if open_sl_cap > 0.0 else 0.0
        if float(open_loss_at_sl_b_sats) + float(loss_at_sl_new_sats) > open_sl_cap + tol:
            return self._decision(
                allowed=False,
                reason="LANE_B_OPEN_SL_RISK_CAP",
                risk_multiplier=0.0,
                lane_b_paused=False,
            )

        risk_mult = cfg.lane_b_soft_risk_mult if self.dd_b >= cfg.lane_b_soft_dd_cut_pct else 1.0
        return self._decision(
            allowed=True,
            reason="LANE_B_ALLOWED",
            risk_multiplier=risk_mult,
            lane_b_paused=False,
        )
