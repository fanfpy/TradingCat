"""生产通知边界：默认只写审计日志，可选发送 Webhook。

通知失败绝不改变交易状态；调用方得到 False 并可据此报警。Webhook 只有显式配置
``TRADINGCAT_NOTIFICATION_WEBHOOK`` 后才启用。
"""

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Protocol, runtime_checkable
from urllib.request import Request, urlopen

from shared import db as dbm


@dataclass(frozen=True)
class Notification:
    topic: str
    title: str
    message: str
    severity: str = "INFO"
    entity_type: str = "system"
    entity_id: str = ""


@runtime_checkable
class NotificationAdapter(Protocol):
    def send(self, notification: Notification) -> bool:
        ...


class AuditNotificationAdapter:
    """永远可用的默认适配器：通知进入 audit_log。"""

    def __init__(self, conn):
        self.conn = conn

    def send(self, notification: Notification) -> bool:
        dbm.audit(
            self.conn, "NOTIFICATION",
            entity_type=notification.entity_type,
            entity_id=notification.entity_id,
            payload=asdict(notification),
        )
        return True


class WebhookNotificationAdapter:
    """通用 JSON Webhook；适配企业微信/自建通知网关。"""

    def __init__(self, url: str, timeout: float = 5.0):
        if not url.startswith(("https://", "http://")):
            raise ValueError("notification webhook 必须是 http(s) URL")
        self.url = url
        self.timeout = timeout

    def send(self, notification: Notification) -> bool:
        payload: Dict = asdict(notification)
        payload["sent_at"] = datetime.now(timezone.utc).isoformat()
        request = Request(
            self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except Exception as exc:
            print(f"[错误] 通知 Webhook 发送失败: {exc}", file=sys.stderr)
            return False


class CompositeNotificationAdapter:
    def __init__(self, *adapters: NotificationAdapter):
        self.adapters = adapters

    def send(self, notification: Notification) -> bool:
        results = []
        for adapter in self.adapters:
            try:
                results.append(adapter.send(notification))
            except Exception as exc:
                print(f"[错误] 通知适配器失败: {exc}", file=sys.stderr)
                results.append(False)
        return all(results)


def configured_notifier(conn) -> NotificationAdapter:
    """创建生产 notifier；审计日志始终保留。"""
    from shared.env import load_selected
    from shared.config import get_config
    load_selected(("TRADINGCAT_NOTIFICATION_WEBHOOK",))
    audit = AuditNotificationAdapter(conn)
    webhook = get_config().integrations.notification_webhook
    if not webhook:
        return audit
    return CompositeNotificationAdapter(audit, WebhookNotificationAdapter(webhook))


def notify(conn, topic: str, title: str, message: str, *, severity: str = "INFO",
           entity_type: str = "system", entity_id: str = "",
           adapter: Optional[NotificationAdapter] = None) -> bool:
    return (adapter or configured_notifier(conn)).send(Notification(
        topic=topic, title=title, message=message, severity=severity,
        entity_type=entity_type, entity_id=entity_id,
    ))


def safe_notify(conn, topic: str, title: str, message: str, **kwargs) -> bool:
    """业务层旁路通知：任何通知错误都不改变决策/执行结果。"""
    try:
        return notify(conn, topic, title, message, **kwargs)
    except Exception as exc:
        print(f"[错误] 通知旁路失败: {exc}", file=sys.stderr)
        return False


def dispatch_signal_outbox(conn, adapter: Optional[NotificationAdapter] = None,
                           limit: int = 100) -> Dict[str, int]:
    """消费已与 SignalEvent 同事务落库的通知任务。

    Worker 崩溃后任务保留；重复业务信号不会创建新的 outbox 行。发送端若支持
    幂等键，应使用 ``outbox_id`` 作为 provider idempotency key。
    """
    target = adapter or configured_notifier(conn)
    rows = (
        dbm.list_notification_outbox(conn, status="PENDING", limit=limit)
        + dbm.list_notification_outbox(conn, status="FAILED_RETRYABLE", limit=limit)
    )[:limit]
    summary = {"processed": 0, "sent": 0, "failed": 0}
    for row in rows:
        summary["processed"] += 1
        dbm.mark_notification_outbox(conn, row["outbox_id"], "SENDING")
        try:
            payload = json.loads(row["payload_json"])
            symbol = payload.get("symbol", "")
            kind = payload.get("kind", "SIGNAL")
            ok = target.send(Notification(
                topic=f"signal.{kind.lower()}",
                title=f"{symbol} {kind} 信号",
                message=payload.get("rationale") or json.dumps(
                    payload, ensure_ascii=False, sort_keys=True),
                severity="WARNING",
                entity_type="signal",
                entity_id=row["event_id"],
            ))
        except Exception as exc:
            print(f"[错误] Outbox {row['outbox_id']} 发送失败: {exc}", file=sys.stderr)
            ok = False
        if ok:
            dbm.mark_notification_outbox(conn, row["outbox_id"], "SENT")
            summary["sent"] += 1
        else:
            dbm.mark_notification_outbox(
                conn, row["outbox_id"], "FAILED_RETRYABLE")
            summary["failed"] += 1
    return summary
