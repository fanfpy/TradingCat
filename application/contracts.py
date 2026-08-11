"""Agent 无关的 v5 application contracts；所有适配器只调用这里。"""

import json
import uuid
from datetime import datetime
from typing import Dict, Optional

from production.monitor import post_market_check
from research.factors import analyze_factor_snapshot
from shared import db as dbm
from shared.indicators import atr22
from shared.longbridge_client import LongbridgeError
from shared.security import SecurityResolver, UNKNOWN_METADATA


SCHEMA_VERSION = "tradingcat.v1"


def _envelope(operation: str, *, data=None, error=None, warnings=None,
              lineage=None) -> Dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": f"req_{uuid.uuid4().hex[:12]}",
        "operation": operation,
        "ok": error is None,
        "data": data,
        "error": error,
        "warnings": list(warnings or []),
        "lineage": dict(lineage or {}),
    }


DEFAULT_SECURITIES = (
    ("AAPL.US", "Apple Inc.", "NASDAQ", "USD", ["Apple", "苹果", "苹果公司", "AAPL"]),
    ("MSFT.US", "Microsoft Corp.", "NASDAQ", "USD", ["Microsoft", "微软", "MSFT"]),
    ("NVDA.US", "NVIDIA Corp.", "NASDAQ", "USD", ["NVIDIA", "英伟达", "NVDA"]),
    ("TSLA.US", "Tesla Inc.", "NASDAQ", "USD", ["Tesla", "特斯拉", "TSLA"]),
    ("0700.HK", "Tencent Holdings", "HKEX", "HKD", ["Tencent", "腾讯", "腾讯控股"]),
)

DEFAULT_SECURITY_RISK = {
    "AAPL.US": {"sector": "TECHNOLOGY", "beta": 1.20},
    "MSFT.US": {"sector": "TECHNOLOGY", "beta": 1.00},
    "NVDA.US": {"sector": "SEMICONDUCTORS", "beta": 1.65},
    "TSLA.US": {"sector": "CONSUMER_DISCRETIONARY", "beta": 2.00},
    "0700.HK": {"sector": "COMMUNICATION_SERVICES", "beta": 1.05},
}


class TradingCatApplication:
    """可被 CLI、Python、MCP/HTTP 或任意 Agent 调用的唯一用例层。"""

    def __init__(self, core_conn, execution_conn=None, fundamental_provider=None,
                 security_provider=None):
        self.core = core_conn
        self.execution = execution_conn
        self.fundamental_provider = fundamental_provider
        self._seed_security_master()
        self.security_resolver = SecurityResolver(self.core, security_provider)

    def _seed_security_master(self) -> None:
        if self.core.execute("SELECT 1 FROM security_master LIMIT 1").fetchone():
            return
        for symbol, name, exchange, currency, aliases in DEFAULT_SECURITIES:
            dbm.upsert_security(
                self.core, symbol, name, exchange, currency, aliases,
                **DEFAULT_SECURITY_RISK.get(symbol, {}))

    def resolve_security(self, query: str) -> Dict:
        try:
            resolution = self.security_resolver.resolve(query)
        except LongbridgeError as exc:
            return _envelope(
                "ResolveSecurity",
                error={"code": type(exc).__name__, "message": str(exc),
                       "retryable": exc.retryable},
            )
        matches = resolution["matches"]
        if resolution["status"] == UNKNOWN_METADATA:
            return _envelope(
                "ResolveSecurity",
                error={"code": UNKNOWN_METADATA,
                       "message": "无法从 security_master 或 Longbridge 确认标的 metadata"},
                data={"symbol": resolution["symbol"], "candidates": []},
            )
        exact = [m for m in matches if m["confidence"] >= 0.9]
        if len(exact) == 1:
            return _envelope("ResolveSecurity", data=exact[0])
        if len(matches) == 1:
            return _envelope("ResolveSecurity", data=matches[0])
        code = "SECURITY_NOT_FOUND" if not matches else "AMBIGUOUS_SECURITY"
        return _envelope(
            "ResolveSecurity", error={"code": code, "message": "无法唯一确定标的"},
            data={"candidates": matches[:10]},
        )

    def analyze_security(self, query: str, as_of: Optional[str] = None) -> Dict:
        resolved = self.resolve_security(query)
        if not resolved["ok"]:
            return {**resolved, "operation": "AnalyzeSecurity"}
        security = resolved["data"]
        symbol = security["symbol"]
        bars = dbm.get_bars(self.core, symbol)
        lifecycle = dbm.get_lifecycle(self.core, symbol)
        latest = dbm.get_latest_strategy_version(self.core, symbol)
        manifest = dbm.get_manifest(self.core, symbol)
        warnings = []
        factor_report = analyze_factor_snapshot(
            self.core, symbol, as_of or datetime.now().strftime("%Y-%m-%d"))
        technical = factor_report["technical"]
        if technical is None:
            warnings.append("日线不足 121 根，完整技术因子不可用")
        if factor_report["fundamental_status"] == "MISSING":
            warnings.append(
                "本地库缺少 as-of 时点可见的 PIT 基本面快照；历史基本面因子已禁用，"
                "未使用当前值回填历史")
        current_fundamentals = None
        # 当前快照只在分析“今天”时补充，绝不参与 as-of 历史分析或落入 PIT 表。
        if self.fundamental_provider is not None and as_of is None:
            fetched = self.fundamental_provider.current(symbol)
            current_fundamentals = fetched.to_dict()
            warnings.extend(fetched.warnings)
        params = json.loads(latest["params_json"]) if latest and latest["params_json"] else None
        data = {
            "security": {k: security[k] for k in ("symbol", "name", "exchange", "currency")},
            "as_of": as_of or datetime.now().strftime("%Y-%m-%d"),
            "factor_registry": factor_report["registry"],
            "fundamental_factors": factor_report["fundamental"],
            "current_fundamentals": current_fundamentals,
            "technical_factors": technical,
            "strategy_suitability": factor_report["strategy_suitability"],
            "research_status": lifecycle["status"] if lifecycle else "UNRESEARCHED",
            "strategy_candidate": ({
                "entry_mode": params.get("entry_mode"),
                "exit_mode": params.get("exit_mode", "chandelier"),
                "final_holdout": params.get("holdout"),
                "robustness": params.get("robustness"),
            } if params else None),
            "trade_eligible": bool(lifecycle and lifecycle["status"] in ("verified", "live")),
            "data_quality": ({
                "source": manifest["source"], "data_version": manifest["sha256"],
                "adjustment_mode": manifest["adjustment_mode"],
                "corporate_actions_status": manifest["corporate_actions_status"],
            } if manifest else None),
        }
        return _envelope(
            "AnalyzeSecurity", data=data, warnings=warnings,
            lineage={"strategy_version_id": latest["version_id"] if latest else None,
                     "data_version": manifest["sha256"] if manifest else None},
        )

    def follow_security(self, query: str, *, account_id: str = "default",
                        reason: str = "", channels=None) -> Dict:
        resolved = self.resolve_security(query)
        if not resolved["ok"]:
            return {**resolved, "operation": "FollowSecurity"}
        symbol = resolved["data"]["symbol"]
        item = dbm.follow_security(
            self.core, account_id, symbol, reason, channels or ["audit"])
        return _envelope(
            "FollowSecurity", data={**item, "strategy_assignment": None,
                                    "trade_eligible": False},
            warnings=["关注不等于策略绑定或交易资格；策略必须由用户另行确认"],
        )

    def review_portfolio(self, *, account_id: str = "default",
                         as_of: Optional[str] = None, account_state=None) -> Dict:
        from production.decision import _positions
        from production.position import KellyPositionSizer

        date = as_of or datetime.now().strftime("%Y-%m-%d")
        positions = _positions(account_state, self.core)
        policy_row = dbm.get_active_investor_policy(self.core, account_id)
        policy = json.loads(policy_row["config_json"])
        nav = float(getattr(account_state, "nav", 0) or 0)
        if nav <= 0:
            account = dbm.get_account(self.core, account_id)
            nav = float(account["nav"] or 0) if account else 0
        advice = []
        for position in positions:
            symbol = position.get("symbol", "")
            lifecycle = dbm.get_lifecycle(self.core, symbol)
            params = json.loads(lifecycle["params_json"]) if lifecycle and lifecycle["params_json"] else None
            bars = dbm.get_bars(self.core, symbol)
            price = float(position.get("last_price", 0) or (bars[-1]["close"] if bars else 0))
            qty = float(position.get("quantity", 0) or 0)
            weight = qty * price / nav if nav > 0 else None
            action, rationale = "KEEP", "未触发退出信号"
            stop = position.get("stop_price")
            target_weight = None
            target_range = [0.0, float(policy["max_single_position"])]
            risk_flags = []
            if params and bars:
                report = post_market_check(
                    self.core, symbol, params, date, realtime_position=position)
                if report.exit_triggered:
                    action, rationale, target_weight = "EXIT", "; ".join(report.messages), 0.0
                    target_range = [0.0, 0.0]
                else:
                    latest = dbm.get_latest_strategy_version(self.core, symbol)
                    stats = None
                    if latest is not None and latest["oos_stats_json"]:
                        try:
                            stats = json.loads(latest["oos_stats_json"])
                        except (TypeError, ValueError):
                            risk_flags.append("INVALID_OOS_STATS")
                    if stop is None or float(stop or 0) <= 0:
                        highs = [float(row["high"]) for row in bars]
                        lows = [float(row["low"]) for row in bars]
                        closes = [float(row["close"]) for row in bars]
                        stop = price - float(params["atr_multiple"]) * atr22(
                            highs, lows, closes)[-1]
                    if (lifecycle and lifecycle["status"] in ("verified", "live")
                            and stats is not None and nav > 0):
                        sized = KellyPositionSizer(policy).size({
                            "symbol": symbol, "oos_stats": stats,
                            "entry_price": price, "stop_price": float(stop),
                        }, nav, account_state)[0]
                        target_weight = float(sized.target_fraction)
                        lower = max(0.0, target_weight * 0.8)
                        upper = min(float(policy["max_single_position"]),
                                    target_weight * 1.2)
                        target_range = [lower, upper]
                        tolerance = 0.0025
                        if weight is not None and weight < lower - tolerance:
                            action = "ADD"
                            rationale = "当前权重低于冻结候选的收缩 Kelly 目标区间"
                        elif weight is not None and weight > upper + tolerance:
                            action = "REDUCE"
                            rationale = "当前权重高于冻结候选的收缩 Kelly 目标区间"
                        else:
                            rationale = "当前权重位于收缩 Kelly 目标区间"
                        if target_weight <= 0:
                            risk_flags.append("KELLY_EVIDENCE_INSUFFICIENT")
                    elif weight is not None and weight > float(policy["max_single_position"]):
                        action, rationale = "REDUCE", "当前权重超过 InvestorPolicy 单标的上限"
                        target_weight = float(policy["max_single_position"])
                    else:
                        risk_flags.append("STRATEGY_NOT_TRADE_ELIGIBLE")
            else:
                rationale = "策略或行情证据不足；仅保留诊断，不建议加仓"
                risk_flags.append("MISSING_STRATEGY_OR_MARKET_DATA")
            advice.append({
                "symbol": symbol, "action": action, "quantity": qty,
                "current_weight": weight,
                "target_weight": target_weight,
                "target_weight_range": target_range,
                "weight_delta": (target_weight - weight
                                 if target_weight is not None and weight is not None else None),
                "stop_price": stop, "rationale": rationale,
                "risk_flags": risk_flags,
                "strategy_status": lifecycle["status"] if lifecycle else "UNRESEARCHED",
            })
        warnings = []
        if account_state is not None and not getattr(account_state, "synced", False):
            warnings.append("AccountState 非 SYNCED：建议仅供诊断，所有 LIVE 提交将被拒绝")
        return _envelope(
            "ReviewPortfolio", data={"account_id": account_id, "nav": nav,
                                     "position_advice": advice,
                                     "investor_policy": policy},
            warnings=warnings,
            lineage={"investor_policy_version_id": policy_row["policy_version_id"]},
        )

    def propose_trade(self, equity: float, *, account_id: str = "default",
                      mode: str = "DRY_RUN", as_of: Optional[str] = None,
                      account_state=None) -> Dict:
        from production.decision import run_decision, target_to_execution_plan

        target = run_decision(self.core, equity, account_state, as_of)
        plan = target_to_execution_plan(
            self.core, target, equity, account_id, mode, account_state)
        return _envelope(
            "ProposeTrade", data={
                "target_portfolio": {
                    "passed": target.passed, "failures": target.failures,
                    "final_fracs": target.final_fracs, "details": target.details,
                },
                "execution_plan": plan.to_dict() if plan else None,
                "requires_explicit_human_approval": plan is not None,
            },
            warnings=[] if target.passed else ["组合风控未通过；新买入目标已归零"],
            lineage={"plan_id": plan.plan_id if plan else None},
        )

    def request_approval(self, plan_id: str) -> Dict:
        if self.execution is None:
            return _envelope(
                "RequestApproval", error={"code": "EXECUTIOND_UNAVAILABLE",
                                          "message": "未连接独立 executiond"})
        from execution.service import ExecutionService
        service = ExecutionService(self.core, self.execution)
        confirmation = service.request_confirmation(plan_id)
        return _envelope(
            "RequestApproval", data={
                "confirmation": confirmation.to_dict(),
                "approval_status": "PENDING",
                "note": "本接口不能批准；仅真实用户的 ApprovalProof 可产生 APPROVED",
            }, lineage={"plan_id": plan_id,
                        "confirmation_id": confirmation.confirmation_id})

    def explain_decision(self, plan_id: str) -> Dict:
        from production.decision import load_execution_plan

        plan = load_execution_plan(self.core, plan_id)
        if plan is None:
            return _envelope(
                "ExplainDecision", error={"code": "PLAN_NOT_FOUND",
                                          "message": f"计划不存在: {plan_id}"})
        logs = [dict(row) for row in dbm.get_audit(
            self.core, entity_type="plan", entity_id=plan_id, limit=100)]
        return _envelope(
            "ExplainDecision", data={"plan": plan.to_dict(), "audit": logs},
            lineage={"plan_id": plan_id,
                     "strategy_version_ids": sorted({
                         order.strategy_version_id for order in plan.orders
                         if order.strategy_version_id is not None}),
                     "investor_policy_version_ids": sorted({
                         order.investor_policy_version_id for order in plan.orders
                         if order.investor_policy_version_id is not None})},
        )
