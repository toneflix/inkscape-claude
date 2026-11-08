"""Claude tool-use loop that edits one SVG file step by step with DocumentOperations."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .attachments import Attachments
from .claude import ClaudeClient
from .cli import InkscapeError
from .operations import BOOLEAN_ACTIONS, DocumentOperations
from .svgdoc import SvgError
from .usage import Usage, context_window

SYSTEM_PROMPT = """You are an illustrator working in Inkscape on one SVG document, using the tools provided. \
Carry out the person's request by editing the document with them.

How to work:
- Start with get_outline to see the pages, the elements, their ids and, if you need geometry, bounding boxes.
- The document can have several pages (artboards), each with a position and size on one shared canvas, in \
user units like the rest of the SVG. Put new artwork inside the page it belongs to; get_pages lists what is on \
each page. Use add_page, update_page and delete_page to change pages, and move_to_page to move objects between them.
- Make focused edits: add_elements, update_element, replace_element and delete_elements for markup; \
boolean_operation, align_objects and run_actions when Inkscape does the job better than hand-written geometry.
- Keep the ids, labels and layers of anything you do not need to change. Give new elements short, descriptive ids.
- The person can attach images to a message, usually as reference: a sketch, a style, a photo of what they \
want. Use them as context. Only put one in the drawing, with place_image and its file name, when they ask for \
that; trace or redraw it with paths when they ask for vector artwork.
- Write valid SVG 1.1 that Inkscape can edit. No <script>, event handler attributes, <foreignObject> or \
external URLs.
- Before you finish, call render_preview once (it shows every page, or one page if you pass page) and fix \
anything that looks wrong.
- When you are done, reply with one or two sentences saying what you changed."""

EXPECTED_ERRORS = (ValueError, KeyError, TypeError, OSError, SvgError, InkscapeError)


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


ID_LIST = {"type": "array", "items": {"type": "string"}}
STRING_MAP = {"type": "object", "additionalProperties": {"type": ["string", "null"]}}
PAGE = {"type": ["string", "integer"], "description": "Page number (from 1), id or label."}
NUMBER = {"type": "number"}

TOOLS = [
    {"name": "get_outline", "description": "List the document's elements with ids, labels, fills and text. "
     "with_bboxes adds Inkscape-measured bounding boxes (x, y, width, height).",
     "input_schema": schema({"with_bboxes": {"type": "boolean"}})},
    {"name": "get_pages", "description": "List the pages (artboards) with their number, id, label, position and "
     "size, and the objects on each.", "input_schema": schema({})},
    {"name": "add_page", "description": "Add a page. Without a position it goes to the right of the last page; "
     "without a size it copies the last page's size.",
     "input_schema": schema({"width": NUMBER, "height": NUMBER, "x": NUMBER, "y": NUMBER,
                             "label": {"type": "string"}})},
    {"name": "update_page", "description": "Rename, move or resize a page. Objects stay where they are.",
     "input_schema": schema({"page": PAGE, "label": {"type": "string"}, "x": NUMBER, "y": NUMBER, "width": NUMBER,
                             "height": NUMBER}, ["page"])},
    {"name": "delete_page", "description": "Remove a page. Objects on it stay on the canvas.",
     "input_schema": schema({"page": PAGE}, ["page"])},
    {"name": "move_to_page", "description": "Move objects to another page, keeping their position on the page "
     "(objects that are on no page are centred).",
     "input_schema": schema({"element_ids": ID_LIST, "page": PAGE}, ["element_ids", "page"])},
    {"name": "get_element", "description": "Return the SVG source of one element.",
     "input_schema": schema({"element_id": {"type": "string"}}, ["element_id"])},
    {"name": "add_elements", "description": "Add SVG elements (a fragment such as '<rect .../><circle .../>') "
     "to parent_id (default: the top layer), at the end or before before_id. Returns the new ids.",
     "input_schema": schema({"svg_fragment": {"type": "string"}, "parent_id": {"type": "string"},
                             "before_id": {"type": "string"}}, ["svg_fragment"])},
    {"name": "update_element", "description": "Change an element's attributes (null removes one; prefixes like "
     "inkscape:label work), merge CSS style properties, or set its text.",
     "input_schema": schema({"element_id": {"type": "string"}, "attributes": STRING_MAP, "style": STRING_MAP,
                             "text": {"type": "string"}}, ["element_id"])},
    {"name": "replace_element", "description": "Replace one element and its children with new SVG markup.",
     "input_schema": schema({"element_id": {"type": "string"}, "svg_fragment": {"type": "string"}},
                            ["element_id", "svg_fragment"])},
    {"name": "delete_elements", "description": "Delete elements by id.",
     "input_schema": schema({"element_ids": ID_LIST}, ["element_ids"])},
    {"name": "boolean_operation", "description": "Path boolean with Inkscape. List the bottom object first; for "
     "difference, cut and division the last object is the cutter. The result keeps the first object's style.",
     "input_schema": schema({"element_ids": ID_LIST, "operation": {"type": "string", "enum": list(BOOLEAN_ACTIONS)}},
                            ["element_ids", "operation"])},
    {"name": "align_objects", "description": "Align elements with Inkscape's Align tool. With relative_to page, "
     "each object aligns to the page it is on, or to page if given.",
     "input_schema": schema({"element_ids": ID_LIST, "page": PAGE,
                             "horizontal": {"type": "string", "enum": ["left", "hcenter", "right"]},
                             "vertical": {"type": "string", "enum": ["top", "vcenter", "bottom"]},
                             "relative_to": {"type": "string", "enum": ["page", "drawing", "selection", "first",
                                                                        "last", "biggest", "smallest"]}},
                            ["element_ids"])},
    {"name": "run_actions", "description": "Run Inkscape 1.x actions on the document, one per item, e.g. "
     "[\"select-by-id:a,b\", \"object-flip-horizontal\"] or [\"select-by-id:t1\", \"object-to-path\"].",
     "input_schema": schema({"actions": {"type": "array", "items": {"type": "string"}}}, ["actions"])},
    {"name": "get_bounding_boxes", "description": "Inkscape-measured bounding boxes for elements.",
     "input_schema": schema({"element_ids": ID_LIST})},
    {"name": "render_preview", "description": "Render all pages, one page, or one element to an image so you can "
     "see it.", "input_schema": schema({"width": {"type": "integer"}, "page": PAGE, "element_id": {"type": "string"}})},
    {"name": "place_image", "description": "Embed an image the person attached in the drawing, by file name. "
     "Without a size it keeps its pixel size, shrunk to fit the page; give width or height to scale it, both to "
     "stretch it. Without x and y it is centred on the page. Move or resize it later with update_element.",
     "input_schema": schema({"name": {"type": "string"}, "x": NUMBER, "y": NUMBER, "width": NUMBER, "height": NUMBER,
                             "page": PAGE, "element_id": {"type": "string"}}, ["name"])},
    {"name": "undo_last_change", "description": "Revert the document to before the most recent edit.",
     "input_schema": schema({})},
]


@dataclass
class Step:
    tool: str
    summary: str
    failed: bool = False


@dataclass
class TurnResult:
    reply: str
    steps: list[Step] = field(default_factory=list)


def summary(content) -> str:
    """One line describing a tool's result, for the chat's list of steps."""
    if isinstance(content, str):
        return content.splitlines()[0][:200] if content else ""

    return "rendered a preview"


class DrawingTools:
    """The drawing tools in TOOLS, working on one file. Shared by the API agent and the Claude Code bridge."""

    def __init__(self, operations: DocumentOperations, path: Path, attachments: Attachments | None = None):
        self.operations = operations
        self.path = path
        self.attachments = attachments

    def call(self, name: str, arguments: dict | None = None) -> str | list[dict]:
        """Runs a tool and returns its text, or image blocks for render_preview. Raises EXPECTED_ERRORS on failure."""
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            raise ValueError(f"Unknown tool '{name}'.")

        return handler(**(arguments or {}))

    def tool_get_outline(self, with_bboxes: bool = False) -> str:
        return self.operations.outline(self.path, with_bboxes)

    def tool_get_pages(self) -> str:
        return self.operations.pages(self.path)

    def tool_add_page(self, width: float | None = None, height: float | None = None, x: float | None = None,
                      y: float | None = None, label: str | None = None) -> str:
        return "Added " + self.operations.add_page(self.path, width, height, x, y, label).describe()

    def tool_update_page(self, page: str | int, label: str | None = None, x: float | None = None,
                         y: float | None = None, width: float | None = None, height: float | None = None) -> str:
        return "Updated " + self.operations.update_page(self.path, page, label, x, y, width, height).describe()

    def tool_delete_page(self, page: str | int) -> str:
        removed = self.operations.delete_page(self.path, page)

        return f"Deleted page {removed.number}.\n\n{self.operations.pages(self.path)}"

    def tool_move_to_page(self, element_ids: list[str], page: str | int) -> str:
        target = self.operations.move_to_page(self.path, element_ids, page)

        return f"Moved {', '.join(element_ids)} to page {target.number}.\n\n{self.operations.pages(self.path)}"

    def tool_get_element(self, element_id: str) -> str:
        return self.operations.element(self.path, element_id)

    def tool_add_elements(self, svg_fragment: str, parent_id: str | None = None, before_id: str | None = None) -> str:
        return "Added: " + ", ".join(self.operations.add(self.path, svg_fragment, parent_id, before_id))

    def tool_update_element(self, element_id: str, attributes: dict | None = None, style: dict | None = None,
                            text: str | None = None) -> str:
        self.operations.update(self.path, element_id, attributes, style, text)

        return f"Updated {element_id}."

    def tool_replace_element(self, element_id: str, svg_fragment: str) -> str:
        return "Replaced with: " + ", ".join(self.operations.replace(self.path, element_id, svg_fragment))

    def tool_delete_elements(self, element_ids: list[str]) -> str:
        return "Deleted: " + ", ".join(self.operations.delete(self.path, element_ids))

    def tool_boolean_operation(self, element_ids: list[str], operation: str) -> str:
        self.operations.boolean(self.path, element_ids, operation)

        return f"Applied {operation}.\n\n{self.operations.outline(self.path)}"

    def tool_align_objects(self, element_ids: list[str], horizontal: str | None = None, vertical: str | None = None,
                           relative_to: str = "page", page: str | int | None = None) -> str:
        self.operations.align(self.path, element_ids, horizontal, vertical, relative_to, page)

        return f"Aligned {', '.join(element_ids)}.\n\n{self.operations.bounding_boxes(self.path, element_ids)}"

    def tool_run_actions(self, actions: list[str]) -> str:
        log = self.operations.run_actions(self.path, actions)

        return f"Ran {len(actions)} action(s). {log}\n\n{self.operations.outline(self.path)}".strip()

    def tool_get_bounding_boxes(self, element_ids: list[str] | None = None) -> str:
        return self.operations.bounding_boxes(self.path, element_ids)

    def tool_render_preview(self, width: int = 800, element_id: str | None = None,
                            page: str | int | None = None) -> list[dict]:
        png = self.operations.preview(self.path, min(int(width), 1600), element_id, page=page)

        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(png).decode("ascii")}}]

    def tool_place_image(self, name: str, x: float | None = None, y: float | None = None, width: float | None = None,
                         height: float | None = None, page: str | int | None = None,
                         element_id: str | None = None) -> str:
        if self.attachments is None:
            raise ValueError("No images are attached in this conversation.")

        placed, (left, top, w, h) = self.operations.place_image(self.path, self.attachments.find(name), x, y, width,
                                                                height, page, element_id)

        return f"Placed {name} as {placed} at ({left:.1f}, {top:.1f}), {w:.1f} x {h:.1f}."

    def tool_undo_last_change(self) -> str:
        remaining = self.operations.undo(self.path)

        return "Nothing to undo." if remaining is None else "Reverted the last change."


class DrawingAgent:
    """Runs a conversation in which Claude edits `path` through tools. Keeps history across turns."""

    def __init__(self, client: ClaudeClient, operations: DocumentOperations, path: Path, max_steps: int = 40,
                 on_step: Callable[[Step], None] | None = None, system: str = SYSTEM_PROMPT,
                 on_text: Callable[[str], None] | None = None, attachments: Attachments | None = None,
                 on_usage: Callable[[Usage], None] | None = None):
        self.client = client
        self.tools = DrawingTools(operations, path, attachments)
        self.usage = Usage()
        self.on_usage = on_usage
        self.max_steps = max_steps
        self.on_step = on_step
        self.on_text = on_text
        self.system = system
        self.messages: list[dict] = []

    def state(self) -> dict:
        return {"messages": self.messages}

    def move_to(self, path: Path) -> None:
        self.tools.path = path

    def restore(self, state: dict) -> None:
        self.messages = list(state.get("messages") or [])

    def run(self, prompt: str, context: list[dict] | None = None) -> TurnResult:
        self.messages.append({"role": "user", "content": [*(context or []), {"type": "text", "text": prompt}]})
        steps: list[Step] = []

        for number in range(1, self.max_steps + 2):
            response = self.client.send(self.system, self.messages, TOOLS, on_text=self.on_text)
            self.messages.append({"role": "assistant", "content": response["content"]})
            self.record(response)

            if response.get("stop_reason") != "tool_use":
                return TurnResult(self.client.finish(response), steps)
            if number > self.max_steps:
                skipped = [{"type": "tool_result", "tool_use_id": block["id"], "is_error": True,
                            "content": "Not run: the step limit was reached."}
                           for block in response["content"] if block.get("type") == "tool_use"]
                self.messages.append({"role": "user", "content": skipped})
                break

            results = [self.execute(block, steps) for block in response["content"] if block.get("type") == "tool_use"]
            if number == self.max_steps:
                results.append({"type": "text", "text": "That was the last step allowed. Do not call more tools; "
                                                        "summarize what you changed."})
            self.messages.append({"role": "user", "content": results})

        return TurnResult(f"Stopped after {self.max_steps} steps.", steps)

    def record(self, response: dict) -> None:
        if not (response.get("usage") or response.get("limits")):
            return

        self.usage.record(response.get("usage"), response.get("limits"), context_window(self.client.model))
        if self.on_usage:
            self.on_usage(self.usage)

    def execute(self, block: dict, steps: list[Step]) -> dict:
        name = block.get("name", "")

        try:
            content = self.tools.call(name, block.get("input"))
            step = Step(name, summary(content))
            result = {"type": "tool_result", "tool_use_id": block["id"], "content": content}
        except EXPECTED_ERRORS as exc:
            step = Step(name, str(exc), failed=True)
            result = {"type": "tool_result", "tool_use_id": block["id"], "content": str(exc), "is_error": True}

        steps.append(step)
        if self.on_step:
            self.on_step(step)

        return result
