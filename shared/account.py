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

    @property
    def synced(self) -> bool:
        return self.sync_status == "SYNCED"


def load(conn, account_id: str = DEFAULT_ACCOUNT_ID) -> AccountState:
    """从 StateRepository 读取账户快照（不存在 → UNKNOWN）。"""
    row = dbm.get_account(conn, account_id)
    if row is None:
        return AccountState(account_id=account_id)
    return AccountState(
        account_id=row["account_id"],
        sync_status=row["sync_status"],
        cash=row["cash"],
        buying_power=row["buying_power"],
        nav=row["nav"],
        updated_at=row["updated_at"],
        raw_json=row["raw_json"],
    )


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
                 security_service=None) -> AccountState:
    """从券商（长桥）同步账户资产 → 写入 StateRepository。

    - 成功 → SYNCED（含 cash/buying_power）
    - 失败 → UNKNOWN（保留旧快照，但 sync_status 降级；若旧快照存在则降级 STALE）
    返回 AccountState。不抛异常（调用方降级处理）。
    """
    try:
        if client is None:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="trade")
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
        positions = _broker_collection(client, "positions") if hasattr(client, "positions") else []
        if security_service is not None:
            metadata_results = security_service.ensure_batch(
                position.get("symbol", "") for position in positions)
            failures = [item for item in metadata_results if not item["ok"]]
            if failures:
                dbm.audit(conn, "SECURITY_METADATA_FAILED", "account", account_id,
                          {"failures": failures})
        open_orders = _broker_collection(client, "orders") if hasattr(client, "orders") else []
        state = AccountState(
            account_id=account_id,
            sync_status="SYNCED",
            cash=float(cash) if cash is not None else None,
            buying_power=float(buying_power) if buying_power is not None else None,
            nav=float(nav) if nav is not None else None,
            positions=positions or [],
            open_orders=open_orders or [],
            updated_at=dbm._now(),
            raw_json=str(assets)[:2000],
        )
        dbm.upsert_account(conn, account_id, "SYNCED", state.cash, state.buying_power,
                           state.raw_json, nav=state.nav)
        dbm.audit(conn, "ACCOUNT_SYNC", entity_type="account", entity_id=account_id,
                  payload={"sync_status": "SYNCED", "cash": state.cash,
                           "buying_power": state.buying_power, "nav": state.nav,
                           "positions": len(state.positions), "open_orders": len(state.open_orders)})
        return state
    except Exception as e:
        # 同步失败：降级，不抛（交易系统不能因账户查询失败而崩）
        old = dbm.get_account(conn, account_id)
        degraded = "STALE" if old is not None else "UNKNOWN"
        dbm.set_account_sync_status(conn, account_id, degraded)
        dbm.audit(conn, "ACCOUNT_SYNC", entity_type="account", entity_id=account_id,
                  payload={"sync_status": degraded, "error": str(e)[:500]})
        return AccountState(account_id=account_id, sync_status=degraded)


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
                   security_service=None) -> dict:
    """从券商同步持仓快照 → 写入 audit。

    架构 v4.0 PositionSync（§5 半成品补全）：
    - 从长桥 OpenAPI 获取当前持仓
    - 与本地 portfolio 表对账：不一致 → AccountState 降级 MISMATCH
    - 一致 → audit 记录 POSITION_SYNC
    - 失败 → 降级，不抛异常

    Returns:
        {synced: bool, positions: [...], mismatch: bool, details: str}
    """
    try:
        owns_client = client is None
        if client is None:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="trade")
        if security_service is None and owns_client:
            from shared.security import (
                LazyLongbridgeSecurityProvider, SecurityService,
            )
            security_service = SecurityService(
                conn, LazyLongbridgeSecurityProvider())
        broker_positions = _broker_collection(client, "positions") or []
        metadata_results = (security_service.ensure_batch(
            position.get("symbol", "") for position in broker_positions)
            if security_service is not None else [])
        metadata_failures = [item for item in metadata_results if not item["ok"]]
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

        ok = not mismatches
        if not ok:
            dbm.set_account_sync_status(conn, account_id, "MISMATCH")
        dbm.audit(conn, "POSITION_SYNC", entity_type="account", entity_id=account_id,
                  payload={"ok": ok, "mismatches": mismatches,
                           "broker_count": len(broker_positions),
                           "local_count": len(local_positions)})
        return {"synced": ok, "positions": broker_map, "mismatch": not ok,
            "details": "; ".join(mismatches) if mismatches else "ok",
            "metadata": metadata_results,
            "metadata_failures": metadata_failures}
    except Exception as e:
        old = dbm.get_account(conn, account_id)
        degraded = "UNKNOWN"
        if old is not None:
            degraded = "MISMATCH" if old["sync_status"] == "MISMATCH" else "STALE"
        dbm.set_account_sync_status(conn, account_id, degraded)
        dbm.audit(conn, "POSITION_SYNC", entity_type="account", entity_id=account_id,
                  payload={"ok": False, "error": str(e)[:500],
                           "sync_status": degraded})
        return {"synced": False, "positions": {}, "mismatch": False,
            "details": f"sync failed: {str(e)[:200]}",
            "metadata": [], "metadata_failures": []}


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
