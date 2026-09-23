"""A file that cannot be READ is not a file that is EMPTY.

Every JSON loader here degraded an unreadable file to an empty one so that a single
corrupt file could not take down the whole library. That is right for a file that is
genuinely corrupt. It was catastrophic for one that was merely busy, because the caller
then mutates the empty value and saves it straight back:

  - state.json    -> every chapter's status, usage and cost for a novel
  - pages.json    -> every page transcribed from a photograph, with its regions
  - glossary.json -> every locked name and term

and on Windows a momentary failure is entirely ordinary — an antivirus or a file-sync
client holding the file open for an instant is enough. The write side already paid to
survive exactly that (``atomic._replace_with_retry``, 10 attempts). The read side gave
up on the first try.

Worse, the mitigation that was supposed to make it survivable could not fire: the
loaders called ``quarantine_unreadable``, which re-read the SAME path and hit the SAME
error, so it preserved nothing in precisely the case it existed for.

So the two failures are now separate. A PARSE error degrades and quarantines, as
before. A READ error retries and then RAISES — and because ``mutate_pages`` and
``mutate_state`` never reach their save when the body raises, the file on disk is left
exactly as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from morning import atomic
from morning.glossary import Glossary, GlossaryEntry, load_pending, save_pending
from morning.state import State
from server import jobs, pages as pages_mod, projects as pj

_REAL_READ_TEXT = Path.read_text
_REAL_READ_BYTES = Path.read_bytes


@pytest.fixture
def busy(monkeypatch):
    """Make one named file raise PermissionError, as a sharing violation does.

    Returns a setter so a test can arm it for a specific path, and a counter so a test
    can let the file become readable again after N attempts.
    """
    state = {"target": None, "fail_times": 10 ** 6, "attempts": 0}

    def guarded_text(self, *args, **kwargs):
        if state["target"] and Path(self) == state["target"]:
            state["attempts"] += 1
            if state["attempts"] <= state["fail_times"]:
                raise PermissionError(13, "The process cannot access the file")
        return _REAL_READ_TEXT(self, *args, **kwargs)

    def guarded_bytes(self, *args, **kwargs):
        if state["target"] and Path(self) == state["target"]:
            state["attempts"] += 1
            if state["attempts"] <= state["fail_times"]:
                raise PermissionError(13, "The process cannot access the file")
        return _REAL_READ_BYTES(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_bytes)
    # Keep the always-failing tests quick; the real policy is 10 attempts.
    monkeypatch.setattr(atomic, "_ATTEMPTS", 3)
    monkeypatch.setattr(atomic, "_BACKOFF", 0.001)
    monkeypatch.setattr(atomic, "_BACKOFF_MAX", 0.002)
    return state


# ---- the page manifest --------------------------------------------------------

@pytest.fixture
def manifest(tmp_path):
    """A project whose manifest is worth real money: pages, regions and a cost."""
    pid = pj.create_project("scans", ingest=pj.INGEST_IMAGES)["id"]
    with pages_mod.mutate_pages(pid) as doc:
        doc["pages"] = [
            {"id": f"{i:08x}", "seq": i, "name": f"p{i}.jpg", "status": "ok",
             "width": 1600, "height": 2400, "file": f"page-{i:04d}.jpg",
             "read": {"width": 1600, "height": 2400,
                      "regions": [{"id": "r0", "box": [0.1, 0.1, 0.2, 0.2],
                                   "text": "本文", "kind": "body", "order": 0}],
                      "order_source": "model", "meta": {"confidence": "high"}}}
            for i in range(1, 13)]
        doc["totals"] = {"cost_usd": 138.42}
    return pid


def test_a_busy_manifest_is_not_mistaken_for_an_empty_one(manifest, busy):
    path = pages_mod.pages_file(manifest)
    before = path.read_bytes()
    busy["target"] = path

    with pytest.raises(PermissionError):
        pages_mod.load_pages(manifest)

    busy["target"] = None          # stop simulating, so we can look at the file
    assert path.read_bytes() == before


def test_a_busy_manifest_survives_a_mutation_attempt(manifest, busy):
    """The destructive path in full: load -> mutate -> save. The load must fail before
    the body runs, so the save never happens and twelve pages of paid-for work stay
    on disk."""
    path = pages_mod.pages_file(manifest)
    before = path.read_bytes()
    busy["target"] = path

    with pytest.raises(PermissionError):
        with pages_mod.mutate_pages(manifest) as doc:
            doc["pages"][0]["status"] = "edited"

    busy["target"] = None
    assert path.read_bytes() == before
    doc = pages_mod.load_pages(manifest)
    assert len(doc["pages"]) == 12
    assert doc["totals"]["cost_usd"] == 138.42


def test_a_momentary_lock_is_ridden_out(manifest, busy):
    """The ordinary case: an antivirus holds the file for an instant and lets go. The
    write side already survives this; the read side now does too."""
    busy["target"] = pages_mod.pages_file(manifest)
    busy["fail_times"] = 2          # fails twice, then succeeds

    doc = pages_mod.load_pages(manifest)

    assert len(doc["pages"]) == 12
    assert busy["attempts"] == 3


def test_a_genuinely_corrupt_manifest_still_degrades_and_is_kept(manifest):
    """The behaviour that was always right, and must not be lost: a truncated file
    (the process was killed mid-write) reads as empty rather than taking the project
    down — and its bytes are preserved first."""
    path = pages_mod.pages_file(manifest)
    path.write_text('{"pages": [{"id": "abc"', encoding="utf-8")

    doc = pages_mod.load_pages(manifest)

    assert doc["pages"] == []
    kept = list(path.parent.glob("pages.json.unreadable-*"))
    assert kept, "the corrupt bytes were not preserved"
    assert "abc" in kept[0].read_text(encoding="utf-8")


def test_an_empty_manifest_file_is_not_corrupt(manifest):
    """A zero-length file is what an interrupted write leaves behind. It parses as
    nothing rather than raising, and there are no bytes worth quarantining."""
    pages_mod.pages_file(manifest).write_text("", encoding="utf-8")

    assert pages_mod.load_pages(manifest)["pages"] == []


# ---- state.json ---------------------------------------------------------------

def test_a_busy_state_file_is_not_mistaken_for_an_untranslated_novel(tmp_path, busy):
    """The most expensive version of this: degrading here made a finished novel read
    as entirely untranslated, and the next sweep re-billed all of it."""
    path = tmp_path / "state.json"
    state = State()
    state.update(1, status="validated", source_hash="h1")
    state.add_usage(1, {"input_tokens": 1000}, 4.25)
    state.save(path)
    before = path.read_bytes()
    busy["target"] = path

    with pytest.raises(PermissionError):
        State.load(path)

    busy["target"] = None
    assert path.read_bytes() == before


def test_a_busy_state_file_survives_a_mutation(tmp_path, busy):
    path = tmp_path / "state.json"
    state = State()
    state.update(1, status="validated", source_hash="h1")
    state.add_usage(1, {"input_tokens": 1000}, 4.25)
    state.save(path)
    busy["target"] = path

    with pytest.raises(PermissionError):
        with jobs.mutate_state(path) as fresh:
            fresh.update(2, status="prepared")

    busy["target"] = None
    recovered = State.load(path)
    assert recovered.is_done(1, "h1", "translate")
    assert recovered.totals()["cost_usd"] == 4.25


def test_a_corrupt_state_file_still_degrades(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    assert State.load(path).chapters == {}
    assert list(path.parent.glob("state.json.unreadable-*"))


# ---- the glossary -------------------------------------------------------------

def test_a_busy_glossary_is_not_mistaken_for_an_empty_one(tmp_path, busy):
    path = tmp_path / "glossary.json"
    glossary = Glossary()
    glossary.add(GlossaryEntry.from_dict(
        {"source": "葵", "english": "Aoi", "type": "name"}))
    glossary.save(path)
    busy["target"] = path

    with pytest.raises(PermissionError):
        Glossary.load(path)


def test_a_busy_pending_queue_is_not_mistaken_for_an_empty_one(tmp_path, busy):
    path = tmp_path / "glossary_pending.json"
    save_pending(path, [{"source": "葵", "english": "Aoi", "type": "name"}])
    busy["target"] = path

    with pytest.raises(PermissionError):
        load_pending(path)


def test_a_corrupt_glossary_still_degrades(tmp_path):
    path = tmp_path / "glossary.json"
    path.write_text("[[[", encoding="utf-8")

    assert Glossary.load(path).entries() == []


def test_an_empty_pending_file_reads_as_no_terms(tmp_path):
    path = tmp_path / "glossary_pending.json"
    path.write_text("   ", encoding="utf-8")

    assert load_pending(path) == []


# ---- the quarantine itself ----------------------------------------------------

def test_the_quarantine_retries_too(tmp_path, busy):
    """It used to fail in exactly the case it existed for: its caller arrived here
    BECAUSE the file would not read, and a single non-retrying read here hit the same
    error and preserved nothing."""
    path = tmp_path / "thing.json"
    path.write_text('{"real": "bytes"}', encoding="utf-8")
    busy["target"] = path
    busy["fail_times"] = 2

    kept = atomic.quarantine_unreadable(path)

    assert kept is not None and "real" in kept.read_text(encoding="utf-8")


def test_the_quarantine_gives_up_rather_than_raising(tmp_path, busy):
    """It is called from an except branch. Raising there would replace the caller's
    real problem with this one."""
    path = tmp_path / "thing.json"
    path.write_text("x", encoding="utf-8")
    busy["target"] = path

    assert atomic.quarantine_unreadable(path) is None


def test_a_missing_file_is_not_retried(tmp_path):
    """Only a sharing violation is worth waiting for. Waiting on a path that does not
    exist would just make every miss slow."""
    with pytest.raises(FileNotFoundError):
        atomic.read_text_retrying(tmp_path / "nope.json")


def test_the_read_and_write_sides_use_the_same_policy():
    """The asymmetry between them WAS the bug, so it is worth pinning that they agree
    rather than leaving it to whoever edits one of them next."""
    source = (Path(atomic.__file__)).read_text(encoding="utf-8")
    assert "_ATTEMPTS" in source and source.count("_ATTEMPTS") >= 3
    assert atomic._ATTEMPTS >= 5
