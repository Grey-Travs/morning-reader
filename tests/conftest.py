"""Shared fixtures.

Two things every test in this suite needs, and both are safety rather than
convenience:

* **The projects directory is redirected into tmp_path.** Without this a test run
  writes into the real library. The redirect is applied by default to every test, not
  opted into, because the one test that forgets is the one that deletes real work.
* **Job state is reset between tests.** There is one job registry per process, so a
  job left behind by one test is found by the next — which shows up as a test that
  passes alone and fails in the suite.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import jobs as jobs_mod  # noqa: E402
from server import projects as pj  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_projects(tmp_path, monkeypatch):
    """Point project storage at a scratch directory for the whole test."""
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pj, "PROJECTS_DIR", root)
    yield root


@pytest.fixture(autouse=True)
def isolated_error_log(tmp_path, monkeypatch):
    """Point the traceback log at a scratch directory too.

    Every test that exercises a failure path wrote its traceback into the repo's own
    `logs/errors.log` — a quarter of a megabyte of test noise mixed into the file where
    a real failure of the running app is looked for. The repo guard below cannot see
    it: the folder already exists, so nothing NEW appears at the top level.
    """
    from server import errors

    monkeypatch.setattr(errors, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(errors, "LOG_FILE", tmp_path / "logs" / "errors.log")


@pytest.fixture(autouse=True)
def clean_jobs():
    """No job may survive a test."""
    jobs_mod.reset_for_tests()
    yield
    jobs_mod.reset_for_tests()


@pytest.fixture(autouse=True)
def quiet_console(monkeypatch):
    """Silence the terminal renderer.

    It is exercised directly in tests/test_term.py; letting every job test paint progress
    bars makes pytest output unreadable and, on a cp1252 Windows console, can itself
    raise mid-assert.
    """
    from server import console

    monkeypatch.setattr(console, "_write", lambda text: None)


# ---- sample source -----------------------------------------------------------
# Invented Japanese prose, written for these tests. Short, ordinary sentences that
# exercise what the code actually keys on: Kanji/Kana mixture, 「」 dialogue, a
# full-width space in a heading, and a chapter that is deliberately already English.

SAMPLE_JA = """第1話　朝の駅

電車はまだ来ない。彼女はホームの端に立っていた。

「遅いね」と彼は言った。

第2話　雨の午後

雨が降り始めた。僕は傘を持っていなかった。

「入る？」と彼女が訊いた。僕は頷いた。

第3話　Already in English

The third chapter is already translated, so nothing should be spent on it again.

It is here to prove the classifier keeps its hands off work that is done.
"""


@pytest.fixture
def sample_source() -> str:
    return SAMPLE_JA


# ---- nothing writes into the repo --------------------------------------------
# Twice in one session a test wrote real output into the source tree: a bare
# ``Config()`` has RELATIVE path defaults — "chapters", "audit", "state.json",
# "glossary_pending.json" — so it resolves against the working directory, which during
# a test run is the repo. `process_chapter` genuinely writes chapters and audit copies,
# and they were committed before anyone noticed.
#
# `isolated_projects` above already redirects the PROJECT library. This covers the
# other half: anything a default Config would drop in the repo root. Cheap enough to
# run per test, and it names the test that did it rather than leaving a mystery folder.

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Churn that is nobody's fault and belongs there.
_EXPECTED = {".git", ".pytest_cache", "__pycache__", ".venv", "node_modules",
             ".ruff_cache", ".mypy_cache"}


def _repo_entries() -> set[str]:
    try:
        return {e for e in os.listdir(_REPO_ROOT) if e not in _EXPECTED}
    except OSError:
        return set()


@pytest.fixture(autouse=True)
def no_writes_into_the_repo():
    """Fail the test that drops a file into the source tree, not a later one."""
    before = _repo_entries()
    yield
    created = _repo_entries() - before
    if created:
        for name in created:
            target = _REPO_ROOT / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        raise AssertionError(
            f"this test wrote into the repo: {sorted(created)}. A bare Config() has "
            f"relative paths, so point cfg.paths at tmp_path (see tests/test_spend.py)."
        )


# Starlette's TestClient uses http://testserver as its base URL, so every request it
# makes carries `Host: testserver`. The production allow-list has no business knowing
# about test infrastructure, so the concession lives here instead.
from server import app as _app_mod  # noqa: E402

_app_mod.ALLOWED_HOSTS.add("testserver")
