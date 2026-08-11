#!/usr/bin/env python3
"""批量跑候选池研究（full 1620 组 / adx 8100 组）。

用法（后台）：
    nohup env PYTHONPATH=. python3 research/batch_research_candidates.py --grid adx > /tmp/research_adx_A.log 2>&1 &
    nohup env PYTHONPATH=. python3 research/batch_research_candidates.py --grid adx --symbols 'GLD.US,TQQQ.US' &

默认范围：lifecycle 状态 in (candidate, degraded, removed) 且 data_manifest bar_count>=504
（跳过 verified，跳过无缓存）。--symbols 指定批次时只跑清单内且在默认范围内的标的。
每个标的 research 后打印一行 JSON 摘要；末尾汇总 verified/degraded/removed/failed 计数。
"""
import argparse
import json
import sys
import time

sys.path.insert(0, '.')
from shared import db as dbm
from shared.backtest import PARAM_GRID, PARAM_GRID_ADX
from research import pipeline

MIN_BARS = pipeline.MIN_BARS  # 504
ELIGIBLE = ("candidate", "degraded", "removed")


def default_todo(conn):
    """默认范围：非 verified 有缓存标的（bar_count>=504），字母序。"""
    cached = dbm.list_manifest_counts(conn)
    statuses = {r["symbol"]: r["status"] for r in dbm.list_lifecycle(conn)}
    return [s for s in sorted(statuses)
            if statuses[s] in ELIGIBLE and cached.get(s, 0) >= MIN_BARS]


def main():
    parser = argparse.ArgumentParser(description="批量跑候选池研究")
    parser.add_argument("--grid", choices=["full", "adx"], default="full",
                        help="full=1620组原网格; adx=8100组(PARAM_GRID_ADX)")
    parser.add_argument("--symbols", default="",
                        help="逗号分隔标的清单（缺省=全部非verified有缓存标的）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 grid 大小与批次清单，不跑研究")
    args = parser.parse_args()

    conn = dbm.get_core_conn()
    grid = None if args.grid == "full" else PARAM_GRID_ADX
    grid_size = len(PARAM_GRID) if args.grid == "full" else len(PARAM_GRID_ADX)
    print(f"grid={args.grid} size={grid_size}", flush=True)

    base = default_todo(conn)
    if args.symbols:
        wanted = [s.strip() for s in args.symbols.split(",") if s.strip()]
        todo = [s for s in wanted if s in base]
        skipped = [s for s in wanted if s not in base]
        if skipped:
            print(f"跳过（不在默认范围: verified/无缓存/bar<504）: {skipped}", flush=True)
    else:
        todo = base
    print(f"批次 {len(todo)} 个: {todo}", flush=True)

    if args.dry_run:
        print("dry-run 完成，不跑研究", flush=True)
        return

    results = {"verified": [], "degraded": [], "removed": [], "failed": []}
    for i, sym in enumerate(todo, 1):
        row = dbm.get_lifecycle(conn, sym)
        old_status = row["status"] if row else "?"
        try:
            bars = [dict(b) for b in dbm.get_bars(conn, sym)]
            pf = pipeline.prefilter(conn, sym, bars)
            if not pf.get("passed", False):
                reasons = pf.get("reasons", pf.get("fail_reasons", []))
                if isinstance(reasons, list):
                    reason = "prefilter: " + ",".join(reasons)
                else:
                    reason = "prefilter: " + str(reasons)
                results["removed"].append((sym, reason))
                print(json.dumps({"i": i, "total": len(todo), "symbol": sym,
                                  "old_status": old_status, "new_status": "removed",
                                  "score": None, "reason": reason}, ensure_ascii=False),
                      flush=True)
                continue
            res = pipeline.research_symbol(conn, sym, grid=grid)
            status = res.get("status", "?")
            score = res.get("score")
            key = status if status in results else "failed"
            results[key].append((sym, score))
            print(json.dumps({"i": i, "total": len(todo), "symbol": sym,
                              "old_status": old_status, "new_status": status,
                              "score": score, "reason": res.get("reasons", [])},
                             ensure_ascii=False), flush=True)
        except Exception as e:
            results["failed"].append({
                "symbol": sym,
                "error_type": type(e).__name__,
                "error_message": str(e)[:300],
                "retryable": bool(getattr(e, "retryable", False)),
            })
            print(json.dumps({"i": i, "total": len(todo), "symbol": sym,
                              "old_status": old_status, "new_status": "failed",
                              "score": None, "reason": str(e)[:120]}, ensure_ascii=False),
                  flush=True)
        time.sleep(0.3)

    print("===== 汇总 =====", flush=True)
    for k, v in results.items():
        print(f"{k} ({len(v)}): {v}", flush=True)


if __name__ == "__main__":
    main()
