"""Tests for the job queue — the spine's most load-bearing piece.

Every operation the user can start runs through one Job per project, which is what
gives them all the Activity view, Stop, and the rate-limit auto-resume without any of
that being written twice. The behaviours pinned here are the ones whose absence is
invisible until it costs someone a translation they paid for.

The worker is asyncio and the tasks are blocking, so these drive a real event loop
with ``asyncio.run`` rather than mocking one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from morning.chapters import Chapter
from morning.config import Config
from morning.exceptions import RateLimited, RateLimitInfo, TaskRefused
from morning.state import (
    STATUS_EMPTY, STATUS_ENGLISH, STATUS_FAILED, STATUS_PENDING, STATUS_PREPARED,
    State,
)
from server import jobs, projects as pj, tasks as task_mod

JA = "電車はまだ来ない。"
EN = "The train has not come yet."


# ---- harness -----------------------------------------------------------------

def _project(n: int = 3, *, english_last: bool = False) -> tuple[str, Config]:
    project = pj.create_project("朝の駅")
    chapters = [Chapter(index=i, title=f"第{i}話", paragraphs=[JA, JA])
                for i in range(1, n + 1)]
    if english_last and chapters:
        chapters[-1].paragraphs = [EN, EN]
    pj.save_source(project["id"], chapters)
    return project["id"], pj.project_config(Config(), project)


def _items(kind: str, *indices: int, force: bool = False):
    return [(i, force, kind) for i in indices]


async def _drain(pid: str, cfg: Config, items, timeout: float = 10.0) -> jobs.Job:
    """Queue work and wait for the worker to finish, returning the Job."""
    result = jobs.enqueue(pid, cfg, items)
    job = jobs.get_job(result["job_id"])
    assert job is not None
    deadline = time.monotonic() + timeout
    while not job.done and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert job.done, "the worker never finished"
    # Let the done-callback run so the project is released.
    await asyncio.sleep(0)
    return job


def _fast_rate_limits(monkeypatch) -> None:
    """Shrink the waiting policy so a test can exercise it in milliseconds.

    Both paths have to be shrunk, not just the reset-time one. A reset only a few
    milliseconds out has already passed by the time the worker evaluates it — there is
    a threadpool round-trip in between — so it is correctly distrusted and the worker
    falls through to the FALLBACK back-off instead. Leaving that at five minutes is
    how this test hung rather than failed.
    """
    monkeypatch.setattr(jobs, "_RATE_LIMIT_BUFFER", 0)
    monkeypatch.setattr(jobs, "_FALLBACK_WAIT", 0.05)
    monkeypatch.setattr(jobs, "_FALLBACK_WAIT_MAX", 0.2)


def _events(job: jobs.Job, kind: str) -> list[dict]:
    return [e for e in job.history if e.get("type") == kind]


def _statuses(cfg: Config) -> dict[int, str]:
    state = State.load(cfg.paths.state_file)
    return {int(k): v.get("status") for k, v in state.chapters.items()}


# ---- the dedup key -----------------------------------------------------------

def test_the_queue_key_namespaces_pages_away_from_chapters():
    """Pages and chapters share one worker but NOT one number space: after a build,
    page 5 and chapter 5 both exist and are different things. Night Reader deduped on
    the bare index, so queueing a page silently dropped a chapter."""
    assert task_mod.queue_key(5, "prepare").startswith("ch:")

    original = task_mod.PAGE_TASK_KINDS
    task_mod.PAGE_TASK_KINDS = ("read-page",)
    try:
        assert task_mod.queue_key(5, "read-page").startswith("pg:")
        assert task_mod.queue_key(5, "read-page") != task_mod.queue_key(5, "prepare")
    finally:
        task_mod.PAGE_TASK_KINDS = original


def test_the_queue_key_separates_different_things_asked_of_one_chapter():
    """Two different operations on the same chapter must both be able to queue."""
    assert task_mod.queue_key(5, "prepare") != task_mod.queue_key(5, "translate")


# ---- ordering and dedup ------------------------------------------------------

def test_items_run_in_the_order_they_were_queued():
    pid, cfg = _project(3)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 3, 1, 2)))

    assert [e["index"] for e in _events(job, "item")] == [3, 1, 2]


def test_queueing_the_same_item_twice_queues_it_once():
    pid, cfg = _project(2)
    job = jobs.Job("j", pid)

    assert job.enqueue(_items("prepare", 1, 1, 2, 1)) == [1, 2]
    assert [i for i, _, _ in job.snapshot_pending()] == [1, 2]


def test_work_can_be_queued_while_the_worker_runs():
    """The user never has to wait for one item to finish before queuing the next."""
    pid, cfg = _project(4)

    async def scenario():
        first = jobs.enqueue(pid, cfg, _items("prepare", 1))
        job = jobs.get_job(first["job_id"])
        second = jobs.enqueue(pid, cfg, _items("prepare", 2, 3, 4))
        assert second["already_running"] is True
        assert second["job_id"] == job.id
        deadline = time.monotonic() + 10
        while not job.done and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return job

    job = asyncio.run(scenario())
    assert sorted(e["index"] for e in _events(job, "item")) == [1, 2, 3, 4]
    assert _events(job, "queued"), "the late enqueue should be announced on the stream"


def test_one_worker_per_project():
    """Two workers would race each other's writes to state.json."""
    pid, cfg = _project(2)

    async def scenario():
        a = jobs.enqueue(pid, cfg, _items("prepare", 1))
        b = jobs.enqueue(pid, cfg, _items("prepare", 2))
        assert a["job_id"] == b["job_id"]
        job = jobs.get_job(a["job_id"])
        deadline = time.monotonic() + 10
        while not job.done and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return job

    asyncio.run(scenario())


# ---- the work actually lands -------------------------------------------------

def test_preparing_records_what_the_chapter_is():
    pid, cfg = _project(2)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2)))

    state = State.load(cfg.paths.state_file)
    rec = state.get(1)
    assert rec["status"] == STATUS_PREPARED
    assert rec["paragraph_count"] == 2
    assert rec["source_fraction"] > 0.8
    assert rec["class"] == "source"
    assert len(rec["source_hash"]) == 64


def test_an_english_chapter_is_recorded_as_english_not_failed():
    """Skipping it is the point — translating English into English spends the user's
    allowance for nothing. Marking it failed would put it in their face forever."""
    pid, cfg = _project(2, english_last=True)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2)))

    assert _statuses(cfg) == {1: STATUS_PREPARED, 2: STATUS_ENGLISH}


def test_an_empty_chapter_is_recorded_as_empty():
    project = pj.create_project("x")
    pj.save_source(project["id"], [Chapter(index=1, title="blank", paragraphs=[])])
    cfg = pj.project_config(Config(), project)

    asyncio.run(_drain(project["id"], cfg, _items("prepare", 1)))

    assert _statuses(cfg) == {1: STATUS_EMPTY}


def test_the_terminal_event_carries_the_totals():
    pid, cfg = _project(1)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    done = _events(job, "done")
    assert len(done) == 1
    assert done[0]["totals"] == {"cost_usd": 0.0, "tokens": {}}
    assert done[0]["pending"] == []


# ---- resumability ------------------------------------------------------------

def test_work_already_done_at_this_source_is_skipped():
    pid, cfg = _project(2)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2)))
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2)))

    assert all(e.get("skipped") for e in _events(job, "item"))
    assert not _events(job, "start"), "a skipped item should not start"


def test_editing_the_source_makes_the_work_run_again():
    """The resumability contract: status AND hash. Edit the text and the hash moves."""
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))
    first = State.load(cfg.paths.state_file).get(1)["source_hash"]

    pj.save_source(pid, [Chapter(index=1, title="第1話",
                                 paragraphs=[JA, JA, JA])])
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    assert not any(e.get("skipped") for e in _events(job, "item"))
    assert State.load(cfg.paths.state_file).get(1)["source_hash"] != first


def test_force_redoes_work_that_was_already_done():
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, force=True)))

    assert not any(e.get("skipped") for e in _events(job, "item"))
    assert _events(job, "start")


def test_a_forced_sweep_queues_everything_rather_than_nothing():
    """"Redo the whole project" must not be filtered by the already-done check —
    otherwise every finished chapter is skipped and the request appears to succeed
    while having queued nothing at all."""
    pid, cfg = _project(3)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2, 3)))

    chapters = pj.load_source(pid)
    state = State.load(cfg.paths.state_file)
    unforced = task_mod.resolve_items(chapters, state, "prepare", cfg)
    forced = task_mod.resolve_items(chapters, state, "prepare", cfg, force=True)

    assert unforced == []
    assert [i for i, _, _ in forced] == [1, 2, 3]


def test_prepare_applies_to_english_chapters_but_translating_does_not():
    """Prepare is what DECIDES a chapter is English, so filtering it by that
    classification would leave the chapter unclassified forever."""
    pid, cfg = _project(2, english_last=True)
    chapters = pj.load_source(pid)
    state = State.load(cfg.paths.state_file)

    assert [i for i, _, _ in
            task_mod.resolve_items(chapters, state, "prepare", cfg)] == [1, 2]
    assert [i for i, _, _ in
            task_mod.resolve_items(chapters, state, "translate", cfg)] == [1]


def test_explicit_indices_are_honoured_even_when_a_sweep_would_skip_them():
    pid, cfg = _project(2, english_last=True)
    chapters = pj.load_source(pid)
    state = State.load(cfg.paths.state_file)

    items = task_mod.resolve_items(chapters, state, "translate", cfg, indices=[2])
    assert [i for i, _, _ in items] == [2]


# ---- stopping ----------------------------------------------------------------

def _stop_once_in_flight(pid: str, monkeypatch) -> None:
    """Press Stop while the item is actually running, as a user does.

    Setting ``job.abort`` before the worker picks the item up would prove nothing: the
    worker clears a stale abort at the top of every item, deliberately, so that a stop
    aimed at one item cannot kill the next one. The only faithful way in is through the
    real ``cancel`` path, from inside the running task.
    """
    fired = {"done": False}

    def stop_midway(kind, chapter, ctx):
        if not fired["done"]:
            fired["done"] = True
            jobs.cancel(pid, stop_current=True)
        ctx.progress.check()  # a real long task polls here; this is where it raises
        raise AssertionError("the task should have been stopped before this")

    monkeypatch.setattr(task_mod, "run_task", stop_midway)


def test_stopping_leaves_an_unfinished_item_pending_so_it_resumes(monkeypatch):
    pid, cfg = _project(1)
    _stop_once_in_flight(pid, monkeypatch)

    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    item = _events(job, "item")[0]
    assert item["aborted"] is True
    assert item["status"] == STATUS_PENDING
    assert _statuses(cfg) == {1: STATUS_PENDING}


def test_a_stopped_item_is_never_marked_failed(monkeypatch):
    """A deliberate stop is not a failure. Marking it failed would put a red row in
    front of the user for something they asked for."""
    pid, cfg = _project(1)
    _stop_once_in_flight(pid, monkeypatch)

    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    assert _events(job, "item")[0]["status"] != STATUS_FAILED
    assert "explain" not in _events(job, "item")[0]


def test_stopping_does_not_downgrade_work_that_was_already_finished(monkeypatch):
    """An interrupted FORCED redo must keep the finished status it already had.
    Reverting it would show completed work as "Queued" while its output sits on disk."""
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))
    _stop_once_in_flight(pid, monkeypatch)

    asyncio.run(_drain(pid, cfg, _items("prepare", 1, force=True)))

    assert _statuses(cfg) == {1: STATUS_PREPARED}


def test_cancel_clears_the_queue_and_reports_what_it_stopped():
    pid, cfg = _project(4)

    async def scenario():
        result = jobs.enqueue(pid, cfg, _items("prepare", 1, 2, 3, 4))
        job = jobs.get_job(result["job_id"])
        out = jobs.cancel(pid, stop_current=True)
        deadline = time.monotonic() + 10
        while not job.done and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return job, out

    job, out = asyncio.run(scenario())
    assert out["ok"] is True
    assert out["pending"] == []
    assert job.cancelled is True
    assert len(_events(job, "item")) < 4, "the queue should not have drained fully"


def test_cancelling_a_project_with_nothing_running_is_harmless():
    pid, _cfg = _project(1)
    assert jobs.cancel(pid) == {"ok": True, "current": None, "pending": [],
                                "stopped": None}


# ---- refusal, failure, isolation ---------------------------------------------

def test_a_refusal_leaves_the_item_exactly_as_it_was(monkeypatch):
    """Nothing broke and nothing changed, so the user should see "not applied" with a
    reason rather than "failed"."""
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    def refuse(kind, chapter, ctx):
        raise TaskRefused("there was nothing to correct")

    monkeypatch.setattr(task_mod, "run_task", refuse)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, force=True)))

    item = _events(job, "item")[0]
    assert item["refused"] is True
    assert item["error"] == "there was nothing to correct"
    assert item["status"] == STATUS_PREPARED   # unchanged
    assert _statuses(cfg) == {1: STATUS_PREPARED}


def test_an_unknown_task_kind_is_refused_not_crashed():
    pid, cfg = _project(1)
    job = asyncio.run(_drain(pid, cfg, [(1, False, "sing-a-song")]))

    assert _events(job, "item")[0]["refused"] is True


def test_one_bad_item_never_kills_the_queue(monkeypatch):
    """Isolation. Without it, chapter 2 failing means chapters 3 and 4 never run and
    the job dies without a terminal event."""
    pid, cfg = _project(4)
    real = task_mod.run_task

    def sometimes_explode(kind, chapter, ctx):
        if chapter.index == 2:
            raise RuntimeError("the model returned something unreadable")
        return real(kind, chapter, ctx)

    monkeypatch.setattr(task_mod, "run_task", sometimes_explode)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2, 3, 4)))

    statuses = _statuses(cfg)
    assert statuses[2] == STATUS_FAILED
    assert statuses[1] == statuses[3] == statuses[4] == STATUS_PREPARED
    assert _events(job, "done"), "the job must still finish visibly"


def test_a_failure_carries_the_same_explanation_the_http_layer_would_give(monkeypatch):
    """So a mid-queue failure opens the identical "what went wrong" dialog instead of
    dumping a raw exception string into the log."""
    pid, cfg = _project(1)

    def explode(kind, chapter, ctx):
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(task_mod, "run_task", explode)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    item = _events(job, "item")[0]
    assert item["status"] == STATUS_FAILED
    assert item["explain"]["code"] == "file-locked"
    assert item["explain"]["fixes"], "an explanation with no fixes helps nobody"


def test_a_missing_chapter_is_dropped_without_failing_the_job():
    pid, cfg = _project(1)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, 99)))

    assert [e["index"] for e in _events(job, "item")] == [1]
    assert _events(job, "done")


# ---- the guarded worker ------------------------------------------------------

def test_a_worker_that_dies_outside_the_per_item_try_still_finishes_visibly(monkeypatch):
    """The failure this guard exists for: an OSError persisting state killed the task
    outright, so every open stream sat on keep-alives forever still showing "running",
    and because ``job.done`` was never set the job could not be evicted either."""
    pid, cfg = _project(1)

    def explode(*a, **k):
        raise OSError("the disk went away")

    monkeypatch.setattr(jobs, "_persist_item_state", explode)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    assert job.done is True
    assert job.terminal is not None
    assert job.terminal["type"] in ("done", "paused")
    assert job.current is None


def test_a_dying_worker_drains_its_queue_rather_than_stranding_it(monkeypatch):
    pid, cfg = _project(3)

    def explode(*a, **k):
        raise OSError("the disk went away")

    monkeypatch.setattr(jobs, "_persist_item_state", explode)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2, 3)))

    assert job.snapshot_pending() == []
    assert job.done is True


# ---- rate limits -------------------------------------------------------------

def test_the_resume_time_uses_the_provider_reset_when_it_is_plausible():
    soon = time.time() + 120
    resume_at, resets_at = jobs.rate_limit_resume_at(
        RateLimited(RateLimitInfo(resets_at=soon)), 1)

    assert resets_at == soon
    assert resume_at == soon + jobs._RATE_LIMIT_BUFFER


def test_a_bare_rate_limit_backs_off_exponentially():
    """No reset time means guessing, and guessing the same short interval repeatedly
    is how you burn an allowance against a wall."""
    first, _ = jobs.rate_limit_resume_at(RateLimited(), 1)
    second, _ = jobs.rate_limit_resume_at(RateLimited(), 2)
    third, _ = jobs.rate_limit_resume_at(RateLimited(), 3)

    assert second - time.time() > first - time.time()
    assert third - time.time() > second - time.time()


def test_an_implausible_reset_time_is_distrusted():
    """A provider reporting a reset a week out — or a machine with a skewed clock —
    would otherwise put the worker to sleep for a week, showing a job that is neither
    running nor finished."""
    a_week = time.time() + 7 * 24 * 3600
    resume_at, _ = jobs.rate_limit_resume_at(
        RateLimited(RateLimitInfo(resets_at=a_week)), 1)

    assert resume_at < time.time() + jobs._FALLBACK_WAIT_MAX + 1


def test_a_reset_time_in_the_past_is_distrusted():
    resume_at, _ = jobs.rate_limit_resume_at(
        RateLimited(RateLimitInfo(resets_at=time.time() - 60)), 1)

    assert resume_at > time.time()


def test_the_worker_rides_out_a_rate_limit_and_resumes(monkeypatch):
    """The behaviour that makes an overnight run survive: the worker stays alive and
    picks up by itself, with no browser open."""
    pid, cfg = _project(2)
    _fast_rate_limits(monkeypatch)
    real = task_mod.run_task
    hits = {"n": 0}

    def limited_once(kind, chapter, ctx):
        if chapter.index == 1 and hits["n"] == 0:
            hits["n"] += 1
            raise RateLimited(RateLimitInfo(resets_at=time.time() + 0.3))
        return real(kind, chapter, ctx)

    monkeypatch.setattr(task_mod, "run_task", limited_once)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2)))

    assert _events(job, "waiting"), "the wait should be announced"
    assert _events(job, "resumed"), "and so should the resume"
    # The interrupted item went back to the HEAD and ran first.
    assert [e["index"] for e in _events(job, "item")] == [1, 2]
    assert _statuses(cfg) == {1: STATUS_PREPARED, 2: STATUS_PREPARED}


def test_a_rate_limit_does_not_downgrade_work_already_finished(monkeypatch):
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))
    _fast_rate_limits(monkeypatch)
    real = task_mod.run_task
    hits = {"n": 0}

    def limited_once(kind, chapter, ctx):
        if hits["n"] == 0:
            hits["n"] += 1
            raise RateLimited(RateLimitInfo(resets_at=time.time() + 0.3))
        return real(kind, chapter, ctx)

    monkeypatch.setattr(task_mod, "run_task", limited_once)

    async def scenario():
        result = jobs.enqueue(pid, cfg, _items("prepare", 1, force=True))
        job = jobs.get_job(result["job_id"])
        # While it waits, the finished status must still be on disk.
        await asyncio.sleep(0.02)
        mid_flight = _statuses(cfg)
        deadline = time.monotonic() + 10
        while not job.done and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return mid_flight

    assert asyncio.run(scenario()) == {1: STATUS_PREPARED}


def test_repeated_rate_limits_give_up_and_pause_rather_than_burning_retries(monkeypatch):
    pid, cfg = _project(1)
    monkeypatch.setattr(jobs, "_MAX_STRIKES", 2)
    _fast_rate_limits(monkeypatch)

    def always_limited(kind, chapter, ctx):
        raise RateLimited(RateLimitInfo(resets_at=time.time() + 0.1),
                          "usage limit reached")

    monkeypatch.setattr(task_mod, "run_task", always_limited)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    assert job.terminal["type"] == "paused"
    assert job.terminal["message"] == "usage limit reached"
    # The item is still listed, so the user knows what is outstanding.
    assert job.terminal["pending"] == [1]


def test_sleep_until_wakes_early_when_asked():
    """Resume-now must not wait out the rest of a five-minute back-off."""
    pid, _cfg = _project(1)
    job = jobs.Job("j", pid)
    job.enqueue(_items("prepare", 1))

    async def scenario():
        started = time.monotonic()
        waker = asyncio.get_running_loop().call_later(0.05, job.wake.set)
        await jobs._sleep_until(job, time.time() + 30)
        waker.cancel()
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5


def test_sleep_until_gives_up_when_the_queue_is_cleared():
    """A worker asleep on a rate limit whose queue was cancelled should not keep the
    project marked busy for the rest of the window."""
    pid, _cfg = _project(1)
    job = jobs.Job("j", pid)  # nothing queued

    async def scenario():
        started = time.monotonic()
        await jobs._sleep_until(job, time.time() + 30)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5


def test_resume_reports_whether_there_was_anything_to_wake():
    pid, _cfg = _project(1)
    assert jobs.resume(pid) == {"ok": True, "resumed": False}


# ---- concurrency with the rest of the app ------------------------------------

def test_the_worker_does_not_clobber_an_edit_made_to_another_chapter(monkeypatch):
    """The worker holds one in-memory state for the whole job while the user edits
    OTHER chapters through their own load->save. Saving the worker's stale whole-state
    would discard those edits, so only the item's own record is overlaid."""
    pid, cfg = _project(2)
    real = task_mod.run_task

    def edit_another_chapter_midway(kind, chapter, ctx):
        if chapter.index == 1:
            # Stand in for a request thread touching chapter 2 while 1 is in flight.
            with jobs.mutate_state(cfg.paths.state_file) as state:
                state.update(2, status="needs-review", note="edited by the user")
        return real(kind, chapter, ctx)

    monkeypatch.setattr(task_mod, "run_task", edit_another_chapter_midway)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    state = State.load(cfg.paths.state_file)
    assert state.get(1)["status"] == STATUS_PREPARED
    assert state.get(2)["note"] == "edited by the user", "the user's edit was erased"


def test_the_worker_re_reads_the_source_for_every_item():
    """A sweep must translate the text that is there NOW. A worker holding the copy it
    loaded at job start would store a source_hash for content nobody can see."""
    pid, cfg = _project(2)

    async def scenario():
        result = jobs.enqueue(pid, cfg, _items("prepare", 1, 2))
        job = jobs.get_job(result["job_id"])
        # Replace chapter 2's text while the sweep is on chapter 1.
        pj.save_source(pid, [
            Chapter(index=1, title="第1話", paragraphs=[JA, JA]),
            Chapter(index=2, title="第2話", paragraphs=[JA, JA, JA, JA]),
        ])
        deadline = time.monotonic() + 10
        while not job.done and time.monotonic() < deadline:
            await asyncio.sleep(0.005)

    asyncio.run(scenario())
    assert State.load(cfg.paths.state_file).get(2)["paragraph_count"] == 4


# ---- events and streaming ----------------------------------------------------

def test_progress_is_not_recorded_in_history():
    """Deltas arrive many times per item; recording them would fill the replay buffer
    with fragments and evict the start/item/done events that actually matter."""
    pid, cfg = _project(1)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    assert _events(job, "progress") == []


def test_non_terminal_events_carry_the_live_queue_state():
    pid, cfg = _project(3)
    job = asyncio.run(_drain(pid, cfg, _items("prepare", 1, 2, 3)))

    first_start = _events(job, "start")[0]
    assert first_start["current"] == 1
    assert first_start["pending"] == [2, 3]


def test_history_is_capped():
    """A replay buffer, not an archive. A thousand-item sweep must not grow without
    bound for the life of the process."""
    job = jobs.Job("j", "p")
    for i in range(jobs._MAX_HISTORY + 50):
        job.publish({"type": "item", "index": i})

    assert len(job.history) == jobs._MAX_HISTORY
    assert job.history[-1]["index"] == jobs._MAX_HISTORY + 49


def test_every_subscriber_gets_its_own_copy_of_an_event():
    """Two tabs, a reconnect, and a dev-mode double render all subscribe at once. A
    shared queue would split events between them."""
    job = jobs.Job("j", "p")

    async def scenario():
        a, b = asyncio.Queue(), asyncio.Queue()
        job.subscribers.extend([a, b])
        job.publish({"type": "item", "index": 1})
        return a.get_nowait(), b.get_nowait()

    first, second = asyncio.run(scenario())
    assert first["index"] == second["index"] == 1


def test_the_live_frame_catches_a_reconnecting_consumer_up():
    job = jobs.Job("j", "p")
    assert job.live_frame() is None

    job.live = {"index": 2, "title": "第2話", "done": 5, "total": 9}
    frame = job.live_frame()

    assert frame["type"] == "live"
    assert frame["index"] == 2 and frame["done"] == 5
    assert "pending" in frame


def test_queue_state_labels_a_queued_operation_before_it_starts():
    """Before anything starts, the job's `kind` is still the default and would
    mislabel what is waiting — so it falls back to the head of the queue."""
    job = jobs.Job("j", "p")
    job.enqueue([(1, False, "translate")])

    assert job.queue_state()["kind"] == "translate"
    assert job.queue_state()["current"] is None


# ---- bookkeeping -------------------------------------------------------------

def test_finished_jobs_are_evicted_to_bound_memory():
    for i in range(jobs._MAX_JOBS + 20):
        job = jobs.Job(f"j{i}", "p")
        job.done = True
        jobs._jobs[job.id] = job
    jobs._evict_finished_jobs()

    assert len(jobs._jobs) <= jobs._KEEP_FINISHED + 1


def test_a_running_job_is_never_evicted():
    running = jobs.Job("alive", "p")
    for i in range(jobs._MAX_JOBS + 20):
        finished = jobs.Job(f"j{i}", "p")
        finished.done = True
        jobs._jobs[finished.id] = finished
    jobs._jobs[running.id] = running
    jobs._evict_finished_jobs()

    assert "alive" in jobs._jobs


def test_a_finished_job_is_no_longer_the_projects_active_one():
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))

    assert jobs.active_job(pid) is None


def test_running_jobs_lists_only_live_ones():
    pid, cfg = _project(1)
    assert jobs.running_jobs() == []
    asyncio.run(_drain(pid, cfg, _items("prepare", 1)))
    assert jobs.running_jobs() == []


def test_pending_can_be_read_from_another_thread_while_the_worker_mutates_it():
    """Iterating a deque that changes size raises RuntimeError, which surfaced as
    intermittent 500s that blanked the dashboard. Every touch goes through helpers
    that hold the lock."""
    import threading

    job = jobs.Job("j", "p")
    stop = threading.Event()
    errors_seen: list[BaseException] = []

    def reader():
        while not stop.is_set():
            try:
                job.snapshot_pending()
                job.queue_state()
            except BaseException as exc:  # noqa: BLE001
                errors_seen.append(exc)
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(3000):
            job.enqueue([(i, False, "prepare")])
            job.take_next()
    finally:
        stop.set()
        t.join()

    assert not errors_seen, f"a concurrent reader raised: {errors_seen[0]!r}"
