#!/usr/bin/env python3
"""
AccountState 建模 — 交易系统 v4.0（架构 D-7）
=============================================
账户状态是执行链的硬前置条件：sync_status 非 SYNCED → PreTradeRisk 必须 REJECT
已批准订单（不阻止研究/信号/计划生成）。

状态机：
    SYNCED      — 最近一次券商同步成功且数据新鲜
    STALE       — 最近一次同步成功但已过期（超过 freshness 阈值）
    RECONCILING — 正在进行 broker/local 对账（D-9）
    MISMATCH    — 对账发现 broker/local 不一致（fail closed）
    UNKNOWN     — 从未同步或同步失败

设计：
- 唯一真相源：长桥 OpenAPI（broker），本地 trading_account 表只是快照
- sync_account() 是唯一写入入口（StateRepository 收口 D-13）
- 每次同步写 audit（D-10 lineage）
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from shared import db as dbm

DEFAULT_ACCOUNT_ID = "default"
# 账户快照新鲜度阈值（秒）：超过则 sync_status 降级 STALE
FRESHNESS_SECONDS = 30 * 60


def _broker_collection(client, method_name: str) -> List[Dict]:
    """交易状态同步优先使用 strict 查询；兼容不支持 strict 的测试/第三方 client。"""
    import inspect
    method = getattr(client, method_name)
    parameters = inspect.signature(method).parameters
    return method(strict=True) if "strict" in parameters else method()


@dataclass
class AccountState:
    account_id: str = DEFAULT_ACCOUNT_ID
    sync_status: str = "UNKNOWN"      # SYNCED|STALE|RECONCILING|MISMATCH|UNKNOWN
    cash: Optional[float] = None
    buying_power: Optional[float] = None
    nav: Optional[float] = None
    positions: List[Dict] = field(default_factory=list)
    open_orders: List[Dict] = field(default_factory=list)
    updated_at: Optional[str] = None
    raw_json: Optional[str] = None
    metadata: List[Dict] = field(default_factory=list)
    metadata_failures: List[Dict] = field(default_factory=list)
    source: str = "unknown"
    source_version: str = "unknown"
    snapshot_version: Optional[str] = None
    failure_reason: Optional[Dict] = None
    last_success_at: Optional[str] = None
    last_attempt_at: Optional[str] = None

    @property
    def synced(self) -> bool:
        return self.sync_status == "SYNCED"


def load(conn, account_id: str = DEFAULT_ACCOUNT_ID) -> AccountState:
    """从 StateRepository 读取账户快照（不存在 → UNKNOWN）。"""
    row = dbm.get_account(conn, account_id)
    if row is None:
        return AccountState(account_id=account_id)
    positions = []
    metadata = []
    metadata_failures = []
    for row in dbm.list_account_positions(conn, account_id):
        try:
            position = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            position = {}
        position.update({
            "symbol": row["symbol"], "quantity": row["quantity"],
            "cost_price": row["cost_price"], "last_price": row["last_price"],
        })
        positions.append(position)
        if row["metadata_status"] == "HYDRATED":
            metadata.append({
                "symbol": row["symbol"], "ok": True,
                "metadata_source": row["metadata_source"],
                "metadata_version": row["metadata_version"],
            })
        else:
            metadata_failures.append({
                "symbol": row["symbol"], "ok": False,
                "error_message": row["metadata_error"] or "UNKNOWN_METADATA",
            })
    orders = []
    for row in dbm.list_account_orders(conn, account_id):
        try:
            order = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            order = {}
        order.setdefault("symbol", row["symbol"])
        order.setdefault("side", row["side"])
        order.setdefault("quantity", row["quantity"])
        order.setdefault("status", row["status"])
        orders.append(order)
    failure_reason = None
    if row := dbm.get_account(conn, account_id):
        if row["last_error_message"]:
            failure_reason = {
                "error_type": row["last_error_type"],
                "error_message": row["last_error_message"],
                "retryable": bool(row["last_error_retryable"]),
            }
    return AccountState(
        account_id=row["account_id"],
        sync_status=row["sync_status"], cash=row["cash"],
        buying_power=row["buying_power"], nav=row["nav"],
        positions=positions, open_orders=orders, metadata=metadata,
        metadata_failures=metadata_failures, updated_at=row["updated_at"],
        raw_json=row["raw_json"], source=row["source"],
        source_version=row["source_version"], snapshot_version=row["snapshot_version"],
        failure_reason=failure_reason, last_success_at=row["last_success_at"],
        last_attempt_at=row["last_attempt_at"],
    )


def _source_identity(client) -> tuple[str, str]:
    return "longbridge", f"{type(client).__module__}.{type(client).__name__}"


def _degraded_status(old) -> str:
    if old is None:
        return "UNKNOWN"
    return "MISMATCH" if old["sync_status"] == "MISMATCH" else "STALE"


def _normalize_position(position: Dict) -> Dict:
    symbol = str(position.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("UNKNOWN_METADATA: position symbol is empty")
    normalized = dict(position)
    normalized["symbol"] = symbol
    normalized["quantity"] = float(position.get("quantity", 0) or position.get("position_amount", 0) or 0)
    return normalized


def _metadata_for_positions(positions: List[Dict], security_service) -> tuple[List[Dict], List[Dict]]:
    results = security_service.ensure_batch(p["symbol"] for p in positions)
    by_symbol = {item["symbol"]: item for item in results}
    failures = [item for item in results if not item["ok"]]
    hydrated = []
    for position in positions:
        result = by_symbol.get(position["symbol"], {
            "symbol": position["symbol"], "ok": False,
            "error_message": "UNKNOWN_METADATA: hydration result missing",
        })
        item = dict(position)
        item["metadata_status"] = "HYDRATED" if result["ok"] else "UNKNOWN"
        item["metadata_source"] = (result.get("metadata") or {}).get(
            "metadata_source", "unknown")
        item["metadata_version"] = (result.get("metadata") or {}).get(
            "metadata_version", "unknown")
        item["metadata_error"] = None if result["ok"] else result.get("error_message")
        item["metadata"] = result.get("metadata")
        hydrated.append(item)
    return hydrated, failures


def _is_fresh(updated_at: Optional[str], max_age_seconds: int = FRESHNESS_SECONDS) -> bool:
    from datetime import datetime, timezone
    if not updated_at:
        return False
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return 0 <= age <= max_age_seconds


def sync_account(conn, client=None, account_id: str = DEFAULT_ACCOUNT_ID,
                 security_service=None, idempotency_key: Optional[str] = None) -> AccountState:
    """从券商（长桥）同步账户资产 → 写入 StateRepository。

    - 成功 → SYNCED（含 cash/buying_power）
    - 失败 → UNKNOWN（保留旧快照，但 sync_status 降级；若旧快照存在则降级 STALE）
    返回 AccountState。不抛异常（调用方降级处理）。
    """
    sync_run = None
    source = "unknown"
    source_version = "unknown"
    try:
        if client is None:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="trade")
        source, source_version = _source_identity(client)
        sync_run = dbm.begin_sync_run(
            conn, "account", account_id=account_id, source=source,
            source_version=source_version, idempotency_key=idempotency_key)
        assets = client.assets()
        if not assets:
            raise RuntimeError("长桥 assets() 返回空")
        def first_present(*keys):
            for key in keys:
                if key in assets and assets[key] is not None:
                    return assets[key]
            return None

        cash = first_present("total_cash", "cash")
        buying_power = first_present("buying_power", "net_buying_power", "max_finance_amount")
        nav = first_present("net_assets", "nav", "equity")
        positions_raw = _broker_collection(client, "positions") if hasattr(client, "positions") else []
        positions = [_normalize_position(item) for item in (positions_raw or [])]
        if security_service is None:
            from shared.security import LazyLongbridgeSecurityProvider, SecurityService
            provider = client if hasattr(client, "static_info") else LazyLongbridgeSecurityProvider()
            security_service = SecurityService(conn, provider)
        hydrated_positions, failures = _metadata_for_positions(positions, security_service)
        metadata_results = [
            {"symbol": item["symbol"], "ok": item["metadata_status"] == "HYDRATED",
             "metadata_source": item["metadata_source"],
             "metadata_version": item["metadata_version"],
             "metadata": item.get("metadata"),
             **({"error_message": item["metadata_error"]} if item["metadata_error"] else {})}
            for item in hydrated_positions
        ]
        if failures:
            dbm.audit(conn, "SECURITY_METADATA_FAILED", "account", account_id,
                      {"failures": failures})
        old = dbm.get_account(conn, account_id)
        open_orders = _broker_collection(client, "orders") if hasattr(client, "orders") else []
        now = dbm._now()
        persisted_status = (
            "MISMATCH" if old is not None and old["sync_status"] == "MISMATCH"
            and not failures else "SYNCED" if not failures else _degraded_status(old))
        snapshot_version = f"{source_version}:{now}"
        if sync_run is None:
            raise RuntimeError("account sync run not initialized")
        # Even a PARTIAL run persists the broker observation for audit/recovery, but
        # only a fully hydrated run advances the account's successful freshness.
        dbm.replace_account_positions(conn, account_id, hydrated_positions,
                                      sync_run["sync_id"], observed_at=now)
        dbm.replace_account_orders(conn, account_id, open_orders or [],
                                    sync_run["sync_id"], observed_at=now)
        raw_json = json.dumps(assets, ensure_ascii=False, default=str)[:2000]
        if not failures:
            dbm.upsert_account(
                conn, account_id, persisted_status,
                float(cash) if cash is not None else None,
                float(buying_power) if buying_power is not None else None,
                raw_json, nav=float(nav) if nav is not None else None,
                source=source, source_version=source_version,
                snapshot_version=snapshot_version, observed_at=now)
            run_status = "SYNCED" if persisted_status == "SYNCED" else "PARTIAL"
        else:
            dbm.set_account_sync_status(
                conn, account_id, persisted_status,
                error_type="MetadataHydrationError", error_message=json.dumps(
                    failures, ensure_ascii=False)[:500], retryable=any(
                        item.get("retryable", False) for item in failures))
            run_status = "PARTIAL"
        dbm.finish_sync_run(conn, sync_run["sync_id"], run_status,
                            details={"positions": len(positions),
                                     "orders": len(open_orders or []),
                                     "metadata_failures": failures})
        state = AccountState(
            account_id=account_id,
            sync_status=persisted_status,
            cash=float(cash) if cash is not None else None,
            buying_power=float(buying_power) if buying_power is not None else None,
            nav=float(nav) if nav is not None else None,
            positions=positions or [],
            open_orders=open_orders or [],
            updated_at=now if not failures else (old["updated_at"] if old else None),
            raw_json=raw_json,
            metadata=metadata_results,
            metadata_failures=failures,
            source=source, source_version=source_version,
            snapshot_version=snapshot_version,
            last_success_at=now if not failures else (old["last_success_at"] if old else None),
            last_attempt_at=now,
        )
        dbm.audit(conn, "ACCOUNT_SYNC", entity_type="account", entity_id=account_id,
                  payload={"sync_status": persisted_status, "cash": state.cash,
                           "buying_power": state.buying_power, "nav": state.nav,
                           "positions": len(state.positions), "open_orders": len(state.open_orders),
                           "metadata_failures": failures, "source": source,
                           "source_version": source_version, "sync_id": sync_run["sync_id"]})
        return state
    except Exception as e:
        # 同步失败：降级，不抛（交易系统不能因账户查询失败而崩）
        old = dbm.get_account(conn, account_id)
        degraded = _degraded_status(old)
        retryable = bool(getattr(e, "retryable", False))
        dbm.set_account_sync_status(
            conn, account_id, degraded, error_type=type(e).__name__,
            error_message=str(e)[:500], retryable=retryable)
        if sync_run is not None:
            dbm.finish_sync_run(conn, sync_run["sync_id"], "FAILED",
                                error_type=type(e).__name__,
                                error_message=str(e)[:500], retryable=retryable)
        dbm.audit(conn, "ACCOUNT_SYNC", entity_type="account", entity_id=account_id,
                  payload={"sync_status": degraded, "error_type": type(e).__name__,
                           "error": str(e)[:500], "retryable": retryable,
                           "sync_id": sync_run["sync_id"] if sync_run else None})
        return AccountState(account_id=account_id, sync_status=degraded,
                            positions=[dict(row) for row in dbm.list_account_positions(conn, account_id)],
                            metadata_failures=[{"error_type": type(e).__name__,
                                                "error_message": str(e)[:500],
                                                "retryable": retryable}],
                            failure_reason={"error_type": type(e).__name__,
                                            "error_message": str(e)[:500],
                                            "retryable": retryable})


def ensure_synced(conn, account_id: str = DEFAULT_ACCOUNT_ID,
                  max_age_seconds: int = FRESHNESS_SECONDS) -> AccountState:
    """读账户并校验新鲜度；过期 → 标记 STALE。

    返回 AccountState；调用方（PreTradeRisk）据此拒绝非 SYNCED 账户。
    """
    state = load(conn, account_id)
    if state.sync_status == "SYNCED" and not _is_fresh(state.updated_at, max_age_seconds):
        dbm.set_account_sync_status(conn, account_id, "STALE")
        state.sync_status = "STALE"
    return state


def sync_positions(conn, client=None, account_id: str = DEFAULT_ACCOUNT_ID,
                   security_service=None, idempotency_key: Optional[str] = None) -> dict:
    """从券商同步持仓快照 → 写入 audit。

    架构 v4.0 PositionSync（§5 半成品补全）：
    - 从长桥 OpenAPI 获取当前持仓
    - 与本地 portfolio 表对账：不一致 → AccountState 降级 MISMATCH
    - 一致 → audit 记录 POSITION_SYNC
    - 失败 → 降级，不抛异常

    Returns:
        {synced: bool, positions: [...], mismatch: bool, details: str}
    """
    sync_run = None
    source = "unknown"
    source_version = "unknown"
    try:
        if client is None:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="trade")
        source, source_version = _source_identity(client)
        sync_run = dbm.begin_sync_run(
            conn, "positions", account_id=account_id, source=source,
            source_version=source_version, idempotency_key=idempotency_key)
        if security_service is None:
            from shared.security import (
                LazyLongbridgeSecurityProvider, SecurityService,
            )
            provider = client if hasattr(client, "static_info") else LazyLongbridgeSecurityProvider()
            security_service = SecurityService(
                conn, provider)
        broker_positions = [_normalize_position(item) for item in (
            _broker_collection(client, "positions") or [])]
        hydrated_positions, metadata_failures = _metadata_for_positions(
            broker_positions, security_service)
        metadata_results = [{
            "symbol": item["symbol"], "ok": item["metadata_status"] == "HYDRATED",
            "metadata": item.get("metadata"),
            **({"error_message": item["metadata_error"]}
               if item["metadata_error"] else {}),
        } for item in hydrated_positions]
        if metadata_failures:
            dbm.audit(conn, "SECURITY_METADATA_FAILED", "account", account_id,
                      {"failures": metadata_failures})
        # 长桥返回格式：[{symbol, quantity, cost_price, current_price, ...}]

        # 获取本地 portfolio 表持仓
        local_positions = dbm.list_positions(conn)
        local_map = {p["symbol"]: float(p["quantity"]) for p in local_positions}

        # 对账：检查本地有但 broker 无的持仓（可能已平仓但本地未更新）
        mismatches = []
        broker_map = {}
        for bp in broker_positions:
            sym = bp.get("symbol", "")
            qty = float(bp.get("quantity", 0) or bp.get("position_amount", 0))
            broker_map[sym] = qty
            local_qty = local_map.get(sym, 0)
            if abs(qty - local_qty) > 0.001:  # 容差 0.001 股
                mismatches.append(f"{sym}: broker={qty} local={local_qty}")

        # 检查本地有但 broker 无的持仓
        for sym, local_qty in local_map.items():
            if sym not in broker_map and abs(local_qty) > 0.001:
                mismatches.append(f"{sym}: broker=0 local={local_qty} (可能已平仓)")

        ok = not mismatches and not metadata_failures
        dbm.replace_account_positions(
            conn, account_id, hydrated_positions, sync_run["sync_id"],
            observed_at=dbm._now())
        old = dbm.get_account(conn, account_id)
        if mismatches:
            dbm.set_account_sync_status(conn, account_id, "MISMATCH")
        elif metadata_failures:
            dbm.set_account_sync_status(
                conn, account_id, _degraded_status(old),
                error_type="MetadataHydrationError",
                error_message=json.dumps(metadata_failures, ensure_ascii=False)[:500],
                retryable=any(item.get("retryable", False) for item in metadata_failures))
        elif old is None:
            # A position-only sync cannot claim account assets are fresh.
            dbm.set_account_sync_status(conn, account_id, "UNKNOWN")
        elif old["sync_status"] == "MISMATCH" and _is_fresh(
                old["last_success_at"] or old["updated_at"]):
            # Explicit fresh broker snapshot + successful reconciliation recovers
            # MISMATCH; a position-only call with an old snapshot cannot do so.
            dbm.set_account_sync_status(conn, account_id, "SYNCED")
        dbm.finish_sync_run(
            conn, sync_run["sync_id"], "SYNCED" if ok else "PARTIAL",
            details={"broker_count": len(broker_positions),
                     "local_count": len(local_positions),
                     "mismatches": mismatches,
                     "metadata_failures": metadata_failures})
        detail_parts = list(mismatches)
        if metadata_failures:
            detail_parts.append("metadata failed")
        dbm.audit(conn, "POSITION_SYNC", entity_type="account", entity_id=account_id,
                  payload={"ok": ok, "mismatches": mismatches,
                           "broker_count": len(broker_positions),
                           "local_count": len(local_positions),
                           "metadata_failures": metadata_failures,
                           "source": source, "source_version": source_version,
                           "sync_id": sync_run["sync_id"]})
        return {"synced": ok, "positions": broker_map, "mismatch": not ok,
            "details": "; ".join(detail_parts) if detail_parts else "ok",
            "metadata": metadata_results,
            "metadata_failures": metadata_failures,
            "sync_status": dbm.get_account(conn, account_id)["sync_status"],
            "sync_id": sync_run["sync_id"], "source": source,
            "source_version": source_version}
    except Exception as e:
        old = dbm.get_account(conn, account_id)
        degraded = _degraded_status(old)
        retryable = bool(getattr(e, "retryable", False))
        dbm.set_account_sync_status(
            conn, account_id, degraded, error_type=type(e).__name__,
            error_message=str(e)[:500], retryable=retryable)
        if sync_run is not None:
            dbm.finish_sync_run(conn, sync_run["sync_id"], "FAILED",
                                error_type=type(e).__name__,
                                error_message=str(e)[:500], retryable=retryable)
        dbm.audit(conn, "POSITION_SYNC", entity_type="account", entity_id=account_id,
                  payload={"ok": False, "error_type": type(e).__name__,
                           "error": str(e)[:500], "retryable": retryable,
                           "sync_status": degraded,
                           "sync_id": sync_run["sync_id"] if sync_run else None})
        return {"synced": False, "positions": {}, "mismatch": False,
            "details": f"sync failed: {str(e)[:200]}",
            "metadata": [], "metadata_failures": [{
                "error_type": type(e).__name__, "error_message": str(e)[:500],
                "retryable": retryable}], "sync_status": degraded,
            "sync_id": sync_run["sync_id"] if sync_run else None,
            "source": source, "source_version": source_version}


if __name__ == "__main__":
    conn = dbm.get_conn(":memory:")

    # 1. 初始 UNKNOWN
    s0 = ensure_synced(conn)
    assert s0.sync_status == "UNKNOWN" and not s0.synced

    # 2. 手工写入 SYNCED 快照
    dbm.upsert_account(conn, "default", "SYNCED", cash=100000.0, buying_power=50000.0)
    s1 = ensure_synced(conn)
    assert s1.synced and s1.cash == 100000.0

    # 3. 过期的 SYNCED → 降级 STALE
    from datetime import datetime, timezone, timedelta
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dbm.set_account_updated_at(conn, "default", old_ts)
    s2 = ensure_synced(conn)
    assert s2.sync_status == "STALE" and not s2.synced

    # 4. 非法状态拒绝
    try:
        dbm.upsert_account(conn, "default", "BOGUS")
        raise AssertionError("非法 sync_status 应被拒绝")
    except (AssertionError, ValueError):
        pass

    # 5. PositionSync 对账（无 broker → 异常降级，不抛）
    def _broker_err(self):
        raise RuntimeError("broker unavailable")
    ps = sync_positions(conn, client=type("Fake", (), {"positions": _broker_err})())
    assert not ps["synced"]  # broker 异常 → 降级且不抛 → synced=False

    # 6. PositionSync 有 broker 但无本地持仓 → ok
    conn2 = dbm.get_conn(":memory:")
    ps2 = sync_positions(conn2, client=type("Fake", (), {"positions": lambda self: []})())
    assert ps2["synced"]

    print("account.py 冒烟测试通过 ✅（含 PositionSync）")
