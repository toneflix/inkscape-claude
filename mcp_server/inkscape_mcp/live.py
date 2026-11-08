"""Live control of a running Inkscape 1.x window through its D-Bus actions.

Inkscape publishes its actions on the session bus wherever GLib finds one: always on Linux, on Windows through
the gdbus.exe that ships with Inkscape, and on macOS once scripts/macos-dbus.sh runs a bus and sets
DBUS_SESSION_BUS_ADDRESS for apps (GLib does not use Homebrew's launchd bus)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .cli import Inkscape

BUS_NAME = "org.inkscape.Inkscape"
APP_PATH = "/org/inkscape/Inkscape"
ACTIONS_INTERFACE = "org.gtk.Actions"
TARGETS = ("app", "window", "document")
MACOS_BUS = "/tmp/inkscape-claude-dbus-{uid}"


class LiveError(RuntimeError):
    pass


def variant(signature: str, value: str | None) -> str:
    """Render an action parameter as GVariant text for gdbus."""
    if not signature:
        return "[]"

    text = value or ""

    if signature == "s":
        escaped = text.replace("\\", "\\\\").replace("'", "\\'")
        return f"[<'{escaped}'>]"

    if signature == "b":
        enabled = value is None or text.lower() in ("1", "true", "yes", "on")
        return f"[<{'true' if enabled else 'false'}>]"

    if signature == "i":
        return f"[<int32 {int(text or 0)}>]"

    if signature == "d":
        return f"[<double {float(text or 0)}>]"

    raise LiveError(f"Actions with parameter type '{signature}' are not supported.")


class LiveInkscape:
    """Sends actions to an Inkscape window that is already open, over the session D-Bus."""

    def __init__(self, inkscape: Inkscape, gdbus: str | None = None, timeout: float = 30):
        self.inkscape = inkscape
        self.gdbus = gdbus or self.find_gdbus(inkscape)
        self.timeout = timeout
        self._actions: dict[str, set[str]] = {}

    @staticmethod
    def find_gdbus(inkscape: Inkscape) -> str | None:
        """The gdbus command on PATH, next to Inkscape or its Python (Windows ships it there), or from Homebrew."""
        found = shutil.which("gdbus")
        if found:
            return found

        folders = [Path(sys.executable).parent]
        try:
            folders.insert(0, Path(inkscape.binary).resolve().parent)
        except (OSError, RuntimeError):
            pass
        places = [folder / name for folder in folders for name in ("gdbus.exe", "gdbus")]
        places += [Path("/opt/homebrew/bin/gdbus"), Path("/usr/local/bin/gdbus")]

        return next((str(place) for place in places if place.is_file()), None)

    def available(self) -> bool:
        if not self.gdbus:
            return False

        try:
            self._call("org.freedesktop.DBus.Introspectable.Introspect", APP_PATH)
        except LiveError:
            return False

        return True

    def require(self) -> None:
        if not self.gdbus:
            raise LiveError("The 'gdbus' command was not found. On Linux install your distribution's glib2 tools; "
                            "on macOS run 'brew install glib dbus' and scripts/macos-dbus.sh. Or set "
                            "INKSCAPE_MCP_LIVE=shell.")
        if not self.available():
            raise LiveError("No running Inkscape was found on the session D-Bus. Open Inkscape first, "
                            "or use live_open.")

    def windows(self) -> list[int]:
        return self._children(f"{APP_PATH}/window")

    def documents(self) -> list[int]:
        return self._children(f"{APP_PATH}/document")

    def activate(self, action: str, parameter: str | None = None, target: str | None = None,
                 number: int | None = None) -> str:
        """Run one action. Without a target, the app, window and document action groups are tried in turn."""
        self.require()

        path = self._find_path(action, target, number)
        signature = self._signature(action, path)
        self._call(f"{ACTIONS_INTERFACE}.Activate", path, action, variant(signature, parameter), "{}")

        return path

    def run(self, actions: list[str], window: int | None = None) -> list[str]:
        """Run actions written like the command line (name or name:value) on the open document."""
        done = []

        for step in Inkscape.normalize_actions(actions):
            name, _, value = step.partition(":")
            self.activate(name.strip(), value if ":" in step else None, number=window)
            done.append(step)

        return done

    def export(self, output: Path, export_type: str = "svg", width: int | None = None) -> Path:
        """Export the document in the active window and wait for the file to appear."""
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)

        steps = [f"export-filename:{output}", f"export-type:{export_type}", "export-overwrite"]
        if export_type == "png":
            steps += ["export-area-page", "export-background:#ffffff", "export-background-opacity:1"]
            if width:
                steps.append(f"export-width:{int(width)}")
        steps.append("export-do")

        self.run(steps)
        self._wait_for(output)

        return output

    def save(self, window: int | None = None, path: Path | None = None) -> None:
        """File > Save. Inkscape publishes window actions only on Linux, so elsewhere the open document is
        exported as Inkscape SVG over its file at `path`."""
        if self.windows():
            self.activate("document-save", target="window", number=window)
            return

        if path is None:
            raise LiveError("This Inkscape does not offer File > Save over D-Bus. Save in Inkscape, or use "
                            "live_pull to copy the document to a file.")

        self.export(path, "svg")

    def saves_in_place(self) -> bool:
        """Whether save() is File > Save, which writes to the file the window has open, wherever that is."""
        return bool(self.windows())

    def moved(self, window: int | None, path: Path) -> None:
        """Nothing to do: Inkscape itself knows which file a window has open."""

    def reload(self, window: int | None = None) -> None:
        """File > Revert, or reloading the file into the same document where window actions are missing."""
        if self.windows():
            self.activate("document-revert", target="window", number=window)
            return

        self.run(["select-clear", "file-rebase:true"])

    def close_window(self, window: int | None) -> None:
        """Closes a window's document. Only possible where Inkscape publishes window actions (Linux)."""
        if not window or window not in self.windows():
            raise LiveError("This Inkscape cannot close a window over D-Bus.")

        self.activate("document-close", target="window", number=window)

    def open(self, path: Path) -> int | None:
        """Open a file in the running Inkscape, or start Inkscape with it. Returns the new window number."""
        controllable = bool(self.gdbus)
        before = set(self.windows()) if controllable and self.available() else set()
        subprocess.Popen([self.inkscape.binary, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        if not controllable:
            return None

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.available():
                new = set(self.windows()) - before
                if new:
                    return max(new)
            time.sleep(0.5)

        return None

    def _find_path(self, action: str, target: str | None, number: int | None) -> str:
        if target and target not in TARGETS:
            raise LiveError(f"Unknown target '{target}'. Use one of: {', '.join(TARGETS)}.")

        for kind in ([target] if target else TARGETS):
            try:
                path = self._object_path(kind, number)
            except LiveError:
                if target:
                    raise
                continue

            if action in self._action_names(path):
                return path

        raise LiveError(f"Inkscape has no action named '{action}'. Use list_actions to find names.")

    def _object_path(self, kind: str, number: int | None) -> str:
        if kind == "app":
            return APP_PATH

        existing = self.windows() if kind == "window" else self.documents()
        if not existing:
            raise LiveError(f"Inkscape has no open {kind}.")
        if number is not None and number not in existing:
            raise LiveError(f"There is no {kind} {number}. Open ones: {existing}.")

        return f"{APP_PATH}/{kind}/{number if number is not None else max(existing)}"

    def _action_names(self, path: str) -> set[str]:
        if path != APP_PATH or path not in self._actions:
            output = self._call(f"{ACTIONS_INTERFACE}.List", path)
            self._actions[path] = set(re.findall(r"'([^']+)'", output))

        return self._actions[path]

    def _signature(self, action: str, path: str) -> str:
        output = self._call(f"{ACTIONS_INTERFACE}.Describe", path, action)
        match = re.search(r"signature '([^']*)'", output)

        return match.group(1) if match else ""

    def _children(self, path: str) -> list[int]:
        try:
            output = self._call("org.freedesktop.DBus.Introspectable.Introspect", path)
        except LiveError:
            return []

        return sorted(int(n) for n in re.findall(r'<node name="(\d+)"', output))

    def _wait_for(self, output: Path) -> None:
        deadline = time.monotonic() + self.timeout
        last_size = -1

        while time.monotonic() < deadline:
            if output.exists():
                size = output.stat().st_size
                if size and size == last_size:
                    return
                last_size = size
            time.sleep(0.3)

        raise LiveError(f"Inkscape did not export {output} in time.")

    @staticmethod
    def environment() -> dict[str, str] | None:
        """On macOS, points gdbus at the bus from scripts/macos-dbus.sh when this process started without its
        address, e.g. an MCP server started before the bus."""
        if sys.platform != "darwin" or os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return None

        socket = Path(MACOS_BUS.format(uid=os.getuid()))
        if not socket.exists():
            return None

        return {**os.environ, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={socket}"}

    def _call(self, method: str, path: str, *args: str) -> str:
        if not self.gdbus:
            raise LiveError("The 'gdbus' command was not found.")

        command = [self.gdbus, "call", "--session", "--dest", BUS_NAME, "--object-path", path,
                   "--method", method, *args]

        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout,
                                  env=self.environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise LiveError("Inkscape did not answer over D-Bus in time. A dialog may be open in its window.") from exc
        except OSError as exc:
            raise LiveError(str(exc)) from exc

        if proc.returncode != 0:
            raise LiveError(proc.stderr.strip() or f"gdbus exited with code {proc.returncode}.")

        return proc.stdout

