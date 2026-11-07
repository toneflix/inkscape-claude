"""MCP server that lets Claude create, edit, preview and export Inkscape (SVG) documents."""

from __future__ import annotations

import functools
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .cli import Inkscape, InkscapeError
from .svgdoc import SvgDocument, SvgError
from .workspace import Workspace

STYLE_ATTRIBUTES = ("style", "class", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
                    "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity", "filter")

BOOLEAN_ACTIONS = {"union": "path-union", "difference": "path-difference", "intersection": "path-intersection",
                   "exclusion": "path-exclusion", "division": "path-division", "cut": "path-cut",
                   "combine": "path-combine"}

SINGLE_RESULT_OPERATIONS = {"union", "difference", "intersection", "exclusion", "combine"}

EXPECTED_ERRORS = (ValueError, OSError, SvgError, InkscapeError)

workspace = Workspace.from_env()
inkscape = Inkscape.from_env()

server = MCPServer(
    name="inkscape",
    instructions=(
        "Tools for drawing and editing SVG documents and running Inkscape on them. "
        f"Relative paths resolve inside the workspace folder ({workspace.root}). "
        "Typical loop: create_document or read_document, edit with add_elements / update_element / "
        "replace_element, then call render_preview to look at the result before you say it is done. "
        "Use boolean_operation, align_objects or run_actions for things that are easier for Inkscape "
        "to do than to write by hand, and export_document for PNG/PDF/EPS output. "
        "Every edit keeps a snapshot, so undo_last_change can revert a mistake."
    ),
)


def tool(fn):
    """Register fn as a tool and report expected errors to Claude with their message."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    server.tool()(wrapper)

    return wrapper


@tool
def inkscape_info() -> str:
    """Report the Inkscape version, the workspace folder and whether Inkscape is usable."""
    try:
        version = inkscape.version()
    except InkscapeError as exc:
        version = f"unavailable ({exc}). SVG editing tools still work; preview/export/actions do not."

    return f"Inkscape: {version}\nWorkspace: {workspace.root}\nAny path allowed: {workspace.allow_any_path}"


@tool
def list_documents() -> str:
    """List the SVG files in the workspace."""
    files = workspace.documents()
    if not files:
        return "No SVG documents in the workspace yet."

    return "\n".join(f"{workspace.relative(f)}  ({f.stat().st_size} bytes)" for f in files)


@tool
def create_document(path: str, width: float = 800, height: float = 600, units: str = "px",
                    background: str | None = None, svg: str | None = None, overwrite: bool = False) -> str:
    """Create a new SVG document.

    Either pass `svg` with a complete SVG document, or leave it empty to get a blank page of
    `width` x `height` with one Inkscape layer (id "layer1"). `background` is an optional fill color.
    """
    target = workspace.resolve(path, must_exist=False)
    if target.suffix.lower() != ".svg":
        raise ValueError("Document path must end in .svg")
    if target.exists() and not overwrite:
        raise FileExistsError(f"'{workspace.relative(target)}' already exists. Pass overwrite=true to replace it.")

    doc = SvgDocument.parse(svg) if svg else SvgDocument.blank(width, height, units, background)
    workspace.write(target, doc)

    return f"Created {workspace.relative(target)}\n\n{doc.outline()}"


@tool
def read_document(path: str, mode: Literal["outline", "full"] = "outline", with_bboxes: bool = False) -> str:
    """Read a document.

    mode="outline" gives a compact tree of elements with ids, labels, fills and text (cheap).
    mode="full" returns the raw SVG source. with_bboxes adds Inkscape-measured bounding boxes.
    """
    source = workspace.resolve(path)
    text = source.read_text(encoding="utf-8")
    if mode == "full":
        return text

    bboxes = inkscape.query_all(source) if with_bboxes else None

    return SvgDocument.parse(text).outline(bboxes=bboxes)


@tool
def write_document(path: str, svg: str) -> str:
    """Replace a whole document with new SVG source. The SVG is checked for valid XML first."""
    target = workspace.resolve(path, must_exist=False)
    doc = SvgDocument.parse(svg)
    workspace.write(target, doc)

    return f"Wrote {workspace.relative(target)}\n\n{doc.outline()}"


@tool
def add_elements(path: str, svg_fragment: str, parent_id: str | None = None, before_id: str | None = None) -> str:
    """Add one or more SVG elements, e.g. '<rect x="10" y="10" width="50" height="50" fill="#e44"/>'.

    They go into `parent_id` (default: the top layer), at the end or just before `before_id`.
    Elements without ids get unique ones; the new ids are returned.
    """
    target, ids = workspace.edit(path, lambda doc: doc.add(svg_fragment, parent_id, before_id))

    return f"Added to {workspace.relative(target)}: {', '.join(ids)}"


@tool
def update_element(path: str, element_id: str, attributes: dict[str, str | None] | None = None,
                   style: dict[str, str | None] | None = None, text: str | None = None) -> str:
    """Change an element's attributes, CSS style properties or text.

    attributes: e.g. {"x": "20", "transform": "rotate(15)", "inkscape:label": "Logo"}; null removes one.
    style: merged into the style attribute, e.g. {"fill": "#336699", "stroke-width": "2"}.
    text: new text content for <text>/<tspan> elements.
    """
    target, _ = workspace.edit(path, lambda doc: doc.update(element_id, attributes, style, text))

    return f"Updated '{element_id}' in {workspace.relative(target)}"


@tool
def replace_element(path: str, element_id: str, svg_fragment: str) -> str:
    """Replace one element (and its children) with new SVG markup."""
    target, ids = workspace.edit(path, lambda doc: doc.replace(element_id, svg_fragment))

    return f"Replaced '{element_id}' in {workspace.relative(target)} with: {', '.join(ids)}"


@tool
def delete_elements(path: str, element_ids: list[str]) -> str:
    """Delete elements by id."""
    target, ids = workspace.edit(path, lambda doc: doc.delete(element_ids))

    return f"Deleted from {workspace.relative(target)}: {', '.join(ids)}"


@tool
def get_element(path: str, element_id: str) -> str:
    """Return the SVG source of one element."""
    return workspace.load(path).element_source(element_id)


@tool
def undo_last_change(path: str) -> str:
    """Revert the document to the snapshot taken before the most recent change."""
    target = workspace.resolve(path, must_exist=False)
    remaining = workspace.history.undo(target)
    if remaining is None:
        return "Nothing to undo."

    return f"Reverted {workspace.relative(target)}. {remaining} earlier snapshot(s) left."


@tool
def render_preview(path: str, width: int = 800, element_id: str | None = None,
                   background: str = "#ffffff") -> Image:
    """Render the document (or one element) to a PNG so you can see it. Use this to check your work."""
    source = workspace.resolve(path)

    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "preview.png"
        inkscape.export(source, output, "png", width=width, object_id=element_id, background=background)

        return Image(data=output.read_bytes(), format="png")


@tool
def export_document(path: str, output_path: str,
                    format: Literal["png", "pdf", "eps", "ps", "svg", "emf", "wmf"] | None = None,
                    dpi: float | None = None, width: int | None = None, height: int | None = None,
                    element_id: str | None = None, area: Literal["page", "drawing"] = "page",
                    background: str | None = None, text_to_path: bool = False, plain_svg: bool = False) -> str:
    """Export with Inkscape to PNG, PDF, EPS, PS, EMF, WMF or plain SVG.

    The format defaults to the output file's extension. Set element_id to export one object only.
    """
    source = workspace.resolve(path)
    output = workspace.resolve(output_path, must_exist=False)

    inkscape.export(source, output, format, dpi=dpi, width=width, height=height, object_id=element_id,
                    area=area, background=background, text_to_path=text_to_path, plain_svg=plain_svg)

    return f"Exported {workspace.relative(output)} ({output.stat().st_size} bytes)"


@tool
def run_actions(path: str, actions: list[str], output_path: str | None = None) -> str:
    """Run Inkscape actions on a document and save the result (in place, or to output_path).

    Actions are Inkscape 1.x action strings, one per list item, e.g.
    ["select-by-id:rect1,circle2", "path-union"], ["select-all", "object-to-path"],
    ["select-by-id:logo", "object-align:hcenter page"], ["select-by-id:t1", "object-flip-horizontal"].
    Use list_actions to discover names. File and export steps are added for you.
    """
    source = workspace.resolve(path)
    target = workspace.resolve(output_path, must_exist=False) if output_path else source

    workspace.history.snapshot(target)
    log = inkscape.run_actions(source, actions, output_path=target)

    message = f"Ran {len(actions)} action(s); saved {workspace.relative(target)}."
    if log:
        message += f"\nInkscape output:\n{log}"

    return f"{message}\n\n{SvgDocument.load(target).outline()}"


@tool
def boolean_operation(path: str, element_ids: list[str],
                      operation: Literal["union", "difference", "intersection", "exclusion", "division", "cut",
                                         "combine"],
                      keep_style_of_first: bool = True) -> str:
    """Path boolean on the given elements (shapes are converted to paths first).

    List the bottom object first. For difference/cut/division the top (last) object is the cutter.
    By default the result keeps the first object's fill/stroke; Inkscape alone may take the top object's.
    """
    if len(element_ids) < 2:
        raise ValueError("Need at least two element ids.")

    source = workspace.resolve(path)
    first = SvgDocument.load(source).find(element_ids[0])
    saved_style = {name: first.get(name) for name in STYLE_ATTRIBUTES}

    workspace.history.snapshot(source)
    log = inkscape.run_actions(source, [f"select-by-id:{','.join(element_ids)}", "object-to-path",
                                        BOOLEAN_ACTIONS[operation]])

    doc = SvgDocument.load(source)
    survivors = [element_id for element_id in element_ids if doc.has(element_id)]
    if keep_style_of_first and operation in SINGLE_RESULT_OPERATIONS and len(survivors) == 1:
        result = doc.find(survivors[0])
        for name, value in saved_style.items():
            if value is None:
                result.attrib.pop(name, None)
            else:
                result.set(name, value)
        doc.save(source)

    message = f"Applied {operation} to {', '.join(element_ids)}; saved {workspace.relative(source)}."
    if log:
        message += f"\nInkscape output:\n{log}"

    return f"{message}\n\n{doc.outline()}"


@tool
def align_objects(path: str, element_ids: list[str],
                  horizontal: Literal["left", "hcenter", "right"] | None = None,
                  vertical: Literal["top", "vcenter", "bottom"] | None = None,
                  relative_to: Literal["page", "drawing", "selection", "first", "last", "biggest",
                                       "smallest"] = "page") -> str:
    """Align elements with Inkscape's Align tool, e.g. horizontal="hcenter", vertical="vcenter", relative_to="page"."""
    if not horizontal and not vertical:
        raise ValueError("Give horizontal and/or vertical.")

    anchor = " ".join(side for side in (horizontal, vertical) if side)

    return run_actions(path, [f"select-by-id:{','.join(element_ids)}", f"object-align:{anchor} {relative_to}"])


@tool
def get_bounding_boxes(path: str, element_ids: list[str] | None = None) -> str:
    """Inkscape-measured bounding boxes (x, y, width, height in user units) for elements."""
    boxes = inkscape.query_all(workspace.resolve(path))
    if element_ids:
        boxes = {key: box for key, box in boxes.items() if key in element_ids}

    lines = [f"{key}: x={x:.2f} y={y:.2f} w={w:.2f} h={h:.2f}" for key, (x, y, w, h) in boxes.items()]

    return "\n".join(lines) or "No matches."


@tool
def list_actions(filter: str | None = None) -> str:
    """List Inkscape action names (optionally only those containing `filter`)."""
    actions = inkscape.action_list()
    if filter:
        needle = filter.lower()
        actions = [(name, desc) for name, desc in actions if needle in name.lower() or needle in desc.lower()]

    return "\n".join(f"{name}: {desc}" for name, desc in actions) or "No matching actions."


@tool
def open_in_inkscape(path: str) -> str:
    """Open the document in the Inkscape app on this computer so the person can see or keep editing it."""
    source = workspace.resolve(path)
    subprocess.Popen([inkscape.binary, str(source)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)

    return f"Opened {workspace.relative(source)} in Inkscape."


def main() -> None:
    workspace.root.mkdir(parents=True, exist_ok=True)
    server.run()


if __name__ == "__main__":
    main()
