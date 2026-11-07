"""Images attached to chat messages: kept in a folder for the conversation, sent to Claude, and placeable in the
drawing."""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"),
              (b"GIF89a", "image/gif"))


def media_type(data: bytes) -> str | None:
    for signature, kind in SIGNATURES:
        if data.startswith(signature):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    return None


def pixel_size(data: bytes) -> tuple[int, int] | None:
    """Width and height read from a PNG, GIF, JPEG or WebP header."""
    kind = media_type(data)
    try:
        if kind == "image/png":
            return struct.unpack(">II", data[16:24])
        if kind == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if kind == "image/webp":
            return webp_size(data)
        if kind == "image/jpeg":
            return jpeg_size(data)
    except struct.error:
        return None

    return None


def webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1

    return None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
            return width, height
        offset += 2 + length

    return None


@dataclass
class Attachment:
    path: Path
    media_type: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()

    @property
    def size(self) -> tuple[int, int] | None:
        return pixel_size(self.data)

    def encoded(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def block(self) -> dict:
        """The image as a Messages API content block."""
        return {"type": "image", "source": {"type": "base64", "media_type": self.media_type, "data": self.encoded()}}

    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.encoded()}"


class Attachments:
    """The conversation's attached images, copied into one folder so the drawing tools can find them by name."""

    def __init__(self, folder: Path):
        self.folder = folder

    def add(self, source: Path) -> Attachment:
        source = Path(source).expanduser()
        if not source.is_file():
            raise ValueError(f"{source} is not a file.")
        if source.stat().st_size > MAX_BYTES:
            raise ValueError(f"{source.name} is larger than 5 MB. Use a smaller or more compressed image.")

        return self.add_data(source.name, source.read_bytes())

    def add_data(self, name: str, data: bytes) -> Attachment:
        if len(data) > MAX_BYTES:
            raise ValueError(f"{name} is larger than 5 MB. Use a smaller or more compressed image.")

        kind = media_type(data[:16])
        if kind is None:
            raise ValueError(f"{name} is not a PNG, JPEG, GIF or WebP image.")

        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.unique(name)
        target.write_bytes(data)

        return Attachment(target, kind)

    def unique(self, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in name).strip(".") or "image"
        target = self.folder / safe
        stem, suffix = target.stem, target.suffix
        number = 2
        while target.exists():
            target = self.folder / f"{stem}-{number}{suffix}"
            number += 1

        return target

    def find(self, name: str) -> Attachment:
        path = self.folder / Path(str(name)).name
        kind = media_type(path.read_bytes()[:16]) if path.is_file() else None
        if kind is None:
            known = ", ".join(self.names()) or "none yet"
            raise ValueError(f"No attached image named '{name}'. Attached: {known}.")

        return Attachment(path, kind)

    def names(self) -> list[str]:
        if not self.folder.is_dir():
            return []

        return sorted(path.name for path in self.folder.iterdir() if path.is_file())
