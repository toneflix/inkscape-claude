"""Pages (artboards) of an Inkscape 1.2+ document, in SVG user units."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lxml import etree

if TYPE_CHECKING:
    from .svgdoc import SvgDocument

INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"

UNIT_PX = {"": 1.0, "px": 1.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pt": 96 / 72, "pc": 16.0, "q": 96 / 101.6}

PAGE_GAP = 20.0

Box = tuple[float, float, float, float]


class PageError(ValueError):
    pass


@dataclass
class Page:
    number: int
    id: str | None
    label: str | None
    x: float
    y: float
    width: float
    height: float

    @property
    def box(self) -> Box:
        return self.x, self.y, self.width, self.height

    def overlap(self, box: Box) -> float:
        x, y, width, height = box
        across = min(self.x + self.width, x + width) - max(self.x, x)
        down = min(self.y + self.height, y + height) - max(self.y, y)

        return max(across, 0.0) * max(down, 0.0)

    def describe(self) -> str:
        name = f' "{self.label}"' if self.label else ""
        ident = f" id={self.id}" if self.id else ""

        return f"page {self.number}{ident}{name}: x={self.x:g} y={self.y:g} size={self.width:g}x{self.height:g}"


class Pages:
    """Reads and edits the inkscape:page elements in the namedview. A document without any has one implicit
    page: the viewBox."""

    def __init__(self, doc: SvgDocument):
        self.doc = doc

    @property
    def root(self):
        return self.doc.root

    def elements(self) -> list:
        return self.root.xpath("./sodipodi:namedview/inkscape:page",
                               namespaces={"sodipodi": SODIPODI_NS, "inkscape": INKSCAPE_NS})

    def list(self) -> list[Page]:
        elements = self.elements()
        if not elements:
            x, y, width, height = self.doc.viewbox()
            return [Page(1, None, None, x, y, width, height)]

        return [self._page(number, element) for number, element in enumerate(elements, 1)]

    def get(self, ref: str | int) -> Page:
        pages = self.list()
        text = str(ref).strip()

        for page in pages:
            if text in (page.id, page.label):
                return page

        if text.isdigit() and 1 <= int(text) <= len(pages):
            return pages[int(text) - 1]

        names = ", ".join(page.label or page.id or str(page.number) for page in pages)
        raise PageError(f"No page '{ref}'. Pages: {names}. Use a page number, id or label.")

    def bounds(self) -> Box:
        pages = self.list()
        left = min(page.x for page in pages)
        top = min(page.y for page in pages)
        right = max(page.x + page.width for page in pages)
        bottom = max(page.y + page.height for page in pages)

        return left, top, right - left, bottom - top

    def pages_under(self, box: Box) -> list[Page]:
        return [page for page in self.list() if page.overlap(box) > 0]

    def page_of(self, box: Box) -> Page | None:
        best = max(self.list(), key=lambda page: page.overlap(box))

        return best if best.overlap(box) > 0 else None

    def add(self, width: float | None = None, height: float | None = None, x: float | None = None,
            y: float | None = None, label: str | None = None) -> Page:
        pages = self.list()
        if not self.elements():
            self._write(self._namedview(), pages[0])

        last = pages[-1]
        width = float(width or last.width)
        height = float(height or last.height)
        if x is None:
            x = max(page.x + page.width for page in pages) + PAGE_GAP
        if y is None:
            y = last.y

        element = etree.SubElement(self._namedview(), f"{{{INKSCAPE_NS}}}page")
        self._write_box(element, float(x), float(y), width, height)
        if label:
            element.set(f"{{{INKSCAPE_NS}}}label", label)
        element.set("id", self._free_id())

        return self.list()[-1]

    def update(self, ref: str | int, label: str | None = None, x: float | None = None, y: float | None = None,
               width: float | None = None, height: float | None = None) -> Page:
        page = self.get(ref)
        if not self.elements():
            self._write(self._namedview(), page)
        element = self.elements()[page.number - 1]

        box = [current if value is None else float(value) for value, current in zip((x, y, width, height), page.box)]
        self._write_box(element, *box)
        if label is not None:
            if label:
                element.set(f"{{{INKSCAPE_NS}}}label", label)
            else:
                element.attrib.pop(f"{{{INKSCAPE_NS}}}label", None)

        if page.number == 1:
            self._sync_viewbox()

        return self.list()[page.number - 1]

    def delete(self, ref: str | int) -> Page:
        page = self.get(ref)
        if len(self.elements()) < 2:
            raise PageError("A document needs at least one page.")

        element = self.elements()[page.number - 1]
        element.getparent().remove(element)
        if page.number == 1:
            self._sync_viewbox()

        return page

    def _page(self, number: int, element) -> Page:
        values = [self._number(element.get(name)) for name in ("x", "y", "width", "height")]

        return Page(number, element.get("id"), element.get(f"{{{INKSCAPE_NS}}}label"), *values)

    def _sync_viewbox(self) -> None:
        """Page 1 is the SVG viewport in Inkscape, so keep viewBox, width and height on it."""
        first = self.list()[0]
        scale = self.doc.user_unit_px()
        unit = self.doc.width_unit()

        self.root.set("viewBox", f"{first.x:g} {first.y:g} {first.width:g} {first.height:g}")
        self.root.set("width", f"{first.width * scale / UNIT_PX[unit]:g}{unit}")
        self.root.set("height", f"{first.height * scale / UNIT_PX[unit]:g}{unit}")

    def _namedview(self):
        found = self.root.find(f"{{{SODIPODI_NS}}}namedview")
        if found is not None:
            return found

        namedview = etree.Element(f"{{{SODIPODI_NS}}}namedview", id="namedview1")
        self.root.insert(0, namedview)

        return namedview

    def _write(self, namedview, page: Page) -> None:
        element = etree.SubElement(namedview, f"{{{INKSCAPE_NS}}}page")
        self._write_box(element, *page.box)
        element.set("id", self._free_id())

    @staticmethod
    def _write_box(element, x: float, y: float, width: float, height: float) -> None:
        if width <= 0 or height <= 0:
            raise PageError("Page width and height must be positive.")

        for name, value in (("x", x), ("y", y), ("width", width), ("height", height)):
            element.set(name, f"{value:g}")

    def _free_id(self) -> str:
        used = set(self.root.xpath("//@id"))
        number = 1
        while f"page{number}" in used:
            number += 1

        return f"page{number}"

    @staticmethod
    def _number(value: str | None) -> float:
        match = re.match(r"\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)", value or "")

        return float(match.group(1)) if match else 0.0
