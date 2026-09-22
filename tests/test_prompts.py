"""Tests for the translation prompt and the contract the model answers in.

Most of the prompt is prose and prose is not worth asserting word by word — it will
be tuned. What IS worth pinning is the machine-readable half: the delimiter, the JSON
key names, and the substitution points. Those are a contract with the response parser
and with the glossary on disk, and a silent drift in any of them produces terms that
are parsed into the wrong field and stored wrong forever.
"""

from __future__ import annotations

import json
import re

from morning.glossary import Glossary, GlossaryEntry
from morning.prompts import (
    NEW_TERMS_DELIMITER, NEW_TERMS_OUTPUT_CONTRACT, PARAGRAPH_OUTPUT_CONTRACT,
    build_system_prompt, build_user_message,
)

SASAKI = "佐々木"
JA_LINE = "佐々木は笑った。"   # invented: "Sasaki laughed."


# ---- the response contract ---------------------------------------------------

def test_the_new_terms_key_is_source_not_a_language_name():
    """The third place Night Reader persists "korean": the model's OWN response
    schema. Whatever the model is asked for under this key is what lands in the
    glossary, so getting it right here is what stops a language name reaching disk."""
    assert '"source"' in NEW_TERMS_OUTPUT_CONTRACT
    assert "korean" not in NEW_TERMS_OUTPUT_CONTRACT.lower()  # scope-guard: ok
    assert '"japanese"' not in NEW_TERMS_OUTPUT_CONTRACT.lower()


def test_the_contract_asks_for_every_field_the_glossary_stores():
    """A field the glossary has but the prompt never asks for is a field that is
    always empty — which reads as "the model never found one" rather than "nobody
    asked"."""
    for key in ("source", "reading", "variants", "english", "type", "note",
                "pronoun", "first_person", "register"):
        assert f'"{key}"' in NEW_TERMS_OUTPUT_CONTRACT, f"the prompt never asks for {key}"


def test_the_contract_example_parses_as_json_with_those_keys():
    """The example in the prompt is what the model imitates. If it is malformed, the
    model imitates malformed output."""
    match = re.search(r"`(\[\{.*?\}\])`", NEW_TERMS_OUTPUT_CONTRACT, re.DOTALL)
    assert match, "the contract has no JSON example for the model to copy"

    parsed = json.loads(match.group(1).replace("…", "..."))
    assert isinstance(parsed, list) and isinstance(parsed[0], dict)
    assert set(GlossaryEntry.from_dict(parsed[0]).__dict__) >= {"source", "english"}


def test_the_contract_example_round_trips_into_a_glossary_entry():
    """End of the chain: what the prompt asks for must be loadable by the code that
    stores it, with no field quietly dropped."""
    entry = GlossaryEntry.from_dict({
        "source": SASAKI, "reading": "ささき",
        "variants": ["ササキ"], "english": "Sasaki", "type": "name",
        "note": "addressed as -san", "pronoun": "she",
        "first_person": "あたし", "register": "polite",
    })

    assert entry.source == SASAKI
    assert entry.spellings() == [SASAKI, "ササキ", "ささき"]
    assert entry.first_person == "あたし"


def test_the_delimiter_is_on_its_own_line_in_the_instruction():
    """The parser splits on it. A delimiter the model puts inline is one the parser
    cannot find."""
    assert NEW_TERMS_DELIMITER in NEW_TERMS_OUTPUT_CONTRACT
    assert "line containing only" in NEW_TERMS_OUTPUT_CONTRACT


def test_the_delimiter_cannot_occur_in_ordinary_prose():
    """It has to be unmistakable, because a false match truncates a chapter."""
    assert NEW_TERMS_DELIMITER.startswith("===")
    assert NEW_TERMS_DELIMITER.isupper() or "_" in NEW_TERMS_DELIMITER


# ---- assembly ----------------------------------------------------------------

def test_the_system_prompt_assembles_without_optional_parts():
    prompt = build_system_prompt()

    # Named placeholders only. Bare braces are legitimate here — the output contract
    # contains a JSON example, which is the thing the model copies, so asserting
    # "no { anywhere" would fail on correct output.
    for placeholder in ("{style_note_line}", "{honorific_note_line}",
                        "{glossary_block}", "{output_contract}"):
        assert placeholder not in prompt, f"{placeholder} was never substituted"
    assert NEW_TERMS_DELIMITER in prompt


def test_the_json_example_survives_formatting_intact():
    """``str.format`` eats single braces. The example is built with ``{{``/``}}`` and
    passed in as a VALUE rather than being part of the template, so its braces reach
    the model — if that ever changes, the model is shown broken JSON to imitate."""
    prompt = build_system_prompt()

    match = re.search(r"`(\[\{.*?\}\])`", prompt, re.DOTALL)
    assert match, "the JSON example did not survive into the assembled prompt"
    json.loads(match.group(1).replace("…", "..."))


def test_the_style_note_is_injected_when_given():
    assert "a slow-paced literary novel" in build_system_prompt(
        style_note="a slow-paced literary novel")


def test_an_empty_style_note_leaves_no_empty_heading():
    """A heading with nothing under it teaches the model that empty sections are
    normal, and it starts producing them."""
    assert "**This work:**" not in build_system_prompt(style_note="   ")


def test_the_glossary_block_is_injected_with_its_heading():
    glossary = Glossary([GlossaryEntry(source=SASAKI, english="Sasaki", type="name",
                                       pronoun="she")])
    prompt = build_system_prompt(glossary_block=glossary.to_prompt_block(JA_LINE))

    assert "Glossary for this chapter" in prompt
    assert "Sasaki" in prompt
    assert "she" in prompt


def test_an_empty_glossary_block_leaves_no_empty_heading():
    assert "Glossary for this chapter" not in build_system_prompt(glossary_block="")


def test_the_output_contract_is_swappable():
    """So a single-paragraph rewrite inherits the whole voice contract above it —
    glossary, names, pronouns, style — and comes back in the same register as its
    neighbours, without the prompt being forked."""
    prompt = build_system_prompt(output_contract=PARAGRAPH_OUTPUT_CONTRACT)

    assert "ONE paragraph" in prompt
    assert NEW_TERMS_DELIMITER not in prompt


def test_the_honorific_note_is_injected():
    assert "keep -senpai as-is" in build_system_prompt(
        honorific_note="keep -senpai as-is")


# ---- Japanese-specific guidance ----------------------------------------------

def test_the_prompt_names_the_japanese_honorifics():
    """Not the Korean ones. This is the guidance most likely to be copied across by
    accident, and -ssi/-nim instructions would be noise the model has to ignore."""
    prompt = build_system_prompt()

    for honorific in ("-san", "-kun", "-chan", "-sama", "-sensei", "-senpai"):
        assert honorific in prompt
    for korean in ("-ssi", "-nim", "-hyung", "-oppa", "-noona"):  # scope-guard: ok
        assert korean not in prompt  # scope-guard: ok


def test_the_prompt_addresses_the_first_person_pronoun_problem():
    """Japanese-specific and load-bearing: 俺 / 僕 / 私 are characterisation, and
    English has only "I", so the distinction has to move into register and diction."""
    prompt = build_system_prompt()

    assert "俺" in prompt and "僕" in prompt and "私" in prompt
    assert "refers to self as" in prompt


def test_the_prompt_tells_the_model_what_to_do_with_the_source_quote_marks():
    prompt = build_system_prompt()

    assert "「」" in prompt
    assert "『』" in prompt


def test_the_prompt_addresses_onomatopoeia():
    """Japanese uses far more of it than English, including for states that make no
    sound. Transliterating them is the characteristic bad machine translation."""
    assert "onomatopoeia" in build_system_prompt().lower()


def test_the_prompt_forbids_untranslated_source_and_meta_commentary():
    """The two failure modes sanitize and validate exist to catch. Asking for them
    not to happen is cheaper than detecting them afterwards."""
    prompt = build_system_prompt().lower()

    assert "untranslated japanese" in prompt
    assert "note about the translation" in prompt


def test_the_prompt_warns_against_guessing_a_reading():
    """Most kanji have several readings. A guessed one becomes a wrong romanization
    in every subsequent chapter, and unpicking it is expensive."""
    assert "Do not guess a reading" in NEW_TERMS_OUTPUT_CONTRACT


# ---- the user message --------------------------------------------------------

def test_the_user_message_carries_the_paragraphs_separated_by_blank_lines():
    message = build_user_message([JA_LINE, "雨が降っていた。"])

    assert JA_LINE in message
    assert "\n\n" in message


def test_a_single_chunk_says_nothing_about_chunks():
    """A model told it is translating part 1 of 1 starts hedging about continuity
    that does not exist."""
    message = build_user_message([JA_LINE])

    assert "part 1 of 1" not in message
    assert "continuity" not in message.lower()


def test_a_later_chunk_is_told_it_is_a_continuation():
    """A model that believes it is translating a whole chapter writes an ending."""
    message = build_user_message([JA_LINE], chunk_index=2, chunk_total=3)

    assert "part 2 of 3" in message
    assert "continues directly from part 1" in message


def test_the_previous_tail_is_marked_as_context_not_input():
    """Without the marking, the model re-translates it and the chapter gains a
    duplicated paragraph at every chunk boundary."""
    message = build_user_message([JA_LINE], chunk_index=2, chunk_total=2,
                                 previous_tail="She had waited a long time.")

    assert "She had waited a long time." in message
    assert "Do not" in message and "repeat it" in message


def test_the_title_is_given_once_not_on_every_chunk():
    first = build_user_message([JA_LINE], title="第1話", chunk_index=1, chunk_total=2)
    second = build_user_message([JA_LINE], title="第1話", chunk_index=2, chunk_total=2)

    assert "第1話" in first
    assert "第1話" not in second


def test_per_work_instructions_are_included():
    message = build_user_message([JA_LINE],
                                 extra_instruction="render sound effects in italics")

    assert "render sound effects in italics" in message


def test_an_empty_extra_instruction_adds_no_empty_heading():
    assert "Additional instructions" not in build_user_message([JA_LINE],
                                                               extra_instruction="  ")
