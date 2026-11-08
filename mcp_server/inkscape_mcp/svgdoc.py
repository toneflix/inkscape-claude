"""SVG document model used by the MCP tools. Works without Inkscape."""

from __future__ import annotations

import copy
import itertools
import re
from collections import Counter
from pathlib import Path

from lxml import etree

from .pages import UNIT_PX, Box, Pages

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
XLINK_NS = "http://www.w3.org/1999/xlink"
NSMAP = {None: SVG_NS, "inkscape": INKSCAPE_NS, "sodipodi": SODIPODI_NS, "xlink": XLINK_NS}
PREFIXES = {"inkscape": INKSCAPE_NS, "sodipodi": SODIPODI_NS, "xlink": XLINK_NS}

NON_VISUAL = {"defs", "metadata", "namedview", "style", "title", "desc"}

EMBEDDED_DATA = re.compile(r'(href="data:[^;,"]*;base64,)([^"]{200,})"')

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


class SvgError(ValueError):
    pass


def parse_style(style: str) -> dict[str, str]:
    out: dict[str, str] = {}

    for part in style.split(";"):
        if ":" in part:
            key, value = part.split(":", 1)
            out[key.strip()] = value.strip()

    return out


def format_style(style: dict[str, str]) -> str:
    return ";".join(f"{key}:{value}" for key, value in style.items())


def qualified_name(name: str) -> str:
    if ":" not in name:
        return name

    prefix, local = name.split(":", 1)
    if prefix not in PREFIXES:
        raise SvgError(f"Unknown attribute prefix '{prefix}'.")

    return f"{{{PREFIXES[prefix]}}}{local}"


class SvgDocument:
    """An SVG tree with the element-level edits the MCP tools expose."""

    def __init__(self, tree: etree._ElementTree):
        self.tree = tree

    @classmethod
    def parse(cls, text: str) -> SvgDocument:
        try:
            root = etree.fromstring(text.encode("utf-8"), PARSER)
        except etree.XMLSyntaxError as exc:
            raise SvgError(f"Not valid XML: {exc}") from exc

        if etree.QName(root).localname != "svg":
            raise SvgError("Root element must be <svg>.")

        return cls(etree.ElementTree(root))

    @classmethod
    def load(cls, path: Path) -> SvgDocument:
        return cls.parse(path.read_text(encoding="utf-8"))

    @classmethod
    def blank(cls, width: float, height: float, units: str = "px", background: str | None = None) -> SvgDocument:
        suffix = "" if units == "px" else units
        root = etree.Element(f"{{{SVG_NS}}}svg", nsmap=NSMAP)
        root.set("width", f"{width:g}{suffix}")
        root.set("height", f"{height:g}{suffix}")
        root.set("viewBox", f"0 0 {width:g} {height:g}")
        root.set("version", "1.1")
        root.set("id", "svg1")

        etree.SubElement(root, f"{{{SVG_NS}}}defs", id="defs1")
        layer = etree.SubElement(root, f"{{{SVG_NS}}}g", id="layer1")
        layer.set(f"{{{INKSCAPE_NS}}}label", "Layer 1")
        layer.set(f"{{{INKSCAPE_NS}}}groupmode", "layer")

        if background:
            etree.SubElement(layer, f"{{{SVG_NS}}}rect", id="background", x="0", y="0",
                             width=f"{width:g}", height=f"{height:g}", fill=background)

        return cls(etree.ElementTree(root))

    @property
    def root(self):
        return self.tree.getroot()

    @property
    def pages(self) -> Pages:
        return Pages(self)

    def viewbox(self) -> Box:
        values = re.split(r"[\s,]+", (self.root.get("viewBox") or "").strip())
        if len(values) == 4:
            try:
                return tuple(float(value) for value in values)
            except ValueError:
                pass

        return 0.0, 0.0, self._length_px("width") or 0.0, self._length_px("height") or 0.0

    def width_unit(self) -> str:
        match = re.match(r"\s*[-+0-9.eE]+\s*([a-zA-Z]*)", self.root.get("width") or "")
        unit = match.group(1).lower() if match else ""

        return unit if unit in UNIT_PX else ""

    def user_unit_px(self) -> float:
        """How many CSS px one user unit is; Inkscape measures, exports and translates in px."""
        width_px = self._length_px("width")
        view_width = self.viewbox()[2]

        return width_px / view_width if width_px and view_width else 1.0

    def to_px(self, box: Box) -> Box:
        scale, (origin_x, origin_y, *_) = self.user_unit_px(), self.viewbox()
        x, y, width, height = box

        return (x - origin_x) * scale, (y - origin_y) * scale, width * scale, height * scale

    def from_px(self, box: Box) -> Box:
        scale, (origin_x, origin_y, *_) = self.user_unit_px(), self.viewbox()
        x, y, width, height = box

        return x / scale + origin_x, y / scale + origin_y, width / scale, height / scale

    def drawn_objects(self) -> list:
        """The objects people place: children of layers, plus anything at the top level that is not a layer."""
        objects = []

        for child in self.root:
            if not isinstance(child.tag, str) or etree.QName(child).localname in NON_VISUAL:
                continue
            if child.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer":
                objects += [item for item in child if isinstance(item.tag, str)
                            and etree.QName(item).localname not in NON_VISUAL]
            else:
                objects.append(child)

        return objects

    def _length_px(self, name: str) -> float | None:
        match = re.match(r"\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z%]*)", self.root.get(name) or "")
        if not match or match.group(2).lower() not in UNIT_PX:
            return None

        return float(match.group(1)) * UNIT_PX[match.group(2).lower()]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.tree.write(str(path), xml_declaration=True, encoding="UTF-8", standalone=False)

    def to_string(self) -> str:
        return etree.tostring(self.tree, encoding="unicode")

    def pop_marked(self, attribute: str) -> list[str]:
        """Removes `attribute` wherever it is set and returns the ids of those elements, in document order."""
        marked = self.root.xpath(f"//*[@{attribute}]")
        for element in marked:
            del element.attrib[attribute]

        return [element.get("id") for element in marked if element.get("id")]

    def has(self, element_id: str) -> bool:
        return bool(self.root.xpath("//*[@id=$i]", i=element_id))

    def find(self, element_id: str):
        hits = self.root.xpath("//*[@id=$i]", i=element_id)
        if not hits:
            raise SvgError(f"No element with id '{element_id}'.")

        return hits[0]

    def element_source(self, element_id: str) -> str:
        """The element's markup, with embedded image data shortened so it stays readable."""
        source = etree.tostring(self.find(element_id), encoding="unicode")

        return EMBEDDED_DATA.sub(lambda match: f'{match.group(1)}…" (embedded image, '
                                               f'{len(match.group(2)) * 3 // 4096} KB)', source)

    def add_image(self, data_uri: str, box: Box, element_id: str, label: str | None = None) -> str:
        """Adds an <image> with the data embedded, stretched to box (x, y, width, height) in user units."""
        x, y, width, height = box
        image = etree.SubElement(self.default_parent(), f"{{{SVG_NS}}}image")
        for name, value in (("id", element_id), ("x", x), ("y", y), ("width", width), ("height", height)):
            image.set(name, value if isinstance(value, str) else f"{value:.6g}")
        image.set("preserveAspectRatio", "none")
        if label:
            image.set(f"{{{INKSCAPE_NS}}}label", label)
        image.set(f"{{{XLINK_NS}}}href", data_uri)

        self._ensure_unique_ids(image)

        return image.get("id")

    def default_parent(self):
        layers = self.root.xpath("./svg:g[@inkscape:groupmode='layer']",
                                 namespaces={"svg": SVG_NS, "inkscape": INKSCAPE_NS})
        return layers[-1] if layers else self.root

    def add(self, fragment: str, parent_id: str | None = None, before_id: str | None = None) -> list[str]:
        parent = self.find(parent_id) if parent_id else self.default_parent()
        anchor = self.find(before_id) if before_id else None
        if anchor is not None and anchor.getparent() is not parent:
            raise SvgError(f"'{before_id}' is not a direct child of the parent.")

        inserted = []
        for element in self._parse_fragment(fragment):
            if anchor is not None:
                anchor.addprevious(element)
            else:
                parent.append(element)
            self._ensure_unique_ids(element)
            inserted.append(element.get("id"))

        return inserted

    def update(self, element_id: str, attributes: dict[str, str | None] | None = None,
               style: dict[str, str | None] | None = None, text: str | None = None) -> None:
        element = self.find(element_id)

        for name, value in (attributes or {}).items():
            name = qualified_name(name)
            if value is None:
                element.attrib.pop(name, None)
            else:
                element.set(name, str(value))

        if style:
            self._merge_style(element, style)

        if text is not None:
            self._set_text(element, text)

    def replace(self, element_id: str, fragment: str) -> list[str]:
        element = self.find(element_id)
        parent = element.getparent()
        if parent is None:
            raise SvgError("Cannot replace the root element; use write_document instead.")

        replacements = self._parse_fragment(fragment)
        for new in replacements:
            element.addprevious(new)
        parent.remove(element)

        for new in replacements:
            self._ensure_unique_ids(new)

        return [new.get("id") for new in replacements]

    def delete(self, element_ids: list[str]) -> list[str]:
        for element_id in element_ids:
            element = self.find(element_id)
            if element.getparent() is None:
                raise SvgError("Cannot delete the root element.")
            element.getparent().remove(element)

        return list(element_ids)

    def outline(self, max_depth: int = 6, bboxes: dict[str, Box] | None = None) -> str:
        """bboxes are in user units, like the page positions."""
        root = self.root
        lines = [f"svg id={root.get('id')} width={root.get('width')} height={root.get('height')} "
                 f"viewBox={root.get('viewBox')}"]
        lines += [f"  {page.describe()}" for page in self.pages.list()]
        self._outline_children(root, 1, max_depth, bboxes or {}, lines)

        return "\n".join(lines)

    def _outline_children(self, element, depth, max_depth, bboxes, lines) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue

            name = etree.QName(child).localname
            if name in NON_VISUAL:
                continue

            lines.append(f"{'  ' * depth}{self._describe(child, name, bboxes)}{self._placement(child, bboxes)}")
            if depth < max_depth:
                self._outline_children(child, depth + 1, max_depth, bboxes, lines)

    @staticmethod
    def _describe(element, name, bboxes) -> str:
        bits = [name]

        if element.get("id"):
            bits.append(f"id={element.get('id')}")

        label = element.get(f"{{{INKSCAPE_NS}}}label")
        if label:
            bits.append(f'label="{label}"')

        if element.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer":
            bits.append("(layer)")

        if name in ("text", "tspan", "flowRoot"):
            text = "".join(element.itertext()).strip()
            if text:
                bits.append(f'text="{text[:60]}"')

        fill = parse_style(element.get("style", "")).get("fill") or element.get("fill")
        if fill:
            bits.append(f"fill={fill}")

        if element.get("id") in bboxes:
            x, y, w, h = bboxes[element.get("id")]
            bits.append(f"bbox=({x:.1f},{y:.1f},{w:.1f}x{h:.1f})")

        return " ".join(bits)

    def _placement(self, element, bboxes) -> str:
        if element.get("id") not in bboxes or element.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer":
            return ""

        pages = self.pages
        under = pages.pages_under(bboxes[element.get("id")])
        if not under:
            return " off-page"
        if len(pages.list()) == 1:
            return ""

        return " on page " + ",".join(str(page.number) for page in under)

    @staticmethod
    def _parse_fragment(fragment: str) -> list:
        prefixes = " ".join(f'xmlns:{prefix}="{ns}"' for prefix, ns in PREFIXES.items())
        wrapper = f'<svg xmlns="{SVG_NS}" {prefixes}>{fragment}</svg>'

        try:
            root = etree.fromstring(wrapper.encode("utf-8"), PARSER)
        except etree.XMLSyntaxError as exc:
            raise SvgError(f"SVG fragment is not valid XML: {exc}") from exc

        children = [copy.deepcopy(child) for child in root if isinstance(child.tag, str)]
        if not children:
            raise SvgError("SVG fragment contains no elements.")

        return children

    def _ensure_unique_ids(self, element) -> None:
        counts = Counter(self.root.xpath("//@id"))
        used = set(counts)
        counter = itertools.count(1)

        for node in element.iter():
            if not isinstance(node.tag, str):
                continue

            node_id = node.get("id")
            if node_id and counts[node_id] == 1:
                continue
            if node_id:
                counts[node_id] -= 1

            tag = etree.QName(node).localname
            candidate = f"{tag}{next(counter)}"
            while candidate in used:
                candidate = f"{tag}{next(counter)}"

            node.set("id", candidate)
            used.add(candidate)

    @staticmethod
    def _merge_style(element, changes: dict[str, str | None]) -> None:
        style = parse_style(element.get("style", ""))

        for key, value in changes.items():
            if value is None:
                style.pop(key, None)
            else:
                style[key] = str(value)

        if style:
            element.set("style", format_style(style))
        else:
            element.attrib.pop("style", None)

    @staticmethod
    def _set_text(element, text: str) -> None:
        target = element
        tspans = element.xpath(".//svg:tspan", namespaces={"svg": SVG_NS})
        if tspans:
            target = tspans[0]
            for extra in tspans[1:]:
                extra.getparent().remove(extra)

        for child in list(target):
            target.remove(child)
        target.text = text
