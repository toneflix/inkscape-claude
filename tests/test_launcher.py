"""Tests for the chat launcher used by Extensions > Claude > Open Claude Chat."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))

from inkscape_mcp.claude import Config  # noqa: E402
from inkscape_mcp.launcher import ChatLauncher, LaunchError  # noqa: E402


def fake_chat(tmp_path: Path, body: str) -> Path:
    package = tmp_path / "fake" / "inkscape_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "chat.py").write_text(body)

    return package.parent


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "path", staticmethod(lambda: tmp_path / "config" / "config.json"))
    return tmp_path / "config"


def test_a_chat_that_fails_at_start_shows_its_log(tmp_path, config_dir):
    paths = fake_chat(tmp_path, "import sys\nprint('starting', sys.argv[1:])\nsys.exit('GTK exploded')\n")
    launcher = ChatLauncher(tmp_path / "drawing.svg", [str(paths)], cwd=tmp_path, python=sys.executable)

    with pytest.raises(LaunchError) as error:
        launcher.launch()

    message = str(error.value)
    assert "exit code 1" in message
    assert "GTK exploded" in message
    assert "--attach" in message
    assert str(config_dir / "chat.log") in message


def test_a_chat_that_keeps_running_is_left_running(tmp_path, config_dir):
    paths = fake_chat(tmp_path, "import time\nprint('up')\ntime.sleep(30)\n")
    launcher = ChatLauncher(tmp_path / "drawing.svg", [str(paths)], cwd=tmp_path, python=sys.executable)
    launcher.startup_seconds = 1

    process = launcher.launch()
    try:
        assert process.poll() is None
        assert (config_dir / "chat.log").read_text().strip() == "up"
    finally:
        process.kill()
        process.wait()


def test_windowless_python_is_only_used_on_windows(tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("")
    (tmp_path / "pythonw.exe").write_text("")

    expected = str(tmp_path / "pythonw.exe") if sys.platform == "win32" else str(python)
    assert ChatLauncher.windowless_python(str(python)) == expected


def test_a_drawing_that_was_never_saved_opens_as_a_draft(tmp_path, monkeypatch, config_dir):
    pytest.importorskip("inkex")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))
    import claude_chat

    launched = []
    monkeypatch.setattr(ChatLauncher, "launch", lambda self: launched.append(self))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.setenv("DOCUMENT_PATH", "")
    source = tmp_path / "from-inkscape.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect id="r" width="4" '
                      'height="4"/></svg>')

    claude_chat.ClaudeChat().run([str(source)], output=open(tmp_path / "out.svg", "wb"))

    [launcher] = launched
    assert launcher.draft and "--draft" in launcher.command and "--attach" not in launcher.command
    assert launcher.document.parent == tmp_path / "home" / "InkscapeClaude"
    assert launcher.document.name.startswith("Untitled ") and 'id="r"' in launcher.document.read_text()


def test_draft_names_do_not_clash(tmp_path, monkeypatch):
    from inkscape_mcp import launcher

    monkeypatch.setattr(launcher.time, "strftime", lambda pattern: "Untitled 2026-11-08 052100.svg")
    first = ChatLauncher.draft_path(tmp_path)
    first.write_text("")

    second = ChatLauncher.draft_path(tmp_path)

    assert second != first and second.name.endswith("(2).svg")


def test_the_chat_says_where_a_draft_was_saved(tmp_path, config_dir):
    from inkscape_mcp.chat import ChatSession, parse_args
    from inkscape_mcp.svgdoc import SvgDocument

    draft = tmp_path / "Untitled.svg"
    SvgDocument.blank(100, 100).save(draft)

    session = ChatSession.open(parse_args([str(draft), "--draft", "--no-live"]))

    assert session.transcript[-1]["text"].startswith(f"Your drawing had not been saved, so the chat saved it as {draft}")
