#!/usr/bin/env python3
"""AAPL 真实只读数据 + 隔离 DRY_RUN 个人投资闭环验收。

只有行情、日线和交易日历会访问 Longbridge quote context。
脚本不创建 TradeContext，不读取真实账户，不调用任何券商下单方法。若真实研究没有
通过验证，不会伪造买入信号；审批/路由的机械安全链使用明确标注的 1 股验收计划。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.contracts import TradingCatApplication
from execution.models import ExecutionPlan, MarketState, PlanOrder, now_utc
from execution.order_manager import ApprovalAdapter, OrderManager
from execution.order_router import DryRunRouter, LongbridgeRouter
from execution.pretrade_risk import evaluate as pretrade_evaluate
from execution.service import ExecutionService
from production.monitor import pre_market_check
from research.pipeline import (
    _normalize_rows, _rows_sha256, add_candidate, cache_bars, prefilter,
    research_symbol,
)
from shared import db as dbm
from shared.account import AccountState
from shared.data_providers import FundamentalProviderChain
from shared.datahub import LongbridgeDataHub
from shared.longbridge_client import LongbridgeClient


SMALL_GRID = [
    {"entry_mode": "momentum", "ma_period": 50, "atr_multiple": 3.0,
     "buffer": 0.0, "exit_mode": "chandelier"},
    {"entry_mode": "momentum", "ma_period": 50, "atr_multiple": 3.0,
     "buffer": 0.0, "exit_mode": "ma_cross"},
    {"entry_mode": "breakout", "ma_period": 50, "atr_multiple": 3.0,
     "buffer": 0.0, "exit_mode": "chandelier"},
    {"entry_mode": "momentum", "ma_period": 100, "atr_multiple": 3.0,
     "buffer": 0.0, "exit_mode": "chandelier"},
    {"entry_mode": "momentum", "ma_period": 50, "atr_multiple": 2.5,
     "buffer": 0.0, "exit_mode": "chandelier"},
]


def _canonical_hash(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def run(symbol: str = "AAPL.US", count: int = 1000) -> dict:
    with tempfile.TemporaryDirectory(prefix="tradingcat-aapl-") as temp_dir:
        core = dbm.get_conn(str(Path(temp_dir) / "core.db"))
        execution = dbm.get_conn(str(Path(temp_dir) / "execution.db"))

        # 只初始化 quote context；不创建 trade context。
        client = LongbridgeClient(scope="quote")
        quote_rows = client.quotes([symbol])
        if not quote_rows:
            raise RuntimeError(f"{symbol} 实时报价为空")
        quote = quote_rows[0]
        price = float(quote.get("current_price") or quote.get("last") or 0)
        if price <= 0:
            raise RuntimeError(f"{symbol} 报价无有效价格")

        bars = _normalize_rows(client.kline_by_count(symbol, count=count, period="day"))
        if len(bars) < 630:
            raise RuntimeError(f"{symbol} 真实日线不足 630 根: {len(bars)}")
        cache_bars(core, symbol, bars, "longbridge", _rows_sha256(bars), bars[-1]["ts"])

        # 真实来源新鲜度必须有官方交易日历，禁止落入自然日测试回退。
        calendar_start = date.fromisoformat(bars[-1]["ts"]) - timedelta(days=10)
        calendar_end = datetime.now(timezone.utc).date()
        LongbridgeDataHub(core, client=client, daily_quota=100).sync_calendar(
            "US", calendar_start, calendar_end)

        add_candidate(core, symbol)
        prefilter_result = prefilter(core, symbol, bars)
        if not prefilter_result["passed"]:
            raise RuntimeError(f"真实数据预筛失败: {prefilter_result}")
        research = research_symbol(core, symbol, grid=SMALL_GRID)

        provider = FundamentalProviderChain([])
        app = TradingCatApplication(core, execution, fundamental_provider=provider)
        analysis = app.analyze_security(symbol)
        followed = app.follow_security(
            symbol, reason="AAPL 真实数据闭环验收", channels=["audit", "daily"])

        lifecycle = dbm.get_lifecycle(core, symbol)
        params = json.loads(lifecycle["params_json"] or "{}") if lifecycle else {}
        monitor = None
        if params:
            report = pre_market_check(core, symbol, params, bars[-1]["ts"])
            monitor = {
                "entry_zone": report.entry_zone,
                "current_stop": report.current_stop,
                "position_open": report.position_open,
                "protective_missing": report.protective_missing,
                "alerts": [str(item) for item in report.alerts],
            }

        simulated_account = AccountState(
            account_id="acceptance", sync_status="SYNCED", cash=100_000,
            buying_power=100_000, nav=100_000, positions=[])
        dbm.upsert_account(core, "acceptance", "SYNCED", 100_000, 100_000,
                           nav=100_000)
        portfolio = app.review_portfolio(
            account_id="acceptance", account_state=simulated_account)
        proposal = app.propose_trade(
            100_000, account_id="acceptance", mode="DRY_RUN",
            as_of=bars[-1]["ts"], account_state=simulated_account)

        # 真实研究若未通过，proposal 必须为空；绝不为了演示而把 AAPL 改为 verified。
        eligible = lifecycle is not None and lifecycle["status"] in ("verified", "live")
        proposed_plan = proposal["data"]["execution_plan"]
        if not eligible and proposed_plan is not None:
            raise AssertionError("非 verified 策略产生了交易计划")

        # 独立验证人工审批后的 DRY_RUN 安全链。该计划是机械验收夹具，不是投资建议。
        test_plan = ExecutionPlan(
            plan_id="plan_aapl_acceptance_dry_run", account_id="acceptance",
            execution_mode="DRY_RUN", expires_at="2099-12-31T23:59:59Z",
            orders=(PlanOrder(
                "1", symbol, "BUY", 1, reference_price=price,
                reference_quote_at=now_utc(), max_slippage_bps=50),),
        )
        dbm.insert_plan(
            core, test_plan.plan_id, test_plan.account_id,
            test_plan.execution_mode, test_plan.expires_at, test_plan.plan_hash,
            [order.to_dict() for order in test_plan.orders])
        service = ExecutionService(core, execution)
        pending = service.request_confirmation(
            test_plan.plan_id, confirmation_id="cfm_aapl_acceptance")
        if pending.status != "PENDING":
            raise AssertionError("审批请求未停留在 PENDING")
        # 此处是验收环境模拟的明确人类动作；生产 Agent 没有该能力。
        approved = ApprovalAdapter(execution, channel="acceptance-fixture").approve(
            pending.confirmation_id, "acceptance-human-fixture",
            nonce="aapl-personal-loop-v1")
        snapshot = service.get_snapshot(test_plan.plan_id)
        market = {symbol: MarketState(symbol, now_utc(), price, max_age_seconds=300)}
        risk = pretrade_evaluate(snapshot, approved, simulated_account, market)
        if not risk.passed:
            raise AssertionError(f"DRY_RUN pre-trade risk failed: {risk.reasons}")
        intents = OrderManager(execution).consume(snapshot, approved)
        routed = [DryRunRouter().route(item, "DRY_RUN") for item in intents]
        if not routed or any(item.status != "submitted" for item in routed):
            raise AssertionError("DRY_RUN 路由未通过")
        try:
            LongbridgeRouter(enable_live=False).route(intents[0], "LIVE")
            raise AssertionError("LIVE 路由未被拒绝")
        except RuntimeError as exc:
            live_guard = str(exc)

        current_snapshots = (
            analysis["data"].get("current_fundamentals") or {}).get("snapshots", [])
        if any(item["pit_safe"] for item in current_snapshots):
            raise AssertionError("当前基本面被错误标记为 PIT safe")

        result = {
            "status": "PASS",
            "mode": "READ_ONLY_DATA_PLUS_ISOLATED_DRY_RUN",
            "symbol": symbol,
            "market_data": {
                "bars": len(bars), "start": bars[0]["ts"], "end": bars[-1]["ts"],
                "quote_price": price, "calendar_source": "longbridge",
            },
            "analysis": {
                "ok": analysis["ok"],
                "technical_available": analysis["data"]["technical_factors"] is not None,
                "current_fundamental_sources": [item["source"] for item in current_snapshots],
                "current_fundamental_status": (
                    "AVAILABLE" if current_snapshots else "MISSING_SAFE_DEGRADE"),
                "historical_pit_status": (
                    "AVAILABLE" if analysis["data"]["fundamental_factors"] else "MISSING_SAFE_DEGRADE"),
            },
            "research": {
                "status": research.get("status"), "score": research.get("score"),
                "eligible": bool(eligible), "reasons": research.get("reasons", []),
                "holdout": research.get("holdout"),
            },
            "watch_and_monitor": {
                "followed": followed["ok"], "monitor": monitor,
            },
            "portfolio": {
                "review_ok": portfolio["ok"],
                "real_evidence_plan_created": proposed_plan is not None,
                "guarded_no_plan_when_ineligible": not eligible and proposed_plan is None,
            },
            "approval_and_dry_run_fixture": {
                "not_investment_recommendation": True,
                "pending": pending.status, "approved": approved.status,
                "risk": risk.decision,
                "intent_count": len(intents),
                "routes": [{"status": item.status,
                            "broker_order_id": item.broker_order_id} for item in routed],
                "live_guard": live_guard,
            },
            "safety": {
                "longbridge_client_scope": "quote",
                "trade_context_created": False,
                "live_order_calls": 0,
                "real_account_read": False,
                "temporary_database_removed_on_exit": True,
            },
        }
        result["evidence_sha256"] = _canonical_hash(result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="AAPL.US")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    result = run(args.symbol, args.count)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
