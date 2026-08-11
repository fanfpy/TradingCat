"""Typed runtime configuration loaded from config.yaml."""

import os
import sys
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
    core_path: str
    execution_path: str


@dataclass(frozen=True)
class QuotaConfig:
    longbridge_daily: int = 1000


@dataclass(frozen=True)
class ReportConfig:
    directory: str
    backup_directory: str


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


def _writable_runtime_fallback(env: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or env.get("APPDATA") or Path.home())
        return base / "TradingCat"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TradingCat"
    base = Path(env.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "tradingcat"


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
    is_bundled = selected == BUNDLED_CONFIG.resolve()

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

    runtime = Path(env["TRADINGCAT_RUNTIME_DIR"]).expanduser().resolve() \
        if env.get("TRADINGCAT_RUNTIME_DIR") else _writable_runtime_fallback(env)
    project_core = None if is_bundled else database_raw.get("core_path")
    project_execution = None if is_bundled else database_raw.get("execution_path")
    project_reports = None if is_bundled else report_raw.get("directory")
    project_backups = None if is_bundled else report_raw.get("backup_directory")

    core_path = env.get("TRADING_CORE_DB") or env.get("TRADING_DB")
    execution_path = env.get("TRADING_EXECUTION_DB")
    core_path = (_absolute(str(core_path), Path.cwd()) if core_path else
                 _absolute(str(project_core), base) if project_core else
                 str(runtime / "trading.db"))
    execution_path = (_absolute(str(execution_path), Path.cwd()) if execution_path else
                      _absolute(str(project_execution), base) if project_execution else
                      str(runtime / "execution.db"))
    if os.path.normcase(os.path.realpath(core_path)) == os.path.normcase(
            os.path.realpath(execution_path)):
        raise ValueError("database core_path 与 execution_path 必须不同")

    report_dir = env.get("TRADINGCAT_REPORTS_DIR")
    backup_dir = env.get("TRADINGCAT_BACKUP_DIR")
    report_dir = (_absolute(str(report_dir), Path.cwd()) if report_dir else
                  _absolute(str(project_reports), base) if project_reports else
                  str(runtime / "reports"))
    backup_dir = (_absolute(str(backup_dir), Path.cwd()) if backup_dir else
                  _absolute(str(project_backups), base) if project_backups else
                  str(runtime / "backups"))

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
            core_path=core_path,
            execution_path=execution_path,
        ),
        quota=QuotaConfig(
            longbridge_daily=int(quota_raw.get("longbridge_daily", 1000))),
        report=ReportConfig(
            directory=report_dir,
            backup_directory=backup_dir,
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