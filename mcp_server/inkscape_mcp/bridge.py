"""The chat's drawing tools for one file as a minimal MCP server over stdio, for Claude Code.

Standard library only, so it runs on Inkscape's bundled Python without the `mcp` package.

Run:  python -m inkscape_mcp.bridge drawing.svg --history DIR [--attachments DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

from .agent import EXPECTED_ERRORS, TOOLS, DrawingTools
from .attachments import Attachments
from .cli import Inkscape
from .operations import DocumentOperations
from .workspace import History

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "inkscape"


class Bridge:
    """Answers MCP JSON-RPC requests, one JSON message per line, with the tools in TOOLS."""

    def __init__(self, tools: DrawingTools):
        self.tools = tools

    def serve(self, reader: TextIO, writer: TextIO) -> None:
        for line in reader:
            if not line.strip():
                continue

            reply = self.handle(line)
            if reply is not None:
                writer.write(json.dumps(reply) + "\n")
                writer.flush()

    def handle(self, line: str) -> dict | None:
        try:
            message = json.loads(line)
        except ValueError:
            return self.error(None, -32700, "Parse error")

        request_id = message.get("id")
        method = message.get("method", "")
        handler = getattr(self, "on_" + method.replace("/", "_"), None)
        if request_id is None:
            return None
        if handler is None:
            return self.error(request_id, -32601, f"Unknown method {method}")

        return {"jsonrpc": "2.0", "id": request_id, "result": handler(message.get("params") or {})}

    def on_initialize(self, params: dict) -> dict:
        return {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0"}}

    def on_ping(self, _params: dict) -> dict:
        return {}

    def on_tools_list(self, _params: dict) -> dict:
        return {"tools": [{"name": tool["name"], "description": tool["description"],
                           "inputSchema": tool["input_schema"]} for tool in TOOLS]}

    def on_tools_call(self, params: dict) -> dict:
        try:
            content = self.tools.call(params.get("name", ""), params.get("arguments"))
        except EXPECTED_ERRORS as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

        if isinstance(content, str):
            return {"content": [{"type": "text", "text": content}]}

        return {"content": [{"type": "image", "data": block["source"]["data"],
                             "mimeType": block["source"]["media_type"]} for block in content]}

    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the drawing tools for one SVG file over MCP stdio.")
    parser.add_argument("document", type=Path)
    parser.add_argument("--history", type=Path, help="folder for undo snapshots (default: next to the file)")
    parser.add_argument("--attachments", type=Path, help="folder with the images place_image can embed")
    args = parser.parse_args(argv)

    operations = DocumentOperations(Inkscape.from_env(), History(root=args.history))
    attachments = Attachments(args.attachments) if args.attachments else None
    Bridge(DrawingTools(operations, args.document.resolve(), attachments)).serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
