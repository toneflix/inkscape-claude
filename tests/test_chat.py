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
from inkscape_mcp.claude import ClaudeError  # noqa: E402
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
    assert b"{{" not in page and b'<svg xmlns="http://www.w3.org/2000/svg"' in page, "icons and logo are filled in"


def test_conversation_edits_the_file_and_undo_reverts(chat):
    server, session, client, path = chat

    status, body = request(server, "/api/message", {"text": "Add a box"}, server.token)
    reply = json.loads(body)

    assert status == 200
    assert reply["text"] == "Added a teal box."
    assert [step["tool"] for step in reply["steps"]] == ["add_elements"]
    assert reply["preview"], "a PNG preview comes back"
    assert reply["can_undo"]
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
    assert session.focus == ["a", "b"], "the preview zooms to the selection"

    live.selected = []
    assert session.send("Hi")["selection"] == []
    assert session.focus == []
    assert client.calls[1][-1]["content"][-1]["text"] == "Hi"


def test_the_reply_streams_and_the_window_reloads_after_each_change(tmp_path, monkeypatch):
    from inkscape_mcp import chat

    monkeypatch.setattr(chat, "LIVE_RELOAD_SECONDS", 0)
    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    client = ScriptedClient([
        tool_call("a", "add_elements", svg_fragment='<rect id="one" width="5" height="5"/>'),
        tool_call("b", "get_outline"),
        tool_call("c", "add_elements", svg_fragment='<rect id="two" width="5" height="5"/>'),
        done("Added two squares."),
    ])
    live = MarkingLive(path, [])
    session = ChatSession(path, client, Inkscape(), live)
    events = []

    session.send("Two squares", lambda kind, payload: events.append((kind, payload)))

    assert [kind for kind, _ in events] == ["step", "step", "step", "text"]
    assert events[-1] == ("text", "Added two squares.")
    assert live.reloads == 3, "after each of the two edits, not the outline, and once at the end"
    assert session.agent.on_text is None, "callbacks are only connected during the turn"


class FailsOnce(ScriptedClient):
    def send(self, system, messages, tools=None, on_text=None):
        if not self.calls:
            self.calls.append([])
            raise ClaudeError("Claude API error 529: Overloaded")

        return super().send(system, messages, tools, on_text)


def test_a_failed_message_can_be_retried(tmp_path):
    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    session = ChatSession(path, FailsOnce([done("Added it.")]), Inkscape(), live=None)

    with pytest.raises(ValueError, match="no failed message"):
        session.retry()
    with pytest.raises(ClaudeError, match="Overloaded"):
        session.send("Add a star")

    assert session.can_retry and session.transcript[-1]["retry"]

    result = session.retry()

    assert result["text"] == "Added it." and not session.can_retry
    assert [entry["role"] for entry in session.transcript] == ["user", "system", "assistant"], \
        "the message is not repeated in the conversation"
    assert session.agent.messages[-2]["content"][-1]["text"] == "Add a star"


class WindowLive(MarkingLive):
    """A live window that can be swapped: records the files opened and the windows closed."""

    def __init__(self, path, can_close=True):
        super().__init__(path, [])
        self.opened, self.closed, self.can_close = [], [], can_close

    def open(self, path):
        self.opened.append(path)
        self.path = path
        return len(self.opened) + 1

    def close_window(self, window):
        from inkscape_mcp.live import LiveError

        if not self.can_close:
            raise LiveError("cannot close")
        self.closed.append(window)


def test_save_as_moves_the_drawing_the_chat_and_the_window(tmp_path):
    from inkscape_mcp.chat_store import ChatStore

    draft = tmp_path / "Untitled.svg"
    SvgDocument.blank(200, 100).save(draft)
    store = ChatStore(draft, tmp_path / "chats")
    live = WindowLive(draft)
    session = ChatSession(draft, ScriptedClient([tool_call("a", "add_elements", svg_fragment='<rect id="box"/>'),
                                                 done("Added."), done("Still here.")]),
                          Inkscape(), live, window=1, store=store)
    session.send("Add a box")

    result = session.save_as(tmp_path / "work" / "poster")

    target = tmp_path / "work" / "poster.svg"
    assert result["text"].startswith(f"Saved as {target}.") and "in place of the old one" in result["text"]
    assert SvgDocument.load(target).has("box") and session.path == target
    assert live.opened == [target] and live.closed == [1] and session.window == 2
    assert not store.file.exists() and store.folder.is_dir(), "the old folder stays for Claude Code's session"

    session.send("Are you there?")

    assert session.agent.tools.path == target
    restored = ChatSession(target, ScriptedClient([]), Inkscape(), None, store=ChatStore(target, tmp_path / "chats"))
    assert [entry["text"] for entry in restored.transcript][:2] == ["Add a box", "Added."]
    assert ChatSession(draft, ScriptedClient([]), Inkscape(), None, store=store).transcript == []

    with pytest.raises(ValueError, match="already works on"):
        session.save_as(target)


def test_save_as_says_to_close_the_old_window_where_inkscape_cannot(tmp_path):
    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    session = ChatSession(path, ScriptedClient([]), Inkscape(), WindowLive(path, can_close=False))

    assert session.save_as(tmp_path / "copy.svg")["text"].endswith("close the old one.")
