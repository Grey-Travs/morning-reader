"""The job queue: one worker per project, fed by an appendable FIFO.

Everything the user can start goes through here, which is what gives every operation
the Activity view, the live console, Stop, and the rate-limit auto-resume without any
of that being written twice. The rules this file exists to enforce:

* **One worker per project.** Writes to that project's ``state.json`` are therefore
  serialized by construction, not by hoping every caller remembers a lock.
* **The queue is appendable while it runs.** The user never has to wait for one item
  to finish before queuing the next, and a repair requested mid-run simply queues
  behind the run rather than racing it.
* **A worker always terminates visibly.** If it dies, something still publishes a
  terminal event, or every open stream sits on keep-alives forever showing "running"
  and the job can never be evicted.
* **Recorded events and high-frequency deltas are different things.** ``publish`` is
  replayed to a reconnecting consumer; ``publish_live`` is not, or the replay buffer
  fills with fragments and evicts the events that matter.

The worker is asyncio; the tasks are blocking and run in a threadpool. Every hook that
a task calls therefore marshals back to the loop with ``call_soon_threadsafe`` and
does no other work inline.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from fastapi.concurrency import run_in_threadpool

from morning.config import Config
from morning.exceptions import RateLimited, TaskAborted, TaskRefused
from morning.state import STATUS_FAILED, STATUS_PENDING, State

from . import console, errors, projects as pj, tasks as task_mod
from .locks import file_lock

# Live jobs, and which one owns each project. Module-level because there is one queue
# per process and the endpoints all need to reach it.
_jobs: dict[str, "Job"] = {}
_active_job_by_project: dict[str, str] = {}   # pid -> job_id of the in-flight job
_running_tasks: set[asyncio.Task] = set()      # strong refs so tasks are not GC'd

# Bound memory: a long-lived server accrues a Job per run. Drop old finished jobs,
# keeping the active ones plus the few most recent for a late stream's replay.
_MAX_JOBS = 40
_KEEP_FINISHED = 10

# A replay buffer, not an archive. Long enough that a reconnecting consumer sees the
# whole of a normal run, short enough that a thousand-chapter sweep cannot exhaust
# memory.
_MAX_HISTORY = 1000


@contextmanager
def mutate_state(path: str | Path):
    """Load -> mutate -> save state.json with no other thread interleaving.

    An unguarded load/mutate/save is a read-modify-write race: two threads each read
    the file, each apply their own change to their own copy, and whichever saves last
    silently discards the other's work — a chapter the worker just finished, erased
    because the user edited a different one at the same moment. The atomic write alone
    cannot prevent that; it makes each save all-or-nothing, not the pair of them
    ordered.

    Yields a FRESHLY LOADED State: anything read before the lock was taken is already
    stale by definition, so mutate what is yielded here, not an older copy.

    Keep the body short — it holds off the worker. Do any validating or fetching
    before entering, not inside.
    """
    with file_lock(path):
        state = State.load(path)
        yield state
        state.save(path)


def _persist_item_state(state: State, path: Path, index: int) -> State:
    """Persist ONLY item ``index``'s record without clobbering concurrent edits.

    The worker holds one in-memory ``state`` for the whole job, but the user can edit
    OTHER items while it runs (each via its own load->save). Saving the worker's stale
    whole-state would discard those edits. So reload the on-disk state, overlay just
    this item, save atomically, and hand the merged state back for the worker to keep
    using — so its later ``is_done`` and cost reads are current.
    """
    rec = state.chapters.get(str(index))
    with mutate_state(path) as fresh:
        if rec is not None:
            fresh.chapters[str(index)] = rec
    return fresh


# ---- rate-limit policy -------------------------------------------------------
# How the worker rides out an exhausted usage window. One copy of the policy, so no
# two task kinds can drift on how long they wait.
_RATE_LIMIT_BUFFER = 60          # sec past resets_at before retrying (clock-skew slack)
_FALLBACK_WAIT = 5 * 60          # first retry when the provider gave no resets_at
_FALLBACK_WAIT_MAX = 60 * 60     # backoff cap for unknown reset times
_MAX_WAIT = 12 * 3600            # sanity cap: distrust reset times further out than this
_MAX_STRIKES = 6                 # consecutive rate-limited retries before giving up


def rate_limit_resume_at(exc: BaseException, strikes: int) -> tuple[float, float | None]:
    """When to resume after a rate limit, plus the plan's own reset time if known.

    The sanity cap matters: a provider that reports a reset a week away (or a machine
    with a skewed clock) would otherwise put the worker to sleep for a week, and the
    user would see a job that is neither running nor finished.
    """
    resets_at = getattr(getattr(exc, "info", None), "resets_at", None)
    now = time.time()
    if resets_at and now < resets_at <= now + _MAX_WAIT:
        return resets_at + _RATE_LIMIT_BUFFER, resets_at
    # No, or stale, reset time (e.g. a bare 429) — back off instead.
    return now + min(_FALLBACK_WAIT * (2 ** max(0, strikes - 1)), _FALLBACK_WAIT_MAX), resets_at


async def _sleep_until(job: "Job", when: float) -> None:
    """Sleep until ``when`` (epoch sec), waking early if the job is cancelled, the
    user clicked resume-now (``job.wake``), or the queue emptied.

    Chunked rather than one long sleep so a laptop that slept through the deadline, or
    a queue that was cleared meanwhile, is noticed within a minute.
    """
    job.wake.clear()
    while time.time() < when and job.has_pending() and not job.cancelled:
        try:
            await asyncio.wait_for(job.wake.wait(),
                                   timeout=min(when - time.time(), 60))
            return  # woken explicitly
        except asyncio.TimeoutError:
            continue


# ---- the job -----------------------------------------------------------------

class Job:
    """A per-project worker fed by an APPENDABLE FIFO queue."""

    def __init__(self, job_id: str, pid: str):
        self.id = job_id
        self.pid = pid
        # (index, force, kind). Routing every operation through this one queue is what
        # puts them all in Activity for free, and keeps one worker per project so
        # writes to state.json stay serialized.
        self.pending: deque[tuple[int, bool, str]] = deque()
        # Namespaced keys pending or in-flight, for dedup — see tasks.queue_key: a page
        # and a chapter can share a number.
        self.queued: set[str] = set()
        self.current: int | None = None   # index being worked on right now
        self.kind: str = task_mod.TASK_PREPARE
        self.history: list[dict] = []     # every recorded event, replayed on (re)connect
        self.subscribers: list[asyncio.Queue] = []  # one queue per live SSE consumer
        self.done = False
        self.cancelled = False
        self.terminal: dict | None = None  # final event, replayable for late consumers
        # Live view of the item in flight, replayed as one frame when a stream
        # (re)connects mid-item so a reload does not drop the user into a blank console.
        self.live: dict | None = None
        # Cooperative stop for the in-flight item. The task polls this; a threadpool
        # thread cannot be killed from out here.
        self.abort = threading.Event()
        # Set while the worker sleeps out a rate limit: {resume_at, resets_at,
        # message, since}. None otherwise.
        self.waiting: dict | None = None
        self.wake = asyncio.Event()       # cancel / resume-now interrupts the sleep
        self.started_at = time.time()
        # The worker mutates `pending` on the event loop while request THREADS read it
        # (/api/queue polls every few seconds). Iterating a deque that changes size
        # raises RuntimeError, which surfaced as intermittent 500s that blanked the
        # dashboard. Every touch of `pending` goes through the helpers below.
        self._pending_lock = threading.Lock()

    # ---- queue access (always under _pending_lock) ----
    def enqueue(self, items: list[tuple[int, bool, str]]) -> list[int]:
        """Append (index, force, kind) triples, skipping ones already queued/in-flight."""
        added = []
        with self._pending_lock:
            for index, force, kind in items:
                key = task_mod.queue_key(index, kind)
                if key in self.queued:
                    continue
                self.queued.add(key)
                self.pending.append((index, force, kind))
                added.append(index)
        return added

    def snapshot_pending(self) -> list[tuple[int, bool, str]]:
        """A stable copy, safe to iterate from any thread."""
        with self._pending_lock:
            return list(self.pending)

    def has_pending(self) -> bool:
        with self._pending_lock:
            return bool(self.pending)

    def take_next(self) -> tuple[int, bool, str] | None:
        with self._pending_lock:
            return self.pending.popleft() if self.pending else None

    def put_back(self, item: tuple[int, bool, str]) -> None:
        """Return an interrupted item to the head so it is retried first."""
        with self._pending_lock:
            self.pending.appendleft(item)

    def drain(self) -> list[tuple[int, bool, str]]:
        """Remove and return everything still waiting."""
        with self._pending_lock:
            dropped = list(self.pending)
            self.pending.clear()
            return dropped

    def queue_state(self) -> dict:
        pending = self.snapshot_pending()
        # Before anything starts, `kind` is the default and would mislabel a queued
        # operation until its start event lands — so fall back to what is at the head.
        kind = self.kind if self.current is not None else (
            pending[0][2] if pending else self.kind)
        return {"current": self.current, "kind": kind,
                "pending": [i for i, _, _ in pending],
                "waiting": self.waiting}

    # ---- publishing ----
    def publish(self, ev: dict) -> None:
        """Fan an event out to every connected stream and remember it for replay.

        Non-terminal events are stamped with the live queue state; terminal
        (paused/done) events carry their own, because by the time one is published the
        queue state has already been torn down. Multiple consumers (two tabs, a
        reconnect, dev StrictMode) each get their own copy — no event splitting.
        """
        if ev.get("type") not in ("paused", "done"):
            ev = {**ev, **self.queue_state()}
        else:
            self.terminal = ev
        self.history.append(ev)
        if len(self.history) > _MAX_HISTORY:
            self.history = self.history[-_MAX_HISTORY:]
        console.print_event(self.pid, ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def publish_live(self, ev: dict) -> None:
        """Fan out a high-frequency event WITHOUT recording it in history.

        Progress arrives many times per item; appending it would fill the replay
        buffer with fragments and evict the real start/item/done events. A
        (re)connecting consumer gets ``live_frame()`` as a single catch-up frame
        instead.

        Must be called on the event loop — a task runs in a worker thread and marshals
        here via ``loop.call_soon_threadsafe``.
        """
        console.print_event(self.pid, ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def live_frame(self) -> dict | None:
        """The current item's accumulated state as one replayable event."""
        return {"type": "live", **self.live, **self.queue_state()} if self.live else None


def _build_progress(job: Job, loop: asyncio.AbstractEventLoop
                    ) -> tuple[task_mod.Progress, Callable[[], None]]:
    """Live-progress handle for one item.

    A task runs in a threadpool, so this callback executes on a WORKER THREAD while
    ``publish_live`` touches asyncio queues that belong to the event loop. It
    therefore marshals across with ``call_soon_threadsafe`` and does no other work
    inline.

    Updates are coalesced on the worker-thread side: a chapter of four hundred
    paragraphs would otherwise schedule four hundred loop callbacks and four hundred
    SSE frames. We flush on a time threshold instead.

    Returns the handle plus a ``flush()`` the caller MUST invoke once the item ends —
    otherwise the final sub-threshold update stays buffered and the progress bar is
    permanently a few paragraphs short of where it really got to.
    """
    latest = {"done": 0, "total": 0, "sent": 0.0, "dirty": False}
    lock = threading.Lock()
    FLUSH_SECONDS = 0.1

    def _apply(done: int, total: int) -> None:
        # Runs on the loop.
        if job.live is None:
            return
        job.live["done"] = done
        job.live["total"] = total
        job.publish_live({"type": "progress", "index": job.live.get("index"),
                          "done": done, "total": total})

    def _flush(force: bool = False) -> None:
        with lock:
            if not latest["dirty"]:
                return
            now = time.monotonic()
            if not force and now - latest["sent"] < FLUSH_SECONDS:
                return
            done, total = latest["done"], latest["total"]
            latest["sent"] = now
            latest["dirty"] = False
        loop.call_soon_threadsafe(_apply, done, total)

    def on_progress(done: int, total: int) -> None:
        with lock:
            latest["done"], latest["total"] = done, total
            latest["dirty"] = True
        _flush()

    progress = task_mod.Progress(on_progress=on_progress, abort=job.abort)
    return progress, lambda: _flush(force=True)


# ---- the worker --------------------------------------------------------------

async def _run_worker(job: Job, cfg: Config) -> None:
    loop = asyncio.get_running_loop()
    state_path = Path(cfg.paths.state_file)
    state = State.load(state_path)

    # Drain the queue. The awaits are run_in_threadpool and the rate-limit sleep, so an
    # enqueue arriving mid-flight is always observed on a later iteration — no lost
    # work, because enqueue keeps appending to this job while it waits.
    strikes = 0  # consecutive rate-limit hits; any completed item resets it
    while not job.cancelled:
        item = job.take_next()
        if item is None:
            break
        index, force, kind = item
        job.current = index
        job.kind = kind
        key = task_mod.queue_key(index, kind)

        # Re-read the source on EVERY item, not once at job start. The user can edit
        # or re-paste the source while a sweep runs, and a worker holding the copy it
        # loaded at startup would translate text that is no longer there — and, worse,
        # store a source_hash for content nobody can see.
        chapters = pj.load_source(job.pid)
        chapter = next((c for c in chapters if c.index == index), None)
        if chapter is None:
            job.queued.discard(key)
            job.current = None
            continue

        # Whether this item was ALREADY done on disk before this attempt. Used both to
        # skip non-forced re-runs and, below, to protect finished work from being
        # downgraded if a forced redo is interrupted.
        already_done = state.is_done(index, chapter.metrics.content_hash, kind)
        if not force and already_done:
            job.queued.discard(key)
            job.current = None
            rec = state.get(index) or {}
            job.publish({"type": "item", "index": index, "kind": kind,
                         "status": rec.get("status", ""), "title": chapter.title,
                         "skipped": True})
            continue

        # A fresh live buffer per item, so a reconnecting stream replays THIS item and
        # never the previous one.
        job.abort.clear()
        job.live = {"index": index, "title": chapter.title,
                    "chars": chapter.metrics.char_count, "kind": kind,
                    "done": 0, "total": max(1, len(chapter.paragraphs)),
                    "started_at": time.time()}
        progress, flush = _build_progress(job, loop)
        job.publish({"type": "start", "index": index, "title": chapter.title,
                     "chars": chapter.metrics.char_count, "kind": kind,
                     "label": task_mod.describe(kind, index),
                     "units": len(chapter.paragraphs),
                     "started_at": job.live["started_at"],
                     "model": cfg.anthropic.model, "effort": cfg.anthropic.effort})

        try:
            result = await run_in_threadpool(
                task_mod.run_task, kind, chapter, cfg, progress)
        except TaskAborted:
            # A deliberate stop, not a failure. Leave an item that already had good
            # output marked as it was; only revert one that was genuinely unfinished.
            flush()
            job.live = None
            if not already_done:
                state.update(index, status=STATUS_PENDING, title=chapter.title)
                state = _persist_item_state(state, state_path, index)
            job.queued.discard(key)
            job.current = None
            rec = state.get(index) or {}
            job.publish({"type": "item", "index": index, "kind": kind,
                         "status": rec.get("status", STATUS_PENDING),
                         "title": chapter.title, "aborted": True})
            if job.cancelled:
                break
            continue
        except RateLimited as exc:
            flush()
            job.live = None
            # A rate limit mid-flight must NOT downgrade an item that was already done
            # on disk (an interrupted forced redo): that would show finished work as
            # "Queued" even though its output is still there. Only mark genuinely
            # unfinished items pending, so they resume.
            if not already_done:
                state.update(index, status=STATUS_PENDING, title=chapter.title)
                state = _persist_item_state(state, state_path, index)
            # Put the interrupted item back at the head (it stays in job.queued) and
            # ride the limit out HERE — the worker stays alive and resumes by itself
            # when the window refreshes, with no browser open.
            job.put_back(item)
            job.current = None
            strikes += 1
            resume_at, resets_at = rate_limit_resume_at(exc, strikes)
            if strikes >= _MAX_STRIKES:
                # Something is off (limit hit straight back N times) — stop burning
                # retries and hand resumption to the user.
                job.done = True
                job.publish({"type": "paused", "index": index, "message": str(exc),
                             "resets_at": resets_at, "current": None,
                             "pending": [i for i, _, _ in job.snapshot_pending()]})
                return
            job.waiting = {"resume_at": resume_at, "resets_at": resets_at,
                           "message": str(exc), "since": time.time()}
            job.publish({"type": "waiting", "index": index, "message": str(exc),
                         "resets_at": resets_at, "resume_at": resume_at})
            await _sleep_until(job, resume_at)
            job.waiting = None
            if job.cancelled or not job.has_pending():
                break  # cancelled/cleared during the wait -> normal terminal 'done'
            job.publish({"type": "resumed"})
            continue
        except TaskRefused as exc:
            # Nothing was written and nothing broke: keep the item's current status and
            # report why, so the user sees "not applied" rather than "failed".
            flush()
            job.live = None
            strikes = 0
            job.queued.discard(key)
            job.current = None
            rec = state.get(index) or {}
            job.publish({"type": "item", "index": index, "kind": kind,
                         "status": rec.get("status", STATUS_PENDING),
                         "title": chapter.title, "refused": True, "error": str(exc)})
            continue
        except Exception as exc:  # isolation: one bad item never kills the queue
            flush()
            job.live = None
            strikes = 0  # the provider answered (badly) — a rate limit is not the problem
            state.update(index, status=STATUS_FAILED, title=chapter.title,
                         error=f"{type(exc).__name__}: {exc}")
            state = _persist_item_state(state, state_path, index)
            job.queued.discard(key)
            job.current = None
            # Carry the same plain-English explanation the HTTP layer produces, so a
            # mid-queue failure opens the identical "what went wrong / how to fix it"
            # dialog instead of dumping a raw exception string into the log.
            job.publish({"type": "item", "index": index, "kind": kind,
                         "status": STATUS_FAILED, "title": chapter.title,
                         "error": str(exc),
                         "explain": errors.as_dict(errors.explain(exc))})
            continue

        flush()
        job.live = None
        strikes = 0
        state.update(index, status=result.status, **result.fields)
        if result.usage or result.cost_usd:
            state.add_usage(index, result.usage, result.cost_usd)
        state = _persist_item_state(state, state_path, index)
        rec = state.get(index) or {}
        job.queued.discard(key)
        job.current = None
        totals = state.totals()
        job.publish({"type": "item", "index": index, "kind": kind,
                     "status": result.status, "title": chapter.title,
                     "cost_usd": totals["cost_usd"], "tokens": rec.get("usage", {}),
                     "totals": totals})

    job.done = True
    job.publish({"type": "done", "totals": State.load(state_path).totals(),
                 "current": None, "pending": []})


async def _run_worker_guarded(job: Job, cfg: Config) -> None:
    """Run the worker, guaranteeing it always terminates VISIBLY.

    ``_run_worker`` handles errors per item, but a few steps sit outside that try —
    loading the source, persisting item state. An OSError there (on Windows, an
    antivirus holding state.json for a moment is the realistic one) escaped and killed
    the asyncio task outright. Nothing then published a terminal event, so every open
    stream sat on keep-alives forever still showing "running"; and because ``job.done``
    was never set, the job could not be evicted either, so its history leaked for the
    life of the process.

    Whatever happens, this leaves the job finished and says so on the stream.
    """
    try:
        await _run_worker(job, cfg)
    except asyncio.CancelledError:
        raise  # shutdown, not a failure — let it propagate
    except Exception as exc:  # noqa: BLE001 — a worker must never die silently
        explained = errors.explain(exc)
        errors.log_error(explained, where=f"worker/{job.pid}")
        job.publish({"type": "item", "index": job.current, "kind": job.kind,
                     "status": STATUS_FAILED, "title": "", "error": str(exc),
                     "explain": errors.as_dict(explained)})
    finally:
        job.live = None
        job.current = None
        job.drain()
        job.done = True
        if job.terminal is None:
            # A normal finish and the rate-limit give-up both publish their own
            # terminal event; this only fires when the worker died on the way there.
            job.publish({"type": "done", "totals": None,
                         "current": None, "pending": []})


def _spawn_worker(pid: str, cfg: Config, job: Job) -> None:
    _evict_finished_jobs()
    _jobs[job.id] = job
    _active_job_by_project[pid] = job.id
    task = asyncio.create_task(_run_worker_guarded(job, cfg))
    _running_tasks.add(task)  # strong ref so the task is not garbage-collected

    def _cleanup(t: asyncio.Task) -> None:
        _running_tasks.discard(t)
        if _active_job_by_project.get(pid) == job.id:
            _active_job_by_project.pop(pid, None)

    task.add_done_callback(_cleanup)


def _evict_finished_jobs() -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    finished = [jid for jid, j in _jobs.items() if j.done]
    for jid in finished[:-_KEEP_FINISHED]:
        _jobs.pop(jid, None)


# ---- the public surface the endpoints use ------------------------------------

def active_job(pid: str) -> Job | None:
    """The in-flight job for this project, if there is one."""
    jid = _active_job_by_project.get(pid)
    job = _jobs.get(jid) if jid else None
    return job if job is not None and not job.done else None


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def running_jobs() -> list[tuple[str, Job]]:
    """(pid, job) for every project with work in flight."""
    out = []
    for pid, jid in list(_active_job_by_project.items()):
        job = _jobs.get(jid)
        if job is not None and not job.done:
            out.append((pid, job))
    return out


def enqueue(pid: str, cfg: Config, items: list[tuple[int, bool, str]]) -> dict:
    """Queue work on this project's single worker, starting one if none is running.

    MUST be called from an ``async def`` endpoint: starting a worker schedules an
    asyncio task, and a sync endpoint runs in a threadpool with no running loop.

    Every operation comes through here, which is why they all appear in Activity and
    why only one of them can touch a project's state.json at a time.
    """
    job = active_job(pid)
    if job is not None:
        added = job.enqueue(items)
        if added:
            job.publish({"type": "queued", "added": added})
        return {"job_id": job.id, "queued": added, "already_running": True,
                **job.queue_state()}

    job = Job(uuid.uuid4().hex, pid)
    added = job.enqueue(items)
    _spawn_worker(pid, cfg, job)
    return {"job_id": job.id, "queued": added, "already_running": False,
            **job.queue_state()}


def cancel(pid: str, *, stop_current: bool = False) -> dict:
    """Drop the not-yet-started items from the queue.

    With ``stop_current`` the item being worked on right now is stopped too. That
    cannot be done by killing the worker thread, so it sets ``job.abort``, which the
    task polls and turns into ``TaskAborted``. A stopped item is never marked failed
    and never overwrites good output.
    """
    job = active_job(pid)
    if job is None:
        return {"ok": True, "current": None, "pending": [], "stopped": None}

    job.drain()
    stopped = None
    if stop_current:
        stopped = job.current
        job.cancelled = True   # ends the drain loop once the current item unwinds
        job.abort.set()        # cooperative stop inside the in-flight task
        job.queued.clear()
    else:
        # Keep only the in-flight item's key, so it can still be re-queued afterwards
        # but nothing waiting behind it survives.
        job.queued = ({task_mod.queue_key(job.current, job.kind)}
                      if job.current is not None else set())
    job.wake.set()  # a worker waiting out a rate limit exits promptly
    return {"ok": True, "current": None if stop_current else job.current,
            "pending": [], "stopped": stopped}


def resume(pid: str) -> dict:
    """Wake a worker that is waiting out a rate limit and retry immediately."""
    job = active_job(pid)
    if job is not None and job.waiting:
        job.wake.set()
        return {"ok": True, "resumed": True, **job.queue_state()}
    return {"ok": True, "resumed": False}


def reset_for_tests() -> None:
    """Clear all job state. Tests only — the process otherwise keeps one registry for
    its whole life, and a job left over from one test would be found by the next."""
    _jobs.clear()
    _active_job_by_project.clear()
    _running_tasks.clear()
