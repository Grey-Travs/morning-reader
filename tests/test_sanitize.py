"""Tests for the two post-translation checks.

The residue cases below are not invented for the test. Twelve are the acceptance
criteria the rule was designed against; the rest are inputs that three adversaries
CONFIRMED by execution would break an earlier version of this rule. Keeping them here
is the whole point — the rule looked sound until someone ran those strings through it.

The plan calls this the rule most likely to be wrong, and says to pin both directions.
Both directions are pinned.

Japanese fixtures are invented sentences, common vocabulary and ordinary set phrases.
"""

from __future__ import annotations

import time

import pytest

from morning.glossary import Glossary, GlossaryEntry
from morning.sanitize import (
    FLAG_AT, LeakReport, block_is_meta, has_source_leak, source_leak_report, strip_meta,
)

# ---- invented fixtures -------------------------------------------------------
LEAK_SENTENCE = "彼女は答えなかった。"   # "she did not answer."
LEAK_LONG = ("電車はまだ来ない。"
             "彼女はホームの端に立っていた。")
KEPT_SIGN = "藪蕎麦"        # a soba shop's name on a sign
KEPT_SFX = "ドン"               # a thud
KEPT_TITLE = "風は知っている"  # invented title, "the wind knows"


# ---- the invariant the losing design broke -----------------------------------

def test_no_single_signal_can_flag_on_its_own():
    """Asserted at import too, but stated here because it is the property that
    matters. The design this replaces promised exactly this in its docstring while two
    of its signals each weighed FLAG_AT — and all three adversaries found that
    independently. One mistaken pattern then becomes a false positive on every chapter
    containing it."""
    import morning.sanitize as s

    weights = [s._W_HARD_END, s._W_PROSE_SHAPE, s._W_PREDICATE, s._W_BIND,
               s._W_TOPIC, s._W_CLAUSE, s._W_FINAL_PARTICLE]
    assert max(weights) < FLAG_AT
    assert FLAG_AT >= 2, "a threshold of 1 is the same as having no threshold"


# ---- must NOT flag -----------------------------------------------------------

@pytest.mark.parametrize("label,text", [
    ("Japanese laughter in Latin", "That's hilarious www"),
    ("kusa as laughter", "He typed 草 and logged off."),
    ("kusa repeated", "草草草 was the only reply."),
    ("kept corner quotes", "「Late, isn't it,」 he said."),
    ("kept honorific", "Thank you, Tanaka-san."),
    ("a kept shop sign", f"The sign read {KEPT_SIGN}, which he could not read."),
    ("a kept sound effect", f"A sharp {KEPT_SFX} echoed down the corridor."),
    ("plain English", "He waited on the platform for a long time."),
    ("romaji", "There is no English word for natsukashii."),
    ("empty", ""),
    ("whitespace", "   \n\n  "),
])
def test_legitimate_english_output_is_not_flagged(label, text):
    assert has_source_leak(text) is False, label


@pytest.mark.parametrize("label,text", [
    # Every one of these was CONFIRMED by an adversary against the earlier rule.
    ("kept term in corner brackets", "「先生」 was all she managed."),
    ("kept sfx in corner brackets",
     "From behind the door came a muffled 「バタン」."),
    ("a sign on its own line",
     "The sign read:\n\n定食屋 まるみ\n\nwhich he could not read."),
    ("a term list with middle dots",
     "Terms used:\n屋台・神社・縁側"),
    ("kept otsukaresama",
     "He bowed and said お疲れ様です before leaving."),
    ("kept itterasshai",
     "Her mother answered いってらっしゃい without looking up."),
    ("kept mottainai",
     "There is no English word for もったいない, she said."),
    ("kept shouganai", "He shrugged. しょうがない, he thought."),
    ("a long sound effect on its own line",
     "The door gave way.\nドオォォドン\nHe turned around."),
    ("a kanji date",
     "Everything is dated to 平成二十三年三月の午後."),
    ("a kept katakana coinage",
     "The プロメテウスシステム hummed in the dark."),
    ("kept food names",
     "A paper bag of たこ焼き and two of お好み焼き."),
    ("a string of place names",
     "They met at 東京都中央区銀座 that morning."),
])
def test_confirmed_adversarial_false_positives_stay_unflagged(label, text):
    """Each of these flagged under the earlier rule. Three separate causes, all gone:

    * a bracketed span and a Latin-free line each scored the full threshold ALONE;
    * the genitive の and the list marker と were counted as clause-binding particles,
      so a date and a title scored as predications;
    * the katakana middle dot was treated as script, welding three separate kept terms
      into one over-long run.
    """
    assert has_source_leak(text) is False, label


# ---- must flag ---------------------------------------------------------------

@pytest.mark.parametrize("label,text", [
    ("a whole untranslated sentence", LEAK_LONG),
    ("a partly translated paragraph", f"He waited on the platform. {LEAK_SENTENCE}"),
    ("untranslated dialogue",
     "「遅いね」と彼は言った。"
     "「もう三十分も待っている」"),
    ("a kana-only sentence",
     "それからしばらくして、"
     "かのじょはゆっくりと"
     "ふりかえった。"),
])
def test_untranslated_source_is_flagged(label, text):
    assert has_source_leak(text) is True, label


@pytest.mark.parametrize("label,text", [
    ("full-width sentence punctuation",
     "He turned away. 彼女は叫んだ！"),
    ("zero-width characters between every character",
     "He waited. 彼女​は​答​え​な"
     "​か​っ​た。"),
    ("a leak immediately after a URL",
     f"Source: https://example.com/n1234a/5/{LEAK_SENTENCE}"),
    ("a short utterance ending in a sentence-final particle",
     "He stopped. 何してるの was all he said."),
    ("an ideographic space holding the sentence together",
     "He sighed. 電車は　まだ　来ない。"),
])
def test_confirmed_adversarial_false_negatives_are_now_caught(label, text):
    """Each of these was MISSED by the earlier rule, and each for a self-inflicted
    reason:

    * it ran NFKC first, which rewrote ！ to ! and U+3000 to a plain space, so its own
      full-width patterns stopped matching — it broke its own inputs;
    * format characters split every run and defeated every lookaround;
    * its URL pattern was ``\\S+``, and Japanese has no whitespace, so a URL swallowed
      the leaked sentence that followed it;
    * its predicate rule fired only clause-finally, so an utterance ending in a
      sentence-final particle scored nothing.
    """
    assert has_source_leak(text) is True, label


# ---- the glossary is the allow-list ------------------------------------------

def test_a_glossary_term_is_not_reported_as_a_leak():
    """The reframing the panel produced. Kept and leaked are character-identical —
    the difference is intent, and intent is not in the text. The glossary IS the
    record of intent, so it is subtracted before anything is scored."""
    text = f"He was rereading {KEPT_TITLE} that winter."
    assert has_source_leak(text) is True, "it should flag before anyone has said to keep it"

    glossary = Glossary([GlossaryEntry(source=KEPT_TITLE, english="The Wind Knows",
                                       type="term")])
    assert has_source_leak(text, glossary=glossary) is False


def test_any_spelling_of_a_glossary_term_is_subtracted():
    """A term kept in kanji in narration and kana in dialogue is one entry."""
    glossary = Glossary([GlossaryEntry(source="佐々木", english="Sasaki",
                                       variants=["ササキ"])])

    assert has_source_leak("She greeted ササキ warmly.",
                           glossary=glossary) is False


def test_subtracting_a_term_does_not_hide_a_real_leak_beside_it():
    """The subtraction blanks with spaces of equal length, so a leak next to a kept
    term is still found — and still reported at the right line."""
    glossary = Glossary([GlossaryEntry(source=KEPT_SIGN, english="Yabu Soba")])
    text = f"Past the {KEPT_SIGN} sign. {LEAK_SENTENCE}"

    report = source_leak_report(text, glossary=glossary)
    assert report.flagged is True
    assert KEPT_SIGN not in report.findings[0].text


def test_extra_terms_can_be_passed_without_a_glossary():
    assert has_source_leak(f"He was rereading {KEPT_TITLE} that winter.",
                           extra_terms=[KEPT_TITLE]) is False


def test_a_broken_glossary_does_not_break_the_check():
    """A check that raises is worse than one that over-reports: it fails the chapter
    with a traceback instead of a verdict."""
    class Broken:
        def entries(self):
            raise RuntimeError("no")

    assert has_source_leak(LEAK_SENTENCE, glossary=Broken()) is True


# ---- the report --------------------------------------------------------------

def test_the_report_says_where_and_why():
    """So a reviewer can see the offending run rather than being told the chapter has
    a problem somewhere — and so adding the term to the glossary takes one glance."""
    report = source_leak_report(f"Line one.\nLine two.\n{LEAK_SENTENCE}")

    assert report.flagged is True
    finding = report.findings[0]
    assert finding.line == 3
    assert finding.text == LEAK_SENTENCE
    assert finding.score >= FLAG_AT
    assert finding.reasons
    assert LEAK_SENTENCE[:8] in report.summary()


def test_a_clean_chapter_reports_nothing():
    report = source_leak_report("He waited on the platform.")

    assert report == LeakReport(flagged=False, findings=[])
    assert report.summary() == ""


# ---- robustness --------------------------------------------------------------

@pytest.mark.parametrize("value", [None, 123, b"bytes", [], {}])
def test_non_text_input_returns_a_verdict_rather_than_crashing(value):
    """An adversary crashed the earlier version with bytes. A check that raises turns
    a questionable chapter into a failed one."""
    assert has_source_leak(value) is False


def test_a_very_long_line_does_not_take_quadratic_time():
    """An adversary measured 3.3s on a pathological line under the earlier rule, which
    compared every sub-run against every bracket span on the line."""
    text = "「あい」" * 20000

    started = time.monotonic()
    source_leak_report(text)
    assert time.monotonic() - started < 5.0


def test_markup_and_urls_are_not_mistaken_for_prose():
    assert has_source_leak("See [the notes](https://example.com/a) and `code`.") is False


# ---- documented misses -------------------------------------------------------
# Stated as tests rather than only in prose, so that if someone later makes the rule
# stricter these fail loudly and the trade-off is a decision rather than a surprise.

def test_noun_only_residue_is_knowingly_missed():
    """An untranslated notice has no grammatical machinery at all. Catching it needs
    different evidence, not a lower threshold — a threshold low enough to catch this
    flags every kept shop sign, which is the case above it in this file."""
    assert has_source_leak("The notice said 本日休業 and nothing else.") is False


def test_very_short_utterances_are_knowingly_missed():
    """Lowering the bar to catch these flags kept food names and sound effects."""
    for text in ("He shrugged. 帰りたい", "She said もういい."):
        assert has_source_leak(text) is False, text


# ---- meta leaks --------------------------------------------------------------

@pytest.mark.parametrize("block", [
    "Here is the translation of the chapter:",
    "Let me re-read the glossary before continuing.",
    "I'll translate the full chapter now.",
    "Translator's note: the pun does not carry over.",
    "As an AI, I should mention that this scene is ambiguous.",
    "Below is the translation.",
    "---",
])
def test_model_chatter_is_recognised(block):
    assert block_is_meta(block) is True


@pytest.mark.parametrize("block", [
    "“Let me redo my makeup,” she said, reaching for the mirror.",
    "“I apologize,” he said. “Let me check the ledger again.”",
    "Let me write you a letter, she decided.",
    "He would translate the runes himself, given time.",
    "She waited on the platform.",
])
def test_real_prose_is_never_mistaken_for_chatter(block):
    """A false positive here DELETES a paragraph the user paid for. The patterns are
    precise for that reason: phrases characters actually say are not signals."""
    assert block_is_meta(block) is False


def test_stripping_removes_a_block_that_is_only_chatter():
    text = ("Here is the translation:\n\n"
            "She waited on the platform.\n\n"
            "The train did not come.")
    clean, removed, suspicious = strip_meta(text)

    assert clean == "She waited on the platform.\n\nThe train did not come."
    assert removed == ["Here is the translation:"]
    assert suspicious == []


def test_a_block_mixing_prose_with_chatter_is_kept_and_reported():
    """The sharp edge. When the model corrects itself MID-paragraph, the block holds
    real sentences on both sides of the chatter, and deleting the block destroys them.

    This test found exactly that in an earlier version of strip_meta: it deleted the
    whole paragraph and lost two real sentences — the failure its own docstring warned
    about. Paragraph granularity alone is not enough.
    """
    text = "She waited on the platform. Let me redo this bit. The train did not come."
    clean, removed, suspicious = strip_meta(text)

    assert clean == text, "real prose must survive"
    assert removed == []
    assert suspicious == [text], "...but a human should still be told"


def test_unambiguous_notation_is_removed_even_beside_prose():
    """"Translator's note" and ===NEW_TERMS=== never occur inside real prose, so there
    is nothing to protect by keeping them."""
    text = ("She waited on the platform. Translator's note: the pun does not carry "
            "over. The train did not come.")
    clean, removed, suspicious = strip_meta(text)

    assert clean == ""
    assert len(removed) == 1


def test_a_mostly_japanese_block_is_not_treated_as_chatter():
    """That is a residue problem, handled separately and by FLAGGING — so the strip
    can never delete a sound effect or a real passage."""
    assert block_is_meta(LEAK_LONG) is False
    assert strip_meta(LEAK_LONG)[0] == LEAK_LONG


def test_stripping_nothing_leaves_the_text_alone():
    assert strip_meta("")[0] == ""
    assert strip_meta("Plain prose.")[0] == "Plain prose."
    assert strip_meta("Plain prose.")[1:] == ([], [])
