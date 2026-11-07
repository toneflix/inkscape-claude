#!/usr/bin/env python3
"""Open Claude Chat: starts the chat panel for the current drawing (Extensions > Claude)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import inkex

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "mcp_server")]

from inkscape_mcp.claude import DEFAULT_MODEL, ClaudeError, Config  # noqa: E402


class ClaudeChat(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--tab", default="chat")
        pars.add_argument("--model", default=DEFAULT_MODEL)
        pars.add_argument("--effort", default="medium")
        pars.add_argument("--api_key", default="")

    def effect(self):
        document = self.document_path()
        if not document or not Path(document).exists():
            raise inkex.AbortExtension("Save the drawing to a file first, then open the chat again.")

        try:
            api_key = Config.api_key(self.options.api_key)
        except ClaudeError as exc:
            raise inkex.AbortExtension(str(exc))

        env = {**os.environ, "ANTHROPIC_API_KEY": api_key,
               "PYTHONPATH": os.pathsep.join([str(HERE), str(HERE.parent / "mcp_server"), os.environ.get("PYTHONPATH", "")])}
        command = [sys.executable, "-m", "inkscape_mcp.chat", str(document), "--attach",
                   "--model", self.options.model.strip() or DEFAULT_MODEL, "--effort", self.options.effort,
                   "--idle-minutes", "120"]

        subprocess.Popen(command, env=env, cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

        self.msg(f"The Claude chat is opening in your browser for {Path(document).name}.")


if __name__ == "__main__":
    ClaudeChat().run()
