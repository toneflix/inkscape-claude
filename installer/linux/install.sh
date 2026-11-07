#!/bin/sh
# Inkscape Claude @VERSION@ installer for Linux.
#
#   sh inkscape-claude-*.run              install for the current user
#   sh inkscape-claude-*.run --uninstall  remove it again
#
# Installs the extension into every Inkscape found (regular, Flatpak, Snap)
# and the inkscape-mcp executable into ~/.local/bin.

set -eu

CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN_DIR="${INKSCAPE_CLAUDE_BIN_DIR:-$HOME/.local/bin}"

extension_dirs() {
    echo "$CONFIG_HOME/inkscape/extensions"

    if [ -d "$HOME/.var/app/org.inkscape.Inkscape" ]; then
        echo "$HOME/.var/app/org.inkscape.Inkscape/config/inkscape/extensions"
    fi

    if [ -d "$HOME/snap/inkscape" ]; then
        echo "$HOME/snap/inkscape/current/.config/inkscape/extensions"
    fi
}

uninstall() {
    extension_dirs | while read -r dir; do
        rm -rf "$dir/claude"
        echo "Removed $dir/claude"
    done

    rm -f "$BIN_DIR/inkscape-mcp"
    echo "Removed $BIN_DIR/inkscape-mcp"
}

install() {
    payload=$(mktemp -d)
    trap 'rm -rf "$payload"' EXIT

    line=$(awk '/^__PAYLOAD_BELOW__$/ { print NR + 1; exit }' "$0")
    tail -n +"$line" "$0" | tar -xzf - -C "$payload"

    extension_dirs | while read -r dir; do
        mkdir -p "$dir"
        rm -rf "$dir/claude"
        cp -R "$payload/extension/claude" "$dir/claude"
        echo "Installed the extension in $dir/claude"
    done

    mkdir -p "$BIN_DIR"
    cp "$payload/bin/inkscape-mcp" "$BIN_DIR/inkscape-mcp"
    chmod 755 "$BIN_DIR/inkscape-mcp"
    echo "Installed $BIN_DIR/inkscape-mcp"

    echo
    echo "Restart Inkscape, then find Ask Claude and Open Claude Chat under Extensions > Claude."
    echo "To use the MCP server in Claude Code:  claude mcp add inkscape -- $BIN_DIR/inkscape-mcp"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) echo "Note: $BIN_DIR is not on your PATH." ;;
    esac
}

case "${1:-}" in
    --uninstall) uninstall ;;
    "") install ;;
    *) echo "Usage: sh $0 [--uninstall]" >&2; exit 2 ;;
esac

exit 0
__PAYLOAD_BELOW__
