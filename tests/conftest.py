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
import sys

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
def clean_jobs():
    """No job may survive a test."""
    jobs_mod.reset_for_tests()
    yield
    jobs_mod.reset_for_tests()


@pytest.fixture(autouse=True)
def quiet_console(monkeypatch):
    """Silence the terminal renderer.

    It is exercised directly by its own test; letting every job test paint progress
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
