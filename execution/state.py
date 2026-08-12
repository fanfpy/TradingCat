"""Execution-local order state transitions.

The shared database helper predates the execution state machine and performs
blind updates.  Keep the execution safety invariant here so every execution
caller observes terminal states monotonically.
"""

from typing import Optional

from shared import db as dbm


TERMINAL_INTENT_STATUSES = frozenset(("FILLED", "REJECTED", "CANCELLED"))


def set_intent_status(conn, intent_id: int, status: str,
                      broker_order_id: Optional[str] = None,
                      commit: bool = True) -> bool:
    """Set an intent status without allowing terminal state regression.

    Returns ``True`` when the database row changed and ``False`` when the
    requested transition was ignored because the intent is already terminal.
    UNKNOWN is deliberately not terminal: reconciliation may resolve it, but
    callers must still treat it as fail-closed until then.
    """
    row = dbm.get_intent(conn, intent_id)
    if row is None:
        raise ValueError(f"intent 不存在: {intent_id}")
    current = row["status"]
    if current in TERMINAL_INTENT_STATUSES and status != current:
        if broker_order_id and not row["broker_order_id"]:
            conn.execute(
                "UPDATE trading_order_intent SET broker_order_id=? WHERE intent_id=?",
                (broker_order_id, intent_id),
            )
            if commit:
                conn.commit()
        return False
    if current == status and not broker_order_id:
        return False
    dbm.set_intent_status(conn, intent_id, status, broker_order_id, commit=commit)
    return True
