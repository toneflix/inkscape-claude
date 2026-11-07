#!/usr/bin/env python3
"""Open Claude Chat: opens the chat window for the current drawing (Extensions > Claude).

The menu item has no dialog; the model, effort and API key are set in the chat window's Settings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import inkex

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from inkscape_mcp.launcher import ChatLauncher, LaunchError  # noqa: E402


class ClaudeChat(inkex.EffectExtension):
    def effect(self):
        document = self.document_path()
        if not document or not Path(document).exists():
            raise inkex.AbortExtension("Save the drawing to a file first, then open the chat again.")

        launcher = ChatLauncher(Path(document), [str(HERE), str(HERE.parent / "mcp_server")], cwd=HERE)
        try:
            launcher.launch()
        except (LaunchError, OSError) as exc:
            raise inkex.AbortExtension(f"Could not open Claude Chat. {exc}")


if __name__ == "__main__":
    ClaudeChat().run()
