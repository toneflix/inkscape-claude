"""Document operations shared by the MCP tools and the tool-use agent."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, TypeVar

from .cli import Inkscape
from .svgdoc import SvgDocument
from .workspace import History

T = TypeVar("T")

STYLE_ATTRIBUTES = ("style", "class", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
                    "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity", "filter")

BOOLEAN_ACTIONS = {"union": "path-union", "difference": "path-difference", "intersection": "path-intersection",
                   "exclusion": "path-exclusion", "division": "path-division", "cut": "path-cut",
                   "combine": "path-combine"}

SINGLE_RESULT_OPERATIONS = {"union", "difference", "intersection", "exclusion", "combine"}


class DocumentOperations:
    """Edits SVG files on disk, using Inkscape where geometry is involved, with undo snapshots."""

    def __init__(self, inkscape: Inkscape, history: History | None = None):
        self.inkscape = inkscape
        self.history = history or History()

    def write(self, path: Path, doc: SvgDocument) -> None:
        self.history.snapshot(path)
        doc.save(path)

    def edit(self, path: Path, change: Callable[[SvgDocument], T]) -> T:
        doc = SvgDocument.load(path)
        result = change(doc)
        self.write(path, doc)

        return result

    def undo(self, path: Path) -> int | None:
        return self.history.undo(path)

    def outline(self, path: Path, with_bboxes: bool = False) -> str:
        bboxes = self.inkscape.query_all(path) if with_bboxes else None

        return SvgDocument.load(path).outline(bboxes=bboxes)

    def element(self, path: Path, element_id: str) -> str:
        return SvgDocument.load(path).element_source(element_id)

    def add(self, path: Path, fragment: str, parent_id: str | None = None, before_id: str | None = None) -> list[str]:
        return self.edit(path, lambda doc: doc.add(fragment, parent_id, before_id))

    def update(self, path: Path, element_id: str, attributes: dict | None = None, style: dict | None = None,
               text: str | None = None) -> None:
        self.edit(path, lambda doc: doc.update(element_id, attributes, style, text))

    def replace(self, path: Path, element_id: str, fragment: str) -> list[str]:
        return self.edit(path, lambda doc: doc.replace(element_id, fragment))

    def delete(self, path: Path, element_ids: list[str]) -> list[str]:
        return self.edit(path, lambda doc: doc.delete(element_ids))

    def run_actions(self, source: Path, actions: list[str], target: Path | None = None) -> str:
        target = target or source
        self.history.snapshot(target)

        return self.inkscape.run_actions(source, actions, output_path=target)

    def boolean(self, path: Path, element_ids: list[str], operation: str, keep_style_of_first: bool = True) -> str:
        if len(element_ids) < 2:
            raise ValueError("Need at least two element ids.")
        if operation not in BOOLEAN_ACTIONS:
            raise ValueError(f"Unknown operation '{operation}'. Use one of: {', '.join(BOOLEAN_ACTIONS)}.")

        first = SvgDocument.load(path).find(element_ids[0])
        saved_style = {name: first.get(name) for name in STYLE_ATTRIBUTES}

        log = self.run_actions(path, [f"select-by-id:{','.join(element_ids)}", "object-to-path",
                                      BOOLEAN_ACTIONS[operation]])

        doc = SvgDocument.load(path)
        survivors = [element_id for element_id in element_ids if doc.has(element_id)]
        if keep_style_of_first and operation in SINGLE_RESULT_OPERATIONS and len(survivors) == 1:
            result = doc.find(survivors[0])
            for name, value in saved_style.items():
                if value is None:
                    result.attrib.pop(name, None)
                else:
                    result.set(name, value)
            doc.save(path)

        return log

    def align(self, path: Path, element_ids: list[str], horizontal: str | None = None, vertical: str | None = None,
              relative_to: str = "page") -> str:
        if not horizontal and not vertical:
            raise ValueError("Give horizontal and/or vertical.")

        anchor = " ".join(side for side in (horizontal, vertical) if side)

        return self.run_actions(path, [f"select-by-id:{','.join(element_ids)}", f"object-align:{anchor} {relative_to}"])

    def bounding_boxes(self, path: Path, element_ids: list[str] | None = None) -> str:
        boxes = self.inkscape.query_all(path)
        if element_ids:
            boxes = {key: box for key, box in boxes.items() if key in element_ids}

        lines = [f"{key}: x={x:.2f} y={y:.2f} w={w:.2f} h={h:.2f}" for key, (x, y, w, h) in boxes.items()]

        return "\n".join(lines) or "No matches."

    def preview(self, path: Path, width: int = 800, element_id: str | None = None,
                background: str = "#ffffff") -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "preview.png"
            self.inkscape.export(path, output, "png", width=width, object_id=element_id, background=background)

            return output.read_bytes()
