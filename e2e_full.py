#!/usr/bin/env python3
"""
TradingCat 全链路 DRY_RUN 演练
==============================
模拟完整交易策略流程，覆盖新架构全部组件（全程 DRY_RUN，绝不触达券商）。

用法：PYTHONPATH=. python3 e2e_full.py
退出码 0 = 全链路通过。
"""

import sys
import math
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared import db as dbm
from shared.account import AccountState, sync_positions
from shared.backtest import PARAM_GRID
from shared.feature_lab import compute_features
from shared.alpha_model import RuleBasedAlpha
from shared.indicators import sma, ma_slope, atr22
from research.walk_forward import run_walk_forward
from research.score import decide_lifecycle
from research.pipeline import add_candidate, prefilter, cache_bars, research_symbol
from production.monitor import pre_market_check, reset_alert_log
from production.decision import run_decision, target_to_execution_plan
from execution.models import ExecutionPlan, PlanOrder, MarketState, now_utc
from execution.order_manager import ConfirmationService, ApprovalAdapter, OrderManager
from execution.pretrade_risk import evaluate as pretrade_evaluate
from execution.broker import BrokerEventHandler, Reconciliation
from execution.order_router import DryRunRouter, LongbridgeRouter


# 小参数网格（e2e 演练用，加速回测；不超过 20 组才被标记为 small）
PARAM_GRID_SMALL = [
    {"entry_mode": "momentum", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.0, "exit_mode": "chandelier"},
    {"entry_mode": "momentum", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.0, "exit_mode": "ma_cross"},
    {"entry_mode": "breakout", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.0, "exit_mode": "chandelier"},
    {"entry_mode": "momentum", "ma_period": 100, "atr_multiple": 3.0, "buffer": 0.0, "exit_mode": "chandelier"},
    {"entry_mode": "momentum", "ma_period": 50, "atr_multiple": 2.5, "buffer": 0.0, "exit_mode": "chandelier"},
]


def gen_bars(n, trend, base_price, vol=15_000_000):
    bars = []
    for i in range(n):
        if trend == "up":
            # 上行但带足够真实回撤，使四个 OOS 窗口能产生可统计的进出场；
            # Decision E2E 必须使用真实 WF 成交，禁止再伪造 Kelly fallback。
            close = base_price + i * 0.3 + 10.0 * math.sin(i / 3)
        elif trend == "down":
            close = base_price - i * 0.3 + 2.0 * math.sin(i / 8)
        else:
            close = base_price + 5.0 * math.sin(i / 6)
        open_p = close - 0.3
        high = max(open_p, close) + abs(0.5 * math.sin(i / 3)) + 0.5
        low = min(open_p, close) - abs(0.5 * math.cos(i / 3)) - 0.5
        if high < max(open_p, close):
            high = max(open_p, close) + 0.1
        if low > min(open_p, close):
            low = min(open_p, close) - 0.1
        if low <= 0:
            low = 0.1
        year = 2022 + i // 252
        doy = (i % 252) + 1
        month = (doy - 1) // 21 + 1
        day = ((doy - 1) % 21) + 1
        ts = f"{year:04d}-{month:02d}-{day:02d}"
        bars.append({"ts": ts, "open": round(open_p, 4), "high": round(high, 4),
                     "low": round(low, 4), "close": round(close, 4),
                     "volume": int(vol + vol * 0.3 * math.sin(i / 5))})
    return bars


def sha256_rows(rows):
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main():
    print("=" * 70)
    print("TradingCat — 全链路 DRY_RUN 演练")
    print("=" * 70)
    print(f"时间: {datetime.now(timezone.utc).isoformat()}")
    print(f"模式: DRY_RUN（全程不触达券商）")

    conn = dbm.get_conn(":memory:")
    reconcile_core = dbm.get_core_conn(":memory:")

    # ── Phase 1: DataHub ──
    print("\n--- Phase 1: DataHub ---")
    symbols = {
        "ALPHA.US": {"trend": "up", "base": 80.0},
        "BETA.US": {"trend": "up", "base": 150.0},
        "GAMMA.US": {"trend": "flat", "base": 50.0},
    }
    for sym, cfg in symbols.items():
        dbm.upsert_security(
            conn, sym, sym, "NASDAQ", "USD", asset_type="EQUITY", lot_size=1)
        # 504 根开发区 + 至少 126 根一次性 Holdout；留足余量给 nested WF。
        bars = gen_bars(800, cfg["trend"], cfg["base"])
        cache_bars(conn, sym, bars, source="simulation",
                   sha256=sha256_rows(bars), last_completed=bars[-1]["ts"])
        add_candidate(conn, sym)
        print(f"  {sym}: {len(bars)} bars  {bars[0]['close']} -> {bars[-1]['close']}")
    dbm.upsert_account(conn, "default", "SYNCED", cash=200_000.0, buying_power=150_000.0)
    print("  账户: SYNCED  cash=200k  bp=150k")
    print("  Phase 1 OK")

    # ── Phase 2: Research ──
    print("\n--- Phase 2: Research (WF + Score + Lifecycle) ---")
    results = {}
    for sym in symbols:
        bars = dbm.get_bars(conn, sym)
        pf = prefilter(conn, sym, bars)
        if pf["metrics"]["bar_count"] < 504:
            print(f"  {sym}: prefilter fail")
            continue
        r = research_symbol(conn, sym, grid=PARAM_GRID_SMALL)
        results[sym] = r
        print(f"  {sym}: status={r['status']:10s}  score={r['score']:5.1f}  subscribed={r.get('subscribed', False)}")
    verified = [s for s, r in results.items() if r["status"] == "verified"]
    print(f"  verified: {verified}")
    assert len(verified) >= 1, "至少 1 个标的应通过验证"
    print("  Phase 2 OK")

    # ── Phase 3: FeatureLab + AlphaModel ──
    print("\n--- Phase 3: FeatureLab + AlphaModel ---")
    sym = verified[0]
    bars = dbm.get_bars(conn, sym)
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    volumes = [float(b["volume"]) for b in bars]
    features = compute_features(closes, highs, lows, volumes)
    f = features[-1]
    print(f"  {sym} 最新特征: mom20={f['momentum_20']:.4f}  adx={f['adx']:.1f}  "
          f"rsi={f['rsi_14']:.1f}  atr={f['atr']:.4f}")
    mas = sma(closes, 50)
    slopes = ma_slope(mas, 5)
    alpha = RuleBasedAlpha(entry_mode="momentum", buffer=0.0, ma_period=50)
    signals = alpha.generate(sym, closes, mas, slopes)
    print(f"  AlphaModel ({alpha.entry_mode}): {len(signals)} 笔信号")
    assert len(signals) > 0, "上涨趋势应有信号"
    print("  Phase 3 OK")

    # ── Phase 4: Monitor ──
    print("\n--- Phase 4: SignalEngine (Monitor 盘前) ---")
    sym = verified[0]
    bars = dbm.get_bars(conn, sym)
    row = dbm.get_lifecycle(conn, sym)
    params = json.loads(row["params_json"]) if row and row["params_json"] else {
        "entry_mode": "momentum", "ma_period": 50, "atr_multiple": 3.0,
        "buffer": 0.0, "exit_mode": "chandelier"}
    print(f"  {sym} 参数: entry={params['entry_mode']}  MA={params['ma_period']}")
    reset_alert_log(bars[-1]["ts"])
    report = pre_market_check(conn, sym, params, bars[-1]["ts"])
    print(f"  盘前: entry_zone={'有' if report.entry_zone else '无'}  "
          f"stop={report.current_stop}  alerts={len(report.alerts)}")
    print("  Phase 4 OK")

    # ── Phase 5: Decision orchestration ──
    print("\n--- Phase 5: Decision (Signal → PositionSizer) ---")
    account = AccountState(account_id="default", sync_status="SYNCED",
                           cash=200_000.0, buying_power=150_000.0, nav=200_000.0)
    as_of_date = dbm.get_bars(conn, verified[0])[-1]["ts"]
    tp = run_decision(conn, 200_000.0, account_state=account, as_of_date=as_of_date)
    assert len(tp.intents) >= 1
    for intent in tp.intents:
        print(f"  {intent.symbol}: side={intent.side} frac={intent.target_fraction:.4f} "
              f"qty={intent.target_quantity}")
    print(f"  合计 {len(tp.intents)} 个 PositionIntent")
    print("  Phase 5 OK")

    # ── Phase 6: TargetPortfolio ──
    print("\n--- Phase 6: TargetPortfolio (组合审查) ---")
    print(f"  passed={tp.passed}  failures={tp.failures}")
    print(f"  final_fracs={tp.final_fracs}")
    print("  Phase 6 OK")

    # ── Phase 7: Execution Pipeline ──
    print("\n--- Phase 7: ExecutionPlan -> Confirmation -> PreTradeRisk -> OrderManager ---")
    plan = target_to_execution_plan(conn, tp, 200_000.0, account_state=account)
    assert plan is not None, "Decision 输出应产生至少一笔差额订单"
    orders = plan.orders
    print(f"  [1] Plan: {plan.plan_id}  hash={plan.plan_hash[:12]}  orders={len(orders)}")
    for o in orders:
        print(f"      {o.symbol} {o.side} {o.quantity} @ {o.reference_price}")
    svc = ConfirmationService(conn)
    cfm = svc.create(plan)
    print(f"  [2] Confirmation: {cfm.confirmation_id}  status={cfm.status}")
    approved = ApprovalAdapter(conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce="e2e_full_001")
    print(f"  [3] APPROVED  by={approved.approved_by}")
    market_states = {}
    for o in orders:
        market_states[o.symbol] = MarketState(symbol=o.symbol, quote_at=now_utc(),
                                               price=o.reference_price, max_age_seconds=300)
    risk = pretrade_evaluate(plan, approved, account, market_states)
    print(f"  [4] PreTradeRisk: {'PASS' if risk.passed else 'REJECT'}")
    assert risk.passed, f"PreTradeRisk 应 PASS: {risk.reasons}"
    om = OrderManager(conn)
    created = om.consume(plan, approved)
    cfm_s = dbm.get_confirmation(conn, approved.confirmation_id)["status"]
    print(f"  [5] OrderManager: {len(created)} intents  confirmation={cfm_s}")
    print("  Phase 7 OK")

    # ── Phase 8: OrderRouter ──
    print("\n--- Phase 8: OrderRouter (DRY_RUN) ---")
    router = DryRunRouter()
    for intent_row in created:
        result = router.route({
            "client_request_id": intent_row["client_request_id"],
            "symbol": intent_row["symbol"], "side": intent_row["side"],
            "quantity": intent_row["quantity"], "order_type": "MARKET"},
            execution_mode="DRY_RUN")
        print(f"  {intent_row['symbol']}: {result.broker_order_id}  {result.status}")
        assert result.status == "submitted"
    lr = LongbridgeRouter(enable_live=False)
    try:
        lr.route({"client_request_id": "x", "symbol": "X", "side": "BUY",
                  "quantity": 1}, execution_mode="LIVE")
        assert False, "应拒绝 LIVE"
    except RuntimeError as e:
        print(f"  LIVE 拒绝: {str(e)[:50]}")
    print("  Phase 8 OK")

    # ── Phase 9: BrokerEventHandler ──
    print("\n--- Phase 9: BrokerEventHandler (partial fill) ---")
    eh = BrokerEventHandler(conn)
    for intent_row in created:
        intent_id = dbm.get_intent_by_request_id(conn, intent_row["client_request_id"])["intent_id"]
        qty = intent_row["quantity"]
        sym = intent_row["symbol"]
        eh.handle({"type": "submitted", "intent_id": intent_id, "broker_order_id": f"bo_{intent_id}"})
        fill1 = int(qty * 0.6)
        eh.handle({"type": "filled", "intent_id": intent_id, "broker_order_id": f"bo_{intent_id}",
                   "symbol": sym, "side": "BUY", "quantity": fill1, "price": 100.0})
        status = dbm.get_intent(conn, intent_id)["status"]
        print(f"  {sym}: partial {fill1}/{qty}  status={status}")
        assert status == "SUBMITTED"
        fill2 = qty - fill1
        eh.handle({"type": "filled", "intent_id": intent_id, "broker_order_id": f"bo_{intent_id}",
                   "symbol": sym, "side": "BUY", "quantity": fill2, "price": 101.0})
        status = dbm.get_intent(conn, intent_id)["status"]
        print(f"  {sym}: full {qty}/{qty}  status={status}")
        assert status == "FILLED"
    rec = Reconciliation(reconcile_core, conn, None)
    r = rec.reconcile_plan(plan.plan_id)
    print(f"  Reconciliation: ok={r['ok']}")
    assert r["ok"]
    print("  Phase 9 OK")

    # ── Phase 10: PositionSync ──
    print("\n--- Phase 10: PositionSync (持仓对账) ---")
    class FakeBroker:
        def positions(self):
            return []
    ps = sync_positions(conn, client=FakeBroker())
    print(f"  synced={ps['synced']}  details={ps['details']}")
    assert ps["synced"]
    print("  Phase 10 OK")

    # ── Phase 11: StrategyVersion ──
    print("\n--- Phase 11: StrategyVersion 版本固化 ---")
    for sym in verified:
        row = dbm.get_lifecycle(conn, sym)
        params_json = row["params_json"] if row else "{}"
        vid = dbm.save_strategy_version(
            conn, sym, status="verified", params_json=params_json,
            wf_report_json=json.dumps(results.get(sym, {}), ensure_ascii=False)[:500],
            git_commit="abc123def456", code_hash=hashlib.md5(b"v4.0").hexdigest(),
            data_version="2026-08-08")
        versions = dbm.list_strategy_versions(conn, sym)
        print(f"  {sym}: version_id={vid}  versions={len(versions)}")
    print("  Phase 11 OK")

    # ── Phase 12: Audit Trail ──
    print("\n--- Phase 12: Audit Trail 完整血缘 ---")
    # 计划级血缘：execution_plan + plan 两种 entity_type
    logs = dbm.get_audit(conn, entity_type="execution_plan", entity_id=plan.plan_id)
    logs += dbm.get_audit(conn, entity_type="plan", entity_id=plan.plan_id)
    # Confirmation 级血缘
    logs += dbm.get_audit(conn, entity_type="confirmation", entity_id=approved.confirmation_id)
    events = [l["event"] for l in logs]
    print(f"  plan+confirmation lineage ({len(events)} events):")
    for e in events:
        print(f"    {e}")
    all_logs = conn.execute(
        "SELECT event, COUNT(*) as cnt FROM audit_log GROUP BY event ORDER BY cnt DESC").fetchall()
    print(f"  全库审计类型:")
    for r in all_logs:
        print(f"    {r['event']:40s}  {r['cnt']}")
    required = ["PLAN_CREATED", "CONFIRMATION", "CONFIRMATION_APPROVED",
                "ORDER_INTENT", "FILL", "RECONCILE", "POSITION_SYNC"]
    found = set(events) | {r["event"] for r in all_logs}
    for req in required:
        assert req in found, f"审计链缺少 {req}"
    print(f"  关键事件链验证: {required}")
    print("  Phase 12 OK")

    # ── 总结 ──
    print("\n" + "=" * 70)
    print("全链路模拟实盘演练通过")
    print("=" * 70)
    print("""
覆盖组件（新架构全部）:
  Phase 1  DataHub（模拟数据缓存 + manifest）
  Phase 2  Research（预筛 -> 回测 -> WF -> 评分 -> 生命周期）
  Phase 3  FeatureLab + AlphaModel（因子计算 -> alpha 信号）
  Phase 4  SignalEngine/Monitor（盘前检查）
  Phase 5  PositionSizer（收缩 Kelly -> PositionIntent）
  Phase 6  TargetPortfolio（PositionIntent -> 组合审查）
  Phase 7  ExecutionPlan + ConfirmationGate + PreTradeRisk + OrderManager
  Phase 8  OrderRouter（DRY_RUN + LIVE 铁律拒绝）
  Phase 9  BrokerEventHandler（partial fill + Reconciliation）
  Phase 10 PositionSync（持仓级对账）
  Phase 11 StrategyVersion 版本固化
  Phase 12 Audit Trail 完整血缘
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
