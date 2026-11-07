"""Tests for pages (artboards): the page model, the Inkscape-backed page operations and the agent's page tools."""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inkscape_mcp.cli import Inkscape  # noqa: E402
from inkscape_mcp.operations import DocumentOperations  # noqa: E402
from inkscape_mcp.pages import PageError  # noqa: E402
from inkscape_mcp.svgdoc import SvgDocument  # noqa: E402
from test_agent import make_agent, tool_call, done  # noqa: E402

TWO_PAGES = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" width="100mm" height="50mm"
     viewBox="0 0 100 50" id="svg1">
  <sodipodi:namedview id="nv1">
    <inkscape:page id="p1" x="0" y="0" width="100" height="50" inkscape:label="Front"/>
    <inkscape:page id="p2" x="110" y="0" width="100" height="50" inkscape:label="Back"/>
  </sodipodi:namedview>
  <g id="layer1" inkscape:groupmode="layer">
    <rect id="a" x="10" y="10" width="20" height="20" fill="red"/>
    <rect id="b" x="120" y="10" width="20" height="20" fill="blue"/>
    <rect id="lost" x="300" y="300" width="5" height="5"/>
  </g>
</svg>"""


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


@pytest.fixture
def two_pages(tmp_path):
    path = tmp_path / "pages.svg"
    path.write_text(TWO_PAGES)

    return path, DocumentOperations(Inkscape())


def test_a_document_without_pages_has_one_implicit_page():
    doc = SvgDocument.blank(200, 100)

    [page] = doc.pages.list()

    assert (page.number, page.id, page.box) == (1, None, (0, 0, 200, 100))
    assert "page 1: x=0 y=0 size=200x100" in doc.outline()


def test_adding_a_page_makes_the_first_one_explicit_and_goes_to_the_right():
    doc = SvgDocument.blank(200, 100)

    added = doc.pages.add(label="Back")

    assert [page.id for page in doc.pages.list()] == ["page1", "page2"]
    assert (added.number, added.label, added.box) == (2, "Back", (220, 0, 200, 100))
    assert doc.pages.add(width=50, height=60, x=0, y=200).box == (0, 200, 50, 60)


def test_pages_are_found_by_number_id_or_label():
    doc = SvgDocument.parse(TWO_PAGES)

    assert doc.pages.get(2).id == "p2"
    assert doc.pages.get("2").id == "p2"
    assert doc.pages.get("Back").id == "p2"
    assert doc.pages.get("p1").label == "Front"

    with pytest.raises(PageError, match="Front, Back"):
        doc.pages.get("Inside")


def test_changing_the_first_page_keeps_the_viewport_on_it():
    doc = SvgDocument.parse(TWO_PAGES)

    doc.pages.update(1, width=80, label="Cover")

    assert doc.root.get("viewBox") == "0 0 80 50"
    assert doc.root.get("width") == "80mm"
    assert doc.pages.get(1).label == "Cover"

    doc.pages.delete("Cover")

    assert doc.root.get("viewBox") == "110 0 100 50"
    with pytest.raises(PageError, match="at least one page"):
        doc.pages.delete(1)


def test_units_convert_between_user_units_and_px():
    doc = SvgDocument.parse(TWO_PAGES)

    px = doc.to_px((110, 0, 100, 50))

    assert doc.user_unit_px() == pytest.approx(96 / 25.4)
    assert px == pytest.approx((415.748, 0, 377.953, 188.976), rel=1e-4)
    assert doc.from_px(px) == pytest.approx((110, 0, 100, 50))


def test_listing_shows_what_is_on_each_page(two_pages):
    path, operations = two_pages

    listing = operations.pages(path)

    assert 'page 1 id=p1 "Front": x=0 y=0 size=100x50\n  objects: a' in listing
    assert 'page 2 id=p2 "Back": x=110 y=0 size=100x50\n  objects: b' in listing
    assert "Off every page: lost" in listing
    assert "1 user unit = 3.78px" in listing
    assert "rect id=b fill=blue bbox=(120.0,10.0,20.0x20.0) on page 2" in operations.outline(path, True)


def test_move_to_page_keeps_the_spot_and_centres_lost_objects(two_pages):
    path, operations = two_pages

    operations.move_to_page(path, ["a", "lost"], "Back")
    boxes = operations.boxes(path)

    assert boxes["a"][:2] == pytest.approx((120, 10), abs=0.01)
    assert boxes["lost"][:2] == pytest.approx((157.5, 22.5), abs=0.01)


def test_align_to_page_uses_the_page_each_object_is_on(two_pages):
    path, operations = two_pages

    operations.align(path, ["a", "b"], "right", "bottom")
    boxes = operations.boxes(path)

    assert boxes["a"][:2] == pytest.approx((80, 30), abs=0.01)
    assert boxes["b"][:2] == pytest.approx((190, 30), abs=0.01)

    operations.align(path, ["a"], "left", None, page=2)

    assert operations.boxes(path)["a"][0] == pytest.approx(110, abs=0.01)


def test_preview_shows_all_pages_or_one(two_pages):
    path, operations = two_pages

    everything = png_size(operations.preview(path, 420))
    back = png_size(operations.preview(path, 200, page="Back"))

    assert everything == (420, 100)
    assert back == (200, 100)


def test_agent_adds_a_page_and_moves_artwork_onto_it(tmp_path):
    agent, client, path = make_agent(tmp_path, [
        tool_call("a", "add_elements", svg_fragment='<circle id="sun" cx="50" cy="50" r="10"/>'),
        tool_call("b", "add_page", label="Night"),
        tool_call("c", "move_to_page", element_ids=["sun"], page="Night"),
        tool_call("d", "render_preview", page=2, width=100),
        tool_call("e", "get_pages"),
        done("Moved the sun to a new page."),
    ])

    result = agent.run("Put the sun on its own page")

    pages_result = client.calls[-1][-1]["content"][0]["content"]

    assert [step.failed for step in result.steps] == [False] * 5
    assert 'page 2 id=page2 "Night": x=120 y=0 size=100x100\n  objects: sun' in pages_result
    assert "page 1 id=page1: x=0 y=0 size=100x100\n  objects: (empty)" in pages_result


def test_preview_zooms_to_focus_objects(tmp_path):
    path = tmp_path / "focus.svg"
    doc = SvgDocument.blank(200, 100)
    doc.add('<rect id="a" x="10" y="10" width="20" height="20"/><rect id="tall" x="150" y="0" width="2" height="80"/>')
    doc.save(path)
    operations = DocumentOperations(Inkscape())

    assert png_size(operations.preview(path, 100)) == (100, 50)
    assert png_size(operations.preview(path, 100, focus=["a"])) == (100, 100)
    assert png_size(operations.preview(path, 100, focus=["tall"])) == (100, 200), "no taller than twice as wide"
    assert png_size(operations.preview(path, 100, focus=["gone"])) == (100, 50), "deleted objects show everything"
