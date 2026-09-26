"""Step 5: printed text on a page that is not the story.

The plan expected a stripper for text copied out of a Japanese site, derived from real
exports the way the Korean app's was. The real samples were page images, and the raws
arrived clean — no viewer interface, no logo, no stamp — so there was nothing to strip.
What the samples did carry was two shapes of printed text that are not the story: page
numbers in the decorated print style (—26—, —9—, —13—) on the raws, and, on the finished
English pages, a site's address stamped onto the art — which a raw from another source
can carry just as well.

Both are decided from the text itself rather than left to the page reader's label,
because a miss is expensive either way: in a novel a page number is flattened into the
prose, and in a manga it is translated and drawn over the art.

Every string below is invented or generic. The owner's sample pages stay out of the
repository — they are someone else's artwork.
"""

from __future__ import annotations

import pytest

from morning import ocr
from morning.manga import collect_lines
from morning.pageread import (
    FURNITURE_KINDS, KIND_BODY, KIND_BUBBLE, KIND_CAPTION, KIND_PAGE_NUMBER,
    KIND_RUNNING_HEAD, KIND_WATERMARK, REGION_KINDS, PageMeta, PageRead, Region,
    flatten, furniture_kind, mark_furniture, page_from_dict, validate,
)

PROSE = "彼女は駅のホームに立っていた。"


def region(rid: str, text: str, kind: str = KIND_BUBBLE, order: int = 0) -> Region:
    return Region(id=rid, box=(0.1, 0.1 + order * 0.2, 0.3, 0.1), text=text,
                  kind=kind, order=order)


# ---- what counts ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "—26—", "—9—", "—13—",          # the shape in the samples
    "－２６－",                       # full-width, folded by NFKC
    "- 26 -", "– 104 –", "―7―",
    "ー26ー",                         # a long-vowel mark read in place of a dash
    "〜3〜",
])
def test_a_decorated_page_number_is_a_page_number(text):
    assert furniture_kind(text) == KIND_PAGE_NUMBER


@pytest.mark.parametrize("text", [
    "somesite.com",
    "some\nsite.com",                 # a stylised logo stacked over two lines
    "some \n site.com",
    "www.example.net",
    "https://example.org/series/12",
    "ＳＯＭＥＳＩＴＥ．ＣＯＭ",           # full-width, folded by NFKC
    "example.co.jp",
])
def test_a_bare_web_address_is_a_watermark(text):
    assert furniture_kind(text) == KIND_WATERMARK


# ---- what must NOT count --------------------------------------------------------
# The expensive mistake here is the other direction: story text relabelled as
# furniture is dropped from a novel's prose or left untranslated on a manga page.

@pytest.mark.parametrize("text", [
    "26",                   # bare: could be a score, a room, a countdown — left to the model
    "1−1",                  # a classroom sign — digits on BOTH sides of the dash
    "1-1",
    "10月4日(月)",           # a date chalked on a blackboard
    "…26",
    "26ページ",
    "—",                    # a dash with no number
    "——あのさ——",           # a line of dialogue set between dashes
    "ｗｗｗ",               # laughter, not a domain
    "Mr.Tanaka",            # letters.letters, but not a known top-level domain
    "Ver.2.0",
    "Class 1-1",
    "the site is somesite.com",   # an address INSIDE a sentence is dialogue
    "visit somesite.com",
    "some site.com",              # a space is not a line break; left as a line
    "somesite.comに行って",
    PROSE,
    "",
    "   ",
])
def test_story_text_is_left_alone(text):
    assert furniture_kind(text) is None


# ---- how it is applied ----------------------------------------------------------

def test_marking_relabels_and_says_how_many():
    regions = [region("r0", "—26—", KIND_BUBBLE), region("r1", PROSE, KIND_BODY),
               region("r2", "somesite.com", KIND_CAPTION)]

    assert mark_furniture(regions) == 2
    assert [r.kind for r in regions] == [KIND_PAGE_NUMBER, KIND_BODY, KIND_WATERMARK]


def test_marking_twice_changes_nothing_the_second_time():
    regions = [region("r0", "—26—")]
    mark_furniture(regions)
    assert mark_furniture(regions) == 0


def test_a_watermark_the_text_does_not_prove_goes_back_to_being_text():
    """The label is this rule's to give, not the model's. A read stored while the page
    prompt offered it could file a shop sign under it — and furniture is never
    translated."""
    regions = [region("r0", "ニコニコ生放送", KIND_WATERMARK),
               region("r1", "somesite.com", KIND_WATERMARK)]

    assert mark_furniture(regions) == 1
    assert [r.kind for r in regions] == [KIND_BODY, KIND_WATERMARK]


def test_a_region_already_filed_as_furniture_is_left_as_it_is():
    """A running head that happens to look like a page number stays a running head —
    both are furniture, and churning between them helps nobody."""
    regions = [region("r0", "—26—", KIND_RUNNING_HEAD)]
    assert mark_furniture(regions) == 0
    assert regions[0].kind == KIND_RUNNING_HEAD


def test_a_page_read_applies_it():
    raw = ('{"width": 1600, "height": 2400, "regions": ['
           '{"box": [100, 100, 400, 300], "text": "' + PROSE + '", "kind": "body",'
           ' "order": 0},'
           '{"box": [760, 2300, 80, 40], "text": "—26—", "kind": "bubble", "order": 1}'
           '], "meta": {"confidence": "high"}}')

    page = ocr.parse_page_response(raw, width=1600, height=2400)

    assert [r.kind for r in page.regions] == [KIND_BODY, KIND_PAGE_NUMBER]


def test_a_stored_read_is_corrected_when_it_is_loaded():
    """Pages transcribed before the rule existed are fixed on sight, not by paying to
    read them again."""
    stored = {"width": 1600, "height": 2400, "order_source": "model",
              "meta": {"confidence": "high"},
              "regions": [region("r0", PROSE, KIND_BODY).to_dict(),
                          region("r1", "—26—", KIND_BODY, order=1).to_dict()]}

    page = page_from_dict(stored)

    assert page.regions[1].kind == KIND_PAGE_NUMBER


def test_a_novel_page_no_longer_carries_its_page_number_into_the_prose():
    """The novel half of the cost. `body` is prose, so a page number the reader filed
    as `body` used to be flattened straight into the chapter."""
    stored = {"width": 1600, "height": 2400, "order_source": "model",
              "meta": {"confidence": "high"},
              "regions": [region("r0", PROSE, KIND_BODY).to_dict(),
                          region("r1", "—26—", KIND_BODY, order=1).to_dict()]}

    assert flatten(page_from_dict(stored)) == PROSE


def test_a_manga_page_does_not_translate_its_furniture():
    """The manga half: translated and drawn over the art on every page."""
    page = PageRead(width=1600, height=2400, meta=PageMeta(confidence="high"),
                    regions=[region("r0", "なんで？", KIND_BUBBLE, 0),
                             region("r1", "—26—", KIND_BUBBLE, 1),
                             region("r2", "some\nsite.com", KIND_BUBBLE, 2)])
    mark_furniture(page.regions)

    lines = collect_lines([(1, page)])

    assert [line.text for line in lines] == ["なんで？"]


def test_a_caption_on_a_manga_page_is_translated():
    """The correction bundled with this step: a narration box the reader filed as
    `caption` is story text and is now part of the translation call."""
    page = PageRead(width=1600, height=2400, meta=PageMeta(confidence="high"),
                    regions=[region("r0", "あの日から。", KIND_CAPTION, 0)])

    assert [line.text for line in collect_lines([(1, page)])] == ["あの日から。"]


# ---- the contract ---------------------------------------------------------------

def test_watermark_is_a_known_kind():
    """Additive: `validate` must accept it, or every page carrying a stamp would be
    reported as malformed and sent to a human."""
    assert KIND_WATERMARK in REGION_KINDS
    page = PageRead(width=1600, height=2400, meta=PageMeta(confidence="high"),
                    regions=[region("r0", "somesite.com", KIND_WATERMARK)])
    assert validate(page) == []


def test_every_furniture_kind_is_a_known_kind():
    assert FURNITURE_KINDS <= set(REGION_KINDS)
