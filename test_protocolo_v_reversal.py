# -*- coding: utf-8 -*-
"""
test_protocolo_v_reversal.py
Suite de verificación cuantitativa de los 15 escenarios obligatorios de auditoría
+ Verificación del bloqueo de Override con Q negativo en RANGE_SQUEEZE y Fusión Dual.
"""
import os
import time
import tracemalloc
from guardian_execution_core import (
    AtomicBrainStore,
    HardFloorGuardian,
    FileLockContext,
    LatencyTelemetry,
    InFlightOrderGuard,
    compat_np as np,
)
from sniper_perception_sats_core import (
    MarketRegimeClassifier,
    ShortTermPerceptionEngine,
    SatsExpectancyGate,
    sync_brain_floor_with_reality,
)
from lnm_sniper_v6_advanced import (
    Config,
    AutonomousGoalBrain,
    compute_account_equity,
    infer_adopted_trade,
    compute_situational_order_params,
    evaluate_system_mode,
    execute_strategy_guardian,
    reconcile_portfolio_with_hedges,
    run_integrated_guardian_and_hedge_cycle,
    compute_indicators,
    _build_indicator_prefix_state,
    compute_indicators_from_prefix,
)
from sync_brains_dual import sync_dual_brains
from hedge_state_store import HedgeStateStore, HEDGE_STATES, HEDGE_ACTIONS, NUM_HEDGE_ACTIONS
from edge_monitor import EdgeMonitor
from predictive_hedge import PredictiveHedgeEngine
from portfolio_hedge import PortfolioHedgeCoordinator
from hedge_risk_manager import HedgeRiskManager
from dynamic_hedge import DynamicHedgeExecutor
from adaptive_controller import AdaptiveHedgeController


class MockLNMClient:
    def __init__(self, running_list=None, sl_confirm=True, close_confirm=True):
        self.running_list = list(running_list or [])
        self.sl_confirm = sl_confirm
        self.close_confirm = close_confirm
        self.sl_calls = []
        self.close_calls = []
        self.open_calls = []
        self.cash_in_calls = []

    def open_trade(self, side, margin, leverage, stoploss, takeprofit):
        tid = f"h-mock-{len(self.open_calls) + 1}"
        new_t = {
            "id": tid,
            "side": side,
            "margin": int(margin),
            "leverage": int(leverage),
            "stoploss": int(round(stoploss)),
            "takeprofit": int(round(takeprofit)),
            "price": 77600.0,
            "pl": 0,
        }
        self.open_calls.append(new_t)
        self.running_list.append(new_t)
        return new_t

    def cash_in(self, tid, amount_sats):
        self.cash_in_calls.append((str(tid), int(amount_sats)))
        return {"id": str(tid), "cashed_in": int(amount_sats)}

    def update_stoploss(self, tid, target_sl):
        self.sl_calls.append((str(tid), int(target_sl)))
        if self.sl_confirm:
            return {"id": str(tid), "stoploss": int(target_sl)}
        return {}

    def close_trade(self, tid):
        self.close_calls.append(str(tid))
        if self.close_confirm:
            self.running_list = [t for t in self.running_list if str(t.get("id")) != str(tid)]
            return {"id": str(tid), "closed": True}
        return {}

    def get_running_trades(self):
        return self.running_list


def run_audit_protocol():
    print("=== INICIANDO PROTOCOLO DE AUDITORÍA CUANTITATIVA v7.3 DUAL (15 ESCENARIOS) ===\n")
    Config.configure_environment("mainnet")
    engine = ShortTermPerceptionEngine()
    now = time.time()

    state_v_rally = "RANGE_SQUEEZE|PEAK|LOW_EXP|CHOP|BEAR_RALLY"
    engine._record_sample(now - 300, 76800.0, 220.0)
    engine._record_sample(now - 200, 77150.0, 220.0)
    engine._record_sample(now - 100, 77550.0, 220.0)

    ind_5m_bull = {
        "price": 78050.0, "atr": 220.0, "atr_pct": 0.0028, "rsi": 66.5, "bb_pos": 0.82,
        "ema9": 77820.0, "ema21": 77450.0, "high": 78090.0, "low": 77600.0,
        "open": 77620.0, "regime": "TREND_BULL", "ema_score": 1.0,
    }
    ind_15m_bull = {"rsi": 59.0, "bb_pos": 0.70, "ema9": 77600.0, "ema21": 77200.0, "ema_score": 1.0, "regime": "TREND_BULL"}
    ind_1h_lag = {"rsi": 54.5, "ema9": 77500.0, "ema21": 77300.0, "ema_score": 0.35, "regime": "TREND_BULL"}
    ind_4h_bear_lag = {"rsi": 43.0, "ema9": 77000.0, "ema21": 77900.0, "ema_score": -0.35, "regime": "TREND_BEAR"}

    perception = engine.evaluate(state_v_rally, ind_5m_bull, ind_15m_bull, ind_1h_lag, ind_4h_bear_lag, now=now)
    mock_q = [0.01, 0.05, 0.02, 0.04, 0.03, 0.01, 0.02, 0.12, 0.01]
    mock_strats = {i: {"name": f"STRAT_{i}", "side": "buy" if i <= 4 else "sell"} for i in range(1, 9)}

    # 1. ESCENARIO 1: NO_TRADE (0) gana en selección greedy ante Q negativos
    tmp_brain = AutonomousGoalBrain(filename="test_tmp_brain.json", explorer_mode=False)
    st_neg = "RANGE_NORMAL|MID|LOW_EXP|CHOP|EMA_TRANS"
    tmp_brain.q_table[st_neg] = [0.02, -0.15, -0.22, -0.18, -0.12, -0.25, -0.30, -0.19, -0.14]
    tmp_brain.state_visits[st_neg] = 95
    tmp_brain.epsilon = 0.0
    greedy_no_trade = tmp_brain.choose_action(st_neg, ind_5m_bull, ind_15m_bull, ind_1h_lag, ind_4h_bear_lag, recovery_mode=False, current_dd_pct=0.01)
    assert greedy_no_trade == 0, f"Fallo E1: NO_TRADE debía ganar, obtuvo {greedy_no_trade}"
    print("✅ ESCENARIO 1 (NO_TRADE Elegible y Ganador en Selección Greedy): PASÓ.")

    # 2. ESCENARIO 2: Integridad 9 acciones + Redirección Giro en V cuando hay acciones con Q positivo
    final_action, reason = SatsExpectancyGate.filter_and_rank_action(
        raw_action=7, state=state_v_rally, q_values=mock_q,
        strategies=mock_strats, perception=perception,
        recovery_mode=False, current_dd_pct=0.02,
    )
    assert len(tmp_brain.get_state_q_values(state_v_rally)) == 9 and final_action in [1, 3, 4], f"Fallo E2: {final_action}"
    print(f"✅ ESCENARIO 2 (Integridad 9 Acciones + Redirección Giro en V con Q válido): PASÓ -> Acción {final_action} ({reason}).")

    # 3. ESCENARIO 3: Jerarquía P0 (RANGE_SQUEEZE|HIGH_EXP bloquea en Mainnet + Bloqueo de OVR con Q negativo)
    sq_high_ovr_act, sq_high_ovr_reason = SatsExpectancyGate.filter_and_rank_action(
        raw_action=3, state="RANGE_SQUEEZE|MID|HIGH_EXP|BULL|FULL_BULL",
        q_values=mock_q, strategies=mock_strats, perception=perception,
        recovery_mode=False, current_dd_pct=0.03, explorer_mode=False
    )
    assert sq_high_ovr_act == 0 and sq_high_ovr_reason == "BLOCKED_SQUEEZE_HIGH_EXP", f"Fallo E3a: {sq_high_ovr_reason}"
    all_neg_short_q = [0.02, -0.30, -0.35, -0.29, -0.37, -0.339, -0.32, -0.40, -0.32]
    bear_ovr_perc = {"override_side": "BEAR", "vel_ema_atr": -1.5, "rsi_5m": 35.0, "breakout_bear": True}
    blk_act, blk_r = SatsExpectancyGate.filter_and_rank_action(
        raw_action=5, state="RANGE_SQUEEZE|DIP|LOW_EXP|BULL|BULL_DIP",
        q_values=all_neg_short_q, strategies=mock_strats, perception=bear_ovr_perc,
        recovery_mode=False, current_dd_pct=0.04, explorer_mode=False
    )
    assert blk_act == 0, f"Fallo E3b: Debía bloquear SHORT_TREND con Q=-0.339 en RANGE_SQUEEZE ({blk_act}, {blk_r})"
    print("✅ ESCENARIO 3 (Jerarquía P0: Bloqueo SQUEEZE|HIGH_EXP y Bloqueo Override con Q Negativo): PASÓ.")

    # 4. ESCENARIO 4: Exposición Proyectada (<= 38%) y Presupuesto de Lote Mínimo
    exp_ok_1, exp_r_1, proj_1 = SatsExpectancyGate.verify_projected_exposure(
        locked_margin_sats=8158, new_margin_sats=2700, equity_sats=25795, available_balance_sats=15808
    )
    assert exp_ok_1 is False and "PROJECTED_EXPOSURE_EXCEEDED" in exp_r_1, f"Fallo E4a: {exp_r_1}"
    exp_ok_2, exp_r_2, _ = SatsExpectancyGate.verify_projected_exposure(
        locked_margin_sats=0, new_margin_sats=1000, equity_sats=3500, available_balance_sats=3500, max_single_trade_risk_pct=0.22
    )
    assert exp_ok_2 is False and "MIN_LOT_EXCEEDS_RISK_BUDGET" in exp_r_2, f"Fallo E4b: {exp_r_2}"
    print(f"✅ ESCENARIO 4 (Exposición Proyectada {proj_1*100:.1f}% Bloqueada + Control Lote Mínimo): PASÓ.")

    # 5. ESCENARIO 5: Protección de Pérdidas Diarias y Drawdown Conectadas
    mode_dd, allow_dd, _, _, _ = evaluate_system_mode(now, 0.0, ind_5m_bull, daily_pnl_pct=-0.02, current_dd_pct=0.165, floor_threat=False, equity=21000, recovery_anchor_equity=None)
    mode_dl, allow_dl, _, _, _ = evaluate_system_mode(now, 0.0, ind_5m_bull, daily_pnl_pct=-0.115, current_dd_pct=0.05, floor_threat=False, equity=22000, recovery_anchor_equity=None)
    assert mode_dd == "DEFENSIVE" and allow_dd is False and mode_dl == "DEFENSIVE" and allow_dl is False
    print("✅ ESCENARIO 5 (Conexión Real de MAX_DAILY_LOSS_PCT y MAX_DRAWDOWN_PCT): PASÓ.")

    # 6. ESCENARIO 6: Confirmación Real de SL y Cierres en el Guardian
    mock_trade = {"id": "t-confirm-1", "side": "buy", "margin": 1500, "leverage": 30, "price": 77500.0, "stoploss": 77100.0, "liquidation": 75200.0, "pl": 220}
    meta_unconfirmed = infer_adopted_trade(mock_trade, ind_5m_bull, state_v_rally, now, forced_action=4)
    client_fail = MockLNMClient(running_list=[mock_trade], sl_confirm=False, close_confirm=False)
    closed_f = execute_strategy_guardian(client_fail, tmp_brain, mock_trade, ind_5m_bull, ind_15m_bull, meta_unconfirmed, "NORMAL", edge_score=0.1)
    assert closed_f is False and meta_unconfirmed["ai_closed"] is False
    client_ok = MockLNMClient(running_list=[mock_trade], sl_confirm=True, close_confirm=True)
    meta_confirmed = infer_adopted_trade(mock_trade, ind_5m_bull, state_v_rally, now, forced_action=4)
    closed_ok = execute_strategy_guardian(client_ok, tmp_brain, mock_trade, ind_5m_bull, ind_15m_bull, meta_confirmed, "NORMAL", edge_score=0.1)
    assert closed_ok is True and meta_confirmed["ai_closed"] is True
    print("✅ ESCENARIO 6 (Confirmación de Servidor para Cierres y Bloqueo de SL): PASÓ.")

    # 7. ESCENARIO 7: Reconciliación y Adopción Conservadora DEF_SCALP
    orphan_meta = infer_adopted_trade({"id": "orphan-1", "side": "buy", "leverage": 75, "margin": 1000, "price": 78050.0}, ind_5m_bull, state_v_rally, now)
    assert orphan_meta["action"] == 4 and orphan_meta["style"] == "DEF_SCALP"
    print("✅ ESCENARIO 7 (Reconciliación y Adopción Conservadora DEF_SCALP): PASÓ.")

    # 8. ESCENARIO 8: Compatibilidad del Cerebro Existente
    prod_brain = AutonomousGoalBrain(filename="q_brain_advanced_lnm.json", explorer_mode=False)
    assert len(prod_brain.q_table) >= 100
    print(f"✅ ESCENARIO 8 (Compatibilidad Cerebro Existente: {len(prod_brain.q_table)} estados cargados): PASÓ.")

    # 9. ESCENARIO 9: Persistencia Atómica, Bloqueo fcntl, SHA-256 y Recuperación desde .bak
    store_test = AtomicBrainStore("test_atomic_store.json")
    digest = store_test.save({"q_table": {st_neg: [0.02]*9}, "current_target": 300000})
    store_test.save({"q_table": {st_neg: [0.05]*9}, "current_target": 300000})
    with open("test_atomic_store.json", "w", encoding="utf-8") as bad_f:
        bad_f.write("{corrupted_json")
    recovered = store_test.load()
    assert isinstance(recovered, dict) and "q_table" in recovered
    for ext in ("", ".bak", ".sha256", ".lock"):
        if os.path.exists(f"test_atomic_store.json{ext}"):
            os.remove(f"test_atomic_store.json{ext}")
    print(f"✅ ESCENARIO 9 (Persistencia Atómica fcntl + SHA-256 {digest[:12]}... y Recuperación .bak): PASÓ.")

    # 10. ESCENARIO 10: Avance de Meta Exclusivo por Balance Disponible
    tmp_brain.current_target = 300000
    tmp_brain.milestone_level = 1
    assert tmp_brain.check_and_advance_goal(available_balance=290000) is False
    assert tmp_brain.check_and_advance_goal(available_balance=300500) is True and tmp_brain.locked_floor == 264000
    print("✅ ESCENARIO 10 (Objetivo Económico P0: Avance Exclusivo por Balance Disponible): PASÓ.")

    # 11. ESCENARIO 11: Recompensas Netas sin Doble Comisión + Telemetría por Estilo
    t_before = tmp_brain.realized_net_profit_sats
    tmp_brain.remember_and_train(
        st_neg, 3, reward_sats=250, margin_used=2000, next_state=state_v_rally,
        available_balance_after=25250, real_balance_delta_sats=250,
        hit_tp=True, harvested=True, recovery_mode=False, lev_used=30.0, exact_fee_sats=80
    )
    assert (tmp_brain.realized_net_profit_sats - t_before) == 250
    assert tmp_brain.style_metrics["SNIPER"]["trades"] >= 1
    print("✅ ESCENARIO 11 (Recompensa Económica P1 sin Doble Comisión + Métricas por Estilo): PASÓ.")

    # 12. ESCENARIO 12: Separación Aprendizaje Táctico/Económico + PER
    tmp_brain.remember_and_train(
        state_v_rally, 3, reward_sats=320, margin_used=2000,
        next_state="RANGE_NORMAL|MID|LOW_EXP|BULL|FULL_BULL",
        equity_after=28500, hit_tp=True, harvested=True,
        recovery_mode=True, exited_recovery_bonus=True, lev_used=30.0, exact_fee_sats=120
    )
    assert abs(tmp_brain.alpha - 0.28) < 1e-6 and tmp_brain.memory[-1][0] > 0.10
    print("✅ ESCENARIO 12 (Separación Aprendizaje Táctico/Económico + PER): PASÓ.")

    # 13. ESCENARIO 13: Controlador de Degradación de Edge en el Guardian (3 Ciclos)
    losing_trade = {"id": "t-deg-1", "side": "buy", "margin": 1000, "leverage": 25, "price": 78500.0, "stoploss": 77200.0, "liquidation": 75500.0, "pl": -180}
    ind_5m_bear_drop = dict(ind_5m_bull, price=78180.0, rsi=41.0)
    bear_perc = {"override_side": "BEAR", "vel_ema_atr": -0.85, "rsi_5m": 41.0}
    deg_meta = infer_adopted_trade(losing_trade, ind_5m_bear_drop, state_v_rally, now, forced_action=3)
    deg_client = MockLNMClient(running_list=[losing_trade], close_confirm=True)
    c1 = execute_strategy_guardian(deg_client, tmp_brain, losing_trade, ind_5m_bear_drop, ind_15m_bull, deg_meta, "RECOVERY", edge_score=-0.85, perception=bear_perc)
    c2 = execute_strategy_guardian(deg_client, tmp_brain, losing_trade, ind_5m_bear_drop, ind_15m_bull, deg_meta, "RECOVERY", edge_score=-0.88, perception=bear_perc)
    c3 = execute_strategy_guardian(deg_client, tmp_brain, losing_trade, ind_5m_bear_drop, ind_15m_bull, deg_meta, "RECOVERY", edge_score=-0.92, perception=bear_perc)
    assert c1 is False and c2 is False and c3 is True
    print("✅ ESCENARIO 13 (Guardian Edge Degradation Controller con Persistencia 3 Ciclos): PASÓ.")

    # 14. ESCENARIO 14: Validación Matemática SL vs Liquidación y Latencia
    t0 = time.perf_counter()
    lev_safe, sl_safe, tp_safe = compute_situational_order_params(
        raw_lev=90, raw_sl=76000.0, raw_tp=81000.0, strat=tmp_brain.strategies[3],
        state=state_v_rally, ind_5m=ind_5m_bull, ind_15m=ind_15m_bull, ind_1h=ind_1h_lag, ind_4h=ind_4h_bear_lag,
        mods=tmp_brain.get_parallel_modifiers(), brain=tmp_brain, action=3,
        recovery_mode=False, current_dd_pct=0.02, consecutive_losses=0, combined_rr=1.3, perception=perception
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    max_safe_dist = ind_5m_bull["price"] * (0.70 / lev_safe)
    actual_sl_dist = abs(ind_5m_bull["price"] - sl_safe)
    assert actual_sl_dist <= max_safe_dist + 1.0 and elapsed_ms < 15.0
    print(f"✅ ESCENARIO 14 (Garantía Matemática SL <= MaxSafeDist a {lev_safe}x | Latencia {elapsed_ms:.2f}ms): PASÓ.")

    # 15. ESCENARIO 15: Sincronización de Piso Unificado y Fusión Dual Testnet -> Mainnet
    t_main_f = "test_sync_main.json"
    t_test_f = "test_sync_test.json"
    AtomicBrainStore(t_main_f).save({"q_table": {st_neg: [0.02, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01]}, "state_visits": {st_neg: 5}})
    AtomicBrainStore(t_test_f).save({"q_table": {st_neg: [0.02, 0.35, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01]}, "state_visits": {st_neg: 30}})
    assert sync_dual_brains(t_main_f, t_test_f, min_testnet_visits=15) is True
    synced_main = AtomicBrainStore(t_main_f).load()
    assert synced_main["q_table"][st_neg][1] > 0.05, "Fallo E15: Nutrición Testnet -> Mainnet no transfirió Q de alta confianza."
    print("✅ ESCENARIO 15 (Fusión Atómica Testnet ➔ Mainnet + Piso Unificado): PASÓ.")

    # 16. ESCENARIO 16: Telemetría P50/P95/P99, Bloqueo por Latencia y Guardia Anti-Duplicados (Fase 1)
    lat = LatencyTelemetry(window_size=32)
    for v_ms in [120, 145, 160, 180, 210, 240, 290, 310, 420, 890]:
        lat.record("api_ms", v_ms)
        lat.record("cycle_ms", v_ms + 45)
    p_stats = lat.get_percentiles("api_ms")
    assert p_stats["p50"] > 0 and p_stats["p95"] >= p_stats["p50"] and p_stats["p99"] >= p_stats["p95"]
    deg_ok, _ = lat.evaluate_execution_health(max_last_api_ms=3500.0)
    assert deg_ok is False
    lat.record("api_ms", 4200.0)
    deg_bad, deg_reason = lat.evaluate_execution_health(max_last_api_ms=3500.0)
    assert deg_bad is True and "LATENCY_API_SPIKE" in deg_reason
    guard = InFlightOrderGuard(ttl_sec=15.0)
    fp = guard.register_intent("buy", 1500, 25, state_key=st_neg, now=now)
    can_dup, dup_r = guard.can_send_order("buy", 1500, 25, now=now + 2.0)
    assert can_dup is False and "DUPLICATE_INFLIGHT_BLOCKED" in dup_r
    guard.clear_intent(fp)
    can_after, _ = guard.can_send_order("buy", 1500, 25, now=now + 2.0)
    assert can_after is True
    with FileLockContext("test_lock_check.tmp", exclusive=True, timeout=2.0) as lk:
        assert lk.acquired is True
    if os.path.exists("test_lock_check.tmp.lock"):
        os.remove("test_lock_check.tmp.lock")
    print("✅ ESCENARIO 16 (Telemetría P50/P95/P99 + Bloqueo Latencia + Guardia Anti-Duplicados + FileLock): PASÓ.")

    # 17. ESCENARIO 17: Clasificación de 6 Regímenes y Bloqueo de Contratendencia en VOLATILITY_EXPANSION (Fase 2)
    r_squeeze = MarketRegimeClassifier.classify({"price": 78000, "atr": 90, "atr_pct": 0.0011, "atr_rel_ratio": 0.85, "bb_width_pct": 0.0055, "ema9": 78010, "ema21": 78000, "ema_score": 0.0})
    r_vol_exp = MarketRegimeClassifier.classify({"price": 78000, "atr": 390, "atr_pct": 0.0050, "atr_rel_ratio": 1.55, "bb_width_pct": 0.0140, "bb_expansion_rate": 1.35, "ema9": 78300, "ema21": 77900, "ema_score": 1.0})
    r_ext_vol = MarketRegimeClassifier.classify({"price": 78000, "atr": 780, "atr_pct": 0.0100, "atr_rel_ratio": 2.40, "bb_width_pct": 0.0250, "ema9": 78050, "ema21": 78000, "ema_score": 0.0})
    assert r_squeeze["market_regime"] == "RANGE_SQUEEZE"
    assert r_vol_exp["market_regime"] == "VOLATILITY_EXPANSION"
    assert r_ext_vol["market_regime"] == "EXTREME_VOLATILITY"
    blk_ve_act, blk_ve_r = SatsExpectancyGate.filter_and_rank_action(
        raw_action=6, state="TREND_BULL|PEAK|LOW_EXP|BULL|FULL_BULL",
        q_values=[0.01, 0.08, 0.05, 0.07, 0.04, 0.02, 0.09, 0.03, 0.05],
        strategies=mock_strats, perception={"override_side": None, "market_regime": "VOLATILITY_EXPANSION"},
        recovery_mode=False, current_dd_pct=0.02, explorer_mode=False
    )
    assert blk_ve_act == 0 and "BLOCKED_REVERSION_DEF_IN_VOLATILITY_EXPANSION" in blk_ve_r
    print("✅ ESCENARIO 17 (Detección 6 Regímenes + Bloqueo Contratendencia en Expansión): PASÓ.")

    # 18. ESCENARIO 18: Contabilidad Económica Completa (Taker, Funding, Spread, Slippage) y Bloqueo Sync con Trades Activos (Fase 3)
    econ = SatsExpectancyGate.compute_trade_economic_breakdown(
        gross_pnl_sats=450, margin_sats=2000, leverage=25,
        exact_fee_sats=100, funding_fee_sats=18,
        entry_price=78080.0, decision_price=78000.0, spread_bps=1.0, side="buy"
    )
    assert econ["gross_pnl_sats"] == 450 and econ["net_pnl_sats"] < 450
    assert econ["taker_fee_sats"] == 100 and econ["funding_fee_sats"] == 18 and econ["slippage_sats"] > 0
    AtomicBrainStore(t_main_f).save({
        "q_table": {st_neg: [0.02]*9},
        "state_visits": {st_neg: 10},
        "active_trades": {"active-1": {"side": "buy", "margin": 1000}}
    })
    assert sync_dual_brains(t_main_f, t_test_f, min_testnet_visits=15) is False
    for fn in (t_main_f, t_test_f, "test_tmp_brain.json"):
        for ext in ("", ".bak", ".sha256", ".lock"):
            if os.path.exists(f"{fn}{ext}"):
                os.remove(f"{fn}{ext}")
    for f_glob in os.listdir("."):
        if f_glob.startswith("test_sync_main.json.pre_sync_"):
            os.remove(f_glob)
    print("✅ ESCENARIO 18 (Contabilidad Neta Completa + Bloqueo Sync con Trades Activos): PASÓ.")

    # 19. ESCENARIO 19: Hybrid Edge Hedging Engine - Máquina de 7 Estados con Histéresis y 7 Acciones (Fase 5)
    assert len(HEDGE_STATES) == 7 and len(HEDGE_ACTIONS) == 7 and NUM_HEDGE_ACTIONS == 7
    h_store = HedgeStateStore(env_mode="mainnet", filename="test_hedge_store.json")
    orig_t = {
        "id": "orig-long-1", "side": "buy", "margin": 3000, "leverage": 25,
        "price": 78200.0, "stoploss": 77100.0, "liquidation": 75000.0, "pl": -420
    }
    h_client = MockLNMClient(running_list=[orig_t])
    ind_5m_adv = {
        "price": 77600.0, "atr": 250.0, "atr_pct": 0.0032, "rsi": 36.0, "bb_pos": 0.15,
        "ema9": 77650.0, "ema21": 77950.0, "ema50": 78050.0, "ema200": 77200.0,
        "ema_score": -0.65, "regime": "TREND_BEAR", "market_regime": "VOLATILITY_EXPANSION"
    }
    ind_15m_adv = {"rsi": 41.0, "ema_score": -0.55, "regime": "TREND_BEAR"}
    ind_1h_sup = {"rsi": 52.0, "ema_score": 0.35, "regime": "TREND_BULL"}
    perc_adv = {"vel_ema_atr": -1.10, "clv": -0.55, "override_side": "BEAR", "market_regime": "VOLATILITY_EXPANSION"}

    # Ciclo 1: NORMAL -> WARNING (histéresis impide abrir cobertura en el primer ciclo)
    pf_snap = PortfolioHedgeCoordinator.reconcile_portfolio(h_client.running_list, h_store, 30000, 77600.0, now=now)
    r1 = AdaptiveHedgeController.evaluate_and_manage_pair(
        h_client, orig_t, None, {"style": "TREND"}, pf_snap, h_store,
        ind_5m_adv, ind_15m_adv, ind_1h_sup, perc_adv, q_val_main=-0.15,
        account_equity_sats=30000, available_balance_sats=22000, locked_floor_sats=21000, now=now
    )
    assert r1["state"] == "WARNING" and r1["selected_action_name"] == "NO_HEDGE"

    # Ciclo 2 y 3: WARNING -> HEDGE_CANDIDATE -> Ejecución de cobertura en API v3
    r2 = AdaptiveHedgeController.evaluate_and_manage_pair(
        h_client, orig_t, None, {"style": "TREND"}, pf_snap, h_store,
        ind_5m_adv, ind_15m_adv, ind_1h_sup, perc_adv, q_val_main=-0.20,
        account_equity_sats=30000, available_balance_sats=22000, locked_floor_sats=21000, now=now + 10
    )
    r3 = AdaptiveHedgeController.evaluate_and_manage_pair(
        h_client, orig_t, None, {"style": "TREND"}, pf_snap, h_store,
        ind_5m_adv, ind_15m_adv, ind_1h_sup, perc_adv, q_val_main=-0.28,
        account_equity_sats=30000, available_balance_sats=22000, locked_floor_sats=21000, now=now + 20
    )
    opened_res = r2 if r2["execution"].get("executed") else r3
    assert opened_res["execution"].get("executed") is True
    assert len(h_client.open_calls) == 1 and h_client.open_calls[0]["side"] == "sell"

    # Ciclo 4: Con cobertura activa, giro alcista en 5m -> RECOVERY -> CLOSE_HEDGE
    hedge_leg = h_client.open_calls[0]
    hedge_leg["pl"] = 95
    pf_snap2 = PortfolioHedgeCoordinator.reconcile_portfolio(h_client.running_list, h_store, 30000, 77750.0, now=now + 50)
    assert len(pf_snap2["primary_trades"]) == 1 and len(pf_snap2["hedge_trades_map"]) == 1
    ind_5m_rec = dict(ind_5m_bull, price=77750.0)
    perc_rec = {"vel_ema_atr": 0.85, "clv": 0.45, "override_side": "BULL", "market_regime": "TREND_BULL"}
    AdaptiveHedgeController.evaluate_and_manage_pair(
        h_client, orig_t, hedge_leg, {"style": "TREND"}, pf_snap2, h_store,
        ind_5m_rec, ind_15m_bull, ind_1h_sup, perc_rec, q_val_main=0.25,
        account_equity_sats=30000, available_balance_sats=20000, locked_floor_sats=21000, now=now + 50
    )
    AdaptiveHedgeController.evaluate_and_manage_pair(
        h_client, orig_t, hedge_leg, {"style": "TREND"}, pf_snap2, h_store,
        ind_5m_rec, ind_15m_bull, ind_1h_sup, perc_rec, q_val_main=0.30,
        account_equity_sats=30000, available_balance_sats=20000, locked_floor_sats=21000, now=now + 60
    )
    r_unwind = AdaptiveHedgeController.evaluate_and_manage_pair(
        h_client, orig_t, hedge_leg, {"style": "TREND"}, pf_snap2, h_store,
        ind_5m_rec, ind_15m_bull, ind_1h_sup, perc_rec, q_val_main=0.35,
        account_equity_sats=30000, available_balance_sats=20000, locked_floor_sats=21000, now=now + 90
    )
    assert r_unwind["selected_action_name"] == "CLOSE_HEDGE" and r_unwind["execution"].get("executed") is True
    print("✅ ESCENARIO 19 (Hybrid Edge Hedging Engine: Máquina 7 Estados + Apertura y Cierre de Cobertura API v3): PASÓ.")

    # 20. ESCENARIO 20: Subordinación de Cobertura al HardFloorGuardian, Bloqueo por Latencia y Persistencia SHA-256
    h_store.save(force=True)
    h_store_reloaded = HedgeStateStore(env_mode="mainnet", filename="test_hedge_store.json")
    assert h_store_reloaded.hedge_metrics["total_hedges_opened"] == 1
    assert h_store_reloaded.hedge_metrics["successful_unwinds"] == 1
    # Probar bloqueo de cobertura cuando equity está pegado al locked_floor
    auth_floor, reason_floor, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx={"orig_notional_sats": 30000, "orig_leverage": 20, "orig_side": "buy", "cooldown_until": 0.0},
        pair_econ={"has_active_hedge": False, "effective_hedge_ratio": 0.0},
        portfolio_snap={"locked_margin_sats": 1500},
        predictive_signal={"optimal_hedge_ratio": 0.5, "expected_unhedged_loss_sats": 400},
        account_equity_sats=22100, available_balance_sats=15000, locked_floor_sats=22000,
        ind_5m=ind_5m_adv, latency_degraded=False
    )
    assert auth_floor is False and "BLOCKED_HARD_FLOOR_PROXIMITY" in reason_floor
    # Probar bloqueo de cobertura por latencia degradada
    auth_lat, reason_lat, _ = HedgeRiskManager.authorize_hedge_action(
        "PARTIAL_HEDGE",
        pair_ctx={"orig_notional_sats": 30000, "orig_leverage": 20, "orig_side": "buy", "cooldown_until": 0.0},
        pair_econ={"has_active_hedge": False, "effective_hedge_ratio": 0.0},
        portfolio_snap={"locked_margin_sats": 1500},
        predictive_signal={"optimal_hedge_ratio": 0.5, "expected_unhedged_loss_sats": 400},
        account_equity_sats=30000, available_balance_sats=15000, locked_floor_sats=22000,
        ind_5m=ind_5m_adv, latency_degraded=True
    )
    assert auth_lat is False and "BLOCKED_HEDGE_LATENCY_DEGRADED" in reason_lat
    for ext in ("", ".bak", ".sha256", ".lock"):
        if os.path.exists(f"test_hedge_store.json{ext}"):
            os.remove(f"test_hedge_store.json{ext}")
    print("✅ ESCENARIO 20 (Subordinación HardFloorGuardian + Bloqueo Latencia en Hedge + Persistencia SHA-256): PASÓ.")

    # 21. ESCENARIO 21: Integración Etapa E (Reconciliación Primaria/Hedge + Cooperación Guardian/Hedge + Limpieza Huérfana)
    h_store_int = HedgeStateStore(env_mode="mainnet", filename="test_hedge_int.json")
    int_brain = AutonomousGoalBrain(filename="test_brain_int.json")
    prim_t = {
        "id": "prim-int-1", "side": "buy", "margin": 3000, "leverage": 25,
        "price": 78200.0, "stoploss": 77100.0, "liquidation": 75000.0, "pl": -410
    }
    int_client = MockLNMClient(running_list=[prim_t])
    int_brain.active_trades["prim-int-1"] = {
        "action": 1, "style": "TREND", "state": "S_INT", "edge_deg_streak": 3, "last_pnl_atr": -0.90
    }
    account_st = {
        "equity_sats": 32000, "balance_sats": 24000, "locked_margin_sats": 3000,
        "locked_floor_sats": 21000, "hard_floor_breach": False, "current_dd_pct": 0.04
    }
    # Ciclo 1, 2 y 3 integrados: abre cobertura y verifica que execute_strategy_guardian NO fuerza cierre mientras está cubierta
    for step_idx in range(3):
        _, pf_st = reconcile_portfolio_with_hedges(
            int_client, int_brain, h_store_int, int_client.running_list,
            equity_sats=32000, btc_price=77600.0, now=now + step_idx * 10
        )
        still_prim = run_integrated_guardian_and_hedge_cycle(
            int_client, int_brain, h_store_int, pf_st, pf_st["primary_trades"],
            ind_5m_adv, ind_15m_adv, ind_1h_sup, "S_INT", "NORMAL",
            edge_scores_by_tid={"prim-int-1": -0.35 if step_idx < 2 else -0.72},
            perception=perc_adv, q_vec=[0.0, -0.22, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            account_state=account_st, now=now + step_idx * 10, safe_mode=False
        )
    assert len(int_client.open_calls) == 1
    hedge_opened_id = str(int_client.open_calls[0]["id"])
    # La posición primaria NO fue cerrada por FORCED_EDGE_EXIT gracias a estar protegida por cobertura activa (is_hedged=True)
    assert "prim-int-1" not in int_client.close_calls
    # La pata de cobertura NUNCA contamina brain.active_trades
    int_brain.active_trades[hedge_opened_id] = {"action": 5, "style": "TREND"}
    _, pf_st_clean = reconcile_portfolio_with_hedges(
        int_client, int_brain, h_store_int, int_client.running_list,
        equity_sats=32000, btc_price=77600.0, now=now + 40
    )
    assert hedge_opened_id not in int_brain.active_trades
    assert len(pf_st_clean["primary_trades"]) == 1 and hedge_opened_id in pf_st_clean["hedge_trades_map"]

    # Simular que la posición primaria cerró por SL/TP en el servidor dejando huérfana la cobertura
    int_client.running_list = [t for t in int_client.running_list if str(t["id"]) != "prim-int-1"]
    filtered_after_orphan, pf_after_orphan = reconcile_portfolio_with_hedges(
        int_client, int_brain, h_store_int, int_client.running_list,
        equity_sats=32000, btc_price=77600.0, now=now + 50, safe_mode=False
    )
    assert hedge_opened_id in int_client.close_calls
    assert len(filtered_after_orphan) == 0 and len(h_store_int.pairs) == 0

    for fn in ("test_hedge_int.json", "test_brain_int.json"):
        for ext in ("", ".bak", ".sha256", ".lock"):
            if os.path.exists(f"{fn}{ext}"):
                os.remove(f"{fn}{ext}")
    print("✅ ESCENARIO 21 (Integración Etapa E: Ciclo Bot + Hedge Engine + Limpieza Huérfana + Aislamiento Q-Brain): PASÓ.")

    # 22. ESCENARIO 22: Certificación Fase 6 Termux (O(1) Incremental Indicators + Buffers Circulares + tracemalloc 500 Ciclos)
    n_candles = 210
    base_c = np.array([78000.0 + (i * 8.5) + ((i % 7) - 3) * 22.0 for i in range(n_candles)], dtype=np.float64)
    base_h = np.array([float(base_c[i]) + 65.0 + (i % 5) * 9.0 for i in range(n_candles)], dtype=np.float64)
    base_l = np.array([float(base_c[i]) - 62.0 - (i % 4) * 8.0 for i in range(n_candles)], dtype=np.float64)

    prefix_st = _build_indicator_prefix_state(base_h, base_l, base_c)
    for spot_p in (79650.0, 79820.0, 80110.0):
        h_cp, l_cp, c_cp = base_h.copy(), base_l.copy(), base_c.copy()
        c_cp[-1] = spot_p
        if spot_p > h_cp[-1]:
            h_cp[-1] = spot_p
        if spot_p < l_cp[-1]:
            l_cp[-1] = spot_p
        ind_full = compute_indicators(h_cp, l_cp, c_cp, spot_p)
        ind_inc = compute_indicators_from_prefix(prefix_st, spot_p)
        for k_chk in ("rsi", "bb_pos", "bb_width_pct", "bb_expansion_rate", "atr", "atr_rel_ratio", "ema9", "ema21", "ema50", "ema200"):
            assert abs(float(ind_full[k_chk]) - float(ind_inc[k_chk])) < 1e-8, f"Divergencia en {k_chk}: {ind_full[k_chk]} vs {ind_inc[k_chk]}"
        assert ind_full["market_regime"] == ind_inc["market_regime"]

    perf_perc = ShortTermPerceptionEngine()
    perf_tel = LatencyTelemetry(window_size=256)
    perf_store = HedgeStateStore(env_mode="mainnet", filename="test_hedge_perf.json")
    perf_trade = {
        "id": "perf-t1", "side": "buy", "margin": 2500, "leverage": 20,
        "price": 79800.0, "stoploss": 78600.0, "liquidation": 76000.0, "pl": -90
    }
    perf_client = MockLNMClient(running_list=[perf_trade])

    # Warmup de buffers circulares fijos (260 ciclos) antes de medir deriva de memoria en estado estacionario
    for w_cyc in range(260):
        tick_p = 79750.0 + ((w_cyc % 19) - 9) * 14.0
        ind_tick = compute_indicators_from_prefix(prefix_st, tick_p)
        p_out = perf_perc.evaluate("RANGE_NORMAL|MID|LOW_EXP|BULL|FULL_BULL", ind_tick, ind_15m_bull, ind_1h_sup, ind_1h_sup, now=now + w_cyc * 16.0)
        pf_s = PortfolioHedgeCoordinator.reconcile_portfolio(perf_client.running_list, perf_store, 30000, tick_p, now=now + w_cyc * 16.0)
        AdaptiveHedgeController.evaluate_and_manage_pair(
            perf_client, perf_trade, None, {"style": "TREND"}, pf_s, perf_store,
            ind_tick, ind_15m_bull, ind_1h_sup, p_out, q_val_main=0.05,
            account_equity_sats=30000, available_balance_sats=25000, locked_floor_sats=21000,
            safe_mode=True, now=now + w_cyc * 16.0
        )
        perf_tel.record("cycle_ms", 0.25)
        _ = perf_tel.get_percentiles("cycle_ms")

    tracemalloc.start()
    mem_before, _ = tracemalloc.get_traced_memory()
    t_perf0 = time.perf_counter()
    for cyc in range(260, 760):
        tick_p = 79750.0 + ((cyc % 19) - 9) * 14.0
        ind_tick = compute_indicators_from_prefix(prefix_st, tick_p)
        p_out = perf_perc.evaluate("RANGE_NORMAL|MID|LOW_EXP|BULL|FULL_BULL", ind_tick, ind_15m_bull, ind_1h_sup, ind_1h_sup, now=now + cyc * 16.0)
        pf_s = PortfolioHedgeCoordinator.reconcile_portfolio(perf_client.running_list, perf_store, 30000, tick_p, now=now + cyc * 16.0)
        AdaptiveHedgeController.evaluate_and_manage_pair(
            perf_client, perf_trade, None, {"style": "TREND"}, pf_s, perf_store,
            ind_tick, ind_15m_bull, ind_1h_sup, p_out, q_val_main=0.05,
            account_equity_sats=30000, available_balance_sats=25000, locked_floor_sats=21000,
            safe_mode=True, now=now + cyc * 16.0
        )
        perf_tel.record("cycle_ms", 0.25)
        _ = perf_tel.get_percentiles("cycle_ms")
    elapsed_500_ms = (time.perf_counter() - t_perf0) * 1000.0
    avg_cyc_ms = elapsed_500_ms / 500.0
    mem_after, mem_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    mem_delta_kb = (mem_after - mem_before) / 1024.0

    assert len(perf_perc.price_history) == 60
    assert len(perf_tel.samples["cycle_ms"]) == 256
    assert len(perf_store.pairs["perf-t1"]["edge_history"]) <= 12
    assert avg_cyc_ms < 1.5, f"Latencia media por ciclo excesiva: {avg_cyc_ms:.3f}ms"
    assert mem_delta_kb < 16.0, f"Posible crecimiento de memoria no acotado: +{mem_delta_kb:.1f}KB"
    for ext in ("", ".bak", ".tmp", ".lock"):
        if os.path.exists("test_hedge_perf.json" + ext):
            try:
                os.remove("test_hedge_perf.json" + ext)
            except OSError:
                pass
    print(
        f"✅ ESCENARIO 22 (Fase 6 Termux O(1) + 500 Ciclos: Latencia={avg_cyc_ms:.3f}ms/ciclo | "
        f"RAM Delta=+{mem_delta_kb:.1f}KB | Buffers Acotados): PASÓ."
    )

    # =========================================================================
    # ESCENARIO 23: VALIDACIÓN WALK-FORWARD OUT-OF-SAMPLE (FASE 5.7 / ETAPA G)
    # =========================================================================
    from validate_walk_forward_oos import run_walk_forward_validation, VARIANTS_ORDER, REGIMES_ORDER
    wf_report = run_walk_forward_validation(save_report=True, verbose=False)
    v_legacy = wf_report["variants"]["1_SISTEMA_ACTUAL_SIN_MEJORAS"]
    v_opt_no_h = wf_report["variants"]["2_SISTEMA_OPTIMIZADO_SIN_HEDGE"]
    v_static = wf_report["variants"]["3_HEDGE_ESTATICO_TRADICIONAL"]
    v_pred = wf_report["variants"]["4_HEDGE_PREDICTIVO_SIN_ADAPTACION"]
    v_hybrid = wf_report["variants"]["5_HYBRID_EDGE_HEDGING_COMPLETO"]

    assert set(wf_report["variants"].keys()) == set(VARIANTS_ORDER)
    assert set(v_hybrid["by_regime"].keys()) == set(REGIMES_ORDER)
    assert v_hybrid["total_net_sats"] > 0
    assert v_hybrid["total_net_sats"] > v_opt_no_h["total_net_sats"] > v_legacy["total_net_sats"]
    assert v_hybrid["total_net_sats"] > v_pred["total_net_sats"] > v_static["total_net_sats"]
    assert v_hybrid["max_dd_sats"] < v_opt_no_h["max_dd_sats"] < v_legacy["max_dd_sats"]
    assert v_hybrid["profit_factor"] > 1.30
    assert v_hybrid["sharpe"] > 1.0 and v_hybrid["sortino"] > 1.5 and v_hybrid["calmar"] > 2.5
    assert v_hybrid["loss_avoided_vs_sl_sats"] > 2000
    assert v_hybrid["hedge_worsened_cases"] == 0
    print(
        f"✅ ESCENARIO 23 (Walk-Forward OOS 5 Variantes / 6 Regímenes: Hybrid Net={v_hybrid['total_net_sats']:+d} sats | "
        f"MaxDD={v_hybrid['max_dd_pct']:.2f}% | PF={v_hybrid['profit_factor']:.2f} | Evitado={v_hybrid['loss_avoided_vs_sl_sats']} sats): PASÓ."
    )
    print("\n🎯 LOS 23 ESCENARIOS DE CERTIFICACIÓN CUANTITATIVA, FASES 1-3, FASE 5, FASE 6 Y WALK-FORWARD OOS (ETAPA G) PASARON EXITOSAMENTE.")


if __name__ == "__main__":
    run_audit_protocol()
