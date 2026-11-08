#!/usr/bin/env python3
"""Open Claude Chat: opens the chat window for the current drawing (Extensions > Claude).

The menu item has no dialog; the model, effort and API key are set in the chat window's Settings. A drawing that
was never saved is saved as a draft first, which the chat opens in its own window.
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
        draft = not document or not Path(document).exists()
        if draft:
            document = self.save_draft()

        launcher = ChatLauncher(Path(document), [str(HERE), str(HERE.parent / "mcp_server")], cwd=HERE, draft=draft)
        try:
            launcher.launch()
        except (LaunchError, OSError) as exc:
            raise inkex.AbortExtension(f"Could not open Claude Chat. {exc}")

    def save_draft(self) -> Path:
        path = ChatLauncher.draft_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.document.write(str(path), encoding="utf-8", xml_declaration=True)
        except OSError as exc:
            raise inkex.AbortExtension(f"Could not save the drawing for the chat at {path}: {exc}. "
                                       "Save it yourself, then open the chat again.")

        return path


if __name__ == "__main__":
    ClaudeChat().run()
