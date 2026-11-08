"""Chat panel: a local web page for an ongoing conversation with Claude about one Inkscape drawing.

Run:  inkscape-claude-chat drawing.svg
"""

from __future__ import annotations

import argparse
import base64
import json
import re
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

from .agent import DrawingAgent, Step
from .attachments import Attachment, Attachments
from .chat_store import ChatStore
from .claude import BACKENDS, DEFAULT_MODEL, EFFORTS, ClaudeClient, ClaudeError, Config
from .claude_code import ClaudeCode, ClaudeCodeAgent
from .cli import Inkscape, InkscapeError
from .icons import svg
from .live import LiveError, LiveInkscape
from .operations import DocumentOperations
from .shell import ShellInkscape, live_control
from .svgdoc import SvgDocument, SvgError
from .usage import Usage
from .workspace import History

PAGE = Path(__file__).with_name("chat.html")
LOGO = Path(__file__).with_name("logo.svg")
SELECTION_MARK = "data-claude-selected"
READ_ONLY_TOOLS = {"get_outline", "get_pages", "get_element", "get_bounding_boxes", "render_preview"}
LIVE_RELOAD_SECONDS = 1.5

EXPECTED_ERRORS = (ClaudeError, InkscapeError, LiveError, SvgError, OSError, ValueError)


class ChatSession:
    """One conversation about one SVG file, keeping an open Inkscape window in sync when it can. Claude is
    reached with an API key (backend "api") or through the `claude` command (backend "claude-code")."""

    def __init__(self, path: Path, client: ClaudeClient, inkscape: Inkscape,
                 live: LiveInkscape | ShellInkscape | None, window: int | None = None, backend: str = "api",
                 claude_path: str | None = None, store: ChatStore | None = None):
        self.path = path
        self.live = live
        self.window = window
        self.claude_path = claude_path
        self.store = store
        self.history_dir = tempfile.TemporaryDirectory(prefix="inkscape-claude-chat-")
        self.operations = DocumentOperations(inkscape, History(root=Path(self.history_dir.name)))
        self.attachments = Attachments(store.attachments if store else Path(self.history_dir.name) / "attachments")
        self.backend = backend
        self.agent = self.make_agent(client, backend)
        self.transcript: list[dict] = []
        self.notes: list[str] = []
        self.focus: list[str] = []
        self.failed: tuple[str, list[Attachment]] | None = None
        self.lock = threading.Lock()
        self.restore()

    @classmethod
    def open(cls, args: argparse.Namespace) -> ChatSession:
        """Builds a session from the command line options, opening an Inkscape window to sync with if needed."""
        document = args.document.expanduser().resolve()
        if not document.exists():
            raise ValueError(f"{document} does not exist.")

        settings = Config.chat_settings()
        api_key = Config.find_api_key()
        client = ClaudeClient.create(api_key, model=args.model or settings["model"],
                                     effort=args.effort or settings["effort"])
        backend = args.backend or settings["backend"] or cls.default_backend(api_key, settings["claude_path"])
        inkscape = Inkscape.from_env()
        live = None if args.no_live else live_control(inkscape)
        window = args.window
        notice = None
        if args.draft and live is None:
            notice = (f"Your drawing had not been saved, so the chat saved it as {document}. Open that file in "
                      "Inkscape to see Claude's changes.")
        elif args.draft:
            notice = (f"Your drawing had not been saved, so the chat saved it as {document} and opened it in a new "
                      "Inkscape window that it keeps in sync. Work in that window and close the untitled one. To keep it "
                      "under another name, use File > Save As there, then open the chat again from that window: this "
                      "chat stays with the Untitled file.")
        if live is not None and (isinstance(live, ShellInkscape) or not args.attach) and window is None:
            try:
                window = live.open(document)
            except LiveError as exc:
                notice = f"Could not open an Inkscape window to keep in sync: {exc}"
            else:
                if args.attach and isinstance(live, ShellInkscape):
                    notice = ("Your Inkscape window could not be reached over D-Bus, so the chat opened the drawing "
                              "in a new window that it keeps in sync. Work in that one and close the other, so you "
                              "don't edit two copies.")

        session = cls(document, client, inkscape, live, window, backend, settings["claude_path"], ChatStore(document))
        if notice:
            print(notice, flush=True)
            session.transcript.append({"role": "system", "text": notice})

        return session

    @staticmethod
    def default_backend(api_key: str, claude_path: str | None = None) -> str:
        """The API when a key is set, otherwise Claude Code when it is installed."""
        return "claude-code" if not api_key and ClaudeCode.find(claude_path) else "api"

    def make_agent(self, client: ClaudeClient, backend: str) -> DrawingAgent | ClaudeCodeAgent:
        if backend == "claude-code":
            workdir = None
            if self.store:
                workdir = self.store.folder
                workdir.mkdir(parents=True, exist_ok=True)
            return ClaudeCodeAgent(client, self.path, Path(self.history_dir.name), self.claude_path,
                                   attachments=self.attachments, workdir=workdir)

        return DrawingAgent(client, self.operations, self.path, attachments=self.attachments)

    @property
    def live_enabled(self) -> bool:
        return self.live is not None and self.live.available()

    @property
    def client(self) -> ClaudeClient:
        return self.agent.client

    @property
    def needs_key(self) -> bool:
        return self.backend == "api" and not self.client.api_key

    def configure(self, model: str, effort: str, api_key: str | None = None, backend: str | None = None) -> None:
        """Changes the model and effort for the next messages, and the API key and backend if given. A new
        backend starts a new conversation."""
        self.client.model = model.strip() or DEFAULT_MODEL
        self.client.effort = effort if effort in EFFORTS else "medium"
        if api_key:
            self.client.api_key = api_key.strip()
        if backend in BACKENDS and backend != self.backend:
            self.backend = backend
            self.agent = self.make_agent(self.client, backend)
            self.save()

    def restore(self) -> None:
        """Brings back this drawing's saved chat. What Claude remembers only carries over on the same backend."""
        data = self.store.load() if self.store else {}
        if not data:
            return

        self.transcript = [entry for entry in data.get("transcript") or [] if isinstance(entry, dict)]
        self.notes = [str(note) for note in data.get("notes") or []]
        if data.get("backend") == self.backend:
            self.agent.restore(data.get("agent") or {})
            self.agent.usage = Usage.from_dict(data.get("usage"))
        elif self.transcript:
            self.transcript.append({"role": "system", "text": "These messages went through another connection, so "
                                                              "Claude does not remember them. Use New chat to start "
                                                              "over, or switch back in Settings."})

    def save(self) -> None:
        if not self.store:
            return

        state = dict(self.agent.state())
        if "messages" in state:
            state["messages"] = ChatStore.without_images(state["messages"])
        try:
            self.store.save({"backend": self.backend, "transcript": self.transcript, "notes": self.notes,
                             "agent": state, "usage": self.usage.to_dict()})
        except OSError as exc:
            print(f"Could not save the chat: {exc}", flush=True)

    def clear(self) -> dict:
        """Starts a new chat for this drawing: forgets the conversation and the attached images."""
        with self.lock:
            if self.store:
                self.store.clear()
            self.agent = self.make_agent(self.client, self.backend)
            self.transcript, self.notes, self.focus, self.failed = [], [], [], None

            return {"role": "system", "text": "Started a new chat.", "usage": self.usage.to_dict()}

    @property
    def owns_window(self) -> bool:
        """True when the chat opened the Inkscape window itself, so the window lives and dies with the chat."""
        return isinstance(self.live, ShellInkscape)

    def close(self) -> None:
        if self.owns_window:
            self.live.close()

    @property
    def usage(self) -> Usage:
        return self.agent.usage

    def state(self) -> dict:
        return {"document": str(self.path), "live": self.live_enabled, "transcript": self.transcript,
                "usage": self.usage.to_dict(), "can_retry": self.can_retry, "can_undo": self.can_undo}

    @property
    def can_undo(self) -> bool:
        return bool(self.operations.history.snapshots(self.path))

    @property
    def can_retry(self) -> bool:
        return self.failed is not None

    def retry(self, on_event: Callable[[str, object], None] | None = None) -> dict:
        """Sends the message that last failed again, with its images. Edits made before it failed stay."""
        if self.failed is None:
            raise ValueError("There is no failed message to retry.")

        text, images = self.failed

        return self.send(text, on_event, images, retrying=True)

    def send(self, text: str, on_event: Callable[[str, object], None] | None = None,
             images: list[Attachment] | None = None, retrying: bool = False) -> dict:
        """Runs one turn, with any attached images (from self.attachments) shown to Claude. on_event gets
        ("text", piece of the reply) as Claude writes, ("step", step dict) after each tool call and ("usage",
        usage dict) after each response; the Inkscape window reloads after drawing changes, at most every
        LIVE_RELOAD_SECONDS, and once more at the end."""
        text = text.strip()
        images = images or []
        if not text and images:
            text = "Here is an image."
        if not text:
            raise ValueError("Type a message first.")

        if self.needs_key:
            raise ClaudeError("Add your Claude API key in Settings first, set ANTHROPIC_API_KEY, or choose "
                              "Claude Code in Settings to use your subscription.")

        with self.lock:
            selection = self.sync_from_inkscape()
            names = [image.name for image in images]
            if not retrying:
                self.transcript.append({"role": "user", "text": text, "images": names})

            prompt = "\n".join([*self.notes, self.selection_note(selection), self.images_note(names), text]).strip()
            self.notes.clear()
            self.listen(on_event)
            try:
                result = self.agent.run(prompt, [image.block() for image in images])
            except EXPECTED_ERRORS as exc:
                self.failed = (text, images)
                self.transcript.append({"role": "system", "text": f"Error: {exc}", "retry": True})
                raise
            finally:
                self.listen(None)
                self.sync_to_inkscape()
                self.save()

            self.failed = None
            entry = {"role": "assistant", "text": result.reply, "steps": [asdict(step) for step in result.steps],
                     "selection": selection}
            self.transcript.append(entry)

            self.focus = selection
            self.save()

            return {**entry, "preview": self.preview(), "usage": self.usage.to_dict()}

    def listen(self, on_event: Callable[[str, object], None] | None) -> None:
        """Connects the agent's text and step callbacks to on_event, or disconnects them."""
        last_reload = time.monotonic()

        def on_step(step: Step) -> None:
            nonlocal last_reload
            if not step.failed and step.tool not in READ_ONLY_TOOLS and \
                    time.monotonic() - last_reload >= LIVE_RELOAD_SECONDS:
                try:
                    self.sync_to_inkscape()
                except LiveError:
                    pass
                last_reload = time.monotonic()
            if on_event:
                on_event("step", asdict(step))

        self.agent.on_step = on_step if on_event or self.live_enabled else None
        self.agent.on_text = (lambda piece: on_event("text", piece)) if on_event else None
        self.agent.on_usage = (lambda usage: on_event("usage", usage.to_dict())) if on_event else None

    def undo(self) -> dict:
        with self.lock:
            remaining = self.operations.undo(self.path)
            if remaining is None:
                raise ValueError("Nothing to undo.")

            self.notes.append("(I undid your last edit to the document.)")
            self.transcript.append({"role": "system", "text": "Undid the last change."})
            self.sync_to_inkscape()
            self.save()

            return {"text": "Undid the last change.", "preview": self.preview()}

    def preview(self) -> str:
        """The whole drawing, or the area around the objects that were selected for the last message."""
        png = self.operations.preview(self.path, 600, focus=self.focus or None)

        return base64.b64encode(png).decode("ascii")

    @staticmethod
    def images_note(names: list[str]) -> str:
        if not names:
            return ""

        return (f"(Attached images: {', '.join(names)}. They are for context; place_image puts one in the drawing "
                "if I ask for that.)")

    @staticmethod
    def selection_note(selection: list[str]) -> str:
        if not selection:
            return ""

        return (f"(Selected in Inkscape: {', '.join(selection)}. When I say this, these, it or them, I mean "
                "these objects.)")

    def sync_from_inkscape(self) -> list[str]:
        """Saves the drawing from the Inkscape window and returns the ids of the objects selected in it.

        Inkscape keeps the selection out of the file, so the selected objects are marked with an attribute
        first, and the mark is taken out of the saved file. Reloading the window afterwards clears it there."""
        if not self.live_enabled:
            return []

        try:
            self.live.run([f"object-set-attribute:{SELECTION_MARK},1"], self.window)
        except LiveError:
            pass
        self.live.save(self.window, self.path)

        return self.take_selection()

    def take_selection(self) -> list[str]:
        doc = SvgDocument.load(self.path)
        selection = doc.pop_marked(SELECTION_MARK)
        if selection:
            doc.save(self.path)

        return selection

    def sync_to_inkscape(self) -> None:
        if self.live_enabled:
            self.live.reload(self.window)


class Progress:
    """The reply being written, for the browser page to poll while it waits."""

    def __init__(self):
        self.lock = threading.Lock()
        self.text = ""
        self.steps: list[dict] = []
        self.usage: dict | None = None

    def reset(self) -> None:
        with self.lock:
            self.text, self.steps, self.usage = "", [], None

    def update(self, kind: str, payload) -> None:
        with self.lock:
            if kind == "text":
                self.text += str(payload)
            elif kind == "step":
                self.steps.append(payload)
            elif kind == "usage":
                self.usage = payload

    def snapshot(self) -> dict:
        with self.lock:
            return {"text": self.text, "steps": list(self.steps), "usage": self.usage}


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
            page = page.replace("{{LOGO}}", base64.b64encode(LOGO.read_bytes()).decode("ascii"))
            page = re.sub(r"\{\{icon:([a-z0-9-]+)\}\}", lambda match: svg(match.group(1)), page)
            return self.reply(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

        if url.path == "/api/progress" and self.authorized():
            return self.reply(HTTPStatus.OK, self.server.progress.snapshot())
        if url.path == "/api/state" and self.authorized():
            return self.reply(HTTPStatus.OK, self.session.state())

        self.reply(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self):
        if not self.authorized():
            return self.reply(HTTPStatus.FORBIDDEN, {"error": "Missing or wrong chat token."})

        routes = {"/api/message": self.send_message,
                  "/api/retry": self.retry_message,
                  "/api/clear": lambda body: self.session.clear(),
                  "/api/undo": lambda body: self.session.undo()}
        route = routes.get(urlparse(self.path).path)
        if route is None:
            return self.reply(HTTPStatus.NOT_FOUND, {"error": "Not found."})

        try:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self.reply(HTTPStatus.OK, {**route(body), "can_undo": self.session.can_undo})
        except EXPECTED_ERRORS as exc:
            self.reply(HTTPStatus.BAD_REQUEST, {"error": str(exc), "retry": self.session.can_retry,
                                                "can_undo": self.session.can_undo})

    def send_message(self, body: dict) -> dict:
        images = [self.session.attachments.add_data(str(image.get("name") or "image"),
                                                    base64.b64decode(str(image.get("data", ""))))
                  for image in body.get("images") or []]
        self.server.progress.reset()

        return self.session.send(str(body.get("text", "")), self.server.progress.update, images)

    def retry_message(self, _body: dict) -> dict:
        self.server.progress.reset()

        return self.session.retry(self.server.progress.update)

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
        self.progress = Progress()
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
    parser.add_argument("--ui", choices=["app", "browser"], default="app",
                        help="a desktop chat window (default; needs GTK 3) or a page in your browser")
    parser.add_argument("--port", type=int, default=0, help="browser page: port to listen on (default: any free port)")
    parser.add_argument("--model", help=f"default: the saved setting, or {DEFAULT_MODEL}")
    parser.add_argument("--effort", choices=EFFORTS, help="default: the saved setting, or medium")
    parser.add_argument("--backend", choices=BACKENDS,
                        help="api: your API key; claude-code: the `claude` command and its sign-in, e.g. a "
                             "subscription (default: the saved setting, else api when a key is set)")
    parser.add_argument("--no-browser", action="store_true", help="browser page: print the link instead of opening it")
    parser.add_argument("--no-live", action="store_true", help="do not open or sync an Inkscape window")
    parser.add_argument("--attach", action="store_true",
                        help="the document is already open in Inkscape; sync with the newest window (needs D-Bus; "
                             "without it the chat opens its own window)")
    parser.add_argument("--draft", action="store_true",
                        help="the document is a copy of a drawing that was never saved; open it in its own window")
    parser.add_argument("--window", type=int, help="Inkscape window number to sync with")
    parser.add_argument("--idle-minutes", type=float, help="browser page: stop after this many minutes without use")

    return parser.parse_args(argv)


def serve_in_browser(session: ChatSession, args: argparse.Namespace) -> None:
    server = ChatServer(session, args.port)
    print(f"Claude chat for {session.path}\nOpen: {server.url}\nPress Ctrl+C to stop.", flush=True)

    if session.owns_window and session.live_enabled:
        server.stop_when(lambda: not session.live_enabled)
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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    try:
        session = ChatSession.open(args)
    except (ClaudeError, ValueError) as exc:
        sys.exit(str(exc))

    try:
        if args.ui == "app":
            from .chat_window import ChatWindow

            if ChatWindow.supported():
                return ChatWindow.run(session)
            print("GTK 3 is not available to this Python, so the chat opens in your browser instead.", flush=True)

        serve_in_browser(session, args)
    finally:
        session.close()


if __name__ == "__main__":
    main()
