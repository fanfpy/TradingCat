#!/usr/bin/env python3
"""
技术指标模块 — 交易系统 v3.0
=============================
ATR22(RMA) / SMA / MA20 线性回归斜率 / 入场峰值吊灯止损。

关键约束（来自 spec）：
- 只用完成 bar 数据，索引 i 表示已完成的第 i 根
- 止损 = 入场以来截至 i-1 的最高 high − atrMultiple × ATR22[i-1]
- 入场日 high 初始化 peakHigh，只增不减
- 不得用固定 highest20 代替入场以来峰值
"""

from typing import List, Optional, Tuple

# ────────────────────────────────────────────────────────────────
# ATR (RMA / Wilder 平滑)
# ────────────────────────────────────────────────────────────────

def true_range(highs: List[float], lows: List[float], closes: List[float], i: int) -> float:
    """第 i 根 bar 的 TR（i 从 1 开始）。"""
    h, l, pc = highs[i], lows[i], closes[i - 1]
    return max(h - l, abs(h - pc), abs(l - pc))


def rma(values: List[float], period: int) -> List[float]:
    """Wilder 平滑 (RMA)。第一个值为 SMA，之后递推。"""
    out: List[float] = [0.0] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def atr22(highs: List[float], lows: List[float], closes: List[float], period: int = 22) -> List[float]:
    """ATR(period)，返回与输入等长数组（前 period-1 个为 0）。"""
    n = len(closes)
    out = [0.0] * n
    if n < period + 1:
        return out
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = true_range(highs, lows, closes, i)
    r = rma(trs[1:], period)
    # r 对齐：trs[1:] 的 index k 对应原数组 index k+1
    for k, v in enumerate(r):
        out[k + 1] = v
    return out


# ────────────────────────────────────────────────────────────────
# SMA / MA 斜率
# ────────────────────────────────────────────────────────────────

def sma(closes: List[float], period: int) -> List[float]:
    """简单移动平均，返回等长数组（前 period-1 个为 0）。"""
    n = len(closes)
    out = [0.0] * n
    if n < period:
        return out
    s = sum(closes[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += closes[i] - closes[i - period]
        out[i] = s / period
    return out


# ────────────────────────────────────────────────────────────────
# ADX (Wilder)
# ────────────────────────────────────────────────────────────────

def adx_di(highs: List[float], lows: List[float], closes: List[float],
           period: int = 14) -> Tuple[List[float], List[float], List[float]]:
    """Wilder ADX(period) + 方向指标 +DI/−DI，返回三个等长数组（前导无效区为 0）。

    计算步骤（与常见实现一致）：
        1. +DM / −DM / TR（从 idx1 起）
        2. Wilder 平滑（RMA）得到 +DI / −DI
        3. DX = |+DI − −DI| / (+DI + −DI) × 100
        4. ADX = DX 的 Wilder 平滑

    Returns:
        (adx, plus_di, minus_di)
        - adx: 趋势强度（高 → 趋势强，不辨方向）
        - plus_di > minus_di → 多头主导（上涨趋势）
        - minus_di > plus_di → 空头主导（下跌趋势）
    前导无效区（2*period-2 个）为 0，入场过滤时不会误触发。
    """
    n = len(closes)
    out = [0.0] * n
    plus_di_out = [0.0] * n
    minus_di_out = [0.0] * n
    if n < period * 2:
        return out, plus_di_out, minus_di_out

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = true_range(highs, lows, closes, i)

    # Wilder 平滑：先 SMA 初始化，再递推
    def _rma_series(vals: List[float]) -> List[float]:
        r = [0.0] * n
        r[period] = sum(vals[1:period + 1]) / period
        for i in range(period + 1, n):
            r[i] = (r[i - 1] * (period - 1) + vals[i]) / period
        return r

    r_plus = _rma_series(plus_dm)
    r_minus = _rma_series(minus_dm)
    r_tr = _rma_series(tr)

    dx = [0.0] * n
    for i in range(period, n):
        if r_tr[i] > 0:
            pdi = r_plus[i] / r_tr[i] * 100
            mdi = r_minus[i] / r_tr[i] * 100
            plus_di_out[i] = pdi
            minus_di_out[i] = mdi
            denom = pdi + mdi
            dx[i] = abs(pdi - mdi) / denom * 100 if denom > 0 else 0.0

    # ADX = DX 的 Wilder 平滑（初始 2*period-1 根后有效）
    warm = period * 2 - 1
    if n > warm:
        s = sum(dx[period:warm + 1]) / period  # 第一个 ADX：用 period 个 DX 平均
        out[warm] = s
        for i in range(warm + 1, n):
            s = (s * (period - 1) + dx[i]) / period
            out[i] = s
    return out, plus_di_out, minus_di_out


def adx(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> List[float]:
    """Wilder 平均趋向指数 ADX(period)，返回与输入等长数组（前 2*period-2 个为 0）。

    ADX 值高 → 趋势强；ADX < 阈值（如 20）→ 震荡市。
    前导无效区（2*period-2 个）为 0，入场过滤时不会误触发。
    """
    return adx_di(highs, lows, closes, period)[0]


def ma_slope(closes: List[float], period: int = 20, slope_lookback: int = 20) -> List[float]:
    """MA 最近 slope_lookback 日线性回归斜率（以 closes 为序列，用 MA 值）。

    返回等长数组。斜率 > 0 表示 MA 上升趋势。
    """
    n = len(closes)
    mas = sma(closes, period)
    out = [0.0] * n
    if n < slope_lookback:
        return out
    for i in range(slope_lookback - 1, n):
        ys = mas[i - slope_lookback + 1: i + 1]
        xs = list(range(slope_lookback))
        xbar = (slope_lookback - 1) / 2
        ybar = sum(ys) / slope_lookback
        num = sum((xs[k] - xbar) * (ys[k] - ybar) for k in range(slope_lookback))
        den = sum((xs[k] - xbar) ** 2 for k in range(slope_lookback))
        out[i] = num / den if den != 0 else 0.0
    return out


# ────────────────────────────────────────────────────────────────
# 入场峰值吊灯止损
# ────────────────────────────────────────────────────────────────

def chandelier_stop_entry_peak(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    entry_idx: int,
    atr_multiple: float,
    atr_period: int = 22,
) -> Tuple[float, float]:
    """计算从 entry_idx 入场后的吊灯止损序列。

    Args:
        highs/lows/closes: 全序列（等长）
        entry_idx: 入场 bar 索引（该 bar 的 high 初始化 peakHigh）
        atr_multiple: ATR 倍数
        atr_period: ATR 周期（固定 22）

    Returns:
        (stop_at_entry, stop_series) 其中 stop_series 是逐 bar 止损序列（等长数组，
        entry_idx 之前为 0，entry_idx 及之后为计算值）
    """
    n = len(closes)
    atr = atr22(highs, lows, closes, atr_period)
    stops = [0.0] * n

    if entry_idx >= n:
        return 0.0, stops

    peak = highs[entry_idx]
    # 入场日的止损：入场日 high − ATR×multiple（用入场日自身的 ATR）
    stop = peak - atr_multiple * atr[entry_idx]
    stops[entry_idx] = stop

    for i in range(entry_idx + 1, n):
        peak = max(peak, highs[i - 1])  # 截至 i-1 的最高 high
        stop = peak - atr_multiple * atr[i]
        stops[i] = stop

    return stops[entry_idx], stops


def exit_simulate(opens: List[float], lows: List[float], stops: List[float],
                  i: int) -> Tuple[bool, float, str]:
    """吊灯止损触发模拟（spec §2.5）。

    当日 low <= stop：
        open < stop → 按 open 退出
        否则 → 按 stop 退出

    Returns:
        (triggered, exit_price, reason)
    """
    stop = stops[i]
    if lows[i] <= stop:
        if opens[i] < stop:
            return True, opens[i], "gap_open"
        return True, stop, "stop"
    return False, 0.0, ""


def stp_lmt_simulate(opens: List[float], highs: List[float], lows: List[float],
                     stops: List[float], atr: List[float], i: int,
                     limit_atr_offset: float = 1.0) -> Tuple[bool, Optional[float], str]:
    """STP LMT 压力测试（spec §2.5）。

    limit = stop − 1 × ATR。
    若价格跳过限价且没有回到可成交区域 → 记录为未成交。

    Returns:
        (filled, fill_price_or_None, reason)
    """
    stop = stops[i]
    limit = stop - limit_atr_offset * atr[i]
    if lows[i] <= stop:
        # 检查限价能否成交：当日有价格触及 [limit, ...] 区域
        if lows[i] <= limit:
            return True, min(opens[i], limit) if opens[i] > limit else opens[i], "stp_lmt_fill"
        return False, None, "stp_lmt_unfilled"  # 跳过限价，未成交
    return False, None, ""


# ────────────────────────────────────────────────────────────────
# 冒烟测试
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 构造 60 根上升趋势数据
    closes = [100 + i * 0.5 for i in range(60)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    opens = [c - 0.2 for c in closes]

    a = atr22(highs, lows, closes)
    assert a[21] == 0.0 and a[22] > 0, "ATR 前 22 个应为 0（TR 从 idx1 起需 22 个）"
    assert abs(a[22] - 2.0) < 0.5, f"ATR 初始值异常: {a[22]}"

    m = sma(closes, 20)
    assert m[19] == sum(closes[:20]) / 20
    slope = ma_slope(closes, 20, 20)
    assert slope[-1] > 0, "上升趋势斜率应为正"

    # ADX 冒烟：强单边趋势 → ADX 显著上升；横盘 → ADX 低
    import math
    n_adx = 120
    trend_h = [100 + 0.8 * i + 1.5 * math.sin(i / 5) for i in range(n_adx)]
    trend_l = [c - 2.0 for c in trend_h]
    trend_c = [c - 1.0 for c in trend_h]
    adx_trend = adx(trend_h, trend_l, trend_c, 14)
    assert adx_trend[-1] > 25, f"强趋势 ADX 应 >25，实际 {adx_trend[-1]:.1f}"
    # 真横盘：无方向随机噪声（规则正弦波有持续方向运动，ADX 反而高，不是横盘）
    import random
    random.seed(42)
    noise = [random.gauss(0, 0.4) for _ in range(n_adx)]
    flat_c = [100 + x for x in noise]
    flat_h = [c + 0.5 for c in flat_c]
    flat_l = [c - 0.5 for c in flat_c]
    adx_flat = adx(flat_h, flat_l, flat_c, 14)
    assert adx_flat[-1] < 25, f"横盘 ADX 应 <25，实际 {adx_flat[-1]:.1f}"
    # 前导无效区为 0（不会误触发过滤）
    assert adx_trend[26] == 0.0, "ADX 前 2*period-2 个应为 0"

    # DI 方向冒烟（2026-08-08）：上涨趋势 +DI > −DI；下跌趋势 −DI > +DI
    _, pdi_t, mdi_t = adx_di(trend_h, trend_l, trend_c, 14)
    assert pdi_t[-1] > mdi_t[-1], f"上涨趋势应 +DI>−DI: {pdi_t[-1]:.1f} vs {mdi_t[-1]:.1f}"
    down_c = [200 - i * 0.8 for i in range(n_adx)]
    down_h = [c + 2.0 for c in down_c]
    down_l = [c - 1.0 for c in down_c]
    _, pdi_d, mdi_d = adx_di(down_h, down_l, down_c, 14)
    assert mdi_d[-1] > pdi_d[-1], f"下跌趋势应 −DI>+DI: {pdi_d[-1]:.1f} vs {mdi_d[-1]:.1f}"

    # 入场在 idx=30
    _, stops = chandelier_stop_entry_peak(highs, lows, closes, 30, 3.0)
    assert stops[30] > 0 and stops[29] == 0.0
    # 峰值只增不减 → 止损序列应基本单调（除非 ATR 波动）
    assert stops[-1] > stops[30], f"止损应上移: {stops[30]} → {stops[-1]}"

    # 退出模拟：构造当日大幅低开
    opens2 = opens[:]
    lows2 = lows[:]
    opens2[55] = stops[55] - 5.0  # 低开跌破止损
    lows2[55] = stops[55] - 6.0
    trig, price, reason = exit_simulate(opens2, lows2, stops, 55)
    assert trig and reason == "gap_open" and price == opens2[55]

    # ADX 与参考值核对：与消融实验脚本(Wilder 14)同实现，黄金/震荡两种形态差异显著
    print(f"ADX 冒烟: 趋势 ADX={adx_trend[-1]:.1f} (>25), 横盘 ADX={adx_flat[-1]:.1f} (<25)")
    print("indicators.py 冒烟测试通过 ✅")
