"""Tests for the page-read contract — the decision the whole app hinges on.

The contract carries regions with geometry from day one, for both kinds. These tests
exist to make that expensive-to-defer decision cheap to keep, and they pin the four
properties the plan calls for by name:

* a box is a fraction, so a re-scan at another resolution leaves positions valid;
* reading order is a permutation of the regions;
* every box sits inside the page;
* **flattening a page yields exactly what a flat-text read would have returned**, so
  novels pay nothing for carrying regions.

Nothing here needs an image, a model, or a network. The contract is a contract.
"""

from __future__ import annotations

import morning.pageread as pr

# Invented Japanese prose, written for these tests. A novel page in vertical Japanese
# reads right-to-left, so the RIGHTMOST column is first — which is exactly the case a
# left-to-right assumption gets wrong, and why order is an explicit field.
COL_A = "電車はまだ来ない。"      # the train has not come yet.
COL_B = "彼女はホームに立っていた。"  # she stood on the platform.
COL_C = "「遅いね」と彼は言った。"        # "late, isn't it," he said.

PAGE_W, PAGE_H = 1600, 2400


def _novel_page(**meta) -> pr.PageRead:
    """A two-column novel page with a page number in the margin.

    Columns are laid out right-to-left: r0 is the rightmost. The page number is
    furniture — on the page, not part of the novel.
    """
    return pr.PageRead(
        width=PAGE_W, height=PAGE_H,
        meta=pr.PageMeta(confidence="high", **meta),
        regions=[
            pr.Region(id="r0", order=0, kind=pr.KIND_BODY, text=COL_A,
                      box=pr.from_pixels(1180, 200, 300, 1900, PAGE_W, PAGE_H)),
            pr.Region(id="r1", order=1, kind=pr.KIND_BODY, text=COL_B,
                      join_prev=pr.JOIN_SENTENCE, join_glue=pr.GLUE_NONE,
                      box=pr.from_pixels(820, 200, 300, 1900, PAGE_W, PAGE_H)),
            pr.Region(id="r2", order=2, kind=pr.KIND_BODY, text=COL_C,
                      join_prev=pr.JOIN_PARAGRAPH,
                      box=pr.from_pixels(460, 200, 300, 1900, PAGE_W, PAGE_H)),
            pr.Region(id="r3", order=3, kind=pr.KIND_PAGE_NUMBER, text="12",
                      box=pr.from_pixels(760, 2280, 80, 60, PAGE_W, PAGE_H)),
        ],
    )


# ---- the contract is well-formed ---------------------------------------------

def test_a_well_formed_page_validates():
    assert pr.validate(_novel_page()) == []
    assert pr.is_valid(_novel_page())


def test_a_page_with_no_dimensions_is_refused():
    """Without pixel dimensions nothing can be anchored to the page, which is the
    entire reason the overlay reader needs them."""
    page = _novel_page()
    page.width = 0

    assert any("dimensions" in p for p in pr.validate(page))


def test_a_box_outside_the_page_is_refused():
    page = _novel_page()
    page.regions[0].box = (0.9, 0.1, 0.5, 0.2)  # runs off the right edge

    assert any("not inside the page" in p for p in pr.validate(page))


def test_a_negative_box_is_refused():
    page = _novel_page()
    page.regions[0].box = (-0.2, 0.1, 0.3, 0.2)

    assert any("not inside the page" in p for p in pr.validate(page))


def test_a_box_that_ends_exactly_at_the_edge_is_accepted():
    """Float slack matters: a model answering in pixels produces 1.0000000000000002
    often enough that an exact comparison would reject legitimate full-width boxes."""
    page = _novel_page()
    page.regions[0].box = pr.from_pixels(0, 0, PAGE_W, PAGE_H, PAGE_W, PAGE_H)

    assert pr.validate(page) == []


def test_a_nan_box_is_refused():
    """NaN compares false against every bound, so a naive range check would let it
    through and the overlay would draw nothing, silently."""
    page = _novel_page()
    page.regions[0].box = (float("nan"), 0.1, 0.2, 0.2)

    assert any("not inside the page" in p for p in pr.validate(page))


def test_reading_order_must_be_a_permutation():
    """Two regions claiming the same place in the reading would render as a repeated
    or dropped line in the script view."""
    page = _novel_page()
    page.regions[1].order = 0  # now two zeroes and a hole

    assert any("permutation" in p for p in pr.validate(page))


def test_a_hole_in_the_reading_order_is_refused():
    page = _novel_page()
    page.regions[3].order = 9

    assert any("permutation" in p for p in pr.validate(page))


def test_unknown_kinds_joins_and_glues_are_refused():
    page = _novel_page()
    page.regions[0].kind = "speech-bubble"       # not the vocabulary
    page.regions[1].join_prev = "column"
    page.regions[2].join_glue = "hyphen"

    problems = " | ".join(pr.validate(page))
    assert "unknown kind" in problems
    assert "unknown join" in problems
    assert "unknown glue" in problems


def test_duplicate_region_ids_are_refused():
    """Ids are what a human's reordering is expressed in terms of. Two regions sharing
    one would make a reorder ambiguous."""
    page = _novel_page()
    page.regions[1].id = "r0"

    assert any("duplicate id" in p for p in pr.validate(page))


# ---- fractions, not pixels ---------------------------------------------------

def test_a_box_is_stored_as_fractions_of_the_page():
    page = _novel_page()
    x, y, w, h = page.regions[0].box

    assert 0 < x < 1 and 0 < y < 1 and 0 < w < 1 and 0 < h < 1
    assert abs(x - 1180 / PAGE_W) < 1e-12
    assert abs(h - 1900 / PAGE_H) < 1e-12


def test_a_rescan_at_another_resolution_leaves_positions_valid():
    """The plan's own requirement, stated as a test.

    The same stored box, placed on a page scanned at double the resolution, lands on
    the same part of the artwork — twice the pixels, same picture. Storing pixels
    would have made every position in the book wrong.
    """
    page = _novel_page()
    box = page.regions[0].box

    at_1x = pr.to_pixels(box, PAGE_W, PAGE_H)
    at_2x = pr.to_pixels(box, PAGE_W * 2, PAGE_H * 2)

    assert at_1x == (1180, 200, 300, 1900)
    assert at_2x == (2360, 400, 600, 3800)
    assert all(abs(b - a * 2) < 1e-9 for a, b in zip(at_1x, at_2x))
    # And the page is still valid at the new size — the boxes did not need rewriting.
    rescanned = pr.PageRead(width=PAGE_W * 2, height=PAGE_H * 2,
                            regions=page.regions, meta=page.meta)
    assert pr.validate(rescanned) == []


def test_a_page_with_no_dimensions_yields_a_zero_box_rather_than_raising():
    """A page whose size could not be determined must still be able to store its
    text — losing a transcription over a missing header would be the worse failure."""
    assert pr.from_pixels(10, 10, 5, 5, 0, 0) == (0.0, 0.0, 0.0, 0.0)


# ---- flattening: the novel path ----------------------------------------------

def test_flatten_reproduces_what_a_flat_text_read_would_have_returned():
    """The assertion the whole design rests on: regions cost the novel path nothing.

    Columns A and B are one sentence split across a column break, so they join with no
    separator at all — Japanese has no inter-word space. Column C is a new paragraph,
    so it gets a blank line. That is precisely the string a flat-text OCR would have
    produced from this page.
    """
    expected = COL_A + COL_B + "\n\n" + COL_C

    assert pr.flatten(_novel_page()) == expected


def test_flatten_leaves_out_page_furniture():
    """A page number is on the page without being part of the novel. Flat-text OCR
    never had to decide this because the prompt threw the layout away; keeping regions
    means deciding it, and deciding it wrong means page numbers translated as prose."""
    flat = pr.flatten(_novel_page())

    assert "12" not in flat
    assert COL_A in flat


def test_flatten_leaves_out_furigana():
    """Ruby is a pronunciation gloss beside a kanji, not a sentence. Splicing it in
    would corrupt every line it appears on — while still being worth capturing, because
    it is the best evidence in the book for how a name is read."""
    page = _novel_page()
    page.regions.append(pr.Region(
        id="r4", order=4, kind=pr.KIND_FURIGANA, text="となり",
        box=pr.from_pixels(1500, 300, 40, 120, PAGE_W, PAGE_H)))

    assert pr.validate(page) == []
    assert "となり" not in pr.flatten(page)
    # ...but it is still on the record, ready for the glossary.
    assert any(r.kind == pr.KIND_FURIGANA for r in page.regions)


def test_a_sentence_seam_with_space_glue_inserts_one():
    """The glue is part of the vocabulary because a page can carry English too —
    a quoted title, a transliterated name — and there the space is required."""
    page = _novel_page()
    page.regions[0].text = "the sentence runs"
    page.regions[1].text = "across the column break"
    page.regions[1].join_glue = pr.GLUE_SPACE

    assert pr.flatten(page).startswith("the sentence runs across the column break")


def test_an_unstated_seam_reads_as_a_paragraph():
    """A spurious break is visible and a human can delete it; a missing one welds two
    paragraphs together where nobody will notice. The safe default is the loud one."""
    page = _novel_page()
    page.regions[1].join_prev = ""

    assert pr.flatten(page) == COL_A + "\n\n" + COL_B + "\n\n" + COL_C


def test_flatten_follows_order_not_list_position():
    """Reading order is an explicit field precisely so detection order can differ from
    it — right-to-left, panel-aware order is not the order regions are found in."""
    page = _novel_page()
    page.regions[0].order, page.regions[2].order = 2, 0
    page.regions[0].join_prev, page.regions[2].join_prev = pr.JOIN_PARAGRAPH, ""
    page.regions[1].join_prev = pr.JOIN_PARAGRAPH

    assert pr.flatten(page) == COL_C + "\n\n" + COL_B + "\n\n" + COL_A


def test_duplicate_orders_still_flatten_deterministically():
    """A malformed page is still worth reading. Sorting falls back to list position,
    so the same bad input always produces the same output rather than an arbitrary one
    that changes between runs."""
    page = _novel_page()
    for r in page.regions:
        r.order = 0

    assert pr.flatten(page) == pr.flatten(page)


def test_empty_regions_are_skipped_without_leaving_blank_gaps():
    page = _novel_page()
    page.regions[1].text = "   "

    assert pr.flatten(page) == COL_A + "\n\n" + COL_C


def test_a_gap_seam_is_reported_as_a_warning():
    """A `gap` is the only place a page you never photographed can surface. It joins
    as a paragraph so the text still reads, and the build step gets told."""
    page = _novel_page()
    page.regions[2].join_prev = pr.JOIN_GAP

    assert pr.flatten(page) == COL_A + COL_B + "\n\n" + COL_C
    assert pr.gaps(page) == ["text may be missing just before region r2"]


def test_flatten_can_be_asked_for_the_manga_script_kinds_instead():
    """The same operation drives the script view — regions in reading order — so there
    is one implementation of "in order, as text" rather than two that drift."""
    page = pr.PageRead(
        width=PAGE_W, height=PAGE_H, meta=pr.PageMeta(confidence="high"),
        regions=[
            pr.Region(id="b0", order=0, kind=pr.KIND_BUBBLE, text=COL_C,
                      box=pr.from_pixels(1100, 150, 300, 300, PAGE_W, PAGE_H)),
            pr.Region(id="s0", order=1, kind=pr.KIND_SFX, text="ドン",
                      box=pr.from_pixels(600, 700, 400, 400, PAGE_W, PAGE_H)),
            pr.Region(id="b1", order=2, kind=pr.KIND_NARRATION, text=COL_A,
                      join_prev=pr.JOIN_PARAGRAPH,
                      box=pr.from_pixels(200, 1400, 500, 300, PAGE_W, PAGE_H)),
        ],
    )

    assert pr.validate(page) == []
    script = pr.flatten(page, kinds=pr.SCRIPT_KINDS)
    assert script == COL_C + "\n\n" + COL_A
    assert "ドン" not in script   # an sfx sits on the art, not in the script
    assert pr.flatten(page) == ""          # and none of it is novel prose


# ---- reordering is a human decision and it sticks ----------------------------

def test_a_human_reorder_is_applied_and_marked_as_theirs():
    page = _novel_page()
    assert page.order_source == "model"

    fixed = pr.reorder(page, ["r2", "r1", "r0", "r3"])

    assert fixed.order_source == "user"
    assert [r.id for r in fixed.in_order()] == ["r2", "r1", "r0", "r3"]


def test_reorder_does_not_mutate_the_original():
    """It returns a new page, so the caller persists it under a lock rather than
    having a half-applied reorder visible to a concurrent reader."""
    page = _novel_page()
    pr.reorder(page, ["r3", "r2", "r1", "r0"])

    assert [r.id for r in page.in_order()] == ["r0", "r1", "r2", "r3"]
    assert page.order_source == "model"


def test_a_partial_reorder_is_refused():
    """A short list would silently drop regions from the reading."""
    page = _novel_page()
    try:
        pr.reorder(page, ["r1", "r0"])
    except ValueError as exc:
        assert "exactly once" in str(exc)
    else:
        raise AssertionError("a partial reorder should have been refused")


def test_a_reorder_naming_an_unknown_region_is_refused():
    page = _novel_page()
    try:
        pr.reorder(page, ["r0", "r1", "r2", "r9"])
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown region id should have been refused")


# ---- serialization -----------------------------------------------------------

def test_a_page_round_trips_through_json():
    page = _novel_page(heading="第1話", ends_mid_sentence=True,
                       notes=["the gutter clipped one column"])
    back = pr.page_from_dict(page.to_dict())

    assert back.to_dict() == page.to_dict()
    assert pr.flatten(back) == pr.flatten(page)
    assert back.meta.heading == "第1話"
    assert back.meta.ends_mid_sentence is True
    assert back.meta.notes == ["the gutter clipped one column"]


def test_a_user_reorder_survives_the_round_trip():
    """The rule this exists for: once a human has fixed the reading order, nothing may
    quietly revert it — including a save/load cycle."""
    fixed = pr.reorder(_novel_page(), ["r3", "r2", "r1", "r0"])
    back = pr.page_from_dict(fixed.to_dict())

    assert back.order_source == "user"
    assert [r.id for r in back.in_order()] == ["r3", "r2", "r1", "r0"]


def test_a_malformed_record_degrades_rather_than_raising():
    """Same discipline as every other loader: a page transcribed at real cost must not
    become unreadable because one field came back the wrong type."""
    page = pr.page_from_dict({
        "width": "wide", "height": None,
        "regions": [{"box": "over there", "text": 5, "order": "first"},
                    "not a region at all"],
        "meta": "not a mapping",
        "order_source": "nonsense",
    })

    assert page.width == 0 and page.height == 0
    assert len(page.regions) == 2
    assert page.regions[0].box == (0.0, 0.0, 0.0, 0.0)
    assert page.regions[0].text == "5"
    assert page.regions[0].order == 0
    assert page.regions[1].id == "r1"          # an id was invented from its position
    assert page.order_source == "model"        # anything but "user" means "model"
    assert page.meta.confidence == "low"
    # It degraded — and it is honest about being invalid, rather than pretending.
    assert pr.validate(page) != []


def test_meta_booleans_survive_being_sent_as_strings():
    """Models answer "true" as often as true. Losing the seam flags to a type
    mismatch would break page stitching in a way that looks like bad transcription."""
    page = pr.page_from_dict({"width": 10, "height": 10, "regions": [],
                              "meta": {"confidence": "high",
                                       "ends_mid_sentence": "true",
                                       "starts_mid_sentence": "false",
                                       "notes": "one note"}})

    assert page.meta.ends_mid_sentence is True
    assert page.meta.starts_mid_sentence is False
    assert page.meta.notes == ["one note"]
