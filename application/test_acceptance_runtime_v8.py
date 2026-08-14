"""Offline acceptance-script regression tests collected by the default suite."""

import json

import pytest

from scripts import acceptance_v5
from scripts import deployment_readiness as readiness


def test_run_decodes_utf8_subprocess_output_on_non_utf8_locales(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "中文输出"
        stderr = ""

    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(acceptance_v5.subprocess, "run", fake_run)

    result = acceptance_v5._run(["python", "-c", "print('ok')"])

    assert result["passed"] is True
    assert result["output_tail"] == "中文输出"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_main_configures_utf8_stdout(monkeypatch):
    calls = {}

    class Stream:
        def reconfigure(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(acceptance_v5.sys, "stdout", Stream())
    acceptance_v5._configure_output()

    assert calls == {"encoding": "utf-8", "errors": "replace"}


def test_template_requires_socket_group_and_core_read_group(tmp_path):
    template = tmp_path / "executiond.service"
    template.write_text("User=tradingcat-exec\n", encoding="utf-8")

    result = readiness._check_template(template)

    assert result["status"] == "FAIL"
    assert "EnvironmentFile=/etc/tradingcat/executiond.env" in result["missing"]


def test_non_linux_field_readiness_is_not_run(monkeypatch, tmp_path):
    template = tmp_path / "executiond.service"
    template.write_text(
        "\n".join((
            "User=tradingcat-exec", "EnvironmentFile=/etc/tradingcat/executiond.env",
            "ProtectSystem=strict", "ProtectHome=true", "NoNewPrivileges=true",
            "PrivateTmp=true", "UMask=0077", "RuntimeDirectory=tradingcat",
            "SupplementaryGroups=tradingcat-core-read tradingcat-exec-client",
            "--socket-group tradingcat-exec-client",
        )), encoding="utf-8")
    monkeypatch.setattr(readiness.platform, "system", lambda: "Windows")

    report = readiness.run_deployment_readiness(
        core_user="core", execution_user="exec", execution_read_group="readers",
        socket_client_group="clients", core_db="/core.db", execution_db="/execution.db",
        socket_path="/run/executiond.sock", core_env_file="/core.env",
        execution_env_file="/exec.env", service="executiond.service", template_path=template,
    )

    assert report["status"] == "NOT_RUN"
    assert report["checks"]["template"]["status"] == "PASS"
    assert report["checks"]["field_environment"]["status"] == "NOT_RUN"


def test_cli_returns_nonzero_and_not_run_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(readiness.platform, "system", lambda: "Windows")

    assert readiness.main([]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "NOT_RUN"


def test_acceptance_refuses_p0a_record_without_field_pass(monkeypatch):
    monkeypatch.setattr(acceptance_v5, "_run", lambda command: {"passed": True})
    monkeypatch.setattr(acceptance_v5, "diagnose_longbridge", lambda **kwargs: {
        "passed": True, "version": "4.4.3", "capabilities": {},
    })
    monkeypatch.setattr(acceptance_v5, "run_deployment_readiness", lambda **kwargs: {
        "status": "NOT_RUN", "checks": {}, "reason": "not Linux",
    })

    with pytest.raises(RuntimeError, match="现场检查 PASS"):
        acceptance_v5.main(["--no-connect", "--deployment-readiness", "--record-p0a"])


def test_systemctl_output_is_decoded_as_utf8(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stdout = "状态"
        stderr = ""

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(readiness.subprocess, "run", fake_run)
    result = readiness._systemctl(["is-active", "executiond.service"])

    assert result == ("状态", "")
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
