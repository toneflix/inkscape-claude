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
from .live import LiveError, LiveInkscape
from .operations import DocumentOperations
from .svgdoc import SvgDocument, SvgError
from .workspace import Workspace

EXPECTED_ERRORS = (ValueError, OSError, SvgError, InkscapeError, LiveError)

workspace = Workspace.from_env()
inkscape = Inkscape.from_env()
live = LiveInkscape(inkscape)
operations = DocumentOperations(inkscape, workspace.history)

server = MCPServer(
    name="inkscape",
    instructions=(
        "Tools for drawing and editing SVG documents and running Inkscape on them. "
        f"Relative paths resolve inside the workspace folder ({workspace.root}). "
        "Typical loop: create_document or read_document, edit with add_elements / update_element / "
        "replace_element, then call render_preview to look at the result before you say it is done. "
        "Use boolean_operation, align_objects or run_actions for things that are easier for Inkscape "
        "to do than to write by hand, and export_document for PNG/PDF/EPS output. "
        "Documents can have several pages (artboards) on one canvas: read_document and list_pages show them and "
        "what sits on each; add_page, update_page, delete_page and move_to_page change them, and render_preview "
        "and export_document take a page. "
        "Every edit keeps a snapshot, so undo_last_change can revert a mistake. "
        "On Linux the live_* tools work on the Inkscape window the person has open: live_snapshot shows it, "
        "live_run_actions changes it directly, and for file edits call live_save, edit the file, then live_reload."
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


def saved_message(summary: str, target: Path, log: str) -> str:
    message = f"{summary}; saved {workspace.relative(target)}."
    if log:
        message += f"\nInkscape output:\n{log}"

    return f"{message}\n\n{SvgDocument.load(target).outline()}"


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
                    background: str | None = None, svg: str | None = None, overwrite: bool = False,
                    pages: int = 1) -> str:
    """Create a new SVG document.

    Either pass `svg` with a complete SVG document, or leave it empty to get a blank page of
    `width` x `height` with one Inkscape layer (id "layer1"). `background` is an optional fill color.
    `pages` above 1 adds more pages of the same size side by side.
    """
    target = workspace.resolve(path, must_exist=False)
    if target.suffix.lower() != ".svg":
        raise ValueError("Document path must end in .svg")
    if target.exists() and not overwrite:
        raise FileExistsError(f"'{workspace.relative(target)}' already exists. Pass overwrite=true to replace it.")

    doc = SvgDocument.parse(svg) if svg else SvgDocument.blank(width, height, units, background)
    for _ in range(max(int(pages), 1) - 1):
        doc.pages.add()
    operations.write(target, doc)

    return f"Created {workspace.relative(target)}\n\n{doc.outline()}"


@tool
def read_document(path: str, mode: Literal["outline", "full"] = "outline", with_bboxes: bool = False) -> str:
    """Read a document.

    mode="outline" gives a compact tree of elements with ids, labels, fills and text (cheap).
    mode="full" returns the raw SVG source. The outline starts with the pages (artboards). with_bboxes adds
    Inkscape-measured bounding boxes and the page each element is on.
    """
    source = workspace.resolve(path)
    if mode == "full":
        return source.read_text(encoding="utf-8")

    return operations.outline(source, with_bboxes)


@tool
def write_document(path: str, svg: str) -> str:
    """Replace a whole document with new SVG source. The SVG is checked for valid XML first."""
    target = workspace.resolve(path, must_exist=False)
    doc = SvgDocument.parse(svg)
    operations.write(target, doc)

    return f"Wrote {workspace.relative(target)}\n\n{doc.outline()}"


@tool
def add_elements(path: str, svg_fragment: str, parent_id: str | None = None, before_id: str | None = None) -> str:
    """Add one or more SVG elements, e.g. '<rect x="10" y="10" width="50" height="50" fill="#e44"/>'.

    They go into `parent_id` (default: the top layer), at the end or just before `before_id`.
    Elements without ids get unique ones; the new ids are returned.
    """
    target = workspace.resolve(path)
    ids = operations.add(target, svg_fragment, parent_id, before_id)

    return f"Added to {workspace.relative(target)}: {', '.join(ids)}"


@tool
def update_element(path: str, element_id: str, attributes: dict[str, str | None] | None = None,
                   style: dict[str, str | None] | None = None, text: str | None = None) -> str:
    """Change an element's attributes, CSS style properties or text.

    attributes: e.g. {"x": "20", "transform": "rotate(15)", "inkscape:label": "Logo"}; null removes one.
    style: merged into the style attribute, e.g. {"fill": "#336699", "stroke-width": "2"}.
    text: new text content for <text>/<tspan> elements.
    """
    target = workspace.resolve(path)
    operations.update(target, element_id, attributes, style, text)

    return f"Updated '{element_id}' in {workspace.relative(target)}"


@tool
def replace_element(path: str, element_id: str, svg_fragment: str) -> str:
    """Replace one element (and its children) with new SVG markup."""
    target = workspace.resolve(path)
    ids = operations.replace(target, element_id, svg_fragment)

    return f"Replaced '{element_id}' in {workspace.relative(target)} with: {', '.join(ids)}"


@tool
def delete_elements(path: str, element_ids: list[str]) -> str:
    """Delete elements by id."""
    target = workspace.resolve(path)
    ids = operations.delete(target, element_ids)

    return f"Deleted from {workspace.relative(target)}: {', '.join(ids)}"


@tool
def get_element(path: str, element_id: str) -> str:
    """Return the SVG source of one element."""
    return operations.element(workspace.resolve(path), element_id)


@tool
def undo_last_change(path: str) -> str:
    """Revert the document to the snapshot taken before the most recent change."""
    target = workspace.resolve(path, must_exist=False)
    remaining = operations.undo(target)
    if remaining is None:
        return "Nothing to undo."

    return f"Reverted {workspace.relative(target)}. {remaining} earlier snapshot(s) left."


@tool
def list_pages(path: str) -> str:
    """List the pages (artboards): number, id, label, position and size in user units, and the objects on each."""
    return operations.pages(workspace.resolve(path))


@tool
def add_page(path: str, width: float | None = None, height: float | None = None, x: float | None = None,
             y: float | None = None, label: str | None = None) -> str:
    """Add a page. By default it copies the last page's size and goes to its right."""
    page = operations.add_page(workspace.resolve(path), width, height, x, y, label)

    return f"Added {page.describe()}"


@tool
def update_page(path: str, page: str, label: str | None = None, x: float | None = None, y: float | None = None,
                width: float | None = None, height: float | None = None) -> str:
    """Rename, move or resize a page, named by number (from 1), id or label. Objects stay where they are."""
    changed = operations.update_page(workspace.resolve(path), page, label, x, y, width, height)

    return f"Updated {changed.describe()}"


@tool
def delete_page(path: str, page: str) -> str:
    """Remove a page (by number, id or label). The objects on it stay on the canvas."""
    source = workspace.resolve(path)
    removed = operations.delete_page(source, page)

    return f"Deleted page {removed.number}.\n\n{operations.pages(source)}"


@tool
def move_to_page(path: str, element_ids: list[str], page: str) -> str:
    """Move objects to another page, keeping their position on the page. Objects on no page are centred."""
    source = workspace.resolve(path)
    target = operations.move_to_page(source, element_ids, page)

    return f"Moved {', '.join(element_ids)} to page {target.number}.\n\n{operations.pages(source)}"


@tool
def render_preview(path: str, width: int = 800, element_id: str | None = None, page: str | None = None,
                   background: str = "#ffffff") -> Image:
    """Render every page (default), one page, or one element to a PNG so you can see it. Use this to check
    your work."""
    png = operations.preview(workspace.resolve(path), width, element_id, background, page)

    return Image(data=png, format="png")


@tool
def export_document(path: str, output_path: str,
                    format: Literal["png", "pdf", "eps", "ps", "svg", "emf", "wmf"] | None = None,
                    dpi: float | None = None, width: int | None = None, height: int | None = None,
                    element_id: str | None = None, page: str | None = None,
                    area: Literal["page", "pages", "drawing"] = "page",
                    background: str | None = None, text_to_path: bool = False, plain_svg: bool = False) -> str:
    """Export with Inkscape to PNG, PDF, EPS, PS, EMF, WMF or plain SVG.

    The format defaults to the output file's extension. Set element_id to export one object only, or page
    (number, id or label) for one page. area "page" is the first page, "pages" all pages together, "drawing"
    the bounding box of everything drawn.
    """
    source = workspace.resolve(path)
    output = workspace.resolve(output_path, must_exist=False)

    inkscape.export(source, output, format, dpi=dpi, width=width, height=height, object_id=element_id,
                    area=operations.area(source, page, area), background=background, text_to_path=text_to_path,
                    plain_svg=plain_svg)

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
    log = operations.run_actions(source, actions, target)

    return saved_message(f"Ran {len(actions)} action(s)", target, log)


@tool
def boolean_operation(path: str, element_ids: list[str],
                      operation: Literal["union", "difference", "intersection", "exclusion", "division", "cut",
                                         "combine"],
                      keep_style_of_first: bool = True) -> str:
    """Path boolean on the given elements (shapes are converted to paths first).

    List the bottom object first. For difference/cut/division the top (last) object is the cutter.
    By default the result keeps the first object's fill/stroke; Inkscape alone may take the top object's.
    """
    source = workspace.resolve(path)
    log = operations.boolean(source, element_ids, operation, keep_style_of_first)

    return saved_message(f"Applied {operation} to {', '.join(element_ids)}", source, log)


@tool
def align_objects(path: str, element_ids: list[str],
                  horizontal: Literal["left", "hcenter", "right"] | None = None,
                  vertical: Literal["top", "vcenter", "bottom"] | None = None,
                  relative_to: Literal["page", "drawing", "selection", "first", "last", "biggest",
                                       "smallest"] = "page", page: str | None = None) -> str:
    """Align elements with Inkscape's Align tool, e.g. horizontal="hcenter", vertical="vcenter", relative_to="page".

    With several pages, relative_to="page" aligns each object to the page it is on, or to `page` if given.
    """
    source = workspace.resolve(path)
    log = operations.align(source, element_ids, horizontal, vertical, relative_to, page)

    return saved_message(f"Aligned {', '.join(element_ids)}", source, log)


@tool
def get_bounding_boxes(path: str, element_ids: list[str] | None = None) -> str:
    """Inkscape-measured bounding boxes for elements: x, y, width, height in user units, like page positions."""
    return operations.bounding_boxes(workspace.resolve(path), element_ids)


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


@tool
def live_status() -> str:
    """Report whether a running Inkscape window can be controlled live, and its window/document numbers."""
    if not live.available():
        try:
            live.require()
        except LiveError as exc:
            return f"Live control unavailable: {exc}"

    return f"Live control available. Windows: {live.windows()}. Documents: {live.documents()}."


@tool
def live_open(path: str) -> str:
    """Open a workspace document in the running Inkscape (starting Inkscape if needed) for live editing."""
    source = workspace.resolve(path)
    window = live.open(source)
    if window is None:
        return f"Asked Inkscape to open {workspace.relative(source)}, but no new window showed up on D-Bus."

    return f"Opened {workspace.relative(source)} in Inkscape window {window}."


@tool
def live_run_actions(actions: list[str], window: int | None = None) -> str:
    """Run Inkscape actions in the open window, where the person sees the result immediately.

    Same syntax as run_actions, e.g. ["select-by-id:rect1,circle2", "path-union"] or
    ["select-all:all", "object-align:hcenter page"]. Ctrl+Z in Inkscape undoes them.
    The newest window is used unless `window` is given (see live_status).
    """
    done = live.run(actions, window)

    return f"Ran {len(done)} action(s) in Inkscape: {'; '.join(done)}"


@tool
def live_snapshot(width: int = 800) -> Image:
    """Render what is currently in the active Inkscape window to a PNG, including unsaved changes."""
    with tempfile.TemporaryDirectory() as tmp:
        output = live.export(Path(tmp) / "live.png", "png", width=width)

        return Image(data=output.read_bytes(), format="png")


@tool
def live_pull(path: str) -> str:
    """Copy the document in the active Inkscape window (with unsaved changes) to a workspace file."""
    target = workspace.resolve(path, must_exist=False)
    operations.history.snapshot(target)
    live.export(target, "svg")

    return f"Saved the live document to {workspace.relative(target)}\n\n{SvgDocument.load(target).outline()}"


@tool
def live_save(window: int | None = None) -> str:
    """Save the open document to its own file (File > Save). Do this before editing that file with other tools."""
    live.save(window)

    return "Saved the document in Inkscape."


@tool
def live_reload(window: int | None = None) -> str:
    """Reload the open document from disk (File > Revert) so Inkscape shows edits made to its file.

    Unsaved changes in the window would be lost and Inkscape may ask first, so call live_save before editing.
    """
    live.reload(window)

    return "Reloaded the document in Inkscape."


def main() -> None:
    workspace.root.mkdir(parents=True, exist_ok=True)
    server.run()


if __name__ == "__main__":
    main()
