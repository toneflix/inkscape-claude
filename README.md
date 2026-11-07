# Inkscape + Claude (prototype)

Two ways for Claude to work with Inkscape 1.x:

1. **`inkscape-mcp`**: an MCP server. Claude (in Claude Desktop, Claude Code, or any MCP client) can
   create and edit SVG documents, look at a rendered preview, and run Inkscape for booleans, alignment,
   any other Inkscape action, and export to PNG/PDF/EPS.
2. **Ask Claude…**: an Inkscape extension (`Extensions > Claude > Ask Claude…`). Type a prompt, and
   Claude edits the open drawing, only the selected objects, or adds new artwork. Ctrl+Z undoes it.

![Demo badge drawn through the MCP server](examples/demo-badge.png)

The badge above was drawn through the MCP server by `examples/demo_drawing.py`, using the same tool calls
Claude makes: create a page, add shapes, a path intersection, page alignment, then export.

## 1. MCP server

### Install

```bash
cd mcp_server
pip install .            # needs Python 3.10+, installs `mcp` (2.x) and `lxml`
```

Inkscape 1.x must be installed for previews, exports and actions. The server finds it on `PATH`,
in the usual macOS/Windows install folders, or at `INKSCAPE_BIN`. Plain SVG editing works without it.

### Connect it to Claude

**Claude Code**

```bash
claude mcp add inkscape -- inkscape-mcp
```

**Claude Desktop**: add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "inkscape": {
      "command": "inkscape-mcp",
      "env": { "INKSCAPE_MCP_WORKSPACE": "/Users/you/Drawings" }
    }
  }
}
```

Then ask something like *"Draw a 600×400 poster for a jazz night in poster.svg, check the preview,
and export a PDF."*

### Tools

| Tool | What it does |
| --- | --- |
| `create_document` | New blank page (with a layer) or from full SVG source |
| `read_document` | Compact outline of elements (ids, labels, fills, text, optional bounding boxes) or full source |
| `write_document` | Replace the whole document |
| `add_elements` / `update_element` / `replace_element` / `delete_elements` / `get_element` | Element-level edits; new elements get unique ids |
| `render_preview` | Renders the page or one object to PNG and returns the image so Claude can check its work |
| `export_document` | PNG, PDF, EPS, PS, EMF, WMF or plain SVG; page, drawing or one object; DPI/size |
| `boolean_operation` | Union, difference, intersection, exclusion, division, cut, combine |
| `align_objects` | Inkscape's Align tool (relative to page, drawing, selection, first, last…) |
| `run_actions` | Any Inkscape 1.x action list, e.g. `["select-by-id:a,b", "object-flip-horizontal"]` |
| `list_actions` | Searches the action names Inkscape knows |
| `get_bounding_boxes` | Inkscape-measured geometry (handles text, transforms and strokes) |
| `undo_last_change` | Every edit snapshots the file first; this steps back one snapshot |
| `list_documents`, `inkscape_info`, `open_in_inkscape` | Housekeeping; the last opens the file in the Inkscape app |

### Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `INKSCAPE_MCP_WORKSPACE` | `~/InkscapeClaude` | Folder relative paths resolve in. Paths outside it are refused. |
| `INKSCAPE_MCP_ALLOW_ANY_PATH` | off | Set to `1` to allow any path on disk. |
| `INKSCAPE_BIN` | auto-detect | Path to the Inkscape executable. |
| `INKSCAPE_TIMEOUT` | `120` | Seconds before an Inkscape run is stopped. |

## 2. "Ask Claude…" extension

### Install

Copy the three files in `extension/` into your Inkscape user extensions folder, in a subfolder such as
`claude/`, then restart Inkscape. Find the folder in **Edit > Preferences > System > User extensions**.
Typical locations:

- Linux: `~/.config/inkscape/extensions/`
- macOS: `~/Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions/`
- Windows: `%APPDATA%\inkscape\extensions\`

### API key

Get a key from the [Claude Console](https://platform.claude.com/), then do one of:

- paste it in the extension's **Settings** tab, or
- set `ANTHROPIC_API_KEY` in the environment Inkscape starts from, or
- create `~/.config/inkscape-claude/config.json` (Windows: `%APPDATA%\inkscape-claude\config.json`)
  containing `{"api_key": "sk-ant-..."}`.

### Use

**Extensions > Claude > Ask Claude…**, type what you want, and press Apply.

- **Apply to**: the selection if there is one, otherwise the whole drawing (default); or force whole
  drawing / selection only; or *Add new artwork* to the current layer without touching anything else.
- A picture of the canvas is sent along by default so Claude can see what it is editing.
- Defaults: model `claude-opus-5-5`, adaptive thinking, effort `medium`. Change them in Settings.
- Embedded raster images are swapped for short placeholders before sending and restored afterwards,
  so a photo in the document does not use up the request.
- Claude's SVG is cleaned before it goes in: scripts, event handlers, `foreignObject` and external links
  are removed. Ids that clash with existing ones are renamed.

If the `anthropic` Python package is installed in the Python that Inkscape uses, the extension uses it
(with streaming). Otherwise it falls back to a plain HTTPS request with the standard library, because
Inkscape's bundled Python on Windows and macOS usually does not have extra packages.

### Limits of this first version

- Inkscape freezes while the extension waits for Claude (often 10–60 seconds). Extensions in Inkscape
  are one-shot, so there is no back-and-forth chat inside the canvas yet.
- *Selection only* replaces the selected objects in place. If they sit in different groups or layers,
  the result goes where the first one was.
- The MCP server works on files. It does not drive an Inkscape window that is already open; use
  `open_in_inkscape`, or File > Revert in Inkscape, to see changes made on disk.

## Tests

```bash
pip install pytest
python -m pytest tests            # 16 tests; the extension runs against a fake Claude API, no key needed
python tests/smoke_mcp.py         # starts the MCP server and calls every tool once (needs Inkscape)
python examples/demo_drawing.py   # draws the demo badge into the workspace
```

Tested with Inkscape 1.2.2, Python 3.13, `mcp` 2.3, `inkex` 1.4 and `anthropic` 2.6 on Linux.

## Ideas for next steps

- Live control of a running Inkscape window through D-Bus/Gio actions (Linux) so changes show up instantly.
- A docked chat panel: a small local server that the extension talks to, so the conversation keeps going
  across several edits.
- Let the extension call the MCP tools itself (booleans, alignment) through Claude tool use, instead
  of writing everything as raw SVG.
