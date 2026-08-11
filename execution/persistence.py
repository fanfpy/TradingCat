"""Execution plan persistence compatibility helpers.

The legacy shared DB helper predates PAPER and validates only DRY_RUN/LIVE.
This module keeps that helper untouched while using the existing table and
immutability contract for the new local-only PAPER mode; it does not alter
the database schema.
"""

import json

from execution.models import now_utc
from shared import db as dbm


def insert_plan(conn, plan_id, account_id, execution_mode, expires_at,
                plan_hash, orders, status="PENDING"):
    if execution_mode != "PAPER":
        return dbm.insert_plan(conn, plan_id, account_id, execution_mode,
                               expires_at, plan_hash, orders, status=status)
    existing = dbm.get_plan(conn, plan_id)
    if existing is not None:
        if (existing["account_id"] != account_id
                or existing["execution_mode"] != execution_mode
                or existing["expires_at"] != expires_at
                or existing["plan_hash"] != plan_hash
                or json.loads(existing["orders_json"]) != orders):
            raise ValueError(f"plan_id {plan_id} 已绑定其他不可变内容")
        return
    conn.execute(
        "INSERT INTO trading_execution_plan "
        "(plan_id, account_id, execution_mode, expires_at, plan_hash, "
        "orders_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, account_id, execution_mode, expires_at, plan_hash,
         json.dumps(orders, ensure_ascii=False), status, now_utc()),
    )
    conn.commit()
    dbm.audit(conn, "PLAN_CREATED", entity_type="execution_plan", entity_id=plan_id,
              payload={"execution_mode": execution_mode, "plan_hash": plan_hash,
                       "orders": orders})
