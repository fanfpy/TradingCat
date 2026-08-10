#!/usr/bin/env python3
"""
AlphaModel — Alpha 信号生成抽象（架构 v4.0 研究层）
=====================================================
架构 §5 D-2：AlphaModel "⚠️ 半成品（现为入场模式）" 补全。

D-2 设计分层：
- RuleBasedAlpha：现有入场模式（pullback/breakout/hybrid/momentum/donchian/golden_cross）
- QlibAlphaModel（P4 未来实现）：Qlib 评分 → TopN 候选 → 进现有 pipeline

AlphaModel 职责：
- 消费 FeatureLab 输出的特征 + 原始 OHLCV
- 判断第 i 根 bar 是否触发入场信号
- 输出 AlphaSignal（symbol/premise/rule/entry_mode/feature_snapshot）

不改 backtest.py 的 check_entry——RuleBasedAlpha 内部委托 check_entry，
保持向后兼容。新写法可用 AlphaModel 协议；旧回测引擎走 check_entry。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable


@dataclass
class AlphaSignal:
    """Alpha 信号（AlphaModel 输出）。"""
    symbol: str
    premise: str          # 策略前提描述
    rule: str             # 入场模式名
    entry_idx: int        # 触发 bar 索引
    feature_snapshot: Dict = field(default_factory=dict)


@runtime_checkable
class AlphaModel(Protocol):
    """Alpha 信号生成接口。"""
    def generate(self, symbol: str, closes: List[float], mas: List[float],
                 slopes: List[float], **kwargs) -> List[AlphaSignal]:
        ...


class RuleBasedAlpha:
    """现有入场模式的 AlphaModel 封装（D-2 RuleBasedAlpha）。

    内部委托 backtest.check_entry，不重复实现入场逻辑。
    entry_mode 列表 = pullback / breakout / hybrid / momentum / donchian / golden_cross
    """

    def __init__(self, entry_mode: str = "momentum", buffer: float = 0.02,
                 ma_period: int = 20):
        self.entry_mode = entry_mode
        self.buffer = buffer
        self.ma_period = ma_period

    def generate(self, symbol: str, closes: List[float], mas: List[float],
                 slopes: List[float], **kwargs) -> List[AlphaSignal]:
        from shared.backtest import check_entry
        fast_ma = kwargs.get("fast_ma")
        features = kwargs.get("features") or []
        start_idx = max(0, int(kwargs.get("start_idx", 0)))
        end_idx = min(len(closes), int(kwargs.get("end_idx", len(closes))))
        signals = []
        for i in range(start_idx, end_idx):
            if check_entry(closes, mas, slopes, i, self.entry_mode, self.buffer,
                           fast_ma=fast_ma, ma_period=self.ma_period):
                snapshot = dict(features[i]) if i < len(features) else {}
                snapshot.update({
                    "close": closes[i], "ma": mas[i],
                    "slope": slopes[i], "buffer": self.buffer,
                })
                signals.append(AlphaSignal(
                    symbol=symbol,
                    premise=f"rule-based {self.entry_mode}",
                    rule=self.entry_mode,
                    entry_idx=i,
                    feature_snapshot=snapshot,
                ))
        return signals


# ───────── 冒烟测试 ─────────

if __name__ == "__main__":
    from shared.indicators import sma, ma_slope

    # 造上升趋势
    closes = [100.0 + i * 0.5 for i in range(300)]
    mas = sma(closes, 50)
    slopes = ma_slope(mas, 5)

    model = RuleBasedAlpha(entry_mode="momentum", buffer=0.0, ma_period=50)
    assert isinstance(model, AlphaModel), "RuleBasedAlpha 应满足 AlphaModel Protocol"

    signals = model.generate("TEST.US", closes, mas, slopes)
    assert len(signals) > 0, "强趋势应有入场信号"
    assert signals[0].rule == "momentum"
    assert signals[0].symbol == "TEST.US"
    print(f"signals: {len(signals)} 笔，首笔 idx={signals[0].entry_idx}")
    print("alpha_model.py 冒烟测试通过 ✅")
