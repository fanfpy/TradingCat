"""研究、回放与生产共用的唯一策略规则实现。

本模块只判断完成 bar 上的策略语义：入场候选、ADX/DI 过滤、吊灯止损和
均线死叉。成交成本、STP-LMT 是否成交等属于回测成交模拟，不属于策略规则。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from shared.indicators import adx_di, atr22, ma_slope, sma


ATR_PERIOD = 22
MA_SLOPE_PERIOD = 20
ADX_PERIOD = 14
ADX_THRESHOLD = 20
MA_CROSS_FAST_DEFAULT = 20
MA_CROSS_SLOW_DEFAULT = 100

ENTRY_MODES = (
    "pullback", "breakout", "hybrid", "momentum", "donchian", "golden_cross",
)
EXIT_MODES = ("chandelier", "ma_cross", "chandelier_or_cross")


@dataclass(frozen=True)
class StrategyDecision:
    """单根完成 bar 上的确定性策略决策。"""

    action: str                 # ENTER | EXIT | NONE
    triggered: bool
    reason: str = ""
    reference_price: Optional[float] = None
    stop_price: Optional[float] = None


def entry_rule(closes: List[float], mas: List[float], slopes: List[float],
               i: int, entry_mode: str, buffer: float,
               fast_ma: Optional[List[float]] = None,
               ma_period: int = 20) -> bool:
    """唯一入场规则实现；兼容入口由 ``backtest.check_entry`` 委托到这里。"""
    if i < 1 or i >= len(closes):
        return False
    if entry_mode == "pullback":
        any_above = any(
            closes[j] > mas[j]
            for j in range(max(1, i - 4), i + 1)
            if mas[j] > 0
        )
        if not any_above or slopes[i] <= 0:
            return False
        return abs(closes[i] - mas[i]) / mas[i] <= buffer if mas[i] > 0 else False
    if entry_mode == "breakout":
        if mas[i - 1] <= 0:
            return False
        if closes[i - 1] >= mas[i - 1]:
            return False
        dist = (closes[i] - mas[i]) / mas[i] if mas[i] > 0 else 1.0
        return closes[i] > mas[i] or abs(dist) <= buffer
    if entry_mode == "hybrid":
        return mas[i] > 0 and abs(closes[i] - mas[i]) / mas[i] <= buffer
    if entry_mode == "momentum":
        return mas[i] > 0 and slopes[i] > 0 and closes[i] > mas[i]
    if entry_mode == "donchian":
        if i < ma_period:
            return False
        return closes[i] > max(closes[max(0, i - ma_period):i])
    if entry_mode == "golden_cross":
        if fast_ma is None:
            return False
        if min(fast_ma[i], mas[i], fast_ma[i - 1], mas[i - 1]) <= 0:
            return False
        return fast_ma[i] > mas[i] and fast_ma[i - 1] <= mas[i - 1]
    return False


def dead_cross(fast_ma: List[float], slow_ma: List[float], i: int) -> bool:
    """唯一均线死叉规则实现。"""
    if i < 1 or i >= len(fast_ma) or i >= len(slow_ma):
        return False
    if min(fast_ma[i], slow_ma[i], fast_ma[i - 1], slow_ma[i - 1]) <= 0:
        return False
    return fast_ma[i] < slow_ma[i] and fast_ma[i - 1] >= slow_ma[i - 1]


class StrategyEvaluator:
    """把冻结参数和完成 OHLC 序列编译成可逐 bar 重放的策略评估器。"""

    version = "strategy-evaluator-v1"

    def __init__(self, opens: List[float], highs: List[float], lows: List[float],
                 closes: List[float], params: Dict):
        if not (len(opens) == len(highs) == len(lows) == len(closes)):
            raise ValueError("OHLC 序列长度必须一致")
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.params = dict(params)
        self.entry_mode = self.params["entry_mode"]
        self.exit_mode = self.params.get("exit_mode", "chandelier")
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(f"未知 entry_mode: {self.entry_mode}")
        if self.exit_mode not in EXIT_MODES:
            raise ValueError(f"未知 exit_mode: {self.exit_mode}")

        self.ma_period = int(self.params["ma_period"])
        self.buffer = float(self.params["buffer"])
        self.atr_multiple = float(self.params["atr_multiple"])
        self.mas = sma(closes, self.ma_period)
        self.slopes = ma_slope(closes, self.ma_period, MA_SLOPE_PERIOD)
        self.atr = atr22(highs, lows, closes, ATR_PERIOD)

        fast_period = int(self.params.get("ma_cross_fast", MA_CROSS_FAST_DEFAULT))
        slow_period = int(self.params.get("ma_cross_slow", MA_CROSS_SLOW_DEFAULT))
        need_fast = self.entry_mode == "golden_cross" or self.exit_mode != "chandelier"
        self.fast_ma = sma(closes, fast_period) if need_fast else []
        self.slow_ma = sma(closes, slow_period) if self.exit_mode != "chandelier" else []

        self.adx_filter = bool(self.params.get("adx_filter", False))
        self.adx_direction = bool(self.params.get("adx_direction", False))
        self.adx_threshold = float(self.params.get("adx_threshold", ADX_THRESHOLD))
        if self.adx_filter or self.adx_direction:
            period = int(self.params.get("adx_period", ADX_PERIOD))
            self.adx, self.plus_di, self.minus_di = adx_di(highs, lows, closes, period)
        else:
            n = len(closes)
            self.adx = [0.0] * n
            self.plus_di = [0.0] * n
            self.minus_di = [0.0] * n

    def evaluate_entry(self, i: int,
                       candidate_override: Optional[bool] = None) -> StrategyDecision:
        """评估入场；自定义 Alpha 可提供候选，但仍必须经过统一 ADX/DI 过滤。"""
        candidate = candidate_override
        if candidate is None:
            candidate = entry_rule(
                self.closes, self.mas, self.slopes, i, self.entry_mode,
                self.buffer, fast_ma=self.fast_ma or None,
                ma_period=self.ma_period,
            )
        if not candidate:
            return StrategyDecision("NONE", False, "entry_rule_not_triggered")
        if self.adx_filter and self.adx[i] <= self.adx_threshold:
            return StrategyDecision("NONE", False, "adx_filter")
        if self.adx_direction and self.plus_di[i] <= self.minus_di[i]:
            return StrategyDecision("NONE", False, "adx_direction")
        return StrategyDecision(
            "ENTER", True, self.entry_mode, reference_price=self.closes[i],
        )

    def current_stop(self, i: int, peak_high: float) -> Optional[float]:
        if self.exit_mode == "ma_cross":
            return None
        if i < 0 or i >= len(self.closes):
            return None
        stop = float(peak_high) - self.atr_multiple * self.atr[i]
        return stop if stop > 0 else None

    def evaluate_exit(self, i: int, peak_high: float) -> StrategyDecision:
        """评估退出；combined 模式保持吊灯止损优先于死叉。"""
        if i < 1 or i >= len(self.closes):
            return StrategyDecision("NONE", False, "invalid_index")

        if self.exit_mode == "ma_cross":
            if dead_cross(self.fast_ma, self.slow_ma, i):
                return StrategyDecision(
                    "EXIT", True, "ma_cross", reference_price=self.closes[i],
                )
            return StrategyDecision("NONE", False, "ma_cross_not_triggered")

        stop = self.current_stop(i, peak_high)
        if stop is not None and self.lows[i] <= stop:
            if self.opens[i] < stop:
                return StrategyDecision(
                    "EXIT", True, "gap_open", reference_price=self.opens[i],
                    stop_price=stop,
                )
            return StrategyDecision(
                "EXIT", True, "stop", reference_price=stop, stop_price=stop,
            )
        if self.exit_mode == "chandelier_or_cross" and dead_cross(
                self.fast_ma, self.slow_ma, i):
            return StrategyDecision(
                "EXIT", True, "ma_cross", reference_price=self.closes[i],
                stop_price=stop,
            )
        return StrategyDecision("NONE", False, "exit_not_triggered", stop_price=stop)

