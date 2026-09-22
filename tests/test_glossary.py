"""Tests for the glossary — specifically the two things Japanese breaks.

Night Reader stores one source spelling per entry and matches it as a plain substring.
For Korean that is right. For Japanese it is wrong in two directions at once, and both
are pinned here:

* a name written three ways is one entry, so all three spellings must find it;
* a one-character entry matched as a bare substring hits inside longer words.

Common surnames and ordinary vocabulary are used as fixtures; nothing here is drawn
from a real novel.
"""

from __future__ import annotations

import json

from morning.glossary import (
    GlossaryEntry, Glossary, glossary_lock, load_pending, merge_pending,
    normalize_pronoun, save_pending,
)

# 佐々木 / ささき / ササキ — one surname, three scripts. This is ordinary Japanese
# orthography, not an edge case: narration uses kanji, a child's dialogue uses kana,
# a sign uses katakana.
SASAKI_KANJI = "佐々木"
SASAKI_HIRA = "ささき"
SASAKI_KATA = "ササキ"

MORI = "森"            # 森 — a one-character surname, "Mori"
FOREST = "森林"    # 森林 — "forest", contains 森
AOMORI = "青森"    # 青森 — a place name, ends with 森
MORITA = "森田"    # 森田 — "Morita", a different person


def _sasaki(**kw) -> GlossaryEntry:
    return GlossaryEntry(source=SASAKI_KANJI, english="Sasaki", type="name",
                         variants=[SASAKI_HIRA, SASAKI_KATA], **kw)


# ---- variants ----------------------------------------------------------------

def test_a_name_is_found_by_every_spelling_it_is_written_in():
    """The defect this exists to prevent: the narration locks 佐々木 to "Sasaki", a
    later chapter writes ササキ, the entry does not match, the model re-guesses, and
    the character is called "Sasaki" in one chapter and "Sazaki" in the next."""
    glossary = Glossary([_sasaki()])

    for spelling in (SASAKI_KANJI, SASAKI_HIRA, SASAKI_KATA):
        chapter = f"{spelling}は窓の外を見ていた。"
        hits = glossary.relevant_to(chapter)
        assert [e.english for e in hits] == ["Sasaki"], f"missed {spelling}"


def test_an_absent_name_is_not_injected():
    """The other half of the contract: only what occurs in THIS chapter, or a large
    glossary would dominate the prompt and push the prose out of attention."""
    glossary = Glossary([_sasaki()])

    assert glossary.relevant_to("雨が降っていた。") == []


def test_the_reading_counts_as_a_spelling():
    """A novel that introduces a name with furigana and later writes it in kana is
    writing the same name twice. The reading is also exactly what a scanned page's
    furigana regions provide (morning/pageread.py, KIND_FURIGANA)."""
    entry = GlossaryEntry(source="御堂筋", english="Midosuji", type="place",
                          reading="みどうすじ")
    glossary = Glossary([entry])

    assert glossary.relevant_to("みどうすじを歩いた。")


def test_spellings_are_deduplicated_and_primary_comes_first():
    entry = GlossaryEntry(source=SASAKI_KANJI, english="Sasaki",
                          variants=[SASAKI_HIRA, SASAKI_KANJI],
                          reading=SASAKI_HIRA)

    assert entry.spellings() == [SASAKI_KANJI, SASAKI_HIRA]


def test_a_variant_can_be_learned_later():
    glossary = Glossary([GlossaryEntry(source=SASAKI_KANJI, english="Sasaki")])

    assert glossary.add_variant(SASAKI_KANJI, SASAKI_KATA) is True
    assert glossary.relevant_to(f"{SASAKI_KATA}さん")

    # Learning it twice is not an error, but it is not news either.
    assert glossary.add_variant(SASAKI_KANJI, SASAKI_KATA) is False
    assert glossary.add_variant("誰もいない", "x") is False


def test_an_entry_is_retrievable_by_any_of_its_spellings():
    glossary = Glossary([_sasaki()])

    assert glossary.get(SASAKI_KATA) is glossary.get(SASAKI_KANJI)
    assert glossary.get("別の名前") is None


# ---- the length floor --------------------------------------------------------

def test_a_one_character_name_is_found_when_it_stands_alone():
    """森 followed by a particle is the surname. This must still match, or the floor
    would have thrown the entry away rather than making it precise."""
    glossary = Glossary([GlossaryEntry(source=MORI, english="Mori", type="name")])

    for chapter in (f"{MORI}は答えなかった。",   # 森は…
                    f"{MORI}さんが立ち上がった。",  # 森さん…
                    f"「{MORI}」と彼は言った。"):        # 「森」…
        assert glossary.relevant_to(chapter), f"missed a real mention: {chapter}"


def test_a_one_character_name_does_not_match_inside_a_longer_word():
    """The reason the floor exists. A bare substring test injects "Mori" into every
    chapter that mentions a forest, a trip to Aomori, or a different character called
    Morita."""
    glossary = Glossary([GlossaryEntry(source=MORI, english="Mori", type="name")])

    for chapter in (f"{FOREST}を抜けた。",   # 森林を抜けた
                    f"{AOMORI}へ向かった。",  # 青森へ向かった
                    f"{MORITA}さんが笑った。"):  # 森田さんが笑った
        assert glossary.relevant_to(chapter) == [], f"over-matched: {chapter}"


def test_a_one_character_name_is_still_found_when_the_word_also_appears():
    """A chapter can contain both. Finding the real mention matters more than being
    tidy about the other one."""
    glossary = Glossary([GlossaryEntry(source=MORI, english="Mori", type="name")])
    chapter = f"{FOREST}を抜けると、{MORI}が待っていた。"

    assert glossary.relevant_to(chapter)


def test_two_character_terms_use_a_plain_substring():
    """Two characters are specific enough in practice, and substring is the correct
    mechanism for a language with no inter-word spaces — that part of the Korean
    design transfers unchanged."""
    glossary = Glossary([GlossaryEntry(source=MORITA, english="Morita", type="name")])

    assert glossary.relevant_to(f"{MORITA}は帰った。")


# ---- English-only placeholders -----------------------------------------------

def test_english_only_names_are_offered_as_canonical_spellings():
    """Learned from chapters that were already in English. Injected as "match these"
    so a freshly translated chapter stays consistent with what is on disk."""
    glossary = Glossary([GlossaryEntry(english="Haruka", type="name")])

    assert [e.english for e in glossary.canonical()] == ["Haruka"]
    assert glossary.relevant_to("anything") == []


def test_a_mapped_entry_supersedes_its_english_only_placeholder():
    """Once the source form is learned, keeping both would inject the same name
    twice."""
    glossary = Glossary([GlossaryEntry(english="Sasaki", type="name")])
    glossary.add(_sasaki())

    assert glossary.canonical() == []
    assert len(glossary) == 1


def test_a_placeholder_never_displaces_a_mapped_entry():
    """The reverse ordering must not lose the source spellings."""
    glossary = Glossary([_sasaki()])
    glossary.add(GlossaryEntry(english="Sasaki", type="name"))

    assert len(glossary) == 1
    assert glossary.entries()[0].spellings() == [SASAKI_KANJI, SASAKI_HIRA, SASAKI_KATA]


def test_adding_the_same_source_twice_replaces_rather_than_duplicates():
    glossary = Glossary([GlossaryEntry(source=SASAKI_KANJI, english="Sasaki")])
    glossary.add(GlossaryEntry(source=SASAKI_KANJI, english="Sazaki"))

    assert len(glossary) == 1
    assert glossary.entries()[0].english == "Sazaki"


def test_an_entry_with_nothing_in_it_is_ignored():
    glossary = Glossary()
    glossary.add(GlossaryEntry())

    assert len(glossary) == 0


# ---- persistence -------------------------------------------------------------

def test_the_glossary_round_trips(tmp_path):
    path = tmp_path / "glossary.json"
    Glossary([_sasaki(pronoun="they", first_person="私", register="polite")]).save(path)
    back = Glossary.load(path)

    entry = back.entries()[0]
    assert entry.source == SASAKI_KANJI
    assert entry.variants == [SASAKI_HIRA, SASAKI_KATA]
    assert entry.pronoun == "they"
    assert entry.first_person == "私"


def test_the_stored_field_is_source_and_never_a_language_name(tmp_path):
    """The whole reason this app is separate. This key is on every entry on disk."""
    path = tmp_path / "glossary.json"
    Glossary([_sasaki()]).save(path)

    record = json.loads(path.read_text(encoding="utf-8"))[0]
    assert "source" in record
    assert "korean" not in record  # scope-guard: ok
    assert "japanese" not in record


def test_the_glossary_is_written_unescaped(tmp_path):
    path = tmp_path / "glossary.json"
    Glossary([_sasaki()]).save(path)

    raw = path.read_text(encoding="utf-8")
    assert SASAKI_KANJI in raw
    assert "\\u" not in raw


def test_a_corrupt_glossary_degrades_to_empty_and_keeps_the_bytes(tmp_path):
    """Degrading stops one bad file taking down the library. Quarantining first is
    what stops the next approval — which loads this, adds a term, and saves it back —
    from turning a momentary read failure into every locked name being erased."""
    path = tmp_path / "glossary.json"
    path.write_text('[{"source": "truncated', encoding="utf-8")

    assert len(Glossary.load(path)) == 0
    assert len(list(tmp_path.glob("glossary.json.unreadable-*"))) == 1


def test_a_glossary_of_the_wrong_shape_degrades(tmp_path):
    path = tmp_path / "glossary.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")

    assert len(Glossary.load(path)) == 0


def test_a_missing_glossary_loads_as_empty(tmp_path):
    assert len(Glossary.load(tmp_path / "nothing.json")) == 0


def test_the_markdown_companion_is_written_before_the_json(tmp_path):
    """The JSON is the source of truth, so it is written LAST — a failure partway
    leaves the previous good JSON intact rather than a truncated one."""
    path = tmp_path / "glossary.json"
    md = tmp_path / "glossary.md"
    Glossary([_sasaki(note="the neighbour")]).save(path, md)

    text = md.read_text(encoding="utf-8")
    assert "Sasaki" in text
    assert SASAKI_KATA in text
    assert "the neighbour" in text


def test_the_lock_is_the_shared_one(tmp_path):
    from morning.locks import file_lock

    assert glossary_lock(tmp_path / "glossary.json") is file_lock(tmp_path / "glossary.json")


# ---- the prompt block --------------------------------------------------------

def test_the_prompt_block_carries_the_character_hints():
    """Japanese omits pronouns constantly and marks register in verb endings. English
    has neither, so the model can only stay consistent if it is told every chapter."""
    glossary = Glossary([_sasaki(pronoun="she", first_person="あたし",
                                 register="polite")])
    block = glossary.to_prompt_block(f"{SASAKI_KANJI}は笑った。")

    assert "Sasaki" in block
    assert "she" in block
    assert "あたし" in block
    assert "polite" in block


def test_the_prompt_block_lists_every_spelling():
    glossary = Glossary([_sasaki()])
    block = glossary.to_prompt_block(f"{SASAKI_KATA}さん")

    for spelling in (SASAKI_KANJI, SASAKI_HIRA, SASAKI_KATA):
        assert spelling in block


def test_the_prompt_block_is_empty_when_nothing_is_relevant():
    """An empty glossary must not add a heading with nothing under it."""
    assert Glossary().to_prompt_block("anything") == ""
    assert Glossary([_sasaki()]).to_prompt_block("雨が降る") == ""


def test_the_prompt_block_includes_canonical_names_even_without_a_match():
    glossary = Glossary([GlossaryEntry(english="Haruka", type="name")])

    assert "Haruka" in glossary.to_prompt_block("雨が降る")


# ---- the pending queue -------------------------------------------------------

def test_proposed_terms_land_in_the_queue_not_the_glossary():
    """The gate. A wrong reading of a name would otherwise propagate silently through
    every later chapter, and Japanese makes wrong readings easy — most kanji have
    several."""
    queue, added = merge_pending([], [{"source": SASAKI_KANJI, "english": "Sasaki"}])

    assert added == 1
    assert queue[0]["source"] == SASAKI_KANJI


def test_a_term_already_in_the_glossary_is_not_queued():
    glossary = Glossary([_sasaki()])
    queue, added = merge_pending([], [{"source": SASAKI_KANJI, "english": "Sasaki"}],
                                 glossary=glossary)

    assert (queue, added) == ([], 0)


def test_a_term_already_in_the_queue_is_not_queued_twice():
    """The same name appears in thirty chapters."""
    existing = [{"source": SASAKI_KANJI, "english": "Sasaki"}]
    queue, added = merge_pending(existing, [{"source": SASAKI_KANJI, "english": "Sasaki"}])

    assert added == 0
    assert len(queue) == 1


def test_a_new_spelling_of_a_known_name_becomes_a_variant_not_a_new_term():
    """Otherwise the same character is queued once per script they are written in,
    and the reviewer has to notice they are the same person."""
    glossary = Glossary([GlossaryEntry(source=SASAKI_KANJI, english="Sasaki", type="name")])
    queue, added = merge_pending([], [{"source": SASAKI_KATA, "english": "Sasaki"}],
                                 glossary=glossary)

    assert (queue, added) == ([], 0)
    assert glossary.relevant_to(f"{SASAKI_KATA}さん")


def test_a_model_proposed_pronoun_is_normalised():
    queue, _ = merge_pending([], [{"source": MORITA, "english": "Morita",
                                   "pronoun": "unknown"}])

    assert queue[0]["pronoun"] == ""


def test_junk_proposals_are_skipped_rather_than_stored():
    queue, added = merge_pending([], ["not a dict", {"english": "no source"}, {}])

    assert (queue, added) == ([], 0)


def test_normalize_pronoun_accepts_only_the_three():
    for good in ("he", "she", "they", "  SHE  "):
        assert normalize_pronoun(good) in {"he", "she", "they"}
    for bad in ("unknown", "", None, "it", 7):
        assert normalize_pronoun(bad) == ""


def test_the_queue_round_trips(tmp_path):
    path = tmp_path / "glossary_pending.json"
    save_pending(path, [{"source": SASAKI_KANJI, "english": "Sasaki"}])

    assert load_pending(path)[0]["source"] == SASAKI_KANJI


def test_a_corrupt_queue_degrades_and_keeps_the_bytes(tmp_path):
    path = tmp_path / "glossary_pending.json"
    path.write_text("[{broken", encoding="utf-8")

    assert load_pending(path) == []
    assert len(list(tmp_path.glob("*.unreadable-*"))) == 1


def test_a_missing_queue_loads_as_empty(tmp_path):
    assert load_pending(tmp_path / "nothing.json") == []
