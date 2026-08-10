import os

from shared.env import load_selected


def test_load_selected_reads_only_whitelist_and_does_not_override(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("ALLOWED=from-file\nSECRET=must-not-load\n", encoding="utf-8")
    monkeypatch.delenv("ALLOWED", raising=False)
    monkeypatch.delenv("SECRET", raising=False)
    load_selected(("ALLOWED",), str(path))
    assert os.environ["ALLOWED"] == "from-file"
    assert "SECRET" not in os.environ
    monkeypatch.setenv("ALLOWED", "process-wins")
    load_selected(("ALLOWED",), str(path))
    assert os.environ["ALLOWED"] == "process-wins"
