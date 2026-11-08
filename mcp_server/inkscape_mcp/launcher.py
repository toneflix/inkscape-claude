"""Starts the chat window as its own process, so it keeps running after the Inkscape extension returns."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .claude import Config


class LaunchError(RuntimeError):
    pass


class ChatLauncher:
    """Runs `python -m inkscape_mcp.chat` detached, with its output in chat.log next to the config file.

    If the chat stops within the first seconds, its log is turned into a LaunchError, so the user sees why.
    """

    startup_seconds = 4
    log_lines = 15

    def __init__(self, document: Path, python_paths: list[str], cwd: Path, python: str | None = None,
                 draft: bool = False):
        self.document = document
        self.python_paths = python_paths
        self.cwd = cwd
        self.python = python or self.windowless_python(sys.executable)
        self.draft = draft

    @staticmethod
    def draft_path(folder: Path | None = None) -> Path:
        """Where a drawing that was never saved is kept for the chat: ~/InkscapeClaude/Untitled <time>.svg."""
        folder = folder or Path.home() / "InkscapeClaude"
        path = folder / time.strftime("Untitled %Y-%m-%d %H%M%S.svg")
        number = 2
        while path.exists():
            path = path.with_name(f"{path.stem.split(' (')[0]} ({number}).svg")
            number += 1

        return path

    @property
    def log_path(self) -> Path:
        return Config.path().with_name("chat.log")

    @property
    def command(self) -> list[str]:
        mode = "--draft" if self.draft else "--attach"

        return [self.python, "-m", "inkscape_mcp.chat", str(self.document), mode, "--idle-minutes", "120"]

    @property
    def env(self) -> dict[str, str]:
        paths = [*self.python_paths, os.environ.get("PYTHONPATH", "")]

        return {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in paths if path), "PYTHONUNBUFFERED": "1"}

    def launch(self) -> subprocess.Popen:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.log_path, "w", encoding="utf-8") as log:
            process = self.start(log)

        try:
            code = process.wait(timeout=self.startup_seconds)
        except subprocess.TimeoutExpired:
            return process

        if code != 0:
            raise LaunchError(f"The chat stopped right after starting (exit code {code}).\n\n{self.log_tail()}\n\n"
                              f"Full log: {self.log_path}")

        return process

    def start(self, log) -> subprocess.Popen:
        options = dict(env=self.env, cwd=str(self.cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        if sys.platform != "win32":
            return subprocess.Popen(self.command, start_new_session=True, **options)

        detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(self.command, creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                                    **options)
        except OSError:
            return subprocess.Popen(self.command, creationflags=detached, **options)

    def log_tail(self) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return "(no log was written)"

        return "\n".join(lines[-self.log_lines:]) or "(the log is empty)"

    @staticmethod
    def windowless_python(executable: str) -> str:
        """pythonw.exe next to python.exe on Windows, so no console window flashes up; the same Python elsewhere."""
        path = Path(executable)
        windowless = path.with_name("pythonw.exe")
        if sys.platform == "win32" and path.name.lower() == "python.exe" and windowless.exists():
            return str(windowless)

        return executable
