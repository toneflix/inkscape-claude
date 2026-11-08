"""Inkscape's recently used files (GTK's recently-used.xbel), to notice when a drawing was saved under a new name
with Inkscape's own File > Save As, which Inkscape tells no other program about."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from urllib.parse import unquote, urlparse

from .svgdoc import SODIPODI_NS, SVG_NS, SvgDocument, SvgError
from .usage import timestamp

APPLICATION = "{http://www.freedesktop.org/standards/desktop-bookmarks}application"
SAME_DRAWING = 0.6
CONTENT_IDS = ("//*[@id][not(ancestor-or-self::svg:defs or ancestor-or-self::sodipodi:namedview or "
               "ancestor-or-self::svg:metadata)][not(self::svg:svg)]/@id")
NAMESPACES = {"svg": SVG_NS, "sodipodi": SODIPODI_NS}


class RecentFiles:
    def __init__(self, files: list[Path] | None = None):
        self.files = files if files is not None else self.default_files()

    @staticmethod
    def default_files() -> list[Path]:
        """Where GTK keeps the list: the user data folder (XDG_DATA_HOME or ~/.local/share), and on Windows the
        local application data folder."""
        folders = [Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")]
        if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
            folders.insert(0, Path(os.environ["LOCALAPPDATA"]))

        return [folder / "recently-used.xbel" for folder in folders]

    def saved_by_inkscape(self, since: float) -> list[Path]:
        """SVG files Inkscape used after `since` (epoch seconds) that still exist, newest first."""
        found: dict[Path, float] = {}
        for file in self.files:
            try:
                root = ElementTree.parse(file).getroot()
            except (OSError, ElementTree.ParseError):
                continue

            for bookmark in root.iter("bookmark"):
                url = urlparse(bookmark.get("href", ""))
                path = Path(unquote(url.path).lstrip("/") if url.path[2:3] == ":" else unquote(url.path))
                for application in bookmark.iter(APPLICATION):
                    when = timestamp(application.get("modified", ""))
                    if url.scheme == "file" and path.suffix.lower() == ".svg" and when and when > since and \
                            "inkscape" in application.get("name", "").lower() and path.is_file():
                        found[path] = max(when, found.get(path, 0))

        return sorted(found, key=found.get, reverse=True)

    @staticmethod
    def same_drawing(first: Path, second: Path) -> bool:
        """Whether two files look like versions of one drawing: most ids of the drawn objects in common."""
        try:
            ids = [set(SvgDocument.load(path).root.xpath(CONTENT_IDS, namespaces=NAMESPACES))
                   for path in (first, second)]
        except (OSError, SvgError, ValueError):
            return False

        union = ids[0] | ids[1]

        return bool(union) and len(ids[0] & ids[1]) / len(union) >= SAME_DRAWING
