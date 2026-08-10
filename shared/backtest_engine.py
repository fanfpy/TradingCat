"""可替换的研究回测引擎接口。

NativeBacktestEngine 是稳定默认实现。引擎在一个标的的 Walk-Forward 开始前只
计算一次 FeatureLab，随后将同一份 point-in-time 特征交给每个 AlphaModel。
"""

from typing import Dict, List, Optional, Protocol, runtime_checkable

from shared.alpha_model import AlphaModel
from shared.feature_lab import compute_features


@runtime_checkable
class BacktestEngine(Protocol):
    """Walk-Forward 可消费的最小回测引擎协议。"""

    def prepare(self, closes: List[float], highs: List[float], lows: List[float],
                volumes: Optional[List[float]] = None) -> List[Dict]:
        ...

    def run(self, symbol: str, ts: List[str], opens: List[float],
            highs: List[float], lows: List[float], closes: List[float],
            params: Dict, **kwargs):
        ...


class NativeBacktestEngine:
    """现有事件驱动回测器的 v4 适配器。"""

    name = "native"

    def __init__(self, alpha_model: Optional[AlphaModel] = None):
        self.alpha_model = alpha_model

    def prepare(self, closes: List[float], highs: List[float], lows: List[float],
                volumes: Optional[List[float]] = None) -> List[Dict]:
        return compute_features(closes, highs, lows, volumes)

    def run(self, symbol: str, ts: List[str], opens: List[float],
            highs: List[float], lows: List[float], closes: List[float],
            params: Dict, **kwargs):
        from shared.backtest import run_backtest
        kwargs.setdefault("alpha_model", self.alpha_model)
        return run_backtest(
            symbol, ts, opens, highs, lows, closes, params, **kwargs,
        )


class VectorbtBacktestEngine:
    """可选 vectorbt 批量信号扫描器，不进入生产 Walk-Forward 精算。"""

    name = "vectorbt"

    def __init__(self, vectorbt_module=None):
        if vectorbt_module is None:
            try:
                import vectorbt as vectorbt_module
            except ImportError as exc:
                raise RuntimeError("vectorbt 未安装；请安装 research 可选依赖") from exc
        self.vbt = vectorbt_module

    def prepare(self, closes: List[float], highs: List[float], lows: List[float],
                volumes: Optional[List[float]] = None) -> List[Dict]:
        return compute_features(closes, highs, lows, volumes)

    def scan(self, closes: List[float], entries: List[bool], exits: List[bool],
             cost_bps: float = 25.0, initial_cash: float = 100_000.0):
        """快速扫描显式 entry/exit 矩阵，返回 vectorbt Portfolio。

        结果仅用于探索；吊灯止损、跳空和 STP-LMT 的最终审计必须重跑 Native。
        """
        if not (len(closes) == len(entries) == len(exits)):
            raise ValueError("closes/entries/exits 长度必须一致")
        return self.vbt.Portfolio.from_signals(
            closes, entries=entries, exits=exits,
            init_cash=initial_cash, fees=cost_bps / 10_000.0,
        )

    def run(self, *args, **kwargs):
        raise RuntimeError(
            "VectorbtBacktestEngine 只允许 scan；Walk-Forward 最终精算必须用 Native")
