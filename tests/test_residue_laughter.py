"""The laughter carve-out in the residue check is CONTEXTUAL, not a character range.

Japanese writes laughter as ``www`` (Latin, or full-width ｗｗｗ) and ``草``, an ordinary
Kanji meaning "grass" that also turns up inside real words (草原, meadow) and names
(草野). The Korean app kept its chat laughter out by CHARACTER RANGE, and the plan says
plainly that this cannot be ported: "Japanese laughter is www and 草: Latin and Kanji.
The carve-out cannot be a range exclusion." So 草 is subtracted only when it stands
ALONE, between characters that are not Japanese script.

There are two easy ways to get this wrong, and both pass the obvious test:

* **Too broad.** Dropping U+8349 from the script class (a range exclusion), or blanking
  every 草 whatever surrounds it. A leaked sentence such as 草原を歩いていた。 is still
  flagged either way, because the rest of the sentence carries it, so "is it flagged?"
  proves nothing on its own. What actually breaks is that the reviewer is shown
  原を歩いていた。, and a sentence whose only Kanji is 草 loses the evidence that
  flagged it, so a real leak passes silently. That is why the tests below compare each
  草 sentence against a CONTROL that differs in that one Kanji and nothing else: same
  text, same score, same reasons, same residual fraction.
* **Too narrow.** For example, only between spaces. Laughter at the start or end of a
  line, in brackets, or followed by 。 then counts as Japanese. It inflates the residual
  fraction that the wholesale "was this translated at all?" check reads, and when it is
  punctuated like a sentence it flags on its own.

The must-not-flag cases in test_sanitize.py check only the verdict. A lone 草 never
reaches the threshold anyway, because one character scores nothing, so those cases
would pass with no carve-out at all. These tests check that the laughter was actually
SUBTRACTED: its residual fraction is zero.

All Japanese here is invented for these tests.
"""

from __future__ import annotations

import pytest

from morning.sanitize import LeakReport, has_source_leak, source_leak_report

# ---- invented fixtures -------------------------------------------------------
# Each 草 sentence is paired with a control that differs in that one Kanji and in
# nothing else, so any difference in how the two are treated is the carve-out's doing.
MEADOW = "草原を歩いていた。"            # "(she) was walking across the meadow."
MEADOW_CONTROL = "野原を歩いていた。"    # the same, with 野原 ("field")
NAME = "草野さんは帰らなかった。"        # "Kusano did not go home."
NAME_CONTROL = "野村さんは帰らなかった。"  # the same, with another surname
# Here 草 is the ONLY Kanji, so it alone makes the run prose-shaped. Without it, what is
# left (がのびたね) scores below the threshold. This is the pair where a range exclusion
# changes the VERDICT, and not only the text shown to the reviewer.
GROWN = "草がのびたね"                   # "the grass has grown, hasn't it"
GROWN_CONTROL = "芽がのびたね"           # "the buds have grown, haven't they"

LEAK = "彼女は答えなかった。"            # "she did not answer."

PAIRS = [
    pytest.param(MEADOW, MEADOW_CONTROL, id="meadow"),
    pytest.param(NAME, NAME_CONTROL, id="surname"),
    pytest.param(GROWN, GROWN_CONTROL, id="kusa-is-the-only-kanji"),
]


def _in_english(japanese: str) -> str:
    """A partly translated paragraph: the usual shape a leak arrives in."""
    return f"He looked up at the sky. {japanese}"


def _shape(report: LeakReport) -> tuple:
    """Everything about a report except the finding text, which is expected to differ
    between a 草 sentence and its control by exactly that one character."""
    return (report.flagged,
            [(f.line, f.score, f.reasons) for f in report.findings],
            report.residual_fraction)


def _assert_subtracted(text: str) -> None:
    report = source_leak_report(text)
    assert report.flagged is False, text
    assert report.findings == [], text
    assert report.residual_fraction == 0.0, (
        f"the laughter in {text!r} was scored as Japanese rather than subtracted")


# ---- 草 inside a word is ordinary Japanese -----------------------------------

@pytest.mark.parametrize("sentence", [MEADOW, NAME, GROWN],
                         ids=["meadow", "surname", "kusa-is-the-only-kanji"])
def test_a_leaked_sentence_with_kusa_inside_a_word_is_flagged_with_kusa_still_in_it(
        sentence):
    """The reviewer is shown the offending run, and adding it to the glossary is meant
    to take one glance. If 草 is dropped from the script class, or blanked wherever it
    appears, the run shown is 原を歩いていた。 or 野さんは帰らなかった。, which is a
    fragment that is not in the chapter and cannot be added as a term."""
    report = source_leak_report(_in_english(sentence))

    assert report.flagged is True
    assert [f.text for f in report.findings] == [sentence]


@pytest.mark.parametrize("sentence,control", PAIRS)
def test_kusa_inside_a_word_is_treated_exactly_like_any_other_kanji(sentence, control):
    """This is the assertion that catches a range exclusion. The verdict alone does not,
    because the rest of a long sentence carries it. Swapping 草 for another Kanji must
    change nothing: not the score, not the reasons, and not the residual fraction that
    the wholesale check reads."""
    kusa = source_leak_report(_in_english(sentence))
    other = source_leak_report(_in_english(control))

    assert other.flagged is True, "the control must itself be a leak, or this proves nothing"
    assert _shape(kusa) == _shape(other)


def test_a_sentence_whose_only_kanji_is_kusa_is_still_caught():
    """The failure that matters. When 草 is the sentence's only Kanji, it is what makes
    the run look like prose. If 草 is carved out by range, what is left scores below
    the threshold and a real untranslated line passes with no warning.

    The second assertion proves the fixture's premise. If the weights ever change so
    that the remainder flags without 草, this test no longer discriminates. It should
    fail loudly then, rather than keep passing for the wrong reason."""
    assert has_source_leak(f"He knelt by the path. {GROWN}") is True
    assert has_source_leak(f"He knelt by the path. {GROWN[1:]}") is False, (
        "the fixture no longer depends on 草; pick one where it does")


def test_kusa_inside_a_kept_word_still_counts_as_residual_japanese():
    """A kept word is too short to flag, but it is still Japanese on the page, and the
    wholesale "was this translated at all?" check adds up exactly that. Blanking every
    草 would make a chapter full of 草原 read as less Japanese than the same chapter
    written with 野原."""
    kusa = source_leak_report("They crossed the 草原 at dawn.")
    other = source_leak_report("They crossed the 野原 at dawn.")

    assert kusa.flagged is False
    assert kusa.residual_fraction > 0.0
    assert kusa.residual_fraction == other.residual_fraction


# ---- 草 standing alone is laughter -------------------------------------------

@pytest.mark.parametrize("text", [
    pytest.param("She answered 草 and went back to her soup.", id="kusa"),
    pytest.param("The only reply was 草草草 from the back row.", id="kusa-repeated"),
    pytest.param("Someone in the chat wrote www and nothing more.", id="www"),
    pytest.param("Someone in the chat wrote ｗｗｗ and nothing more.",
                 id="full-width-www"),
    pytest.param("He sent 草www and logged off.", id="kusa-then-www"),
    pytest.param("He sent 草ｗｗ and logged off.", id="kusa-then-full-width-ww"),
])
def test_lone_laughter_between_english_words_is_subtracted(text):
    """What a translated chat log or comment thread is made of. Counted as Japanese, a
    chapter of it would move toward the wholesale "not translated" failure one laugh at
    a time."""
    _assert_subtracted(text)


@pytest.mark.parametrize("text", [
    pytest.param("草 was all the thread said.", id="start-of-text"),
    pytest.param("All the thread said was 草", id="end-of-text"),
    pytest.param("The thread went quiet.\n草草\nThen someone posted again.",
                 id="a-line-of-its-own"),
    pytest.param("The thread ended with 草\nand nobody posted after that.",
                 id="end-of-a-line"),
])
def test_lone_laughter_at_the_start_or_end_of_a_line_is_subtracted(text):
    """"Alone" means no Japanese script on either side, and the edge of the line or of
    the text counts as "no script". A carve-out written as "between spaces" misses every
    one of these, and "between whitespace" still misses the first two. A comment thread
    puts laughter on a line by itself more often than anywhere else."""
    _assert_subtracted(text)


@pytest.mark.parametrize("text", [
    pytest.param("He typed 「草」 and left.", id="corner-brackets"),
    pytest.param('"草" was all she wrote.', id="straight-quotes"),
    pytest.param("The reply (草) came at once.", id="parentheses"),
    pytest.param("草! he typed.", id="exclamation-mark"),
    pytest.param("Just 草, nothing else.", id="comma"),
    pytest.param("The reply was one word: 草。", id="full-width-full-stop"),
])
def test_lone_laughter_touching_punctuation_is_subtracted(text):
    """Punctuation is not script, so it does not make 草 part of a word. That includes
    the Japanese kind: corner brackets and 。 are exactly what a translation keeps around
    a quoted reply."""
    _assert_subtracted(text)


def test_laughter_punctuated_like_a_sentence_does_not_flag():
    """Without the carve-out, this run has a clause break and a sentence end. That is
    two signals agreeing, and it reaches the threshold on laughter alone. The control
    line proves the shape really would flag if the characters were not laughter."""
    assert has_source_leak("The whole thread was 草、草草、草草草。") is False
    assert has_source_leak("The whole thread was 芽、芽芽、芽芽芽。") is True, (
        "the fixture no longer reaches the threshold; it tests nothing")
    _assert_subtracted("The whole thread was 草、草草、草草草。")


# ---- laughter next to a real leak --------------------------------------------

@pytest.mark.parametrize("text", [
    pytest.param(f"{LEAK}草", id="kusa-after"),
    pytest.param(f"草草 {LEAK}", id="kusa-before"),
    pytest.param(f"{LEAK}ｗｗｗ", id="full-width-www-after"),
])
def test_laughter_beside_a_real_leak_neither_hides_it_nor_joins_it(text):
    """Blanking the laughter must not take the sentence next to it. The run shown to the
    reviewer is the sentence alone: 草 after 。 is a reaction to the sentence, not part
    of it."""
    report = source_leak_report(text)

    assert report.flagged is True
    assert [f.text for f in report.findings] == [LEAK]


def test_laughter_after_a_leak_adds_nothing_to_the_residual():
    """The same sentence with and without a trailing laugh is the same amount of
    untranslated Japanese."""
    assert (source_leak_report(f"{LEAK}草").residual_fraction
            == source_leak_report(LEAK).residual_fraction)
