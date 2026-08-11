#!/usr/bin/env python3
"""批量拉取长桥自选 candidate 数据缓存（后台任务用）。"""
import sys, time, json
sys.path.insert(0, '.')
from shared import db as dbm
from research import pipeline

conn = dbm.get_core_conn()
# 走 StateRepository 封装（D-13）：禁止在业务层直接写裸 SQL。
cands = [r["symbol"] for r in dbm.list_lifecycle(conn, "candidate")]
cached = set(dbm.list_manifest_symbols(conn))
todo = [c for c in cands if c not in cached]

print(f'待拉取: {len(todo)} 个', flush=True)
ok, fail, skipped = [], [], []
for i, sym in enumerate(todo, 1):
    try:
        rc = pipeline.cache_symbol(conn, sym, count=2000)
        if rc:
            ok.append(sym)
            print(f'[{i}/{len(todo)}] {sym} ✅', flush=True)
        else:
            skipped.append(sym)
            print(f'[{i}/{len(todo)}] {sym} ⏭️ 跳过（无数据/已最新）', flush=True)
    except Exception as e:
        fail.append({"symbol": sym, "error_type": type(e).__name__,
                     "error_message": str(e)[:300],
                     "retryable": bool(getattr(e, "retryable", False))})
        print(f'[{i}/{len(todo)}] {sym} ❌ {str(e)[:80]}', flush=True)
    time.sleep(0.3)

print(json.dumps({'ok': ok, 'fail': fail, 'skipped': skipped}, ensure_ascii=False, indent=1))