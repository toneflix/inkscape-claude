"""Chat panel: a local web page for an ongoing conversation with Claude about one Inkscape drawing.

Run:  inkscape-claude-chat drawing.svg
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .agent import DrawingAgent
from .claude import DEFAULT_MODEL, ClaudeClient, ClaudeError, Config
from .cli import Inkscape, InkscapeError
from .live import LiveError, LiveInkscape
from .operations import DocumentOperations
from .shell import ShellInkscape, live_control
from .svgdoc import SvgError
from .workspace import History

PAGE = Path(__file__).with_name("chat.html")

EXPECTED_ERRORS = (ClaudeError, InkscapeError, LiveError, SvgError, OSError, ValueError)


class ChatSession:
    """One conversation about one SVG file, keeping an open Inkscape window in sync when it can."""

    def __init__(self, path: Path, client: ClaudeClient, inkscape: Inkscape,
                 live: LiveInkscape | ShellInkscape | None, window: int | None = None):
        self.path = path
        self.live = live
        self.window = window
        self.history_dir = tempfile.TemporaryDirectory(prefix="inkscape-claude-chat-")
        self.operations = DocumentOperations(inkscape, History(root=Path(self.history_dir.name)))
        self.agent = DrawingAgent(client, self.operations, path)
        self.transcript: list[dict] = []
        self.notes: list[str] = []
        self.lock = threading.Lock()

    @property
    def live_enabled(self) -> bool:
        return self.live is not None and self.live.available()

    def state(self) -> dict:
        return {"document": str(self.path), "live": self.live_enabled, "transcript": self.transcript}

    def send(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("Type a message first.")

        with self.lock:
            self.sync_from_inkscape()
            self.transcript.append({"role": "user", "text": text})

            prompt = "\n".join([*self.notes, text])
            self.notes.clear()
            try:
                result = self.agent.run(prompt)
            except EXPECTED_ERRORS as exc:
                self.transcript.append({"role": "system", "text": f"Error: {exc}"})
                raise

            entry = {"role": "assistant", "text": result.reply, "steps": [asdict(step) for step in result.steps]}
            self.transcript.append(entry)

            self.sync_to_inkscape()

            return {**entry, "preview": self.preview()}

    def undo(self) -> dict:
        with self.lock:
            remaining = self.operations.undo(self.path)
            if remaining is None:
                raise ValueError("Nothing to undo.")

            self.notes.append("(I undid your last edit to the document.)")
            self.transcript.append({"role": "system", "text": "Undid the last change."})
            self.sync_to_inkscape()

            return {"text": "Undid the last change.", "preview": self.preview()}

    def preview(self) -> str:
        return base64.b64encode(self.operations.preview(self.path, 600)).decode("ascii")

    def sync_from_inkscape(self) -> None:
        if self.live_enabled:
            self.live.save(self.window)

    def sync_to_inkscape(self) -> None:
        if self.live_enabled:
            self.live.reload(self.window)


class ChatHandler(BaseHTTPRequestHandler):
    session: ChatSession
    token: str

    def log_message(self, *args):
        pass

    def parse_request(self) -> bool:
        self.server.touch()

        return super().parse_request()

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == "/":
            if parse_qs(url.query).get("token", [""])[0] != self.token:
                return self.reply(HTTPStatus.FORBIDDEN, {"error": "Open the link printed by inkscape-claude-chat."})
            page = PAGE.read_text(encoding="utf-8").replace("{{TOKEN}}", self.token)
            return self.reply(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

        if url.path == "/api/state" and self.authorized():
            return self.reply(HTTPStatus.OK, self.session.state())

        self.reply(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self):
        if not self.authorized():
            return self.reply(HTTPStatus.FORBIDDEN, {"error": "Missing or wrong chat token."})

        routes = {"/api/message": lambda body: self.session.send(str(body.get("text", ""))),
                  "/api/undo": lambda body: self.session.undo()}
        route = routes.get(urlparse(self.path).path)
        if route is None:
            return self.reply(HTTPStatus.NOT_FOUND, {"error": "Not found."})

        try:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self.reply(HTTPStatus.OK, route(body))
        except EXPECTED_ERRORS as exc:
            self.reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("x-chat-token", ""), self.token)

    def reply(self, status: HTTPStatus, body, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: ChatSession, port: int = 0):
        self.token = secrets.token_urlsafe(16)
        self.last_activity = time.monotonic()
        handler = type("BoundChatHandler", (ChatHandler,), {"session": session, "token": self.token})
        super().__init__(("127.0.0.1", port), handler)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def stop_when_idle(self, minutes: float) -> None:
        """Shut the server down after `minutes` without requests."""
        self.stop_when(lambda: time.monotonic() - self.last_activity >= minutes * 60, min(30.0, minutes * 60))

    def stop_when(self, done: Callable[[], bool], every: float = 2.0) -> None:
        def watch():
            while not done():
                time.sleep(every)
            self.shutdown()

        threading.Thread(target=watch, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?token={self.token}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with Claude about an Inkscape drawing.")
    parser.add_argument("document", type=Path, help="SVG file to work on")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")
    parser.add_argument("--no-live", action="store_true", help="do not open or sync an Inkscape window")
    parser.add_argument("--attach", action="store_true",
                        help="the document is already open in Inkscape; sync with the newest window (Linux only; "
                             "elsewhere the chat opens its own window)")
    parser.add_argument("--window", type=int, help="Inkscape window number to sync with")
    parser.add_argument("--idle-minutes", type=float, help="stop after this many minutes without requests")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    document = args.document.expanduser().resolve()
    if not document.exists():
        sys.exit(f"{document} does not exist.")

    try:
        client = ClaudeClient.create(Config.api_key(), model=args.model, effort=args.effort)
    except ClaudeError as exc:
        sys.exit(str(exc))

    inkscape = Inkscape.from_env()
    live = None if args.no_live else live_control(inkscape)
    owns_window = isinstance(live, ShellInkscape)
    window = args.window
    if live is not None and (owns_window or not args.attach) and window is None:
        try:
            window = live.open(document)
        except LiveError as exc:
            print(f"Could not open an Inkscape window to keep in sync: {exc}", flush=True)

    session = ChatSession(document, client, inkscape, live, window)
    server = ChatServer(session, args.port)
    print(f"Claude chat for {document}\nOpen: {server.url}\nPress Ctrl+C to stop.", flush=True)

    if owns_window and live.available():
        server.stop_when(lambda: not live.available())
    elif args.idle_minutes:
        server.stop_when_idle(args.idle_minutes)
    if not args.no_browser:
        webbrowser.open(server.url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if owns_window:
            live.close()


if __name__ == "__main__":
    main()
