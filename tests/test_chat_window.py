"""Tests for the desktop chat window. Needs GTK 3 bindings and a display, e.g. xvfb-run -a python -m pytest."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inkscape_mcp.chat import ChatSession  # noqa: E402
from inkscape_mcp.chat_window import ChatWindow  # noqa: E402
from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402
from test_agent import ScriptedClient, done, tool_call  # noqa: E402

pytestmark = pytest.mark.skipif(not ChatWindow.supported(), reason="needs GTK 3 and a display")


def texts(chat: ChatWindow) -> list[str]:
    from gi.repository import Gtk

    found = []

    def walk(widget):
        if isinstance(widget, Gtk.Label) and widget.get_style_context().has_class("steps") is False:
            found.append(widget.get_text())
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                walk(child)

    walk(chat.messages)

    return found


def wait_until_idle(chat: ChatWindow, timeout: float = 60) -> None:
    from gi.repository import Gtk

    deadline = time.monotonic() + timeout
    while not chat.send_button.get_sensitive() and time.monotonic() < deadline:
        Gtk.main_iteration_do(False)
        time.sleep(0.01)
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


@pytest.fixture
def chat(tmp_path):
    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    client = ScriptedClient([
        tool_call("a", "add_elements", svg_fragment='<rect id="box" width="50" height="50" fill="teal"/>'),
        done("Added a teal box."),
    ])
    window = ChatWindow(ChatSession(path, client, Inkscape(), live=None))
    window.window.show_all()

    yield window, path

    window.window.destroy()


def test_send_shows_the_answer_and_a_preview(chat):
    from gi.repository import Gtk

    window, path = chat

    window.prompt.get_buffer().set_text("Add a box")
    window.send()
    wait_until_idle(window)

    shown = texts(window)
    images = [w for row in window.messages.get_children() for bubble in row.get_children()
              for w in bubble.get_children() if isinstance(w, Gtk.Image)]

    assert "Add a box" in shown and "Added a teal box." in shown
    assert len(images) == 1, "a preview of the drawing follows the answer"
    assert SvgDocument.load(path).has("box")
    assert window.prompt.get_buffer().get_char_count() == 0
    assert "Not synced" in window.badge.get_text()


def test_undo_reverts_and_errors_show_in_the_chat(chat):
    window, path = chat

    window.prompt.get_buffer().set_text("Add a box")
    window.send()
    wait_until_idle(window)
    window.undo()
    wait_until_idle(window)

    assert not SvgDocument.load(path).has("box")
    assert "Undid the last change." in texts(window)

    window.undo()
    wait_until_idle(window)

    assert texts(window)[-1] == "Error: Nothing to undo."
