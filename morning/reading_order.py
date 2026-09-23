"""Working out reading order from the boxes alone — no model call, no cost.

The model proposes an ``order`` when it reads a page, and that proposal is the single
most dangerous field in the contract. A page ordered left-to-right instead of
right-to-left is backwards sentence by sentence, and it is **completely invisible** to
someone who does not read Japanese: every line is fluent English, the page just makes
no sense, and the natural conclusion is that the story is odd rather than that the app
is wrong.

So there is a second opinion, and it is free. The boxes are already stored; laying them
out is arithmetic. This gives three things the model's proposal cannot:

* **A one-click fix.** When the model has gone left-to-right, the whole page can be
  re-sorted without paying to read it again.
* **A disagreement signal.** When this and the model differ, that is worth surfacing —
  not because this is right and the model is wrong, but because a page where two
  independent methods disagree is exactly the page a human should look at.
* **Something to test against.** A model's judgement cannot be unit-tested. A recursive
  cut over rectangles can be, exhaustively, against layouts drawn by hand.

**The algorithm is a recursive X-Y cut**, which is the standard way to recover a
reading order from a page of boxes, with one adaptation: when it cuts vertically, the
groups are ordered RIGHT first. That single flip is the whole difference between a
Japanese page and a Western one.

At each step:

1. Look for horizontal gaps — a y with no box straddling it. If there are any, the page
   splits into tiers; read them top to bottom and recurse into each.
2. Otherwise look for vertical gaps. The page splits into columns; read them right to
   left (or left to right for a horizontally-set work) and recurse into each.
3. Otherwise the boxes genuinely overlap and no cut exists. Fall back to
   rightmost-then-topmost, which is what a reader does inside one cluttered panel.

Trying tiers before columns is deliberate. Manga is drawn in tiers, and a page with
both structures — a wide banner panel over a row of small ones — reads banner-first.
Cutting vertically at the top level would interleave the banner with the row.

Two known limits, stated rather than hidden:

* **A region that spans the page blocks every cut.** A sound effect drawn diagonally
  across the whole page is one box covering everything, so no gap exists and the page
  falls to step 3. The result is sane but not clever, and a human can fix it.
* **Furigana is ordered by position like anything else.** A reading gloss sits beside
  its kanji and will usually land just before it. That is harmless: ``furigana`` is in
  neither ``PROSE_KINDS`` nor ``SCRIPT_KINDS``, so nothing that assembles text reads
  it in order anyway.

Nothing here decides whether to APPLY an order. That is a question about whose
judgement wins, which belongs with ``order_source`` — see :mod:`morning.pageread`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pageread import PageRead, Region

# Which way the columns of a page are read.
DIRECTION_RTL = "rtl"   # Japanese: the first column is the RIGHTMOST one
DIRECTION_LTR = "ltr"   # a horizontally-set work, as some modern books are
DIRECTIONS = (DIRECTION_RTL, DIRECTION_LTR)

# How much two boxes may overlap and still be treated as separated.
#
# Not zero, and the reason is worth stating: panel borders are drawn, bubbles are
# hand-placed, and a model estimating a box in pixels rounds. Requiring a strictly
# empty gap means one bubble overhanging its tier by a few pixels suppresses the cut
# for the WHOLE page, and a four-panel grid then reads as two columns — top-right,
# bottom-right, top-left, bottom-left. That is the classic wrong answer and it looks
# plausible enough to ship.
#
# As a fraction of the page, so it is resolution-independent like everything else here.
DEFAULT_SLACK = 0.01


@dataclass(frozen=True)
class _Rect:
    """One region reduced to what the layout cares about."""

    id: str
    x0: float
    y0: float
    x1: float
    y1: float
    seq: int  # original list position — the stable tie-break, never a judgement


def _rects(regions: list[Region]) -> list[_Rect]:
    out: list[_Rect] = []
    for seq, region in enumerate(regions):
        try:
            x, y, w, h = (float(v) for v in region.box)
        except (TypeError, ValueError):
            x = y = w = h = 0.0
        if any(v != v for v in (x, y, w, h)):  # NaN compares false against everything
            x = y = w = h = 0.0
        # A negative size is meaningless but must not invert the rectangle, which would
        # put x1 before x0 and silently corrupt every comparison below.
        w, h = max(0.0, w), max(0.0, h)
        out.append(_Rect(id=region.id, x0=x, y0=y, x1=x + w, y1=y + h, seq=seq))
    return out


def has_geometry(regions: list[Region]) -> bool:
    """Whether these boxes say anything about position at all.

    A page whose dimensions could not be read stores zero boxes — the text is kept,
    the geometry is not. Laying those out would return an order based on nothing and
    present it with the same confidence as a real one, so callers check this first.
    """
    return any(r.x1 > r.x0 and r.y1 > r.y0 for r in _rects(regions))


def _groups(rects: list[_Rect], *, axis: str, slack: float) -> list[list[_Rect]] | None:
    """Split on gaps along one axis, or None when there is no gap to split on.

    Returns the groups in increasing coordinate order — top-to-bottom for ``y``,
    left-to-right for ``x``. Reversing for right-to-left is the caller's decision,
    because it is the one thing here that is about language rather than geometry.
    """
    lo = (lambda r: r.y0) if axis == "y" else (lambda r: r.x0)
    hi = (lambda r: r.y1) if axis == "y" else (lambda r: r.x1)

    ordered = sorted(rects, key=lambda r: (lo(r), hi(r), r.seq))
    groups: list[list[_Rect]] = []
    current: list[_Rect] = []
    reach = float("-inf")

    for rect in ordered:
        # A gap exists when this box starts after everything before it has ended —
        # allowing the slack above, so a few pixels of overhang does not suppress the
        # cut for the whole page. `reach` is the running maximum, not the previous
        # box's end: a tall box early in a tier still covers the ones after it.
        if not current:
            current, reach = [rect], hi(rect)
            continue
        if lo(rect) >= reach - slack:
            groups.append(current)
            current, reach = [rect], hi(rect)
            continue
        current.append(rect)
        reach = max(reach, hi(rect))

    if current:
        groups.append(current)
    return groups if len(groups) > 1 else None


def _walk(rects: list[_Rect], *, direction: str, slack: float) -> list[_Rect]:
    """Recursive X-Y cut. Tiers before columns — see the module docstring."""
    if len(rects) <= 1:
        return list(rects)

    tiers = _groups(rects, axis="y", slack=slack)
    if tiers is not None:
        out: list[_Rect] = []
        for tier in tiers:                    # top to bottom
            out.extend(_walk(tier, direction=direction, slack=slack))
        return out

    columns = _groups(rects, axis="x", slack=slack)
    if columns is not None:
        # The one flip that makes this a Japanese page rather than a Western one.
        if direction == DIRECTION_RTL:
            columns = list(reversed(columns))
        out = []
        for column in columns:
            out.extend(_walk(column, direction=direction, slack=slack))
        return out

    # Genuinely overlapping, so no cut exists. Rightmost first, then topmost — what a
    # reader does inside one cluttered panel. `seq` last, so the result is stable
    # rather than arbitrary.
    if direction == DIRECTION_RTL:
        return sorted(rects, key=lambda r: (-r.x1, r.y0, r.seq))
    return sorted(rects, key=lambda r: (r.x0, r.y0, r.seq))


def propose_order(regions: list[Region], *, direction: str = DIRECTION_RTL,
                  slack: float = DEFAULT_SLACK) -> list[str]:
    """Region ids in the order a reader would read them.

    Pure: it reads boxes and returns ids, and decides nothing about whether to apply
    them. A page with no usable geometry keeps the order it already has — a layout
    derived from nothing must not be handed back as though it were derived from
    something.
    """
    if direction not in DIRECTIONS:
        direction = DIRECTION_RTL
    rects = _rects(regions)
    if not rects:
        return []
    if not any(r.x1 > r.x0 and r.y1 > r.y0 for r in rects):
        return [r.id for r in sorted(rects, key=lambda r: r.seq)]
    return [r.id for r in _walk(rects, direction=direction, slack=max(0.0, slack))]


def current_order(page: PageRead) -> list[str]:
    """The ids in the order the page is currently read in."""
    return [r.id for r in page.in_order()]


def disagreement(page: PageRead, *, direction: str = DIRECTION_RTL,
                 slack: float = DEFAULT_SLACK) -> int:
    """How many regions the geometry would place somewhere else.

    A count rather than a boolean, because the interesting distinction is between "two
    bubbles in one panel swapped", which nobody needs to look at, and "the whole page
    is backwards", which somebody does.
    """
    proposed = propose_order(page.regions, direction=direction, slack=slack)
    current = current_order(page)
    if len(proposed) != len(current):
        return len(current)
    return sum(1 for a, b in zip(proposed, current) if a != b)


def looks_reversed(page: PageRead, *, slack: float = DEFAULT_SLACK) -> bool:
    """Whether this page reads as though it were laid out the other way round.

    The specific failure this exists for: a model that defaults to left-to-right on a
    Japanese page. That page is backwards sentence by sentence and every sentence is
    still fluent, so nothing downstream can notice — but the page's own geometry can,
    because the left-to-right layout of the same boxes matches what the model said and
    the right-to-left one does not.

    Requires the page to have real geometry and more than one region; anything else
    cannot be judged and is reported as fine rather than as suspicious.
    """
    if len(page.regions) < 2 or not has_geometry(page.regions):
        return False
    current = current_order(page)
    rtl = propose_order(page.regions, direction=DIRECTION_RTL, slack=slack)
    ltr = propose_order(page.regions, direction=DIRECTION_LTR, slack=slack)
    if rtl == ltr:
        # The layout reads the same either way — a single column of prose, say. There
        # is nothing for the direction to be wrong about.
        return False
    return current == ltr and current != rtl
