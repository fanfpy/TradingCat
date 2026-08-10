"""PIT 因子快照与策略适配报告。

这里只做可解释的特征注册和研究建议；没有通过 Walk-Forward/Holdout 的候选不会
被描述为可交易策略。
"""

import json
import math
from dataclasses import asdict, dataclass
from typing import Dict, List

from shared import db as dbm
from shared.feature_lab import compute_features, momentum


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    category: str
    lookback: str
    direction: str
    pit_required: bool


FACTOR_REGISTRY = (
    FactorDefinition("momentum_20", "technical", "20d", "higher_is_stronger", False),
    FactorDefinition("momentum_60", "technical", "60d", "higher_is_stronger", False),
    FactorDefinition("momentum_120", "technical", "120d", "higher_is_stronger", False),
    FactorDefinition("volatility_20", "risk", "20d", "lower_is_safer", False),
    FactorDefinition("ma_distance_20", "technical", "20d", "signed", False),
    FactorDefinition("rsi_14", "technical", "14d", "bounded_0_100", False),
    FactorDefinition("adx", "technical", "14d", "higher_is_trending", False),
    FactorDefinition("median_dollar_volume_20", "liquidity", "20d", "higher_is_liquid", False),
    FactorDefinition("fundamental_snapshot", "fundamental", "reported", "provider_defined", True),
)


def _technical(bars) -> Dict:
    closes = [float(row["close"]) for row in bars]
    highs = [float(row["high"]) for row in bars]
    lows = [float(row["low"]) for row in bars]
    volumes = [float(row["volume"]) for row in bars]
    features = compute_features(closes, highs, lows, volumes)[-1]
    features["momentum_120"] = momentum(closes, 120)[-1]
    dollar = sorted(c * v for c, v in zip(closes[-20:], volumes[-20:]))
    features["median_dollar_volume_20"] = dollar[len(dollar) // 2] if dollar else None
    features["annualized_volatility_20"] = features["volatility_20"] * math.sqrt(252)
    return features


def analyze_factor_snapshot(conn, symbol: str, as_of: str) -> Dict:
    bars = dbm.get_bars(conn, symbol, end=as_of)
    technical = _technical(bars) if len(bars) >= 121 else None
    fundamental_rows = dbm.fundamentals_as_of(conn, symbol, as_of)
    fundamentals: List[Dict] = []
    for row in fundamental_rows:
        fundamentals.append({
            "period_end": row["period_end"], "published_at": row["published_at"],
            "available_at": row["available_at"], "revision": row["revision"],
            "source": row["source"], "values": json.loads(row["payload_json"]),
        })

    suitability = []
    if technical is not None:
        trend_score = sum((technical["momentum_20"] > 0,
                           technical["momentum_60"] > 0,
                           technical["momentum_120"] > 0,
                           technical["adx"] >= 20)) / 4
        suitability.append({
            "strategy_family": "trend_following",
            "descriptive_score": trend_score,
            "research_readiness": "READY_FOR_BACKTEST",
            "reasons": ["20/60/120 日动量与 ADX 的描述性匹配；尚非交易资格"],
        })
        mean_reversion_score = min(1.0, abs(technical["rsi_14"] - 50.0) / 30.0)
        suitability.append({
            "strategy_family": "mean_reversion",
            "descriptive_score": mean_reversion_score,
            "research_readiness": "READY_FOR_BACKTEST",
            "reasons": ["RSI 偏离仅用于生成研究假设；必须独立回测"],
        })
    suitability.append({
        "strategy_family": "fundamental_quality_value",
        "descriptive_score": None,
        "research_readiness": "READY_FOR_BACKTEST" if fundamentals else "DATA_MISSING",
        "reasons": (["PIT 财报快照可用；仍需定义并验证因子"] if fundamentals else
                    ["缺少 as-of 时点可见的 PIT 财报，禁止用当前基本面回填历史"]),
    })
    return {
        "as_of": as_of,
        "registry": [asdict(item) for item in FACTOR_REGISTRY],
        "technical": technical,
        "fundamental": fundamentals or None,
        "fundamental_status": "AVAILABLE" if fundamentals else "MISSING",
        "strategy_suitability": suitability,
    }

