"""Tests for the double-click entry points.

Nothing imports a ``.bat`` file, so nothing else catches a mistake in one — and the
mistakes they attract are silent. A batch file with LF endings runs and mis-parses its
own multi-line blocks; one that points at a script that has been renamed prints a
confusing error to a window that closes. Both are the first thing a person ever sees
of this app.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATCH_FILES = ("start.bat", "setup.bat")


def _raw(name: str) -> bytes:
    return (ROOT / name).read_bytes()


def _text(name: str) -> str:
    return _raw(name).decode("utf-8")


@pytest.mark.parametrize("name", BATCH_FILES)
def test_the_batch_file_exists(name):
    assert (ROOT / name).is_file(), f"{name} is missing"


@pytest.mark.parametrize("name", BATCH_FILES)
def test_every_line_ends_crlf(name):
    """cmd.exe mis-parses multi-line blocks and ``goto :label`` with bare LF, and it
    does so silently — the file runs and does the wrong thing rather than erroring.

    ``.gitattributes`` pins this for anyone who clones the repo; this pins it for the
    copy on disk, which is the one that actually gets double-clicked.
    """
    raw = _raw(name)
    lone_lf = re.findall(rb"(?<!\r)\n", raw)

    assert not lone_lf, f"{name} has {len(lone_lf)} line(s) not ending in CRLF"


@pytest.mark.parametrize("name", BATCH_FILES)
def test_it_runs_from_its_own_folder(name):
    """Double-clicking from Explorer can leave the working directory anywhere, so
    every relative path below it would resolve against the wrong folder."""
    assert 'cd /d "%~dp0"' in _text(name)


@pytest.mark.parametrize("name", BATCH_FILES)
def test_it_is_ascii_only(name):
    """A console codepage is not UTF-8. An em dash or a curly quote in an ``echo``
    line renders as mojibake in the very window that is supposed to explain what to
    do next."""
    raw = _raw(name)
    non_ascii = [b for b in raw if b > 0x7F]

    assert not non_ascii, f"{name} contains non-ASCII bytes: {non_ascii[:8]}"


@pytest.mark.parametrize("name", BATCH_FILES)
def test_it_pauses_so_the_window_does_not_vanish(name):
    """Without a pause, a failure flashes a console window shut and the person is
    left with an app that "did nothing"."""
    assert "pause" in _text(name)


# ---- start.bat ---------------------------------------------------------------

def test_start_launches_the_real_entry_point():
    text = _text("start.bat")

    assert "launch.py" in text
    assert (ROOT / "launch.py").is_file(), "start.bat points at a script that is gone"


def test_start_uses_the_projects_own_interpreter():
    r"""``.venv\Scripts\python.exe`` explicitly, never a bare ``python``.

    Night Reader sits beside this app. A shell with its environment already active
    would satisfy the imports from the WRONG environment, which is how an undeclared
    dependency stays hidden until it reaches a user.
    """
    text = _text("start.bat")

    assert r".venv\Scripts\python.exe" in text
    assert not re.search(r"^\s*python\s+launch\.py", text, re.MULTILINE)


def test_start_explains_itself_when_setup_has_not_been_run():
    """The guard has to name the file it wants, and that file has to exist."""
    text = _text("start.bat")

    assert "setup.bat" in text
    assert (ROOT / "setup.bat").is_file(), "start.bat sends the user to a missing file"
    assert "exit /b 1" in text, "the guard should stop, not fall through to launching"


# ---- setup.bat ---------------------------------------------------------------

def test_setup_installs_both_halves_of_the_app():
    text = _text("setup.bat")

    assert "-m venv .venv" in text
    assert "requirements.txt" in text
    assert "npm install" in text


def test_setup_installs_from_the_declared_requirements():
    assert (ROOT / "requirements.txt").is_file()


def test_setup_reports_a_failure_instead_of_claiming_success():
    """Without the error branch, a failed pip install still prints "Setup complete!"
    and the person goes on to double-click start.bat wondering why it will not run."""
    text = _text("setup.bat")

    assert "goto :err" in text
    # The LABEL, anchored to line-start — a bare `":err" in text` matches the first
    # `goto :err` instead, which is not what the ordering below is about.
    label = re.search(r"^:err\s*$", text, re.MULTILINE)
    assert label, "setup.bat jumps to :err but never defines it"
    assert text.index("exit /b 0") < label.start(), \
        "the success path must exit before falling into the error label"


def test_setup_does_not_promise_a_google_login_this_app_cannot_use_yet():
    """The sibling's setup ends by asking for ``client_secret.json``. Docs ingestion
    is step 2 here, so repeating that instruction would send someone hunting for a
    file nothing reads."""
    assert "client_secret" not in _text("setup.bat")


def test_setup_mentions_the_port_so_the_two_apps_are_not_confused():
    from launch import DEFAULT_PORT

    assert str(DEFAULT_PORT) in _text("setup.bat")
