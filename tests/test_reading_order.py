"""Reading order recovered from the boxes alone.

These are layouts drawn by hand, because that is the only way to test this: the thing
being checked is whether a rectangle arrangement produces the order a human would read
it in, and there is no way to assert that except to draw the arrangement and say what
the answer is.

The failure this guards against is the worst kind in the whole app. A page ordered
left-to-right instead of right-to-left is backwards sentence by sentence, every
sentence is still fluent English, and nothing downstream can tell. The owner does not
read Japanese; that is why they are using this.

Boxes are ``(x, y, w, h)`` as fractions of the page, so the layouts below read as a
picture: ``(0.55, 0.05, 0.4, 0.4)`` is the top-right quadrant.
"""

from __future__ import annotations

import pytest

from morning.pageread import KIND_BUBBLE, PageMeta, PageRead, Region
from morning.reading_order import (
    DEFAULT_SLACK, DIRECTION_LTR, DIRECTION_RTL, disagreement, has_geometry,
    looks_reversed, propose_order,
)


def r(rid: str, x: float, y: float, w: float, h: float, order: int = 0) -> Region:
    return Region(id=rid, box=(x, y, w, h), text=f"[{rid}]", kind=KIND_BUBBLE,
                  order=order)


def page(regions: list[Region], *, order: list[str] | None = None) -> PageRead:
    """A page whose reading order is whatever ``order`` says, defaulting to list order."""
    if order is not None:
        position = {rid: i for i, rid in enumerate(order)}
        for region in regions:
            region.order = position[region.id]
    else:
        for i, region in enumerate(regions):
            region.order = i
    return PageRead(width=1600, height=2400, regions=regions,
                    meta=PageMeta(confidence="high"))


# A four-panel grid — the layout this algorithm exists for.
#
#     +---------+---------+
#     |   TL    |   TR    |
#     +---------+---------+
#     |   BL    |   BR    |
#     +---------+---------+
GRID = lambda: [  # noqa: E731 — a fixture-shaped literal reads better than a fixture
    r("tl", 0.05, 0.05, 0.40, 0.40),
    r("tr", 0.55, 0.05, 0.40, 0.40),
    r("bl", 0.05, 0.55, 0.40, 0.40),
    r("br", 0.55, 0.55, 0.40, 0.40),
]


class TestTheGrid:
    def test_a_japanese_page_reads_top_right_first(self):
        assert propose_order(GRID()) == ["tr", "tl", "br", "bl"]

    def test_a_western_page_reads_top_left_first(self):
        assert propose_order(GRID(), direction=DIRECTION_LTR) == \
            ["tl", "tr", "bl", "br"]

    def test_the_two_directions_differ_only_in_the_columns(self):
        """Tiers are top-to-bottom in both. Only the left-right order flips, which is
        the single adaptation that makes this a Japanese layout."""
        rtl = propose_order(GRID(), direction=DIRECTION_RTL)
        ltr = propose_order(GRID(), direction=DIRECTION_LTR)
        assert rtl[:2] == list(reversed(ltr[:2]))
        assert rtl[2:] == list(reversed(ltr[2:]))

    def test_an_unknown_direction_reads_as_japanese(self):
        """This app is for Japanese. A typo in a direction must not silently produce a
        backwards page, which is the one failure nobody can see."""
        assert propose_order(GRID(), direction="sideways") == \
            propose_order(GRID(), direction=DIRECTION_RTL)


class TestTiersBeforeColumns:
    def test_a_banner_over_a_row_reads_banner_first(self):
        """
            +-------------------+
            |      BANNER       |
            +---------+---------+
            |    L    |    R    |
            +---------+---------+

        Cutting vertically at the top level would interleave the banner with the row —
        the banner spans both columns, so it would be forced into one of them.
        """
        regions = [
            r("banner", 0.05, 0.05, 0.90, 0.30),
            r("left", 0.05, 0.45, 0.40, 0.50),
            r("right", 0.55, 0.45, 0.40, 0.50),
        ]
        assert propose_order(regions) == ["banner", "right", "left"]

    def test_a_tall_panel_on_the_left_is_read_after_the_stack_beside_it(self):
        """
            +---------+---------+
            |         |  UPPER  |
            |  TALL   +---------+
            |         |  LOWER  |
            +---------+---------+

        No horizontal cut exists — the tall panel spans both tiers — so it falls to a
        vertical cut, which is right-first, and only then recurses into the stack.
        """
        regions = [
            r("tall", 0.05, 0.05, 0.40, 0.90),
            r("upper", 0.55, 0.05, 0.40, 0.40),
            r("lower", 0.55, 0.55, 0.40, 0.40),
        ]
        # `tall` is on the LEFT, so a right-to-left read reaches the stack first.
        assert propose_order(regions) == ["upper", "lower", "tall"]

    def test_the_mirror_image_reads_the_tall_one_first(self):
        """The same layout flipped: now the tall panel is the rightmost thing on the
        page, so it is read before the stack. The pair of tests is the point — one
        assertion alone would pass just as well if the direction were ignored."""
        regions = [
            r("tall", 0.55, 0.05, 0.40, 0.90),
            r("upper", 0.05, 0.05, 0.40, 0.40),
            r("lower", 0.05, 0.55, 0.40, 0.40),
        ]
        assert propose_order(regions) == ["tall", "upper", "lower"]


class TestAJapaneseNovelPage:
    def test_vertical_columns_read_right_to_left(self):
        """A novel page is set vertically: the first column is the RIGHTMOST one, and
        getting this backwards reverses the whole page."""
        regions = [
            r("c1", 0.05, 0.05, 0.12, 0.90),
            r("c2", 0.25, 0.05, 0.12, 0.90),
            r("c3", 0.45, 0.05, 0.12, 0.90),
            r("c4", 0.65, 0.05, 0.12, 0.90),
            r("c5", 0.85, 0.05, 0.12, 0.90),
        ]
        assert propose_order(regions) == ["c5", "c4", "c3", "c2", "c1"]

    def test_a_heading_above_the_columns_comes_first(self):
        regions = [
            r("head", 0.10, 0.03, 0.80, 0.08),
            r("c1", 0.10, 0.20, 0.15, 0.70),
            r("c2", 0.50, 0.20, 0.15, 0.70),
            r("c3", 0.75, 0.20, 0.15, 0.70),
        ]
        assert propose_order(regions) == ["head", "c3", "c2", "c1"]


class TestTheSlack:
    """The slack is the difference between a four-panel page reading correctly and
    reading as two columns — which is the classic wrong answer and looks plausible."""

    @staticmethod
    def _grid_with_overhang() -> list[Region]:
        # The bottom-left panel reaches up 0.005 into the top tier: a bubble drawn
        # slightly over a panel border, or a model rounding its box.
        regions = GRID()
        regions[2] = r("bl", 0.05, 0.445, 0.40, 0.50)
        return regions

    def test_a_small_overhang_does_not_break_the_tiers(self):
        assert propose_order(self._grid_with_overhang()) == ["tr", "tl", "br", "bl"]

    def test_without_the_slack_the_same_page_reads_as_two_columns(self):
        """Pins what the slack is FOR. Nobody would guess this failure from the code;
        it shows up as a page that reads down the right side and then down the left."""
        assert propose_order(self._grid_with_overhang(), slack=0.0) == \
            ["tr", "br", "tl", "bl"]

    def test_a_large_overlap_is_not_slack(self):
        """Two panels that genuinely share space are one group, not two tiers."""
        regions = [
            r("a", 0.05, 0.05, 0.40, 0.60),
            r("b", 0.55, 0.40, 0.40, 0.55),
        ]
        # They overlap vertically by 0.25, far past the slack, so there is no tier cut
        # and the vertical one decides: right first.
        assert propose_order(regions) == ["b", "a"]

    def test_a_negative_slack_is_treated_as_none(self):
        assert propose_order(GRID(), slack=-5.0) == propose_order(GRID(), slack=0.0)


class TestWhenThereIsNothingToGoOn:
    def test_an_empty_page_orders_nothing(self):
        assert propose_order([]) == []

    def test_one_region_is_its_own_order(self):
        assert propose_order([r("only", 0.1, 0.1, 0.5, 0.5)]) == ["only"]

    def test_a_page_with_no_geometry_keeps_the_order_it_has(self):
        """A page whose dimensions could not be read stores zero boxes — the text is
        kept, the geometry is not. An order derived from nothing must not come back
        looking like one derived from something."""
        regions = [r("a", 0, 0, 0, 0), r("b", 0, 0, 0, 0), r("c", 0, 0, 0, 0)]
        assert propose_order(regions) == ["a", "b", "c"]
        assert not has_geometry(regions)

    def test_a_page_with_some_geometry_is_still_laid_out(self):
        regions = [r("blank", 0, 0, 0, 0), r("real", 0.5, 0.5, 0.4, 0.4)]
        assert has_geometry(regions)
        assert set(propose_order(regions)) == {"blank", "real"}

    def test_overlapping_boxes_fall_back_to_rightmost_then_topmost(self):
        """One cluttered panel with bubbles piled on each other. No cut exists, so the
        answer is what a reader does: start at the top right."""
        regions = [
            r("low_left", 0.10, 0.60, 0.50, 0.30),
            r("high_right", 0.40, 0.10, 0.50, 0.30),
            r("mid", 0.20, 0.35, 0.50, 0.30),
        ]
        assert propose_order(regions) == ["high_right", "mid", "low_left"]


class TestMalformedBoxes:
    def test_a_nan_box_does_not_crash_or_win(self):
        regions = [r("sane", 0.5, 0.1, 0.4, 0.3),
                   Region(id="nan", box=(float("nan"),) * 4)]
        order = propose_order(regions)
        assert sorted(order) == ["nan", "sane"]

    def test_a_negative_size_does_not_invert_the_rectangle(self):
        """x1 < x0 would silently reverse every comparison for that box, which is the
        kind of thing that produces one wrong page in a volume."""
        regions = [Region(id="bad", box=(0.5, 0.5, -0.4, -0.4)),
                   r("good", 0.1, 0.1, 0.2, 0.2)]
        assert sorted(propose_order(regions)) == ["bad", "good"]

    def test_a_box_that_is_not_numbers_is_treated_as_having_no_position(self):
        regions = [Region(id="junk", box=("a", "b", "c", "d")),
                   r("real", 0.1, 0.1, 0.2, 0.2)]
        assert sorted(propose_order(regions)) == ["junk", "real"]


class TestTheResultIsAlwaysUsable:
    """Whatever the layout, the answer has to be a permutation of the regions — that is
    what ``pageread.validate`` requires, and a non-permutation means two regions claim
    the same place in the reading."""

    LAYOUTS = {
        "grid": GRID(),
        "columns": [r(f"c{i}", 0.05 + i * 0.18, 0.05, 0.15, 0.90) for i in range(5)],
        "pile": [r(f"p{i}", 0.1, 0.1, 0.8, 0.8) for i in range(4)],
        "blank": [r(f"b{i}", 0, 0, 0, 0) for i in range(3)],
        "one": [r("only", 0.2, 0.2, 0.2, 0.2)],
        "touching": [r("a", 0.0, 0.0, 0.5, 0.5), r("b", 0.5, 0.5, 0.5, 0.5)],
    }

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    @pytest.mark.parametrize("direction", [DIRECTION_RTL, DIRECTION_LTR])
    def test_every_region_appears_exactly_once(self, name, direction):
        regions = self.LAYOUTS[name]
        order = propose_order(regions, direction=direction)
        assert sorted(order) == sorted(rr.id for rr in regions)

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_the_same_page_always_orders_the_same_way(self, name):
        """Ties break on list position, so the answer is stable rather than arbitrary.
        An order that changed between runs would make a re-read look like a correction."""
        regions = self.LAYOUTS[name]
        assert propose_order(regions) == propose_order(regions)


class TestTellingTheUser:
    def test_a_page_laid_out_backwards_is_detected(self):
        """The specific failure: a model that defaulted to left-to-right. The page's own
        geometry catches it, because the left-to-right layout matches what the model
        said and the right-to-left one does not."""
        backwards = page(GRID(), order=["tl", "tr", "bl", "br"])
        assert looks_reversed(backwards)

    def test_a_page_laid_out_correctly_is_not(self):
        correct = page(GRID(), order=["tr", "tl", "br", "bl"])
        assert not looks_reversed(correct)

    def test_a_single_column_is_never_called_backwards(self):
        """One column of prose reads the same either way, so there is nothing for the
        direction to be wrong about. Flagging it would train the user to ignore the
        warning on the pages where it matters."""
        regions = [r("a", 0.2, 0.05, 0.5, 0.2), r("b", 0.2, 0.35, 0.5, 0.2),
                   r("c", 0.2, 0.65, 0.5, 0.2)]
        assert not looks_reversed(page(list(regions), order=["a", "b", "c"]))
        assert not looks_reversed(page(list(regions), order=["c", "b", "a"]))

    def test_a_page_with_one_region_is_never_called_backwards(self):
        assert not looks_reversed(page([r("only", 0.1, 0.1, 0.5, 0.5)]))

    def test_a_page_with_no_geometry_is_never_called_backwards(self):
        blank = page([r("a", 0, 0, 0, 0), r("b", 0, 0, 0, 0)])
        assert not looks_reversed(blank)

    def test_a_scrambled_page_is_not_called_backwards_either(self):
        """`looks_reversed` claims something specific — that the page was read the other
        way round. An order that is merely wrong is a different problem with a different
        fix, and conflating them would send the user to the wrong button."""
        scrambled = page(GRID(), order=["br", "tl", "tr", "bl"])
        assert not looks_reversed(scrambled)
        assert disagreement(scrambled) > 0


class TestDisagreement:
    def test_an_agreeing_page_counts_zero(self):
        assert disagreement(page(GRID(), order=["tr", "tl", "br", "bl"])) == 0

    def test_a_fully_reversed_page_counts_every_region(self):
        assert disagreement(page(GRID(), order=["tl", "tr", "bl", "br"])) == 4

    def test_two_swapped_bubbles_count_two(self):
        """A count, not a flag: two bubbles in one panel swapped is not worth anyone's
        attention, and a whole page backwards is."""
        assert disagreement(page(GRID(), order=["tl", "tr", "br", "bl"])) == 2

    def test_the_default_slack_is_the_one_the_module_documents(self):
        assert DEFAULT_SLACK == 0.01
