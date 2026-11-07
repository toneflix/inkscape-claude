"""End-to-end check: start the MCP server over stdio and call every tool once."""

import asyncio
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    ws = tempfile.mkdtemp()
    params = StdioServerParameters(command=sys.executable, args=["-m", "inkscape_mcp.server"],
                                   env={**os.environ, "INKSCAPE_MCP_WORKSPACE": ws})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("TOOLS:", tools)
            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                for c in res.content:
                    if c.type == "text":
                        print(f"--- {name}{' ERROR' if res.is_error else ''}\n{c.text[:600]}")
                    else:
                        print(f"--- {name}: {c.type} {getattr(c,'mime_type','')} {len(getattr(c,'data',''))} b64 chars")
                assert not res.is_error, name
                return res
            await call("inkscape_info")
            await call("create_document", path="t.svg", width=400, height=300, background="#f4f1ea")
            await call("add_elements", path="t.svg", svg_fragment='<rect id="sq" x="40" y="40" width="120" height="120" fill="#d33"/><circle id="ci" cx="160" cy="160" r="70" fill="#36c"/><text x="20" y="280" font-size="24">Hello</text>')
            await call("update_element", path="t.svg", element_id="text1", style={"fill": "#222"}, text="Hello Inkscape")
            await call("read_document", path="t.svg", with_bboxes=True)
            await call("boolean_operation", path="t.svg", element_ids=["sq", "ci"], operation="union")
            await call("align_objects", path="t.svg", element_ids=["sq"], horizontal="hcenter", vertical="vcenter")
            await call("get_bounding_boxes", path="t.svg", element_ids=["sq"])
            await call("render_preview", path="t.svg", width=400)
            await call("export_document", path="t.svg", output_path="t.pdf")
            await call("export_document", path="t.svg", output_path="t.png", dpi=150)
            await call("add_page", path="t.svg", label="Back")
            await call("update_page", path="t.svg", page="Back", width=300)
            await call("move_to_page", path="t.svg", element_ids=["sq"], page="2")
            await call("align_objects", path="t.svg", element_ids=["sq"], horizontal="left", page="Back")
            await call("list_pages", path="t.svg")
            await call("render_preview", path="t.svg", width=400, page="Back")
            await call("export_document", path="t.svg", output_path="back.png", page="Back")
            await call("export_document", path="t.svg", output_path="all.png", area="pages")
            await call("delete_page", path="t.svg", page="Back")
            await call("create_document", path="book.svg", width=200, height=300, pages=3)
            await call("undo_last_change", path="t.svg")
            await call("list_documents")
            await call("list_actions", filter="path-")
            res = await s.call_tool("read_document", {"path": "../escape.svg"})
            assert res.is_error; print("--- path escape correctly refused:", res.content[0].text[:120])
    print("WORKSPACE", ws)


asyncio.run(main())
