import json
from datetime import date, timedelta

from production.decision import (
    collect_signals, load_execution_plan, run_decision, target_to_execution_plan,
)
from production.position import PositionIntent
from production.target_portfolio import TargetPortfolio
from shared import db as dbm
from shared.account import AccountState
from tc import _run_trade_plan


def _bars(n=120):
    start = date.today() - timedelta(days=n - 1)
    rows = []
    for i in range(n):
        close = 100.0 + i
        rows.append({"ts": (start + timedelta(days=i)).isoformat(),
                     "open": close - 0.2, "high": close + 1.0,
                     "low": close - 1.0, "close": close, "volume": 20_000_000})
    return rows


def _ready_conn(with_oos=True):
    conn = dbm.get_conn(":memory:")
    rows = _bars()
    dbm.upsert_bars(conn, "A.US", rows, "test")
    dbm.set_manifest(conn, "A.US", {
        "source": "test", "fetched_at": dbm._now(),
        "last_completed": rows[-1]["ts"], "date_start": rows[0]["ts"],
        "date_end": rows[-1]["ts"], "bar_count": len(rows), "sha256": "test",
    })
    params = {"entry_mode": "momentum", "ma_period": 50,
              "atr_multiple": 3.0, "buffer": 0.0}
    dbm.set_lifecycle(conn, "A.US", "verified", params_json=json.dumps(params))
    dbm.save_strategy_version(
        conn, "A.US", "verified", params_json=json.dumps(params),
        oos_stats_json=json.dumps({"n": 24, "wins": 15, "losses": 9,
                                   "avg_win": 3.0, "avg_loss": 1.5,
                                   "p": 0.625, "b": 2.0,
                                   "positive_folds": 4, "total_folds": 4,
                                   "final_test_accepted": True}) if with_oos else None,
    )
    dbm.upsert_account(conn, "default", "SYNCED", cash=100_000,
                       buying_power=100_000, nav=100_000)
    return conn


def test_decision_uses_real_oos_and_builds_persisted_plan():
    conn = _ready_conn()
    account = AccountState(sync_status="SYNCED", cash=100_000,
                           buying_power=100_000, nav=100_000)
    signals = collect_signals(conn, account, as_of_date=date.today().isoformat())
    assert len(signals) == 1 and signals[0]["oos_stats"]["n"] == 24
    tp = run_decision(conn, 100_000, account, as_of_date=date.today().isoformat())
    assert tp.passed and tp.final_fracs["A.US"] > 0
    plan = target_to_execution_plan(conn, tp, 100_000, account_state=account)
    assert plan is not None and plan.orders[0].side == "BUY"
    assert load_execution_plan(conn, plan.plan_id).plan_hash == plan.plan_hash


def test_missing_oos_never_uses_fabricated_fallback():
    conn = _ready_conn(with_oos=False)
    account = AccountState(sync_status="SYNCED", buying_power=100_000, nav=100_000)
    assert collect_signals(conn, account, as_of_date=date.today().isoformat()) == []


def test_execution_plan_is_position_and_pending_order_delta():
    conn = _ready_conn()
    price = float(dbm.get_bars(conn, "A.US")[-1]["close"])
    account = AccountState(sync_status="SYNCED", buying_power=100_000, nav=100_000,
                           positions=[{"symbol": "A.US", "quantity": 10,
                                       "last_price": price, "cost_price": price}])
    dbm.insert_intent(conn, "pending", "old-plan", "1", "A.US", "BUY", 5)
    conn.commit()
    intent = PositionIntent("A.US", "BUY", 0.05, entry_price=price,
                            evidence={"strategy_version_id": 1})
    tp = TargetPortfolio(intents=[intent], final_fracs={"A.US": 0.05}, passed=True)
    plan = target_to_execution_plan(conn, tp, 100_000, account_state=account)
    expected = int(0.05 * 100_000 // price) - 10 - 5
    assert plan.orders[0].quantity == expected


def test_trade_plan_confirms_the_same_persisted_plan():
    conn = _ready_conn()
    price = float(dbm.get_bars(conn, "A.US")[-1]["close"])
    account = AccountState(sync_status="SYNCED", buying_power=100_000, nav=100_000)
    intent = PositionIntent("A.US", "BUY", 0.02, entry_price=price)
    tp = TargetPortfolio(intents=[intent], final_fracs={"A.US": 0.02}, passed=True)
    plan = target_to_execution_plan(conn, tp, 100_000, account_state=account)
    rc = _run_trade_plan(conn, plan.plan_id, confirm_input=lambda: True,
                         quote_provider=lambda _c, _s: (price, dbm._now()))
    assert rc == 0
    intents = dbm.list_intents(conn, plan.plan_id)
    assert len(intents) == 1 and intents[0]["plan_id"] == plan.plan_id
