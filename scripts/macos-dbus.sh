#!/bin/sh
# macOS: runs a D-Bus session bus at login and tells apps opened from Finder or the Dock where it is, so Claude
# can follow Inkscape windows you open yourself. GLib (which Inkscape uses) only finds a bus through
# DBUS_SESSION_BUS_ADDRESS on macOS, not through Homebrew's launchd service.
#
#   sh scripts/macos-dbus.sh            set it up (needs: brew install dbus glib)
#   sh scripts/macos-dbus.sh status     check that the bus runs and Inkscape is on it
#   sh scripts/macos-dbus.sh remove     undo the setup

set -eu

LABEL="org.inkscape-claude.dbus"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
ADDRESS="unix:path=/tmp/inkscape-claude-dbus-$(id -u)"
DOMAIN="gui/$(id -u)"

find_tool() {
    for candidate in "$(command -v "$1" 2>/dev/null || true)" "/opt/homebrew/bin/$1" "/usr/local/bin/$1"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            echo "$candidate"
            return
        fi
    done
}

install_agent() {
    daemon="$(find_tool dbus-daemon)"
    if [ -z "$daemon" ]; then
        echo "dbus-daemon was not found. Install it first: brew install dbus glib" >&2
        exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>launchctl setenv DBUS_SESSION_BUS_ADDRESS $ADDRESS; exec '$daemon' --session --nofork --address=$ADDRESS</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENT"

    echo "D-Bus now runs at $ADDRESS and starts again at every login."
    echo "Quit Inkscape (Cmd+Q) and open it again, so it joins the bus."
}

show_status() {
    gdbus="$(find_tool gdbus)"
    if [ -z "$gdbus" ]; then
        echo "gdbus was not found. Install it: brew install glib" >&2
        exit 1
    fi

    names="$(DBUS_SESSION_BUS_ADDRESS="$ADDRESS" "$gdbus" call --session --dest org.freedesktop.DBus \
        --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames 2>&1)" || {
        echo "The bus is not running: $names" >&2
        exit 1
    }

    echo "The bus is running."
    echo "Apps opened from now on get: $(launchctl getenv DBUS_SESSION_BUS_ADDRESS)"
    case "$names" in
        *org.inkscape.Inkscape*) echo "Inkscape is on the bus: Claude can follow its windows." ;;
        *) echo "Inkscape is not on the bus. Quit Inkscape (Cmd+Q) and open it again." ;;
    esac
}

remove_agent() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl unsetenv DBUS_SESSION_BUS_ADDRESS
    rm -f "$AGENT"

    echo "Removed. Inkscape uses the chat's own window again after you reopen it."
}

case "${1:-install}" in
    install) install_agent ;;
    status) show_status ;;
    remove) remove_agent ;;
    *) echo "Usage: sh $0 [install|status|remove]" >&2; exit 2 ;;
esac
