#!/usr/bin/env python3
"""
OrderRouter — 订单路由抽象（架构 v4.0 执行层）
================================================
架构 §5：OrderRouter "⚠️ 已有（长桥 CLI）" → 独立抽象补全。

D-9 幂等链：... → OrderIntent(client_request_id) → OrderRouter → Broker(broker_order_id)

职责：
- 将 OrderIntent 提交到券商（Longbridge Openapi）
- 返回 broker_order_id（或 dry-run 模拟 ID）
- 不做风控判断（PreTradeRisk 已完成）
- 不做确认校验（OrderManager 已完成）

当前 LiveBroker 已含提交逻辑，OrderRouter 是协议抽象层——
LiveBroker 实现 OrderRouter Protocol，保持向后兼容。
"""

from dataclasses import dataclass
from typing import Optional, Dict, Protocol, runtime_checkable


@dataclass
class RouteResult:
    """路由结果。"""
    broker_order_id: Optional[str]   # 券商订单 ID（dry-run 时为 None）
    status: str                      # submitted | rejected
    raw_response: Dict               # 券商原始返回
    error: Optional[str] = None


@runtime_checkable
class OrderRouter(Protocol):
    """订单路由接口——将 OrderIntent 提交到券商。"""

    def route(self, intent: dict, execution_mode: str = "DRY_RUN") -> RouteResult:
        """提交一笔 OrderIntent 到券商。

        Args:
            intent: OrderIntent dict（含 client_request_id/symbol/side/quantity/order_type）
            execution_mode: DRY_RUN | LIVE

        Returns:
            RouteResult
        """
        ...


class DryRunRouter:
    """DRY_RUN 路由器——模拟券商提交，不触网。"""

    def route(self, intent: dict, execution_mode: str = "DRY_RUN") -> RouteResult:
        broker_id = f"dry-{intent.get('client_request_id', 'unknown')}"
        return RouteResult(
            broker_order_id=broker_id,
            status="submitted",
            raw_response={"mode": "DRY_RUN", "broker_order_id": broker_id},
        )


class LongbridgeRouter:
    """长桥实盘路由器（委托 longbridge_client）。

    安全约束：
    - LIVE 模式需显式 enable_live=True
    - 内部 lazy-load LongbridgeClient，失败不静默降级
    """

    def __init__(self, enable_live: bool = False):
        self.enable_live = enable_live
        self._client = None

    def _get_client(self):
        if self._client is None:
            from shared.longbridge_client import LongbridgeClient
            self._client = LongbridgeClient(scope="trade")
        return self._client

    def route(self, intent: dict, execution_mode: str = "DRY_RUN") -> RouteResult:
        if execution_mode == "DRY_RUN":
            return DryRunRouter().route(intent, execution_mode)

        if not self.enable_live:
            raise RuntimeError("LIVE 路由需要 enable_live=True（铁律 1：禁止自动实盘）")
        raise RuntimeError("禁止直接 LIVE route；必须使用 OrderManager → LiveBroker 安全链")


# ───────── 冒烟测试 ─────────

if __name__ == "__main__":
    # DryRunRouter
    router = DryRunRouter()
    assert isinstance(router, OrderRouter), "DryRunRouter 应满足 OrderRouter Protocol"
    r = router.route({"client_request_id": "test-1", "symbol": "NVDA.US",
                      "side": "BUY", "quantity": 10, "order_type": "MARKET"})
    assert r.status == "submitted"
    assert r.broker_order_id == "dry-test-1"
    print(f"DryRun route: {r.broker_order_id} status={r.status}")

    # LongbridgeRouter DRY_RUN（不走真实券商）
    lr = LongbridgeRouter(enable_live=False)
    r2 = lr.route({"client_request_id": "test-2", "symbol": "AAPL.US",
                   "side": "BUY", "quantity": 5}, execution_mode="DRY_RUN")
    assert r2.status == "submitted"
    assert r2.broker_order_id == "dry-test-2"

    # LIVE 未启用 → 应拒绝
    try:
        lr.route({"client_request_id": "test-3", "symbol": "AAPL.US",
                  "side": "BUY", "quantity": 5}, execution_mode="LIVE")
        raise AssertionError("应拒绝 LIVE 路由")
    except RuntimeError as e:
        assert "enable_live" in str(e)

    print("order_router.py 冒烟测试通过 ✅")
