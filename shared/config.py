"""Typed runtime configuration loaded from config.yaml."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CONFIG = Path(__file__).with_name("default_config.yaml")


@dataclass(frozen=True)
class ResearchConfig:
    grid: str = "full"
    min_bars: int = 630
    prefilter_min_bars: int = 504


@dataclass(frozen=True)
class MonitorConfig:
    scope: str = "portfolio"
    critical_distance_pct: float = 1.0


@dataclass(frozen=True)
class DatabaseConfig:
    core_path: str = str(PROJECT_ROOT / "shared" / "trading.db")
    execution_path: str = str(PROJECT_ROOT / "shared" / "execution.db")


@dataclass(frozen=True)
class QuotaConfig:
    longbridge_daily: int = 1000


@dataclass(frozen=True)
class ReportConfig:
    directory: str = str(PROJECT_ROOT / "reports")
    backup_directory: str = str(PROJECT_ROOT / "data" / "backups")


@dataclass(frozen=True)
class IntegrationConfig:
    webhook_url: str = ""
    notification_webhook: str = ""
    openalice_adapter_command: str = ""


@dataclass(frozen=True)
class TradingCatConfig:
    research: ResearchConfig
    monitor: MonitorConfig
    database: DatabaseConfig
    quota: QuotaConfig
    report: ReportConfig
    integrations: IntegrationConfig


def _absolute(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def load_config(path: Optional[str] = None,
                environ: Optional[Mapping[str, str]] = None) -> TradingCatConfig:
    env = os.environ if environ is None else environ
    explicit = path or env.get("TRADINGCAT_CONFIG")
    project_config = PROJECT_ROOT / "config.yaml"
    selected = Path(explicit or (
        project_config if project_config.is_file() else BUNDLED_CONFIG))
    selected = selected.expanduser().resolve()
    raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    base = selected.parent

    research_raw = raw.get("research", {})
    monitor_raw = raw.get("monitor", {})
    database_raw = raw.get("database", {})
    quota_raw = raw.get("quota", {})
    report_raw = raw.get("report", {})

    grid = str(research_raw.get("grid", "full"))
    if grid not in ("full", "small", "adx"):
        raise ValueError("research.grid 必须是 full/small/adx")
    scope = str(monitor_raw.get("scope", "portfolio"))
    if scope not in ("portfolio", "watchlist"):
        raise ValueError("monitor.scope 必须是 portfolio/watchlist")

    core_path = (env.get("TRADING_CORE_DB") or env.get("TRADING_DB")
                 or database_raw.get("core_path", "shared/trading.db"))
    execution_path = (env.get("TRADING_EXECUTION_DB")
                      or database_raw.get("execution_path", "shared/execution.db"))
    runtime_root = env.get("TRADINGCAT_RUNTIME_DIR")
    report_dir = (env.get("TRADINGCAT_REPORTS_DIR")
                  or report_raw.get("directory", "reports"))
    backup_dir = (env.get("TRADINGCAT_BACKUP_DIR")
                  or report_raw.get("backup_directory", "data/backups"))
    if runtime_root:
        runtime = Path(runtime_root).expanduser()
        report_dir = env.get("TRADINGCAT_REPORTS_DIR", str(runtime / "reports"))
        backup_dir = env.get("TRADINGCAT_BACKUP_DIR", str(runtime / "backups"))

    return TradingCatConfig(
        research=ResearchConfig(
            grid=grid,
            min_bars=int(research_raw.get("min_bars", 630)),
            prefilter_min_bars=int(research_raw.get("prefilter_min_bars", 504)),
        ),
        monitor=MonitorConfig(
            scope=scope,
            critical_distance_pct=float(
                monitor_raw.get("critical_distance_pct", 1.0)),
        ),
        database=DatabaseConfig(
            core_path=_absolute(str(core_path), base),
            execution_path=_absolute(str(execution_path), base),
        ),
        quota=QuotaConfig(
            longbridge_daily=int(quota_raw.get("longbridge_daily", 1000))),
        report=ReportConfig(
            directory=_absolute(str(report_dir), base),
            backup_directory=_absolute(str(backup_dir), base),
        ),
        integrations=IntegrationConfig(
            webhook_url=env.get("TRADINGCAT_WEBHOOK_URL", "").strip(),
            notification_webhook=env.get(
                "TRADINGCAT_NOTIFICATION_WEBHOOK", "").strip(),
            openalice_adapter_command=env.get(
                "TRADINGCAT_OPENALICE_ADAPTER_COMMAND", "").strip(),
        ),
    )


def get_config() -> TradingCatConfig:
    return load_config()