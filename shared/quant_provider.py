"""Longbridge Quant 可选探索适配器。

它只提供远端快速预览/交叉验证，输出永远标记为 ``research_only``。TradingCat
本地 Native 引擎的 Walk-Forward、Final Holdout、PIT 和成本压力测试仍是唯一
可以推进策略生命周期的验证器。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class QuantCapability:
    available: bool
    provider: str
    cli_path: Optional[str]
    cli_version: Optional[str]
    reason: Optional[str]
    final_validation: bool = False
    authentication: str = "oauth_device_flow_managed_by_longbridge_cli"

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class QuantPreviewResult:
    symbol: str
    start: str
    end: str
    provider: str
    research_only: bool
    performance: Dict
    report: Dict
    raw: Dict

    def to_dict(self) -> Dict:
        return asdict(self)


class LongbridgeQuantProvider:
    """通过 argv 调用官方 ``longbridge quant run``，不使用 shell。"""

    name = "longbridge_quant"

    def __init__(self, binary: str = "longbridge", *, timeout: float = 120.0,
                 runner: Callable = subprocess.run,
                 which: Callable[[str], Optional[str]] = shutil.which):
        self.binary = binary
        self.timeout = timeout
        self.runner = runner
        self.which = which

    def capability(self) -> QuantCapability:
        path = self.which(self.binary)
        if not path:
            return QuantCapability(False, self.name, None, None,
                                   "longbridge CLI 未安装")
        version = None
        try:
            checked = self.runner(
                [path, "--version"], text=True, capture_output=True,
                timeout=10, check=False)
            version = (checked.stdout or checked.stderr or "").strip() or None
            help_result = self.runner(
                [path, "quant", "run", "--help"], text=True,
                capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return QuantCapability(False, self.name, path, version, str(exc))
        if help_result.returncode != 0:
            detail = (help_result.stderr or help_result.stdout or "").strip()
            return QuantCapability(
                False, self.name, path, version,
                "当前 CLI 不支持 quant run" + (f": {detail[:180]}" if detail else ""))
        return QuantCapability(True, self.name, path, version, None)

    def run_script(self, symbol: str, start: str, end: str,
                   script: str) -> QuantPreviewResult:
        if not symbol or not start or not end or not script.strip():
            raise ValueError("symbol/start/end/script 均为必填")
        capability = self.capability()
        if not capability.available:
            raise RuntimeError(capability.reason or "Longbridge Quant 不可用")
        completed = self.runner([
            capability.cli_path, "quant", "run", symbol,
            "--start", start, "--end", end,
            "--format", "json", "--script", script,
        ], text=True, capture_output=True, timeout=self.timeout, check=False)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "quant run failed").strip()
            raise RuntimeError(
                "Longbridge Quant 预览失败；若提示登录，请显式执行官方 OAuth 流程。"
                f"详情: {message[:400]}")
        try:
            raw = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Longbridge Quant 未返回有效 JSON") from exc
        encoded_report = raw.get("report_json") if isinstance(raw, dict) else None
        if isinstance(encoded_report, str):
            try:
                report = json.loads(encoded_report)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Longbridge Quant report_json 无效") from exc
        elif isinstance(encoded_report, dict):
            report = encoded_report
        else:
            report = {}
        return QuantPreviewResult(
            symbol=symbol, start=start, end=end, provider=self.name,
            research_only=True,
            performance=report.get("performanceAll", {}),
            report=report, raw=raw,
        )
