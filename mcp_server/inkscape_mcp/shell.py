"""Live control on any OS: starts the Inkscape window itself and drives it through its interactive shell."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from itertools import count
from pathlib import Path

from .cli import Inkscape, InkscapeError
from .live import LiveError, LiveInkscape

HEARTBEAT_SECONDS = 0.02


class ShellWindow:
    """One Inkscape window started with --with-gui --shell.

    The shell only lets the window handle its events while it reads input, so a heartbeat of empty lines keeps
    the window responsive. Commands are confirmed by exporting a marker file after them.
    """

    def __init__(self, inkscape: Inkscape, path: Path, timeout: float = 30):
        self.path = path
        self.timeout = timeout
        self._lock = threading.Lock()
        self._markers = count(1)
        self._folder = Path(tempfile.mkdtemp(prefix="inkscape-claude-"))
        self._process = subprocess.Popen([inkscape.console_binary, "--with-gui", "--shell", str(path)],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         text=True, **self._hidden_console())
        threading.Thread(target=self._heartbeat, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def run(self, actions: list[str]) -> None:
        """Runs the actions and waits until Inkscape has done them."""
        marker = self._folder / f"done-{next(self._markers)}.svg"
        steps = [*actions, f"export-filename:{marker}", "export-type:svg", "export-overwrite", "export-do"]

        self.send(steps)
        self.wait_for(marker)
        marker.unlink(missing_ok=True)

    def send(self, actions: list[str]) -> None:
        for action in actions:
            if ";" in action or "\n" in action:
                raise LiveError(f"Action '{action}' contains ';' or a line break.")

        self._write(";".join(actions))

    def wait_for(self, output: Path) -> None:
        deadline = time.monotonic() + self.timeout
        last_size = -1

        while time.monotonic() < deadline:
            if not self.alive:
                raise LiveError("The Inkscape window was closed.")
            if output.exists():
                size = output.stat().st_size
                if size and size == last_size:
                    return
                last_size = size
            time.sleep(0.2)

        raise LiveError("Inkscape did not finish in time. A dialog may be open in its window.")

    def close(self) -> None:
        if self.alive:
            self._write("quit")
            try:
                self._process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _write(self, line: str) -> None:
        with self._lock:
            if not self.alive:
                raise LiveError("The Inkscape window was closed.")
            try:
                self._process.stdin.write(line + "\n")
                self._process.stdin.flush()
            except OSError as exc:
                raise LiveError("The Inkscape window was closed.") from exc

    def _heartbeat(self) -> None:
        while self.alive:
            try:
                self._write("")
            except LiveError:
                return
            time.sleep(HEARTBEAT_SECONDS)

    @staticmethod
    def _hidden_console() -> dict:
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NO_WINDOW}

        return {}


class ShellInkscape:
    """Live control through Inkscape windows that Claude opened itself; works on Linux, macOS and Windows.

    Same methods as LiveInkscape. Saving writes the window's document to its file, and reloading uses the
    file-rebase action (Inkscape 1.3 and later), which reloads the file into the same window.
    """

    def __init__(self, inkscape: Inkscape, timeout: float = 30):
        self.inkscape = inkscape
        self.timeout = timeout
        self._windows: dict[int, ShellWindow] = {}
        self._numbers = count(1)
        self._can_rebase: bool | None = None

    def available(self) -> bool:
        return bool(self.windows())

    def require(self) -> None:
        if not self.available():
            raise LiveError("No Inkscape window opened by Claude is running. Use live_open to open the document; "
                            "on this system Claude can only follow windows it opened itself.")

    def windows(self) -> list[int]:
        return [number for number, window in self._windows.items() if window.alive]

    def documents(self) -> list[int]:
        return self.windows()

    def open(self, path: Path) -> int | None:
        for number, window in self._windows.items():
            if window.alive and window.path == path:
                return number

        try:
            window = ShellWindow(self.inkscape, path, self.timeout)
        except (InkscapeError, OSError) as exc:
            raise LiveError(f"Could not start Inkscape: {exc}") from exc

        number = next(self._numbers)
        self._windows[number] = window
        window.run([])

        return number

    def close_window(self, number: int | None) -> None:
        window = self._windows.pop(number, None)
        if window is not None:
            window.close()

    def run(self, actions: list[str], window: int | None = None) -> list[str]:
        steps = Inkscape.normalize_actions(actions)
        self._window(window).run(steps)

        return steps

    def export(self, output: Path, export_type: str = "svg", width: int | None = None,
               window: int | None = None) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)

        steps = [f"export-filename:{output}", f"export-type:{export_type}", "export-overwrite"]
        if export_type == "png":
            steps += ["export-area-page", "export-background:#ffffff", "export-background-opacity:1",
                      f"export-width:{int(width or 800)}"]
        steps.append("export-do")

        target = self._window(window)
        target.send(steps)
        target.wait_for(output)

        return output

    def save(self, window: int | None = None, path: Path | None = None) -> None:
        target = self._window(window)
        self.export(target.path, "svg", window=window)

    def saves_in_place(self) -> bool:
        return False

    def moved(self, window: int | None, path: Path) -> None:
        """Saves the window to `path` from now on, after it was saved there under a new name."""
        self._window(window).path = path

    def reload(self, window: int | None = None) -> None:
        if not self._rebase_supported():
            raise LiveError("Reloading the window needs Inkscape 1.3 or later. Use File > Revert in Inkscape.")

        self._window(window).run(["select-clear", "file-rebase:true"])

    def close(self) -> None:
        """Saves and closes every window Claude opened."""
        for number in self.windows():
            window = self._windows[number]
            try:
                self.save(number)
            except LiveError:
                pass
            window.close()

    def _window(self, number: int | None) -> ShellWindow:
        self.require()
        open_windows = self.windows()
        if number is not None and number not in open_windows:
            raise LiveError(f"There is no window {number}. Open ones: {open_windows}.")

        return self._windows[number if number is not None else max(open_windows)]

    def _rebase_supported(self) -> bool:
        if self._can_rebase is None:
            try:
                self._can_rebase = any(name == "file-rebase" for name, _ in self.inkscape.action_list())
            except InkscapeError:
                self._can_rebase = False

        return self._can_rebase


def live_control(inkscape: Inkscape, mode: str | None = None) -> LiveInkscape | ShellInkscape:
    """D-Bus, which can follow windows the person opened: on Linux always, and on macOS and Windows when a
    running Inkscape answers on the bus. Otherwise the shell. INKSCAPE_MCP_LIVE=dbus or shell picks one."""
    mode = mode or os.environ.get("INKSCAPE_MCP_LIVE", "")
    dbus = LiveInkscape(inkscape)

    if mode == "dbus":
        return dbus
    if mode != "shell" and dbus.gdbus and (sys.platform.startswith("linux") or dbus.available()):
        return dbus

    return ShellInkscape(inkscape)
