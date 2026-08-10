"""Agent/供应商无关的数据提供器契约与基本面适配器。

基本面分成两条互不混用的通道：

* ``CurrentFundamentalProvider`` 只服务“现在这家公司怎么样”的分析；
* ``FundamentalPITProvider`` 只服务历史回测，必须提供当时可见时间。

当前快照即使来自可信供应商也不能自动变成 PIT 数据。这个边界用于阻止
look-ahead bias，而不是评价供应商的数据质量。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, runtime_checkable


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ProviderCapabilities:
    """提供器能力声明；调用方不得从数据形状猜测 PIT 能力。"""

    current_snapshot: bool
    historical_reports: bool
    pit_metadata: bool
    point_in_time_safe: bool


@dataclass(frozen=True)
class FundamentalSnapshot:
    symbol: str
    source: str
    observed_at: str
    values: Dict
    capabilities: ProviderCapabilities
    period_end: Optional[str] = None
    published_at: Optional[str] = None
    available_at: Optional[str] = None

    @property
    def pit_safe(self) -> bool:
        return bool(
            self.capabilities.point_in_time_safe
            and self.period_end and self.published_at and self.available_at
        )

    def to_dict(self) -> Dict:
        result = asdict(self)
        result["pit_safe"] = self.pit_safe
        return result


@dataclass(frozen=True)
class FundamentalFetchResult:
    snapshots: List[FundamentalSnapshot]
    warnings: List[str]

    def to_dict(self) -> Dict:
        return {
            "snapshots": [item.to_dict() for item in self.snapshots],
            "warnings": list(self.warnings),
        }


@runtime_checkable
class MarketDataProvider(Protocol):
    def daily_bars(self, symbol: str, count: int) -> Iterable[Dict]: ...


@runtime_checkable
class CurrentFundamentalProvider(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def current_snapshot(self, symbol: str) -> FundamentalSnapshot: ...


@runtime_checkable
class FundamentalPITProvider(Protocol):
    """历史因子接口；每行必须有 period/published/available 三个时间。"""

    name: str
    capabilities: ProviderCapabilities

    def financial_snapshots(self, symbol: str, as_of: str) -> Iterable[Dict]: ...


@runtime_checkable
class CorporateActionProvider(Protocol):
    def corporate_actions(self, symbol: str, as_of: str) -> Iterable[Dict]: ...


@runtime_checkable
class TradingCalendarProvider(Protocol):
    def trading_calendar(self, market: str, start, end) -> Iterable[Dict]: ...


class OpenAliceCommandProvider:
    """OpenAlice/TraderHub 的可选 JSON-stdio 桥接器。

    TradingCat 不绑定 OpenAlice 的进程形态。配置的命令从 stdin 收到
    ``{"operation":"current_fundamentals","symbol":"AAPL.US"}``，并向
    stdout 返回 JSON 对象（也接受 TradingCat envelope 中的 ``data``）。命令
    通过 argv 直接执行，永不经过 shell。

    OpenAlice 当前财务数据没有可复现的历史可见时间，因此能力固定为非 PIT。
    """

    name = "openalice"
    capabilities = ProviderCapabilities(
        current_snapshot=True,
        historical_reports=False,
        pit_metadata=False,
        point_in_time_safe=False,
    )

    def __init__(self, command: Sequence[str], *, timeout: float = 30.0,
                 runner: Callable = subprocess.run,
                 clock: Callable[[], str] = _utc_now):
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("OpenAlice adapter command 不能为空")
        self.command = tuple(command)
        self.timeout = timeout
        self.runner = runner
        self._clock = clock

    @classmethod
    def from_env(cls) -> Optional["OpenAliceCommandProvider"]:
        from shared.env import load_selected
        load_selected(["TRADINGCAT_OPENALICE_ADAPTER_COMMAND"])
        raw = os.environ.get("TRADINGCAT_OPENALICE_ADAPTER_COMMAND", "").strip()
        return cls(shlex.split(raw)) if raw else None

    def current_snapshot(self, symbol: str) -> FundamentalSnapshot:
        request = json.dumps({
            "schema_version": "tradingcat.provider.v1",
            "operation": "current_fundamentals",
            "symbol": symbol,
        }, ensure_ascii=False)
        completed = self.runner(
            list(self.command), input=request, text=True,
            capture_output=True, timeout=self.timeout, check=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or "adapter exited non-zero").strip()
            raise RuntimeError(f"OpenAlice adapter 失败: {message[:300]}")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenAlice adapter 未返回有效 JSON") from exc
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError(f"OpenAlice adapter 返回失败: {payload.get('error')}")
        values = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(values, dict):
            raise RuntimeError("OpenAlice adapter data 必须是 JSON object")
        return FundamentalSnapshot(
            symbol=symbol, source=self.name, observed_at=self._clock(),
            values=values, capabilities=self.capabilities,
        )


class FundamentalProviderChain:
    """聚合当前快照并保留逐来源 lineage；单一来源故障时安全降级。"""

    def __init__(self, providers: Iterable[CurrentFundamentalProvider]):
        self.providers = tuple(providers)

    def current(self, symbol: str) -> FundamentalFetchResult:
        snapshots: List[FundamentalSnapshot] = []
        warnings: List[str] = []
        for provider in self.providers:
            try:
                snapshot = provider.current_snapshot(symbol)
                if snapshot.pit_safe:
                    warnings.append(
                        f"{provider.name} 当前快照即使含时间字段也未自动写入 PIT 库")
                snapshots.append(snapshot)
            except Exception as exc:
                warnings.append(f"基本面来源 {provider.name} 不可用: {exc}")
        if not snapshots:
            warnings.append("没有可用的当前基本面来源；技术面研究仍可继续")
        return FundamentalFetchResult(snapshots, warnings)


def validate_pit_snapshot(row: Dict) -> Dict:
    """验证外部历史财报行；缺少 PIT 元数据时 fail closed。"""

    required = ("period_end", "published_at", "available_at", "values", "source")
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError("PIT 基本面缺少字段: " + ", ".join(missing))
    return row
