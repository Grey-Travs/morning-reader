"""One manga chapter through the engine: collect, render, call, read the answer.

No test here makes a real model call. ``translator._call`` is replaced, which is the
same seam ``morning/ocr.py`` is exercised through and the same one the worker builds.

Japanese fixtures are invented for these tests.

The parser gets the most attention, and deliberately. A chapter is a hundred and fifty
lines the owner already paid to have READ; the translation call is a few percent of
that spend on top. So the expensive mistake is not a bad answer — it is a parser that
throws a whole good answer away because the model added a preamble or dropped two
records.
"""

from __future__ import annotations

import pytest

from morning.config import TranslationConfig
from morning.manga import (
    LineResult, ScriptLine, collect_lines, continuity_tail, page_blocks,
    parse_script_response, render_pages, should_retry, translate_script,
)
from morning.pageread import (
    KIND_BUBBLE, KIND_PAGE_NUMBER, KIND_SFX, KIND_SIGN, PageMeta, PageRead, Region,
)
from morning.prompts import NEW_TERMS_DELIMITER

TAB = "\t"

# Invented, short, and shaped like what a manga page actually carries: a question and
# an answer whose subject is nowhere in the answer, plus a drawn sound.
Q = "もう無理だって"
A = "……そう?"
SFX = "ドン"
SIGN = "定休日"


def region(rid: str, text: str, *, kind: str = KIND_BUBBLE, order: int = -1,
           box=(0.1, 0.1, 0.2, 0.2)) -> Region:
    # -1, not 0, as "unset". Using 0 would make an explicit `order=0` indistinguishable
    # from no order at all, and the helper below would then renumber it — which quietly
    # broke two of these tests into passing for the wrong reason.
    return Region(id=rid, box=box, text=text, kind=kind, order=order)


def read(regions: list[Region], *, width: int = 1600, height: int = 2400) -> PageRead:
    for i, r in enumerate(regions):
        if r.order < 0:
            r.order = i
    return PageRead(width=width, height=height, regions=regions,
                    meta=PageMeta(confidence="high"))


class FakeTranslator:
    """Stands in at the seam the worker builds through. Records what it was asked."""

    def __init__(self, answers: list[str] | None = None,
                 tcfg: TranslationConfig | None = None):
        self.answers = list(answers or [])
        self.tcfg = tcfg or TranslationConfig()
        self.calls: list[dict] = []

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        self.calls.append({"system": system_text, "user": user_text})
        answer = self.answers.pop(0) if self.answers else ""
        return answer, {"input_tokens": 100, "output_tokens": 50}, 0.01


def answer(rows: list[tuple[str, str, str]], terms: str = "[]") -> str:
    body = "\n".join(TAB.join(row) for row in rows)
    return f"{body}\n{NEW_TERMS_DELIMITER}\n{terms}"


# ---- collecting --------------------------------------------------------------

class TestCollecting:
    def test_it_takes_the_translatable_kinds_in_reading_order(self):
        lines = collect_lines([(5, read([region("r0", Q, order=1),
                                         region("r1", A, order=0)]))])
        assert [line.text for line in lines] == [A, Q]

    def test_a_sign_and_a_sound_effect_are_collected(self):
        lines = collect_lines([(5, read([region("r0", SFX, kind=KIND_SFX),
                                         region("r1", SIGN, kind=KIND_SIGN)]))])
        assert {line.text for line in lines} == {SFX, SIGN}

    def test_page_furniture_is_not(self):
        lines = collect_lines([(5, read([region("r0", Q),
                                         region("r1", "42", kind=KIND_PAGE_NUMBER)]))])
        assert [line.text for line in lines] == [Q]

    def test_an_empty_region_contributes_nothing(self):
        lines = collect_lines([(5, read([region("r0", "   "), region("r1", Q)]))])
        assert [line.text for line in lines] == [Q]

    def test_a_line_id_carries_its_page(self):
        """Region ids are page-local and repeat on every page, so `r0` alone names a
        hundred different bubbles in a volume."""
        lines = collect_lines([(7, read([region("r0", Q)]))])
        assert lines[0].id == "7:r0"

    def test_two_pages_can_both_have_r0(self):
        lines = collect_lines([(1, read([region("r0", Q)])),
                               (2, read([region("r0", A)]))])
        assert [line.id for line in lines] == ["1:r0", "2:r0"]

    def test_each_line_carries_the_hash_of_its_own_japanese(self):
        from morning.pageread import region_hash

        r = region("r0", Q)
        lines = collect_lines([(1, read([r]))])
        assert lines[0].source_hash == region_hash(r)

    def test_panels_are_numbered_from_one_in_reading_order(self):
        lines = collect_lines([(1, read([
            region("tr", Q, box=(0.55, 0.05, 0.40, 0.40), order=0),
            region("tl", A, box=(0.05, 0.05, 0.40, 0.40), order=1),
            region("br", SFX, kind=KIND_SFX, box=(0.55, 0.55, 0.40, 0.40), order=2),
        ]))])
        assert [line.panel for line in lines] == [1, 2, 3]

    def test_panels_stay_ascending_even_when_a_human_reordered_the_page(self):
        """Geometry and the human's order can disagree. A script whose headers ran
        'panel 2, panel 1, panel 2' would read as an error rather than as the reading
        order it faithfully is."""
        lines = collect_lines([(1, read([
            region("tr", Q, box=(0.55, 0.05, 0.40, 0.40), order=2),
            region("tl", A, box=(0.05, 0.05, 0.40, 0.40), order=0),
            region("br", SFX, kind=KIND_SFX, box=(0.55, 0.55, 0.40, 0.40), order=1),
        ]))])
        assert [line.panel for line in lines] == [1, 2, 3]

    def test_a_page_with_no_geometry_still_collects(self):
        lines = collect_lines([(1, read([region("r0", Q, box=(0, 0, 0, 0)),
                                         region("r1", A, box=(0, 0, 0, 0))],
                                        width=0, height=0))])
        assert [line.text for line in lines] == [Q, A]

    def test_nothing_at_all_collects_nothing(self):
        assert collect_lines([]) == []


# ---- rendering ---------------------------------------------------------------

class TestRendering:
    def test_a_line_is_id_kind_and_text(self):
        lines = collect_lines([(12, read([region("r0", Q)]))])
        block = render_pages(lines)[0]
        assert f"12:r0{TAB}[bubble]{TAB}{Q}" in block

    def test_each_page_is_its_own_block(self):
        lines = collect_lines([(1, read([region("r0", Q)])),
                               (2, read([region("r0", A)]))])
        blocks = render_pages(lines)
        assert len(blocks) == 2
        assert Q in blocks[0] and A in blocks[1]

    def test_a_page_header_names_the_page_and_the_panel(self):
        lines = collect_lines([(12, read([
            region("tr", Q, box=(0.55, 0.05, 0.40, 0.40)),
            region("tl", A, box=(0.05, 0.05, 0.40, 0.40)),
        ]))])
        block = render_pages(lines)[0]
        assert "Page 12 — panel 1" in block
        assert "Page 12 — panel 2" in block

    def test_a_page_with_no_panels_still_gets_a_header(self):
        lines = collect_lines([(3, read([region("r0", Q, box=(0, 0, 0, 0))],
                                        width=0, height=0))])
        assert render_pages(lines)[0].startswith("Page 3")

    def test_the_ids_are_carried_beside_the_block_not_searched_for_later(self):
        """Searching a block for `1:r0` would also match `11:r0`, so a chapter long
        enough to chunk would tell one call it owned another call's lines — and those
        lines would come back reported missing."""
        lines = collect_lines([(1, read([region("r0", Q)])),
                               (11, read([region("r0", A)]))])
        blocks = page_blocks(lines)
        assert [ids for _, ids in blocks] == [["1:r0"], ["11:r0"]]


# ---- reading the answer ------------------------------------------------------

class TestParsing:
    def test_an_ordinary_answer(self):
        lines, terms, warnings = parse_script_response(
            answer([("1:r0", "Aoi", "I told you it was impossible."),
                    ("1:r1", "Kenji", "...Did you?")]),
            ["1:r0", "1:r1"])

        assert lines["1:r0"] == LineResult("I told you it was impossible.", "Aoi")
        assert lines["1:r1"].speaker == "Kenji"
        assert terms == [] and warnings == []

    def test_an_empty_speaker_is_allowed(self):
        """An honest blank is worth far more than a guess — a wrong name is carried
        into every later chapter."""
        lines, _, _ = parse_script_response(answer([("1:r0", "", "KRASH")]), ["1:r0"])
        assert lines["1:r0"] == LineResult("KRASH", "")

    def test_a_two_field_record_is_taken_as_id_and_english(self):
        raw = f"1:r0{TAB}Just the English.\n{NEW_TERMS_DELIMITER}\n[]"
        lines, _, _ = parse_script_response(raw, ["1:r0"])
        assert lines["1:r0"] == LineResult("Just the English.", "")

    def test_english_containing_a_tab_is_kept_whole(self):
        raw = f"1:r0{TAB}Aoi{TAB}one{TAB}two\n{NEW_TERMS_DELIMITER}\n[]"
        lines, _, _ = parse_script_response(raw, ["1:r0"])
        assert lines["1:r0"].english == f"one{TAB}two"

    def test_a_preamble_is_ignored_rather_than_fatal(self):
        raw = ("Here are the translations:\n\n"
               + answer([("1:r0", "Aoi", "Fine.")]))
        lines, _, warnings = parse_script_response(raw, ["1:r0"])
        assert lines["1:r0"].english == "Fine."
        assert any("not records" in w for w in warnings)

    def test_a_code_fence_is_ignored(self):
        raw = "```\n" + answer([("1:r0", "", "Fine.")]) + "\n```"
        lines, _, _ = parse_script_response(raw, ["1:r0"])
        assert lines["1:r0"].english == "Fine."

    def test_a_truncated_answer_keeps_every_complete_record(self):
        """The whole reason the contract is tab-separated. A truncated JSON array
        yields nothing; here the hundred records before the cut are still usable."""
        raw = (f"1:r0{TAB}Aoi{TAB}First line.\n"
               f"1:r1{TAB}Kenji{TAB}Second line.\n"
               f"1:r2{TAB}Aoi{TAB}Third line, cut off mid-")
        lines, _, warnings = parse_script_response(raw, ["1:r0", "1:r1", "1:r2"])
        assert len(lines) == 3
        assert any(NEW_TERMS_DELIMITER in w for w in warnings)

    def test_missing_lines_are_reported_and_the_rest_kept(self):
        lines, _, warnings = parse_script_response(
            answer([("1:r0", "", "Fine.")]), ["1:r0", "1:r1", "1:r2"])
        assert set(lines) == {"1:r0"}
        assert any("did not come back" in w for w in warnings)

    def test_a_record_for_a_line_that_is_not_in_this_chapter_is_dropped(self):
        """A stricter filter than any chatter heuristic: the id has to be one this
        chapter actually contains."""
        lines, _, warnings = parse_script_response(
            answer([("9:r9", "", "From somewhere else."),
                    ("1:r0", "", "Fine.")]), ["1:r0"])
        assert set(lines) == {"1:r0"}
        assert any("not in this chapter" in w for w in warnings)

    def test_a_repeated_line_keeps_the_first(self):
        """A repeated record is far more often a duplicate than a correction, and
        preferring the later one lets a stray echo overwrite the real translation."""
        lines, _, warnings = parse_script_response(
            answer([("1:r0", "", "First."), ("1:r0", "", "Second.")]), ["1:r0"])
        assert lines["1:r0"].english == "First."
        assert any("came back twice" in w for w in warnings)

    def test_new_terms_flow_through(self):
        raw = answer([("1:r0", "Aoi", "Fine.")],
                     terms='[{"source": "葵", "english": "Aoi", "type": "name"}]')
        _, terms, _ = parse_script_response(raw, ["1:r0"])
        assert terms == [{"source": "葵", "english": "Aoi", "type": "name"}]

    def test_a_malformed_terms_block_does_not_lose_the_translation(self):
        raw = answer([("1:r0", "", "Fine.")], terms="[{not json")
        lines, terms, warnings = parse_script_response(raw, ["1:r0"])
        assert lines["1:r0"].english == "Fine."
        assert terms == []
        assert any("new-terms" in w for w in warnings)

    def test_an_answer_of_nothing_yields_nothing_rather_than_raising(self):
        lines, terms, warnings = parse_script_response("", ["1:r0"])
        assert lines == {} and terms == []
        assert warnings

    def test_japanese_left_in_a_record_is_kept_not_deleted(self):
        """Nothing here deletes prose. Residue is flagged by validation, never
        removed — the rule the whole app is built on."""
        lines, _, _ = parse_script_response(
            answer([("1:r0", "", "もう無理だって")]), ["1:r0"])
        assert lines["1:r0"].english == "もう無理だって"


# ---- the call ----------------------------------------------------------------

class TestTheCall:
    def test_an_ordinary_chapter_is_one_call(self):
        lines = collect_lines([(1, read([region("r0", Q), region("r1", A)]))])
        translator = FakeTranslator([answer([("1:r0", "Aoi", "No more."),
                                             ("1:r1", "Kenji", "Really?")])])

        result = translate_script(translator, lines)

        assert len(translator.calls) == 1
        assert result.chunks == 1
        assert result.lines["1:r0"].english == "No more."
        assert result.missing == []

    def test_the_voice_contract_is_inherited_not_forked(self):
        """The manga path swaps only the OUTPUT contract. Losing the rules above it
        would ship bubbles without honorifics and without the glossary's pronoun pins
        being authoritative — and every one of those failures is fluent English."""
        lines = collect_lines([(1, read([region("r0", Q)]))])
        translator = FakeTranslator([answer([("1:r0", "", "No more.")])])

        translate_script(translator, lines, glossary_block="葵 -> Aoi [she]")

        system = translator.calls[0]["system"]
        assert "-san" in system and "-senpai" in system      # honorifics
        assert "[he]" in system and "authoritative" in system  # pronoun pins
        assert "onomatopoeia" in system                       # sound
        assert "葵 -> Aoi [she]" in system                     # the glossary block
        assert "one record per line" in system                # ...and the new contract

    def test_the_lines_are_given_in_reading_order(self):
        lines = collect_lines([(1, read([region("r0", Q, order=1),
                                         region("r1", A, order=0)]))])
        translator = FakeTranslator([answer([("1:r1", "", "x"), ("1:r0", "", "y")])])

        translate_script(translator, lines)

        user = translator.calls[0]["user"]
        assert user.index(A) < user.index(Q)

    def test_the_title_is_passed_on_the_first_chunk(self):
        lines = collect_lines([(1, read([region("r0", Q)]))])
        translator = FakeTranslator([answer([("1:r0", "", "x")])])
        translate_script(translator, lines, title="第1話 朝の駅")
        assert "第1話 朝の駅" in translator.calls[0]["user"]

    def test_an_oversized_chapter_chunks_at_page_boundaries(self):
        pages = [(i, read([region("r0", Q * 200)])) for i in range(1, 6)]
        lines = collect_lines(pages)
        tcfg = TranslationConfig(chunk_threshold=2000)
        translator = FakeTranslator(
            [answer([(f"{i}:r0", "", f"part {i}")]) for i in range(1, 6)], tcfg=tcfg)

        result = translate_script(translator, lines)

        assert result.chunks > 1
        assert len(translator.calls) == result.chunks

    def test_a_chunked_chapter_asks_each_call_only_for_its_own_lines(self):
        """The substring trap: page 1 and page 11 both have an `r0`, and a chunk that
        holds only 11:r0 must not be told it owes 1:r0."""
        pages = [(1, read([region("r0", Q * 400)])),
                 (11, read([region("r0", A * 400)]))]
        lines = collect_lines(pages)
        tcfg = TranslationConfig(chunk_threshold=500)
        translator = FakeTranslator([answer([("1:r0", "", "one")]),
                                     answer([("11:r0", "", "eleven")])], tcfg=tcfg)

        result = translate_script(translator, lines)

        assert result.chunks == 2
        assert result.lines["1:r0"].english == "one"
        assert result.lines["11:r0"].english == "eleven"
        assert result.missing == []
        assert not any("did not come back" in w for w in result.warnings)

    def test_a_later_chunk_is_given_the_previous_one_s_english(self):
        """Manga speech breaks across a page turn far more often than prose does, so
        the tail is frequently the only place the next bubble's subject appears."""
        pages = [(1, read([region("r0", Q * 400)])),
                 (2, read([region("r0", A * 400)]))]
        lines = collect_lines(pages)
        tcfg = TranslationConfig(chunk_threshold=500)
        translator = FakeTranslator([answer([("1:r0", "", "I said no more.")]),
                                     answer([("2:r0", "", "Did you?")])], tcfg=tcfg)

        translate_script(translator, lines)

        assert "I said no more." in translator.calls[1]["user"]
        assert "do not output records for these" in translator.calls[1]["user"].lower()

    def test_spend_accumulates_across_chunks(self):
        pages = [(i, read([region("r0", Q * 400)])) for i in (1, 2)]
        lines = collect_lines(pages)
        tcfg = TranslationConfig(chunk_threshold=500)
        translator = FakeTranslator([answer([]), answer([])], tcfg=tcfg)

        result = translate_script(translator, lines)

        assert result.cost_usd == pytest.approx(0.02)
        assert result.usage["input_tokens"] == 200

    def test_a_chapter_with_no_lines_costs_nothing(self):
        translator = FakeTranslator([])
        result = translate_script(translator, [])
        assert translator.calls == []
        assert result.lines == {} and result.cost_usd == 0.0

    def test_a_retry_hint_reaches_the_model(self):
        lines = collect_lines([(1, read([region("r0", Q)]))])
        translator = FakeTranslator([answer([("1:r0", "", "x")])])
        translate_script(translator, lines, retry_hint="\n\nTRY AGAIN")
        assert translator.calls[0]["user"].endswith("TRY AGAIN")


class TestWhetherToPayTwice:
    @staticmethod
    def _lines(n: int) -> list[ScriptLine]:
        return [ScriptLine(page_seq=1, region_id=f"r{i}", kind=KIND_BUBBLE,
                           text=Q, source_hash="h") for i in range(n)]

    def test_nothing_came_back_is_worth_a_retry(self):
        from morning.manga import ScriptResult

        assert should_retry(self._lines(10), ScriptResult())

    def test_two_missing_out_of_a_hundred_and_fifty_is_not(self):
        """Re-billing a whole chapter to recover two bubbles is a worse trade than
        showing those two in Japanese and letting the owner ask — the same reasoning
        pipeline.py uses for capping the prose retry at one."""
        from morning.manga import ScriptResult

        lines = self._lines(150)
        result = ScriptResult(lines={line.id: LineResult("x") for line in lines[:148]},
                              missing=[line.id for line in lines[148:]])
        assert not should_retry(lines, result)

    def test_half_the_chapter_missing_is(self):
        from morning.manga import ScriptResult

        lines = self._lines(10)
        result = ScriptResult(lines={line.id: LineResult("x") for line in lines[:5]},
                              missing=[line.id for line in lines[5:]])
        assert should_retry(lines, result)

    def test_an_empty_chapter_is_never_retried(self):
        from morning.manga import ScriptResult

        assert not should_retry([], ScriptResult())


class TestTheContinuityTail:
    def test_it_pairs_japanese_with_the_english_already_produced(self):
        lines = [ScriptLine(1, "r0", KIND_BUBBLE, Q, "h"),
                 ScriptLine(1, "r1", KIND_BUBBLE, A, "h")]
        produced = {"1:r0": LineResult("No more."), "1:r1": LineResult("Really?")}

        tail = continuity_tail(lines, produced, 2)

        assert "No more." in tail and "Really?" in tail
        assert Q in tail and A in tail

    def test_it_takes_only_the_last_few(self):
        lines = [ScriptLine(1, f"r{i}", KIND_BUBBLE, f"line{i}", "h")
                 for i in range(10)]
        tail = continuity_tail(lines, {}, 3)
        assert "line9" in tail and "line0" not in tail

    def test_a_line_with_no_english_yet_still_carries_its_japanese(self):
        lines = [ScriptLine(1, "r0", KIND_BUBBLE, Q, "h")]
        assert continuity_tail(lines, {}, 1).strip() == Q

    def test_asking_for_none_gives_none(self):
        lines = [ScriptLine(1, "r0", KIND_BUBBLE, Q, "h")]
        assert continuity_tail(lines, {}, 0) == ""
