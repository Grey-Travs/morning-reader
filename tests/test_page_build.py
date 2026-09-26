"""Joining pages into chapters.

The seam that matters is ``sentence``. A page break mid-sentence is extremely common
and completely invisible in the text — the page simply stops. Join it as a paragraph
and the novel gains a false break mid-line; join a real paragraph break as a sentence
and two paragraphs weld together.

This file also carries the round-trip the plan asks for by name: upload → regions →
flatten → chapters, asserting the flattened text is identical to what a flat-text read
would have produced, so regions cost the novel path nothing.

Japanese fixtures are invented.
"""

from __future__ import annotations

import pytest

from morning.chapters import Chapter
from morning.page_build import Join, assemble, has_unclosed_quote, propose_join
from morning.pageread import (
    GLUE_NONE, GLUE_SPACE, JOIN_CHAPTER, JOIN_GAP, JOIN_PARAGRAPH, JOIN_SENTENCE,
    KIND_BODY, KIND_PAGE_NUMBER, PageMeta, PageRead, Region, from_pixels,
)
from server.pages import page_text

W, H = 1600, 2400

# Invented prose. A single sentence deliberately split across a page break, which is
# the case the whole module exists for.
HALF_ONE = "電車はまだ"          # "the train has not yet"
HALF_TWO = "来ない。"                # "come."
WHOLE = HALF_ONE + HALF_TWO
LATER = "彼女はホームに立っていた。"


def _page(text: str, *, seq: int = 1, heading=None, ends_mid_sentence=False,
          starts_mid_sentence=False, ends_mid_word=False, join=None,
          glue=GLUE_NONE, name="") -> dict:
    """A page record shaped the way the manifest stores one."""
    read = PageRead(
        width=W, height=H,
        meta=PageMeta(confidence="high", heading=heading,
                      ends_mid_sentence=ends_mid_sentence,
                      starts_mid_sentence=starts_mid_sentence,
                      ends_mid_word=ends_mid_word),
        regions=[Region(id="r0", order=0, kind=KIND_BODY, text=text,
                        box=from_pixels(100, 100, 400, 2000, W, H))],
    ).to_dict()
    page = {"seq": seq, "name": name, "read": read, "join_glue": glue}
    if join is not None:
        page["join_prev"] = join
    return page


# ---- proposing a seam --------------------------------------------------------

def test_a_printed_chapter_heading_wins_over_everything():
    """The book itself is saying where the boundary falls."""
    previous = _page(WHOLE, ends_mid_sentence=True)
    following = _page(LATER, heading="第2話", starts_mid_sentence=True)

    join = propose_join(WHOLE, LATER, previous, following)

    assert join.kind == JOIN_CHAPTER
    assert join.confidence > 0.9
    assert "第2話" in join.reason


def test_a_word_split_across_the_break_is_a_continuation():
    """Nothing else explains a word cut in half, and joining it as anything but a
    continuation leaves half a word at the end of a paragraph."""
    join = propose_join(HALF_ONE, HALF_TWO,
                        _page(HALF_ONE, ends_mid_word=True), _page(HALF_TWO))

    assert join.kind == JOIN_SENTENCE
    assert join.confidence > 0.9


def test_both_sides_agreeing_the_sentence_runs_on():
    join = propose_join(
        HALF_ONE, HALF_TWO,
        _page(HALF_ONE, ends_mid_sentence=True),
        _page(HALF_TWO, starts_mid_sentence=True))

    assert join.kind == JOIN_SENTENCE
    assert join.glue == GLUE_NONE, "Japanese has no spaces between words"


def test_a_disagreement_is_reported_as_a_possible_missing_page():
    """One side stops mid-sentence and the other starts fresh. A missing page is the
    one thing a reader cannot recover by re-reading, so it is surfaced rather than
    smoothed over."""
    join = propose_join(
        HALF_ONE, LATER,
        _page(HALF_ONE, ends_mid_sentence=True),
        _page(LATER, starts_mid_sentence=False))

    assert join.kind == JOIN_GAP
    assert "may be missing" in join.reason


def test_a_page_opening_with_a_continuation_particle_is_not_a_gap():
    """A closing bracket or a particle at the start is strong evidence the sentence
    carries on, whatever the flag said."""
    join = propose_join(
        HALF_ONE, "」と彼は言った。",
        _page(HALF_ONE, ends_mid_sentence=True),
        _page("」と彼は言った。"))

    assert join.kind == JOIN_SENTENCE


def test_a_full_stop_in_the_text_is_trusted_over_an_absent_flag():
    """A model that forgot to set a flag still transcribed the full stop it could
    see."""
    join = propose_join(WHOLE, LATER, _page(WHOLE), _page(LATER))

    assert join.kind == JOIN_PARAGRAPH
    assert "ends a sentence" in join.reason


def test_no_signal_at_all_defaults_to_paragraph():
    """The safe direction, for the same reason it is safe inside a page: a spurious
    break is visible and fixable, a missing one welds two paragraphs together where
    nobody will notice."""
    join = propose_join("no terminal mark here", LATER,
                        _page("no terminal mark here"), _page(LATER))

    assert join.kind == JOIN_PARAGRAPH
    assert join.confidence < 0.6, "and it says it is unsure"


def test_a_page_with_no_read_still_gets_a_proposal():
    """An unread page must not crash the proposal for the one after it."""
    join = propose_join("", LATER, {"seq": 1}, {"seq": 2})

    assert join.kind in (JOIN_PARAGRAPH, JOIN_SENTENCE, JOIN_GAP, JOIN_CHAPTER)


# ---- speech that runs across the break ----------------------------------------
# Night Reader's rule: a page that ends inside a quotation carries on onto the next.
# It was not carried over, so a page ending 「明日の朝、駅で was never evidence of
# anything, and one ending 「今日は雨だ。 was taken as a finished paragraph.

OPEN = "「今日は雨だ。"                       # speech still open, full stop and all
CONTINUES = "明日は晴れるだろう。」と彼は言った。"


def _join(previous_text, following_text, **flags):
    ends = {k: v for k, v in flags.items() if k.startswith("ends")}
    starts = {k: v for k, v in flags.items() if k.startswith("starts")}
    return propose_join(previous_text, following_text,
                        _page(previous_text, **ends), _page(following_text, **starts))


def test_open_speech_continues_onto_the_next_page():
    join = _join("「あの時言ったじゃない", "だからもうやめて。」")

    assert join.kind == JOIN_SENTENCE
    assert "quotation is still open" in join.reason


def test_a_full_stop_inside_open_speech_does_not_end_the_paragraph():
    """「今日は雨だ。 ends in 。 and is still inside the speech. The full-stop rule
    alone split one line of dialogue into two paragraphs."""
    assert _join(OPEN, CONTINUES).kind == JOIN_SENTENCE


def test_open_speech_is_not_mistaken_for_a_missing_page():
    """A model flags an open 「 as ending mid-sentence without flagging the next page
    as starting mid-sentence. That disagreement read as a page never photographed, and
    the build warned about a hole that was not there."""
    join = _join(OPEN, CONTINUES, ends_mid_sentence=True, starts_mid_sentence=False)

    assert join.kind == JOIN_SENTENCE
    assert "may be missing" not in join.reason


def test_a_fresh_quotation_is_not_the_same_speech():
    """The next page opening its own 「 cannot be continuing the open one."""
    assert _join("「あの時言ったじゃない", "「何の話？」").kind != JOIN_SENTENCE


@pytest.mark.parametrize("previous_text,following_text", [
    ("「彼が『明日", "来る』と言った」"),     # both open
    ("「彼は『行く』", "と言った。」"),         # inner closed, outer still open — and
])                                           # the trailing 』 alone looks like an end
def test_nested_quotation_marks(previous_text, following_text):
    assert _join(previous_text, following_text).kind == JOIN_SENTENCE


def test_a_closed_quotation_still_ends_a_sentence():
    """Japanese usually drops the 。 before 」, so 「行くぞ」 is a whole line of speech.
    Night Reader's own end-of-sentence check does not know that; it stays out."""
    assert _join("「行くぞ」", "彼は歩き出した。").kind == JOIN_PARAGRAPH


def test_a_chapter_heading_still_beats_open_speech():
    previous = _page("「今日は")
    following = _page("朝が来た。", heading="第2話")

    assert propose_join("「今日は", "朝が来た。", previous, following).kind == \
        JOIN_CHAPTER


def test_open_speech_before_a_page_with_no_prose_proves_nothing():
    """An illustration or a title page flattens to no prose. An empty first character
    must not count as 'not an opener' — the frozenset rather than a string is what
    stops that."""
    assert _join(OPEN, "").kind != JOIN_SENTENCE


def test_vertical_typesetting_quotes_pair_too():
    assert _join("〝今日は雨だ。", "明日だ〟と言った。").kind == JOIN_SENTENCE


class TestIsAQuotationStillOpen:
    @pytest.mark.parametrize("text", [
        "「あの時言ったじゃない", "『本の題", '"どこへ行くの', "「彼が『明日』と",
        "〝今日は",
    ])
    def test_open(self, text):
        assert has_unclosed_quote(text)

    @pytest.mark.parametrize("text", [
        "「あの時言ったじゃない」", "『本』", '"どこへ?" と彼が聞いた。',
        "「x」「y」", "〝今日は〞", "", "彼女はホームに立っていた。",
    ])
    def test_closed(self, text):
        assert not has_unclosed_quote(text)

    def test_a_page_that_closes_old_speech_and_opens_new_speech_is_still_open(self):
        """Whole-page counting calls this balanced — one 「 and one 」 — and it is the
        ordinary shape of the page after a speech ran across the break."""
        assert has_unclosed_quote("だろう。」と彼は言った。\n\n「今日は雨だ。")

    def test_a_stray_closer_does_not_cancel_a_later_opener(self):
        assert has_unclosed_quote("」と言った。「待って")


# ---- assembling --------------------------------------------------------------

def test_a_sentence_split_across_two_pages_is_rejoined():
    """The whole point. Joined as a paragraph, the novel would carry a false break in
    the middle of a line."""
    pages = [_page(HALF_ONE, seq=1),
             _page(HALF_TWO, seq=2, join=JOIN_SENTENCE)]

    built = assemble(pages, text_of=page_text)

    assert len(built.chapters) == 1
    assert built.chapters[0].paragraphs == [WHOLE]


def test_a_paragraph_seam_starts_a_new_paragraph():
    pages = [_page(WHOLE, seq=1), _page(LATER, seq=2, join=JOIN_PARAGRAPH)]

    built = assemble(pages, text_of=page_text)

    assert built.chapters[0].paragraphs == [WHOLE, LATER]


def test_a_chapter_seam_starts_a_new_chapter():
    pages = [_page(WHOLE, seq=1),
             _page(LATER, seq=2, join=JOIN_CHAPTER, heading="第2話")]

    built = assemble(pages, text_of=page_text)

    assert len(built.chapters) == 2
    assert built.chapters[1].title == "第2話"
    assert [c.index for c in built.chapters] == [1, 2]


def test_a_printed_heading_names_the_chapter_without_being_repeated():
    """If the heading is already the first line of the page, prepending it again would
    print the title twice."""
    pages = [_page(f"第1話\n\n{WHOLE}", seq=1, heading="第1話")]

    built = assemble(pages, text_of=page_text)

    assert built.chapters[0].title == "第1話"
    assert built.chapters[0].text.count("第1話") == 1


def test_a_heading_missing_from_the_text_is_restored():
    """The split needs something to break on, and a chapter whose title appears only
    in metadata would open mid-scene."""
    pages = [_page(WHOLE, seq=1, heading="第1話")]

    built = assemble(pages, text_of=page_text)

    assert built.chapters[0].text.startswith("第1話")


def test_a_gap_is_joined_but_reported():
    """It is the only place a page you never photographed can surface. Swallowing it
    leaves a novel with a silent hole in the middle of a scene."""
    pages = [_page(HALF_ONE, seq=1),
             _page(LATER, seq=2, join=JOIN_GAP, name="IMG_0042.jpg")]

    built = assemble(pages, text_of=page_text)

    assert len(built.chapters) == 1, "the text still reads"
    assert built.warnings and "IMG_0042.jpg" in built.warnings[0]
    assert "may be missing" in built.warnings[0]


def test_a_blank_page_is_skipped_without_breaking_the_join():
    """A photographed blank leaf is ordinary and must not become an empty paragraph
    or split a sentence in two."""
    pages = [_page(HALF_ONE, seq=1),
             _page("", seq=2, join=JOIN_PARAGRAPH),
             _page(HALF_TWO, seq=3, join=JOIN_SENTENCE)]

    built = assemble(pages, text_of=page_text)

    assert built.chapters[0].paragraphs == [WHOLE]
    assert built.pages_used == 2


def test_nothing_assembles_to_nothing():
    built = assemble([], text_of=page_text)

    assert built.chapters == [] and built.warnings == []


def test_a_space_glue_is_honoured_when_asked_for():
    """The join is between words of a language that needs one — a quoted title, a
    transliterated name."""
    pages = [_page("the sentence runs", seq=1),
             _page("across the page break", seq=2, join=JOIN_SENTENCE,
                   glue=GLUE_SPACE)]

    built = assemble(pages, text_of=page_text)

    assert built.chapters[0].paragraphs == ["the sentence runs across the page break"]


# ---- the round trip ----------------------------------------------------------

def test_a_scanned_novel_round_trips_into_ordinary_chapters():
    """The plan's acceptance test: upload -> regions -> flatten -> chapters.

    What comes out is an ordinary list of Chapter objects, indistinguishable from a
    paste — which is what lets the whole translate/validate/read pipeline run on a
    scanned novel without knowing it was ever a stack of photographs.
    """
    pages = [
        _page("第1話\n\n" + HALF_ONE, seq=1, heading="第1話",
              ends_mid_sentence=True),
        _page(HALF_TWO + "\n\n" + LATER, seq=2, join=JOIN_SENTENCE),
        _page(WHOLE, seq=3, join=JOIN_CHAPTER, heading="第2話"),
    ]

    built = assemble(pages, text_of=page_text)

    assert [c.title for c in built.chapters] == ["第1話", "第2話"]
    assert isinstance(built.chapters[0], Chapter)
    # The split sentence survived the page break intact.
    assert WHOLE in built.chapters[0].text
    # And every chapter is measurable and classifiable like any other.
    for chapter in built.chapters:
        assert chapter.metrics.content_hash
        assert chapter.metrics.source_fraction > 0.8


def test_flattening_regions_gives_what_flat_text_would_have():
    """The plan states this one directly: regions must cost the novel path nothing.

    The same page described as three regions and as one flat block must produce the
    same string, or every scanned novel already processed would need re-reading when
    regions arrived.
    """
    as_regions = PageRead(
        width=W, height=H, meta=PageMeta(confidence="high"),
        regions=[
            Region(id="r0", order=0, kind=KIND_BODY, text=HALF_ONE,
                   box=from_pixels(1180, 200, 300, 1900, W, H)),
            Region(id="r1", order=1, kind=KIND_BODY, text=HALF_TWO,
                   join_prev=JOIN_SENTENCE, join_glue=GLUE_NONE,
                   box=from_pixels(820, 200, 300, 1900, W, H)),
            Region(id="r2", order=2, kind=KIND_BODY, text=LATER,
                   join_prev=JOIN_PARAGRAPH,
                   box=from_pixels(460, 200, 300, 1900, W, H)),
            # Furniture a flat-text read would never have returned as prose.
            Region(id="r3", order=3, kind=KIND_PAGE_NUMBER, text="12",
                   box=from_pixels(760, 2280, 80, 60, W, H)),
        ],
    ).to_dict()

    what_flat_text_would_be = WHOLE + "\n\n" + LATER

    assert page_text({"read": as_regions}) == what_flat_text_would_be
