#!/usr/bin/env python3
"""Ask Claude: an Inkscape 1.x extension (Extensions > Claude > Ask Claude…).

Sends the drawing (or the selected objects) and a prompt to Claude, then puts Claude's SVG
back into the document. Ctrl+Z undoes the whole change in one step.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import tempfile
from pathlib import Path

import inkex
from inkex import command
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_api import DEFAULT_MODEL, ClaudeClient, ClaudeError, Config  # noqa: E402

SVG_WRAPPER = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd">{}</svg>'
)

NAMEDVIEW_TAG = "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}namedview"

SYSTEM_PROMPT = """You are an illustrator and SVG expert working inside Inkscape. The person gives you \
an SVG drawing (or part of one), usually a picture of how it currently looks, and an instruction. \
Carry out the instruction by writing SVG.

Rules for your answer:
- Put the SVG in exactly one fenced code block that starts with ```svg. Anything outside the block is \
shown to the person as your note, so keep notes to a sentence or two, or leave them out.
- Write valid SVG 1.1 that Inkscape can edit: real shapes, paths, text and gradients. No <script>, no event \
handler attributes, no <foreignObject>, no external files or URLs, no CSS @import.
- Keep the ids, inkscape:label attributes and layer structure of anything you do not need to change, so the \
person's existing work and references survive. Give new elements short, descriptive ids.
- Prefer clean, editable geometry: simple shapes where they fit, grouped logically, with readable path data.
- Placeholders such as href="data:inkscape-claude-image-3" stand for embedded images that were left out to \
save space. Keep them exactly as they are; they are restored afterwards.
"""

SCOPE_INSTRUCTIONS = {
    "document": "Return the complete, updated SVG document (the whole <svg> root, including <defs> and \
sodipodi:namedview if present).",
    "selection": "Return only the SVG elements that should replace the selected elements, as a fragment \
(no <svg> root). Put any gradients, markers or filters they need in a <defs> element in the same block. \
Use the same coordinate system as the selected elements.",
    "add": "Return only the new SVG elements to add, as a fragment (no <svg> root). Do not repeat existing \
elements. Put any gradients, markers or filters they need in a <defs> element in the same block. \
Coordinates are in the target layer's user space, described below.",
}


class ImagePlaceholders:
    """Swaps large embedded data URIs for short keys before sending, and back afterwards."""

    PATTERN = re.compile(r'((?:xlink:)?href\s*=\s*")(data:[^"]{200,})(")')

    def __init__(self):
        self.images: dict[str, str] = {}

    def strip(self, text: str) -> str:
        return self.PATTERN.sub(self._replace, text)

    def restore(self, text: str) -> str:
        for key, data in self.images.items():
            text = text.replace(key, data)

        return text

    def _replace(self, match) -> str:
        key = f"data:inkscape-claude-image-{len(self.images) + 1}"
        self.images[key] = match.group(2)

        return match.group(1) + key + match.group(3)


class ClaudeReply:
    """Claude's answer split into the SVG markup and the note around it."""

    FENCE = re.compile(r"```(?:svg|xml|html)?\s*\n(.*?)```", re.S | re.I)
    BARE_SVG = re.compile(r"(<svg[\s>].*</svg>)", re.S)

    def __init__(self, svg: str | None, note: str):
        self.svg = svg
        self.note = note

    @classmethod
    def parse(cls, reply: str) -> ClaudeReply:
        match = cls.FENCE.search(reply)
        if match and "<" in match.group(1):
            return cls(match.group(1).strip(), cls._around(reply, match))

        match = cls.BARE_SVG.search(reply)
        if match:
            return cls(match.group(1).strip(), cls._around(reply, match))

        return cls(None, reply.strip())

    @staticmethod
    def _around(reply: str, match) -> str:
        return (reply[: match.start()] + reply[match.end():]).strip()


class Markup:
    """Makes Claude's SVG safe to insert: renames clashing ids and removes active content."""

    @staticmethod
    def rename_clashing_ids(text: str, taken: set[str]) -> str:
        new_ids = re.findall(r'\sid\s*=\s*"([^"]+)"', text)
        used = set(taken) | set(new_ids)

        for old in dict.fromkeys(new_ids):
            if old not in taken:
                continue

            number = 1
            while f"{old}-{number}" in used:
                number += 1
            new = f"{old}-{number}"
            used.add(new)

            escaped = re.escape(old)
            text = re.sub(rf'(\sid\s*=\s*"){escaped}(")', rf"\g<1>{new}\2", text)
            text = re.sub(rf"url\(\s*#{escaped}\s*\)", f"url(#{new})", text)
            text = re.sub(rf'(href\s*=\s*"#){escaped}(")', rf"\g<1>{new}\2", text)

        return text

    @staticmethod
    def sanitize(root) -> None:
        for element in list(root.iter()):
            if not isinstance(element.tag, str):
                continue

            if etree.QName(element).localname in ("script", "foreignObject"):
                element.getparent().remove(element)
                continue

            for attribute in list(element.attrib):
                local = etree.QName(attribute).localname
                value = element.attrib[attribute].strip().lower()
                if local.lower().startswith("on"):
                    del element.attrib[attribute]
                elif local == "href" and value.startswith(("javascript:", "http")):
                    del element.attrib[attribute]


class AskClaude(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--tab", default="prompt")
        pars.add_argument("--prompt", default="")
        pars.add_argument("--scope", default="auto")
        pars.add_argument("--send_image", type=inkex.Boolean, default=True)
        pars.add_argument("--show_reply", type=inkex.Boolean, default=True)
        pars.add_argument("--model", default=DEFAULT_MODEL)
        pars.add_argument("--effort", default="medium")
        pars.add_argument("--api_key", default="")
        pars.add_argument("--max_tokens", type=int, default=64000)

    def effect(self):
        prompt = self.options.prompt.replace("\\n", "\n").strip()
        if not prompt:
            raise inkex.AbortExtension("Type what you want Claude to do in the Prompt box.")

        selected = list(self.selected_in_order())
        scope = self.resolve_scope(selected)
        self.placeholders = ImagePlaceholders()

        reply = ClaudeReply.parse(self.ask(prompt, scope, selected))
        if reply.svg is None:
            raise inkex.AbortExtension("Claude did not return any SVG. Its reply was:\n\n" + reply.note[:2000])

        svg = self.placeholders.restore(reply.svg)
        if scope == "document":
            self.apply_document(svg)
        elif scope == "selection":
            self.apply_selection(svg, selected)
        else:
            self.apply_add(svg)

        if reply.note and self.options.show_reply:
            self.msg(f"Claude: {reply.note}")

    def resolve_scope(self, selected) -> str:
        scope = self.options.scope
        if scope == "auto":
            return "selection" if selected else "document"

        if scope == "selection" and not selected:
            raise inkex.AbortExtension("Nothing is selected. Select objects first, or choose another "
                                       "'Apply to' option.")

        return scope

    def ask(self, prompt: str, scope: str, selected) -> str:
        try:
            client = ClaudeClient.create(Config.api_key(self.options.api_key),
                                         model=self.options.model.strip() or DEFAULT_MODEL,
                                         effort=self.options.effort, max_tokens=self.options.max_tokens)
        except ClaudeError as exc:
            raise inkex.AbortExtension(str(exc))

        content = []
        if self.options.send_image:
            png = self.render_png()
            if png:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": png}})

        context = self.describe(scope, selected)
        content.append({"type": "text",
                        "text": f"{context}\n\nInstruction: {prompt}\n\n{SCOPE_INSTRUCTIONS[scope]}"})

        try:
            return client.ask(SYSTEM_PROMPT, content)
        except ClaudeError as exc:
            raise inkex.AbortExtension(str(exc))

    def selected_in_order(self):
        selection = self.svg.selection
        if hasattr(selection, "rendering_order"):
            return selection.rendering_order()

        return selection.values()

    def target_layer(self):
        layer = self.svg.get_current_layer()
        if layer is not self.svg:
            return layer

        layers = self.svg.xpath("./svg:g[@inkscape:groupmode='layer']")

        return layers[-1] if layers else layer

    def describe(self, scope: str, selected) -> str:
        page = f"Page: width={self.svg.get('width')} height={self.svg.get('height')} viewBox={self.svg.get('viewBox')}."

        if scope == "document":
            return f"{page}\nThe image shows the current page.\n\nCurrent document:\n```svg\n{self.document_source()}\n```"

        if scope == "selection":
            return self.describe_selection(page, selected)

        layer = self.target_layer()
        transform = self.transform_note(layer, " (its transform to page coordinates is {})")

        return (f"{page}\nNew elements go at the top of layer id=\"{layer.get('id')}\"{transform}.\n\n"
                f"Current document, for context:\n```svg\n{self.document_source()}\n```")

    def describe_selection(self, page: str, selected) -> str:
        parent = selected[0].getparent()
        fragment = "\n".join(self.placeholders.strip(etree.tostring(el, encoding="unicode")) for el in selected)
        transform = self.transform_note(parent, ", whose transform to page coordinates is {}")
        defs = self.defs_source()

        text = (f"{page}\nThe image shows the whole page; only the selected elements below will be replaced.\n"
                f"Their parent element is <{etree.QName(parent).localname} id=\"{parent.get('id')}\">{transform}.\n")
        if defs:
            text += f"\nDocument <defs> (for reference; you can reuse these ids):\n```svg\n{defs}\n```\n"

        return text + f"\nSelected elements ({len(selected)}):\n```svg\n{fragment}\n```"

    @staticmethod
    def transform_note(element, template: str) -> str:
        if not hasattr(element, "composed_transform"):
            return ""

        transform = str(element.composed_transform())

        return template.format(transform) if transform else ""

    def document_source(self) -> str:
        return self.placeholders.strip(etree.tostring(self.document, encoding="unicode"))

    def defs_source(self) -> str:
        defs = self.svg.defs
        if defs is None or len(defs) == 0:
            return ""

        return self.placeholders.strip("\n".join(etree.tostring(child, encoding="unicode") for child in defs))

    def render_png(self) -> str | None:
        """Render the page to PNG with Inkscape; returns base64, or None if rendering fails."""
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = os.path.join(tmp, "canvas.png")
                command.inkscape(self.options.input_file, export_filename=output, export_type="png",
                                 export_width=1024, export_area_page=True, export_background="#ffffff",
                                 export_background_opacity=1)

                with open(output, "rb") as handle:
                    return base64.b64encode(handle.read()).decode("ascii")
        except Exception:
            return None

    def apply_document(self, svg: str):
        document = inkex.load_svg(svg.encode("utf-8"))
        root = document.getroot()
        if etree.QName(root).localname != "svg":
            raise inkex.AbortExtension("Claude's answer is not a complete SVG document.")

        Markup.sanitize(root)

        namedview = self.svg.find(NAMEDVIEW_TAG)
        if namedview is not None and root.find(NAMEDVIEW_TAG) is None:
            root.insert(0, namedview)

        self.document = document
        self.svg = root

    def apply_selection(self, svg: str, selected):
        removed = {node.get("id") for el in selected for node in el.iter() if isinstance(node.tag, str)}
        items = self.parse_fragment(svg, set(self.svg.xpath("//@id")) - removed)
        anchor = selected[0]

        if any(el.getparent() is not anchor.getparent() for el in selected):
            self.msg("Note: the selected objects were in different groups or layers; "
                     "Claude's result was placed where the first one was.")

        for item in items:
            anchor.addprevious(item)
        for el in selected:
            el.getparent().remove(el)

    def apply_add(self, svg: str):
        items = self.parse_fragment(svg, set(self.svg.xpath("//@id")))
        layer = self.target_layer()

        for item in items:
            layer.append(item)

    def parse_fragment(self, svg: str, taken_ids: set[str]):
        """Parse Claude's fragment, move its <defs> into the document and return the drawable elements."""
        text = svg.strip()
        if text.startswith("<?xml"):
            text = text.split("?>", 1)[1]
        if not re.match(r"\s*<svg[\s>]", text):
            text = SVG_WRAPPER.format(text)

        try:
            root = inkex.load_svg(Markup.rename_clashing_ids(text, taken_ids).encode("utf-8")).getroot()
        except etree.XMLSyntaxError as exc:
            raise inkex.AbortExtension(f"Claude's SVG could not be parsed: {exc}")

        Markup.sanitize(root)

        items = []
        for child in list(root):
            if not isinstance(child.tag, str):
                continue

            name = etree.QName(child).localname
            if name == "defs":
                for definition in list(child):
                    self.svg.defs.append(definition)
            elif name not in ("namedview", "metadata", "title", "desc"):
                items.append(child)

        if not items:
            raise inkex.AbortExtension("Claude's answer had no drawable elements.")

        return items


if __name__ == "__main__":
    AskClaude().run()
