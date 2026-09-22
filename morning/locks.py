"""One lock per on-disk file, shared by everything that reads-modifies-writes it.

An atomic write makes a single save all-or-nothing; it does NOT order two saves.
Without a lock, load -> mutate -> save from two threads is a read-modify-write race:
both read, both apply their own change to their own copy, and whichever writes last
silently discards the other's work.

Every writer in this app runs on a THREAD — sync endpoints and ``run_in_threadpool``
alike — never on the event loop, so a threading lock is the right primitive.

**RLock, not Lock.** A chapter edit is read -> splice -> write, and the caller holds the
lock across all three while the low-level writer acquires it again underneath. With a
plain Lock that same-thread re-acquisition is a deadlock; re-entrancy is what lets the
lock sit on the low-level writer (where no caller can forget it) *and* span a wider
critical section when one is needed.

Lives in ``morning`` rather than ``server`` so the engine — which must not import the
web layer — can use the same registry. ``server.locks`` re-exports it, so there is
exactly one lock per path across the whole process.
"""

from __future__ import annotations

import threading
from pathlib import Path

# Keyed by resolved path, so the same file always maps to the same lock however the
# caller spelled it.
_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def file_lock(path: str | Path) -> threading.RLock:
    """The lock guarding ``path``. Stable for the process lifetime, and re-entrant."""
    key = str(Path(path).resolve())
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock
