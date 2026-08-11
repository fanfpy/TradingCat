#!/usr/bin/env python3
"""
订阅/监听管理 — P1（对齐 OpenAlice automation/rss 订阅设计）
================================================================
模式：日频报告（对齐用户盘前/盘中/盘后工作流），非会话级 WebSocket。

命令：
    tc subscribe add GLD.US --push-daily    注册订阅
    tc subscribe list                        查看订阅
    tc subscribe rm GLD.US                   取消订阅
    tc subscribe run [--symbol X]            生成订阅报告（cron 驱动入口）

存储：StateRepository.watchlist_item + monitoring_subscription，是唯一订阅真相源。
推送：TRADINGCAT_WEBHOOK_URL 环境变量存在时 POST JSON 摘要；否则只落盘报告文件。
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from shared.config import get_config

TS_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = get_config()
REPORTS_DIR = Path(_CONFIG.report.directory)


# ────────────────────────────────────────────────────────────────
# 订阅存储
# ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_subs(conn=None) -> Dict:
    """加载订阅清单 {symbol: {push_daily, created_at, last_run_at, last_status}}"""
    from shared import db as dbm
    conn = conn or dbm.get_core_conn()
    rows = conn.execute(
        "SELECT w.symbol,w.created_at,s.enabled,s.last_run_at,s.last_status "
        "FROM watchlist_item w JOIN monitoring_subscription s "
        "ON s.account_id=w.account_id AND s.symbol=w.symbol "
        "WHERE w.account_id='default' AND w.status='FOLLOWING' AND s.channel='daily'"
    ).fetchall()
    return {row["symbol"]: {
        "push_daily": bool(row["enabled"]), "created_at": row["created_at"],
        "last_run_at": row["last_run_at"], "last_status": row["last_status"],
    } for row in rows}


def add_sub(symbol: str, push_daily: bool = True, conn=None) -> Dict:
    from shared import db as dbm
    conn = conn or dbm.get_core_conn()
    subs = load_subs(conn)
    symbol = symbol.upper()
    if symbol in subs:
        subs[symbol]["push_daily"] = push_daily
        msg = f"已更新订阅: {symbol} (push_daily={push_daily})"
    else:
        subs[symbol] = {"push_daily": push_daily, "created_at": _now(),
                        "last_run_at": None, "last_status": None}
        msg = f"已添加订阅: {symbol} (push_daily={push_daily})"
    dbm.follow_security(conn, "default", symbol, "verified strategy subscription",
                        ["daily"])
    conn.execute(
        "UPDATE monitoring_subscription SET enabled=? "
        "WHERE account_id='default' AND symbol=? AND channel='daily'",
        (1 if push_daily else 0, symbol))
    conn.commit()
    subs = load_subs(conn)
    return {"message": msg, "symbol": symbol, "subs": subs}


def remove_sub(symbol: str, conn=None) -> Dict:
    from shared import db as dbm
    conn = conn or dbm.get_core_conn()
    subs = load_subs(conn)
    symbol = symbol.upper()
    if symbol in subs:
        conn.execute(
            "UPDATE monitoring_subscription SET enabled=0 "
            "WHERE account_id='default' AND symbol=? AND channel='daily'", (symbol,))
        conn.execute(
            "UPDATE watchlist_item SET status='UNFOLLOWED',updated_at=? "
            "WHERE account_id='default' AND symbol=?", (_now(), symbol))
        conn.commit()
        return {"message": f"已取消订阅: {symbol}", "symbol": symbol}
    return {"message": f"未订阅: {symbol}", "symbol": symbol, "error": "not_found"}


def list_subs(conn=None) -> Dict:
    subs = load_subs(conn)
    return {"count": len(subs), "subs": [
        {"symbol": s, **info} for s, info in sorted(subs.items())
    ]}


# ────────────────────────────────────────────────────────────────
# 报告生成 + 推送
# ────────────────────────────────────────────────────────────────

def run_subs(symbol: Optional[str] = None, conn=None) -> Dict:
    """对订阅标的跑盘后监控，生成报告文件，有 webhook 则推送摘要。

    报告落盘: reports/YYYY-MM-DD/<SYMBOL>.json + summary.json
    """
    from shared import db as dbm
    conn = conn or dbm.get_core_conn()
    subs = load_subs(conn)
    if not subs:
        return {"error": "no_subscriptions", "note": "先执行 tc subscribe add SYMBOL"}

    targets = [symbol.upper()] if symbol else list(subs.keys())
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = REPORTS_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for sym in targets:
        if sym not in subs:
            results[sym] = {"error": "not_subscribed"}
            continue
        try:
            proc = subprocess.run(
                [sys.executable, str(TS_ROOT / "production/monitor.py"),
                 "post", "--scope", "watchlist", "--symbol", sym],
                capture_output=True, text=True, timeout=120,
                cwd=str(TS_ROOT),
                env={**os.environ, "PYTHONPATH": str(TS_ROOT)},
            )
            if proc.returncode != 0:
                results[sym] = {"error": "monitor_failed",
                                "stderr": proc.stderr.strip()[-500:]}
                continue
            data = json.loads(proc.stdout)
            results[sym] = data
            # 落盘
            (out_dir / f"{sym}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2))
            # 更新订阅状态
            conn.execute(
                "UPDATE monitoring_subscription SET last_run_at=?,last_status=? "
                "WHERE account_id='default' AND symbol=? AND channel='daily'",
                (_now(), data.get("status", "unknown"), sym))
        except Exception as e:
            results[sym] = {"error": str(e)}

    conn.commit()

    # 汇总
    summary = {
        "date": date_str,
        "generated_at": _now(),
        "count": len(results),
        "results": results,
    }
    summary_file = out_dir / "summary.json"
    summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    # 推送
    pushed = _push_if_configured(summary)

    return {**summary, "report_dir": str(out_dir), "pushed": pushed}


def _push_if_configured(summary: Dict) -> Optional[str]:
    """有 webhook 配置则 POST 摘要；返回推送目标或 None。"""
    webhook = get_config().integrations.webhook_url
    if not webhook:
        return None
    try:
        payload = json.dumps(summary, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return f"webhook:{webhook} (HTTP {resp.status})"
    except Exception as e:
        return f"webhook_failed:{e}"


# ────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="订阅/监听管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="注册订阅")
    p_add.add_argument("symbol")
    # 默认开启日频推送；用 BooleanOptionalAction 让 CLI 同时支持 --push-daily 和 --no-push-daily
    p_add.add_argument("--push-daily", action=argparse.BooleanOptionalAction, default=True,
                       help="每日盘后推送报告（默认开启；--no-push-daily 关闭）")

    p_list = sub.add_parser("list", help="查看订阅")
    p_rm = sub.add_parser("rm", help="取消订阅")
    p_rm.add_argument("symbol")

    p_run = sub.add_parser("run", help="生成订阅报告（cron 驱动）")
    p_run.add_argument("--symbol", default=None, help="只跑指定标的")

    args = parser.parse_args()
    if args.cmd == "add":
        print(json.dumps(add_sub(args.symbol, args.push_daily), ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(json.dumps(list_subs(), ensure_ascii=False, indent=2))
    elif args.cmd == "rm":
        print(json.dumps(remove_sub(args.symbol), ensure_ascii=False, indent=2))
    elif args.cmd == "run":
        print(json.dumps(run_subs(args.symbol), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
