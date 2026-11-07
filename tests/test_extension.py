"""Runs the Ask Claude extension end to end against a fake Claude API (no key or network needed).

Checks both the SDK path (streaming) and the standard-library fallback, and all three scopes.
Run: python -m pytest tests/test_extension.py
"""

import base64
import json
import os
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from lxml import etree

HERE = Path(__file__).resolve().parent
EXT = HERE.parent / "extension" / "ask_claude.py"

DOC = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" width="200" height="200" viewBox="0 0 200 200" id="svg1">
  <sodipodi:namedview id="nv1" pagecolor="#ffffff"/>
  <defs id="defs1"/>
  <g id="layer1" inkscape:groupmode="layer" inkscape:label="Layer 1">
    <rect id="r1" x="10" y="10" width="50" height="50" fill="red"/>
    <rect id="r2" x="100" y="100" width="50" height="50" fill="green"/>
  </g>
</svg>"""

REPLIES = {
    "document": 'Made it blue.\n```svg\n<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" id="svg1">'
                '<g id="layer1"><circle id="c1" cx="100" cy="100" r="50" fill="blue" onclick="alert(1)"/>'
                '<script>alert(1)</script></g></svg>\n```',
    "selection": '```svg\n<defs><linearGradient id="g1"><stop offset="0" stop-color="#000"/></linearGradient></defs>'
                 '<circle id="r2" cx="35" cy="35" r="25" fill="url(#g1)"/>\n```\nSwapped the square for a circle.',
    "add": '```svg\n<text id="r2" x="20" y="190">Hi</text>\n```',
}


class FakeClaude(BaseHTTPRequestHandler):
    """Answers like the Messages API. Pops scripted responses from `script`, else replies with `reply` text."""

    requests = []
    reply = ""
    script = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeClaude.requests.append({"body": body, "headers": dict(self.headers)})

        if FakeClaude.script:
            content, stop_reason = FakeClaude.script.pop(0)
        else:
            content, stop_reason = [{"type": "text", "text": FakeClaude.reply}], "end_turn"

        if body.get("stream"):
            self.stream(body, content, stop_reason)
        else:
            self.respond(body, content, stop_reason)

    @staticmethod
    def message(body, content, stop_reason):
        return {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": content,
                "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}

    def respond(self, body, content, stop_reason):
        out = json.dumps(self.message(body, content, stop_reason)).encode()

        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def stream(self, body, content, stop_reason):
        events = [("message_start", {"type": "message_start", "message": self.message(body, [], None)})]

        for index, block in enumerate(content):
            if block["type"] == "text":
                start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
            else:
                start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            events += [
                ("content_block_start", {"type": "content_block_start", "index": index, "content_block": start}),
                ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": index}),
            ]

        events += [
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())


def tool_use(tool_id, name, **arguments):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


@pytest.fixture(scope="module")
def fake_api():
    srv = HTTPServer(("127.0.0.1", 0), FakeClaude)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def run_ext(tmp_path, base_url, scope, ids=(), use_sdk=True, method="rewrite", doc=DOC, send_image=False):
    src = tmp_path / "in.svg"
    src.write_text(doc)
    FakeClaude.reply = REPLIES.get(scope, "Done.")
    FakeClaude.requests.clear()
    env = {**os.environ, "ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_API_KEY": "sk-test",
           "INKSCAPE_CLAUDE_NO_SDK": "0" if use_sdk else "1"}
    args = [sys.executable, str(EXT), f"--scope={scope}", "--prompt=Do the thing\\nplease",
            f"--send_image={str(send_image).lower()}",
            f"--method={method}"]
    args += [f"--id={i}" for i in ids]
    proc = subprocess.run(args + [str(src)], capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, proc.stderr
    return etree.fromstring(proc.stdout.encode()), proc.stderr, FakeClaude.requests[-1]


@pytest.mark.parametrize("use_sdk", [True, False])
def test_document_scope(tmp_path, fake_api, use_sdk):
    root, err, req = run_ext(tmp_path, fake_api, "document", use_sdk=use_sdk)
    body = req["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in {k.lower(): v for k, v in req["headers"].items()}.get("anthropic-beta", "")
    assert "Do the thing\nplease" in body["messages"][0]["content"][-1]["text"]
    assert root.xpath("//*[@id='c1']")
    assert not root.xpath("//*[local-name()='script']"), "scripts must be stripped"
    assert root.xpath("//*[@id='c1']")[0].get("onclick") is None
    assert root.xpath("//*[local-name()='namedview']"), "namedview should be kept"
    assert "Made it blue" in err


def test_selection_scope(tmp_path, fake_api):
    root, err, req = run_ext(tmp_path, fake_api, "selection", ids=["r1"])
    assert "Selected elements (1)" in req["body"]["messages"][0]["content"][-1]["text"]
    assert not root.xpath("//*[@id='r1']"), "selected element replaced"
    assert root.xpath("//*[@id='r2']")[0].get("fill") == "green", "untouched element kept"
    circle = root.xpath("//*[local-name()='circle']")[0]
    assert circle.get("id") == "r2-1", "clashing id renamed"
    assert root.xpath("//*[local-name()='defs']/*[@id='g1']"), "gradient moved into defs"
    layer = root.xpath("//*[@id='layer1']")[0]
    assert [c.get("id") for c in layer] == ["r2-1", "r2"], "result placed where the selection was"
    assert "Swapped" in err


def test_add_scope(tmp_path, fake_api):
    root, _, _ = run_ext(tmp_path, fake_api, "add")
    layer = root.xpath("//*[@id='layer1']")[0]
    assert [c.get("id") for c in layer] == ["r1", "r2", "r2-1"]
    assert layer[-1].text == "Hi"


@pytest.mark.parametrize("use_sdk", [True, False])
def test_tool_method_edits_step_by_step(tmp_path, fake_api, use_sdk):
    FakeClaude.script = [
        ([tool_use("t1", "get_outline")], "tool_use"),
        ([{"type": "text", "text": "Merging the squares."},
          tool_use("t2", "add_elements", svg_fragment='<rect id="r3" x="40" y="40" width="80" height="80" fill="blue"/>'),
          tool_use("t3", "boolean_operation", element_ids=["r1", "r3"], operation="union")], "tool_use"),
        ([tool_use("t4", "delete_elements", element_ids=["missing"]), tool_use("t5", "render_preview", width=200)],
         "tool_use"),
        ([{"type": "text", "text": "Joined the red square with a new one."}], "end_turn"),
    ]

    root, err, last = run_ext(tmp_path, fake_api, "document", use_sdk=use_sdk, method="tools")

    bodies = [r["body"] for r in FakeClaude.requests]
    assert len(bodies) == 4
    assert {t["name"] for t in bodies[0]["tools"]} >= {"get_outline", "boolean_operation", "render_preview"}

    outline_result = bodies[1]["messages"][-1]["content"][0]
    assert outline_result["tool_use_id"] == "t1" and "r1" in outline_result["content"]

    results = {r["tool_use_id"]: r for r in bodies[3]["messages"][-1]["content"]}
    assert results["t4"]["is_error"] and "missing" in results["t4"]["content"]
    assert results["t5"]["content"][0]["type"] == "image"

    assert root.xpath("//*[@id='r1']")[0].tag.endswith("path"), "union ran in Inkscape"
    assert not root.xpath("//*[@id='r3']")
    assert root.xpath("//*[@id='r1']")[0].get("fill") == "red", "result kept the bottom object's style"
    assert root.xpath("//*[@id='r2']"), "untouched element kept"
    assert "Joined the red square" in err and "(5 step(s))" in err


def test_rewrite_knows_about_pages(tmp_path, fake_api):
    pages = ('<inkscape:page id="p1" x="0" y="0" width="200" height="200" inkscape:label="Front"/>'
             '<inkscape:page id="p2" x="220" y="0" width="200" height="200" inkscape:label="Back"/>')
    doc = DOC.replace('<sodipodi:namedview id="nv1" pagecolor="#ffffff"/>',
                      f'<sodipodi:namedview id="nv1" pagecolor="#ffffff">{pages}</sodipodi:namedview>')
    doc = doc.replace('id="r2" x="100"', 'id="r2" x="300"')

    root, _, req = run_ext(tmp_path, fake_api, "selection", ids=["r2"], doc=doc, send_image=True)
    content = req["body"]["messages"][0]["content"]

    assert "It has 2 pages (artboards)" in content[-1]["text"]
    assert 'page 2 id=p2 "Back": x=220 y=0 size=200x200' in content[-1]["text"]
    assert "The selection is on page 2." in content[-1]["text"]
    png = base64.b64decode(content[0]["source"]["data"])
    assert struct.unpack(">II", png[16:24]) == (1024, 488), "the picture covers both pages"
    assert len(root.xpath("//*[local-name()='page']")) == 2, "pages survive the edit"


def test_missing_key(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(DOC)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HOME"] = str(tmp_path)  # no config file here
    proc = subprocess.run([sys.executable, str(EXT), "--prompt=x", str(src)], capture_output=True, text=True, env=env)
    assert "No Claude API key" in proc.stderr


def test_reply_parsing():
    sys.path.insert(0, str(EXT.parent))
    from ask_claude import ClaudeReply

    reply = ClaudeReply.parse("Here you go:\n```svg\n<rect/>\n```\nEnjoy")

    assert reply.svg == "<rect/>" and "Here you go" in reply.note and "Enjoy" in reply.note
    assert ClaudeReply.parse("no svg here").svg is None
    assert ClaudeReply.parse('<svg x="1"><g/></svg>').svg.startswith("<svg")


def test_inx_matches_script():
    import warnings
    import unittest
    from inkex.tester.inx import InxMixin

    class Check(InxMixin, unittest.TestCase):
        def runTest(self):
            pass

    cwd = os.getcwd()
    os.chdir(EXT.parent)  # the inx refers to the script by relative path
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for inx in ("ask_claude.inx", "claude_chat.inx"):
                Check().assertInxIsGood(inx)
    finally:
        os.chdir(cwd)
