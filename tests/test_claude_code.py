"""Tests for the Claude Code backend: the MCP bridge, and the chat run through a fake `claude` command."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))

from inkscape_mcp.agent import TOOLS  # noqa: E402
from inkscape_mcp.chat import ChatSession  # noqa: E402
from inkscape_mcp.claude import ClaudeClient, ClaudeError  # noqa: E402
from inkscape_mcp.claude_code import ClaudeCode, ClaudeCodeAgent  # noqa: E402
from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402

MCP_SERVER = str(Path(__file__).resolve().parent.parent / "mcp_server")

# Stands in for `claude -p`: records its arguments, starts the MCP server from --mcp-config, makes the tool calls
# in FAKE_CLAUDE_CALLS through it and prints stream-json events like Claude Code does.
FAKE_CLAUDE = '''
import json, os, subprocess, sys

args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "prompt": prompt, "api_key": os.environ.get("ANTHROPIC_API_KEY")}) + "\\n")

config = json.loads(next(a for a in args if a.startswith("--mcp-config=")).split("=", 1)[1])
server = config["mcpServers"]["inkscape"]
bridge = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True, env={**os.environ, **server["env"]})

def rpc(number, method, params):
    bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}) + "\\n")
    bridge.stdin.flush()
    return json.loads(bridge.stdout.readline())["result"]

def emit(event):
    print(json.dumps({**event, "session_id": "session-1"}), flush=True)

rpc(0, "initialize", {"protocolVersion": "2025-06-18"})
emit({"type": "system", "subtype": "init", "mcp_servers": [{"name": "inkscape", "status": "connected"}]})
for number, (name, arguments) in enumerate(json.loads(os.environ["FAKE_CLAUDE_CALLS"]), 1):
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{number}", "name": f"mcp__inkscape__{name}", "input": arguments}]}})
    result = rpc(number, "tools/call", {"name": name, "arguments": arguments})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{number}",
          "content": result["content"], "is_error": result.get("isError", False)}]}})
bridge.stdin.close()
bridge.wait()
emit({"type": "result", "subtype": "success", "is_error": False, "result": os.environ["FAKE_CLAUDE_REPLY"]})
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("the fake claude command is a shell script")

    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    log = tmp_path / "claude-log.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(script))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-claude-code")

    def script_turn(reply, *calls):
        monkeypatch.setenv("FAKE_CLAUDE_CALLS", json.dumps(calls))
        monkeypatch.setenv("FAKE_CLAUDE_REPLY", reply)

    def runs():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return script_turn, runs


@pytest.fixture
def drawing(tmp_path):
    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)

    return path


def test_bridge_speaks_mcp(drawing, tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, env={**os.environ, "PYTHONPATH": MCP_SERVER},
                                   args=["-m", "inkscape_mcp.bridge", str(drawing), "--history", str(tmp_path / "history")])

    async def talk():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            added = await session.call_tool("add_elements", {"svg_fragment": '<circle id="dot" r="5"/>'})
            missing = await session.call_tool("delete_elements", {"element_ids": ["nope"]})
            preview = await session.call_tool("render_preview", {"width": 100})

            return listed, added, missing, preview

    listed, added, missing, preview = asyncio.run(talk())

    assert [tool.name for tool in listed.tools] == [tool["name"] for tool in TOOLS]
    assert added.content[0].text == "Added: dot" and not added.is_error
    assert missing.is_error
    assert preview.content[0].type == "image" and preview.content[0].mime_type == "image/png"
    assert SvgDocument.load(drawing).has("dot")
    assert len(list((tmp_path / "history" / "drawing.svg").glob("*.svg"))) == 1, "the edit left an undo snapshot"


def test_chat_through_claude_code_edits_resumes_and_undoes(drawing, fake_claude):
    script_turn, runs = fake_claude
    session = ChatSession(drawing, ClaudeClient("", model="opus", effort="low"), Inkscape(), live=None,
                          backend="claude-code")

    script_turn("Added a teal box.",
                ("add_elements", {"svg_fragment": '<rect id="box" width="50" height="50" fill="teal"/>'}),
                ("delete_elements", {"element_ids": ["nope"]}))
    first = session.send("Add a box")
    script_turn("Done.")
    session.send("Thanks")
    undone = session.undo()

    [one, two] = runs()
    assert first["text"] == "Added a teal box." and first["preview"]
    assert [(step["tool"], step["failed"]) for step in first["steps"]] == [("add_elements", False),
                                                                          ("delete_elements", True)]
    assert first["steps"][0]["summary"] == "Added: box"
    assert one["prompt"] == "Add a box" and one["api_key"] is None, "the subscription is used, not the key"
    assert ["--model", "opus", "--effort", "low"] == one["args"][4:8]
    assert "--tools=" in one["args"] and "--resume" not in one["args"]
    assert two["args"][-2:] == ["--resume", "session-1"]
    assert undone["text"] == "Undid the last change." and not SvgDocument.load(drawing).has("box")


def test_claude_code_errors_are_clear(drawing, tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeCode, "candidates", staticmethod(lambda configured=None: []))
    agent = ClaudeCodeAgent(ClaudeClient(""), drawing, tmp_path)

    with pytest.raises(ClaudeError, match="Could not find Claude Code"):
        agent.run("hi")

    failed = {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}
    with pytest.raises(ClaudeError, match="Please run /login"):
        agent.finish(failed, [], "")
    with pytest.raises(ClaudeError, match="boom"):
        agent.finish(None, [], "starting\nboom")


def test_without_a_key_the_chat_picks_claude_code_when_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(ClaudeCode, "find", classmethod(lambda cls, configured=None: "/usr/bin/claude"))

    assert ChatSession.default_backend("") == "claude-code"
    assert ChatSession.default_backend("sk-ant-key") == "api"

    monkeypatch.setattr(ClaudeCode, "find", classmethod(lambda cls, configured=None: None))

    assert ChatSession.default_backend("") == "api"


def test_switching_backend_in_settings(drawing, fake_claude):
    session = ChatSession(drawing, ClaudeClient(""), Inkscape(), live=None)

    assert session.needs_key
    with pytest.raises(ClaudeError, match="choose Claude Code"):
        session.send("hi")

    session.configure("opus", "high", backend="claude-code")

    assert isinstance(session.agent, ClaudeCodeAgent) and not session.needs_key
    assert session.client.effort == "high"


def test_finds_claude_from_the_configured_path(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("")
    monkeypatch.delenv("CLAUDE_CODE_BIN", raising=False)

    assert ClaudeCode.find(str(binary)) == str(binary)
    assert str(binary) not in ClaudeCode.candidates()
