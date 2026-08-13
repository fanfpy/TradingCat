"""Offline tests for acceptance report subprocess handling."""

from scripts import acceptance_v5


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
