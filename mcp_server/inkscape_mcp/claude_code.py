"""Runs the chat through the `claude` command (Claude Code), so a Claude subscription works without an API key."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .agent import SYSTEM_PROMPT, TOOLS, Step, TurnResult, summary
from .bridge import SERVER_NAME
from .claude import ClaudeClient, ClaudeError, Config

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"


class ClaudeCode:
    """Finds the `claude` command: the configured path, CLAUDE_CODE_BIN, PATH, then the usual install places,
    because apps started from a menu often get a short PATH."""

    @staticmethod
    def candidates(configured: str | None = None) -> list[str]:
        home = Path.home()
        found = [configured, os.environ.get("CLAUDE_CODE_BIN"), shutil.which("claude")]
        found += [str(home / ".claude" / "local" / "claude"), str(home / ".local" / "bin" / "claude"),
                  "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
        if os.name == "nt":
            found += [str(home / ".local" / "bin" / "claude.exe")]
            if os.environ.get("APPDATA"):
                found += [str(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")]

        return [path for path in found if path]

    @classmethod
    def find(cls, configured: str | None = None) -> str | None:
        for candidate in cls.candidates(configured):
            path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
            if path:
                return path

        return None


class ClaudeCodeAgent:
    """Same interface as DrawingAgent, but each turn is one `claude -p` run with the drawing tools served by
    the bridge. Claude Code keeps the conversation, resumed by session id."""

    def __init__(self, settings: ClaudeClient, path: Path, history: Path, binary: str | None = None,
                 on_step: Callable[[Step], None] | None = None):
        self.settings = settings
        self.path = path
        self.history = history
        self.binary = binary
        self.on_step = on_step
        self.session_id: str | None = None

    @property
    def client(self) -> ClaudeClient:
        return self.settings

    def mcp_config(self) -> str:
        env = {"PYTHONPATH": os.pathsep.join(filter(None, [str(PACKAGE_ROOT), os.environ.get("PYTHONPATH")]))}
        server = {"command": sys.executable,
                  "args": ["-m", "inkscape_mcp.bridge", str(self.path), "--history", str(self.history)],
                  "env": env}

        return json.dumps({"mcpServers": {SERVER_NAME: server}})

    def command(self, binary: str) -> list[str]:
        allowed = ",".join(TOOL_PREFIX + tool["name"] for tool in TOOLS)
        command = [binary, "-p", "--output-format", "stream-json", "--verbose",
                   "--model", self.settings.model, "--effort", self.settings.effort,
                   "--append-system-prompt", SYSTEM_PROMPT, "--strict-mcp-config",
                   f"--mcp-config={self.mcp_config()}", f"--allowedTools={allowed}", "--tools="]
        if self.session_id:
            command += ["--resume", self.session_id]

        return command

    @staticmethod
    def environment() -> dict[str, str]:
        """Without ANTHROPIC_API_KEY, so Claude Code uses the signed-in subscription."""
        return {name: value for name, value in os.environ.items() if name != "ANTHROPIC_API_KEY"}

    def run(self, prompt: str, context: list[dict] | None = None) -> TurnResult:
        binary = ClaudeCode.find(self.binary)
        if binary is None:
            raise ClaudeError("Could not find Claude Code (the `claude` command). Install it from "
                              "claude.com/claude-code and sign in once by running `claude`, or put its path in "
                              f"{Config.path()} as {{\"claude_path\": \"...\"}}.")

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(self.command(binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                       cwd=self.history, env=self.environment(), creationflags=flags)
        except OSError as exc:
            raise ClaudeError(f"Could not start Claude Code ({binary}): {exc}") from exc

        try:
            process.stdin.write(prompt)
            process.stdin.close()
            result, steps = self.read(process.stdout)
        except OSError:
            result, steps = None, []
        finally:
            process.stdout.close()
            errors = process.stderr.read().strip()
            process.wait()

        return self.finish(result, steps, errors)

    def read(self, lines) -> tuple[dict | None, list[Step]]:
        """Turns the stream-json events into steps, and returns the final result event."""
        names: dict[str, str] = {}
        steps: list[Step] = []
        result = None

        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue

            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                self.check_tools(event)
            elif kind == "assistant":
                for block in self.blocks(event, "tool_use"):
                    names[block.get("id")] = block.get("name", "").removeprefix(TOOL_PREFIX)
            elif kind == "user":
                for block in self.blocks(event, "tool_result"):
                    step = Step(names.get(block.get("tool_use_id"), "tool"), self.result_text(block),
                                failed=bool(block.get("is_error")))
                    steps.append(step)
                    if self.on_step:
                        self.on_step(step)
            elif kind == "result":
                result = event
            if event.get("session_id"):
                self.session_id = event["session_id"]

        return result, steps

    @staticmethod
    def check_tools(event: dict) -> None:
        for server in event.get("mcp_servers") or []:
            if server.get("name") == SERVER_NAME and server.get("status") not in ("connected", None):
                raise ClaudeError(f"The drawing tools did not start in Claude Code ({server.get('status')}).")

    @staticmethod
    def blocks(event: dict, kind: str) -> list[dict]:
        content = (event.get("message") or {}).get("content")

        return [block for block in content if block.get("type") == kind] if isinstance(content, list) else []

    @staticmethod
    def result_text(block: dict) -> str:
        content = block.get("content")
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content if part.get("type") == "text"]
            return summary("\n".join(texts)) if texts else summary(content)

        return summary(str(content or ""))

    @staticmethod
    def finish(result: dict | None, steps: list[Step], errors: str) -> TurnResult:
        if result is None:
            detail = errors.splitlines()[-1] if errors else "it stopped without an answer"
            raise ClaudeError(f"Claude Code failed: {detail}")

        text = str(result.get("result") or "").strip()
        if result.get("is_error") or result.get("subtype") != "success":
            raise ClaudeError(f"Claude Code: {text or result.get('subtype', 'error')}")
        if not text:
            raise ClaudeError("Claude returned an empty answer.")

        return TurnResult(text, steps)
