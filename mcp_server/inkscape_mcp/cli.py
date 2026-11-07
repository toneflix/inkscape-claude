"""Inkscape 1.x command line wrapper."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

KNOWN_LOCATIONS = (
    "/Applications/Inkscape.app/Contents/MacOS/inkscape",
    r"C:\Program Files\Inkscape\bin\inkscape.com",
    r"C:\Program Files\Inkscape\bin\inkscape.exe",
)

NOISE = ("GtkRecentManager", "Gtk-", "Unable to init server")

ACTION_SEPARATOR = re.compile(r"\s*;\s*")


class InkscapeError(RuntimeError):
    pass


class Inkscape:
    """Runs Inkscape headless to export, query and apply actions to SVG files."""

    def __init__(self, binary: str | None = None, timeout: float = 120):
        self._binary = binary
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> Inkscape:
        return cls(os.environ.get("INKSCAPE_BIN"), float(os.environ.get("INKSCAPE_TIMEOUT", "120")))

    @property
    def binary(self) -> str:
        if self._binary:
            return self._binary

        for name in ("inkscape", "inkscape.com", "inkscape.exe"):
            found = shutil.which(name)
            if found:
                return found

        for candidate in KNOWN_LOCATIONS:
            if Path(candidate).exists():
                return candidate

        raise InkscapeError("Inkscape was not found. Install Inkscape 1.x or set INKSCAPE_BIN to its path.")

    @property
    def console_binary(self) -> str:
        """On Windows, inkscape.com reads standard input; inkscape.exe does not."""
        binary = Path(self.binary)
        console = binary.with_suffix(".com")

        return str(console) if binary.suffix.lower() == ".exe" and console.exists() else str(binary)

    def run(self, args: list[str]) -> str:
        env = {**os.environ, "SELF_CALL": os.environ.get("SELF_CALL", "1")}

        try:
            proc = subprocess.run([self.binary, *args], capture_output=True, text=True,
                                  timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise InkscapeError(f"Inkscape timed out after {self.timeout:.0f}s.") from exc

        errors = self._clean(proc.stderr)
        if proc.returncode != 0:
            raise InkscapeError(f"Inkscape exited with code {proc.returncode}.\n{errors}".strip())

        return (proc.stdout + ("\n" + errors if errors else "")).strip()

    def version(self) -> str:
        return self.run(["--version"]).splitlines()[0]

    def run_actions(self, svg_path: Path, actions: list[str] | str, output_path: Path | None = None) -> str:
        """Apply actions and save the result to output_path, or back to svg_path."""
        target = output_path or svg_path
        fd, tmp_name = tempfile.mkstemp(suffix=".svg", dir=str(target.parent))
        os.close(fd)
        tmp = Path(tmp_name)

        steps = self.normalize_actions(actions)
        steps += [f"export-filename:{tmp}", "export-overwrite", "export-type:svg", "export-do"]

        try:
            log = self.run([str(svg_path), f"--actions={';'.join(steps)}"])
            if tmp.stat().st_size == 0:
                raise InkscapeError("Inkscape did not write an output file. Check the action names.\n" + log)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

        return log

    def export(self, svg_path: Path, output_path: Path, export_type: str | None = None, dpi: float | None = None,
               width: int | None = None, height: int | None = None, object_id: str | None = None,
               area: str | tuple[float, float, float, float] = "page", background: str | None = None,
               text_to_path: bool = False, plain_svg: bool = False) -> str:
        export_type = (export_type or output_path.suffix.lstrip(".") or "png").lower()
        args = [str(svg_path), f"--export-filename={output_path}", f"--export-type={export_type}",
                "--export-overwrite"]

        if dpi:
            args.append(f"--export-dpi={dpi:g}")
        if width:
            args.append(f"--export-width={int(width)}")
        if height:
            args.append(f"--export-height={int(height)}")

        if object_id:
            args += [f"--export-id={object_id}", "--export-id-only"]
        elif isinstance(area, tuple):
            x, y, box_width, box_height = area
            args.append(f"--export-area={x:g}:{y:g}:{x + box_width:g}:{y + box_height:g}")
        elif area == "drawing":
            args.append("--export-area-drawing")
        else:
            args.append("--export-area-page")

        if background:
            args += [f"--export-background={background}", "--export-background-opacity=1"]
        if text_to_path:
            args.append("--export-text-to-path")
        if plain_svg:
            args.append("--export-plain-svg")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.run(args)
        if not output_path.exists():
            raise InkscapeError(f"Export failed; no file was written.\n{log}")

        return log

    def query_all(self, svg_path: Path) -> dict[str, tuple[float, float, float, float]]:
        boxes = {}

        for line in self.run([str(svg_path), "--query-all"]).splitlines():
            parts = line.strip().split(",")
            if len(parts) != 5:
                continue
            try:
                boxes[parts[0]] = tuple(float(p) for p in parts[1:])
            except ValueError:
                continue

        return boxes

    def action_list(self) -> list[tuple[str, str]]:
        actions = []

        for line in self.run(["--action-list"]).splitlines():
            if ":" not in line:
                continue
            name, description = line.split(":", 1)
            name = name.strip()
            if name and " " not in name:
                actions.append((name, description.strip()))

        return actions

    @staticmethod
    def normalize_actions(actions: list[str] | str) -> list[str]:
        if isinstance(actions, str):
            actions = ACTION_SEPARATOR.split(actions)

        steps = [action.strip() for action in actions if action and action.strip()]
        for step in steps:
            if ";" in step:
                raise InkscapeError(f"Action '{step}' contains ';'. Pass actions as separate list items.")

        return steps

    @staticmethod
    def _clean(stderr: str) -> str:
        return "\n".join(line for line in stderr.splitlines()
                         if line.strip() and not any(noise in line for noise in NOISE))
