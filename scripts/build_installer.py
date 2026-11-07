"""Build the installer for this system into dist/.

    python scripts/build_installer.py [--version 1.2.3]

Windows: an Inno Setup .exe. macOS: a .pkg. Linux: a self-extracting .run.
Each one installs the Inkscape extension (Ask Claude, Open Claude Chat) into the user's
extensions folder and a standalone inkscape-mcp executable, built with PyInstaller.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_extension import files as extension_files

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
BUILD = ROOT / "build" / "installer"
DIST = ROOT / "dist"
APP_NAME = "inkscape-claude"


class Release:
    """The version and platform names that go into an installer's file name."""

    def __init__(self, version: str | None = None):
        self.version = (version or self.project_version()).removeprefix("v")
        self.system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
        self.arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())

    @staticmethod
    def project_version() -> str:
        with open(ROOT / "mcp_server" / "pyproject.toml", "rb") as handle:
            return tomllib.load(handle)["project"]["version"]

    def file_name(self, suffix: str) -> str:
        return f"{APP_NAME}-{self.version}-{self.system}-{self.arch}{suffix}"


class Stage:
    """The files every installer ships: extension/claude/ and bin/inkscape-mcp."""

    def __init__(self, root: Path = BUILD / "stage"):
        self.root = root
        self.extension = root / "extension"
        self.bin = root / "bin"

    @property
    def server(self) -> Path:
        return self.bin / ("inkscape-mcp.exe" if sys.platform == "win32" else "inkscape-mcp")

    def build(self) -> Stage:
        shutil.rmtree(self.root, ignore_errors=True)
        self.copy_extension()
        self.freeze_server()

        return self

    def copy_extension(self) -> None:
        for source, name in extension_files():
            target = self.extension / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def freeze_server(self) -> None:
        work = BUILD / "pyinstaller"
        command = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean",
                   "--name", "inkscape-mcp", "--distpath", str(self.bin), "--workpath", str(work),
                   "--specpath", str(work),
                   str(INSTALLER / "inkscape_mcp_entry.py")]
        subprocess.run(command, check=True)


class ServerCheck:
    """Starts the frozen server and makes sure it answers MCP initialize and tools/list."""

    timeout = 60

    def __init__(self, server: Path):
        self.server = server

    def run(self) -> int:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "installer-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        stdin = "".join(json.dumps(request) + "\n" for request in requests)
        env = {**os.environ, "INKSCAPE_MCP_WORKSPACE": str(BUILD / "check-workspace")}

        process = subprocess.Popen([str(self.server)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                   text=True)
        timer = threading.Timer(self.timeout, process.kill)
        timer.start()

        process.stdin.write(stdin)
        process.stdin.flush()

        tools = self.read_tools(process)
        timer.cancel()
        process.kill()
        process.wait()

        if not tools:
            raise SystemExit(f"{self.server.name} did not list any tools")

        print(f"{self.server.name} answered with {len(tools)} tools")
        return len(tools)

    @staticmethod
    def read_tools(process: subprocess.Popen) -> list:
        for line in process.stdout:
            message = json.loads(line)
            if message.get("id") == 2:
                return message.get("result", {}).get("tools", [])

        return []


class Packager:
    """Turns a Stage into one installer file in dist/."""

    suffix = ""

    def __init__(self, release: Release, stage: Stage):
        self.release = release
        self.stage = stage
        self.output = DIST / release.file_name(self.suffix)

    @staticmethod
    def for_system(release: Release, stage: Stage) -> Packager:
        packagers = {"windows": WindowsPackager, "macos": MacPackager, "linux": LinuxPackager}
        return packagers[release.system](release, stage)

    def build(self) -> Path:
        raise NotImplementedError


class WindowsPackager(Packager):
    """An Inno Setup installer that needs no administrator rights."""

    suffix = ".exe"

    def build(self) -> Path:
        DIST.mkdir(exist_ok=True)
        command = [self.compiler(), f"/DAppVersion={self.release.version}", f"/DStage={self.stage.root}",
                   f"/DOutputDir={DIST}", f"/DOutputName={self.output.stem}",
                   str(INSTALLER / "windows" / "inkscape-claude.iss")]
        subprocess.run(command, check=True)

        return self.output

    @staticmethod
    def compiler() -> str:
        found = shutil.which("iscc")
        if found:
            return found

        for folder in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                       os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            candidate = Path(folder or "") / "Inno Setup 6" / "ISCC.exe"
            if candidate.exists():
                return str(candidate)

        raise SystemExit("Inno Setup 6 (ISCC.exe) was not found; install it from https://jrsoftware.org/isinfo.php")


class MacPackager(Packager):
    """A flat .pkg that puts the files in /Library/Application Support and the extension in the user's Inkscape."""

    suffix = ".pkg"
    identifier = "org.inkscape-claude.installer"
    payload_folder = Path("Library/Application Support/Inkscape Claude")

    def build(self) -> Path:
        root = BUILD / "pkgroot"
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(self.stage.root, root / self.payload_folder)
        shutil.copy2(INSTALLER / "macos" / "uninstall.sh", root / self.payload_folder / "uninstall.sh")

        scripts = INSTALLER / "macos" / "scripts"
        for script in scripts.iterdir():
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        DIST.mkdir(exist_ok=True)
        command = ["pkgbuild", "--root", str(root), "--scripts", str(scripts), "--identifier", self.identifier,
                   "--version", self.release.version, "--install-location", "/", str(self.output)]
        subprocess.run(command, check=True)

        return self.output


class LinuxPackager(Packager):
    """A shell script with the staged files appended as a tar.gz; running it installs for the current user."""

    suffix = ".run"
    marker = "__PAYLOAD_BELOW__"

    def build(self) -> Path:
        header = (INSTALLER / "linux" / "install.sh").read_text().replace("@VERSION@", self.release.version)
        if not header.endswith(self.marker + "\n"):
            raise SystemExit(f"installer/linux/install.sh must end with a {self.marker} line")

        DIST.mkdir(exist_ok=True)
        with open(self.output, "wb") as handle:
            handle.write(header.encode())
            handle.write(self.payload())

        self.output.chmod(0o755)
        return self.output

    def payload(self) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            archive.add(self.stage.root, arcname=".")

        return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", help="version to stamp on the installer (default: the pyproject version)")
    args = parser.parse_args()

    release = Release(args.version)
    stage = Stage().build()
    ServerCheck(stage.server).run()

    output = Packager.for_system(release, stage).build()
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
