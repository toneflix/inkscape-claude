"""Build dist/ask-claude-extension.zip: the Inkscape extension plus the inkscape_mcp code it uses.

Unzip the result into Inkscape's user extensions folder.
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist" / "ask-claude-extension.zip"


def files():
    for path in sorted((ROOT / "extension").iterdir()):
        if path.suffix in (".inx", ".py"):
            yield path, Path("claude") / path.name

    for path in sorted((ROOT / "mcp_server" / "inkscape_mcp").iterdir()):
        if path.suffix in (".py", ".html"):
            yield path, Path("claude") / "inkscape_mcp" / path.name


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)

    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, name in files():
            archive.write(source, name.as_posix())

    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
