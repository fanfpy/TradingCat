#!/usr/bin/env python3
"""
tc — TradingCat 统一命令入口（对齐 OpenAlice CLI-first 设计）
================================================================

领域边界（模仿 OpenAlice 四 CLI 设计，但收敛为一个命名空间）：
    tc market ...    行情/数据（长桥 Python SDK）
    tc watchlist ... 长桥自选 ⇄ 系统候选池同步（生命周期 candidate/backtesting/verified/degraded/removed）
    tc subscribe ... 订阅清单（监听范围，research 验证通过自动订阅）
    tc research ...  研究流水线（pipeline.py）
    tc monitor ...   盘前/盘中/盘后监控（monitor.py，--scope watchlist 读订阅清单）
    tc position ...  仓位计算（position.py）
    tc risk ...      组合风控（portfolio_risk.py）
    tc trade ...     下单（保留二次确认，dry-run 默认）

设计原则：
    1. 薄封装：不重写逻辑，只做入口统一 + --json 输出规范
    2. 生产逻辑永远走 trading-system（docs/architecture.md 安全边界）
    3. 破坏性/下单操作必须先 dry-run 再确认
    4. 监听范围 = StateRepository 关注/订阅表，research verified 自动订阅

用法：
    tc market quote GLD.US --json
    tc watchlist sync
    tc research run GLD.US --grid full
    tc monitor pre --scope watchlist
    tc position GLD.US
    tc risk portfolio
    tc account sync
    tc execution reconcile
    tc strategy list
    tc backup daily
    tc trade order --symbol GLD.US --qty 10 --mode DRY_RUN   # 交互式确认后走 Execution 安全链
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# trading-system 根目录
TS_ROOT = Path(__file__).resolve().parent


def _reexec_project_venv() -> None:
    """直接运行 tc.py 时优先切换到项目虚拟环境。"""
    venv_python = TS_ROOT / ".venv" / "bin" / "python"
    if sys.prefix == sys.base_prefix and venv_python.is_file():
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    _reexec_project_venv()


def run_python(script: str, args: list) -> int:
    """运行 trading-system 下的 Python 脚本，继承输出。"""
    cmd = [sys.executable, str(TS_ROOT / script)] + args
    return subprocess.call(cmd, cwd=str(TS_ROOT))


def cmd_market(args) -> int:
    """行情/数据：直接使用 Python SDK，不启动会触发 OAuth 的 CLI。"""
    try:
        if args.cmd == "fundamentals":
            from shared.data_providers import (
                FundamentalProviderChain, OpenAliceCommandProvider,
            )
            providers = []
            openalice = OpenAliceCommandProvider.from_env()
            if openalice is not None:
                providers.append(openalice)
            data = FundamentalProviderChain(providers).current(args.symbol).to_dict()
        else:
            from shared.longbridge_client import LongbridgeClient
            client = LongbridgeClient(scope="quote")
        if args.cmd == "quote":
            data = client.quotes(args.symbols)
        elif args.cmd == "kline":
            data = {
                symbol: client.kline_by_count(
                    symbol, count=args.count, period=args.period, adjust=args.adjust)
                for symbol in args.symbols
            }
        elif args.cmd == "depth":
            data = [client.depth(symbol) for symbol in args.symbols]
            data = [item for item in data if item]
        elif args.cmd == "fundamentals":
            pass
        else:
            print(f"[错误] 未知 market 子命令: {args.cmd}", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"[错误] 长桥 SDK 初始化或查询失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(data, ensure_ascii=False, indent=None if args.json else 2))
    if args.cmd == "fundamentals":
        return 0 if data.get("snapshots") else 2
    return 0 if data else 1


def cmd_watchlist(args) -> int:
    """自选池同步。"""
    if args.cmd == "sync":
        from research.pipeline import sync_watchlist
        from shared import db as dbm
        conn = dbm.get_conn()
        report = sync_watchlist(conn)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    elif args.cmd == "list":
        from shared import db as dbm
        conn = dbm.get_conn()
        rows = dbm.list_lifecycle(conn, args.status)
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return 0
    print(f"[错误] 未知 watchlist 子命令: {args.cmd}", file=sys.stderr)
    return 1


def cmd_research(args) -> int:
    """研究流水线：薄封装 pipeline.py。"""
    if args.cmd == "add":
        return run_python("research/pipeline.py", ["add", args.symbol])
    elif args.cmd == "run":
        return run_python("research/pipeline.py", ["research", args.symbol, "--grid", args.grid])
    elif args.cmd == "cache":
        return run_python("research/pipeline.py", ["cache", args.symbol])
    elif args.cmd == "prefilter":
        return run_python("research/pipeline.py", ["prefilter", args.symbol])
    elif args.cmd == "quant-preview":
        if not args.capability and not args.script_file:
            print("[错误] quant-preview 需要 --script-file，或使用 --capability 只检查能力",
                  file=sys.stderr)
            return 2
        return cmd_quant_preview(args)
    print(f"[错误] 未知 research 子命令: {args.cmd}", file=sys.stderr)
    return 1


def cmd_monitor(args) -> int:
    """监控：薄封装 monitor.py。"""
    scope = []
    if args.scope:
        scope = ["--scope", args.scope]
    symbol = []
    if getattr(args, "symbol", None):
        symbol = ["--symbol", args.symbol]
    return run_python("production/monitor.py", [args.cmd] + scope + symbol)


def cmd_position(args) -> int:
    """仓位计算：薄封装 position.py。"""
    return run_python("production/position.py", ["--symbol", args.symbol])


def cmd_risk(args) -> int:
    """组合风控：对真实账户快照和本地持仓执行只读检查。"""
    from production.operations import check_current_portfolio
    from shared import db as dbm
    result = check_current_portfolio(dbm.get_conn())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def cmd_account(args, _conn=None, _client=None) -> int:
    """账户状态查询、资产同步与持仓对账。"""
    from dataclasses import asdict
    from production.operations import sync_runtime_state
    from shared import db as dbm
    from shared.account import ensure_synced, sync_positions

    conn = _conn or dbm.get_conn()
    if args.cmd == "show":
        result = asdict(ensure_synced(conn, args.account_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["sync_status"] == "SYNCED" else 1
    if args.cmd == "sync":
        result = sync_runtime_state(conn, client=_client, account_id=args.account_id)
    else:
        result = sync_positions(conn, client=_client, account_id=args.account_id)
        result["ok"] = result["synced"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_execution(args, _conn=None, _broker=None) -> int:
    """订单生命周期运维：只读查询券商并与本地订单对账。"""
    from execution.broker_live import LiveBroker
    from production.operations import reconcile_runtime
    from shared import db as dbm

    conn = _conn or dbm.get_conn()
    broker = _broker or LiveBroker(conn, enable_live=True)
    result = reconcile_runtime(conn, broker, plan_id=args.plan_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_strategy(args, _conn=None) -> int:
    """查询 StrategyVersion 快照。"""
    from shared import db as dbm
    conn = _conn or dbm.get_conn()
    rows = dbm.list_strategy_versions(
        conn, symbol=args.symbol, limit=args.limit, newest_first=True)
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    return 0


def cmd_backup(args) -> int:
    """WAL 安全的每日/每周在线备份。"""
    from production.backup import run_daily, run_weekly
    result = (run_daily(args.dest) if args.cmd == "daily"
              else run_weekly(args.dest))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_portfolio(args) -> int:
    """构建目标组合和持久化 ExecutionPlan；绝不自动批准或提交。"""
    from production.decision import run_decision, target_to_execution_plan
    from shared import db as dbm
    from shared.account import ensure_synced

    conn = dbm.get_conn()
    state = ensure_synced(conn)
    equity = args.equity or state.nav or state.cash
    if equity is None or equity <= 0:
        print("[错误] 无可用账户 NAV/equity；请先同步账户或传 --equity", file=sys.stderr)
        return 1
    if args.mode == "LIVE" and not state.synced:
        print(f"[拒绝] LIVE 计划要求 AccountState=SYNCED，当前 {state.sync_status}", file=sys.stderr)
        return 1
    effective_state = state if state.synced else None
    tp = run_decision(conn, equity, account_state=effective_state)
    plan = target_to_execution_plan(
        conn, tp, equity, mode="DRY_RUN" if args.dry_run else args.mode,
        account_state=effective_state,
    )
    output = {
        "passed": tp.passed, "failures": tp.failures,
        "final_fracs": tp.final_fracs, "details": tp.details,
        "plan": plan.to_dict() if plan else None,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if plan is not None:
        print(f"\n下一步：python3 tc.py trade plan --plan-id {plan.plan_id}")
    return 0


def cmd_trade(args, confirm_input=None, live_confirm_input=None, _conn=None,
              _quote_provider=None, _broker=None, _execution_conn=None) -> int:
    """下单：交互式确认 → Execution 安全链（默认 DRY_RUN，不触达券商）。

    从占位建议升级为完整安全链（US-004）：
        ExecutionPlan(plan_hash) → 展示订单详情 → y/N 确认 →
        ApprovalAdapter(APPROVED) → PreTradeRisk(PASS/REJECT) → OrderManager.consume

    - confirm_input: 可注入的确认函数（返回 True=批准 / False=取消），pytest 用；
      None 时从真实 stdin 读（超时/EOF/非 y → 取消，零 OrderIntent）。
    - _conn/_quote_provider: 测试注入（内存 DB / 假行情），生产默认走真实 DB + 长桥行情。
    """
    from shared import db as dbm
    conn = _conn if _conn is not None else dbm.get_core_conn()
    # 生产 CLI 始终使用 executiond 私有库；测试若未显式注入则保留 DRY_RUN 单库兼容。
    execution_conn = (_execution_conn if _execution_conn is not None
                      else (dbm.get_execution_conn() if _conn is None else conn))
    provider = _quote_provider if _quote_provider is not None else _fetch_quote
    if args.cmd == "plan":
        return _run_trade_plan(
            conn, args.plan_id, confirm_input, provider,
            enable_live=getattr(args, "enable_live", False),
            live_confirm_input=live_confirm_input, broker=_broker,
            execution_conn=execution_conn,
        )
    if args.cmd != "order":
        print(f"[错误] 未知 trade 子命令: {args.cmd}", file=sys.stderr)
        return 1
    return _run_trade_order(
        conn, args.symbol, args.qty, args.mode, confirm_input, provider,
        enable_live=getattr(args, "enable_live", False),
        live_confirm_input=live_confirm_input, broker=_broker,
        execution_conn=execution_conn,
    )


def _run_trade_plan(conn, plan_id: str, confirm_input=None, quote_provider=None,
                    enable_live: bool = False, live_confirm_input=None,
                    broker=None, execution_conn=None) -> int:
    """展示并确认已持久化的同一个 ExecutionPlan（不重新构造订单）。"""
    from production.decision import load_execution_plan
    from execution.order_manager import ApprovalAdapter, ConfirmationService
    import uuid

    plan = load_execution_plan(conn, plan_id)
    if plan is None:
        print(f"[错误] ExecutionPlan 不存在: {plan_id}", file=sys.stderr)
        return 1
    if plan.execution_mode == "LIVE" and not enable_live:
        print("[拒绝] LIVE 计划必须显式传 --enable-live；计划保留为 PENDING，未创建订单。")
        return 1
    confirm_input = confirm_input or _default_confirm_input
    quote_provider = quote_provider or _fetch_quote
    execution_conn = execution_conn if execution_conn is not None else conn
    if plan.execution_mode == "LIVE":
        try:
            dbm.assert_separate_stores(conn, execution_conn)
        except RuntimeError as exc:
            print(f"[拒绝] {exc}；零 OrderIntent。")
            return 1
    cfm = ConfirmationService(conn, execution_conn).create(plan)
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    print("是否确认执行该 plan_id？[y/N] ", end="", flush=True)
    if not confirm_input():
        dbm.set_confirmation_status(execution_conn, cfm.confirmation_id, "CANCELLED")
        dbm.set_plan_status(conn, plan.plan_id, "CANCELLED")
        print("\n[取消] 未产生 OrderIntent。")
        return 0
    if plan.execution_mode == "LIVE":
        if not _confirm_live_phrase(plan.plan_id, live_confirm_input):
            dbm.set_confirmation_status(execution_conn, cfm.confirmation_id, "CANCELLED")
            dbm.set_plan_status(conn, plan.plan_id, "CANCELLED")
            print("\n[取消] LIVE 精确确认短语不匹配，零 OrderIntent。")
            return 1
        print("\n[拒绝] P0-A 模式禁止 CLI 直接 mint LIVE APPROVED；"
              "请由独立 executiond 验证 ApprovalProof。计划保持 PENDING，零 OrderIntent。")
        return 1
    approved = ApprovalAdapter(execution_conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce=uuid.uuid4().hex)
    rc, created = _post_approval(conn, plan, approved, quote_provider, broker=broker,
                                 execution_conn=execution_conn)
    if rc == 0:
        print(f"[成功] 已为原计划 {plan.plan_id} 创建 {len(created)} 个 OrderIntent。")
    return rc


def _default_confirm_input(timeout: float = 30.0) -> bool:
    """从 stdin 读 y/N。返回 True=批准 / False=取消。

    超时（select 超时）、EOF（管道/重定向）、非 y/yes 输入 → False（取消，零 intent）。
    """
    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return False
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    if not line:
        return False
    return line.strip().lower() in ("y", "yes")


def _default_live_confirm_input(plan_id: str, timeout: float = 30.0) -> str:
    """读取 LIVE 精确确认短语。EOF/超时一律返回空串。"""
    import select
    expected = f"LIVE {plan_id}"
    print(f"请输入精确短语 `{expected}` 以启用真实券商提交：", end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return ""
        return sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt, OSError):
        return ""


def _confirm_live_phrase(plan_id: str, reader=None) -> bool:
    expected = f"LIVE {plan_id}"
    value = (reader or _default_live_confirm_input)(plan_id)
    return isinstance(value, str) and value == expected


def _build_live_broker(conn):
    from execution.broker import BrokerEventHandler
    from execution.broker_live import LiveBroker
    return LiveBroker(conn, enable_live=True,
                      event_handler=BrokerEventHandler(conn))


def _fetch_quote(conn, symbol: str):
    """最佳努力获取参考价：长桥实时行情 → market_state 表 → 最后一根日线收盘。

    返回 (price, quote_at) 或 (None, None)。任何失败降级，不抛异常（US-004 修复：
    dbm 缺失导致 fallback 路径裸 NameError；每段均 try/except 兜底）。
    """
    from execution.models import now_utc
    from shared import db as dbm
    # 1) 长桥 Python SDK 实时行情（有三凭证时；失败静默降级）
    try:
        from shared.longbridge_client import LongbridgeClient
        data = LongbridgeClient(scope="quote").quote(symbol)
        if data:
            v = float(data.get("current_price") or data.get("last") or 0)
            if v > 0:
                return v, now_utc()
    except Exception:
        pass
    # 2) market_state 表（本地快照，带 quote_at）
    try:
        row = dbm.get_market_state(conn, symbol)
        if row is not None and row["price"]:
            return float(row["price"]), row["quote_at"]
    except Exception:
        pass
    # 3) 最后一根日线收盘（demo 降级：作为参考价展示）
    try:
        bar = dbm.get_last_bar(conn, symbol)
        if bar is not None and bar["close"]:
            return float(bar["close"]), now_utc()
    except Exception:
        pass
    return None, None


def _plan_expiry(hours: float = 1.0) -> str:
    """计划有效期：默认 1 小时后过期。"""
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_plan(conn, symbol: str, qty: float, mode: str, quote_provider,
                execution_conn=None) -> tuple:
    """生成 ExecutionPlan（含 plan_hash）+ PENDING Confirmation，并落库审计。"""
    from shared import db as dbm
    import uuid
    from execution.models import ExecutionPlan, PlanOrder, now_utc
    from execution.order_manager import ConfirmationService

    plan_id = f"plan_{uuid.uuid4().hex[:10]}"
    ref_price, ref_at = quote_provider(conn, symbol)
    order = PlanOrder("1", symbol, "BUY", qty,
                      reference_price=ref_price, reference_quote_at=ref_at,
                      max_slippage_bps=50.0)
    plan = ExecutionPlan(plan_id=plan_id, account_id="default",
                         execution_mode=mode, expires_at=_plan_expiry(),
                         orders=[order])
    dbm.insert_plan(conn, plan.plan_id, plan.account_id, plan.execution_mode,
                    plan.expires_at, plan.plan_hash, [order.to_dict()])
    svc = ConfirmationService(conn, execution_conn)
    cfm = svc.create(plan)
    return plan, cfm


def _post_approval(conn, plan, approved, quote_provider, broker=None,
                   execution_conn=None) -> tuple:
    """确认后链条：PreTradeRisk → OrderManager.consume。

    返回 (exit_code, created_intents)。PTR REJECT / consume 异常 → (非0, [])，零 intent。
    无效 confirmation（plan_hash 不匹配等）在此被 PTR 拒绝，绝不进入 consume。
    """
    from shared import db as dbm
    from shared.account import load as load_account
    from execution.models import MarketState, now_utc
    from execution.pretrade_risk import evaluate as pretrade_evaluate
    from execution.order_manager import OrderManager

    # 为 PTR 构造 MarketState：重新取行情（D-8：参考价 vs 当前市场价校验 slippage）
    states = {}
    for o in plan.orders:
        price, quote_at = quote_provider(conn, o.symbol)
        if price:
            states[o.symbol] = MarketState(symbol=o.symbol, quote_at=quote_at or now_utc(),
                                           price=price, max_age_seconds=300)

    execution_conn = execution_conn if execution_conn is not None else conn
    intents = dbm.list_intents(execution_conn)
    pending = sum(1 for r in intents if r["status"] in ("PENDING", "SUBMITTED"))
    unknown = sum(1 for r in intents if r["status"] == "UNKNOWN")

    risk = pretrade_evaluate(plan, approved, load_account(conn), states,
                             pending_intents=pending, unknown_intents=unknown)
    if not risk.passed:
        print("[拒绝] PreTradeRisk 校验未通过（零 OrderIntent）：")
        for r in risk.reasons:
            print(f"  - {r}")
        dbm.set_confirmation_status(execution_conn, approved.confirmation_id, "REJECTED")
        dbm.set_plan_status(conn, plan.plan_id, "REJECTED")
        dbm.audit(conn, "PRETRADE", entity_type="plan", entity_id=plan.plan_id,
                  payload={"decision": "REJECT", "reasons": risk.reasons})
        return 1, []

    if plan.execution_mode == "LIVE" and broker is None:
        print("[拒绝] LIVE 缺少已显式启用的 broker（零 OrderIntent）。")
        return 1, []
    om = OrderManager(execution_conn, broker=broker)
    try:
        if plan.execution_mode == "LIVE":
            created = om.submit(
                plan, approved, market_states=states,
                account_state=load_account(conn),
            )
        else:
            created = om.consume(plan, approved)
    except Exception as e:
        print(f"[错误] OrderManager 消费失败（零 OrderIntent）: {e}")
        return 1, []
    dbm.audit(conn, "PRETRADE", entity_type="plan", entity_id=plan.plan_id,
              payload={"decision": "PASS"})
    return 0, created


def _positive_float(value: str) -> float:
    """argparse type：数量必须 > 0（US-004 修复：拒绝 --qty 0 / 负数）。

    非法输入由 argparse 层报错退出（EXIT 2），不产生任何 intent。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"无效数量 '{value}'：必须是数字")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"无效数量 {v}：数量必须 > 0")
    return v


def _run_trade_order(conn, symbol: str, qty: float, mode: str,
                     confirm_input=None, quote_provider=None,
                     enable_live: bool = False, live_confirm_input=None,
                     broker=None, execution_conn=None) -> int:
    """完整交互式安全链（DRY_RUN 默认；LIVE 必须显式三重解锁）。

    - 生成 ExecutionPlan + PENDING Confirmation → 展示订单详情 → y/N 确认
    - y → ApprovalAdapter(APPROVED, cli, nonce 随机) → PreTradeRisk → consume（OrderIntent）
    - N / 超时 / EOF → 取消，零 OrderIntent
    - LIVE → --enable-live + y/N + 精确短语，随后仍须全部风控通过
    """
    from shared import db as dbm
    from execution.models import now_utc
    from execution.order_manager import ApprovalAdapter, ConfirmationService

    # US-004 修复：数量必须 > 0（防 BUY 0.0 / BUY -5.0 非法 intent）。
    # argparse type=_positive_float 已拦 CLI；此处兜底直接 API 调用（pytest / 脚本复用）。
    if qty is None or qty <= 0:
        print(f"[错误] 无效数量 qty={qty}：数量必须 > 0（拒绝下单，零 OrderIntent）",
              file=sys.stderr)
        return 1

    if confirm_input is None:
        confirm_input = _default_confirm_input
    if quote_provider is None:
        quote_provider = _fetch_quote

    execution_conn = execution_conn if execution_conn is not None else conn
    if mode == "LIVE":
        try:
            dbm.assert_separate_stores(conn, execution_conn)
        except RuntimeError as exc:
            print(f"[拒绝] {exc}；LIVE 不创建 Confirmation/OrderIntent。")
            return 1

    plan, cfm = _build_plan(conn, symbol, qty, mode, quote_provider,
                            execution_conn=execution_conn)

    # ── 展示订单详情 ────────────────────────────────────────────
    print("=" * 64)
    print("📋 交易计划 ExecutionPlan（不可变，plan_hash 强绑定）")
    print(f"  plan_id        : {plan.plan_id}")
    print(f"  execution_mode : {plan.execution_mode}")
    print(f"  plan_hash      : {plan.plan_hash[:20]}…")
    print(f"  confirmation   : {cfm.confirmation_id} (PENDING)")
    print("-" * 64)
    for o in plan.orders:
        print(f"  订单 {o.plan_order_id}:")
        print(f"    symbol           : {o.symbol}")
        print(f"    side             : {o.side}")
        print(f"    qty              : {o.quantity}")
        print(f"    reference_price  : {o.reference_price if o.reference_price is not None else '— (未取到行情)'}")
        print(f"    max_slippage_bps : {o.max_slippage_bps}")
        print(f"    expires_at       : {plan.expires_at}")
    print("=" * 64)

    if mode == "LIVE":
        print("[警告] CLI 只能创建 LIVE 待审批计划；实际批准必须由 executiond 验证 ApprovalProof。")

    print("是否确认提交该计划？[y/N] ", end="", flush=True)
    confirmed = confirm_input()

    if not confirmed:
        print("\n[取消] 未获确认（N/超时/EOF），不产生任何 OrderIntent。")
        dbm.set_confirmation_status(execution_conn, cfm.confirmation_id, "CANCELLED")
        dbm.set_plan_status(conn, plan.plan_id, "CANCELLED")
        return 0

    if mode == "LIVE":
        if not enable_live:
            print("\n[拒绝] 未显式传 --enable-live（零 OrderIntent）。")
            dbm.set_confirmation_status(execution_conn, cfm.confirmation_id, "CANCELLED")
            dbm.set_plan_status(conn, plan.plan_id, "CANCELLED")
            return 1
        if not _confirm_live_phrase(plan.plan_id, live_confirm_input):
            print("\n[取消] LIVE 精确确认短语不匹配（零 OrderIntent）。")
            dbm.set_confirmation_status(execution_conn, cfm.confirmation_id, "CANCELLED")
            dbm.set_plan_status(conn, plan.plan_id, "CANCELLED")
            return 1
        print("\n[拒绝] P0-A 模式禁止 CLI 直接 mint LIVE APPROVED；"
              "请由独立 executiond 验证 ApprovalProof。计划保持 PENDING，零 OrderIntent。")
        return 1

    # ── y：ApprovalAdapter 生成 APPROVED（真实用户动作，nonce 随机防 replay）──
    import uuid
    approved = ApprovalAdapter(execution_conn, channel="cli").approve(
        cfm.confirmation_id, approved_by="owner", nonce=uuid.uuid4().hex)
    print(f"\n[确认] APPROVED by={approved.approved_by} channel={approved.approval_channel} "
          f"confirmation={approved.confirmation_id}")

    rc, created = _post_approval(conn, plan, approved, quote_provider, broker=broker,
                                 execution_conn=execution_conn)
    if rc != 0:
        return rc

    result_mode = "LIVE 已提交券商" if mode == "LIVE" else "DRY_RUN，未触达券商"
    print(f"[成功] {len(created)} 个 OrderIntent 已创建（{result_mode}）：")
    for c in created:
        print(f"  - {c['client_request_id']} {c['symbol']} {c['side']} {c['quantity']} "
              f"status={c['status']} plan_hash={plan.plan_hash[:12]}…")
    return 0


def cmd_subscribe(args) -> int:
    """订阅/监听管理。"""
    from production.subscribe import add_sub, list_subs, remove_sub, run_subs
    if args.cmd == "add":
        print(json.dumps(add_sub(args.symbol, args.push_daily), ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(json.dumps(list_subs(), ensure_ascii=False, indent=2))
    elif args.cmd == "rm":
        print(json.dumps(remove_sub(args.symbol), ensure_ascii=False, indent=2))
    elif args.cmd == "run":
        print(json.dumps(run_subs(args.symbol), ensure_ascii=False, indent=2))
    return 0


def cmd_quant_preview(args) -> int:
    """运行可选 Longbridge Quant 交叉验证；永不更新策略生命周期。"""
    from pathlib import Path
    from shared.quant_provider import LongbridgeQuantProvider

    provider = LongbridgeQuantProvider()
    capability = provider.capability()
    if args.capability:
        print(json.dumps(capability.to_dict(), ensure_ascii=False, indent=2))
        # quant 是可选探索能力；不可用是可观测降级，不应令系统健康检查失败。
        return 0
    script = Path(args.script_file).read_text(encoding="utf-8")
    result = provider.run_script(args.symbol, args.start, args.end, script)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="tc",
        description="TradingCat 统一命令入口（研究 / 监控 / 行情 / 自选 / 仓位 / 交易）",
    )
    sub = parser.add_subparsers(dest="domain", required=True)

    # market
    p_market = sub.add_parser("market", help="行情/数据（长桥 Python SDK）")
    m_sub = p_market.add_subparsers(dest="cmd", required=True)
    m_quote = m_sub.add_parser("quote", help="实时报价")
    m_quote.add_argument("symbols", nargs="+")
    m_quote.add_argument("--json", action="store_true", help="JSON 输出")
    m_kline = m_sub.add_parser("kline", help="K线")
    m_kline.add_argument("symbols", nargs="+")
    m_kline.add_argument("--count", type=int, default=30)
    m_kline.add_argument("--period", choices=["day", "week", "month", "1m", "5m", "15m", "30m", "60m"], default="day")
    m_kline.add_argument("--adjust", choices=["forward", "none"], default="forward")
    m_kline.add_argument("--json", action="store_true")
    m_depth = m_sub.add_parser("depth", help="盘口深度")
    m_depth.add_argument("symbols", nargs="+")
    m_depth.add_argument("--json", action="store_true")
    m_fund = m_sub.add_parser(
        "fundamentals", help="当前基本面（需配置 OpenAlice JSON 适配器）")
    m_fund.add_argument("symbol")
    m_fund.add_argument("--json", action="store_true")

    # watchlist
    p_wl = sub.add_parser("watchlist", help="自选池同步与管理")
    wl_sub = p_wl.add_subparsers(dest="cmd", required=True)
    wl_sync = wl_sub.add_parser("sync", help="长桥自选 → 系统候选池同步")
    wl_list = wl_sub.add_parser("list", help="列出系统生命周期")
    wl_list.add_argument("--status", default="verified")

    # research
    p_res = sub.add_parser("research", help="研究流水线")
    res_sub = p_res.add_subparsers(dest="cmd", required=True)
    res_add = res_sub.add_parser("add", help="加入候选池")
    res_add.add_argument("symbol")
    res_run = res_sub.add_parser("run", help="跑完整研究")
    res_run.add_argument("symbol")
    res_run.add_argument("--grid", choices=["full", "small", "adx"], default="full")
    res_cache = res_sub.add_parser("cache", help="拉数据入缓存")
    res_cache.add_argument("symbol")
    res_pre = res_sub.add_parser("prefilter", help="预筛")
    res_pre.add_argument("symbol")
    res_quant = res_sub.add_parser(
        "quant-preview", help="Longbridge Quant 可选交叉验证（仅研究，不授予交易资格）")
    res_quant.add_argument("symbol", nargs="?", default="AAPL.US")
    res_quant.add_argument("--start", default="2025-01-01")
    res_quant.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    res_quant.add_argument("--script-file", default=None,
                           help="Navi/Pine 脚本文件；非 capability 模式必填")
    res_quant.add_argument("--capability", action="store_true",
                           help="只检查 CLI 能力，不发起认证或远端回测")

    # monitor
    p_mon = sub.add_parser("monitor", help="盘前/盘中/盘后监控")
    mon_sub = p_mon.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("pre", "盘前"), ("intra", "盘中"), ("post", "盘后")):
        m = mon_sub.add_parser(name, help=help_)
        m.add_argument("--scope", choices=["portfolio", "watchlist"])
        m.add_argument("--symbol", default=None)

    # position
    p_pos = sub.add_parser("position", help="仓位计算")
    p_pos.add_argument("--symbol", required=True)

    # subscribe
    p_sub = sub.add_parser("subscribe", help="订阅/监听管理（日频报告）")
    sub_cmd = p_sub.add_subparsers(dest="cmd", required=True)
    sub_add = sub_cmd.add_parser("add", help="注册订阅")
    sub_add.add_argument("symbol")
    # 默认开启日频推送；用 BooleanOptionalAction 让 CLI 同时支持 --push-daily 和 --no-push-daily
    sub_add.add_argument("--push-daily", action=argparse.BooleanOptionalAction, default=True,
                         help="每日盘后推送报告（默认开启；--no-push-daily 关闭）")
    sub_cmd.add_parser("list", help="查看订阅")
    sub_rm = sub_cmd.add_parser("rm", help="取消订阅")
    sub_rm.add_argument("symbol")
    sub_run = sub_cmd.add_parser("run", help="生成订阅报告（cron 驱动）")
    sub_run.add_argument("--symbol", default=None)

    # risk
    p_risk = sub.add_parser("risk", help="组合风控")
    risk_sub = p_risk.add_subparsers(dest="cmd", required=True)
    risk_sub.add_parser("check", aliases=["portfolio"], help="当前组合风险检查")

    # account runtime state
    p_account = sub.add_parser("account", help="账户与持仓状态")
    account_sub = p_account.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("show", "查看本地账户快照"),
                        ("sync", "同步资产并对账持仓"),
                        ("sync-positions", "仅执行持仓对账")):
        item = account_sub.add_parser(name, help=help_)
        item.add_argument("--account-id", default="default")

    # execution operations
    p_execution = sub.add_parser("execution", help="执行链运维")
    execution_sub = p_execution.add_subparsers(dest="cmd", required=True)
    e_reconcile = execution_sub.add_parser("reconcile", help="对账活跃订单计划")
    e_reconcile.add_argument("--plan-id", default=None, help="缺省对账全部非终态计划")

    # strategy registry
    p_strategy = sub.add_parser("strategy", help="策略版本注册表")
    strategy_sub = p_strategy.add_subparsers(dest="cmd", required=True)
    s_list = strategy_sub.add_parser("list", help="列出 StrategyVersion")
    s_list.add_argument("--symbol", default=None)
    s_list.add_argument("--limit", type=int, default=100)

    # backup
    p_backup = sub.add_parser("backup", help="SQLite 在线一致性备份")
    backup_sub = p_backup.add_subparsers(dest="cmd", required=True)
    for name in ("daily", "weekly"):
        item = backup_sub.add_parser(name)
        item.add_argument("--dest", default=None)

    # portfolio decision chain
    p_portfolio = sub.add_parser("portfolio", help="目标组合构建")
    portfolio_sub = p_portfolio.add_subparsers(dest="portfolio_cmd", required=True)
    p_build = portfolio_sub.add_parser("build", help="运行 SIG→SIZE→TP→PR→EP")
    p_build.add_argument("--mode", choices=["DRY_RUN", "LIVE"], default="DRY_RUN")
    p_build.add_argument("--dry-run", action="store_true", help="强制 DRY_RUN")
    p_build.add_argument("--equity", type=_positive_float, default=None,
                         help="账户 NAV 不可用时显式提供权益（仅建议/DRY_RUN）")

    # trade
    p_trade = sub.add_parser("trade", help="下单（交互式确认，默认 DRY_RUN）")
    t_sub = p_trade.add_subparsers(dest="cmd", required=True)
    t_order = t_sub.add_parser("order", help="拟议订单（生成 ExecutionPlan → y/N 确认 → 安全链）")
    t_order.add_argument("--symbol", required=True)
    t_order.add_argument("--qty", type=_positive_float, required=True,
                         help="数量（必须 > 0）")
    t_order.add_argument("--mode", choices=["DRY_RUN", "LIVE"], default="DRY_RUN",
                         help="DRY_RUN 默认（不触达券商）")
    t_order.add_argument("--enable-live", action="store_true",
                         help="显式解锁 LIVE；仍需交互确认和精确短语")
    t_plan = t_sub.add_parser("plan", help="确认并执行已持久化的同一个 ExecutionPlan")
    t_plan.add_argument("--plan-id", required=True)
    t_plan.add_argument("--enable-live", action="store_true",
                        help="显式解锁 LIVE；仍需交互确认和精确短语")

    args = parser.parse_args()
    domain = args.domain
    if domain == "market":
        sys.exit(cmd_market(args))
    elif domain == "watchlist":
        sys.exit(cmd_watchlist(args))
    elif domain == "research":
        sys.exit(cmd_research(args))
    elif domain == "monitor":
        sys.exit(cmd_monitor(args))
    elif domain == "position":
        sys.exit(cmd_position(args))
    elif domain == "subscribe":
        sys.exit(cmd_subscribe(args))
    elif domain == "risk":
        sys.exit(cmd_risk(args))
    elif domain == "account":
        sys.exit(cmd_account(args))
    elif domain == "execution":
        sys.exit(cmd_execution(args))
    elif domain == "strategy":
        sys.exit(cmd_strategy(args))
    elif domain == "backup":
        sys.exit(cmd_backup(args))
    elif domain == "portfolio":
        sys.exit(cmd_portfolio(args))
    elif domain == "trade":
        sys.exit(cmd_trade(args))


if __name__ == "__main__":
    main()
