"""Calls that completed were billed, whatever happened next.

The accumulators that tracked spend were FRAME LOCALS — ``total_usage``/``total_cost``
inside ``pipeline.translate_with_retry``, ``usage``/``cost`` inside
``Translator.translate_chapter``, and the ``ScriptResult`` inside
``manga.translate_script`` — committed onto the result only at the very end. Any raise
between a completed call and that commit unwound straight past them.

Three ordinary events sit in exactly that window:

* a chapter fails its checks, the retry starts, and the rate limit lands;
* the owner presses Stop between the two attempts;
* a long chapter translates in four calls and the connection drops on the fourth.

In each case the completed calls had already been charged to the owner's plan, and the
record showed nothing. The worker then re-ran the chapter from scratch and the books
never knew about the first round at all.

The chapter is still re-translated after an interruption — the chapter is the unit of
resumability and that is by design. What must not happen is the spend going unrecorded.
"""

from __future__ import annotations

import pytest

from morning.chapters import Chapter
from morning.config import Config
from morning.exceptions import RateLimited, RateLimitInfo, TaskAborted
from morning.glossary import Glossary
from morning.spend import Spend
from morning.state import State
from morning.translator import TranslationResult
from server import pages as pages_mod, projects as pj, tasks as task_mod

LONG = ["これは日本語の本文です。" * 12] * 4


def _chapter(index: int = 1) -> Chapter:
    return Chapter(index=index, title="第1話", paragraphs=list(LONG))


def _cfg(tmp_path) -> Config:
    """A config whose every path is inside tmp_path.

    A bare ``Config()`` has RELATIVE defaults — "chapters", "audit", "state.json",
    "glossary_pending.json" — so it resolves against the working directory, which for
    a test run is the repo. `process_chapter` really does write chapters and audit
    copies, so a test holding a default Config writes them into the source tree. It
    did, and they were committed.
    """
    cfg = Config()
    cfg.paths.output_dir = tmp_path / "chapters"
    cfg.paths.audit_dir = tmp_path / "audit"
    cfg.paths.state_file = tmp_path / "state.json"
    cfg.paths.glossary_json = tmp_path / "glossary.json"
    cfg.paths.glossary_md = tmp_path / "glossary.md"
    cfg.paths.glossary_pending = tmp_path / "glossary_pending.json"
    return cfg


def _ctx(translator, tmp_path, pid: str = "") -> task_mod.TaskContext:
    return task_mod.TaskContext(
        cfg=_cfg(tmp_path), state=State(), total=1, pid=pid,
        glossary=Glossary(), translator=translator)


def _rate_limited() -> RateLimited:
    return RateLimited(info=RateLimitInfo(resets_at=None),
                       message="the plan's window is exhausted")


# ---- the accumulator itself ---------------------------------------------------

class TestSpend:
    def test_it_adds_rather_than_assigns(self):
        spend = Spend()
        spend.add({"input_tokens": 10}, 0.01)
        spend.add({"input_tokens": 5, "output_tokens": 2}, 0.02)

        assert spend.usage == {"input_tokens": 15, "output_tokens": 2}
        assert spend.cost_usd == pytest.approx(0.03)

    def test_an_empty_one_is_falsey(self):
        """So a caller can write `if spend:` and not leave a misleading zero-cost
        entry on a chapter nothing was spent on."""
        assert not Spend()
        assert Spend(cost_usd=0.01)
        assert Spend(usage={"input_tokens": 1})

    def test_non_numbers_are_ignored(self):
        spend = Spend()
        spend.add({"model": "opus", "flag": True, "input_tokens": 3}, None)
        assert spend.usage == {"input_tokens": 3}


# ---- the prose path -----------------------------------------------------------

class _BillsThenRaises:
    """Bills a real call, then raises on the next one — the retry window."""

    def __init__(self, exc, cost: float = 0.05):
        self.exc = exc
        self.cost = cost
        self.calls = 0
        from morning.config import TranslationConfig
        self.tcfg = TranslationConfig()

    def translate_chapter(self, chapter, *, glossary_block="", hooks=None,
                          retry_hint="", spend=None):
        self.calls += 1
        if self.calls == 1:
            if spend is not None:
                spend.add({"input_tokens": 100}, self.cost)
            return TranslationResult(english="A first attempt.",
                                     usage={"input_tokens": 100},
                                     cost_usd=self.cost, chunks=1)
        raise self.exc


@pytest.fixture
def always_fails_validation(monkeypatch):
    """Force the retry, deterministically. What makes a chapter fail its checks is a
    separate question with its own tests; here it only has to happen."""
    from morning import pipeline
    from morning.validate import ValidationResult

    monkeypatch.setattr(
        pipeline, "validate_translation",
        # `ok` is a plain field defaulting to True, NOT derived from `failures` —
        # so it has to be said explicitly or this fixture forces nothing.
        lambda *a, **k: ValidationResult(ok=False, failures=["forced"], warnings=[],
                                         metrics={}, leak_findings=[]))


class TestTheProseRetryWindow:
    def test_a_rate_limit_during_the_retry_still_records_the_first_attempt(
            self, always_fails_validation, tmp_path):
        translator = _BillsThenRaises(_rate_limited())
        ctx = _ctx(translator, tmp_path)

        with pytest.raises(RateLimited):
            task_mod.translate_chapter(_chapter(), ctx)

        record = ctx.state.get(1) or {}
        assert record.get("cost_usd") == pytest.approx(0.05)
        assert record.get("usage") == {"input_tokens": 100}

    def test_a_stop_during_the_retry_still_records_it(self, always_fails_validation,
                                                      tmp_path):
        translator = _BillsThenRaises(TaskAborted("stopped"))
        ctx = _ctx(translator, tmp_path)

        with pytest.raises(TaskAborted):
            task_mod.translate_chapter(_chapter(), ctx)

        assert (ctx.state.get(1) or {}).get("cost_usd") == pytest.approx(0.05)

    def test_an_ordinary_failure_during_the_retry_records_it_too(
            self, always_fails_validation, tmp_path):
        translator = _BillsThenRaises(RuntimeError("the connection dropped"))
        ctx = _ctx(translator, tmp_path)

        with pytest.raises(RuntimeError):
            task_mod.translate_chapter(_chapter(), ctx)

        assert (ctx.state.get(1) or {}).get("cost_usd") == pytest.approx(0.05)

    def test_a_chapter_that_never_billed_records_nothing(self, tmp_path):
        """`if spend:` — a chapter whose very first call failed must not get a
        misleading zero-cost entry."""
        class _RaisesImmediately(_BillsThenRaises):
            def translate_chapter(self, chapter, **kw):
                raise self.exc

        ctx = _ctx(_RaisesImmediately(RuntimeError("cold start")), tmp_path)

        with pytest.raises(RuntimeError):
            task_mod.translate_chapter(_chapter(), ctx)

        assert "cost_usd" not in (ctx.state.get(1) or {})

    def test_the_success_path_is_not_double_counted(self, tmp_path):
        """`process_chapter` already credits state on the way out. The accumulator
        must not add a second copy."""
        class _Succeeds(_BillsThenRaises):
            def translate_chapter(self, chapter, *, glossary_block="", hooks=None,
                                  retry_hint="", spend=None):
                self.calls += 1
                if spend is not None:
                    spend.add({"input_tokens": 100}, 0.05)
                return TranslationResult(english="Fine.", usage={"input_tokens": 100},
                                         cost_usd=0.05, chunks=1)

        translator = _Succeeds(None)
        ctx = _ctx(translator, tmp_path)
        task_mod.translate_chapter(_chapter(), ctx)

        # Once per CALL, which is the real invariant. This prose fails its checks, so
        # it is legitimately attempted twice — asserting a flat 0.05 would have been
        # asserting that the retry never happened.
        assert (ctx.state.get(1) or {}).get("cost_usd") == pytest.approx(
            0.05 * translator.calls)


class TestChunks:
    def test_completed_chunks_survive_a_failure_on_a_later_one(self):
        """A long chapter is several calls. Three completed and billed, the fourth
        drops — and the three used to vanish with the frame."""
        from morning.translator import Translator

        class _ThreeThenDrop:
            def __init__(self):
                self.calls = 0

            def _call(self, system_text, user_text, hooks=None, **kw):
                self.calls += 1
                if self.calls > 3:
                    raise RuntimeError("the connection dropped")
                return "part", {"input_tokens": 10}, 0.02

        translator = Translator.__new__(Translator)
        from morning.config import AnthropicConfig, TranslationConfig
        translator.cfg = AnthropicConfig()
        translator.tcfg = TranslationConfig(chunk_threshold=40)
        translator._call = _ThreeThenDrop()._call

        spend = Spend()
        chapter = Chapter(index=1, title="t", paragraphs=["日本語です。" * 10] * 8)

        with pytest.raises(RuntimeError):
            translator.translate_chapter(chapter, spend=spend)

        assert spend.cost_usd == pytest.approx(0.06)
        assert spend.usage == {"input_tokens": 30}


# ---- the manga path -----------------------------------------------------------

class TestTheMangaRetryWindow:
    @pytest.fixture
    def manga_project(self):
        from morning.pageread import KIND_BUBBLE, Region

        pid = pj.create_project("m", kind=pj.KIND_MANGA,
                                ingest=pj.INGEST_IMAGES)["id"]
        regions = [Region(id=f"r{i}", box=(0.1, 0.1 + i * 0.2, 0.2, 0.15),
                          text=f"セリフ{i}", kind=KIND_BUBBLE, order=i).to_dict()
                   for i in range(4)]
        with pages_mod.mutate_pages(pid) as doc:
            doc["pages"] = [{
                "id": "aaaa1111", "seq": 1, "status": "ok",
                "width": 1600, "height": 2400, "join_prev": "",
                "read": {"width": 1600, "height": 2400, "regions": regions,
                         "order_source": "model", "meta": {"confidence": "high"}}}]
            doc["chapters"] = [{"index": 1, "title": "第1話", "start_seq": 1,
                                "end_seq": 1, "page_ids": ["aaaa1111"],
                                "status": ""}]
        return pid

    @staticmethod
    def _chapter_record(pid):
        return pages_mod.load_pages(pid)["chapters"][0]

    def test_an_interrupted_first_call_still_records_what_it_cost(
            self, manga_project, tmp_path):
        class _Raises:
            def __init__(self):
                from morning.config import TranslationConfig
                self.tcfg = TranslationConfig()

            def _call(self, *a, **kw):
                raise _rate_limited()

        ctx = _ctx(_Raises(), tmp_path, pid=manga_project)
        with pytest.raises(RateLimited):
            task_mod.translate_script_task(self._chapter_record(manga_project), ctx)

        # Nothing completed, so nothing is charged — and no misleading entry appears.
        assert pages_mod.load_pages(manga_project)["totals"]["cost_usd"] == 0.0

    def test_an_interrupted_retry_still_records_the_paid_first_attempt(
            self, manga_project, tmp_path):
        """The first attempt is complete, billed, and holds real English. It used to
        go with the frame, and the whole chapter was bought again from nothing."""
        from morning.prompts import NEW_TERMS_DELIMITER

        class _PartialThenRaise:
            def __init__(self):
                from morning.config import TranslationConfig
                self.tcfg = TranslationConfig()
                self.calls = 0

            def _call(self, system_text, user_text, hooks=None, **kw):
                self.calls += 1
                if self.calls == 1:
                    # Only one of four lines, so `should_retry` fires.
                    return (f"1:r0\tAoi\tOnly this one.\n{NEW_TERMS_DELIMITER}\n[]",
                            {"input_tokens": 100}, 0.07)
                raise _rate_limited()

        ctx = _ctx(_PartialThenRaise(), tmp_path, pid=manga_project)
        with pytest.raises(RateLimited):
            task_mod.translate_script_task(self._chapter_record(manga_project), ctx)

        assert pages_mod.load_pages(manga_project)["totals"]["cost_usd"] == \
            pytest.approx(0.07)

    def test_the_retry_merge_keeps_what_the_second_attempt_found(
            self, manga_project, tmp_path):
        """The merge copied the retry's lines, usage, cost and warnings but dropped
        its new terms and its call count."""
        from morning.prompts import NEW_TERMS_DELIMITER

        class _PartialThenRest:
            def __init__(self):
                from morning.config import TranslationConfig
                self.tcfg = TranslationConfig()
                self.calls = 0

            def _call(self, system_text, user_text, hooks=None, **kw):
                self.calls += 1
                if self.calls == 1:
                    return (f"1:r0\tAoi\tOne.\n{NEW_TERMS_DELIMITER}\n[]",
                            {"input_tokens": 10}, 0.01)
                rows = "\n".join(f"1:r{i}\t\tLine {i}." for i in range(4))
                terms = '[{"source": "葵", "english": "Aoi", "type": "name"}]'
                return (f"{rows}\n{NEW_TERMS_DELIMITER}\n{terms}",
                        {"input_tokens": 10}, 0.01)

        ctx = _ctx(_PartialThenRest(), tmp_path, pid=manga_project)
        fields = task_mod.translate_script_task(
            self._chapter_record(manga_project), ctx)

        # Two calls were made, and the chapter says so.
        assert fields["chapter"]["chunks"] == 2
        assert fields["chapter"]["attempts"] == 2
