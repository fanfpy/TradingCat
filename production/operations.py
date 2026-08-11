"""生产运维编排：账户同步、当前组合风控与订单对账。"""

from dataclasses import asdict
from typing import Dict, Optional

from execution.broker import Reconciliation
from production.portfolio_risk import PositionPlan, check_portfolio
from shared import db as dbm
from shared.account import ensure_synced, sync_account, sync_positions
from shared.security import require_security_metadata


def sync_runtime_state(conn, client=None, account_id: str = "default",
                       security_service=None) -> Dict:
    """同步资产和持仓并返回最终账户状态；任一不确定均 fail closed。"""
    if security_service is None and client is None:
        from shared.security import LazyLongbridgeSecurityProvider, SecurityService
        security_service = SecurityService(
            conn, LazyLongbridgeSecurityProvider())
    account = sync_account(
        conn, client=client, account_id=account_id,
        security_service=security_service)
    positions = sync_positions(
        conn, client=client, account_id=account_id,
        security_service=security_service)
    final = ensure_synced(conn, account_id)
    # ensure_synced 只读取数据库快照；把本次 hydration 结果附加到返回值，
    # 使 metadata failure 不会在编排层被吞掉。
    final.metadata = account.metadata
    final.metadata_failures = account.metadata_failures
    result = {
        "ok": (account.synced and positions["synced"] and final.synced
               and not account.metadata_failures
               and not positions.get("metadata_failures")),
        "account": asdict(final),
        "position_sync": positions,
    }
    dbm.audit(conn, "RUNTIME_SYNC", entity_type="account", entity_id=account_id,
              payload={"ok": result["ok"], "sync_status": final.sync_status,
                       "position_sync": positions})
    return result


def check_current_portfolio(conn, account_state=None,
                            account_id: str = "default") -> Dict:
    """用当前账户权益和本地持仓执行组合风险检查，不生成或修改订单。"""
    state = account_state or ensure_synced(conn, account_id)
    equity = state.nav or state.cash
    failures = []
    plans = []
    if not state.synced:
        failures.append(f"account_not_synced:{state.sync_status}")
    if equity is None or equity <= 0:
        failures.append("account_equity_unknown")

    if not failures:
        for row in dbm.list_positions(conn):
            bars = dbm.get_bars(conn, row["symbol"])
            price = float(bars[-1]["close"]) if bars else 0.0
            if price <= 0:
                failures.append(f"market_price_unknown:{row['symbol']}")
                continue
            try:
                metadata = require_security_metadata(conn, row["symbol"])
            except ValueError:
                failures.append(f"unknown_security_metadata:{row['symbol']}")
                continue
            plans.append(PositionPlan(
                symbol=row["symbol"],
                target_frac=float(row["quantity"]) * price / float(equity),
                stop_price=float(row["stop_price"]),
                entry_price=float(row["entry_price"]),
                is_proposed=False,
                sector=metadata["sector"], currency=metadata["currency"],
                asset_type=metadata["asset_type"],
                beta=float(metadata["beta"]),
                leverage=float(metadata["leverage"]),
            ))

    if failures:
        result = {"passed": False, "failures": failures, "details": {},
                  "positions_checked": len(plans), "equity": equity}
    else:
        checked = check_portfolio(conn, float(equity), plans, account_state=state)
        result = {"passed": checked.passed, "failures": checked.failures,
                  "details": checked.details, "positions_checked": len(plans),
                  "equity": equity}
    dbm.audit(conn, "RISK_CHECK", entity_type="account", entity_id=account_id,
              payload=result)
    return result


def reconcile_runtime(core_conn, execution_conn, broker,
                      plan_id: Optional[str] = None) -> Dict:
    """对账一个或全部活跃计划；查询失败或不一致时保持 fail closed。"""
    dbm.assert_separate_stores(core_conn, execution_conn)
    previous = dbm.get_account(core_conn, "default")
    previous_status = previous["sync_status"] if previous is not None else "UNKNOWN"
    dbm.set_account_sync_status(core_conn, "default", "RECONCILING")
    poll_result = None
    if hasattr(broker, "poll_active_orders"):
        poll_result = broker.poll_active_orders(plan_id)
    reconciler = Reconciliation(core_conn, execution_conn, broker=broker)
    result = (reconciler.reconcile_plan(plan_id) if plan_id
              else reconciler.reconcile_all())
    current = dbm.get_account(core_conn, "default")
    poll_ok = poll_result is None or poll_result.get("ok", False)
    if (result["ok"] and poll_ok and current is not None
            and current["sync_status"] == "RECONCILING"):
        dbm.set_account_sync_status(core_conn, "default", previous_status)
    if poll_result is not None:
        result["poll"] = poll_result
        if not poll_result.get("ok", False):
            # 查询失败本身就是状态未知：保持 RECONCILING，禁止后续 LIVE。
            result["ok"] = False
            result.setdefault("errors", []).extend(poll_result.get("errors", []))
    return result
