"""What step 4 adds to the region contract, and why each addition is shaped that way.

All of it is additive: no stored region field is added, nothing existing changes, and a
manifest written by step 3 loads unchanged. That constraint is not tidiness — a new
stored field on ``Region`` would be absent on every page already transcribed, so the
feature that needed it would be empty across the whole library until the owner paid to
read every page a second time.

Two additions carry most of the weight:

* ``region_hash`` is the per-line analogue of ``source_hash``. It decides whether
  English the owner paid for is still true.
* ``apply_text_order`` is how a human's corrected reading order survives a re-read,
  given that region ids are NOT an identity — ``ocr._region_from`` assigns them from
  the model's list position.
"""

from __future__ import annotations

import pytest

from morning.pageread import (
    KIND_ASIDE, KIND_BODY, KIND_BUBBLE, KIND_NARRATION, KIND_PAGE_NUMBER,
    KIND_RUNNING_HEAD, KIND_SFX, KIND_SIGN, KIND_THOUGHT, PROSE_KINDS, SCRIPT_KINDS,
    TRANSLATED_KINDS, PageMeta, PageRead, Region, apply_text_order, order_texts,
    region_hash,
)
from morning.reading_order import propose_panels


def r(rid: str, text: str, *, box=(0.1, 0.1, 0.2, 0.2), kind: str = KIND_BUBBLE,
      order: int = 0) -> Region:
    return Region(id=rid, box=box, text=text, kind=kind, order=order)


def page(regions: list[Region]) -> PageRead:
    for i, region in enumerate(regions):
        if region.order == 0:
            region.order = i
    return PageRead(width=1600, height=2400, regions=regions,
                    meta=PageMeta(confidence="high"))


class TestWhatGetsTranslated:
    def test_a_sign_and_a_sound_effect_are_translated(self):
        """Both are on the page in Japanese and both need English. The contract's own
        docstring already said so; this names the set."""
        assert KIND_SIGN in TRANSLATED_KINDS
        assert KIND_SFX in TRANSLATED_KINDS

    def test_everything_the_script_view_lists_is_translated(self):
        assert SCRIPT_KINDS <= TRANSLATED_KINDS

    def test_but_they_are_not_the_same_set(self):
        """"What is translated" and "what is listed as dialogue" are different
        questions. A sign is scenery; it gets English and does not get a script line."""
        assert TRANSLATED_KINDS != SCRIPT_KINDS
        assert KIND_SIGN not in SCRIPT_KINDS

    @pytest.mark.parametrize("kind", [KIND_PAGE_NUMBER, KIND_RUNNING_HEAD, "watermark"])
    def test_page_furniture_is_never_translated(self, kind):
        """Paying to translate a page number is the clearest waste there is, and a
        translated running head or site stamp would then be drawn over the art on
        every page."""
        assert kind not in TRANSLATED_KINDS

    def test_every_kind_of_story_text_is_translated_on_a_manga_page(self):
        """Replaces an earlier test that asserted the manga set and PROSE_KINDS were
        DISJOINT — which pinned the bug. The page reader offers `caption` and `body`
        for any page and coerces unknown labels to `body`, so a manga narration box
        filed that way was left untranslated, undrawn, and absent from the line strip,
        and the reader said "Nothing is said on this page" over it.

        The only things a manga page does not translate are furigana (a reading
        printed beside a kanji, not a line of its own) and page furniture."""
        from morning.pageread import FURNITURE_KINDS, KIND_FURIGANA, REGION_KINDS

        assert TRANSLATED_KINDS == (
            frozenset(REGION_KINDS) - FURNITURE_KINDS - {KIND_FURIGANA})

    def test_overlapping_with_the_prose_set_is_harmless(self):
        """A novel never reads TRANSLATED_KINDS and a manga never flattens, so the
        overlap cannot leak one path into the other — the project's kind decides."""
        assert PROSE_KINDS <= TRANSLATED_KINDS

    @pytest.mark.parametrize("kind", [KIND_BUBBLE, KIND_THOUGHT, KIND_NARRATION,
                                      KIND_ASIDE])
    def test_every_spoken_kind_is_in_both(self, kind):
        assert kind in SCRIPT_KINDS and kind in TRANSLATED_KINDS


class TestTheStalenessKey:
    def test_the_same_words_hash_the_same(self):
        assert region_hash(r("r0", "もう無理だって")) == \
            region_hash(r("r9", "もう無理だって"))

    def test_different_words_hash_differently(self):
        assert region_hash(r("r0", "もう無理だって")) != region_hash(r("r0", "そう?"))

    def test_a_nudged_box_does_not_change_it(self):
        """Deliberate. A re-read moves every box a little, and re-billing a whole
        chapter's translation because the boxes drifted would make every re-read cost a
        full re-translation."""
        assert region_hash(r("r0", "そう", box=(0.1, 0.1, 0.2, 0.2))) == \
            region_hash(r("r0", "そう", box=(0.11, 0.09, 0.21, 0.19)))

    def test_a_relabelled_kind_does_not_change_it(self):
        """The words are the same; what changed is how the reader draws them."""
        assert region_hash(r("r0", "危険", kind=KIND_BUBBLE)) == \
            region_hash(r("r0", "危険", kind=KIND_SIGN))

    def test_surrounding_whitespace_is_not_a_difference(self):
        assert region_hash(r("r0", "  そう  ")) == region_hash(r("r0", "そう"))

    def test_an_empty_region_still_hashes(self):
        assert region_hash(r("r0", "")) == region_hash(r("r1", "   "))

    def test_the_hash_is_the_full_sha256(self):
        digest = region_hash(r("r0", "そう"))
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


class TestCarryingAHumanOrderAcrossAReRead:
    """Region ids are assigned from the model's list position, so a re-read can hand
    the same bubble a different id. Matching on the WORDS is what survives, because the
    words are what the human was looking at when they decided."""

    def test_the_order_comes_back_as_the_human_set_it(self):
        saved = ["third", "first", "second"]
        fresh = page([r("r0", "first"), r("r1", "second"), r("r2", "third")])

        applied = apply_text_order(fresh, saved)

        assert applied is not None
        assert order_texts(applied) == saved

    def test_it_is_marked_as_the_human_s_decision(self):
        fresh = page([r("r0", "a"), r("r1", "b")])
        applied = apply_text_order(fresh, ["b", "a"])
        assert applied.order_source == "user"

    def test_it_survives_every_id_changing(self):
        """The case this exists for. A second read of the same page renumbers
        everything; the human's order must still land on the right bubbles."""
        saved = ["second", "first"]
        # Same two bubbles, detected in the other order, so the ids have swapped.
        fresh = page([r("r0", "second"), r("r1", "first")])

        applied = apply_text_order(fresh, saved)

        assert order_texts(applied) == saved

    def test_it_survives_every_box_moving(self):
        saved = ["b", "a"]
        fresh = page([r("r0", "a", box=(0.9, 0.9, 0.05, 0.05)),
                      r("r1", "b", box=(0.0, 0.0, 0.05, 0.05))])
        assert order_texts(apply_text_order(fresh, saved)) == saved

    def test_duplicate_lines_resolve_by_position(self):
        """Two bubbles saying the same thing is ordinary — 「……」 twice on one page.
        Consuming each saved text against the first region that still has it keeps the
        result a permutation rather than dropping one."""
        fresh = page([r("r0", "……"), r("r1", "そう"), r("r2", "……")])
        applied = apply_text_order(fresh, ["……", "……", "そう"])
        assert applied is not None
        assert order_texts(applied) == ["……", "……", "そう"]

    def test_surrounding_whitespace_is_not_a_difference(self):
        fresh = page([r("r0", " a "), r("r1", "b")])
        assert apply_text_order(fresh, ["b", "a"]) is not None

    def test_a_page_that_now_says_something_else_refuses(self):
        """The important one. Forcing a human's ordering onto bubbles they never saw is
        worse than having no order, because it looks decided."""
        fresh = page([r("r0", "a"), r("r1", "COMPLETELY DIFFERENT")])
        assert apply_text_order(fresh, ["b", "a"]) is None

    def test_a_page_with_an_extra_region_refuses(self):
        fresh = page([r("r0", "a"), r("r1", "b"), r("r2", "c")])
        assert apply_text_order(fresh, ["b", "a"]) is None

    def test_a_page_with_a_missing_region_refuses(self):
        fresh = page([r("r0", "a")])
        assert apply_text_order(fresh, ["b", "a"]) is None

    def test_an_empty_saved_order_refuses_a_page_with_regions(self):
        fresh = page([r("r0", "a")])
        assert apply_text_order(fresh, []) is None

    def test_the_original_page_is_not_mutated(self):
        """`reorder` returns a new PageRead; a caller holding the model's order must
        still have it after a failed or successful carry."""
        fresh = page([r("r0", "a"), r("r1", "b")])
        before = order_texts(fresh)
        apply_text_order(fresh, ["b", "a"])
        assert order_texts(fresh) == before

    def test_what_gets_saved_is_what_comes_back(self):
        """`order_texts` and `apply_text_order` are a round trip — saving one and
        applying it to the same page is the identity."""
        original = page([r("r0", "one"), r("r1", "two"), r("r2", "three")])
        saved = order_texts(original)
        assert order_texts(apply_text_order(original, saved)) == saved


class TestPanels:
    """A panel is where the cut stops. Getting one wrong groups lines under the wrong
    heading in the script view — visible and harmless, which is the whole reason it can
    be derived rather than asked for."""

    def test_a_four_panel_grid_gives_four_panels_in_reading_order(self):
        regions = [
            r("tl", "a", box=(0.05, 0.05, 0.40, 0.40)),
            r("tr", "b", box=(0.55, 0.05, 0.40, 0.40)),
            r("bl", "c", box=(0.05, 0.55, 0.40, 0.40)),
            r("br", "d", box=(0.55, 0.55, 0.40, 0.40)),
        ]
        assert propose_panels(regions) == [["tr"], ["tl"], ["br"], ["bl"]]

    def test_two_bubbles_in_one_panel_stay_together_when_they_touch(self):
        regions = [
            r("a", "x", box=(0.10, 0.10, 0.30, 0.30)),
            r("b", "y", box=(0.15, 0.35, 0.30, 0.30)),   # overlapping: no cut
            r("far", "z", box=(0.60, 0.70, 0.30, 0.25)),
        ]
        panels = propose_panels(regions)
        assert ["a", "b"] in [sorted(p) for p in panels]
        assert ["far"] in panels

    def test_every_region_lands_in_exactly_one_panel(self):
        regions = [r(f"r{i}", str(i), box=(0.05 + i * 0.18, 0.05, 0.15, 0.30))
                   for i in range(5)]
        panels = propose_panels(regions)
        flat = [rid for panel in panels for rid in panel]
        assert sorted(flat) == sorted(rr.id for rr in regions)

    def test_the_panels_are_in_reading_order(self):
        """Flattening the panels must reproduce the reading order exactly, or the
        script view would list panels in one order and lines in another."""
        from morning.reading_order import propose_order

        regions = [
            r("tl", "a", box=(0.05, 0.05, 0.40, 0.40)),
            r("tr", "b", box=(0.55, 0.05, 0.40, 0.40)),
            r("bl", "c", box=(0.05, 0.55, 0.40, 0.40)),
            r("br", "d", box=(0.55, 0.55, 0.40, 0.40)),
        ]
        flat = [rid for panel in propose_panels(regions) for rid in panel]
        assert flat == propose_order(regions)

    def test_a_western_layout_panels_the_other_way(self):
        from morning.reading_order import DIRECTION_LTR

        regions = [
            r("tl", "a", box=(0.05, 0.05, 0.40, 0.40)),
            r("tr", "b", box=(0.55, 0.05, 0.40, 0.40)),
        ]
        assert propose_panels(regions, direction=DIRECTION_LTR) == [["tl"], ["tr"]]

    def test_a_page_with_no_geometry_is_one_panel(self):
        """Inventing panels from nothing would read as structure the page does not
        have. One panel holding everything is the only honest answer."""
        regions = [r("a", "x", box=(0, 0, 0, 0)), r("b", "y", box=(0, 0, 0, 0))]
        assert propose_panels(regions) == [["a", "b"]]

    def test_an_empty_page_has_no_panels(self):
        assert propose_panels([]) == []

    def test_a_region_spanning_the_page_collapses_it_to_one_panel(self):
        """A documented limit: a sound effect drawn across the whole page blocks every
        cut. Pinned so the behaviour is a decision rather than a surprise."""
        regions = [
            r("sfx", "ドーン", box=(0.0, 0.0, 1.0, 1.0), kind=KIND_SFX),
            r("a", "x", box=(0.10, 0.10, 0.20, 0.20)),
            r("b", "y", box=(0.60, 0.60, 0.20, 0.20)),
        ]
        assert len(propose_panels(regions)) == 1


class TestNothingWasTakenAway:
    """Step 4 is additive. A page written by step 3 must still load and behave the
    same, because a stored field added now would be absent on every page already paid
    for."""

    def test_a_region_has_no_speaker_or_panel_field(self):
        """Both were considered and both were rejected. `panel` is derived from the
        boxes; `speaker` is a judgement about a region, not something printed on it,
        and lives on the translated line."""
        stored = r("r0", "そう").to_dict()
        assert "speaker" not in stored
        assert "panel" not in stored

    def test_the_stored_shape_is_unchanged(self):
        assert set(r("r0", "そう").to_dict()) == {
            "id", "box", "text", "kind", "order", "join_prev", "join_glue"}
