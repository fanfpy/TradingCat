"""TradingCat v6 Phase 1 fully offline golden path."""

import hashlib
import json
import math
from dataclasses import asdict
from datetime import date, timedelta
from typing import Callable, Dict, List

from production.monitor import post_market_to_outbox
from production.notification import dispatch_signal_outbox
from research.pipeline import (
    _small_grid,
    add_candidate,
    cache_bars,
    prefilter,
    research_symbol,
)
from shared import db as dbm


SYMBOL = "V6TEST.US"


def _synthetic_bars(count: int = 800) -> List[Dict]:
    rows = []
    first_date = date.today() - timedelta(days=count - 1)
    for index in range(count):
        close = 80.0 + index * 0.3 + 10.0 * math.sin(index / 3)
        open_price = close - 0.3
        rows.append({
            "ts": str(first_date + timedelta(days=index)),
            "open": round(open_price, 4),
            "high": round(max(open_price, close) + 0.8, 4),
            "low": round(min(open_price, close) - 0.8, 4),
            "close": round(close, 4),
            "volume": int(15_000_000 + 2_000_000 * math.sin(index / 5)),
        })
    return rows


def _sha256(rows: List[Dict]) -> str:
    payload = json.dumps(
        rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    def send(self, notification) -> bool:
        self.sent.append(notification)
        return True


def run_offline_golden_path(core_conn=None) -> Dict:
    conn = core_conn or dbm.get_core_conn(":memory:")
    stages = []

    def stage(name: str, action: Callable[[], Dict]):
        try:
            data = action()
            stages.append({"stage": name, "status": "ok", "data": data})
            return data
        except Exception as exc:
            stages.append({
                "stage": name,
                "status": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "retryable": bool(getattr(exc, "retryable", False)),
            })
            return None

    candidate = stage("candidate", lambda: {
        "message": add_candidate(conn, SYMBOL), "symbol": SYMBOL})
    if candidate is None:
        return {"ok": False, "offline": True, "stages": stages}

    rows = _synthetic_bars()
    cached = stage("cache_bars", lambda: {
        "bar_count": cache_bars(
            conn, SYMBOL, rows, "synthetic", _sha256(rows), rows[-1]["ts"]),
        "source": "synthetic",
    })
    if cached is None:
        return {"ok": False, "offline": True, "stages": stages}

    def run_prefilter():
        result = prefilter(conn, SYMBOL, rows)
        if not result["passed"]:
            raise RuntimeError(f"prefilter failed: {result['reasons']}")
        return result

    filtered = stage("prefilter", run_prefilter)
    if filtered is None:
        return {"ok": False, "offline": True, "stages": stages}

    def run_research():
        result = research_symbol(conn, SYMBOL, grid=_small_grid())
        if result.get("error"):
            raise RuntimeError(result["error"])
        return {"symbol": result["symbol"], "status": result["status"],
                "score": result["score"], "grid": "small"}

    researched = stage("research", run_research)
    if researched is None:
        return {"ok": False, "offline": True, "stages": stages}

    lifecycle = dbm.get_lifecycle(conn, SYMBOL)
    params = json.loads(lifecycle["params_json"])
    monitor_result = {}

    def run_monitor_post():
        result = post_market_to_outbox(
            conn, SYMBOL, params, rows[-1]["ts"], account_id="offline",
            channels=["offline"])
        monitor_result["signal"] = result["signal"]
        return asdict(result["report"])

    monitored = stage("monitor_post", run_monitor_post)
    if monitored is None:
        return {"ok": False, "offline": True, "stages": stages}

    adapter = _RecordingAdapter()

    def run_notification():
        dispatched = dispatch_signal_outbox(conn, adapter)
        if dispatched != {"processed": 1, "sent": 1, "failed": 0}:
            raise RuntimeError(f"outbox dispatch failed: {dispatched}")
        return {"created": monitor_result["signal"]["created"], **dispatched}

    notified = stage("notification_outbox", run_notification)
    if notified is None:
        return {"ok": False, "offline": True, "stages": stages}

    def check_isolation():
        plans = dbm.list_plans(conn)
        return {
            "execution_plan_count": len(plans),
            "live_plan_count": sum(
                1 for plan in plans if plan["execution_mode"] == "LIVE"),
        }

    isolation = stage("runtime_isolation", check_isolation)
    if isolation is None:
        return {"ok": False, "offline": True, "stages": stages}
    if isolation and isolation["execution_plan_count"]:
        stages[-1].update({"status": "error", "error_type": "IsolationError",
                           "error_message": "golden path created an execution plan",
                           "retryable": False})
        return {"ok": False, "offline": True, "stages": stages}
    return {"ok": True, "offline": True, "stages": stages}


def main() -> int:
    result = run_offline_golden_path()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())