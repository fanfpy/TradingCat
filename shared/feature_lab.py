#!/usr/bin/env python3
"""
FeatureLab — 因子/特征计算（架构 v4.0 研究层）
================================================
架构 §5：FeatureLab "⚠️ 半成品（入场模式=因子，待术语重构）" 补全。

职责：从原始 OHLCV 计算特征/因子序列，供 AlphaModel 消费。
- momentum_n：N 日动量（close[i]/close[i-n]-1）
- volatility_n：N 日波动率（日收益标准差）
- ma_distance_n：收盘价距 N 日均线百分比偏离
- rsi_n：N 日 RSI
- volume_trend_n：N 日成交量趋势（当前/均值比）
- adx：ADX 趋势强度（委托 indicators.adx_di）
- atr：ATR 波动（委托 indicators.atr22）

设计原则（D-2）：
- FeatureLab 只计算特征，不做策略判断（不决定买/卖）
- 特征 → AlphaModel → SignalEngine 层级清晰
"""

from typing import List, Dict, Optional
from shared.indicators import atr22, sma, adx_di


def momentum(closes: List[float], n: int = 20) -> List[float]:
    """N 日动量（close[i]/close[i-n]-1）。前 n 个为 0。"""
    out = [0.0] * len(closes)
    for i in range(n, len(closes)):
        if closes[i - n] > 0:
            out[i] = closes[i] / closes[i - n] - 1
    return out


def volatility(closes: List[float], n: int = 20) -> List[float]:
    """N 日日收益标准差（年化需乘 sqrt(252)，此处返回原始标准差）。"""
    out = [0.0] * len(closes)
    rets = [0.0]
    for i in range(1, len(closes)):
        r = closes[i] / closes[i - 1] - 1 if closes[i - 1] > 0 else 0.0
        rets.append(r)
    for i in range(n, len(closes)):
        window = rets[i - n + 1:i + 1]
        mean = sum(window) / n
        var = sum((r - mean) ** 2 for r in window) / n
        out[i] = var ** 0.5
    return out


def ma_distance(closes: List[float], n: int = 20) -> List[float]:
    """收盘价距 N 日 SMA 的百分比偏离 (close-MA)/MA。"""
    ma = sma(closes, n)
    out = [0.0] * len(closes)
    for i in range(len(closes)):
        if ma[i] > 0:
            out[i] = (closes[i] - ma[i]) / ma[i]
    return out


def rsi(closes: List[float], n: int = 14) -> List[float]:
    """N 日 RSI（Wilder 平滑）。"""
    out = [50.0] * len(closes)
    if len(closes) < n + 1:
        return out
    gains, losses = [], []
    for i in range(1, n + 1):
        chg = closes[i] - closes[i - 1]
        gains.append(max(0, chg))
        losses.append(max(0, -chg))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    for i in range(n, len(closes)):
        if i > n:
            chg = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (n - 1) + max(0, chg)) / n
            avg_loss = (avg_loss * (n - 1) + max(0, -chg)) / n
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        out[i] = 100 - 100 / (1 + rs)
    return out


def volume_trend(volumes: List[float], n: int = 20) -> List[float]:
    """N 日成交量趋势（当前量 / N 日均量）。"""
    out = [1.0] * len(volumes)
    for i in range(n, len(volumes)):
        avg_vol = sum(volumes[i - n:i]) / n if n > 0 else 1.0
        out[i] = volumes[i] / avg_vol if avg_vol > 0 else 1.0
    return out


def compute_features(closes: List[float], highs: List[float] = None,
                     lows: List[float] = None, volumes: List[float] = None) -> List[Dict]:
    """计算全部特征序列，返回每根 bar 的特征字典列表。

    可被 AlphaModel / ic_analysis / 回测前分析消费。
    """
    n = len(closes)
    feat_momentum_20 = momentum(closes, 20)
    feat_momentum_60 = momentum(closes, 60)
    feat_vol_20 = volatility(closes, 20)
    feat_ma_dist_20 = ma_distance(closes, 20)
    feat_rsi_14 = rsi(closes, 14)
    feat_atr = atr22(highs or closes, lows or closes, closes)
    adx_vals, _, _ = adx_di(highs or closes, lows or closes, closes) if highs and lows else ([0.0]*n, [0.0]*n, [0.0]*n)
    feat_vol_trend = volume_trend(volumes, 20) if volumes else [1.0]*n

    out = []
    for i in range(n):
        out.append({
            "momentum_20": feat_momentum_20[i],
            "momentum_60": feat_momentum_60[i],
            "volatility_20": feat_vol_20[i],
            "ma_distance_20": feat_ma_dist_20[i],
            "rsi_14": feat_rsi_14[i],
            "atr": feat_atr[i],
            "adx": adx_vals[i],
            "volume_trend_20": feat_vol_trend[i],
        })
    return out


# ───────── 冒烟测试 ─────────

if __name__ == "__main__":
    # 造趋势数据
    closes = [100.0 + i * 0.5 for i in range(300)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [20_000_000.0] * 300

    feats = compute_features(closes, highs, lows, volumes)
    assert len(feats) == 300
    # 强趋势：momentum > 0, adx > 0
    assert feats[-1]["momentum_20"] > 0, "上升趋势 momentum 应 > 0"
    assert feats[-1]["adx"] > 0, "ADX 应 > 0"
    assert 0 < feats[-1]["rsi_14"] <= 100, "RSI 在 0-100"
    assert feats[-1]["ma_distance_20"] != 0, "MA distance 非零"
    print("feature_lab.py 冒烟测试通过 ✅")