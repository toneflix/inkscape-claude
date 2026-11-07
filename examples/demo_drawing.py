"""Draws a small badge through the MCP server, the same way Claude would, and exports it.

Run: python examples/demo_drawing.py  (writes demo-badge.svg/png/pdf into the workspace)
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

STEPS = [
    ("create_document", dict(path="demo-badge.svg", width=480, height=480, background="#f6f1e7", overwrite=True)),
    ("add_elements", dict(path="demo-badge.svg", svg_fragment="""
      <defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#ffb35c"/><stop offset="1" stop-color="#e2546b"/></linearGradient></defs>
      <circle id="disc" cx="240" cy="220" r="150" fill="url(#sky)"/>
      <circle id="sun" cx="240" cy="230" r="62" fill="#fff4d6"/>
      <path id="hills" d="M60 300 Q160 220 250 290 T420 270 L420 400 L60 400 Z" fill="#3b2f5c"/>
      <circle id="hill-mask" cx="240" cy="220" r="150" fill="#000"/>
      <text id="title" x="240" y="440" font-family="sans-serif" font-size="34" font-weight="bold" text-anchor="middle" fill="#3b2f5c">INKSCAPE + CLAUDE</text>
    """)),
    ("boolean_operation", dict(path="demo-badge.svg", element_ids=["hills", "hill-mask"], operation="intersection")),
    ("align_objects", dict(path="demo-badge.svg", element_ids=["title"], horizontal="hcenter", relative_to="page")),
    ("read_document", dict(path="demo-badge.svg", with_bboxes=True)),
    ("export_document", dict(path="demo-badge.svg", output_path="demo-badge.png", dpi=96)),
    ("export_document", dict(path="demo-badge.svg", output_path="demo-badge.pdf")),
]


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "inkscape_mcp.server"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        for name, args in STEPS:
            res = await s.call_tool(name, args)
            print(f"## {name}\n{res.content[0].text if res.content[0].type == 'text' else res.content[0].type}\n")
            if res.is_error:
                sys.exit(1)


asyncio.run(main())
