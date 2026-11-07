#!/bin/sh
# Removes Inkscape Claude:  sudo sh "/Library/Application Support/Inkscape Claude/uninstall.sh"

set -eu

user="${SUDO_USER:-$(stat -f%Su /dev/console)}"
home=$(dscl . -read "/Users/$user" NFSHomeDirectory | awk '{ print $2 }')

rm -rf "$home/Library/Application Support/org.inkscape.Inkscape/config/inkscape/extensions/claude"
rm -f /usr/local/bin/inkscape-mcp
rm -rf "/Library/Application Support/Inkscape Claude"
pkgutil --forget org.inkscape-claude.installer >/dev/null 2>&1 || true

echo "Inkscape Claude was removed."
