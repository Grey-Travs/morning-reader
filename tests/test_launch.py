"""Tests for the launcher.

The launcher is the one file nothing else imports, so nothing else catches a mistake
in it — and a mistake there is total: the app does not start at all. It shipped with
exactly that (``PORT`` read before its ``global`` declaration, a SyntaxError), which
is why these exist.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import launch  # noqa: E402


def test_the_launcher_imports():
    """The cheapest possible guard against a syntax error in the one entry point."""
    assert callable(launch.main)


def test_it_defaults_to_a_port_night_reader_does_not_own():
    """The two apps are meant to run side by side on this machine. A collision would
    make whichever started second look broken for reasons nobody would look for."""
    assert launch.DEFAULT_PORT != 8000
    assert launch.url_for(launch.DEFAULT_PORT) == f"http://localhost:{launch.DEFAULT_PORT}"


def test_a_busy_port_is_reported_rather_than_crashing(capsys, monkeypatch):
    """Running it twice should say so, not raise. This is also what would have caught
    the NameError: the branch is only reached when the port is taken."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((launch.HOST, 0))
        held.listen(1)
        busy_port = held.getsockname()[1]

        monkeypatch.setattr(sys, "argv",
                            ["launch.py", "--no-build", "--no-browser",
                             "--port", str(busy_port)])
        monkeypatch.setattr(launch, "ensure_config", lambda: None)

        assert launch.main() == 1

    printed = capsys.readouterr().out
    assert str(busy_port) in printed
    assert "already in use" in printed


def test_a_free_port_is_recognised():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((launch.HOST, 0))
        taken = probe.getsockname()[1]
        assert launch.port_is_free(taken) is False

    # Once released it is free again. (A small race in principle; in practice nothing
    # else on this machine is racing for an ephemeral port mid-test.)
    assert launch.port_is_free(taken) is True


def test_the_build_is_stale_when_a_source_file_is_newer(tmp_path, monkeypatch):
    """Checking only whether ``dist/`` EXISTS is the obvious version and it is wrong:
    every run after a source edit then quietly serves the previous interface."""
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "dist").mkdir()
    (web / "index.html").write_text("<html></html>", encoding="utf-8")
    (web / "package.json").write_text("{}", encoding="utf-8")
    (web / "vite.config.js").write_text("", encoding="utf-8")
    built = web / "dist" / "index.html"
    built.write_text("built", encoding="utf-8")

    monkeypatch.setattr(launch, "WEB", web)
    monkeypatch.setattr(launch, "DIST", web / "dist")

    import os
    import time

    # The build is older than nothing yet -> fresh.
    old = time.time() - 100
    for path in (web / "index.html", web / "package.json", web / "vite.config.js"):
        os.utime(path, (old, old))
    os.utime(built, (time.time(), time.time()))
    assert launch.build_is_stale() is False

    # Touch a source file so it is newer than the build -> stale.
    source = web / "src" / "App.jsx"
    source.write_text("export default function App() {}", encoding="utf-8")
    newer = time.time() + 10
    os.utime(source, (newer, newer))
    assert launch.build_is_stale() is True


def test_a_missing_build_is_stale(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    monkeypatch.setattr(launch, "WEB", web)
    monkeypatch.setattr(launch, "DIST", web / "dist")

    assert launch.build_is_stale() is True


def test_ensure_config_copies_the_example_once(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / "config.example.toml").write_text("[anthropic]\n", encoding="utf-8")

    launch.ensure_config()
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == "[anthropic]\n"

    # It must not clobber the user's edited config on the next run.
    (tmp_path / "config.toml").write_text("[anthropic]\nmodel = 'mine'\n",
                                          encoding="utf-8")
    launch.ensure_config()
    assert "mine" in (tmp_path / "config.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("flag", ["--reload", "--no-build", "--no-browser"])
def test_the_documented_flags_are_accepted(flag, monkeypatch):
    """A flag mentioned in the docstring but not wired up is worse than none."""
    monkeypatch.setattr(sys, "argv", ["launch.py", flag, "--port", "0"])
    monkeypatch.setattr(launch, "ensure_config", lambda: None)
    monkeypatch.setattr(launch, "port_is_free", lambda port: False)

    assert launch.main() == 1  # stops at the busy-port check, having parsed the flag
