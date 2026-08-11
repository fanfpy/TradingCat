from pathlib import Path

import pytest

from shared import config as config_module
from shared.config import load_config


def test_config_yaml_is_runtime_source(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "research:\n  grid: small\n  min_bars: 700\n  prefilter_min_bars: 510\n"
        "monitor:\n  scope: watchlist\n  critical_distance_pct: 2.5\n"
        "database:\n  core_path: core.db\n  execution_path: execution.db\n"
        "quota:\n  longbridge_daily: 77\n"
        "report:\n  directory: reports\n  backup_directory: backups\n",
        encoding="utf-8",
    )

    config = load_config(str(path), environ={})

    assert config.research.grid == "small"
    assert config.research.min_bars == 700
    assert config.monitor.scope == "watchlist"
    assert config.monitor.critical_distance_pct == 2.5
    assert config.quota.longbridge_daily == 77
    assert config.database.core_path == str(tmp_path / "core.db")


def test_environment_only_overrides_deployment_paths(tmp_path):
    config = load_config(environ={
        "TRADING_CORE_DB": str(tmp_path / "core.db"),
        "TRADING_EXECUTION_DB": str(tmp_path / "execution.db"),
        "TRADINGCAT_REPORTS_DIR": str(tmp_path / "reports"),
    })

    assert config.database.core_path == str(tmp_path / "core.db")
    assert config.database.execution_path == str(tmp_path / "execution.db")
    assert config.report.directory == str(tmp_path / "reports")
    assert config.research.grid == "full"


def test_installed_package_falls_back_to_bundled_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    runtime = tmp_path / "runtime"
    config = load_config(environ={"TRADINGCAT_RUNTIME_DIR": str(runtime)})
    assert config.research.min_bars == 630
    assert config.monitor.scope == "portfolio"
    assert config.database.core_path == str(runtime / "trading.db")
    assert config.database.execution_path == str(runtime / "execution.db")
    assert config.report.directory == str(runtime / "reports")
    assert config.report.backup_directory == str(runtime / "backups")


def test_explicit_database_env_wins_over_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path / "missing")
    runtime = tmp_path / "runtime"
    explicit = tmp_path / "explicit-core.db"
    config = load_config(environ={
        "TRADINGCAT_RUNTIME_DIR": str(runtime),
        "TRADING_CORE_DB": str(explicit),
    })
    assert config.database.core_path == str(explicit)
    assert config.database.execution_path == str(runtime / "execution.db")


def test_explicit_project_database_paths_win_over_runtime(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "database:\n  core_path: state/core.db\n"
        "  execution_path: state/execution.db\n",
        encoding="utf-8",
    )
    config = load_config(
        str(config_path),
        environ={"TRADINGCAT_RUNTIME_DIR": str(tmp_path / "runtime")},
    )
    assert config.database.core_path == str(tmp_path / "state" / "core.db")
    assert config.database.execution_path == str(
        tmp_path / "state" / "execution.db")


def test_fallback_paths_are_distinct_and_outside_bundled_package(
        monkeypatch, tmp_path):
    package_root = tmp_path / "site-packages"
    monkeypatch.setattr(config_module, "PROJECT_ROOT", package_root)
    runtime = tmp_path / "user-state"
    monkeypatch.setattr(
        config_module, "_writable_runtime_fallback", lambda env: runtime)

    config = load_config(environ={})

    assert config.database.core_path != config.database.execution_path
    assert Path(config.database.core_path).parent == runtime
    assert Path(config.database.execution_path).parent == runtime
    assert package_root not in Path(config.database.core_path).parents


def test_same_core_and_execution_path_is_rejected(tmp_path):
    shared_path = tmp_path / "same.db"
    with pytest.raises(ValueError, match="必须不同"):
        load_config(environ={
            "TRADING_CORE_DB": str(shared_path),
            "TRADING_EXECUTION_DB": str(shared_path),
        })