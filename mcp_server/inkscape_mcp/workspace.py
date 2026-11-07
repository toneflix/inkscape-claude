"""Workspace folder that the MCP tools read and write in, with per-file undo history."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, TypeVar

from .svgdoc import SvgDocument

HISTORY_FOLDER = ".inkscape-claude-history"

T = TypeVar("T")


class History:
    """Numbered snapshots of a file, kept next to it in a hidden folder."""

    def __init__(self, limit: int = 25):
        self.limit = limit

    def folder(self, path: Path) -> Path:
        return path.parent / HISTORY_FOLDER / path.name

    def snapshots(self, path: Path) -> list[Path]:
        return sorted(self.folder(path).glob("*.svg"))

    def snapshot(self, path: Path) -> None:
        if not path.exists():
            return

        folder = self.folder(path)
        folder.mkdir(parents=True, exist_ok=True)
        existing = self.snapshots(path)
        number = int(existing[-1].stem) + 1 if existing else 1
        shutil.copy2(path, folder / f"{number:05d}.svg")

        for old in existing[: max(0, len(existing) + 1 - self.limit)]:
            old.unlink()

    def undo(self, path: Path) -> int | None:
        snapshots = self.snapshots(path)
        if not snapshots:
            return None

        os.replace(snapshots[-1], path)

        return len(snapshots) - 1


class Workspace:
    """Resolves tool paths inside one root folder and saves documents with history."""

    def __init__(self, root: Path, allow_any_path: bool = False, history: History | None = None):
        self.root = root.expanduser().resolve()
        self.allow_any_path = allow_any_path
        self.history = history or History()

    @classmethod
    def from_env(cls) -> Workspace:
        root = Path(os.environ.get("INKSCAPE_MCP_WORKSPACE", Path.home() / "InkscapeClaude"))

        return cls(root, os.environ.get("INKSCAPE_MCP_ALLOW_ANY_PATH") == "1")

    def resolve(self, path: str, must_exist: bool = True) -> Path:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = self.root / resolved
        resolved = resolved.resolve()

        if not self.allow_any_path and self.root not in (resolved, *resolved.parents):
            raise ValueError(f"Path '{path}' is outside the workspace {self.root}. "
                             "Set INKSCAPE_MCP_ALLOW_ANY_PATH=1 to allow any path.")
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"'{resolved}' does not exist.")

        return resolved

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def documents(self) -> list[Path]:
        self.root.mkdir(parents=True, exist_ok=True)

        return sorted(f for f in self.root.rglob("*.svg") if HISTORY_FOLDER not in f.parts)

    def load(self, path: str) -> SvgDocument:
        return SvgDocument.load(self.resolve(path))

    def write(self, target: Path, doc: SvgDocument) -> None:
        self.history.snapshot(target)
        doc.save(target)

    def edit(self, path: str, change: Callable[[SvgDocument], T]) -> tuple[Path, T]:
        target = self.resolve(path)
        doc = SvgDocument.load(target)
        result = change(doc)
        self.write(target, doc)

        return target, result
