"""Tests for images attached to chat messages and for the context and usage limits the chat shows."""

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inkscape_mcp.attachments import Attachments, pixel_size  # noqa: E402
from inkscape_mcp.chat import ChatSession  # noqa: E402
from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.operations import DocumentOperations  # noqa: E402
from inkscape_mcp.svgdoc import XLINK_NS, SvgDocument  # noqa: E402
from inkscape_mcp.usage import Limit, Usage, context_window  # noqa: E402


def png(width: int, height: int, noise: bool = False) -> bytes:
    """A real PNG, grey or random, so GTK can load it too."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = b"".join(b"\0" + (bytes((x * 7 + y * 13) % 256 for x in range(width)) if noise else b"\x80" * width)
                      for y in range(height))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def test_context_window_by_model():
    assert context_window("claude-opus-5-5") == 1_000_000
    assert context_window("claude-sonnet-4-6") == 1_000_000
    assert context_window("claude-haiku-4-5-20251001") == 200_000
    assert context_window("claude-sonnet-4-20250514") == 200_000
    assert context_window("opus") is None


def test_usage_and_limits_read_like_the_chat_shows_them():
    usage = Usage()
    usage.record({"input_tokens": 5, "cache_read_input_tokens": 41_000, "output_tokens": 995}, window=1_000_000)
    info = {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.12, "resetsAt": 1_000_000 + 2 * 3600 + 5 * 60, "status": "allowed"},
        "seven_day_opus": {"utilization": 91, "resetsAt": 1_000_000 + 3 * 86400}}}
    [five_hour, weekly] = Limit.from_claude_code(info)

    assert usage.context_text() == "Context: 42K of 1M (4%)"
    assert (five_hour.name, five_hour.used, weekly.name, weekly.used) == ("5-hour limit", 0.12,
                                                                          "Weekly Opus limit", 0.91)
    assert five_hour.describe(now=1_000_000).startswith("5-hour limit: 12% used, resets in 2 h 5 min (")
    assert "resets in 3 d 0 h" in weekly.describe(now=1_000_000)
    assert Limit.from_claude_code({"rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": 5})[0].used == 0.5
    assert Limit("5-hour limit", 1.0, None, "rejected").describe() == "5-hour limit: reached"


def test_attachments_check_the_file_and_keep_names_apart(tmp_path):
    attachments = Attachments(tmp_path / "attached")
    source = tmp_path / "logo.png"
    source.write_bytes(png(30, 10))
    (tmp_path / "notes.txt").write_text("hello")

    first = attachments.add(source)
    second = attachments.add(source)

    assert (first.name, second.name, first.media_type, first.size) == ("logo.png", "logo-2.png", "image/png", (30, 10))
    assert first.block()["source"]["media_type"] == "image/png"
    assert attachments.find("logo-2.png").path == second.path
    with pytest.raises(ValueError, match="not a PNG, JPEG"):
        attachments.add(tmp_path / "notes.txt")
    with pytest.raises(ValueError, match="No attached image named 'nope.png'. Attached: logo-2.png, logo.png"):
        attachments.find("nope.png")
    with pytest.raises(ValueError, match="larger than 5 MB"):
        attachments.add_data("huge.png", b"\x89PNG\r\n\x1a\n" + bytes(6 * 1024 * 1024))


def test_reads_jpeg_gif_and_webp_sizes():
    jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 48, 64)
    gif = b"GIF89a" + struct.pack("<HH", 12, 9)
    webp = b"RIFF\0\0\0\0WEBPVP8X" + bytes(8) + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")

    assert pixel_size(jpeg + bytes(10)) == (64, 48)
    assert pixel_size(gif) == (12, 9)
    assert pixel_size(webp) == (100, 50)


def test_place_image_fits_the_page_and_keeps_the_aspect(tmp_path):
    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    source = tmp_path / "wide photo.png"
    source.write_bytes(png(400, 100, noise=True))
    image = Attachments(tmp_path / "attached").add(source)
    operations = DocumentOperations(Inkscape())

    placed, box = operations.place_image(path, image)
    scaled, scaled_box = operations.place_image(path, image, x=0, y=0, height=10)

    doc = SvgDocument.load(path)
    element = doc.find(placed)
    assert (placed, box) == ("wide-photo", (20.0, 30.0, 160.0, 40.0))
    assert scaled != placed and scaled_box == (0.0, 0.0, 40.0, 10.0)
    assert element.get(f"{{{XLINK_NS}}}href").startswith("data:image/png;base64,iVBOR")
    assert "(embedded image," in doc.element_source(placed) and len(doc.element_source(placed)) < 600


def test_api_chat_shows_images_to_claude_and_reports_usage(tmp_path):
    from test_agent import ScriptedClient, done, tool_call

    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    usage = {"input_tokens": 1200, "output_tokens": 80}
    placing = tool_call("a", "place_image")
    placing["content"][0]["input"] = {"name": "logo.png"}
    client = ScriptedClient([{**placing, "usage": usage,
                              "limits": [Limit("Rate limit (requests per minute)", 0.1, 30.0)]},
                             {**done("Placed your logo."), "usage": {**usage, "input_tokens": 3000}}])
    session = ChatSession(path, client, Inkscape(), live=None)
    source = tmp_path / "logo.png"
    source.write_bytes(png(20, 20))
    image = session.attachments.add(source)
    events = []

    result = session.send("", lambda kind, payload: events.append((kind, payload)), [image])

    first_message = client.calls[0][0]["content"]
    assert [block["type"] for block in first_message] == ["image", "text"]
    assert "(Attached images: logo.png." in first_message[1]["text"]
    assert session.transcript[0] == {"role": "user", "text": "Here is an image.", "images": ["logo.png"]}
    assert SvgDocument.load(path).find("logo").get("width") == "20"
    assert [kind for kind, _ in events].count("usage") == 2
    assert result["usage"]["context_tokens"] == 3080 and result["usage"]["context_window"] == 1_000_000
    assert result["usage"]["limits"][0]["name"] == "Rate limit (requests per minute)"
    assert session.state()["usage"]["context_text"] == "Context: 3K of 1M (0%)"


def test_browser_page_takes_images(tmp_path):
    import base64
    import json
    import threading
    import urllib.request

    from inkscape_mcp.chat import ChatServer
    from test_agent import ScriptedClient, done

    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    session = ChatSession(path, ScriptedClient([done("I see a grey square.")]), Inkscape(), live=None)
    server = ChatServer(session)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        body = {"text": "What is this?", "images": [{"name": "square.png", "data": base64.b64encode(png(8, 8)).decode()}]}
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/message", method="POST",
                                         data=json.dumps(body).encode(),
                                         headers={"x-chat-token": server.token, "content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()

    assert result["text"] == "I see a grey square."
    assert session.attachments.names() == ["square.png"]
    assert session.agent.messages[0]["content"][0]["type"] == "image"


def test_the_chat_is_kept_per_drawing(tmp_path):
    from inkscape_mcp.chat_store import IMAGE_LEFT_OUT, ChatStore
    from test_agent import ScriptedClient, done

    path = tmp_path / "drawing.svg"
    SvgDocument.blank(200, 100).save(path)
    store = ChatStore(path, tmp_path / "chats")
    first = ChatSession(path, ScriptedClient([{**done("A grey square."), "usage": {"input_tokens": 900}}]),
                        Inkscape(), live=None, store=store)
    source = tmp_path / "square.png"
    source.write_bytes(png(8, 8))

    first.send("What is this?", images=[first.attachments.add(source)])
    second = ChatSession(path, ScriptedClient([done("Yes.")]), Inkscape(), live=None, store=store)

    assert [entry["text"] for entry in second.transcript] == ["What is this?", "A grey square."]
    assert second.transcript[0]["images"] == ["square.png"] and second.attachments.names() == ["square.png"]
    assert second.agent.messages[0]["content"][0] == {"type": "text", "text": IMAGE_LEFT_OUT}
    assert second.usage.context_tokens == 900

    second.send("Still there?")

    assert len(second.client.calls[0]) == 3, "Claude gets the earlier conversation"
    other = ChatSession(tmp_path / "other.svg", ScriptedClient([]), Inkscape(), live=None,
                        store=ChatStore(tmp_path / "other.svg", tmp_path / "chats"))
    assert other.transcript == []

    switched = ChatSession(path, ScriptedClient([]), Inkscape(), live=None, backend="claude-code", store=store)
    assert switched.transcript[-1]["text"].startswith("These messages went through another connection")

    assert second.clear()["text"] == "Started a new chat."
    assert not store.folder.exists() and second.transcript == [] and second.agent.messages == []
    assert ChatSession(path, ScriptedClient([]), Inkscape(), live=None, store=store).transcript == []
