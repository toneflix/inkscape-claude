"""Unit tests for the drawing agent's tool loop, with a scripted client instead of the API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))

from inkscape_mcp.agent import DrawingAgent  # noqa: E402
from inkscape_mcp.claude import ClaudeClient  # noqa: E402
from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.operations import DocumentOperations  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402


class ScriptedClient(ClaudeClient):
    def __init__(self, responses):
        super().__init__("sk-test")
        self.responses = list(responses)
        self.calls = []

    def send(self, system, messages, tools=None):
        self.calls.append([dict(m) for m in messages])

        return self.responses.pop(0)


def tool_call(tool_id, name, **arguments):
    return {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": arguments}],
            "stop_reason": "tool_use"}


def done(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def make_agent(tmp_path, responses, **kwargs):
    path = tmp_path / "doc.svg"
    SvgDocument.blank(100, 100).save(path)
    client = ScriptedClient(responses)

    return DrawingAgent(client, DocumentOperations(Inkscape()), path, **kwargs), client, path


def test_edits_and_keeps_history_across_turns(tmp_path):
    agent, client, path = make_agent(tmp_path, [
        tool_call("a", "add_elements", svg_fragment='<circle id="sun" r="5"/>'),
        done("Added a sun."),
        tool_call("b", "update_element", element_id="sun", style={"fill": "gold"}),
        done("Made it gold."),
    ])

    first = agent.run("Add a sun")
    second = agent.run("Make it gold")

    assert first.reply == "Added a sun." and [s.tool for s in first.steps] == ["add_elements"]
    assert second.reply == "Made it gold."
    assert "fill:gold" in SvgDocument.load(path).find("sun").get("style")
    assert len(client.calls[-1]) == 7, "second turn sees the whole first turn"


def test_tool_errors_are_reported_to_claude(tmp_path):
    agent, client, _ = make_agent(tmp_path, [tool_call("a", "delete_elements", element_ids=["nope"]),
                                             tool_call("b", "no_such_tool"), done("Sorry.")])

    result = agent.run("Delete nope")

    assert [s.failed for s in result.steps] == [True, True]
    assert client.calls[1][-1]["content"][0]["is_error"]
    assert "Unknown tool" in client.calls[2][-1]["content"][0]["content"]


def test_step_limit_leaves_a_valid_history(tmp_path):
    agent, client, _ = make_agent(tmp_path, [tool_call(str(i), "get_outline") for i in range(3)], max_steps=2)

    result = agent.run("Loop forever")

    assert result.reply == "Stopped after 2 steps."
    assert any(block.get("type") == "text" for block in client.calls[-1][-1]["content"]), "told to stop"
    assert agent.messages[-1]["role"] == "user"
    assert agent.messages[-1]["content"][0]["is_error"], "unanswered tool call closed with an error result"
