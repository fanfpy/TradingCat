"""可审计的市场/流动性交易成本模型。

输出是单边成本 bps：显式费用 + 半点差 + 市场冲击。研究阶段使用保守的
假设订单金额；下单计划阶段使用真实目标金额。模型不声称替代券商回报，成交后
仍应以实际 fill 做 TCA 校准。
"""

from dataclasses import asdict, dataclass
import math
from statistics import median
from typing import Iterable, Optional


COST_MODEL_VERSION = "liquidity-cost-v1"


@dataclass(frozen=True)
class CostEstimate:
    model_version: str
    market: str
    fee_bps: float
    half_spread_bps: float
    impact_bps: float
    total_bps_per_side: float
    median_dollar_volume: Optional[float]
    assumed_order_notional: float

    def to_dict(self):
        return asdict(self)


def _market(symbol: str) -> str:
    suffix = symbol.upper().rsplit(".", 1)[-1]
    return suffix if suffix in ("US", "HK", "SH", "SZ") else "OTHER"


def estimate_cost(symbol: str, closes: Iterable[float], volumes: Iterable[float],
                  order_notional: float = 10_000.0) -> CostEstimate:
    """按市场与成交额参与率估算单边成本，缺少成交量时保守退化为 35bps。"""
    market = _market(symbol)
    fee_bps = {"US": 2.0, "HK": 10.0, "SH": 8.0, "SZ": 8.0}.get(market, 8.0)
    half_spread_bps = {"US": 4.0, "HK": 8.0, "SH": 6.0, "SZ": 6.0}.get(market, 8.0)
    dollar_volumes = [float(c) * float(v) for c, v in zip(closes, volumes)
                      if float(c) > 0 and float(v) > 0]
    adv = median(dollar_volumes[-60:]) if dollar_volumes else None
    if adv is None or adv <= 0:
        impact_bps = max(0.0, 35.0 - fee_bps - half_spread_bps)
    else:
        participation = max(float(order_notional), 0.0) / adv
        # 平方根冲击模型；上限避免异常成交量让成本无限大。
        impact_bps = min(60.0, 80.0 * math.sqrt(participation))
    total = min(100.0, max(5.0, fee_bps + half_spread_bps + impact_bps))
    return CostEstimate(
        COST_MODEL_VERSION, market, fee_bps, half_spread_bps,
        round(impact_bps, 6), round(total, 6), adv,
        float(order_notional),
    )

