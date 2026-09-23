"""Tests for one chapter end to end.

The safety property these exist for: **a chapter that validates is written to
chapters/ and read; a chapter that does not is written ONLY to its audit copy.** A
questionable translation must never be mistaken for a finished one, and the audit copy
must exist regardless so the prose is never lost.

No test here makes a real model call — the translator is a stand-in returning canned
results, so the pipeline's decisions are exercised at no cost. Japanese fixtures are
invented.
"""

from __future__ import annotations

import pytest

from morning.chapter_files import read_audit_translation, read_chapter
from morning.chapters import Chapter
from morning.config import Config
from morning.glossary import Glossary, GlossaryEntry, load_pending
from morning.pipeline import process_chapter, queue_new_terms
from morning.state import (
    STATUS_EMPTY, STATUS_ENGLISH, STATUS_NEEDS_REVIEW, STATUS_VALIDATED, State,
)
from morning.translator import TranslationResult

JA_A = "電車はまだ来ない。彼女はホームの端に立っていた。"
JA_B = "「遅いね」と彼は言った。"
LEAK = "彼女は答えなかった。"

GOOD = ("The train still had not come. She stood at the end of the platform.\n\n"
        "“Late, isn’t it,” he said.")

# A translation that matches _long_chapter() paragraph for paragraph, so it actually
# passes. A SHORT "good" result against a long source fails the paragraph check just as
# surely as a bad one does — correct behaviour, but useless as a fixture for testing
# that a retry succeeded.
GOOD_LONG = "\n\n".join(
    ["The train still had not come. She stood at the end of the platform."] * 20)


class FakeTranslator:
    """Returns canned results. Records what it was asked, so a test can assert on the
    retry hint and the glossary block without a model being involved."""

    def __init__(self, *results: TranslationResult):
        self.results = list(results)
        self.calls: list[dict] = []

    def translate_chapter(self, chapter, *, glossary_block="", hooks=None,
                          retry_hint="", spend=None):
        self.calls.append({"glossary_block": glossary_block,
                           "retry_hint": retry_hint, "chapter": chapter})
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if hooks is not None:
            hooks.source(chapter.paragraphs)
        return result


def _result(english: str, *, terms=None, cost=0.01, usage=None) -> TranslationResult:
    return TranslationResult(english=english, new_terms=list(terms or []),
                             usage=dict(usage or {"input_tokens": 10}),
                             cost_usd=cost, chunks=1)


@pytest.fixture
def project(tmp_path):
    cfg = Config()
    cfg.paths.output_dir = tmp_path / "chapters"
    cfg.paths.audit_dir = tmp_path / "audit"
    cfg.paths.glossary_json = tmp_path / "glossary.json"
    cfg.paths.glossary_pending = tmp_path / "glossary_pending.json"
    return cfg


def _chapter(*paragraphs: str) -> Chapter:
    return Chapter(index=1, title="第1話",
                   paragraphs=list(paragraphs) or [JA_A, JA_B])


def _long_chapter() -> Chapter:
    """A chapter long enough that a one-paragraph translation genuinely fails.

    Needed because the ratio band is uncalibrated and therefore only WARNS — so a
    short translation of a SHORT chapter passes, correctly. The paragraph-count check
    is the one that still bites, and it is the honest way to make a failing case.
    """
    return _chapter(*([JA_A] * 20))


def _run(cfg, translator, chapter=None, glossary=None, state=None, hooks=None):
    return process_chapter(chapter or _chapter(), 10, translator,
                           glossary or Glossary(), cfg, state or State(), hooks=hooks)


# ---- chapters that never reach the model -------------------------------------

def test_an_empty_chapter_costs_nothing(project):
    translator = FakeTranslator(_result(GOOD))
    state = State()

    blank = Chapter(index=1, title="第1話", paragraphs=[])
    outcome = _run(project, translator, blank, state=state)

    assert outcome.status == STATUS_EMPTY
    assert translator.calls == [], "a blank chapter must not be sent to the model"
    assert state.get(1)["status"] == STATUS_EMPTY
    assert state.get(1)["source_hash"]


def test_a_whitespace_only_paragraph_is_english_not_empty(project):
    """Surprising enough to pin: a chapter is EMPTY only when it has no paragraphs at
    all. One paragraph of whitespace is a paragraph containing no Japanese, which is
    the already-English case."""
    translator = FakeTranslator(_result(GOOD))

    assert _run(project, translator, _chapter("   ")).status == STATUS_ENGLISH


def test_an_already_english_chapter_costs_nothing(project):
    """Translating English into English spends the allowance for nothing."""
    translator = FakeTranslator(_result(GOOD))
    english = _chapter("The train still had not come.", "She waited on the platform.")

    outcome = _run(project, translator, english)

    assert outcome.status == STATUS_ENGLISH
    assert translator.calls == []


def test_an_english_chapter_is_translated_when_skipping_is_off(project):
    project.translation.skip_non_source = False
    translator = FakeTranslator(_result(GOOD))

    outcome = _run(project, translator,
                   _chapter("The train still had not come. She waited."))

    assert translator.calls, "with skipping off it should be translated"
    assert outcome.status in (STATUS_VALIDATED, STATUS_NEEDS_REVIEW)


# ---- a translation that passes ------------------------------------------------

def test_a_good_translation_is_written_and_recorded(project):
    state = State()
    translator = FakeTranslator(_result(GOOD))

    outcome = _run(project, translator, state=state)

    assert outcome.status == STATUS_VALIDATED
    assert read_chapter(project.paths.output_dir, 1, 10).strip() == GOOD
    assert read_audit_translation(project.paths.audit_dir, 1, 10) == GOOD

    record = state.get(1)
    assert record["status"] == STATUS_VALIDATED
    assert record["failures"] == []
    assert record["validation"]["length_ratio"] > 0
    assert record["chunks"] == 1
    assert record["cost_usd"] == 0.01
    assert record["usage"]["input_tokens"] == 10


def test_the_glossary_block_is_handed_to_the_translator(project):
    glossary = Glossary([GlossaryEntry(source="ホーム", english="platform",
                                       type="term")])
    translator = FakeTranslator(_result(GOOD))

    _run(project, translator, glossary=glossary)

    assert "platform" in translator.calls[0]["glossary_block"]


# ---- a translation that fails -------------------------------------------------

def test_a_failed_translation_is_not_written_to_chapters(project):
    """The safety property. A questionable translation must never be mistaken for a
    finished one."""
    state = State()
    translator = FakeTranslator(_result("Too short."))

    outcome = _run(project, translator, state=state,
                   chapter=_long_chapter())

    assert outcome.status == STATUS_NEEDS_REVIEW
    assert read_chapter(project.paths.output_dir, 1, 10) is None
    assert state.get(1)["failures"]


def test_a_failed_translation_is_still_kept_in_the_audit(project):
    """It is the ONLY copy — that is what the review screen reads and what Accept
    later promotes. Without it the prose the user paid for is gone."""
    translator = FakeTranslator(_result("Too short."))

    _run(project, translator, chapter=_long_chapter())

    assert read_audit_translation(project.paths.audit_dir, 1, 10) == "Too short."


def test_the_audit_records_why_it_failed(project):
    translator = FakeTranslator(_result("Too short."))

    _run(project, translator, chapter=_long_chapter())

    text = (project.paths.audit_dir / "chapter-01.md").read_text(encoding="utf-8")
    assert "paragraph count" in text


def test_untranslated_source_fails_the_chapter(project):
    english = "She stood at the end of the platform, watching the rails. " * 12
    translator = FakeTranslator(_result(english + LEAK))

    outcome = _run(project, translator)

    assert outcome.status == STATUS_NEEDS_REVIEW
    assert any("Untranslated Japanese" in f for f in outcome.failures)


# ---- retrying -----------------------------------------------------------------

def test_a_failed_attempt_is_retried_and_the_good_one_kept(project):
    state = State()
    translator = FakeTranslator(_result("Too short.", cost=0.01),
                                _result(GOOD_LONG, cost=0.02))

    outcome = _run(project, translator, state=state, chapter=_long_chapter())

    assert outcome.status == STATUS_VALIDATED
    assert outcome.attempts == 2
    assert read_chapter(project.paths.output_dir, 1, 10).strip() == GOOD_LONG


def test_the_retry_is_told_why_the_first_attempt_failed(project):
    """An identical prompt tends to produce an identical answer, so a retry that says
    nothing is money spent to get the same result back."""
    translator = FakeTranslator(_result("Too short."), _result(GOOD_LONG))

    _run(project, translator, chapter=_long_chapter())

    assert translator.calls[0]["retry_hint"] == ""
    assert "fidelity check" in translator.calls[1]["retry_hint"]


def test_every_attempt_is_billed_so_every_attempt_is_counted(project):
    state = State()
    translator = FakeTranslator(
        _result("Too short.", cost=0.01, usage={"input_tokens": 10}),
        _result(GOOD_LONG, cost=0.02, usage={"input_tokens": 20}))

    _run(project, translator, state=state, chapter=_long_chapter())

    record = state.get(1)
    assert record["cost_usd"] == 0.03
    assert record["usage"]["input_tokens"] == 30


def test_the_better_attempt_is_kept_when_neither_passes(project):
    """A second attempt can be worse than the first. Silently preferring the newest
    would throw away the better translation."""
    worse = "x"
    better = "She stood at the end of the platform, watching the rails go by."
    translator = FakeTranslator(_result(better), _result(worse))

    _run(project, translator, chapter=_long_chapter())

    assert read_audit_translation(project.paths.audit_dir, 1, 10) == better


def test_retrying_can_be_switched_off(project):
    project.translation.validation_retries = 0
    translator = FakeTranslator(_result("Too short."), _result(GOOD_LONG))

    outcome = _run(project, translator, chapter=_long_chapter())

    assert outcome.attempts == 1
    assert len(translator.calls) == 1


def test_a_live_view_is_told_to_discard_what_it_showed_on_a_retry(project):
    """The whole chapter is being redone, so a view that kept the first attempt would
    show two translations stacked on top of each other."""
    from morning.translator import StreamHooks

    resets: list[str] = []
    translator = FakeTranslator(_result("Too short."), _result(GOOD_LONG))

    _run(project, translator, chapter=_long_chapter(),
         hooks=StreamHooks(on_reset=resets.append))

    assert resets == ["retry"]


# ---- new terms ----------------------------------------------------------------

def test_terms_from_a_good_chapter_are_queued_not_committed(project):
    """A wrong reading propagates to every later chapter and is expensive to unpick,
    and Japanese makes wrong readings easy. The human gate is the point."""
    glossary = Glossary()
    translator = FakeTranslator(_result(GOOD, terms=[
        {"source": "佐々木", "english": "Sasaki", "type": "name"}]))

    outcome = _run(project, translator, glossary=glossary)

    assert outcome.new_terms_queued == 1
    assert len(glossary) == 0, "nothing may reach the glossary without approval"
    assert load_pending(project.paths.glossary_pending)[0]["source"] == "佐々木"


def test_terms_are_not_harvested_from_a_chapter_that_failed(project):
    """Otherwise the review queue fills with readings taken from prose nobody has
    accepted."""
    translator = FakeTranslator(_result("Too short.", terms=[
        {"source": "佐々木", "english": "Sasaki"}]))

    outcome = _run(project, translator, chapter=_long_chapter())

    assert outcome.new_terms_queued == 0
    assert load_pending(project.paths.glossary_pending) == []


def test_a_term_already_known_is_not_queued_again(project):
    glossary = Glossary([GlossaryEntry(source="佐々木", english="Sasaki")])

    added = queue_new_terms(project, glossary,
                            [{"source": "佐々木", "english": "Sasaki"}])

    assert added == 0


def test_queueing_nothing_writes_nothing(project):
    assert queue_new_terms(project, Glossary(), []) == 0
    assert not project.paths.glossary_pending.exists()


# ---- state --------------------------------------------------------------------

def test_the_state_record_is_serialisable(project):
    """It is written to state.json and sent to the browser."""
    import json

    state = State()
    _run(project, FakeTranslator(_result(GOOD)), state=state)

    json.dumps(state.chapters)


def test_the_source_hash_is_recorded_so_the_chapter_is_resumable(project):
    state = State()
    chapter = _chapter()

    _run(project, FakeTranslator(_result(GOOD)), chapter=chapter, state=state)

    assert state.get(1)["source_hash"] == chapter.metrics.content_hash
    assert state.is_done(1, chapter.metrics.content_hash, "translate")


def test_the_pipeline_does_not_save_state_itself(project, tmp_path):
    """The caller owns persistence — it is the one holding the lock and merging
    against concurrent edits to other chapters."""
    state = State()
    _run(project, FakeTranslator(_result(GOOD)), state=state)

    assert not project.paths.state_file.exists()
