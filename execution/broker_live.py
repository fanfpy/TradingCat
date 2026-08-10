#!/usr/bin/env python3
"""
LiveBroker — 真实券商适配器（US-007）
======================================
基于 longbridge Python SDK 的真实下单适配器，与 execution/ 安全链衔接：

    OrderManager.submit（LIVE）→ LiveBroker.submit_order(intent, confirmation, plan)
        → LiveBroker.submit：确认校验 → _submit_live（唯一真实下单路径）

安全边界（用户拍板 C：要接 LIVE，但铁律 = 实现只到适配器 + mock 演练 + checklist，
不实际提交真实订单）：

1. **默认 DRY_RUN**：LiveBroker(enable_live=False) 永远不下真实单——
   submit() 只打印 + 落审计（BROKER_DRY_RUN），返回 BrokerAck(status="DRY_RUN_SUBMITTED")。
2. **LIVE 必须显式 enable_live=True** 且传入可用的券商 client（否则构造/提交时报错）。
3. **无绕过 Confirmation 的直通路径**：submit() 必须携带 Confirmation + ExecutionPlan；
   对象层断言 confirmation.status == "APPROVED" 且 plan_hash 强绑定；
   LIVE 路径额外断言 DB 中 confirmation 已 CONSUMED（证明已走 OrderManager.consume
   原子消费链——APPROVED 未消费 = 绕过链直通 = 拒绝）。
   `_submit_live` 是**私有**方法（下划线），全仓唯一调用点 = 本类 submit()。
   代码内 grep 可证：不存在 OrderRouter 等绕过 Confirmation 的直接下单路径。
4. **事件兼容接口**：on_submitted / on_rejected / on_partial_fill / on_filled /
   on_cancelled / on_changed —— 默认落审计（BROKER_EVENT），可注入 event_handler
   （现有 execution/broker.py BrokerEventHandler）驱动本地 intent 状态机。
   注意：submit ack 与异步事件分离（架构 v4.0）：submit 返回 ack 不直接改状态，
   状态流转由事件显式触发（真实场景下由长桥回调/轮询驱动）。

DRY_RUN / LIVE 演练：
    pytest execution/tests/test_broker_live.py   # mock 券商全流程演练
    PYTHONPATH=. python3 execution/e2e_dry_run.py  # 端到端 dry-run（不触网）

接实盘前必读：docs/live-trading-checklist.md（铁律：TradingCat 不会自动接 LIVE）。
"""

import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# 让 shared 可导入：trading-system/shared（db / longbridge_client 等，自包含目录，架构 §6.1）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db as dbm


# ────────────────────────────────────────────────────────────────
# 结果类型 / 异常
# ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BrokerAck:
    """券商 submit 的 ack。broker_order_id 是券商侧订单号。"""
    broker_order_id: str
    status: str                     # SUBMITTED | REJECTED | DRY_RUN_SUBMITTED
    is_live: bool = False
    raw: Optional[Dict] = None


class LiveBrokerSafetyError(ValueError):
    """安全边界违规：无 Confirmation / 未批准 / 未消费 / 直通提交。"""


class LiveBrokerError(RuntimeError):
    """券商提交失败（fail closed：不产生 ack，由上层决定如何处理）。"""


# ────────────────────────────────────────────────────────────────
# LiveBroker
# ────────────────────────────────────────────────────────────────

class LiveBroker:
    """真实券商适配器（长桥 Python SDK）。

    构造：
        LiveBroker(conn, client=None, enable_live=False, account_id="default",
                   event_handler=None)
        - client: 券商客户端（默认 None）。测试注入 mock；None 时在首次真实提交
          _submit_live 内懒加载 LongbridgeClient（shared/longbridge_client.py）。
        - enable_live: **默认 False（DRY_RUN）**。只有显式 True 才允许真实下单。
        - event_handler: 可选，现有 BrokerEventHandler（execution/broker.py），
          用于把券商事件驱动到本地 intent 状态机。

    安全不变量（构造时已固定，提交时再断言）：
        - enable_live=False → 任何 submit() 都不触达券商
        - enable_live=True  → submit() 必须携带 APPROVED（且 LIVE 时已 CONSUMED）
          Confirmation + ExecutionPlan，否则 LiveBrokerSafetyError
    """

    def __init__(self, conn, client=None, enable_live: bool = False,
                 account_id: str = "default", event_handler=None):
        self.conn = conn
        self.client = client                    # 测试注入 mock；None → 懒加载真实客户端
        self.enable_live = bool(enable_live)    # 默认 False = DRY_RUN（铁律）
        self.account_id = account_id
        if event_handler is None and self.enable_live:
            from execution.broker import BrokerEventHandler
            event_handler = BrokerEventHandler(conn)
        self.event_handler = event_handler
        self._client_loaded = client is not None
        self._event_queue = deque()
        self._stream_attached = False

    # ── 主入口（OrderManager 兼容 + 演练入口） ─────────────────

    def submit_order(self, intent: Dict, confirmation=None, plan=None) -> BrokerAck:
        """OrderManager 兼容接口：委托 submit()。

        OrderManager.submit（LIVE 分支）调用本方法时传入
        (intent, confirmation=approved, plan=plan) —— 确认与计划在链内传递，
        保证 broker 侧能再次校验（US-007 安全边界）。
        """
        return self.submit(intent, confirmation=confirmation, plan=plan)

    def submit(self, intent: Dict, confirmation=None, plan=None) -> BrokerAck:
        """提交 OrderIntent → BrokerAck。

        安全校验（无论 DRY_RUN/LIVE 一律执行，杜绝直通）：
        1. confirmation 与 plan 必须提供（缺失 → LiveBrokerSafetyError）
        2. confirmation.status == "APPROVED"（只有已批准确认可提交）
        3. confirmation.plan_hash == plan.plan_hash（D-3 强绑定）
        4. DB 行 confirmation 必须存在；LIVE 时状态必须为 CONSUMED
           （证明经 OrderManager.consume 原子消费链；APPROVED 未消费 = 直通 = 拒绝）

        分支：
        - enable_live=False → _dry_run_submit（不触网，审计 BROKER_DRY_RUN）
        - enable_live=True  → _submit_live（唯一真实下单路径，审计 BROKER_LIVE_SUBMIT）
        """
        # 1. 无确认无计划 → 拒绝（任何模式）
        if confirmation is None or plan is None:
            raise LiveBrokerSafetyError(
                "submit 必须携带 Confirmation + ExecutionPlan（禁止绕过确认的直通提交）")
        # 2. 已批准
        if confirmation.status != "APPROVED":
            raise LiveBrokerSafetyError(
                f"confirmation 状态 {confirmation.status} != APPROVED：只有已批准确认可提交")
        # 3. plan_hash 强绑定（D-3）
        if confirmation.plan_hash != plan.plan_hash:
            raise LiveBrokerSafetyError(
                "confirmation.plan_hash 与 plan.plan_hash 不匹配（计划已被修改）")
        # 4. DB 行存在 + 链完整性
        row = dbm.get_confirmation(self.conn, confirmation.confirmation_id)
        if row is None:
            raise LiveBrokerSafetyError(
                f"confirmation {confirmation.confirmation_id} 在 DB 中不存在")
        if plan.execution_mode != "LIVE":
            raise LiveBrokerSafetyError(
                f"plan execution_mode={plan.execution_mode}：LiveBroker 只接受 LIVE 计划")
        if self.enable_live:
            if row["status"] != "CONSUMED":
                raise LiveBrokerSafetyError(
                    f"confirmation 状态 {row['status']} != CONSUMED："
                    "LIVE 提交必须经 OrderManager.consume 原子消费链"
                    "（APPROVED 未消费 = 绕过链直通 = 拒绝）")
            dbm.authorize_live_canary(
                self.conn, account_id=plan.account_id, plan_id=plan.plan_id,
                client_request_id=str(intent.get("client_request_id", "")),
                symbol=str(intent.get("symbol", "")), side=str(intent.get("side", "")),
                quantity=float(intent.get("quantity", 0) or 0),
                reference_price=intent.get("reference_price"),
            )

        if not self.enable_live:
            return self._dry_run_submit(intent, confirmation, plan)
        return self._submit_live(intent, confirmation, plan)

    # ── DRY_RUN 分支（默认，绝不触网） ─────────────────────────

    def _dry_run_submit(self, intent: Dict, confirmation, plan) -> BrokerAck:
        """演练模式：只打印 + 落审计，不调券商。

        broker_order_id 用 dry_ 前缀模拟，明确区分真实订单号。
        """
        symbol = intent.get("symbol", "?")
        side = intent.get("side", "?")
        qty = intent.get("quantity", 0)
        dry_id = f"dry_{plan.plan_id}_{intent.get('plan_order_id', 'x')}"
        print(f"[DRY_RUN] LiveBroker 演练提交（未触达券商）：{side} {symbol} {qty} "
              f"→ broker_order_id={dry_id}")
        dbm.audit(self.conn, "BROKER_DRY_RUN", entity_type="intent",
                  entity_id=str(intent.get("client_request_id")),
                  payload={"broker_order_id": dry_id, "symbol": symbol,
                           "side": side, "quantity": qty,
                           "confirmation": confirmation.confirmation_id,
                           "plan_id": plan.plan_id})
        return BrokerAck(broker_order_id=dry_id, status="DRY_RUN_SUBMITTED",
                         is_live=False)

    # ── LIVE 分支（唯一真实下单路径，私有） ────────────────────

    def _submit_live(self, intent: Dict, confirmation, plan) -> BrokerAck:
        """真实提交（长桥 Python SDK）。私有方法：全仓唯一调用点 = submit()。

        提交前再次确认（防御式，与 submit() 顶层一致）：
            confirmation 已 APPROVED 且已 CONSUMED（DB）。
        任何券商调用失败 → LiveBrokerError（fail closed，不产生 ack）。
        """
        # 防御式断言（grep 审计锚点）：确认已批准 + 已消费
        if confirmation.status != "APPROVED":
            raise LiveBrokerSafetyError("只有 APPROVED 的 Confirmation 可提交")
        row = dbm.get_confirmation(self.conn, confirmation.confirmation_id)
        if row is None or row["status"] != "CONSUMED":
            raise LiveBrokerSafetyError(
                "LIVE 提交必须经 OrderManager.consume 链（confirmation 已消费）")

        client = self._get_client()
        # 推送可用则接入；不可用时仍可由 poll_active_orders 恢复。
        self.attach_order_stream()
        symbol = intent["symbol"]
        side = "buy" if intent["side"] == "BUY" else "sell"
        qty = int(intent["quantity"])
        order_type = intent.get("order_type", "MARKET")
        price = intent.get("reference_price")
        remark = f"tc:{intent.get('client_request_id', '')}"

        try:
            # 长桥订单类型映射：MARKET/LIMIT → LO（限价单，比纯市价更可控）
            lb_type = "LO"
            if qty <= 0 or abs(float(intent["quantity"]) - qty) > 1e-9:
                raise LiveBrokerError("长桥现货订单数量必须为正整数")
            if price is None:
                raise LiveBrokerError("LIVE 限价提交缺少 reference_price，拒绝提交")
            result = client.order(
                side=side, symbol=symbol, qty=qty,
                order_type=lb_type,
                price=float(price) if price is not None else None,
                remark=remark,
            )
        except LiveBrokerError:
            raise
        except Exception as e:
            raise LiveBrokerError(
                f"券商提交失败（fail closed）：{symbol} {side} {qty} — {e}") from e

        if result is None or not result.get("success"):
            raise LiveBrokerError(
                f"券商提交未成功（fail closed）：{symbol} {side} {qty} — {result}")

        broker_order_id = str(result.get("order_id", ""))
        if not broker_order_id:
            raise LiveBrokerError("券商未返回 order_id（fail closed）")

        dbm.audit(self.conn, "BROKER_LIVE_SUBMIT", entity_type="intent",
                  entity_id=str(intent.get("client_request_id")),
                  payload={"broker_order_id": broker_order_id, "symbol": symbol,
                           "side": intent["side"], "quantity": qty,
                           "order_type": lb_type, "price": price,
                           "confirmation": confirmation.confirmation_id,
                           "plan_id": plan.plan_id,
                           "account_id": self.account_id})
        return BrokerAck(broker_order_id=broker_order_id, status="SUBMITTED",
                         is_live=True, raw=result)

    # ── 券商客户端（懒加载） ───────────────────────────────────

    def _get_client(self):
        """返回券商客户端。已注入（mock/真实）则直接用；否则懒加载 LongbridgeClient。

        懒加载失败（无 SDK / 无凭证）→ LiveBrokerSafetyError：
        想开 LIVE 但没有可用券商凭证，构造/提交时报错而不是静默降级。
        """
        if self._client_loaded:
            return self.client
        try:
            from shared.longbridge_client import LongbridgeClient
            self.client = LongbridgeClient(scope="trade")
            self._client_loaded = True
            return self.client
        except Exception as e:
            raise LiveBrokerSafetyError(
                f"LIVE 模式需要可用的券商客户端，懒加载 LongbridgeClient 失败: {e}") from e

    def validate_ready(self) -> bool:
        """只验证交易 SDK 和推送通道，不提交订单。"""
        self._get_client()
        self.attach_order_stream()
        return True

    # ── SDK 异步推送 + 轮询恢复 ────────────────────────────────

    def attach_order_stream(self) -> bool:
        """接入 SDK 订单推送；回调线程只入队，DB 更新在主线程 drain。

        SQLite 连接默认不能跨线程使用，因此绝不在 SDK 回调线程里直接写库。
        """
        if not self.enable_live:
            return False
        if self._stream_attached:
            return True
        client = self._get_client()
        setter = getattr(client, "set_order_changed_callback", None)
        if setter is None:
            return False
        attached = bool(setter(self._event_queue.append))
        self._stream_attached = attached
        dbm.audit(self.conn, "BROKER_STREAM", entity_type="account",
                  entity_id=self.account_id, payload={"attached": attached})
        return attached

    def drain_events(self) -> Dict:
        """在拥有 DB 连接的线程消费已入队 SDK 事件。"""
        processed, ignored, errors = 0, 0, []
        while self._event_queue:
            raw = self._event_queue.popleft()
            try:
                if self._apply_order_snapshot(raw):
                    processed += 1
                else:
                    ignored += 1
            except Exception as exc:
                errors.append(str(exc))
        return {"processed": processed, "ignored": ignored, "errors": errors}

    def poll_active_orders(self, plan_id: Optional[str] = None) -> Dict:
        """轮询恢复非终态 intent，用于推送丢失/进程重启后的最终一致性。"""
        checked, updated, errors = 0, 0, []
        for row in dbm.list_intents(self.conn, plan_id):
            if row["status"] not in ("SUBMITTING", "SUBMITTED", "UNKNOWN"):
                continue
            checked += 1
            broker_order_id = row["broker_order_id"]
            if not broker_order_id:
                errors.append(f"intent {row['intent_id']} 缺 broker_order_id")
                continue
            snapshot = self.order_state(broker_order_id)
            if snapshot is None:
                errors.append(f"broker 无订单 {broker_order_id}")
                continue
            if self._apply_order_snapshot(snapshot, intent=dict(row)):
                updated += 1
        dbm.audit(self.conn, "BROKER_POLL", entity_type="plan",
                  entity_id=plan_id or "*",
                  payload={"checked": checked, "updated": updated, "errors": errors})
        return {"ok": not errors, "checked": checked, "updated": updated,
                "errors": errors}

    def _apply_order_snapshot(self, snapshot: Dict,
                              intent: Optional[Dict] = None) -> bool:
        """把累计券商快照转为幂等的本地增量事件。"""
        from execution.broker import normalize_broker_status

        broker_order_id = str(snapshot.get("order_id") or
                              snapshot.get("broker_order_id") or "")
        if intent is None and broker_order_id:
            row = dbm.get_intent_by_broker_order_id(self.conn, broker_order_id)
            intent = dict(row) if row is not None else None
        if intent is None:
            remark = str(snapshot.get("remark") or "")
            if remark.startswith("tc:"):
                row = dbm.get_intent_by_request_id(self.conn, remark[3:])
                intent = dict(row) if row is not None else None
        if intent is None:
            return False

        status = normalize_broker_status(snapshot.get("status"))
        base = {"intent_id": intent["intent_id"],
                "broker_order_id": broker_order_id or intent.get("broker_order_id")}
        cumulative = float(snapshot.get("executed_quantity") or 0)
        already = (self.event_handler.filled_quantity(intent["intent_id"])
                   if self.event_handler is not None else 0.0)
        delta = cumulative - already
        if delta > 1e-9:
            price = float(snapshot.get("executed_price") or snapshot.get("price") or 0)
            if price <= 0:
                raise LiveBrokerError(f"成交快照缺成交价: {broker_order_id}")
            fill_event = {**base, "symbol": intent["symbol"],
                          "side": intent["side"], "quantity": delta,
                          "price": price}
            if status == "FILLED":
                self.on_filled(fill_event)
            else:
                self.on_partial_fill(fill_event)
            return True
        if status == "FILLED":
            return False
        elif status == "REJECTED":
            self.on_rejected({**base, "reason": snapshot.get("message")})
        elif status == "CANCELLED":
            self.on_cancelled(base)
        elif status == "SUBMITTED":
            self.on_submitted(base)
        else:
            dbm.set_intent_status(self.conn, intent["intent_id"], "UNKNOWN",
                                  base["broker_order_id"])
        return True

    def order_state(self, broker_order_id: str) -> Optional[Dict]:
        """Reconciliation 兼容接口：按券商订单号查状态。

        - DRY_RUN：返回模拟存在（本地演练单视为"存在"）
        - LIVE：调券商查询；查不到 → None（触发 Reconciliation fail closed）
        """
        if not self.enable_live:
            return {"broker_order_id": broker_order_id, "status": "SUBMITTED"}
        try:
            client = self._get_client()
            return client.order_query(broker_order_id)
        except Exception as e:
            dbm.audit(self.conn, "BROKER_ORDER_QUERY_FAILED", entity_type="broker_order",
                      entity_id=broker_order_id, payload={"error": str(e)})
            return None

    # ── BrokerEventHandler 兼容接口（事件 → 审计 + 状态机） ────

    def on_submitted(self, event: Dict) -> None:
        """券商事件：订单已提交。"""
        self._dispatch("submitted", event)

    def on_rejected(self, event: Dict) -> None:
        """券商事件：订单被拒。"""
        self._dispatch("rejected", event)

    def on_partial_fill(self, event: Dict) -> None:
        """券商事件：部分成交。event 需含 {intent_id, symbol, side, quantity, price}。

        委托现有 BrokerEventHandler 的 filled 事件（数量=本次成交量），
        由其累计判断 partial/full 并正确恢复状态。
        """
        self._dispatch("filled", event)

    def on_filled(self, event: Dict) -> None:
        """券商事件：成交（含最后一笔补足）。"""
        self._dispatch("filled", event)

    def on_cancelled(self, event: Dict) -> None:
        """券商事件：撤单。"""
        self._dispatch("cancelled", event)

    def on_changed(self, event: Dict) -> None:
        """券商事件：回改（数量/价格）。"""
        self._dispatch("changed", event)

    def _dispatch(self, etype: str, event: Dict) -> None:
        """事件落审计（BROKER_EVENT）+ 可选委托 BrokerEventHandler 驱动状态机。"""
        dbm.audit(self.conn, "BROKER_EVENT", entity_type="intent",
                  entity_id=str(event.get("intent_id")),
                  payload={"event": etype, **event})
        if self.event_handler is not None:
            self.event_handler.handle({**event, "type": etype})


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from execution.models import ExecutionPlan, PlanOrder, Confirmation, now_utc
    from execution.order_manager import ConfirmationService, ApprovalAdapter, OrderManager

    conn = dbm.get_conn(":memory:")

    class _FakeClient:
        """不触网的 mock 券商。"""
        def __init__(self):
            self.calls = []
            self._n = 0
        def order(self, side, symbol, qty, **kwargs):
            self.calls.append({"side": side, "symbol": symbol, "qty": qty, **kwargs})
            self._n += 1
            return {"order_id": f"mock_{self._n}", "symbol": symbol, "side": side,
                    "quantity": qty, "order_type": kwargs.get("order_type", "LO"),
                    "status": "Submitted", "success": True}

    fake = _FakeClient()
    plan = ExecutionPlan(
        plan_id="p_smoke", account_id="default", execution_mode="LIVE",
        expires_at="2099-01-01T00:00:00Z",
        orders=[PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96)])
    svc = ConfirmationService(conn)
    approved = ApprovalAdapter(conn, channel="cli").approve(
        svc.create(plan).confirmation_id, approved_by="owner", nonce="smoke_1")

    # DRY_RUN 默认：不触网
    dry = LiveBroker(conn, client=fake, enable_live=False)
    intent = {"client_request_id": "cr_x", "plan_id": "p_smoke", "plan_order_id": "1",
              "symbol": "NVDA.US", "side": "BUY", "quantity": 10, "status": "PENDING"}
    ack = dry.submit(intent, confirmation=approved, plan=plan)
    assert ack.status == "DRY_RUN_SUBMITTED" and fake.calls == []
    print(f"DRY_RUN 演练通过：{ack}（未触达券商）")

    # LIVE：经 OrderManager.consume 链后提交
    live = LiveBroker(conn, client=fake, enable_live=True)
    om = OrderManager(conn, broker=live)
    created = om.submit(plan, approved)
    assert len(fake.calls) == 1 and fake.calls[0]["symbol"] == "NVDA.US"
    print(f"LIVE 演练通过：{fake.calls[0]}")

    # 直通防护：未消费的 APPROVED confirmation 直接提交 → 拒绝
    plan2 = ExecutionPlan(
        plan_id="p_smoke2", account_id="default", execution_mode="LIVE",
        expires_at="2099-01-01T00:00:00Z",
        orders=[PlanOrder("1", "NVDA.US", "BUY", 10, reference_price=223.96)])
    approved2 = ApprovalAdapter(conn, channel="cli").approve(
        svc.create(plan2).confirmation_id, approved_by="owner", nonce="smoke_2")
    before = len(fake.calls)
    try:
        live.submit(intent, confirmation=approved2, plan=plan2)
        raise AssertionError("未消费 confirmation 直通提交应被拒绝")
    except LiveBrokerSafetyError:
        pass
    assert len(fake.calls) == before, "直通提交不应触达券商"
    print("直通防护通过 ✅（未消费 APPROVED confirmation 被拒绝，未触网）")
