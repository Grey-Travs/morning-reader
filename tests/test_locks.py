"""Tests for the one-lock-per-file registry.

An atomic write makes a single save all-or-nothing; it does NOT order two saves.
Without a lock, load -> mutate -> save from two threads is a read-modify-write race:
both read, both apply their own change to their own copy, and whichever writes last
silently discards the other's work.
"""

from __future__ import annotations

import json
import threading

from morning.atomic import atomic_write_json
from morning.locks import file_lock
from server.jobs import mutate_state
from server.locks import file_lock as server_file_lock


def test_one_lock_per_path(tmp_path):
    path = tmp_path / "state.json"
    assert file_lock(path) is file_lock(path)


def test_the_same_file_spelled_differently_gets_the_same_lock(tmp_path):
    """Keyed by RESOLVED path. Two modules reaching the same file by different routes
    must not end up holding two different locks, which would be no lock at all."""
    (tmp_path / "sub").mkdir()
    direct = tmp_path / "sub" / "state.json"
    roundabout = tmp_path / "sub" / ".." / "sub" / "state.json"

    assert file_lock(direct) is file_lock(roundabout)


def test_server_and_engine_share_one_registry(tmp_path):
    """``server.locks`` re-exports the engine's registry. If it ever became its own
    copy, the engine's low-level writer and the server's wider critical section would
    take different locks and neither would exclude the other."""
    path = tmp_path / "state.json"
    assert server_file_lock(path) is file_lock(path)


def test_different_paths_get_different_locks(tmp_path):
    assert file_lock(tmp_path / "a.json") is not file_lock(tmp_path / "b.json")


def test_the_lock_is_re_entrant(tmp_path):
    """RLock, not Lock. A caller holds the lock across read -> mutate -> write while
    the low-level writer takes it again underneath; with a plain Lock that same-thread
    re-acquisition is a deadlock."""
    path = tmp_path / "state.json"
    lock = file_lock(path)

    with lock:
        with lock:
            with lock:
                pass  # three deep and still alive


def test_the_registry_is_thread_safe(tmp_path):
    """Two threads asking for a lock on a brand-new path at the same instant must
    still get the SAME object — otherwise the very first contended write is unguarded."""
    path = tmp_path / "brand-new.json"
    barrier = threading.Barrier(8)
    got: list = []
    guard = threading.Lock()

    def grab() -> None:
        barrier.wait()
        lock = file_lock(path)
        with guard:
            got.append(lock)

    pool = [threading.Thread(target=grab) for _ in range(8)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    assert len({id(lock) for lock in got}) == 1


def test_mutate_state_serializes_read_modify_write(tmp_path):
    """The defect this exists to prevent: concurrent load/mutate/save losing updates.

    Each thread adds its own chapter. Unguarded, threads read the same file and the
    last save wins, so most of the chapters vanish. Guarded, every one survives.
    """
    path = tmp_path / "state.json"
    atomic_write_json(path, {"chapters": {}})

    threads = 8
    barrier = threading.Barrier(threads)

    def add(i: int) -> None:
        barrier.wait()
        with mutate_state(path) as state:
            state.update(i, status="prepared", source_hash=f"h{i}")

    pool = [threading.Thread(target=add, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    saved = json.loads(path.read_text(encoding="utf-8"))["chapters"]
    assert sorted(int(k) for k in saved) == list(range(threads))


def test_mutate_state_yields_freshly_loaded_state(tmp_path):
    """Anything read before the lock was taken is stale by definition, so the context
    manager must hand back what is on disk NOW, not an older copy."""
    path = tmp_path / "state.json"
    atomic_write_json(path, {"chapters": {"1": {"status": "prepared"}}})

    with mutate_state(path) as state:
        assert state.get(1) == {"status": "prepared"}
        state.update(2, status="pending")

    with mutate_state(path) as state:
        assert state.get(1) is not None
        assert state.get(2) is not None
