#!/usr/bin/env python3
"""Open Claude Chat: opens the chat window for the current drawing (Extensions > Claude).

The menu item has no dialog; the model, effort and API key are set in the chat window's Settings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import inkex

HERE = Path(__file__).resolve().parent


class ClaudeChat(inkex.EffectExtension):
    def effect(self):
        document = self.document_path()
        if not document or not Path(document).exists():
            raise inkex.AbortExtension("Save the drawing to a file first, then open the chat again.")

        paths = [str(HERE), str(HERE.parent / "mcp_server"), os.environ.get("PYTHONPATH", "")]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
        command = [sys.executable, "-m", "inkscape_mcp.chat", str(document), "--attach", "--idle-minutes", "120"]

        subprocess.Popen(command, env=env, cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


if __name__ == "__main__":
    ClaudeChat().run()
