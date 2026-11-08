"""Keeps the tests away from the real list of recently used files."""

import pytest


@pytest.fixture(autouse=True)
def own_recent_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
