"""Tests for the atomic write behind state.json / project.json / source.json.

The server writes a project's state.json from several threads of ONE process: the job
worker (via run_in_threadpool) and every request thread that reads or edits a chapter.
The write is supposed to be atomic — serialize to a temp file, then os.replace — so a
concurrent reader only ever sees the old file or the new one.

In Night Reader that guarantee was defeated by the temp file's NAME. It was
``state.json.<pid>.tmp``: one name per process, shared by every thread in it. Two
saves at once picked the same path, so thread A was still writing the temp file when
thread B tried to os.replace it, and Windows refuses to move a file another handle has
open:

    PermissionError: [WinError 32] The process cannot access the file because it is
    being used by another process: 'state.json.12500.tmp' -> 'state.json'

The temp file is also the only copy of the new content at that instant, so the same
interleaving could truncate it instead of crashing.

Naming the temp file uniquely per CALL fixes both. These tests hammer the real write
path from many threads at once; they fail against a shared-name implementation.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from morning.atomic import atomic_write_json, atomic_write_text, quarantine_unreadable
from morning.state import State

# Every save fsyncs, and on Windows that is a real disk flush costing tens of
# milliseconds. The collision these tests exist for shows up within a round or two
# against a shared temp name, so a handful of rounds is as diagnostic as fifty and
# keeps the suite from taking minutes.
THREADS = 6
ROUNDS = 4


def _run_concurrently(fn, threads: int = THREADS) -> list[BaseException]:
    """Run fn(i) on `threads` threads released together, collecting what they raise.

    The barrier matters: without it the threads trickle in and the writes serialize by
    accident, so the race the test exists for never happens.
    """
    barrier = threading.Barrier(threads)
    raised: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            fn(i)
        except BaseException as exc:  # noqa: BLE001 — the assertion is "nothing raised"
            with lock:
                raised.append(exc)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return raised


def test_concurrent_saves_never_collide(tmp_path):
    """The reported crash: N threads saving one state.json at the same moment."""
    path = tmp_path / "state.json"
    State().save(path)

    for _ in range(ROUNDS):
        def save(i: int) -> None:
            state = State.load(path)
            state.update(i, status="prepared")
            state.save(path)

        raised = _run_concurrently(save)
        assert not raised, f"a concurrent save raised: {raised[0]!r}"

        # Whatever the interleaving, the file left behind must be parseable JSON —
        # never the half-written temp content.
        json.loads(path.read_text(encoding="utf-8"))


def test_no_temp_files_are_left_behind(tmp_path):
    """A finished write leaves the directory clean — no orphaned .tmp litter."""
    path = tmp_path / "state.json"

    def save(i: int) -> None:
        State.load(path).save(path)

    for _ in range(ROUNDS):
        assert not _run_concurrently(save)

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_replace_cleans_up_its_temp_file(tmp_path, monkeypatch):
    """A write that fails must propagate AND not leave a temp file behind."""
    path = tmp_path / "state.json"

    def boom(src, dst):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(PermissionError):
        atomic_write_text(path, "never lands")

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_locked_destination_is_ridden_out(tmp_path, monkeypatch):
    """A momentary sharing violation retries instead of crashing.

    On Windows an antivirus or a sync client holding the target open for a few
    milliseconds is entirely ordinary. Failing the save would turn that into a lost
    chapter.
    """
    path = tmp_path / "state.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    atomic_write_text(path, "landed")

    assert path.read_text(encoding="utf-8") == "landed"
    assert calls["n"] == 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_the_target_never_contains_partial_content(tmp_path):
    """A reader racing a writer sees the old content or the new one, never a splice.

    The reader pauses briefly between reads on purpose. On Windows, Python's ``open``
    does not pass FILE_SHARE_DELETE, so any open handle blocks ``os.replace`` — an
    unbroken tight-loop reader starves the writer until the retry budget runs out,
    which is a different failure from the one this test is about and is pinned
    separately below. Real readers here are short ``read_text`` calls, which is what
    this models.
    """
    path = tmp_path / "state.json"
    small = json.dumps({"chapters": {"1": {"status": "pending"}}})
    large = json.dumps({"chapters": {str(i): {"status": "prepared"} for i in range(400)}})
    atomic_write_text(path, small)

    stop = threading.Event()
    seen: list[str] = []
    bad: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            (seen if raw in (small, large) else bad).append(raw)
            time.sleep(0.001)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(40):
            atomic_write_text(path, large if i % 2 else small)
    finally:
        stop.set()
        t.join()

    assert not bad, f"a reader observed content that was never written: {bad[0][:120]!r}"
    assert seen, "the reader never managed to read the file at all"


def test_a_permanently_held_destination_gives_up_rather_than_hanging(tmp_path):
    """The retry rides out a MOMENTARY holder, not a permanent one — and says so.

    Worth pinning because the distinction is the whole policy. Ten attempts with a
    capped backoff is roughly a second and a half of patience: enough for an antivirus
    scan or a sync client, not enough to mask something genuinely holding the file.
    Failing loudly there is correct — silently retrying forever would hang the worker
    with no terminal event, which is the outcome the guarded worker exists to prevent.
    """
    path = tmp_path / "state.json"
    atomic_write_text(path, "original")

    def always_locked(src, dst):
        raise PermissionError(32, "being used by another process")

    started = time.monotonic()
    with pytest.raises(PermissionError):
        with_patch = pytest.MonkeyPatch()
        try:
            with_patch.setattr(os, "replace", always_locked)
            atomic_write_text(path, "never lands")
        finally:
            with_patch.undo()
    elapsed = time.monotonic() - started

    assert elapsed < 10, "the retry budget should be seconds, not minutes"
    # The original survives untouched — a failed write never damages what is there.
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob("*.tmp")) == []


def test_json_is_written_unescaped(tmp_path):
    """``ensure_ascii=False`` is not cosmetic: every project here is Japanese, and
    escaping would triple the file size and make it unreadable to a human."""
    path = tmp_path / "source.json"
    atomic_write_json(path, [{"title": "第1話"}])

    raw = path.read_text(encoding="utf-8")
    assert "第1話" in raw
    assert "\\u" not in raw


# ---- quarantine --------------------------------------------------------------

def test_quarantine_preserves_bytes_before_they_are_overwritten(tmp_path):
    """The whole point: a transient read failure must not become permanent loss."""
    path = tmp_path / "state.json"
    path.write_text('{"chapters": {"1": truncated', encoding="utf-8")

    kept = quarantine_unreadable(path)

    assert kept is not None and kept.exists()
    assert kept.read_text(encoding="utf-8") == '{"chapters": {"1": truncated'
    assert kept.name.startswith("state.json.unreadable-")


def test_quarantine_is_idempotent_for_identical_bytes(tmp_path):
    """Repeated reads of the same bad file must not pile up copies."""
    path = tmp_path / "state.json"
    path.write_text("broken", encoding="utf-8")

    first = quarantine_unreadable(path)
    second = quarantine_unreadable(path)

    assert first == second
    assert len(list(tmp_path.glob("*.unreadable-*"))) == 1


def test_quarantine_keeps_a_genuinely_different_corruption(tmp_path):
    """Named by content hash, so two different failures are both kept."""
    path = tmp_path / "state.json"
    path.write_text("broken one", encoding="utf-8")
    quarantine_unreadable(path)
    path.write_text("broken two", encoding="utf-8")
    quarantine_unreadable(path)

    assert len(list(tmp_path.glob("*.unreadable-*"))) == 2


def test_quarantine_ignores_missing_and_empty_files(tmp_path):
    """An empty file holds nothing worth keeping, and a missing one nothing at all."""
    assert quarantine_unreadable(tmp_path / "nope.json") is None

    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert quarantine_unreadable(empty) is None
    assert list(tmp_path.glob("*.unreadable-*")) == []
