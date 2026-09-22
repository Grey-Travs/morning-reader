"""Tests for per-item run state — the resumability contract.

``state.json`` is what stops a crash or a rate limit from forcing a full, re-billed
re-run. Two things make that work and both are pinned here: ``is_done`` keys on the
source hash as well as the status, and a file that cannot be read degrades to empty
*after* its bytes have been preserved.
"""

from __future__ import annotations

import json
import threading

from morning.state import (
    DONE_STATUSES, STATUS_EMPTY, STATUS_ENGLISH, STATUS_FAILED, STATUS_PENDING,
    STATUS_PREPARED, STATUS_VALIDATED, State,
)


def test_a_missing_file_loads_as_empty(tmp_path):
    assert State.load(tmp_path / "nothing.json").chapters == {}


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = State()
    state.update(1, status=STATUS_PREPARED, source_hash="abc")
    state.save(path)

    assert State.load(path).get(1)["status"] == STATUS_PREPARED


def test_update_stamps_timestamps_once_for_created_and_every_time_for_updated(tmp_path):
    state = State()
    first = dict(state.update(1, status=STATUS_PENDING))
    second = state.update(1, status=STATUS_PREPARED)

    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


# ---- is_done -----------------------------------------------------------------

def test_is_done_requires_both_the_status_and_the_hash():
    state = State()
    state.update(1, status=STATUS_PREPARED, source_hash="abc")

    assert state.is_done(1, "abc", "prepare")
    # Edit the source and the hash moves, so the work stops being done and re-runs.
    assert not state.is_done(1, "different", "prepare")


def test_is_done_is_per_kind():
    """"Done" is a property of the QUESTION, not the item. A chapter that is merely
    prepared is done for prepare and emphatically not for translate — conflating them
    is how a queued translate silently no-ops."""
    state = State()
    state.update(1, status=STATUS_PREPARED, source_hash="abc")

    assert state.is_done(1, "abc", "prepare")
    assert not state.is_done(1, "abc", "translate")

    state.update(1, status=STATUS_VALIDATED)
    assert state.is_done(1, "abc", "prepare")
    assert state.is_done(1, "abc", "translate")


def test_terminal_classifications_are_done_for_every_kind():
    """An empty or already-English chapter is finished business. Leaving it not-done
    would re-queue it on every sweep forever."""
    for status in (STATUS_EMPTY, STATUS_ENGLISH):
        state = State()
        state.update(1, status=status, source_hash="abc")
        for kind in DONE_STATUSES:
            assert state.is_done(1, "abc", kind), f"{status} should be done for {kind}"


def test_a_failed_item_is_never_done():
    state = State()
    state.update(1, status=STATUS_FAILED, source_hash="abc")

    assert not state.is_done(1, "abc", "prepare")
    assert not state.is_done(1, "abc", "translate")


def test_an_unknown_kind_is_never_done():
    """Skipping work because of a typo'd kind would be silent and unrecoverable, so
    an unrecognised question answers "not done" rather than guessing."""
    state = State()
    state.update(1, status=STATUS_VALIDATED, source_hash="abc")

    assert not state.is_done(1, "abc", "trnaslate")


def test_an_unknown_index_is_not_done():
    assert not State().is_done(99, "abc", "prepare")


# ---- corruption --------------------------------------------------------------

def test_a_corrupt_file_degrades_to_empty_and_keeps_the_bytes(tmp_path):
    """Degrading keeps one bad file from taking down the library. Quarantining first
    is what stops a MOMENTARY read failure from becoming permanent destruction: the
    caller mutates this empty state and saves it back over the real one."""
    path = tmp_path / "state.json"
    path.write_text('{"chapters": {"1": {"status": "valid', encoding="utf-8")

    state = State.load(path)

    assert state.chapters == {}
    kept = list(tmp_path.glob("state.json.unreadable-*"))
    assert len(kept) == 1
    assert "valid" in kept[0].read_text(encoding="utf-8")


def test_a_file_holding_a_json_list_degrades_to_empty(tmp_path):
    """Valid JSON of the wrong shape must not produce a State with a list for
    ``chapters`` — every later ``.get`` would raise on it."""
    path = tmp_path / "state.json"
    path.write_text('["not", "a", "mapping"]', encoding="utf-8")

    assert State.load(path).chapters == {}


def test_a_file_whose_chapters_key_is_the_wrong_type_degrades(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"chapters": []}', encoding="utf-8")

    assert State.load(path).chapters == {}


def test_an_empty_file_loads_as_empty_without_quarantine(tmp_path):
    """A zero-byte state.json is what a killed process leaves; it holds nothing worth
    preserving, so it should not litter the folder with an empty quarantine copy."""
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")

    assert State.load(path).chapters == {}
    assert list(tmp_path.glob("*.unreadable-*")) == []


def test_a_record_of_the_wrong_type_reads_as_missing():
    state = State({"chapters": {"1": "not a record"}})
    assert state.get(1) is None


# ---- usage and totals --------------------------------------------------------

def test_usage_accumulates_rather_than_replacing():
    """One chapter can be worked on several times, and the project's cost is all of
    it. Assignment here would silently under-report spend."""
    state = State()
    state.add_usage(1, {"input_tokens": 100, "output_tokens": 50}, 0.01)
    state.add_usage(1, {"input_tokens": 20, "output_tokens": 5}, 0.002)

    rec = state.get(1)
    assert rec["usage"] == {"input_tokens": 120, "output_tokens": 55}
    assert rec["cost_usd"] == 0.012


def test_totals_sum_across_items():
    state = State()
    state.add_usage(1, {"input_tokens": 100}, 0.01)
    state.add_usage(2, {"input_tokens": 200, "cache_read": 5}, 0.02)

    assert state.totals() == {"cost_usd": 0.03,
                              "tokens": {"input_tokens": 300, "cache_read": 5}}


def test_totals_ignore_non_numeric_usage():
    """A malformed usage value from a provider must not make the whole totals call
    raise — the dashboard would go blank over one bad record."""
    state = State({"chapters": {"1": {"usage": {"input_tokens": "lots"},
                                      "cost_usd": 0.5}}})

    assert state.totals() == {"cost_usd": 0.5, "tokens": {}}


def test_booleans_are_not_counted_as_usage():
    """``True`` is an int in Python. Counting it would add 1 to a token total from a
    flag that happened to be in the usage dict."""
    state = State()
    state.add_usage(1, {"input_tokens": 10, "cached": True}, 0.0)

    assert state.get(1)["usage"] == {"input_tokens": 10}


def test_concurrent_saves_leave_valid_json(tmp_path):
    """State.save goes through the atomic write, so this is really a guard against
    someone swapping it for a plain write later."""
    path = tmp_path / "state.json"
    State().save(path)
    barrier = threading.Barrier(6)

    def save(i: int) -> None:
        barrier.wait()
        s = State.load(path)
        s.update(i, status=STATUS_PREPARED)
        s.save(path)

    pool = [threading.Thread(target=save, args=(i,)) for i in range(6)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    json.loads(path.read_text(encoding="utf-8"))
