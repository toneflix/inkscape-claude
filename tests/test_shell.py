"""Tests for live control through Inkscape's shell, which works without D-Bus. The integration tests need a display:

    xvfb-run -a python -m pytest tests/test_shell.py
"""

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.live import LiveError, LiveInkscape  # noqa: E402
from inkscape_mcp.shell import ShellInkscape, ShellWindow, live_control  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402

DOC = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
       '<rect id="r1" x="10" y="10" width="80" height="80" fill="red"/>'
       '<circle id="c1" cx="90" cy="90" r="40" fill="blue"/></svg>')

HAS_DISPLAY = bool(shutil.which("inkscape") and (sys.platform != "linux" or os.environ.get("DISPLAY")))
needs_window = pytest.mark.skipif(not HAS_DISPLAY, reason="needs Inkscape and a display")


def can_rebase() -> bool:
    return any(name == "file-rebase" for name, _ in Inkscape().action_list())


def test_live_control_choice():
    assert isinstance(live_control(Inkscape(), "shell"), ShellInkscape)
    assert isinstance(live_control(Inkscape(), "dbus"), LiveInkscape)


def test_macos_and_windows_use_dbus_when_inkscape_is_on_the_bus(monkeypatch):
    monkeypatch.delenv("INKSCAPE_MCP_LIVE", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(LiveInkscape, "find_gdbus", staticmethod(lambda inkscape: "/opt/homebrew/bin/gdbus"))

    monkeypatch.setattr(LiveInkscape, "available", lambda self: True)
    assert isinstance(live_control(Inkscape()), LiveInkscape)

    monkeypatch.setattr(LiveInkscape, "available", lambda self: False)
    assert isinstance(live_control(Inkscape()), ShellInkscape)

    monkeypatch.setattr(LiveInkscape, "find_gdbus", staticmethod(lambda inkscape: None))
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(live_control(Inkscape()), ShellInkscape)


def test_gdbus_is_found_next_to_inkscape(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    (tmp_path / "inkscape.exe").write_text("")
    (tmp_path / "gdbus.exe").write_text("")

    assert LiveInkscape.find_gdbus(Inkscape(str(tmp_path / "inkscape.exe"))) == str(tmp_path / "gdbus.exe")


def test_attaching_without_dbus_says_the_chat_opened_its_own_window(tmp_path, monkeypatch):
    from inkscape_mcp import chat

    document = tmp_path / "drawing.svg"
    document.write_text(DOC)
    monkeypatch.setattr(ShellInkscape, "open", lambda self, path: 1)
    monkeypatch.setattr(chat, "live_control", lambda inkscape: ShellInkscape(inkscape))

    session = chat.ChatSession.open(chat.parse_args([str(document), "--attach"]))

    assert "opened the drawing in a new window" in session.transcript[0]["text"]


def test_needs_a_window_opened_by_claude():
    live = ShellInkscape(Inkscape())

    assert not live.available()
    with pytest.raises(LiveError, match="live_open"):
        live.run(["select-all"])


@pytest.fixture
def shell_window(tmp_path):
    document = tmp_path / "live.svg"
    document.write_text(DOC)
    live = ShellInkscape(Inkscape(), timeout=60)

    window = live.open(document)

    yield live, document, window

    live.close()


@needs_window
def test_actions_show_in_the_window_and_save_to_the_file(shell_window, tmp_path):
    live, document, window = shell_window

    assert live.open(document) == window, "the same file reuses its window"

    live.run(["select-by-id:r1,c1", "path-union"])
    shown = SvgDocument.load(live.export(tmp_path / "shown.svg"))

    assert shown.has("r1") and not shown.has("c1")
    assert SvgDocument.load(document).has("c1"), "the file is unchanged until saved"
    assert live.export(tmp_path / "snap.png", "png", width=100).stat().st_size > 0

    live.save()

    assert not SvgDocument.load(document).has("c1")


@needs_window
def test_reload_shows_file_edits(shell_window, tmp_path):
    live, document, window = shell_window
    if not can_rebase():
        with pytest.raises(LiveError, match="1.3"):
            live.reload()
        return

    on_disk = SvgDocument.load(document)
    on_disk.update("r1", attributes={"fill": "purple"})
    on_disk.add('<rect id="added" width="5" height="5"/>')
    on_disk.save(document)

    live.reload()
    shown = SvgDocument.load(live.export(tmp_path / "shown.svg"))

    assert shown.find("r1").get("fill") == "purple"
    assert shown.has("added")


@needs_window
def test_closing_saves_and_ends_the_window(shell_window):
    live, document, window = shell_window

    live.run(["select-by-id:c1", "delete-selection"])
    live.close()

    assert not live.available()
    assert not SvgDocument.load(document).has("c1")


@needs_window
def test_chat_keeps_its_own_window_in_sync(shell_window, tmp_path):
    from inkscape_mcp.chat import ChatSession
    from test_agent import ScriptedClient, done, tool_call

    live, document, window = shell_window
    if not can_rebase():
        pytest.skip("reloading needs Inkscape 1.3 or later")

    live.run(["select-by-id:c1", "object-flip-horizontal"])
    client = ScriptedClient([tool_call("a", "update_element", element_id="r1", style={"fill": "orange"}),
                             done("Made the square orange."), done("Nothing to change.")])
    session = ChatSession(document, client, Inkscape(), live, window)

    reply = session.send("Make the square orange")
    time.sleep(1)

    shown = SvgDocument.load(live.export(tmp_path / "shown.svg"))
    assert reply["text"] == "Made the square orange."
    assert "fill:orange" in shown.find("r1").get("style", ""), "window reloaded with Claude's edit"
    assert shown.find("c1").get("transform"), "the unsaved flip was saved before Claude edited"
    assert reply["selection"] == ["c1"], "the circle was still selected in the window"
    assert "Selected in Inkscape: c1" in client.calls[0][0]["content"][-1]["text"]
    assert "data-claude-selected" not in document.read_text() + shown.to_string()

    assert session.send("Anything else?")["selection"] == [], "the reload cleared the selection"


def test_actions_with_separators_are_refused():
    window = ShellWindow.__new__(ShellWindow)

    with pytest.raises(LiveError, match="';'"):
        window.send(["select-all;delete"])


def test_macos_finds_the_bus_from_the_setup_script(tmp_path, monkeypatch):
    import inkscape_mcp.live as live_module

    socket = tmp_path / "bus-{uid}"
    monkeypatch.setattr(live_module, "MACOS_BUS", str(socket))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert LiveInkscape.environment() is None, "no bus socket yet"

    Path(str(socket).format(uid=os.getuid())).write_text("")

    assert LiveInkscape.environment()["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={str(socket).format(uid=os.getuid())}"

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/elsewhere")
    assert LiveInkscape.environment() is None, "an address the process already has wins"
