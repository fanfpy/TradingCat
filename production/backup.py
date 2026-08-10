#!/usr/bin/env python3
"""
在线备份 CLI — 交易系统 v4.0（US-003）
======================================
盘后每日快照 + 每周全量归档。WAL 模式下的一致性在线备份（sqlite3 Backup API），
禁止直接 cp trading.db（WAL/shm 未 checkpoint 会丢数据，架构 D-1 / 6.3 备份纪律）。

用法：
    PYTHONPATH=. python3 production/backup.py --daily          # 每日盘后快照，保留 7 份
    PYTHONPATH=. python3 production/backup.py --weekly         # 每周归档，保留 8 份
    PYTHONPATH=. python3 production/backup.py --daily --dest /tmp/foo.db  # 覆盖输出路径

输出：JSON（backup 文件路径 + 清理的旧备份），cron/脚本可解析。
幂等：重复执行同一天/周只会覆盖同一备份文件，不报错。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# 让 shared 可导入：trading-system/shared（db / longbridge_client 等，自包含目录，架构 §6.1）
_TRADING_ROOT = str(Path(__file__).resolve().parents[1])
if _TRADING_ROOT not in sys.path:
    sys.path.insert(0, _TRADING_ROOT)

from shared import db as dbm

# 可迁移运行目录；缺省保持仓库内旧路径，安装部署可用环境变量覆盖。
DATA_BACKUPS = Path(os.environ.get(
    "TRADINGCAT_BACKUP_DIR",
    str(Path(__file__).resolve().parents[1] / "data" / "backups"),
)).expanduser()
DAILY_KEEP = 7    # 每日快照保留份数
WEEKLY_KEEP = 8   # 每周归档保留份数


def prune(backup_dir: Path, keep: int) -> List[str]:
    """删除目录中最旧的备份，保留最近 keep 份。返回被删除的文件路径列表。

    幂等：目录为空 / 不足 keep 份时不做任何事。
    """
    files = sorted((p for p in backup_dir.glob("*.db") if p.is_file()),
                   key=lambda p: p.name)
    removed: List[str] = []
    for old in files[:-keep] if len(files) > keep else []:
        old.unlink()
        removed.append(str(old))
    return removed


def run_daily(dest: Optional[str] = None, db_path: Optional[str] = None) -> dict:
    """每日盘后快照 → data/backups/daily/YYYY-MM-DD.db，保留最近 DAILY_KEEP 份。"""
    daily_dir = DATA_BACKUPS / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    if dest is None:
        name = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".db"
        dest = str(daily_dir / name)
    path = dbm.backup(dest, db_path=db_path or dbm.DB_PATH)
    removed = prune(daily_dir, DAILY_KEEP)
    return {"kind": "daily", "path": path, "retained": DAILY_KEEP, "removed": removed}


def run_weekly(dest: Optional[str] = None, db_path: Optional[str] = None) -> dict:
    """每周全量归档 → data/backups/weekly/YYYY-Www.db（ISO 周），保留最近 WEEKLY_KEEP 份。"""
    weekly_dir = DATA_BACKUPS / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    if dest is None:
        # ISO 年-周：%G-%V（如 2026-W32），与任务规范 YYYY-Www 对齐
        name = datetime.now(timezone.utc).strftime("%G-W%V") + ".db"
        dest = str(weekly_dir / name)
    path = dbm.backup(dest, db_path=db_path or dbm.DB_PATH)
    removed = prune(weekly_dir, WEEKLY_KEEP)
    return {"kind": "weekly", "path": path, "retained": WEEKLY_KEEP, "removed": removed}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="交易系统在线备份 CLI（WAL 安全，Backup API，禁止直接 cp）",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daily", action="store_true",
                      help="每日盘后快照（data/backups/daily/YYYY-MM-DD.db，保留最近 7 份）")
    mode.add_argument("--weekly", action="store_true",
                      help="每周全量归档（data/backups/weekly/YYYY-Www.db，保留最近 8 份）")
    parser.add_argument("--dest", default=None,
                        help="覆盖输出路径（默认按模式写到 data/backups/ 下）")
    args = parser.parse_args()

    if args.daily:
        result = run_daily(args.dest)
    else:
        result = run_weekly(args.dest)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
