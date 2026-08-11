#!/usr/bin/env python3
"""
盘前 / 盘中 / 盘后监控 — 交易系统 v3.0
======================================
spec §3.1：

盘前：读取上一完成日线，计算入场观察区域、现有持仓吊灯止损、保护单缺失、
      账户权益和现有名义敞口、完整组合风险。

盘中：实时价格只做临界预警（距入场边界 <= 0.5%、距昨日确认止损 <= 1%、保护单缺失）。
      同一条件同一天只提醒一次；离开区域后再次进入才可重新提醒。
      盘中信号必须标记：临界预警 / 等待收盘确认，不能直接当作买卖信号。

盘后：完成日线确认——是否产生正式入场、是否触发退出、吊灯止损是否变化、
      模拟账本是否需要更新。

信号生成前提：数据完成、来源未过期、策略版本一致，否则不产生正式信号。
"""

import sys
from pathlib import Path

# 让 shared 可导入：trading-system/shared（db / longbridge_client 等，自包含目录，架构 §6.1）
_TRADING_ROOT = str(Path(__file__).resolve().parents[1])
if _TRADING_ROOT not in sys.path:
    sys.path.insert(0, _TRADING_ROOT)

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from shared import db as dbm
from shared.config import get_config
from shared.indicators import sma, atr22
from shared.market_calendar import completed_bar_freshness, market_for_symbol
from shared.strategy_evaluator import ATR_PERIOD, StrategyEvaluator
from shared.account import ensure_synced
from shared.security import require_security_metadata

# 盘中预警阈值（spec §3.1）
_CONFIG = get_config()
STOP_ALERT = _CONFIG.monitor.critical_distance_pct / 100.0
PRE_ENTRY_ALERT = STOP_ALERT / 2.0


def _market_for_symbol(symbol: str) -> str:
    return market_for_symbol(symbol)


def _completed_bar_freshness(conn, symbol: str, last_bar_date: str,
                             as_of_date: str) -> tuple[bool, str]:
    """按交易日历判断完成日线是否新鲜；US 在亚洲运行时允许一个交易日时差。"""
    source_row = conn.execute(
        "SELECT source FROM bars WHERE symbol=? AND ts=?",
        (symbol, last_bar_date),
    ).fetchone()
    source = source_row["source"] if source_row is not None else ""
    return completed_bar_freshness(
        conn, symbol, last_bar_date, as_of_date, source=source)


def _symbol_data_health(conn, symbol: str, as_of_date: Optional[str] = None) -> Dict:
    """统一生产数据 gate；synthetic/test fixtures 仍可用于纯策略单测。"""
    bars = dbm.get_bars(conn, symbol)
    if not bars:
        return {"ok": False, "status": "UNKNOWN", "reason": "bars_missing"}
    manifest = dbm.get_manifest(conn, symbol)
    source = str(manifest["source"] if manifest is not None else "")
    simulated = source.lower() in {"test", "e2e", "synthetic", "simulation"}
    bar_sources = [row["source"] for row in conn.execute(
        "SELECT DISTINCT source FROM bars WHERE symbol=?", (symbol,)).fetchall()]
    if manifest is None and bar_sources and all(str(source).lower() in {
            "test", "parity", "e2e", "synthetic", "simulation"}
            for source in bar_sources):
        return {"ok": True, "status": "TEST", "source": "test",
                "data_version": "test-fixture"}
    if manifest is None:
        return {"ok": False, "status": "UNKNOWN", "reason": "manifest_missing"}
    required = ("source", "fetched_at", "last_completed", "date_start",
                "date_end", "bar_count", "sha256")
    missing = [field for field in required if manifest[field] in (None, "")]
    if missing:
        return {"ok": False, "status": "UNKNOWN",
                "reason": f"manifest_missing:{','.join(missing)}"}
    if int(manifest["bar_count"] or 0) != len(bars):
        return {"ok": False, "status": "MISMATCH",
                "reason": "manifest_bar_count_mismatch"}
    fresh, freshness_reason = _completed_bar_freshness(
        conn, symbol, bars[-1]["ts"], as_of_date or datetime.now().strftime("%Y-%m-%d"))
    if not fresh and not simulated:
        return {"ok": False, "status": "STALE", "source": source,
                "data_version": manifest["sha256"], "reason": freshness_reason}
    try:
        require_security_metadata(conn, symbol)
    except ValueError as exc:
        return {"ok": False, "status": "UNKNOWN", "source": source,
                "data_version": manifest["sha256"], "reason": str(exc)}
    return {"ok": True, "status": "SYNCED", "source": source,
            "data_version": manifest["sha256"], "freshness": freshness_reason}


def health_check(conn, symbols: Optional[List[str]] = None,
                 account_id: str = "default", *, require_account: bool = True,
                 as_of_date: Optional[str] = None,
                 max_account_age_seconds: int = 30 * 60) -> Dict:
    """返回可审计的 readiness 结果；非 healthy 结果只能安全降级。"""
    checks = {"symbols": {}, "account": None}
    failures: List[Dict] = []
    if require_account:
        account = ensure_synced(conn, account_id, max_account_age_seconds)
        checks["account"] = {
            "status": account.sync_status,
            "updated_at": account.updated_at,
            "source": account.source,
            "source_version": account.source_version,
            "last_success_at": account.last_success_at,
            "failure_reason": account.failure_reason,
        }
        if not account.synced:
            failures.append({"gate": "account", "status": account.sync_status,
                             "reason": account.failure_reason or "account_not_synced"})
    for symbol in symbols or []:
        result = _symbol_data_health(conn, symbol, as_of_date)
        checks["symbols"][symbol] = result
        if not result["ok"]:
            failures.append({"gate": "symbol", "symbol": symbol,
                             "status": result["status"], "reason": result["reason"]})
    report = {"ok": not failures, "status": "HEALTHY" if not failures else "BLOCKED",
              "failures": failures, "checks": checks}
    dbm.audit(conn, "MONITOR_HEALTH", "account", account_id,
              {"status": report["status"], "failures": failures,
               "symbols": list(symbols or [])})
    return report

def reset_alert_log(date: str) -> None:
    """兼容旧调用；v5 去重已持久化，按日期键自然滚动，无需清空内存。"""
    return None


def _log_alert(conn, symbol: str, condition: str, date: str) -> bool:
    """返回 True 表示跨进程首次提醒，False 表示数据库中已提醒。"""
    return dbm.claim_monitor_alert(conn, symbol, condition, date)


@dataclass
class Alert:
    symbol: str
    kind: str          # pre_entry / stop_proximity / missing_protective
    message: str
    level: str = "critical_alert"  # 盘中统一标记

    def __str__(self):
        return f"[{self.level}] {self.symbol} {self.kind}: {self.message}"


# ────────────────────────────────────────────────────────────────
# 盘前
# ────────────────────────────────────────────────────────────────

@dataclass
class PreMarketReport:
    symbol: str
    entry_zone: Optional[Dict] = None      # {ma, buffer_low, buffer_high}
    current_stop: Optional[float] = None
    protective_missing: bool = False
    alerts: List[Alert] = field(default_factory=list)
    position_open: bool = False
    blocked: bool = False
    block_reason: Optional[str] = None


def pre_market_check(conn, symbol: str, params: Dict, date: str,
                     protective_orders: Optional[List[str]] = None,
                     realtime_position: Optional[Dict] = None) -> PreMarketReport:
    """盘前检查单个标的。

    Args:
        params: 该标的的入场参数 {entry_mode, ma_period, atr_multiple, buffer}
        date: 当前交易日
        protective_orders: 当前 broker 上存在的保护单列表（用于缺失检测）
        realtime_position: 长桥实时持仓 dict（portfolio 表无行时的降级来源）
    """
    report = PreMarketReport(symbol=symbol)
    health = _symbol_data_health(conn, symbol, date)
    if not health["ok"]:
        report.blocked = True
        report.block_reason = health["reason"]
        report.alerts.append(Alert(symbol, "data_blocked",
                                   f"数据 gate 阻断：{health['reason']}"))
        return report
    bars = dbm.get_bars(conn, symbol)
    if len(bars) < 50:
        report.alerts.append(Alert(symbol, "data", "数据不足"))
        return report

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]
    n = len(closes)

    evaluator = StrategyEvaluator(opens, highs, lows, closes, params)
    mas = evaluator.mas

    # 入场观察区域（用最后完成 bar）
    i = n - 1
    ma = mas[i]
    buffer = params["buffer"]
    if ma > 0:
        report.entry_zone = {
            "ma": ma,
            "buffer_low": ma * (1 - buffer),
            "buffer_high": ma * (1 + buffer),
        }
        # 若当前 close 已在区域内或接近 → 盘前标记观察
        if evaluator.evaluate_entry(i).triggered:
            report.alerts.append(Alert(symbol, "pre_entry",
                                       f"收盘已在入场区域 (close={closes[i]:.2f} ma={ma:.2f})"))

    # 持仓：计算当前吊灯止损
    # 优先 portfolio 表（旧行为）；无行但有长桥实时持仓 → 降级（peak_high 从 bars 计算）
    pos = dbm.get_position(conn, symbol)
    if pos is None and realtime_position is not None:
        pos = _realtime_position_fallback(symbol, realtime_position, bars, params)
    if pos is not None:
        report.position_open = True
        peak = pos["peak_high"]
        stop = evaluator.current_stop(n - 1, peak)
        report.current_stop = stop
        if stop is None:
            return report
        # 破位检查：止损高于现价 = 吊灯线已击穿（通常是前复权极值或真实破位），
        # 给明确预警而不是一个永远不会触发的止损价
        if stop > closes[n - 1]:
            report.alerts.append(Alert(symbol, "stop_breached",
                                       f"⚠️ 止损 {stop:.2f} 已高于现价 {closes[n-1]:.2f}（吊灯线破位），按策略应退出，请人工确认"))
        prev_stop = pos["stop_price"]
        if prev_stop and stop > prev_stop + 1e-9:
            report.alerts.append(Alert(symbol, "stop_change", f"止损应上移 {prev_stop:.2f} → {stop:.2f}"))
        # 保护单缺失检查
        if protective_orders is not None and symbol not in protective_orders:
            report.protective_missing = True
            report.alerts.append(Alert(symbol, "missing_protective", f"保护单缺失 (应挂 {stop:.2f})"))

    return report


# ────────────────────────────────────────────────────────────────
# 盘中
# ────────────────────────────────────────────────────────────────

@dataclass
class IntradayAlert:
    symbol: str
    condition: str
    message: str
    tag: str = "critical_alert/wait_close_confirm"

    def __str__(self):
        return f"[{self.tag}] {self.symbol} {self.condition}: {self.message}"


def intraday_check(conn, symbol: str, params: Dict, date: str,
                   last_close: float, realtime_price: float,
                   realtime_position: Optional[Dict] = None,
                   protective_orders: Optional[List[str]] = None) -> List[IntradayAlert]:
    """盘中实时价格临界预警。

    Args:
        last_close: 上一完成日线收盘
        realtime_price: 当前实时价格
        realtime_position: 长桥实时持仓 dict（portfolio 表无行时的降级来源）
    """
    alerts: List[IntradayAlert] = []
    health = _symbol_data_health(conn, symbol, date)
    if not health["ok"]:
        alerts.append(IntradayAlert(
            symbol, "data_blocked", f"数据 gate 阻断：{health['reason']}"))
        return alerts
    bars = dbm.get_bars(conn, symbol)
    if not bars:
        return alerts
    closes = [b["close"] for b in bars]
    mas = sma(closes, params["ma_period"])
    ma = mas[-1]

    # 1. 距入场边界 <= 0.5%
    if ma > 0:
        buffer = params["buffer"]
        zone_high = ma * (1 + buffer)
        dist = abs(realtime_price - zone_high) / zone_high
        if dist <= PRE_ENTRY_ALERT and _log_alert(conn, symbol, "pre_entry", date):
            alerts.append(IntradayAlert(symbol, "pre_entry",
                                        f"现价 {realtime_price:.2f} 距入场上界 {zone_high:.2f} 仅 {dist*100:.2f}%"))

    # 2. 距昨日确认止损 <= 1%
    # 优先 portfolio 表（旧行为）；无行但有长桥实时持仓 → 降级（stop 从 bars 计算）
    pos = dbm.get_position(conn, symbol)
    if pos is None and realtime_position is not None:
        pos = _realtime_position_fallback(symbol, realtime_position, bars, params)
    if pos is not None:
        stop = pos["stop_price"]
        # 破位检查：止损高于实时价 = 吊灯线已击穿
        if stop > realtime_price:
            alerts.append(IntradayAlert(symbol, "stop_breached",
                                        f"⚠️ 止损 {stop:.2f} 已高于现价 {realtime_price:.2f}（吊灯线破位），按策略应退出，请人工确认"))
        dist_stop = abs(realtime_price - stop) / stop if stop > 0 else 1.0
        if dist_stop <= STOP_ALERT and _log_alert(conn, symbol, "stop_proximity", date):
            alerts.append(IntradayAlert(symbol, "stop_proximity",
                                        f"现价 {realtime_price:.2f} 距止损 {stop:.2f} 仅 {dist_stop*100:.2f}%"))

        if protective_orders is not None and symbol not in protective_orders:
            if _log_alert(conn, symbol, "missing_protective", date):
                alerts.append(IntradayAlert(
                    symbol, "missing_protective", "券商端未发现活跃 MIT 卖出保护单"))

    return alerts


# ────────────────────────────────────────────────────────────────
# 盘后
# ────────────────────────────────────────────────────────────────

@dataclass
class PostMarketReport:
    symbol: str
    date: str
    formal_entry: bool = False
    exit_triggered: bool = False
    stop_changed: bool = False
    ledger_update_needed: bool = False
    messages: List[str] = field(default_factory=list)


def post_market_check(conn, symbol: str, params: Dict, date: str,
                      realtime_position: Optional[Dict] = None) -> PostMarketReport:
    """盘后完成日线确认。

    前提：数据完成（本日 bar 已收盘）、来源未过期、策略版本一致。
    Args:
        realtime_position: 长桥实时持仓 dict（portfolio 表无行时的降级来源）
    """
    report = PostMarketReport(symbol=symbol, date=date)
    health = _symbol_data_health(conn, symbol, date)
    if not health["ok"]:
        report.messages.append(f"数据 gate 阻断：{health['reason']}")
        return report
    bars = dbm.get_bars(conn, symbol)
    n = len(bars)
    if n < 50:
        report.messages.append("数据不足")
        return report

    # 生产数据严格依赖交易日历；US 在亚洲时区运行允许完成 bar 落后一个交易日。
    fresh, freshness_reason = _completed_bar_freshness(
        conn, symbol, bars[-1]["ts"], date)
    if not fresh:
        report.messages.append(
            f"最新数据 {bars[-1]['ts']} 不满足新鲜度（截至 {date}；"
            f"{freshness_reason}），等待收盘确认")
        return report

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]
    evaluator = StrategyEvaluator(opens, highs, lows, closes, params)

    # 优先 portfolio 表（旧行为）；无行但有长桥实时持仓 → 降级（peak_high 从 bars 计算）
    pos = dbm.get_position(conn, symbol)
    if pos is None and realtime_position is not None:
        pos = _realtime_position_fallback(symbol, realtime_position, bars, params)

    # 1. 正式入场（无持仓 + 今天信号）
    if pos is None:
        if evaluator.evaluate_entry(n - 1).triggered:
            report.formal_entry = True
            report.ledger_update_needed = True
            report.messages.append(f"正式入场信号 @ close {closes[-1]:.2f}")
    else:
        # 2. 退出触发
        peak = pos["peak_high"]
        decision = evaluator.evaluate_exit(n - 1, peak)
        stop = decision.stop_price
        if decision.triggered:
            exit_px = float(decision.reference_price)
            report.exit_triggered = True
            report.ledger_update_needed = True
            stop_msg = f" stop={stop:.2f}" if stop is not None else ""
            report.messages.append(
                f"退出触发 {decision.reason} @ {exit_px:.2f} ({stop_msg.strip()})")
        # 3. 止损变化（上移）；降级 pos 的 stop_price=当前 stop，无历史值不触发
        if stop is not None and pos["stop_price"] and stop > pos["stop_price"] + 1e-9:
            report.stop_changed = True
            report.ledger_update_needed = True
            report.messages.append(f"止损上移 {pos['stop_price']:.2f} → {stop:.2f}")

    return report


def post_market_to_outbox(conn, symbol: str, params: Dict, date: str, *,
                          account_id: str = "default",
                          realtime_position: Optional[Dict] = None,
                          channels: Optional[List[str]] = None) -> Dict:
    """运行盘后监控并把报告原子写入 notification outbox。"""
    report = post_market_check(
        conn, symbol, params, date, realtime_position=realtime_position)
    if report.formal_entry:
        kind = "ENTRY"
    elif report.exit_triggered:
        kind = "EXIT"
    elif report.stop_changed:
        kind = "STOP_CHANGED"
    else:
        kind = "NO_CHANGE"
    latest = dbm.get_latest_strategy_version(conn, symbol)
    strategy_version_id = int(latest["version_id"]) if latest else 0
    payload = {
        "symbol": symbol,
        "kind": f"MONITOR_POST_{kind}",
        "rationale": "; ".join(report.messages) or "盘后检查无状态变化",
        "formal_entry": report.formal_entry,
        "exit_triggered": report.exit_triggered,
        "stop_changed": report.stop_changed,
    }
    persisted = dbm.record_signal_with_outbox(
        conn, account_id=account_id, symbol=symbol,
        strategy_version_id=strategy_version_id, bar_ts=date,
        signal_type=f"MONITOR_POST_{kind}", payload=payload,
        channels=channels,
    )
    return {"report": report, "signal": persisted}


# ────────────────────────────────────────────────────────────────
# 实时持仓（真相源：长桥 OpenAPI；portfolio 表不再维护）
# ────────────────────────────────────────────────────────────────

def _safe_float(v, default: float = 0.0) -> float:
    """安全转 float（长桥持仓字段是字符串，可能为 '0' / None）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _estimate_entry_idx(closes: List[float], cost_price: float) -> Optional[int]:
    """从后往前估算入场日索引：最后一个收盘价 ≈ 成本价（±3%）的位置。

    长桥实时持仓没有 entry_ts，用成本价锚定入场点，从而让吊灯止损只取
    「入场以来」峰值，避免被前复权历史极值（如 SCO 2020 油价崩盘价 ~4000）
    污染成永远不触发的天价止损。找不到精确匹配时放宽为最后一个收盘价
    不高于成本价×1.03 的位置；仍无则返回 None（调用方用近期窗口兜底）。
    """
    if cost_price <= 0:
        return None
    lo, hi = cost_price * 0.97, cost_price * 1.03
    for i in range(len(closes) - 1, -1, -1):
        if lo <= closes[i] <= hi:
            return i
    for i in range(len(closes) - 1, -1, -1):
        if closes[i] <= hi:
            return i
    return None


def _realtime_position_fallback(symbol: str, realtime_position: Dict,
                                bars: List[Dict], params: Dict) -> Optional[Dict]:
    """portfolio 表无行时，用长桥实时持仓 + bars 构造 pos 降级视图。

    返回 dict（字段与 portfolio 表 Row 对齐：peak_high/stop_price/quantity 等）。
    peak_high：入场以来最高 high（用 cost_price 锚定入场日估算，见 _estimate_entry_idx；
               锚定失败时保守取最近 252 日峰值，避免全历史前复权极值污染）；
    stop_price：当前吊灯止损（= peak - atr_multiple×ATR），无历史表值 → 不触发"止损上移"。
    数据不足时返回 None（调用方按无持仓处理，不崩溃）。
    """
    if realtime_position is None:
        return None
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    if not highs:
        return None
    cost = _safe_float(realtime_position.get("cost_price"))
    entry_idx = _estimate_entry_idx(closes, cost)
    if entry_idx is not None:
        peak = max(highs[entry_idx:])
    elif len(highs) >= 252:
        peak = max(highs[-252:])
    else:
        peak = max(highs)
    atr = atr22(highs, lows, closes, ATR_PERIOD)
    stop = peak - params["atr_multiple"] * atr[-1]
    return {
        "symbol": symbol,
        "entry_price": _safe_float(realtime_position.get("cost_price")),
        "entry_ts": "",
        "quantity": _safe_float(realtime_position.get("quantity")),
        "stop_price": stop,
        "peak_high": peak,
        "batch": 1,
        "updated_at": "",
    }


def _realtime_portfolio(client=None) -> Optional[List[Dict]]:
    """从长桥 OpenAPI 实时拉取当前持仓（真相源：broker，替代 portfolio 表）。

    返回：
        持仓 dict 列表（symbol/quantity/cost_price/last_price 等，长桥原始字段），
        空列表 = 长桥可用但当前无持仓；
        None = 长桥不可用（凭证缺失/SDK/导入/API 失败），错误已打印到 stderr，
        由调用方降级处理。本函数不抛未捕获异常。
    """
    try:
        from shared.longbridge_client import LongbridgeClient
    except ImportError as e:
        print(f"[错误] 长桥客户端不可用（实时持仓依赖 shared/longbridge_client.py）: {e}",
              file=sys.stderr)
        return None
    try:
        client = client or LongbridgeClient()
        positions = client.positions()
        return positions if isinstance(positions, list) else []
    except Exception as e:
        print(f"[错误] 长桥实时持仓获取失败: {e}", file=sys.stderr)
        return None


def _position_out(position: Dict) -> Dict:
    """输出用实时持仓字段（symbol/quantity/cost_price/last_price，有则用）。"""
    return {
        "symbol": position.get("symbol", ""),
        "quantity": position.get("quantity"),
        "cost_price": position.get("cost_price"),
        "last_price": position.get("last_price"),
    }


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """内存造数据的冒烟测试（--selftest 入口）。"""
    import math
    conn = dbm.get_core_conn(":memory:")

    # 造 120 根数据
    rows = []
    base = 100.0
    for i in range(120):
        c = base + 0.8 * math.sin(i / 5) + i * 0.05
        rows.append({"ts": f"2024-{(i//28)+1:02d}-{(i%28)+1:02d}", "open": c-0.2,
                     "high": c+1.0, "low": c-1.0, "close": c, "volume": 20_000_000})
        base += 0.1
    dbm.upsert_bars(conn, "M.US", rows, "test")

    params = {"entry_mode": "hybrid", "ma_period": 20, "atr_multiple": 3.0, "buffer": 0.01}
    last_ts = rows[-1]["ts"]

    # 盘前
    pm = pre_market_check(conn, "M.US", params, last_ts, protective_orders=["M.US"])
    print("pre_market:", "position_open" if pm.position_open else "no_position",
          "entry_zone:", pm.entry_zone is not None)

    # 盘后（数据不包含今日 → 应提示等待）
    pm2 = post_market_check(conn, "M.US", params, "2099-12-31")
    print("post_market (future date):", pm2.messages)
    assert "等待收盘确认" in " ".join(pm2.messages)

    # 盘中（无持仓 → 只有 pre_entry 类）
    reset_alert_log("2024-01-01")
    ia = intraday_check(conn, "M.US", params, "2024-01-01",
                        last_close=rows[-1]["close"], realtime_price=rows[-1]["close"])
    print("intraday alerts:", [str(a) for a in ia])

    print("monitor.py 冒烟测试通过 ✅")
    return 0


# ────────────────────────────────────────────────────────────────
# CLI（cron 定时调用入口）
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    import sys
    from datetime import datetime

    # 标的入场参数默认值：hybrid / MA50 / 3.0×ATR / 1% buffer。
    # 来源：SKILL.md 生产监控语义与 backtest.py 参数网格中的保守默认；
    # 若 lifecycle.params_json（研究流水线写入）有该标的参数则优先使用。
    DEFAULT_PARAMS = {"entry_mode": "hybrid", "ma_period": 50, "atr_multiple": 3.0, "buffer": 0.01}
    MIN_BARS = 50  # 与 check 函数的数据量下限一致（<50 视为数据不足）

    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="交易系统盘前/盘中/盘后监控（cron 入口）",
    )
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="{pre,intra,post}")

    p_pre = sub.add_parser("pre", help="盘前：入场区域/当前止损/保护单缺失")
    p_pre.add_argument("--symbol", default=None,
                       help="指定单标的（优先于 --scope）；缺省遍历组合")
    p_pre.add_argument("--scope", choices=["portfolio", "watchlist"],
                       default=_CONFIG.monitor.scope,
                       help="portfolio=仅持仓检查；watchlist=仅自选买入信号检查；缺省=两者并集")

    p_intra = sub.add_parser("intra", help="盘中：实时价格临界预警")
    p_intra.add_argument("--symbol", default=None,
                         help="指定单标的（优先于 --scope）；缺省遍历组合")
    p_intra.add_argument("--scope", choices=["portfolio", "watchlist"],
                         default=_CONFIG.monitor.scope,
                         help="portfolio=仅持仓检查；watchlist=仅自选买入信号检查；缺省=两者并集")
    p_intra.add_argument("--price", type=float, default=None,
                         help="手工覆盖实时价格；缺省使用长桥 SDK 批量实时报价")

    p_post = sub.add_parser("post", help="盘后：完成日线确认（入场/退出/止损变化）")
    p_post.add_argument("--symbol", default=None,
                        help="指定单标的（优先于 --scope）；缺省遍历组合")
    p_post.add_argument("--scope", choices=["portfolio", "watchlist"],
                        default=_CONFIG.monitor.scope,
                        help="portfolio=仅持仓检查；watchlist=仅自选买入信号检查；缺省=两者并集")

    args = parser.parse_args()

    if args.selftest:
        sys.exit(_selftest())

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    conn = dbm.get_core_conn()
    date = datetime.now().strftime("%Y-%m-%d")

    def params_for(symbol: str) -> Dict:
        """参数来源：lifecycle.params_json > 默认 hybrid/50/3.0/0.01。
        portfolio/lifecycle 表除 params_json 外没有参数列。"""
        lc = dbm.get_lifecycle(conn, symbol)
        if lc is not None and lc["params_json"]:
            try:
                p = json.loads(lc["params_json"])
                if all(k in p for k in ("entry_mode", "ma_period", "atr_multiple", "buffer")):
                    return p
            except (ValueError, TypeError):
                pass
        return dict(DEFAULT_PARAMS)

    def traverse_symbols(symbol: Optional[str], scope: Optional[str],
                         rt_positions: Optional[List[Dict]]) -> List[str]:
        """遍历逻辑（优先级：--symbol > --scope > 缺省并集）：
        - --symbol: 仅该标的
        - scope=portfolio: 仅实时持仓（长桥 positions，真相源）
        - scope=watchlist: 仅 StateRepository 中启用的关注/订阅
        - 缺省: 实时持仓 ∪ 订阅清单（去重保序）"""
        if symbol:
            return [symbol]
        rows: List[Dict] = []
        if scope == "portfolio":
            rows = list(rt_positions or [])
        elif scope == "watchlist":
            from production.subscribe import load_subs
            rows = [{"symbol": s} for s in load_subs(conn).keys()]
        else:
            from production.subscribe import load_subs
            rows = ([{"symbol": s} for s in load_subs(conn).keys()]
                    + list(rt_positions or []))
        seen = set()
        out: List[str] = []
        for r in rows:
            s = r["symbol"]
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def run_one(symbol: str, command: str, price: Optional[float],
                position: Optional[Dict] = None,
                protective_orders: Optional[List[str]] = None,
                realtime_quote: Optional[Dict] = None) -> Dict:
        bars = dbm.get_bars(conn, symbol)
        status = "数据不足" if len(bars) < MIN_BARS else "ok"
        data_health = _symbol_data_health(conn, symbol, date)
        params = params_for(symbol)

        if command == "pre":
            rep = pre_market_check(conn, symbol, params, date,
                                   protective_orders=protective_orders,
                                   realtime_position=position)
            result = {
                "symbol": symbol, "status": status, "params": params,
                "entry_zone": rep.entry_zone,
                "current_stop": rep.current_stop,
                "position_open": rep.position_open,
                "protective_missing": rep.protective_missing,
                "health_status": data_health["status"],
                "blocked": rep.blocked,
                "block_reason": rep.block_reason,
                "alerts": [{"level": a.level, "kind": a.kind, "message": a.message}
                           for a in rep.alerts],
            }
            if position is not None:
                result["position"] = _position_out(position)
            return result

        if command == "intra":
            last_close = bars[-1]["close"] if bars else 0.0
            quote_price = _safe_float((realtime_quote or {}).get("current_price"))
            realtime = price if price is not None else quote_price
            if realtime <= 0:
                # 行情不可用时明确降级，输出会标记 fallback，不能伪装成实时价。
                realtime = last_close
            alerts = intraday_check(conn, symbol, params, date, last_close, realtime,
                                    realtime_position=position,
                                    protective_orders=protective_orders)
            result = {
                "symbol": symbol, "status": status, "params": params,
                "price_source": ("cli --price" if price is not None else
                                 "longbridge_sdk" if quote_price > 0 else
                                 "last_close_fallback"),
                "realtime_price": realtime,
                "health_status": data_health["status"],
                "blocked": not data_health["ok"],
                "block_reason": data_health.get("reason"),
                "alerts": [{"condition": a.condition, "tag": a.tag, "message": a.message}
                           for a in alerts],
            }
            if position is not None:
                result["position"] = _position_out(position)
            return result

        # post
        rep = post_market_check(conn, symbol, params, date, realtime_position=position)
        result = {
            "symbol": symbol, "status": status, "params": params,
            "formal_entry": rep.formal_entry,
            "exit_triggered": rep.exit_triggered,
            "stop_changed": rep.stop_changed,
            "ledger_update_needed": rep.ledger_update_needed,
            "health_status": data_health["status"],
            "blocked": not data_health["ok"],
            "block_reason": data_health.get("reason"),
            "messages": rep.messages,
        }
        if position is not None:
            result["position"] = _position_out(position)
        return result

    # 实时持仓真相源：长桥 OpenAPI（portfolio 表不再维护）。
    # watchlist scope / --symbol 不拉长桥（行为不变，避免多余 API 调用）。
    rt_positions: Optional[List[Dict]] = None
    rt_map: Dict[str, Dict] = {}
    lb_error = False
    lb_client = None
    # 同一轮监控复用一个 SDK client，避免持仓、保护单和行情各自重复认证。
    if args.scope != "watchlist" or args.command in ("pre", "intra"):
        try:
            from shared.longbridge_client import LongbridgeClient
            lb_client = LongbridgeClient()
        except Exception as exc:
            print(f"[错误] 长桥监控快照初始化失败: {exc}", file=sys.stderr)
            lb_error = True
    if args.symbol is None and args.scope != "watchlist" and lb_client is not None:
        rt_positions = _realtime_portfolio(lb_client)
        if rt_positions is None:
            lb_error = True
        else:
            rt_map = {p["symbol"]: p for p in rt_positions}

    # --scope portfolio 且长桥不可用：清晰错误 + 退出码非 0（不抛未捕获异常）
    if args.scope == "portfolio" and lb_error:
        print(json.dumps({
            "command": args.command, "date": date, "scope": "portfolio",
            "status": "error",
            "message": "长桥实时持仓不可用（凭证缺失/SDK/API 失败），请检查 LONGBRIDGE 配置",
            "symbols_checked": 0, "results": [],
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    symbols = traverse_symbols(args.symbol, args.scope, rt_positions)
    scope_label = args.scope if args.scope else "union"
    if not symbols:
        if args.scope == "portfolio":
            msg = "长桥可用但当前无持仓"
        elif lb_error:
            msg = "长桥不可用，已降级为仅自选池"
        else:
            msg = "无持仓标的/自选池为空"
        print(json.dumps({
            "command": args.command, "date": date, "scope": scope_label,
            "status": "no_symbols",
            "message": msg, "symbols_checked": 0, "results": [],
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    readiness = health_check(
        conn, symbols, require_account=args.scope != "watchlist", as_of_date=date)
    if not readiness["ok"]:
        print(json.dumps({
            "command": args.command, "date": date, "scope": scope_label,
            "status": "BLOCKED", "symbols_checked": 0, "results": [],
            "health": readiness,
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    quote_map: Dict[str, Dict] = {}
    protective_symbols: Optional[List[str]] = None
    realtime_errors = []
    if lb_client is not None and symbols and args.command in ("pre", "intra"):
        try:
            quote_map = {q["symbol"]: q for q in lb_client.quotes(symbols) if q.get("symbol")}
        except Exception as exc:
            print(f"[错误] 长桥实时行情快照失败: {exc}", file=sys.stderr)
            realtime_errors.append({
                "error_type": type(exc).__name__, "error_message": str(exc),
                "retryable": bool(getattr(exc, "retryable", False)),
            })
        try:
            protective_symbols = sorted({
                order.get("symbol", "") for order in lb_client.stop_orders()
                if order.get("symbol")
            })
        except Exception as exc:
            print(f"[错误] 长桥保护单快照失败: {exc}", file=sys.stderr)
            protective_symbols = None

    results = [run_one(
        s, args.command, getattr(args, "price", None), rt_map.get(s),
        protective_symbols, quote_map.get(s),
    ) for s in symbols]
    out = {
        "command": args.command, "date": date, "scope": scope_label,
        "status": "SAFE_DEGRADE" if realtime_errors else "ok",
        "symbols_checked": len(symbols), "results": results,
        "health": readiness,
    }
    if realtime_errors:
        out["realtime_errors"] = realtime_errors
    if lb_error:
        out["realtime_position_error"] = True
    # 通知是输出的旁路消费者，失败不改变监控/交易判定。
    notification_failures = 0
    try:
        from production.notification import notify
        for item in results:
            events = item.get("alerts") or []
            for event in events:
                if not notify(
                    conn, f"monitor.{args.command}",
                    f"{item['symbol']} {event.get('kind') or event.get('condition')}",
                    event.get("message", ""), severity="WARNING",
                    entity_type="symbol", entity_id=item["symbol"],
                ):
                    notification_failures += 1
    except Exception as exc:
        print(f"[错误] 监控通知失败: {exc}", file=sys.stderr)
        notification_failures += 1
    if notification_failures:
        out["notification_failures"] = notification_failures
    print(json.dumps(out, ensure_ascii=False, indent=2))
