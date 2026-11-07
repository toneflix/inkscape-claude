"""Document operations shared by the MCP tools and the tool-use agent."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, TypeVar

from .cli import Inkscape
from .pages import Box, Page
from .svgdoc import SvgDocument
from .workspace import History

T = TypeVar("T")

STYLE_ATTRIBUTES = ("style", "class", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
                    "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity", "filter")

BOOLEAN_ACTIONS = {"union": "path-union", "difference": "path-difference", "intersection": "path-intersection",
                   "exclusion": "path-exclusion", "division": "path-division", "cut": "path-cut",
                   "combine": "path-combine"}

SINGLE_RESULT_OPERATIONS = {"union", "difference", "intersection", "exclusion", "combine"}

Area = str | tuple[float, float, float, float]


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

    def boxes(self, path: Path, doc: SvgDocument | None = None) -> dict[str, Box]:
        """Inkscape-measured bounding boxes, converted from px to the document's user units."""
        doc = doc or SvgDocument.load(path)

        return {key: doc.from_px(box) for key, box in self.inkscape.query_all(path).items()}

    def outline(self, path: Path, with_bboxes: bool = False) -> str:
        doc = SvgDocument.load(path)

        return doc.outline(bboxes=self.boxes(path, doc) if with_bboxes else None)

    def pages(self, path: Path) -> str:
        doc = SvgDocument.load(path)
        boxes = self.boxes(path, doc)
        objects = [(item.get("id"), boxes[item.get("id")]) for item in doc.drawn_objects() if item.get("id") in boxes]
        lines = []

        scale = doc.user_unit_px()
        if abs(scale - 1) > 1e-6:
            lines.append(f"Positions and sizes are in user units (1 user unit = {scale:.4g}px).")

        for page in doc.pages.list():
            on_page = [object_id for object_id, box in objects if page.overlap(box) > 0]
            lines.append(page.describe())
            lines.append(f"  objects: {', '.join(on_page) if on_page else '(empty)'}")

        off_page = [object_id for object_id, box in objects if not doc.pages.pages_under(box)]
        if off_page:
            lines.append(f"Off every page: {', '.join(off_page)}")

        return "\n".join(lines)

    def add_page(self, path: Path, width: float | None = None, height: float | None = None, x: float | None = None,
                 y: float | None = None, label: str | None = None) -> Page:
        return self.edit(path, lambda doc: doc.pages.add(width, height, x, y, label))

    def update_page(self, path: Path, page: str | int, label: str | None = None, x: float | None = None,
                    y: float | None = None, width: float | None = None, height: float | None = None) -> Page:
        return self.edit(path, lambda doc: doc.pages.update(page, label, x, y, width, height))

    def delete_page(self, path: Path, page: str | int) -> Page:
        return self.edit(path, lambda doc: doc.pages.delete(page))

    def move_to_page(self, path: Path, element_ids: list[str], page: str | int) -> Page:
        """Moves each object by the offset between its page and the target page, so it keeps its spot on the
        page. Objects that are on no page are centred on the target page."""
        doc = SvgDocument.load(path)
        target = doc.pages.get(page)
        boxes = self._boxes_for(path, doc, element_ids)
        moves = {}

        for element_id, box in boxes.items():
            current = doc.pages.page_of(box)
            if current:
                moves[element_id] = (target.x - current.x, target.y - current.y)
            else:
                x, y, width, height = box
                moves[element_id] = (target.x + (target.width - width) / 2 - x,
                                     target.y + (target.height - height) / 2 - y)

        self._translate(path, doc, moves)

        return target

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
              relative_to: str = "page", page: str | int | None = None) -> str:
        """With several pages, "page" means the page given, or else the page each object is on (Inkscape's own
        Align would use the first page)."""
        if not horizontal and not vertical:
            raise ValueError("Give horizontal and/or vertical.")

        doc = SvgDocument.load(path)
        if relative_to == "page" and (page is not None or len(doc.pages.list()) > 1):
            return self._align_to_page(path, doc, element_ids, horizontal, vertical, page)

        anchor = " ".join(side for side in (horizontal, vertical) if side)

        return self.run_actions(path, [f"select-by-id:{','.join(element_ids)}", f"object-align:{anchor} {relative_to}"])

    def bounding_boxes(self, path: Path, element_ids: list[str] | None = None) -> str:
        boxes = self.boxes(path)
        if element_ids:
            boxes = {key: box for key, box in boxes.items() if key in element_ids}

        lines = [f"{key}: x={x:.2f} y={y:.2f} w={w:.2f} h={h:.2f}" for key, (x, y, w, h) in boxes.items()]

        return "\n".join(lines) or "No matches."

    def area(self, path: Path, page: str | int | None = None, area: str = "pages") -> Area:
        """An export area: one page, all pages ("pages"), or Inkscape's own "page" (the first) or "drawing"."""
        doc = SvgDocument.load(path)

        if page is not None:
            return doc.to_px(doc.pages.get(page).box)
        if area == "pages":
            return doc.to_px(doc.pages.bounds())

        return area

    def focus_area(self, path: Path, element_ids: list[str], padding: float = 0.15) -> Area | None:
        """The px area around the given elements with some room on each side, widened so it is at most twice as
        tall as wide, or None when none of them is drawn any more."""
        doc = SvgDocument.load(path)
        boxes = [box for key, box in self.boxes(path, doc).items() if key in element_ids]
        if not boxes:
            return None

        left = min(x for x, _, _, _ in boxes)
        top = min(y for _, y, _, _ in boxes)
        width = max(x + w for x, _, w, _ in boxes) - left
        height = max(y + h for _, y, _, h in boxes) - top
        margin = max(width, height, 1.0) * padding
        left, top, width, height = left - margin, top - margin, width + 2 * margin, height + 2 * margin

        extra = max(0.0, height / 2 - width)

        return doc.to_px((left - extra / 2, top, width + extra, height))

    def preview(self, path: Path, width: int = 800, element_id: str | None = None,
                background: str = "#ffffff", page: str | int | None = None,
                focus: list[str] | None = None) -> bytes:
        """Renders one element, one page, the area around the `focus` elements, or by default every page
        together."""
        area = (focus and self.focus_area(path, focus)) or ("page" if element_id else self.area(path, page))

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "preview.png"
            self.inkscape.export(path, output, "png", width=width, object_id=element_id, area=area,
                                 background=background)

            return output.read_bytes()

    def _boxes_for(self, path: Path, doc: SvgDocument, element_ids: list[str]) -> dict[str, Box]:
        if not element_ids:
            raise ValueError("Give at least one element id.")

        boxes = self.boxes(path, doc)
        missing = [element_id for element_id in element_ids if element_id not in boxes]
        if missing:
            raise ValueError(f"No visible element with id: {', '.join(missing)}.")

        return {element_id: boxes[element_id] for element_id in element_ids}

    def _align_to_page(self, path: Path, doc: SvgDocument, element_ids: list[str], horizontal: str | None,
                       vertical: str | None, page: str | int | None) -> str:
        target = doc.pages.get(page) if page is not None else None
        moves = {}

        for element_id, box in self._boxes_for(path, doc, element_ids).items():
            frame = target or doc.pages.page_of(box) or doc.pages.list()[0]
            moves[element_id] = (self._offset(horizontal, box[0], box[2], frame.x, frame.width, "left", "right"),
                                 self._offset(vertical, box[1], box[3], frame.y, frame.height, "top", "bottom"))

        return self._translate(path, doc, moves)

    @staticmethod
    def _offset(side: str | None, start: float, size: float, frame_start: float, frame_size: float,
                low: str, high: str) -> float:
        if side == low:
            return frame_start - start
        if side == high:
            return frame_start + frame_size - start - size
        if side:
            return frame_start + (frame_size - size) / 2 - start

        return 0.0

    def _translate(self, path: Path, doc: SvgDocument, moves: dict[str, tuple[float, float]]) -> str:
        """Moves objects with Inkscape (it handles transforms and nesting); offsets are in user units."""
        scale = doc.user_unit_px()
        actions = []

        for element_id, (dx, dy) in moves.items():
            if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                actions += ["select-clear", f"select-by-id:{element_id}",
                            f"transform-translate:{dx * scale:g},{dy * scale:g}"]

        return self.run_actions(path, actions) if actions else ""
