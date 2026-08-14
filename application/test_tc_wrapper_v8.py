"""Static contract checks for the Windows PowerShell operational wrapper."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tc_ps1_prefers_project_venv_then_checks_safe_fallbacks():
    source = (ROOT / "tc.ps1").read_text(encoding="utf-8")

    assert '".venv\\Scripts\\python.exe"' in source
    assert 'Label = "python"' in source
    assert 'Label = "py -3"' in source
    assert 'Label = "python3"' in source
    assert source.index(".venv\\Scripts\\python.exe") < source.index('Label = "python"')


def test_tc_ps1_reports_actionable_error_when_no_python_is_usable():
    source = (ROOT / "tc.ps1").read_text(encoding="utf-8")

    assert "No usable Python 3.10+ interpreter found" in source
    assert "Install Python 3.10+ or create .venv\\Scripts\\python.exe" in source
    assert "Checked:" in source
