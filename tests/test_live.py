"""Tests for live control. The integration test needs a display and a session D-Bus, e.g.

    xvfb-run -a dbus-run-session -- python -m pytest tests/test_live.py
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.live import LiveError, LiveInkscape, variant  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402

DOC = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
       '<rect id="r1" x="10" y="10" width="80" height="80" fill="red"/>'
       '<circle id="c1" cx="90" cy="90" r="40" fill="blue"/></svg>')

CAN_RUN_LIVE = (sys.platform.startswith("linux") and shutil.which("gdbus") and shutil.which("inkscape")
                and os.environ.get("DISPLAY") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def test_variant_formats_parameters():
    assert variant("", None) == "[]"
    assert variant("s", "r1,c1") == "[<'r1,c1'>]"
    assert variant("s", "it's") == "[<'it\\'s'>]"
    assert variant("s", None) == "[<''>]"
    assert variant("b", None) == "[<true>]"
    assert variant("b", "false") == "[<false>]"
    assert variant("i", "3") == "[<int32 3>]"
    assert variant("d", "1.5") == "[<double 1.5>]"

    with pytest.raises(LiveError):
        variant("(ii)", "1")


def test_unavailable_without_running_inkscape(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")

    live = LiveInkscape(Inkscape(), timeout=5)

    assert not live.available()
    with pytest.raises(LiveError):
        live.require()


@pytest.fixture
def running_inkscape(tmp_path):
    document = tmp_path / "live.svg"
    document.write_text(DOC)
    live = LiveInkscape(Inkscape(), timeout=60)

    window = live.open(document)
    assert window is not None, "Inkscape did not start a window"
    time.sleep(2)

    yield live, document, window

    subprocess.run(["pkill", "-f", f"inkscape {document}"], check=False)


@pytest.mark.skipif(not CAN_RUN_LIVE, reason="needs Linux, a display, a session D-Bus and Inkscape")
def test_live_round_trip(running_inkscape, tmp_path):
    live, document, window = running_inkscape

    live.run(["select-by-id:r1,c1", "path-union"], window)
    exported = SvgDocument.load(live.export(tmp_path / "pulled.svg"))

    assert exported.has("r1") and not exported.has("c1")
    assert live.export(tmp_path / "snap.png", "png", width=100).stat().st_size > 0

    live.save(window)
    on_disk = SvgDocument.load(document)
    on_disk.update("r1", attributes={"fill": "purple"})
    on_disk.save(document)
    live.reload(window)
    time.sleep(1)

    reloaded = SvgDocument.load(live.export(tmp_path / "reloaded.svg"))
    assert reloaded.find("r1").get("fill") == "purple"


@pytest.mark.skipif(not CAN_RUN_LIVE, reason="needs Linux, a display, a session D-Bus and Inkscape")
def test_chat_keeps_the_window_in_sync(running_inkscape, tmp_path):
    from inkscape_mcp.chat import ChatSession
    from test_agent import ScriptedClient, done, tool_call

    live, document, window = running_inkscape
    live.run(["select-by-id:c1", "object-flip-horizontal"], window)
    client = ScriptedClient([tool_call("a", "update_element", element_id="r1", style={"fill": "orange"}),
                             done("Made the square orange.")])
    session = ChatSession(document, client, Inkscape(), live, window)

    reply = session.send("Make the square orange")
    time.sleep(1)

    shown = SvgDocument.load(live.export(tmp_path / "shown.svg"))
    assert reply["text"] == "Made the square orange."
    assert "fill:orange" in shown.find("r1").get("style", ""), "window reloaded with Claude's edit"
    assert shown.find("c1").get("transform"), "the unsaved flip was saved before Claude edited"
