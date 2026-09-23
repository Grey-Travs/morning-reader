"""Translation through the job queue.

The pipeline is tested on its own; this is about the WIRING — that a translate queued
through the API reaches ``process_chapter`` with a usable context, that its spend is
counted exactly once, and that it inherits the spine (Stop, resumability, the live
console) rather than reimplementing it.

No test here makes a real model call: ``server.jobs.Translator`` is replaced with a
stand-in, which is the same seam the worker builds through.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from morning.chapter_files import read_audit_translation, read_chapter
from morning.chapters import Chapter
from morning.config import Config
from morning.glossary import Glossary, GlossaryEntry, load_pending
from morning.state import (
    STATUS_ENGLISH, STATUS_NEEDS_REVIEW, STATUS_VALIDATED, State,
)
from morning.translator import TranslationResult
from server import jobs, projects as pj, tasks as task_mod

JA = "電車はまだ来ない。彼女はホームの端に立っていた。"
EN = "The train still had not come. She stood at the end of the platform."


class FakeTranslator:
    """Stands in for the real one at the seam the worker builds through."""

    instances: list["FakeTranslator"] = []

    def __init__(self, *_a, **_kw):
        self.calls: list[Chapter] = []
        FakeTranslator.instances.append(self)

    def translate_chapter(self, chapter, *, glossary_block="", hooks=None,
                          retry_hint="", spend=None):
        self.calls.append(chapter)
        if hooks is not None:
            hooks.source(chapter.paragraphs)
            hooks.chunk(1, 1)
            hooks.text(EN)
        return TranslationResult(
            english="\n\n".join([EN] * len(chapter.paragraphs)),
            new_terms=[{"source": "ホーム", "english": "platform",
                        "type": "term"}],
            usage={"input_tokens": 100, "output_tokens": 50},
            cost_usd=0.05, chunks=1)


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    FakeTranslator.instances = []
    monkeypatch.setattr(jobs, "Translator", FakeTranslator)
    yield


def _project(n: int = 2, *, english: bool = False) -> tuple[str, Config]:
    project = pj.create_project("朝の駅")
    body = "The train still had not come." if english else JA
    pj.save_source(project["id"], [
        Chapter(index=i, title=f"第{i}話", paragraphs=[body, body])
        for i in range(1, n + 1)])
    return project["id"], pj.project_config(Config(), project)


async def _drain(pid: str, cfg: Config, items, timeout: float = 10.0) -> jobs.Job:
    result = jobs.enqueue(pid, cfg, items)
    job = jobs.get_job(result["job_id"])
    deadline = time.monotonic() + timeout
    while not job.done and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert job.done, "the worker never finished"
    await asyncio.sleep(0)
    return job


def _translate(*indices: int, force: bool = False):
    return [(i, force, task_mod.TASK_TRANSLATE) for i in indices]


def _events(job: jobs.Job, kind: str) -> list[dict]:
    return [e for e in job.history if e.get("type") == kind]


# ---- the happy path ----------------------------------------------------------

def test_a_queued_translation_writes_the_chapter():
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _translate(1)))

    assert EN in read_chapter(cfg.paths.output_dir, 1, 1)
    assert read_audit_translation(cfg.paths.audit_dir, 1, 1)
    assert State.load(cfg.paths.state_file).get(1)["status"] == STATUS_VALIDATED


def test_the_spend_is_counted_exactly_once():
    """process_chapter writes the record and the spend itself, so the worker must not
    write them again — a second add_usage would double-count every chapter."""
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _translate(1)))

    record = State.load(cfg.paths.state_file).get(1)
    assert record["cost_usd"] == 0.05
    assert record["usage"] == {"input_tokens": 100, "output_tokens": 50}


def test_the_project_total_sums_every_chapter():
    pid, cfg = _project(3)
    asyncio.run(_drain(pid, cfg, _translate(1, 2, 3)))

    assert State.load(cfg.paths.state_file).totals()["cost_usd"] == 0.15


def test_one_translator_is_built_per_job_not_per_chapter():
    """Each one spawns a CLI process. Building one per chapter would add a process
    launch to every item in a sweep."""
    pid, cfg = _project(3)
    asyncio.run(_drain(pid, cfg, _translate(1, 2, 3)))

    assert len(FakeTranslator.instances) == 1
    assert len(FakeTranslator.instances[0].calls) == 3


def test_new_terms_reach_the_pending_queue_not_the_glossary():
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _translate(1)))

    assert load_pending(cfg.paths.glossary_pending)[0]["source"] == "ホーム"
    assert len(Glossary.load(cfg.paths.glossary_json)) == 0


def test_the_projects_glossary_is_loaded_and_offered_to_the_model():
    pid, cfg = _project(1)
    Glossary([GlossaryEntry(source="ホーム", english="platform")]).save(
        cfg.paths.glossary_json)

    asyncio.run(_drain(pid, cfg, _translate(1)))

    # It was loaded, so the term it already knows is not queued a second time.
    assert load_pending(cfg.paths.glossary_pending) == []


# ---- it inherits the spine ---------------------------------------------------

def test_a_translation_is_skipped_once_it_is_done():
    """The resumability contract, on the expensive task — this is the one where
    redoing work costs real money."""
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _translate(1)))
    job = asyncio.run(_drain(pid, cfg, _translate(1)))

    assert _events(job, "item")[0]["skipped"] is True
    assert len(FakeTranslator.instances[-1].calls) == 0


def test_editing_the_source_makes_it_translate_again():
    pid, cfg = _project(1)
    asyncio.run(_drain(pid, cfg, _translate(1)))

    pj.save_source(pid, [Chapter(index=1, title="第1話",
                                 paragraphs=[JA, JA, JA])])
    job = asyncio.run(_drain(pid, cfg, _translate(1)))

    assert not any(e.get("skipped") for e in _events(job, "item"))


def test_an_already_english_chapter_is_not_sent_to_the_model():
    pid, cfg = _project(1, english=True)
    chapters = pj.load_source(pid)
    state = State()

    items = task_mod.resolve_items(chapters, state, task_mod.TASK_TRANSLATE, cfg)
    assert items == [], "a sweep must not queue an English chapter"

    # ...and even when queued explicitly, it costs nothing.
    asyncio.run(_drain(pid, cfg, _translate(1)))
    assert State.load(cfg.paths.state_file).get(1)["status"] == STATUS_ENGLISH
    assert FakeTranslator.instances[0].calls == []


def test_a_translate_is_marked_as_billed_on_the_stream():
    """The free task and the expensive one ride the same queue, so the event has to
    say which is which — the UI warns before starting one that spends money."""
    pid, cfg = _project(1)
    job = asyncio.run(_drain(pid, cfg, _translate(1)))

    assert _events(job, "start")[0]["billed"] is True


def test_prepare_is_not_marked_as_billed():
    pid, cfg = _project(1)
    job = asyncio.run(_drain(pid, cfg, [(1, False, task_mod.TASK_PREPARE)]))

    assert _events(job, "start")[0]["billed"] is False


def test_the_english_is_streamed_to_the_live_console():
    pid, cfg = _project(1)
    frames: list[dict] = []

    async def scenario():
        result = jobs.enqueue(pid, cfg, _translate(1))
        job = jobs.get_job(result["job_id"])
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        deadline = time.monotonic() + 10
        while not job.done and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        while not queue.empty():
            frames.append(queue.get_nowait())

    asyncio.run(scenario())

    assert any(f.get("type") == "delta" and EN in f.get("text", "") for f in frames)
    assert any(f.get("type") == "source" for f in frames)


def test_both_kinds_can_queue_on_one_chapter():
    """Prepare and translate are different questions about the same chapter, so the
    dedup key must not collapse them into one."""
    pid, cfg = _project(1)
    job = jobs.Job("j", pid)

    added = job.enqueue([(1, False, task_mod.TASK_PREPARE),
                         (1, False, task_mod.TASK_TRANSLATE)])

    assert added == [1, 1]
    assert len(job.snapshot_pending()) == 2


# ---- failure paths -----------------------------------------------------------

def test_a_missing_engine_refuses_rather_than_killing_the_queue(monkeypatch):
    """A translate then reports "not applied" with a reason, and any free work in the
    same queue still runs."""
    def no_engine(*_a, **_kw):
        raise RuntimeError("the CLI is not installed")

    monkeypatch.setattr(jobs, "Translator", no_engine)
    pid, cfg = _project(1)

    job = asyncio.run(_drain(pid, cfg, _translate(1)))

    item = _events(job, "item")[0]
    assert item["refused"] is True
    assert _events(job, "done"), "the job must still finish visibly"


def test_a_failed_translation_goes_to_review_without_reaching_chapters(monkeypatch):
    class ShortTranslator(FakeTranslator):
        def translate_chapter(self, chapter, **kw):
            self.calls.append(chapter)
            return TranslationResult(english="Too short.", cost_usd=0.02, chunks=1)

    monkeypatch.setattr(jobs, "Translator", ShortTranslator)
    project = pj.create_project("x")
    pj.save_source(project["id"], [Chapter(index=1, title="t", paragraphs=[JA] * 20)])
    cfg = pj.project_config(Config(), project)

    asyncio.run(_drain(project["id"], cfg, _translate(1)))

    record = State.load(cfg.paths.state_file).get(1)
    assert record["status"] == STATUS_NEEDS_REVIEW
    assert read_chapter(cfg.paths.output_dir, 1, 1) is None
    # ...but the prose is still recoverable, and the spend still recorded.
    assert read_audit_translation(cfg.paths.audit_dir, 1, 1) == "Too short."
    # TWICE the per-call cost: the chapter failed, so it was retried, and both
    # attempts were billed. Under-reporting a retried chapter would make a project's
    # running total quietly wrong in the direction that matters.
    assert record["cost_usd"] == 0.04
    assert record["attempts"] == 2
