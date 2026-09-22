"""One-command launcher for Morning Reader.

Ensures config exists, builds the interface if it is stale, starts the backend (which
also serves the built interface), and opens a browser::

    .venv\\Scripts\\python.exe launch.py        (Windows)
    .venv/bin/python launch.py                  (macOS/Linux)

Two flags worth knowing:

``--reload``
    Restart the server whenever a Python file changes. Night Reader's launcher has no
    such option, and the consequence is a standing trap: a server change does not take
    effect until the whole app is restarted, so verifying against a running instance
    quietly tests the OLD code. Having it here means the default can stay "no reload"
    (which is right for real use — a reload mid-run would kill an in-flight job)
    without that trap being the only option.

``--no-build``
    Skip the interface build. Useful when only the backend changed.

Port 8100, not 8000: Night Reader owns 8000 and the two are meant to run side by side
on this machine. A collision would make whichever started second look broken for
reasons nobody would think to look for.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DIST = WEB / "dist"
HOST = "127.0.0.1"
DEFAULT_PORT = 8100


def url_for(port: int) -> str:
    return f"http://localhost:{port}"

# What the interface is built FROM. If anything here is newer than the build, the
# build is stale.
_BUILD_INPUTS = ("src", "index.html", "package.json", "vite.config.js")


def ensure_config() -> None:
    config, example = ROOT / "config.toml", ROOT / "config.example.toml"
    if not config.exists() and example.exists():
        shutil.copyfile(example, config)
        print("Created config.toml from the example.")


def _newest_mtime(paths) -> float:
    newest = 0.0
    for path in paths:
        try:
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def build_is_stale() -> bool:
    """True when anything the build reads has changed since ``dist/`` was written.

    Checking only whether ``dist/`` EXISTS is the obvious version and it is wrong:
    every run after a source edit then quietly serves the previous interface, and the
    change you are looking for appears not to have worked.
    """
    index = DIST / "index.html"
    if not index.exists():
        return True
    try:
        built_at = index.stat().st_mtime
    except OSError:
        return True

    sources = []
    for name in _BUILD_INPUTS:
        target = WEB / name
        if target.is_dir():
            sources.extend(target.rglob("*"))
        else:
            sources.append(target)
    return _newest_mtime(sources) > built_at


def build_web() -> bool:
    """Build the interface. Returns False if it could not be built."""
    if not (WEB / "node_modules").exists():
        print("Installing the interface's dependencies (first run only)...")
        if subprocess.run(["npm", "install"], cwd=WEB, shell=(os.name == "nt")).returncode:
            return False
    print("Building the interface...")
    return subprocess.run(["npm", "run", "build"], cwd=WEB,
                          shell=(os.name == "nt")).returncode == 0


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def open_browser_when_ready(port: int) -> None:
    """Open the browser once the server answers, not before.

    Opening immediately shows a connection error for the second or two uvicorn takes
    to start, which reads as "the app is broken" on a first run.
    """
    deadline = time.time() + 30
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex((HOST, port)) == 0:
                webbrowser.open(url_for(port))
                return
        time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Morning Reader.")
    parser.add_argument("--reload", action="store_true",
                        help="restart the server when a Python file changes "
                             "(development; a reload kills an in-flight job)")
    parser.add_argument("--no-build", action="store_true",
                        help="skip building the interface")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    port = args.port
    url = url_for(port)

    ensure_config()

    if not args.no_build and build_is_stale():
        if not build_web():
            print("\nThe interface could not be built. The API will still run, but "
                  "there will be no pages to look at.\n")

    if not port_is_free(port):
        print(f"Port {port} is already in use — Morning Reader may already be running.\n"
              f"Open {url}, or start this with --port to use another one.")
        return 1

    if not args.no_browser:
        threading.Thread(target=open_browser_when_ready, args=(port,),
                         daemon=True).start()

    print(f"\nMorning Reader is at {url}   (Ctrl-C to stop)\n")

    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=HOST,
        port=port,
        # Quiet by default: the job console writes to this terminal, and uvicorn's
        # per-request lines would bury it.
        log_level="warning",
        reload=args.reload,
        reload_dirs=[str(ROOT / "server"), str(ROOT / "morning")] if args.reload else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
