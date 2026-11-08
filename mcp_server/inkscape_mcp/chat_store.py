"""Keeps each drawing's chat between runs: what was said, what Claude remembers, and the attached images."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .claude import Config

IMAGE_LEFT_OUT = "(An image was here; it is not kept between chat sessions.)"


class ChatStore:
    """One folder per drawing under the config folder's `chats`, named after the file and a hash of its path."""

    def __init__(self, document: Path, root: Path | None = None):
        self.document = document.resolve()
        digest = hashlib.sha256(str(self.document).encode("utf-8")).hexdigest()[:12]
        stem = "".join(c if c.isalnum() or c in "-_" else "-" for c in self.document.stem)[:40] or "drawing"
        self.folder = (root or Config.path().parent / "chats") / f"{stem}-{digest}"

    @property
    def file(self) -> Path:
        return self.folder / "chat.json"

    @property
    def attachments(self) -> Path:
        return self.folder / "attachments"

    def load(self) -> dict:
        """The saved chat, or {} when there is none or it cannot be read."""
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

        return data if isinstance(data, dict) and data.get("document") == str(self.document) else {}

    def save(self, data: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps({**data, "document": str(self.document)}), encoding="utf-8")
        temporary.replace(self.file)

    def clear(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)

    @classmethod
    def without_images(cls, messages: list[dict]) -> list[dict]:
        """API messages with image data swapped for a note, so the saved chat stays small."""
        return [{**message, "content": cls.strip(message.get("content"))} for message in messages]

    @classmethod
    def strip(cls, content):
        if not isinstance(content, list):
            return content

        blocks = []
        for block in content:
            if not isinstance(block, dict):
                blocks.append(block)
            elif block.get("type") == "image":
                blocks.append({"type": "text", "text": IMAGE_LEFT_OUT})
            elif block.get("type") == "tool_result":
                blocks.append({**block, "content": cls.strip(block.get("content"))})
            else:
                blocks.append(block)

        return blocks
