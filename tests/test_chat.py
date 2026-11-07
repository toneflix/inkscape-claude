"""Tests for the chat panel server, with a scripted Claude client."""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inkscape_mcp.chat import ChatServer, ChatSession  # noqa: E402
from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402
from test_agent import ScriptedClient, done, tool_call  # noqa: E402


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    client = ScriptedClient([
        tool_call("a", "add_elements", svg_fragment='<rect id="box" width="50" height="50" fill="teal"/>'),
        done("Added a teal box."),
        done("Noted."),
    ])
    session = ChatSession(path, client, Inkscape(), live=None)
    server = ChatServer(session)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    yield server, session, client, path

    server.shutdown()
    server.server_close()


def request(server, path, body=None, token=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["x-chat-token"] = token
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_page_and_api_need_the_token(chat):
    server, *_ = chat

    assert request(server, "/")[0] == 403
    assert request(server, "/api/state")[0] == 404
    assert request(server, "/api/message", {"text": "hi"})[0] == 403
    assert request(server, "/api/message", {"text": "hi"}, token="wrong")[0] == 403

    status, page = request(server, f"/?token={server.token}")
    assert status == 200 and server.token.encode() in page


def test_conversation_edits_the_file_and_undo_reverts(chat):
    server, session, client, path = chat

    status, body = request(server, "/api/message", {"text": "Add a box"}, server.token)
    reply = json.loads(body)

    assert status == 200
    assert reply["text"] == "Added a teal box."
    assert [step["tool"] for step in reply["steps"]] == ["add_elements"]
    assert reply["preview"], "a PNG preview comes back"
    assert SvgDocument.load(path).has("box")

    status, body = request(server, "/api/undo", {}, server.token)

    assert status == 200 and not SvgDocument.load(path).has("box")

    request(server, "/api/message", {"text": "Thanks"}, server.token)
    last_prompt = client.calls[-1][-1]["content"][-1]["text"]

    assert "undid your last edit" in last_prompt and last_prompt.endswith("Thanks")
    assert len(client.calls[-1]) == 5, "earlier turns stay in the conversation"

    state = json.loads(request(server, "/api/state", token=server.token)[1])
    assert [entry["role"] for entry in state["transcript"]] == ["user", "assistant", "system", "user", "assistant"]
    assert state["live"] is False


def test_errors_come_back_as_messages(chat):
    server, *_ = chat

    status, body = request(server, "/api/message", {"text": "  "}, server.token)

    assert status == 400 and "Type a message" in json.loads(body)["error"]

    status, body = request(server, "/api/undo", {}, server.token)

    assert status == 400 and "Nothing to undo" in json.loads(body)["error"]


def test_settings_apply_to_the_next_message_and_a_key_is_needed(tmp_path):
    from inkscape_mcp.claude import ClaudeError

    path = tmp_path / "drawing.svg"
    SvgDocument.blank(100, 100).save(path)
    client = ScriptedClient([done("Hi.")])
    client.api_key = ""
    session = ChatSession(path, client, Inkscape(), live=None)

    with pytest.raises(ClaudeError, match="API key"):
        session.send("Hello")

    session.configure("claude-sonnet-5-5", "high", "sk-new")

    assert (client.model, client.effort, client.api_key) == ("claude-sonnet-5-5", "high", "sk-new")
    assert session.send("Hello")["text"] == "Hi."

    session.configure("", "loud")

    assert (client.model, client.effort, client.api_key) == ("claude-opus-5-5", "medium", "sk-new")


class MarkingLive:
    """Stands in for a live Inkscape window: marking the selection writes the mark into the file on save."""

    def __init__(self, path, selected):
        self.path, self.selected, self.actions, self.reloads = path, selected, [], 0

    def available(self):
        return True

    def run(self, actions, window=None):
        self.actions += actions

    def save(self, window=None, path=None):
        doc = SvgDocument.load(self.path)
        for element_id in self.selected:
            doc.find(element_id).set("data-claude-selected", "1")
        doc.save(self.path)

    def reload(self, window=None):
        self.reloads += 1


def test_the_selection_in_inkscape_is_sent_with_the_message(tmp_path):
    path = tmp_path / "drawing.svg"
    doc = SvgDocument.blank(200, 100)
    doc.add('<rect id="a" width="5" height="5"/><rect id="b" x="10" width="5" height="5"/>')
    doc.save(path)
    client = ScriptedClient([done("Made them blue."), done("Hello.")])
    live = MarkingLive(path, ["b", "a"])
    session = ChatSession(path, client, Inkscape(), live)

    reply = session.send("Make these blue")

    assert reply["selection"] == ["a", "b"]
    assert live.actions == ["object-set-attribute:data-claude-selected,1"]
    assert client.calls[0][0]["content"][-1]["text"].startswith("(Selected in Inkscape: a, b.")
    assert "data-claude-selected" not in path.read_text()
    assert live.reloads == 1

    live.selected = []
    assert session.send("Hi")["selection"] == []
    assert client.calls[1][-1]["content"][-1]["text"] == "Hi"
