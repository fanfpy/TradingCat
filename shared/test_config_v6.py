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
    config = load_config(environ={})
    assert config.research.min_bars == 630
    assert config.monitor.scope == "portfolio"