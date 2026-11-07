"""Unit tests for the MCP server's document model and workspace (no Inkscape needed)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))

from inkscape_mcp.svgdoc import INKSCAPE_NS, SvgDocument, SvgError, parse_style  # noqa: E402
from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.operations import DocumentOperations  # noqa: E402
from inkscape_mcp.workspace import Workspace  # noqa: E402


def blank():
    return SvgDocument.blank(100, 50, background="#fff")


def test_blank_document_has_layer_and_background():
    outline = blank().outline()

    assert "layer1" in outline and "(layer)" in outline and "background" in outline


def test_add_assigns_unique_ids():
    doc = blank()

    ids = doc.add('<rect width="1" height="1"/><rect id="background" width="2" height="2"/><circle r="3"/>')

    assert len(ids) == 3 and len(set(ids)) == 3
    assert "background" not in ids
    assert ids[0].startswith("rect")


def test_add_before_and_into_parent():
    doc = blank()
    doc.add('<g id="grp"/>')

    doc.add('<rect id="a"/>', parent_id="grp")
    doc.add('<rect id="b"/>', parent_id="grp", before_id="a")

    assert [child.get("id") for child in doc.find("grp")] == ["b", "a"]


def test_update_style_text_and_namespaced_attribute():
    doc = blank()
    doc.add('<text id="t"><tspan>old</tspan><tspan>x</tspan></text>')

    doc.update("t", attributes={"inkscape:label": "Title", "x": "5"},
               style={"fill": "red", "font-size": "12px"}, text="new")
    element = doc.find("t")

    assert element.get("x") == "5" and element.get(f"{{{INKSCAPE_NS}}}label") == "Title"
    assert parse_style(element.get("style")) == {"fill": "red", "font-size": "12px"}
    assert "".join(element.itertext()) == "new"

    doc.update("t", style={"fill": None}, attributes={"x": None})

    assert element.get("x") is None and "fill" not in element.get("style")


def test_replace_and_delete():
    doc = blank()

    doc.replace("background", '<circle id="bg2" r="4"/>')

    assert "bg2" in doc.outline() and "background" not in doc.outline()

    doc.delete(["bg2"])

    with pytest.raises(SvgError):
        doc.find("bg2")


def test_bad_input_is_rejected():
    with pytest.raises(SvgError):
        SvgDocument.parse("<svg><rect></svg>")

    with pytest.raises(SvgError):
        SvgDocument.parse("<html/>")

    with pytest.raises(SvgError):
        blank().add("just text")


def test_entities_are_not_expanded():
    evil = ('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            '<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>')

    assert "root:" not in SvgDocument.parse(evil).to_string()


def test_workspace_refuses_paths_outside_root(tmp_path):
    workspace = Workspace(tmp_path)

    with pytest.raises(ValueError):
        workspace.resolve("../outside.svg", must_exist=False)


def test_operations_edit_and_undo(tmp_path):
    operations = DocumentOperations(Inkscape())
    target = tmp_path / "a.svg"
    operations.write(target, blank())

    operations.delete(target, ["background"])

    assert not SvgDocument.load(target).has("background")
    assert operations.undo(target) == 0
    assert SvgDocument.load(target).has("background")
