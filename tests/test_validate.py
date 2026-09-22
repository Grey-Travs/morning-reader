"""Tests for the post-translation checks.

Two things here are decisions rather than mechanics, and both are pinned:

* the length-ratio band has not been re-derived for Japanese, so it WARNS instead of
  failing chapters against a number borrowed from Korean;
* nothing in validation ever edits prose. It decides whether a human should look.

Japanese fixtures are invented for these tests.
"""

from __future__ import annotations

import pytest

from morning.chapters import Chapter
from morning.config import ValidationConfig
from morning.glossary import Glossary, GlossaryEntry
from morning.validate import validate_translation

JA_A = "電車はまだ来ない。彼女はホームの端に立っていた。"
JA_B = "「遅いね」と彼は言った。"
LEAK = "彼女は答えなかった。"

GOOD = ("The train still had not come. She stood at the end of the platform.\n\n"
        "“Late, isn’t it,” he said.")


def _chapter(*paragraphs: str) -> Chapter:
    return Chapter(index=1, title="第1話",
                   paragraphs=list(paragraphs) or [JA_A, JA_B])


def _validate(translation: str, chapter: Chapter | None = None, **cfg_kw):
    return validate_translation(chapter or _chapter(), translation,
                                ValidationConfig(**cfg_kw))


# ---- the happy path ----------------------------------------------------------

def test_a_faithful_translation_passes():
    result = _validate(GOOD)

    assert result.ok is True
    assert result.failures == []


def test_the_metrics_are_recorded_on_every_chapter():
    """Especially length_ratio: it is exactly the data needed to re-derive the band,
    so it has to be collected from day one rather than after someone decides to."""
    result = _validate(GOOD)

    assert result.metrics["source_paragraphs"] == 2
    assert result.metrics["output_paragraphs"] == 2
    assert result.metrics["length_ratio"] > 0
    assert result.metrics["source_chars"] > 0
    assert "output_source_fraction" in result.metrics


def test_the_result_serialises():
    """It is stored in state.json and sent to the browser."""
    import json

    json.dumps(_validate(GOOD).as_dict())


# ---- the uncalibrated band ---------------------------------------------------

def test_an_out_of_band_ratio_only_warns_while_the_band_is_uncalibrated():
    """Night Reader's 1.6-3.2 is derived from Korean/English pairs. Letting it FAIL
    Japanese chapters would reject good translations for a reason that has nothing to
    do with them."""
    result = _validate("Short.")

    assert result.ok is True
    assert any("length ratio" in w for w in result.warnings)
    assert any("not been re-derived for this language pair" in w
               for w in result.warnings)


def test_the_same_ratio_fails_once_the_band_is_calibrated():
    """The check is kept either way — the point is only that it does not get to
    condemn a chapter on a borrowed number until someone has done the work."""
    result = _validate("Short.", ratio_band_calibrated=True)

    assert result.ok is False
    assert any("length ratio" in f for f in result.failures)


def test_an_over_long_translation_is_called_embellishment():
    result = _validate("word " * 400, ratio_band_calibrated=True)

    assert any("embellishment" in f for f in result.failures)


def test_a_source_with_no_characters_skips_the_length_check():
    """Rather than dividing by zero, or reporting a ratio of 0 as an omission."""
    result = _validate("Some English.", _chapter("   "))

    assert any("no counted characters" in w for w in result.warnings)
    assert not any("length ratio" in f for f in result.failures)


# ---- untranslated source -----------------------------------------------------

def test_a_wholesale_untranslated_chapter_is_failed():
    result = _validate(JA_A + "\n\n" + JA_B)

    assert result.ok is False
    assert any("substantial untranslated" in f for f in result.failures)


def test_a_stray_leaked_sentence_is_failed_with_a_different_message():
    """Phrased differently on purpose: a chapter that is largely Japanese was not
    translated at all, whereas a stray run is one line to go and look at. Sending the
    reader hunting for a bad line in a chapter that has no good lines is unhelpful."""
    english = ("She stood at the end of the platform, watching the rails. " * 12)
    result = _validate(english + LEAK)

    assert result.ok is False
    assert not any("substantial untranslated" in f for f in result.failures)
    assert any("Untranslated Japanese may remain" in f for f in result.failures)


def test_the_leak_findings_say_where_to_look():
    english = ("She stood at the end of the platform, watching the rails. " * 12)
    result = _validate(english + "\n\n" + LEAK)

    assert result.leak_findings
    assert result.leak_findings[0]["text"] == LEAK
    assert result.leak_findings[0]["line"] == 3


def test_a_glossary_term_is_not_reported_as_untranslated():
    """The allow-list. A term the reader chose to keep is intentional by definition."""
    kept = "風は知っている"   # invented title, a predication
    english = f"She was rereading {kept} that winter. " * 10
    glossary = Glossary([GlossaryEntry(source=kept, english="The Wind Knows")])

    assert validate_translation(_chapter(), english, ValidationConfig()).ok is False
    assert validate_translation(_chapter(), english, ValidationConfig(),
                                glossary=glossary).ok is True


def test_kept_quote_marks_and_honorifics_do_not_count_as_untranslated():
    """A finished English chapter that keeps 「」 for dialogue must not read as
    untranslated — that would re-queue every finished chapter, at real cost."""
    result = _validate("「Late, isn’t it,」 said Tanaka-san. " * 6)

    assert not any("untranslated" in f for f in result.failures)


# ---- model chatter -----------------------------------------------------------

def test_chatter_mixed_into_real_prose_fails_rather_than_being_deleted():
    """strip_meta refuses to delete a block holding real sentences on both sides of
    the chatter. Something still has to tell a human, and this is it."""
    translation = ("She stood at the end of the platform. Let me redo this bit. "
                   "The train did not come. " * 6)
    result = _validate(translation)

    assert result.ok is False
    assert any("could not be removed safely" in f for f in result.failures)


def test_a_standalone_chatter_block_is_removed_and_merely_noted():
    translation = f"Here is the translation:\n\n{GOOD}"
    result = _validate(translation)

    assert result.ok is True
    assert any("model commentary" in w for w in result.warnings)


def test_validation_never_edits_the_translation():
    """It decides whether a human should look, and nothing more. Every edit to prose
    is somewhere else, and deliberately conservative."""
    translation = f"Here is the translation:\n\n{GOOD}"
    before = translation
    _validate(translation)

    assert translation == before


# ---- structural checks -------------------------------------------------------

def test_a_missing_scene_fails_the_paragraph_check():
    source = _chapter(*([JA_A] * 20))
    result = _validate("One paragraph only.", source)

    assert any("paragraph count" in f for f in result.failures)


def test_a_minor_formatting_merge_is_tolerated():
    """The tolerance scales with length, so two merged paragraphs in a long chapter do
    not flag while a missing scene still does."""
    source = _chapter(*([JA_A] * 40))
    translation = "\n\n".join(["English paragraph here."] * 39)
    result = _validate(translation, source, ratio_band_calibrated=False)

    assert not any("paragraph count" in f for f in result.failures)


def test_a_dialogue_mismatch_warns_rather_than_fails():
    """A secondary signal. Failing on it would send good chapters to review over a
    line the translator merged."""
    source = _chapter(*([JA_B] * 10))
    translation = "\n\n".join(["Plain narration, no dialogue."] * 10)
    result = _validate(translation, source)

    assert any("dialogue lines" in w for w in result.warnings)
    assert not any("dialogue lines" in f for f in result.failures)


# ---- surface conventions -----------------------------------------------------

@pytest.mark.parametrize("translation,expected", [
    ("She paused.. then went on. " * 8, "not exactly three dots"),
    ("She paused… then went on. " * 8, "single-character ellipsis"),
    ("「Late,」 he said. " * 10, "「」 remain"),
    ('"Late," he said. ' * 10, "curly quotes"),
])
def test_surface_conventions_warn(translation, expected):
    """Warnings only. None of these makes a translation wrong, and failing a good
    chapter over a punctuation mark trains the reader to ignore the review queue."""
    result = _validate(translation)

    assert any(expected in w for w in result.warnings), result.warnings
    assert result.failures == [] or all(expected not in f for f in result.failures)


def test_a_clean_translation_raises_no_surface_warnings():
    assert _validate(GOOD).warnings == []
