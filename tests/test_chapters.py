"""Tests for the Chapter contract and the script measure everything classifies on.

Two things are load-bearing and pinned here:

* ``content_hash`` hashes the PROSE and nothing else, so retitling does not re-bill a
  translation while editing one word does;
* ``source_fraction`` counts Japanese script but NOT Japanese punctuation — an English
  translation that keeps 「」 for dialogue must not read as untranslated, or every
  finished chapter re-queues itself.

All Japanese here is invented for these tests.
"""

from __future__ import annotations

from morning.chapters import (
    KIND_EMPTY, KIND_ENGLISH, KIND_SOURCE, Chapter, chapters_to_records, classify,
    records_to_chapters, strip_invisibles,
)
from morning.japanese import has_japanese, source_fraction

JA_NARRATION = "電車はまだ来ない。"   # the train has not come yet.
JA_DIALOGUE = "「遅いね」と彼は言った。"  # "late, isn't it," he said.
EN_DIALOGUE = "「Late, is it not,」 he said."


# ---- the script measure ------------------------------------------------------

def test_japanese_prose_scores_high():
    assert source_fraction(JA_NARRATION) > 0.8


def test_plain_english_scores_zero():
    assert source_fraction("The train has not come yet.") == 0.0


def test_english_that_keeps_japanese_quote_marks_still_scores_zero():
    """The case that would break everything downstream.

    Translations of Japanese novels routinely keep 「」 for dialogue. If the corner
    brackets counted as Japanese, a finished English chapter would look partly
    untranslated, fail its language check, and be queued for translation again — at
    real cost, forever.
    """
    assert source_fraction(EN_DIALOGUE) == 0.0
    assert not has_japanese(EN_DIALOGUE)


def test_every_japanese_script_counts():
    hiragana = "こんにちは"
    katakana = "コーヒー"
    kanji = "電車駅"
    halfwidth = "ｶﾀｶﾅ"
    iteration = "佐々木"   # 佐々木 — the 々 is prose, not punctuation

    for text in (hiragana, katakana, kanji, halfwidth, iteration):
        assert source_fraction(text) == 1.0, text


def test_whitespace_is_excluded_from_the_denominator():
    """Japanese prose has almost no spaces and English has many. Counting them would
    make the same passage score differently for reasons unrelated to language."""
    assert source_fraction(JA_NARRATION) == source_fraction("  " + JA_NARRATION + "\n\n")


def test_empty_text_scores_zero_without_dividing_by_zero():
    assert source_fraction("") == 0.0
    assert source_fraction("   \n  ") == 0.0


def test_japanese_laughter_is_not_special_cased_here():
    """``www`` and ``草`` are how Japanese writes laughter, and 草 is a perfectly
    ordinary Kanji. This function counts script and says so; keeping them out of a
    RESIDUE detector is a different rule that cannot be a range exclusion, and it is
    deliberately not faked here."""
    assert source_fraction("www") == 0.0
    assert source_fraction("草") == 1.0


# ---- metrics -----------------------------------------------------------------

def _chapter(*paragraphs: str, title: str = "第1話") -> Chapter:
    return Chapter(index=1, title=title, paragraphs=list(paragraphs))


def test_metrics_count_what_they_say():
    ch = _chapter(JA_NARRATION, JA_DIALOGUE)
    m = ch.metrics

    assert m.paragraph_count == 2
    assert m.dialogue_count == 1          # only the one in corner brackets
    assert m.char_count == len(JA_NARRATION) + len(JA_DIALOGUE)
    assert m.source_fraction > 0.8


def test_straight_and_curly_quotes_also_count_as_dialogue():
    """Text that has been through a western editor or a site export loses the corner
    brackets. A chapter of dialogue should not read as having none."""
    ch = _chapter('"Late, is it not," he said.', "“So it seems.”")
    assert ch.metrics.dialogue_count == 2


def test_the_hash_covers_the_prose_and_not_the_title():
    """Retitling must not re-bill a translation."""
    a = _chapter(JA_NARRATION, title="第1話")
    b = _chapter(JA_NARRATION, title="Chapter One — renamed")

    assert a.metrics.content_hash == b.metrics.content_hash


def test_editing_one_character_moves_the_hash():
    a = _chapter(JA_NARRATION)
    b = _chapter(JA_NARRATION + "。")

    assert a.metrics.content_hash != b.metrics.content_hash


def test_paragraph_boundaries_are_part_of_the_hash():
    """One paragraph and two that concatenate to the same characters are different
    chapters, and translating them produces different prose."""
    joined = _chapter(JA_NARRATION + JA_DIALOGUE)
    split = _chapter(JA_NARRATION, JA_DIALOGUE)

    assert joined.metrics.content_hash != split.metrics.content_hash


def test_invisible_characters_are_stripped_before_hashing():
    """Copy-protection watermarks drift in on a re-paste. Left in, they inflate the
    counts and — far worse — move the content hash, re-billing a chapter that did not
    change."""
    watermarked = JA_NARRATION[:3] + "​‌﻿" + JA_NARRATION[3:]

    assert strip_invisibles(watermarked) == JA_NARRATION
    assert _chapter(strip_invisibles(watermarked)).metrics.content_hash == \
        _chapter(JA_NARRATION).metrics.content_hash


def test_text_joins_paragraphs_with_a_blank_line():
    assert _chapter("a", "b").text == "a\n\nb"


# ---- classification ----------------------------------------------------------

def test_a_japanese_chapter_classifies_as_source():
    assert classify(_chapter(JA_NARRATION, JA_DIALOGUE), 0.15) == KIND_SOURCE


def test_an_english_chapter_classifies_as_english():
    assert classify(_chapter("The train has not come yet.", EN_DIALOGUE), 0.15) == \
        KIND_ENGLISH


def test_a_chapter_with_no_paragraphs_classifies_as_empty():
    assert classify(_chapter(), 0.15) == KIND_EMPTY


def test_the_threshold_is_honoured():
    """A chapter quoting a long English passage dips; where the line sits is a config
    decision, not a constant, which is exactly why it is a parameter."""
    mixed = _chapter(JA_NARRATION + " " + ("english text " * 12))
    fraction = mixed.metrics.source_fraction

    assert classify(mixed, fraction - 0.01) == KIND_SOURCE
    assert classify(mixed, fraction + 0.01) == KIND_ENGLISH


def test_the_classification_values_never_name_a_language():
    """The whole reason this app is separate from Night Reader. These three strings
    are persisted on every record; a language name among them is the mistake that cost
    a migration across 81 projects."""
    assert {KIND_SOURCE, KIND_ENGLISH, KIND_EMPTY} == {"source", "english", "empty"}


# ---- serialization -----------------------------------------------------------

def test_chapters_round_trip_through_records():
    chapters = [Chapter(index=1, title="第1話", paragraphs=[JA_NARRATION]),
                Chapter(index=2, title="第2話", paragraphs=[JA_DIALOGUE])]
    back = records_to_chapters(chapters_to_records(chapters))

    assert [c.title for c in back] == ["第1話", "第2話"]
    assert [c.paragraphs for c in back] == [[JA_NARRATION], [JA_DIALOGUE]]
    assert [c.metrics.content_hash for c in back] == \
        [c.metrics.content_hash for c in chapters]


def test_indices_are_derived_from_position_not_read_back():
    """A hand-edited or half-written source.json could otherwise produce two chapters
    claiming index 4 — which state.json, keyed by index, would merge into one."""
    back = records_to_chapters([{"title": "a", "paragraphs": ["x"], "index": 9},
                                {"title": "b", "paragraphs": ["y"], "index": 9}])

    assert [c.index for c in back] == [1, 2]


def test_malformed_records_are_skipped_and_indices_stay_contiguous():
    back = records_to_chapters([{"title": "a", "paragraphs": ["x"]},
                                "not a record",
                                {"title": "c", "paragraphs": "not a list"}])

    assert [c.index for c in back] == [1, 2]
    assert [c.title for c in back] == ["a", "c"]
    assert back[1].paragraphs == []


def test_records_from_nothing_is_empty_rather_than_raising():
    assert records_to_chapters(None) == []
    assert records_to_chapters([]) == []
