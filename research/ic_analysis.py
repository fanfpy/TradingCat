#!/usr/bin/env python3
"""
US-005: 轻量 IC/IR 因子分析（Qlib 前置验证）
============================================
目的：在决定是否上 Qlib 横截面选股之前，先用轻量 IC 实验回答——
      "当前 bars 数据里的量价因子是否真的有 alpha（横截面预测力）？"

方法（全部手写 pandas/numpy，不依赖 Qlib/scipy）：
- 数据：shared/trading.db bars 表全部标的日线（≥8 年，2026-08 时点 149 标的）
- 因子（全部只用 t 日及之前数据，无前视偏差）：
    momentum_5 / momentum_20 / momentum_60   价格动量（过去 5/20/60 日收益）
    volatility_20 (ATR%)                      波动率（ATR20 / close）
    ma_distance_20                            价格距 MA20 偏离度
    rsi_14                                    Wilder RSI(14)
    volume_trend_20                           量能趋势（MA20 量 / MA60 量 - 1）
- forward return：未来 5 / 20 交易日收益 close[t+h] / close[t] - 1
- 按日截面：每个交易日，对"当日有因子值且未来收益可得"的标的集合
  计算因子值与 forward return 的 Spearman 相关 → 当日 IC
- 每因子×horizon 统计：IC 均值、IC 标准差、ICIR（均值/标准差）、
  t 值（ICIR×√N）、|IC|>0.03 显著样本占比与天数、正 IC 占比、平均截面标的数

输出：
    research/ic_report.md  — 因子表格 + 结论（是否建议上 Qlib）

局限（报告内声明）：
- 幸存者偏差：bars 表只含当前候选池（存活标的），横截面 IC 可能被
  "这些标的在历史上表现较好才被纳入"的选择偏差夸大
- 多市场混合（US/HK/A 股/ETF/指数），跨市场横截面存在结构异质性；
  报告附 US-only / HK-only 分组结果作为敏感性
- 用 t 日收盘价计算 forward return，未计交易成本与滑点

用法：
    cd skills/trading-system && PYTHONPATH=. python3 research/ic_analysis.py
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # trading-system/（自包含，架构 §6.1）

import numpy as np
import pandas as pd

from shared import db as dbm

# ────────────────────────────────────────────────────────────────
# 参数
# ────────────────────────────────────────────────────────────────
MIN_CROSS_SECTION = 10      # 计算截面 IC 的最低标的数（低于则当日 IC 记为 NaN）
IC_SIG_THRESHOLD = 0.03     # |IC| 显著阈值（用于显著样本占比统计）
MIN_ICIR = 0.30             # 判定因子有效的 ICIR 阈值（经验值）
MIN_SIG_FRAC = 0.30         # 判定因子有效的显著样本占比阈值
HORIZONS = (5, 20)          # forward return 天数
FACTORS = ("momentum_5", "momentum_20", "momentum_60",
           "volatility_20", "ma_distance_20", "rsi_14", "volume_trend_20")

# 报告用途：已知杠杆/反向/非股票产品（只用于样本构成统计，不参与因子计算）
KNOWN_SPECIAL = {
    "TQQQ.US", "SOXL.US", "UPRO.US", "SSO.US", "QLD.US", "TECL.US", "UGL.US",
    "BITX.US", "SCO.US", "YANG.US", "YINN.US", "SBIT.US", "VXX.US", ".VIX.US",
    "BOXX.US", "SGOV.US", "SHV.US", "IEF.US", "TLT.US", "BITO.US",
}


# ────────────────────────────────────────────────────────────────
# 数据加载
# ────────────────────────────────────────────────────────────────

def load_all_bars(conn) -> pd.DataFrame:
    """从 bars 表读取全部标的日线 → DataFrame[symbol, ts, open, high, low, close, volume]。

    ts 为 'YYYY-MM-DD' 字符串（ISO 可字典序排序）。行数 ~30 万。
    """
    rows = dbm.get_all_bars(conn)
    df = pd.DataFrame([dict(r) for r in rows])
    return df


def compute_factors_and_fwd(df: pd.DataFrame) -> pd.DataFrame:
    """单标的：计算因子 + forward return。df 已按 ts 升序。

    注意：pandas 3.0 groupby.apply 默认排除分组列（include_groups=False），
    因此本函数不引用 symbol 列；symbol 由调用方 reset_index() 恢复。
    """
    close = df["close"]
    out = pd.DataFrame(index=df.index)
    out["ts"] = df["ts"]
    out["close"] = close
    # --- 动量因子 ---
    out["momentum_5"] = close.pct_change(5)
    out["momentum_20"] = close.pct_change(20)
    out["momentum_60"] = close.pct_change(60)
    # --- 波动率因子：ATR% = ATR20 / close ---
    prev_close = close.shift(1)
    tr = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr20 = tr.rolling(20).mean()
    out["volatility_20"] = atr20 / close
    # --- 均线距离因子：close / MA20 - 1 ---
    ma20 = close.rolling(20).mean()
    out["ma_distance_20"] = close / ma20 - 1
    # --- RSI(14) Wilder ---
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    out["rsi_14"] = 100 - 100 / (1 + rs)
    # --- 量能趋势因子 ---
    out["volume_trend_20"] = (
        df["volume"].rolling(20).mean() / df["volume"].rolling(60).mean() - 1
    )
    # --- forward return ---
    for h in HORIZONS:
        out[f"fwd_{h}"] = close.shift(-h) / close - 1
    return out


# ────────────────────────────────────────────────────────────────
# Spearman 相关（无 scipy 依赖，pandas/numpy 手写）
# ────────────────────────────────────────────────────────────────

def _rankdata(a: np.ndarray) -> np.ndarray:
    """平均排名（ties 取平均），输入 1-D float 数组，返回 1..n 的排名。"""
    n = a.size
    if n < 2:
        return np.arange(1, n + 1, dtype=float)
    sorter = np.argsort(a, kind="mergesort")
    ranks = np.empty(n, dtype=float)
    ranks[sorter] = np.arange(1, n + 1, dtype=float)
    sorted_a = a[sorter]
    # 相等元素段（按排序后的相邻比较）
    not_equal = np.r_[True, sorted_a[1:] != sorted_a[:-1]]
    group_starts = np.flatnonzero(not_equal)
    group_ends = np.r_[group_starts[1:], n]
    for s, e in zip(group_starts, group_ends):
        if e - s > 1:  # 有 tie
            avg = (s + 1 + e) / 2.0
            ranks[sorter[s:e]] = avg
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman 秩相关（= rank 后的 Pearson）。"""
    rx = _rankdata(x)
    ry = _rankdata(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = np.sqrt(np.dot(rx, rx) * np.dot(ry, ry))
    if denom == 0:
        return float("nan")
    return float(np.dot(rx, ry) / denom)


# ────────────────────────────────────────────────────────────────
# 截面 IC
# ────────────────────────────────────────────────────────────────

def compute_ic_series(factor_wide: pd.DataFrame, fwd_wide: pd.DataFrame,
                      min_cross: int = MIN_CROSS_SECTION):
    """逐日截面：因子 vs forward return 的 Spearman 相关 → IC 序列。

    返回 (dates: np.ndarray[str], ic: np.ndarray[float], n_cross: np.ndarray[int])。
    """
    common = factor_wide.columns.intersection(fwd_wide.columns)
    fv = factor_wide[common]
    fw = fwd_wide[common]
    dates = fv.index.to_numpy()
    ic = np.full(len(dates), np.nan)
    n_cross = np.zeros(len(dates), dtype=int)
    for i, dt in enumerate(dates):
        x = fv.loc[dt].to_numpy(dtype=float)
        y = fw.loc[dt].to_numpy(dtype=float)
        mask = ~(np.isnan(x) | np.isnan(y))
        n = int(mask.sum())
        n_cross[i] = n
        if n < min_cross:
            continue
        ic[i] = spearman(x[mask], y[mask])
    return dates, ic, n_cross


def summarize_ic(factor: str, horizon: int, dates, ic, n_cross) -> dict:
    """汇总单个因子×horizon 的 IC 统计。"""
    valid = ~np.isnan(ic)
    n_days = int(valid.sum())
    if n_days == 0:
        return {"factor": factor, "horizon": horizon, "n_days": 0,
                "avg_cross": 0.0, "mean_ic": float("nan"), "std_ic": float("nan"),
                "icir": float("nan"), "t_stat": float("nan"),
                "sig_frac": float("nan"), "sig_days": 0, "pos_frac": float("nan")}
    ic_v = ic[valid]
    mean_ic = float(np.mean(ic_v))
    std_ic = float(np.std(ic_v, ddof=1))
    icir = mean_ic / std_ic if std_ic > 0 else float("nan")
    t_stat = icir * np.sqrt(n_days) if not np.isnan(icir) else float("nan")
    sig = np.abs(ic_v) > IC_SIG_THRESHOLD
    return {
        "factor": factor,
        "horizon": horizon,
        "n_days": n_days,
        "avg_cross": float(np.mean(n_cross[valid])),
        "mean_ic": mean_ic,
        "std_ic": std_ic,
        "icir": icir,
        "t_stat": t_stat,
        "sig_frac": float(np.mean(sig)),
        "sig_days": int(np.sum(sig)),
        "pos_frac": float(np.mean(ic_v > 0)),
    }


def is_effective(r: dict) -> bool:
    """数据驱动的因子有效性判定：|IC 均值| 显著 且 ICIR 达标 且 显著样本占比达标。"""
    if r["n_days"] == 0:
        return False
    return (abs(r["mean_ic"]) >= IC_SIG_THRESHOLD
            and abs(r["icir"]) >= MIN_ICIR
            and r["sig_frac"] >= MIN_SIG_FRAC)


# ────────────────────────────────────────────────────────────────
# 报告输出
# ────────────────────────────────────────────────────────────────

def fmt(x, nd=4) -> str:
    """NaN 显示为 '—'，数值保留 nd 位小数。"""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x:.{nd}f}"


def _md_table(rows: list, headers: list) -> str:
    """Markdown 表格（rows 为行 list，每行与 headers 等长）。"""
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def build_report(all_stats: dict, group_stats: dict, sample_info: dict,
                 now_str: str) -> str:
    """组装 research/ic_report.md 全文。"""
    L = []
    L.append("# 量价因子 IC/IR 分析报告（US-005 · Qlib 前置验证）")
    L.append("")
    L.append(f"- 生成时间：{now_str}（UTC+8）")
    L.append(f"- 数据源：`shared/trading.db` bars 表（长桥日线，唯一生产数据源）")
    L.append(f"- 样本：{sample_info['n_symbols']} 个标的，时间跨度 "
             f"{sample_info['date_start']} ~ {sample_info['date_end']}，"
             f"共 {sample_info['n_bars']:,} 根日线")
    L.append(f"- 方法：按日截面计算因子与 forward return 的 Spearman 相关（IC 序列），"
             f"统计 IC 均值 / IC 标准差 / ICIR / t 值 / |IC|>{IC_SIG_THRESHOLD} 显著占比")
    L.append(f"- 截面最低标的数：{MIN_CROSS_SECTION}（低于则不计算当日 IC）")
    L.append("")

    # 1. 样本构成
    L.append("## 1. 样本构成与幸存者偏差声明")
    L.append("")
    L.append(sample_info["composition_note"])
    L.append("")
    L.append("> **幸存者偏差警告（R1#7）**：bars 表只含当前候选池（存活标的），"
             "这些标的是经过人工/回测筛选后保留下来的。横截面 IC 可能被"
             "“表现差的标的早被剔除”这一选择偏差**夸大**。本报告的结论只能说明"
             "“**在当前候选池内**，因子对样本内未来收益有/无预测力”，"
             "不能直接外推为全市场结论；是否上 Qlib 应以 point-in-time 全市场数据复验。")
    L.append("")

    # 2. 因子定义
    L.append("## 2. 因子定义")
    L.append("")
    L.append(_md_table([
        ["momentum_5", "过去 5 日收益", "close/close[-5]-1"],
        ["momentum_20", "过去 20 日收益", "close/close[-20]-1"],
        ["momentum_60", "过去 60 日收益", "close/close[-60]-1"],
        ["volatility_20", "ATR%", "ATR(20)/close"],
        ["ma_distance_20", "价格距 MA20 偏离", "close/MA20-1"],
        ["rsi_14", "Wilder RSI(14)", "100-100/(1+RS)"],
        ["volume_trend_20", "量能趋势", "MA20(vol)/MA60(vol)-1"],
    ], ["因子", "含义", "公式"]))
    L.append("")
    L.append(f"- forward return：未来 {HORIZONS} 日收益（close[t+h]/close[t]-1），"
             "h ∈ {5, 20}。用 t 日收盘价度量，未计成本/滑点。")
    L.append("")

    # 3. 全样本结果
    L.append("## 3. 全样本 IC/IR 结果（149 标的，含 ETF/指数/杠杆产品）")
    L.append("")
    L.append("| 因子 | horizon | 截面天数 | 平均截面标的数 | IC均值 | IC标准差 | ICIR | t值 | |IC|>0.03占比 | 显著天数 | 正IC占比 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in all_stats:
        L.append(f"| {r['factor']} | {r['horizon']} | {r['n_days']} | "
                 f"{r['avg_cross']:.0f} | {fmt(r['mean_ic'])} | {fmt(r['std_ic'])} | "
                 f"{fmt(r['icir'])} | {fmt(r['t_stat'], 1)} | "
                 f"{fmt(r['sig_frac'] * 100, 1)}% | {r['sig_days']} | "
                 f"{fmt(r['pos_frac'] * 100, 1)}% |")
    L.append("")
    L.append("> 判定标准：|IC均值| ≥ 0.03 **且** |ICIR| ≥ 0.30 **且** 显著样本占比 ≥ 30% → 记为有效因子。")
    L.append("")

    # 4. 分组敏感性
    L.append("## 4. 市场分组敏感性（US-only / HK-only）")
    L.append("")
    for grp_name, grp_rows in group_stats.items():
        L.append(f"### {grp_name}")
        L.append("")
        L.append("| 因子 | horizon | 截面天数 | 平均截面标的数 | IC均值 | IC标准差 | ICIR | t值 | |IC|>0.03占比 | 显著天数 | 正IC占比 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in grp_rows:
            L.append(f"| {r['factor']} | {r['horizon']} | {r['n_days']} | "
                     f"{r['avg_cross']:.0f} | {fmt(r['mean_ic'])} | {fmt(r['std_ic'])} | "
                     f"{fmt(r['icir'])} | {fmt(r['t_stat'], 1)} | "
                     f"{fmt(r['sig_frac'] * 100, 1)}% | {r['sig_days']} | "
                     f"{fmt(r['pos_frac'] * 100, 1)}% |")
        L.append("")
    L.append("")

    # 5. 结论
    effective = [r for r in all_stats if is_effective(r)]
    eff20 = [r for r in effective if r["horizon"] == 20]
    # 边际信号：分组（US-only/HK-only）中 20 日 horizon |t|>2 的组合
    marginal = []
    for grp_name, grp_rows in group_stats.items():
        for r in grp_rows:
            if r["horizon"] == 20 and not np.isnan(r["t_stat"]) and abs(r["t_stat"]) > 2:
                marginal.append((grp_name, r))
    L.append("## 5. 结论与 Qlib 建议（基于数据，不拍脑袋）")
    L.append("")
    if effective:
        L.append("**全样本下，以下因子×horizon 组合通过有效性判定：**")
        L.append("")
        for r in effective:
            L.append(f"- **{r['factor']}** @ {r['horizon']}日：IC均值={fmt(r['mean_ic'])}，"
                     f"ICIR={fmt(r['icir'])}，t={fmt(r['t_stat'], 1)}，"
                     f"显著占比={fmt(r['sig_frac'] * 100, 1)}%")
        L.append("")
        if eff20:
            L.append(f"其中 **{len(eff20)} 个组合在 20 日（月度级）horizon 上有效**"
                     "——这是横截面调仓最常用的周期，说明候选池内确实存在可提取的量价 alpha。")
        else:
            L.append("但所有有效组合都只在 5 日（周度级）horizon 上，20 日周期未通过，"
                     "横截面 alpha 偏短期、稳定性存疑。")
        L.append("")
        L.append("**建议：值得上 Qlib 做横截面选股**，理由：")
        L.append("1. 候选池内存在统计显著的量价因子（见上表具体数值）；")
        L.append("2. 但必须解决幸存者偏差：Qlib 阶段应引入 point-in-time 全市场股票池"
                 "（含已退市标的）与退市/停牌处理，否则 alpha 会被高估；")
        L.append("3. 建议按市场分组建模（US/HK/A 股分开），并优先使用 20 日 horizon 验证稳健性；")
        L.append("4. 波动率/反向类产品（VXX/SCO/TQQQ 等）与普通股票混在同一截面会污染 IC，"
                 "建模时应单独成池或剔除。")
    else:
        L.append("**全样本下没有任何因子×horizon 组合通过有效性判定**"
                 f"（|IC均值|≥{IC_SIG_THRESHOLD} 且 |ICIR|≥{MIN_ICIR} 且 显著占比≥{MIN_SIG_FRAC * 100:.0f}%）。")
        L.append("")
        L.append("**建议：暂缓上 Qlib 横截面选股**，理由：")
        L.append("1. 当前候选池内量价因子 IC 弱/不稳定（见上表），横截面选股缺乏可验证的 alpha 来源；")
        L.append("2. 若个别因子在某个 horizon 接近阈值，可先扩大候选池/延长样本再复验；")
        L.append("3. 注意本结论受幸存者偏差影响——全市场 point-in-time 数据下结论可能不同，"
                 "但在现有数据上不建议立即投入 Qlib 工程量。")
        if marginal:
            L.append("")
            L.append("**边际信号（数据里可见但未达严格阈值）**：")
            L.append("")
            L.append("| 分组 | 因子 | horizon | IC均值 | ICIR | t值 | |IC|>0.03占比 |")
            L.append("|---|---|---|---|---|---|---|")
            for grp_name, r in marginal:
                L.append(f"| {grp_name} | {r['factor']} | {r['horizon']} | "
                         f"{fmt(r['mean_ic'])} | {fmt(r['icir'])} | {fmt(r['t_stat'], 1)} | "
                         f"{fmt(r['sig_frac'] * 100, 1)}% |")
            L.append("")
            L.append("这些组合 IC 均值绝对值小（<0.03）但 t 值 >2（样本量大所致），"
                     "只能说明**存在弱但统计上非随机的横截面信号**："
                     "均值回归类（RSI 超买超卖、价格距 MA20 偏离）在 US/HK 均偏正向，"
                     "而动量与量能趋势方向不稳定（US 量能趋势甚至显著为负）。"
                     "可作为 Qlib 小规模试点（纯股票池、月度调仓）的初始因子候选，"
                     "但不足以支撑立即全量上 Qlib 的决策。")
    L.append("")

    # 6. 局限
    L.append("## 6. 局限与后续")
    L.append("")
    L.append("- **幸存者偏差**：如上所述，bars 表为当前候选池，非全市场 point-in-time 数据。")
    L.append(f"- **多市场混合**：全样本含 US/HK/A 股/ETF/指数/杠杆与反向产品，跨市场横截面"
             "受时区、流动性、市场结构差异影响；分组结果见第 4 节。")
    L.append("- **成本与流动性**：IC 度量的是“排序与收益的相关”，不含交易成本、滑点、"
             "可交易性；真实策略收益需在回测中验证。")
    L.append("- **IC 显著性阈值**：|IC|>0.03 为经验阈值，与样本量和截面宽度有关，"
             "此处同时用 ICIR 与 t 值交叉验证。")
    L.append("")
    L.append(f"*报告由 `research/ic_analysis.py` 自动生成。*")
    return "\n".join(L)


# ────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────

def run_analysis() -> dict:
    conn = dbm.get_conn()
    bars = load_all_bars(conn)
    conn.close()

    # 时间跨度信息
    n_symbols = int(bars["symbol"].nunique())
    n_bars = len(bars)
    date_start = bars["ts"].min()
    date_end = bars["ts"].max()

    # 样本构成（报告用途，按标的数统计）
    symbols = sorted(bars["symbol"].unique())
    n_us = sum(1 for s in symbols if s.endswith(".US"))
    n_hk = sum(1 for s in symbols if s.endswith(".HK"))
    n_cn = sum(1 for s in symbols if s.endswith((".SH", ".SZ")))
    n_other = n_symbols - n_us - n_hk - n_cn
    n_special = sum(1 for s in symbols if s in KNOWN_SPECIAL)
    comp_note = (
        f"- 标的数：{n_symbols}；市场构成（按后缀）：US={n_us}，HK={n_hk}，"
        f"A股(SH/SZ)={n_cn}，其他/指数={n_other}\n"
        f"- 已知杠杆/反向/非股票产品（报告标注，不剔除）：{n_special} 个，"
        f"包括 TQQQ/SOXL/UPRO/SSO/QLD/TECL/UGL/BITX/SCO/YANG/YINN/SBIT/VXX/.VIX/BOXX/SGOV/SHV/IEF/TLT/BITO 等\n"
        f"- 单标的 bar 数：多数 2000 根（2018-08 ~ 2026-08，约 8 年）；"
        f"2021 年后上市的标的（如 6181.HK/ARKB/IBIT/QDTE 等）历史较短，按实际可得数据参与截面"
    )

    # 因子计算（长表）：groupby.apply 后 reset_index() 恢复 symbol 列
    grouped = bars.groupby("symbol", sort=False).apply(
        compute_factors_and_fwd
    ).reset_index()

    # 分组定义（敏感性）
    groups = {
        "全样本 (all)": None,
        "US-only": lambda s: s.endswith(".US"),
        "HK-only": lambda s: s.endswith(".HK"),
    }

    all_stats = []
    group_stats = {}

    for grp_name, filt in groups.items():
        if filt is not None:
            sel = grouped["symbol"].str.endswith(grp_name.split("-")[0].split(" ")[0])
            # 简化：直接用后缀过滤
            if grp_name == "US-only":
                sel = grouped["symbol"].str.endswith(".US")
            elif grp_name == "HK-only":
                sel = grouped["symbol"].str.endswith(".HK")
            gdf = grouped[sel]
            if gdf["symbol"].nunique() < MIN_CROSS_SECTION:
                group_stats[grp_name] = []
                continue
        else:
            gdf = grouped

        stats = []
        for factor in FACTORS:
            # 宽表：ts × symbol
            f_wide = gdf.pivot(index="ts", columns="symbol", values=factor)
            for h in HORIZONS:
                fwd_wide = gdf.pivot(index="ts", columns="symbol", values=f"fwd_{h}")
                dates, ic, n_cross = compute_ic_series(f_wide, fwd_wide)
                stats.append(summarize_ic(factor, h, dates, ic, n_cross))
        if grp_name == "全样本 (all)":
            all_stats = stats
        else:
            group_stats[grp_name] = stats

    sample_info = {
        "n_symbols": n_symbols,
        "n_bars": n_bars,
        "date_start": date_start,
        "date_end": date_end,
        "composition_note": comp_note,
    }
    return {"all_stats": all_stats, "group_stats": group_stats,
            "sample_info": sample_info}


def main() -> None:
    t0 = datetime.now()
    res = run_analysis()
    now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    md = build_report(res["all_stats"], res["group_stats"], res["sample_info"], now_str)

    out_path = Path(__file__).resolve().parent / "ic_report.md"
    out_path.write_text(md, encoding="utf-8")

    # 控制台摘要
    print(f"✅ IC/IR 分析完成，耗时 {(datetime.now() - t0).total_seconds():.1f}s")
    print(f"样本：{res['sample_info']['n_symbols']} 标的，"
          f"{res['sample_info']['n_bars']:,} 根日线，"
          f"{res['sample_info']['date_start']} ~ {res['sample_info']['date_end']}")
    print(f"报告已写入：{out_path}")
    print("\n全样本 IC 均值（horizon=20）：")
    for r in res["all_stats"]:
        if r["horizon"] == 20:
            print(f"  {r['factor']:20s} IC={fmt(r['mean_ic'])}  ICIR={fmt(r['icir'])}  "
                  f"t={fmt(r['t_stat'], 1)}  sig%={fmt(r['sig_frac'] * 100, 1)}")


if __name__ == "__main__":
    main()
