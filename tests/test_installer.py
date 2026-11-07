"""Tests for the Linux installer: the self-extracting .run file installs and uninstalls for one user."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_installer  # noqa: E402
from build_installer import LinuxPackager, Release, Stage  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the .run installer is a POSIX shell script")


@pytest.fixture
def run_file(tmp_path, monkeypatch):
    monkeypatch.setattr(build_installer, "DIST", tmp_path / "dist")

    stage = Stage(tmp_path / "stage")
    stage.copy_extension()
    stage.server.parent.mkdir(parents=True)
    stage.server.write_text("#!/bin/sh\necho fake server\n")

    return LinuxPackager(Release("v9.9.9"), stage).build()


def install(run_file: Path, home: Path, *args: str) -> str:
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": "", "PATH": os.environ["PATH"]}
    result = subprocess.run(["sh", str(run_file), *args], env=env, capture_output=True, text=True, check=True)

    return result.stdout


def test_run_file_is_named_and_stamped_with_the_version(run_file):
    assert run_file.name.startswith("inkscape-claude-9.9.9-linux-")
    assert "Inkscape Claude 9.9.9 installer" in run_file.read_bytes()[:200].decode()


def test_installs_extension_and_server_for_every_inkscape(run_file, tmp_path):
    home = tmp_path / "home"
    (home / ".var/app/org.inkscape.Inkscape").mkdir(parents=True)

    install(run_file, home)

    for extensions in (home / ".config/inkscape/extensions",
                       home / ".var/app/org.inkscape.Inkscape/config/inkscape/extensions"):
        assert (extensions / "claude/ask_claude.inx").exists()
        assert (extensions / "claude/inkscape_mcp/server.py").exists()

    server = home / ".local/bin/inkscape-mcp"
    assert subprocess.run([str(server)], capture_output=True, text=True).stdout == "fake server\n"


def test_reinstall_replaces_old_files_and_uninstall_removes_them(run_file, tmp_path):
    home = tmp_path / "home"
    install(run_file, home)
    stale = home / ".config/inkscape/extensions/claude/old.py"
    stale.write_text("")

    install(run_file, home)
    assert not stale.exists()

    install(run_file, home, "--uninstall")
    assert not (home / ".config/inkscape/extensions/claude").exists()
    assert not (home / ".local/bin/inkscape-mcp").exists()
