#!/usr/bin/env python3
"""
研究流水线主入口 — 交易系统 v3.0
=================================
spec §2.1-2.10 的编排层。

流程：
    add_candidate(symbol) 或 scan → 候选池
    prefilter(symbol)          → 历史/流动性/新鲜度/OHLC 合法性/重复日期
    cache_bars(symbol, rows)   → 固定日线缓存 + 溯源 manifest
    research(symbol)           → 全网格（1620 组，含 exit_mode）回测 → 训练选参 → 四折 WF → 评分 → 生命周期

用法：
    python research/pipeline.py add AAPL.US
    python research/pipeline.py prefilter AAPL.US
    python research/pipeline.py cache AAPL.US        # 从长桥拉取日线(600根)写入缓存
    python research/pipeline.py research AAPL.US --grid small
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

# 让 shared 可导入：trading-system/shared（db / longbridge_client 等，自包含目录，架构 §6.1）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db as dbm
from shared.backtest import PARAM_GRID, PARAM_GRID_ADX, run_backtest
from shared.market_calendar import completed_bar_freshness, market_for_symbol
from shared.cost_model import estimate_cost
from shared.config import get_config
from research.walk_forward import aggregate_oos_trades, run_walk_forward, trade_statistics
from research.score import decide_lifecycle
from research.robustness import (
    MIN_VERIFIED_OOS_TRADES,
    fold_parameter_stability,
    multiple_testing_diagnostic,
    parameter_neighborhood_diagnostic,
)

# 预筛参数（spec §2.1）
_CONFIG = get_config()
MIN_BARS = _CONFIG.research.prefilter_min_bars
MIN_RESEARCH_BARS = _CONFIG.research.min_bars
MIN_MEDIAN_DOLLAR_VOLUME = 10_000_000
MAX_STALENESS_DAYS = 5  # 允许的数据新鲜度（日历日，可配置）
FINAL_HOLDOUT_FRAC = 0.10
FINAL_HOLDOUT_MIN_BARS = 126
FINAL_HOLDOUT_MIN_TRADES = 3

# data_manifest 完整性所需字段（缓存优先/幂等判断用）
MANIFEST_FIELDS = ("source", "fetched_at", "last_completed",
                   "date_start", "date_end", "bar_count", "sha256")


# ────────────────────────────────────────────────────────────────
# 候选池
# ────────────────────────────────────────────────────────────────

def add_candidate(conn, symbol: str, security_service=None) -> str:
    """加入候选池（candidate 状态）。"""
    existing = dbm.get_lifecycle(conn, symbol)
    if security_service is not None:
        result = security_service.ensure_batch([symbol])[0]
        if not result["ok"]:
            dbm.audit(conn, "SECURITY_METADATA_FAILED", "symbol", symbol,
                      result)
    if existing is None:
        dbm.set_lifecycle(conn, symbol, "candidate")
        return f"已加入候选池: {symbol}"
    return f"已存在（状态 {existing['status']}）: {symbol}"


def list_candidates(conn) -> List[str]:
    return [r["symbol"] for r in dbm.list_lifecycle(conn, "candidate")]


# ────────────────────────────────────────────────────────────────
# 长桥自选 ⇄ 系统候选池 同步
# ────────────────────────────────────────────────────────────────

def _reserve_longbridge(conn, scope: str, amount: int = 1) -> None:
    from shared.longbridge_client import RateLimitError
    limit = _CONFIG.quota.longbridge_daily
    reservation = dbm.reserve_api_quota(
        conn, f"longbridge:{scope}", amount=amount, quota_limit=limit)
    if not reservation["allowed"]:
        raise RateLimitError(
            f"Longbridge API quota exceeded: {scope} "
            f"{reservation['used']}/{reservation['limit']}")


def fetch_longbridge_watchlist(client=None, conn=None) -> List[str]:
    """通过长桥 Python SDK 拉取全部分组的自选标的并保序去重。"""
    if conn is not None:
        _reserve_longbridge(conn, "watchlist")
    if client is None:
        from shared.longbridge_client import LongbridgeClient
        client = LongbridgeClient(scope="quote")
    symbols = client.watchlist(strict=True)
    seen = set()
    return [s for s in symbols if not (s in seen or seen.add(s))]


def sync_watchlist(conn, client=None) -> Dict:
    """同步：长桥自选 → 系统候选池。

    规则（docs/architecture.md 的关注、策略资格和授权分离原则）：
      1. 长桥有、系统从未收录    → 加入候选池（candidate）
      2. 长桥有、系统已 removed  → 不重跑，但输出"被拒原因"报告
      3. 长桥有、系统已 degraded → 已测但未通过，保持现状，不自动重跑
      4. 系统有、长桥已移除      → 不自动删除（系统池是验证状态，非长桥镜像）
    返回差异报告 {added, skipped_removed, existing_active, not_in_lb, groups}.
    """
    try:
        if client is None:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="quote")
        lb_symbols = fetch_longbridge_watchlist(client=client, conn=conn)
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "retryable": bool(getattr(exc, "retryable", False)),
        }
    if not lb_symbols:
        return {"ok": True, "status": "NO_DATA", "added": [],
                "note": "长桥自选为空，未同步"}

    snapshot_id = dbm.snapshot_universe(
        conn, "watchlist", lb_symbols, as_of_date=dbm._now(),
        metadata={"provider": "longbridge", "count": len(lb_symbols)})

    lb_set = set(lb_symbols)
    all_rows = {r["symbol"]: r for r in dbm.list_lifecycle(conn)}
    sys_set = set(all_rows)

    added = []          # 新进候选池
    skipped_removed = []  # 长桥关注但系统已 removed（不重跑，附原因）
    existing_verified = []  # 已通过验证的标的
    degraded_kept = []    # 已 degraded，保持
    not_in_lb = []        # 系统有但长桥已移除（不删除，仅报告）
    skipped_other = []    # 其他状态（backtesting/candidate），保持

    from shared.security import SecurityService
    metadata_results = SecurityService(conn, client).ensure_batch(
        symbol for symbol in lb_symbols if symbol not in sys_set)

    for s in lb_symbols:
        if s not in sys_set:
            dbm.set_lifecycle(conn, s, "candidate")
            added.append(s)
        else:
            st = all_rows[s]["status"]
            if st == "verified":
                existing_verified.append(s)
            elif st == "removed":
                skipped_removed.append(s)
            elif st == "degraded":
                degraded_kept.append(s)
            else:
                skipped_other.append(s)

    for s in sorted(sys_set - lb_set):
        not_in_lb.append(s)

    # removed 标的的失败原因（从 last_evidence_hash 里解析不出来就标记 unknown）
    removed_reasons = {}
    for s in skipped_removed:
        removed_reasons[s] = _removed_reason(all_rows[s])

    return {
        "ok": True,
        "lb_total": len(lb_symbols),
        "sys_total": len(sys_set),
        "added_candidate": sorted(added),
        "existing_verified": sorted(existing_verified),
        "degraded_kept": sorted(degraded_kept),
        "skipped_removed": sorted(skipped_removed),
        "skipped_removed_reasons": removed_reasons,
        "not_in_lb_kept": sorted(not_in_lb),
        "universe_snapshot_id": snapshot_id,
        "metadata": metadata_results,
        "note": "added 已进候选池；removed 不重跑（见 reasons）；系统池不因长桥移除而删除",
    }


def _removed_reason(row) -> str:
    """从 lifecycle 记录的 params_json/evidence 里提取失败原因（尽力而为）。"""
    try:
        pj = json.loads(row["params_json"] or "{}")
        ev = pj.get("evidence", {})
        folds = ev.get("folds", [])
        # 汇总所有折的 fail_reasons
        reasons = set()
        for f in folds:
            for r in f.get("fail_reasons", []):
                reasons.add(r)
        if reasons:
            return ",".join(sorted(reasons))
        if ev.get("passed_folds", 0) < 3:
            return f"passed_folds={ev.get('passed_folds')}/4"
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return "unknown"
    return "unknown"


# ────────────────────────────────────────────────────────────────
# 预筛
# ────────────────────────────────────────────────────────────────

def prefilter(conn, symbol: str, bars: List[Dict]) -> Dict:
    """预筛检查，返回 {passed, reasons, metrics}。

    bars: [{ts, open, high, low, close, volume}, ...]（升序）
    """
    reasons: List[str] = []
    metrics: Dict = {}

    n = len(bars)
    metrics["bar_count"] = n
    if n < MIN_BARS:
        reasons.append(f"bar_count<{MIN_BARS}")

    # 20 日中位成交额
    if n >= 20:
        vol_20 = sorted(b["volume"] * b["close"] for b in bars[-20:])
        med = vol_20[len(vol_20) // 2]
        metrics["median_dollar_vol_20d"] = med
        if med < MIN_MEDIAN_DOLLAR_VOLUME:
            reasons.append("median_dollar_vol<10M")

    # OHLC 合法性 + 重复日期
    # 容差：长桥前复权在除权除息边界有系统性舍入噪声（实测 2020-11-18 /
    # 2021-05-05 / 2023-06-05 等日期出现 high<max(o,c) 或 low>min(o,c)，
    # 且 2020-11-18 当日 open 与其余 OHLC 复权基准不一致），52/105 标的各有
    # 1-3 根受影响（2000 根里 <0.15%）。零容忍会误杀一半候选标的；
    # 按异常根数比例容忍：≤0.2%（2000 根 ≤4 根）视为噪声放行，
    # 超阈值才判 invalid_ohlc。数据源侧问题记录在案（2026-08-06）。
    MAX_OHLC_BAD_RATIO = 0.002
    dup = len(bars) != len({b["ts"] for b in bars})
    if dup:
        reasons.append("duplicate_dates")
    bad_ohlc = 0
    for b in bars:
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        if h < max(o, c) or l > min(o, c) or min(o, h, l, c) <= 0:
            bad_ohlc += 1
    if bad_ohlc > max(1, int(MAX_OHLC_BAD_RATIO * n)):
        reasons.append("invalid_ohlc")

    # 数据新鲜度（按交易所交易日历；真实源缺日历时 fail closed）
    if bars:
        from datetime import datetime, timezone
        last = bars[-1]["ts"]
        try:
            as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # bars 查询刻意不携带 source；真实来源以 manifest 为准。此前从
            # sqlite Row 读不存在的 source 会误走测试回退，令无交易日历的
            # 生产数据错误通过新鲜度检查。
            manifest = dbm.get_manifest(conn, symbol)
            source = str(manifest["source"] or "") if manifest is not None else ""
            fresh, freshness_reason = completed_bar_freshness(
                conn, symbol, last, as_of, source=source)
            metrics["freshness"] = freshness_reason
            if not fresh:
                reasons.append("stale_completed_bar")
        except ValueError:
            reasons.append("bad_ts_format")

    for reason in reasons:
        dbm.record_data_quality(
            conn, dataset="bars", severity="ERROR", rule_name=reason,
            details=metrics, symbol=symbol)
    return {"passed": len(reasons) == 0, "reasons": reasons, "metrics": metrics}


# ────────────────────────────────────────────────────────────────
# 数据缓存 + 溯源
# ────────────────────────────────────────────────────────────────

def cache_bars(conn, symbol: str, rows: List[Dict], source: str,
               sha256: str, last_completed: str) -> int:
    """写入日线缓存 + 更新 manifest。"""
    cnt = dbm.upsert_bars(conn, symbol, rows, source)
    dates = [r["ts"] for r in rows]
    simulated = source.lower() in {"test", "e2e", "synthetic", "simulation"}
    dbm.set_manifest(conn, symbol, {
        "source": source,
        "fetched_at": dbm._now(),
        "last_completed": last_completed,
        "date_start": dates[0] if dates else "",
        "date_end": dates[-1] if dates else "",
        "bar_count": len(rows),
        "sha256": sha256,
        # 前复权只说明供应商已调整价格，不等价于本地公司行为台账已逐条同步。
        "adjustment_mode": "TEST" if simulated else "FORWARD",
        "corporate_actions_status": "TEST" if simulated else "PROVIDER_ADJUSTED",
    })
    return cnt


def _normalize_ts(value) -> Optional[str]:
    """长桥 timestamp → 交易所本地日 YYYY-MM-DD。

    长桥日线 timestamp 是 UTC 正午（datetime 对象或 'YYYY-MM-DD HH:MM:SS' 字符串），
    日期部分对 US/HK/CN 主要交易所即交易所本地日，直接截取日期即可，无需时区换算。
    兼容数字 Unix 时间戳（秒/毫秒）→ UTC 日期（防御 SDK 返回格式变化 / fake 注入）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        try:
            ts = value / 1000.0 if value > 1e12 else float(value)
            # 合理范围过滤（2000-01-01 ~ 2100-01-01），避免离谱时间戳产生假日期
            if not (946684800 <= ts <= 4102444800):
                return None
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value).strip())
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _normalize_rows(klines: List[Dict]) -> List[Dict]:
    """长桥 K 线 → db rows：ts 归一化、去重（同日期取最后一条）、按日期升序。

    过滤：timestamp 无法解析或缺少 OHLCV 数值的行（非交易日噪音）。
    """
    by_date: Dict[str, Dict] = {}
    for k in klines:
        ts = _normalize_ts(k.get("timestamp"))
        if not ts:
            continue
        try:
            row = {
                "ts": ts,
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": int(k.get("volume") or 0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        by_date[ts] = row
    return [by_date[d] for d in sorted(by_date)]


def _rows_sha256(rows: List[Dict]) -> str:
    """对规范化 rows 的确定性 JSON 做 SHA-256（同一数据重跑 hash 相同）。"""
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _manifest_complete(manifest) -> bool:
    """data_manifest 是否完整：必需字段齐全且足以划分开发区与 Holdout。

    用于缓存优先/幂等：完整缓存的标的重复 cache 不再调长桥。
    """
    if manifest is None:
        return False
    for f in MANIFEST_FIELDS:
        if manifest[f] in (None, ""):
            return False
    try:
        return int(manifest["bar_count"]) >= MIN_RESEARCH_BARS
    except (TypeError, ValueError):
        return False


def _sync_market_calendar(conn, client, symbol: str, last_completed: str) -> int:
    """同步 freshness gate 所需的最近交易日历。"""
    from datetime import timedelta

    market = market_for_symbol(symbol)
    start = date.fromisoformat(last_completed) - timedelta(days=10)
    end = datetime.now(timezone.utc).date()
    _reserve_longbridge(conn, "calendar")
    rows = client.trading_calendar(market, start, end)
    count = dbm.upsert_calendar(conn, market, rows, source="longbridge")
    dbm.audit(conn, "DATAHUB_CALENDAR", entity_type="market", entity_id=market,
              payload={"start": str(start), "end": str(end), "rows": count})
    return count


def incremental_update(conn, symbol: str, manifest, count: int = 60,
                       client=None) -> Dict:
    """增量缓存（架构 P1 DataHub）：manifest 完整时只拉最近 N 根并追加新 bar。

    - 以 manifest.last_completed 为断点（DataHub 增量断点，见 shared/db.get_last_bar）
    - 无新 bar → 原样返回缓存（幂等，不产生重复行）
    - 有新 bar → upsert 追加 + 更新 manifest（date_end/bar_count/sha256/last_completed）
    """
    last_ts = manifest["last_completed"]
    _reserve_longbridge(conn, "kline")
    if client is None:
        from shared.longbridge_client import LongbridgeClient
        client = LongbridgeClient(scope="quote")
    klines = client.kline_by_count(symbol, count=count, period="day")
    _sync_market_calendar(conn, client, symbol, last_ts)
    if not klines:
        return {
            "symbol": symbol,
            "bar_count": manifest["bar_count"],
            "date_start": manifest["date_start"],
            "date_end": manifest["date_end"],
            "sha256": manifest["sha256"],
            "source": manifest["source"],
            "cached": True,
        }
    rows = _normalize_rows(klines)
    fresh = [r for r in rows if r["ts"] > last_ts]
    if not fresh:
        return {
            "symbol": symbol,
            "bar_count": manifest["bar_count"],
            "date_start": manifest["date_start"],
            "date_end": manifest["date_end"],
            "sha256": manifest["sha256"],
            "source": manifest["source"],
            "cached": True,
        }

    # 追加新 bar（INSERT OR REPLACE 幂等）
    dbm.upsert_bars(conn, symbol, fresh, source="longbridge")
    # manifest hash 是数据版本身份，增量后必须覆盖完整数据集重新计算。
    all_rows = [dict(row) for row in dbm.get_bars(conn, symbol)]
    new_count = len(all_rows)
    new_sha = _rows_sha256(all_rows)
    dbm.update_manifest_increment(
        conn, symbol, fresh[-1]["ts"], fresh[-1]["ts"],
        new_count, dbm._now(), new_sha)
    dbm.audit(conn, "DATAHUB_INCREMENT", entity_type="symbol", entity_id=symbol,
              payload={"added_bars": len(fresh), "from": last_ts, "to": fresh[-1]["ts"]})
    return {
        "symbol": symbol,
        "bar_count": new_count,
        "date_start": manifest["date_start"],
        "date_end": fresh[-1]["ts"],
        "sha256": new_sha,
        "source": manifest["source"],
        "cached": True,
        "incremental": {"added_bars": len(fresh), "from": last_ts, "to": fresh[-1]["ts"]},
    }


def cache_symbol(conn, symbol: str, count: int = 800, client=None) -> Dict:
    """从长桥拉取日线写入缓存（bars + data_manifest），返回结果摘要。

    缓存优先（幂等）：data_manifest 已存在且完整（bar_count>=630）时直接返回
    缓存信息，不再调长桥（不产生重复行）。
    拉取失败/返回空/数据不足时抛 RuntimeError，由 CLI 层转成 stderr + 非零退出码。
    """
    if client is None:
        from shared.longbridge_client import LongbridgeClient
        client = LongbridgeClient(scope="quote")
    from shared.security import SecurityService
    metadata_result = SecurityService(conn, client).ensure_batch([symbol])[0]
    if not metadata_result["ok"]:
        dbm.audit(conn, "SECURITY_METADATA_FAILED", "symbol", symbol,
                  metadata_result)

    manifest = dbm.get_manifest(conn, symbol)
    if _manifest_complete(manifest):
        # 缓存完整 → 增量更新（P1 DataHub：只拉新 bar，不全量重拉）
        result = incremental_update(conn, symbol, manifest, count=60, client=client)
        result["metadata"] = metadata_result
        return result

    _reserve_longbridge(conn, "kline")
    klines = client.kline_by_count(symbol, count=count, period="day")

    if not klines:
        raise RuntimeError(f"长桥返回 {symbol} 0 根K线（行情权限或网络问题）")

    rows = _normalize_rows(klines)
    if len(rows) < MIN_RESEARCH_BARS:
        raise RuntimeError(
            f"{symbol} 日线不足研究所需 {MIN_RESEARCH_BARS} 根（实际 {len(rows)}），未写入缓存")

    _sync_market_calendar(conn, client, symbol, rows[-1]["ts"])

    sha = _rows_sha256(rows)
    cache_bars(conn, symbol, rows, source="longbridge",
               sha256=sha, last_completed=rows[-1]["ts"])
    return {
        "symbol": symbol,
        "bar_count": len(rows),
        "date_start": rows[0]["ts"],
        "date_end": rows[-1]["ts"],
        "sha256": sha,
        "source": "longbridge",
        "metadata": metadata_result,
    }


# ────────────────────────────────────────────────────────────────
# 研究主流程
# ────────────────────────────────────────────────────────────────

def _production_params_from_training(wf) -> Dict:
    """生产候选参数只取最后一折训练阶段的选择，绝不按 OOS 表现排序。

    最后一折代表时间上最新、信息最多的开发期训练窗口。它的 OOS 结果只参与
    Walk-Forward 稳健性验收，不参与候选参数选择。
    """
    if not wf.folds:
        return {"entry_mode": "hybrid", "ma_period": 50, "atr_multiple": 3.0,
                "buffer": 0.01, "exit_mode": "chandelier"}
    selected = next((fold for fold in reversed(wf.folds) if fold.params), None)
    if selected is None:
        return {"entry_mode": "hybrid", "ma_period": 50, "atr_multiple": 3.0,
                "buffer": 0.01, "exit_mode": "chandelier"}
    # 补齐 ADX 默认字段，保证落地参数完整（旧存档无 adx 字段也兼容）
    return {**selected.params,
            "adx_filter": selected.params.get("adx_filter", False),
            "adx_threshold": selected.params.get("adx_threshold", 20),
            "adx_period": selected.params.get("adx_period", 14),
            "adx_direction": selected.params.get("adx_direction", False)}


def _stable_hash(payload: Dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _evaluate_final_holdout(symbol: str, ts: List[str], opens: List[float],
                            highs: List[float], lows: List[float],
                            closes: List[float], params: Dict,
                            start_idx: int, *, volumes: Optional[List[float]] = None,
                            cost_bps: float = 25.0) -> Dict:
    """用锁定候选参数执行最终一次 Holdout；结果只能 PASS/FAIL。"""
    result = run_backtest(
        symbol, ts, opens, highs, lows, closes, params,
        start_idx=start_idx, end_idx=len(closes),
        volumes=volumes, cost_bps=cost_bps,
    )
    unfilled = any("unfilled" in trade.exit_reason for trade in result.trades)
    reasons = []
    if result.trade_count < FINAL_HOLDOUT_MIN_TRADES:
        reasons.append("holdout_insufficient_trades")
    if result.total_return_pct <= 0:
        reasons.append("holdout_return")
    if result.sharpe_daily <= 0:
        reasons.append("holdout_sharpe")
    if result.max_drawdown_pct > 25.0:
        reasons.append("holdout_maxdd")
    if unfilled:
        reasons.append("holdout_stp_lmt_unfilled")
    return {
        "passed": not reasons,
        "minimum_bars": FINAL_HOLDOUT_MIN_BARS,
        "minimum_trades": FINAL_HOLDOUT_MIN_TRADES,
        "reasons": reasons,
        "trade_count": result.trade_count,
        "return_pct": round(result.total_return_pct, 6),
        "sharpe": round(result.sharpe_daily, 6),
        "maxdd_pct": round(result.max_drawdown_pct, 6),
        "start": ts[start_idx],
        "end": ts[-1],
    }


def _wf_evidence(wf) -> Dict:
    """WF 结果摘要（写入 params_json.evidence 与 archive 共用）。"""
    return {
        "passed_folds": wf.passed_folds,
        "positive_folds": wf.positive_folds,
        "cost_positive_folds": wf.cost_positive_folds,
        "parameter_variants": wf.parameter_variants,
        "avg_sharpe": round(wf.avg_sharpe, 3),
        "avg_maxdd": round(wf.avg_maxdd, 2),
        "worst_cost_return_pct": round(wf.worst_cost_return_pct, 2),
        "structurally_robust": wf.structurally_robust,
        "folds": [
            {
                "fold": f.fold,
                "params": f.params,
                "inner_train_end": f.inner_train_end,
                "inner_validation_start": f.inner_validation_start,
                "inner_validation_end": f.inner_validation_end,
                "inner_candidate_count": f.inner_candidate_count,
                "inner_shortlist_count": f.inner_shortlist_count,
                "inner_validation": (
                    f.inner_validation_result.stats()
                    if f.inner_validation_result is not None else None),
                "passed": f.passed,
                "oos_trades": f.oos_trades,
                "oos_return_pct": round(f.oos_return_pct, 2),
                "oos_cost_return_pct": round(f.oos_cost_return_pct, 2),
                "oos_sharpe": round(f.oos_sharpe, 3),
                "oos_maxdd_pct": round(f.oos_maxdd_pct, 2),
                "baseline_return_pct": round(f.baseline_return_pct, 2),
                "fail_reasons": f.fail_reasons,
            }
            for f in wf.folds
        ],
    }


def _next_lifecycle_status(conn, symbol: str, decision_status: str, evidence: str) -> str:
    """生命周期推进（spec §2.10 + 铁律 5/6）：

    - decision=verified（通过验证）→ verified（任何历史状态可翻案，fail_count 不动）
    - decision=degraded：
      - 相同 evidence hash 重跑 → 幂等，保持原失败状态（不重复累计失败）
      - 新证据且已有 1 次失败（fail_count>=1 或已 removed）→ removed
      - 新证据首次失败 → degraded
    """
    if decision_status == "verified":
        return "verified"
    if decision_status in ("research_only", "shadow"):
        return decision_status
    row = dbm.get_lifecycle(conn, symbol)
    prev_status = row["status"] if row is not None else None
    prev_hash = row["last_evidence_hash"] if row is not None else None
    prev_fail = row["fail_count"] if row is not None else 0
    if prev_hash == evidence:
        return prev_status if prev_status in ("degraded", "removed") else "degraded"
    if prev_status == "removed" or prev_fail >= 1:
        return "removed"
    return "degraded"


def research_symbol(conn, symbol: str, grid: Optional[List[Dict]] = None) -> Dict:
    """对单个标的跑完整研究流水线，更新生命周期。

    前置：数据已缓存（bars + manifest）。
    """
    bars = dbm.get_bars(conn, symbol)
    if len(bars) < MIN_RESEARCH_BARS:
        return {"symbol": symbol,
                "error": f"数据不足：至少需要 {MIN_RESEARCH_BARS} 根日线（含 126 根 Holdout）"}

    ts = [b["ts"] for b in bars]
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    # 更新状态为 backtesting
    dbm.set_lifecycle(conn, symbol, "backtesting")

    # 最后 max(10%, 126 日) 永久保留给一次性 Final Holdout。
    holdout_bars = max(FINAL_HOLDOUT_MIN_BARS,
                       int(len(closes) * FINAL_HOLDOUT_FRAC))
    holdout_start_idx = len(closes) - holdout_bars
    if holdout_start_idx < MIN_BARS:
        return {"symbol": symbol, "error": "数据不足以划分开发区与 Final Holdout"}
    cost_estimate = estimate_cost(
        symbol, closes[:holdout_start_idx], volumes[:holdout_start_idx])
    wf = run_walk_forward(
        symbol, ts[:holdout_start_idx], opens[:holdout_start_idx],
        highs[:holdout_start_idx], lows[:holdout_start_idx],
        closes[:holdout_start_idx], params_grid=grid,
        volumes=volumes[:holdout_start_idx],
        cost_bps=cost_estimate.total_bps_per_side,
    )

    # 候选参数只来自最后一折训练结果，不读取各折 OOS 收益排序。
    params = _production_params_from_training(wf)

    # 开发区评分先决定是否有资格打开最终 Holdout；开发区失败时保持 SEALED。
    decision = decide_lifecycle(wf)

    # evidence hash：数据 manifest sha + 网格 + 策略版本
    # grid 标记：full/small/adx（adx 网格与 full 网格是不同搜索空间，hash 必须不同，
    # 否则 ADX 版本研究结果会被原 evidence 幂等跳过）
    manifest = dbm.get_manifest(conn, symbol)
    grid_tag = "full"
    if grid is not None:
        if grid is PARAM_GRID_ADX:
            grid_tag = "adx"
        elif len(grid) <= 20:
            grid_tag = "small"
    data_version = manifest["sha256"] if manifest else "none"
    evidence = json.dumps({
        "data_sha": data_version,
        "grid": grid_tag,
        "strategy_version": "v5.1",
        "validation": "nested_dev_walk_forward_plus_126d_single_use_holdout",
    }, sort_keys=True)

    effective_grid = grid or PARAM_GRID
    development_oos_stats = aggregate_oos_trades(wf.folds)
    robustness = {
        "multiple_testing": multiple_testing_diagnostic(
            wf.avg_sharpe, len(effective_grid), max(2, holdout_start_idx // 2)),
        "fold_parameter_stability": fold_parameter_stability(
            fold.params for fold in wf.folds),
        "parameter_neighborhood": parameter_neighborhood_diagnostic(
            symbol, ts[:holdout_start_idx], opens[:holdout_start_idx],
            highs[:holdout_start_idx], lows[:holdout_start_idx],
            closes[:holdout_start_idx], params, effective_grid),
        "oos_trade_count": development_oos_stats["n"],
        "min_verified_oos_trades": MIN_VERIFIED_OOS_TRADES,
    }
    robustness["passed"] = (
        robustness["multiple_testing"]["passed"]
        and robustness["fold_parameter_stability"]["passed"]
        and robustness["parameter_neighborhood"]["passed"]
        and robustness["oos_trade_count"] >= MIN_VERIFIED_OOS_TRADES
    )
    if decision["status"] == "verified" and not robustness["passed"]:
        decision["status"] = "research_only"
        decision["eligible"] = False
        failed = [name for name in ("multiple_testing", "fold_parameter_stability",
                                    "parameter_neighborhood")
                  if not robustness[name]["passed"]]
        if robustness["oos_trade_count"] < MIN_VERIFIED_OOS_TRADES:
            failed.append("insufficient_oos_trades")
        decision["reasons"] = list(decision["reasons"]) + failed
    decision["robustness"] = robustness
    search_space_hash = _stable_hash({"grid": effective_grid, "grid_tag": grid_tag})
    candidate_version_hash = _stable_hash({
        "symbol": symbol, "params": params,
        "evaluator": "strategy-evaluator-v1",
        "search_space_hash": search_space_hash,
    })
    holdout_id = "ho_" + _stable_hash({
        "symbol": symbol, "data_version": data_version,
        "start": ts[holdout_start_idx], "end": ts[-1],
    })[:20]
    holdout_row = dbm.seal_research_holdout(
        conn, holdout_id, symbol, data_version,
        ts[holdout_start_idx], ts[-1], candidate_version_hash,
    )
    manifest_payload = {
        "symbol": symbol,
        "data_version": data_version,
        "search_space_hash": search_space_hash,
        "candidate_version_hash": candidate_version_hash,
        "candidate_params": params,
        "development_start": ts[0],
        "development_end": ts[holdout_start_idx - 1],
        "holdout_start": ts[holdout_start_idx],
        "holdout_end": ts[-1],
        "cost_model": cost_estimate.to_dict(),
        "holdout_rule": "single_exposure_accept_or_reject",
        "search_trials": len(effective_grid),
        "robustness": robustness,
    }
    research_manifest_id = "rm_" + _stable_hash(manifest_payload)[:20]
    dbm.save_research_manifest(
        conn, research_manifest_id, symbol, data_version, search_space_hash,
        candidate_version_hash, holdout_id, manifest_payload,
    )

    holdout_result = {
        "status": holdout_row["status"], "passed": False,
        "reasons": ["development_validation_failed"],
        "holdout_id": holdout_id,
    }
    if decision["status"] == "verified":
        opened = dbm.open_research_holdout(conn, holdout_id, candidate_version_hash)
        if opened["outcome"] == "OPENED":
            measured = _evaluate_final_holdout(
                symbol, ts, opens, highs, lows, closes, params, holdout_start_idx,
                volumes=volumes, cost_bps=cost_estimate.total_bps_per_side)
            dbm.consume_research_holdout(
                conn, holdout_id, candidate_version_hash, measured)
            holdout_result = {**measured, "status": "CONSUMED",
                              "holdout_id": holdout_id, "cached": False}
        elif opened["outcome"] == "CACHED":
            holdout_result = {**opened["result"], "status": "CONSUMED",
                              "holdout_id": holdout_id, "cached": True}
        else:
            holdout_result = {
                "status": "CONTAMINATED", "passed": False,
                "reasons": ["holdout_contaminated"], "holdout_id": holdout_id,
            }
        if not holdout_result["passed"]:
            # 方向/风险均通过但交易笔数不足时进入 shadow 收集 forward 证据，
            # 不把低频策略误判成失效，也绝不授予交易资格。
            only_small_sample = set(holdout_result.get("reasons", [])) == {
                "holdout_insufficient_trades"}
            decision["status"] = "shadow" if only_small_sample else "degraded"
            decision["eligible"] = False
            decision["reasons"] = list(decision["reasons"]) + list(
                holdout_result["reasons"])
    decision["holdout"] = holdout_result
    decision["research_manifest_id"] = research_manifest_id

    # Kelly 只能消费“最终冻结候选”在选参完成之后的样本。以前把四折中不同
    # 参数的 OOS 成交混在一起，会把不存在的复合策略当成最终策略，现已禁止。
    frozen_oos_stats = {
        "n": 0, "wins": 0, "losses": 0, "avg_win": 0.0, "avg_loss": 0.0,
        "p": 0.0, "b": 0.0, "positive_folds": 0, "total_folds": 2,
    }
    if holdout_result.get("status") == "CONSUMED" and holdout_result.get("passed"):
        frozen_start_idx = wf.folds[-1].test_start
        frozen_result = run_backtest(
            symbol, ts, opens, highs, lows, closes, params,
            start_idx=frozen_start_idx, end_idx=len(closes), volumes=volumes,
            cost_bps=cost_estimate.total_bps_per_side,
        )
        positive_periods = int(wf.folds[-1].oos_cost_return_pct > 0) + int(
            holdout_result.get("return_pct", 0) > 0)
        frozen_oos_stats = trade_statistics(
            frozen_result.trades, positive_periods=positive_periods, total_periods=2)
        frozen_oos_stats.update({
            "evidence_scope": "frozen_candidate_post_selection",
            "evidence_start": ts[frozen_start_idx], "evidence_end": ts[-1],
        })
        if frozen_oos_stats["n"] < MIN_VERIFIED_OOS_TRADES:
            decision["status"] = "shadow"
            decision["eligible"] = False
            decision["reasons"] = list(decision["reasons"]) + [
                "insufficient_frozen_candidate_trades"]

    # params_json：只保存开发区训练选择的锁定候选参数与完整验证证据。
    params_json = json.dumps({
        "entry_mode": params["entry_mode"],
        "ma_period": params["ma_period"],
        "atr_multiple": params["atr_multiple"],
        "buffer": params["buffer"],
        "exit_mode": params.get("exit_mode", "chandelier"),
        "adx_filter": params.get("adx_filter", False),
        "adx_threshold": params.get("adx_threshold", 20),
        "adx_period": params.get("adx_period", 14),
        "adx_direction": params.get("adx_direction", False),
        "score": decision["score"],
        "evidence": _wf_evidence(wf),
        "holdout": holdout_result,
        "robustness": robustness,
        "research_manifest_id": research_manifest_id,
        "candidate_version_hash": candidate_version_hash,
        "cost_model": cost_estimate.to_dict(),
    }, ensure_ascii=False, sort_keys=True)

    # 生命周期推进（含 degraded→removed；相同 evidence hash 不重复累计失败）
    status = _next_lifecycle_status(conn, symbol, decision["status"], evidence)
    dbm.set_lifecycle(conn, symbol, status,
                      evidence_hash=evidence, score=decision["score"],
                      params_json=params_json)

    # StrategyVersion 的仓位统计严格来自同一冻结候选的 post-selection 样本。
    oos_stats = dict(frozen_oos_stats)
    oos_stats["final_test_accepted"] = bool(
        holdout_result.get("status") == "CONSUMED" and holdout_result.get("passed") is True)
    oos_stats["candidate_version_hash"] = candidate_version_hash
    strategy_files = [Path(__file__), Path(__file__).with_name("walk_forward.py"),
                      Path(__file__).resolve().parents[1] / "shared" / "backtest.py",
                      Path(__file__).resolve().parents[1] / "shared" / "strategy_evaluator.py"]
    code_hash = hashlib.sha256(
        b"".join(path.read_bytes() for path in strategy_files)
    ).hexdigest()
    try:
        import subprocess
        git_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
    except Exception:
        git_commit = None
    version_id = dbm.save_strategy_version(
        conn, symbol, status=status, params_json=params_json,
        wf_report_json=json.dumps(_wf_evidence(wf), ensure_ascii=False, sort_keys=True),
        oos_stats_json=json.dumps(oos_stats, ensure_ascii=False, sort_keys=True),
        git_commit=git_commit, code_hash=code_hash,
        data_version=data_version,
    )

    # 2026-08-06 新流程：通过回测验证（verified）的标的自动进入订阅清单，
    # 由 monitor/cron 每日盘前/盘中/盘后监听买卖信号（不再有独立"系统自选池"）。
    subscribed = False
    if status == "verified":
        try:
            from production.subscribe import add_sub
            r = add_sub(symbol, conn=conn)
            subscribed = r.get("message", "")
        except Exception as e:
            subscribed = f"订阅失败: {e}"

    # 非生产研究归档保留全部证据。
    if status in ("research_only", "shadow", "degraded", "removed"):
        dbm.archive(conn, symbol, status,
                    params_json=params_json,
                    score=decision["score"],
                    evidence_json=json.dumps(decision, ensure_ascii=False))

    return {"symbol": symbol, **decision, "status": status, "subscribed": subscribed,
            "strategy_version_id": version_id, "oos_stats": oos_stats,
            "research_manifest_id": research_manifest_id,
            "holdout_id": holdout_id}


def research_candidate(conn, symbol: str,
                       grid: Optional[List[Dict]] = None) -> Dict:
    """研究主链入口；prefilter 未通过时禁止进入回测与 Holdout。"""
    bars = [dict(row) for row in dbm.get_bars(conn, symbol)]
    check = prefilter(conn, symbol, bars)
    if not check["passed"]:
        return {
            "symbol": symbol,
            "error": "prefilter failed",
            "error_type": "PrefilterFailed",
            "error_message": ", ".join(check["reasons"]),
            "retryable": "stale_completed_bar" in check["reasons"],
            "prefilter": check,
        }
    return research_symbol(conn, symbol, grid=grid)


# ────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="研究流水线")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="加入候选池")
    p_add.add_argument("symbol")

    p_list = sub.add_parser("list", help="列出候选")
    p_list.add_argument("--status", default="candidate")

    p_sync = sub.add_parser("sync", help="同步长桥自选 → 系统候选池")

    p_pre = sub.add_parser("prefilter", help="预筛（读 DB 中已缓存数据）")
    p_pre.add_argument("symbol")

    p_cache = sub.add_parser("cache", help="从长桥拉取日线写入缓存（bars + manifest）")
    p_cache.add_argument("symbol")

    p_res = sub.add_parser("research", help="跑完整研究流水线")
    p_res.add_argument("symbol")
    p_res.add_argument("--grid", choices=["full", "small", "adx"],
                       default=_CONFIG.research.grid,
                       help="full=1620组原网格; small=9组快速; adx=1620组+ADX>20过滤")

    args = parser.parse_args()
    conn = dbm.get_core_conn()

    if args.cmd == "add":
        from shared.security import LazyLongbridgeSecurityProvider, SecurityService
        service = SecurityService(conn, LazyLongbridgeSecurityProvider())
        print(add_candidate(conn, args.symbol, security_service=service))
    elif args.cmd == "sync":
        report = sync_watchlist(conn)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(dbm.list_lifecycle(conn, args.status))
    elif args.cmd == "prefilter":
        bars = dbm.get_bars(conn, args.symbol)
        if not bars:
            print(f"{args.symbol} 无缓存数据，先执行 cache")
            return
        result = prefilter(conn, args.symbol,
                           [dict(b) for b in bars])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "cache":
        try:
            result = cache_symbol(conn, args.symbol)
        except Exception as e:
            print(f"[错误] cache {args.symbol}: {e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "research":
        grid = None if args.grid == "full" else (_small_grid() if args.grid == "small" else PARAM_GRID_ADX)
        result = research_candidate(conn, args.symbol, grid=grid)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _small_grid():
    """小网格：3 入场 × 3 出场形态 = 9 组（固定 MA50 / ATR3.0 / buf0.01）。

    必须含 exit_mode：若缺省该字段，run_backtest 默认 chandelier，选参器
    永远只能在吊灯形态里挑，死叉形态（ma_cross）在 P1 网格中的价值无法被验证。
    """
    return [
        {"entry_mode": m, "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.01,
         "exit_mode": ex}
        for m in ("hybrid", "breakout", "pullback")
        for ex in ("chandelier", "ma_cross", "chandelier_or_cross")
    ]


if __name__ == "__main__":
    main()
