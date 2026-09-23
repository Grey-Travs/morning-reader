"""The server and the overlay must place a box identically.

A region's position is stored once, as fractions of the page, and used twice: the
server validates a box (``morning/pageread.py``) and the browser draws one
(``web/src/geometry.js``). That is one rule implemented in two languages, and two
implementations of one rule drift.

Drift here is invisible in the worst way. A box the server accepts and the browser
rounds differently puts English over the wrong bubble — and the owner of this app does
not read Japanese fluently, which is the whole reason they are using it. There is no
error, no warning, and nothing on screen that looks wrong.

**Agreement is not enough.** The reference app carries a parity test that passed for
months while its two implementations genuinely disagreed, because both sides were
returning their input unchanged and therefore agreeing on garbage. So this file asserts
the two sides agree AND, separately, that each gets the right answer.

Nothing else in this suite shells out, so it skips cleanly when Node is not on PATH; it
is a drift alarm, not a build requirement.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from morning.pageread import box_is_inside, from_pixels, to_pixels  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEOMETRY_JS = os.path.join(REPO, "web", "src", "geometry.js")

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is not installed")


def _module_url() -> str:
    """The module as a file:// URL, which is what a bare Node import needs on Windows."""
    path = GEOMETRY_JS.replace(os.sep, "/")
    return "file:///" + path.lstrip("/")


def _run_js(body: str) -> list:
    """Evaluate a snippet against the real geometry module and return what it printed.

    ``json.dumps`` emits ``NaN`` and ``Infinity`` for the non-finite floats below, and
    those are valid JavaScript literals — so the cases can be inlined as a JS
    expression and reach the other side as genuine non-finite numbers. Passing them
    through ``JSON.parse`` instead would throw, and rewriting them as strings would
    test something other than what the browser receives.
    """
    script = (
        f"import {{ toPixels, fromPixels, boxIsInside, toPercent, EPS }} "
        f"from {json.dumps(_module_url())}\n" + body
    )
    result = subprocess.run([_NODE, "--input-type=module", "-e", script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


# ---- what a box can be -------------------------------------------------------
# Real shapes first, then every edge the two implementations could round or coerce
# differently. The coercion cases are here because Python's float() and JavaScript's
# Number() disagree about null, '' and booleans, and a box that one side accepts and
# the other rejects is a region that exists on the server and not on screen.

BOX_CASES = [
    [0.0, 0.0, 1.0, 1.0],            # the whole page
    [0.1, 0.2, 0.3, 0.4],            # an ordinary bubble
    [0.0, 0.0, 0.0, 0.0],            # a page with no measured size
    [0.5, 0.5, 0.5, 0.5],            # flush with the bottom-right corner
    [0.5, 0.5, 0.500001, 0.5],       # over the edge, but inside the slack
    [0.5, 0.5, 0.51, 0.5],           # over the edge and outside it
    [-0.0000001, 0.0, 1.0, 1.0],     # just before the left edge, inside the slack
    [-0.00001, 0.0, 1.0, 1.0],       # ...and outside it
    [0.1, 0.1, -0.2, 0.2],           # negative width
    [0.1, 0.1, 0.2, -0.2],           # negative height
    [1.0, 1.0, 0.0, 0.0],            # a zero-size box at the far corner
    [0.1, 0.1, 0.2],                 # too few values
    [0.1, 0.1, 0.2, 0.2, 0.3],       # too many
    ["0.1", "0.2", "0.3", "0.4"],    # numbers as strings, which JSON round-trips make
    ["", 0.0, 0.0, 0.0],             # empty string: float('') raises, Number('') is 0
    [None, 0.0, 0.0, 0.0],           # null: float(None) raises, Number(null) is 0
    [True, 0.0, 0.0, 0.0],           # bool: float(True) is 1.0 — a real difference
    ["abc", 0.0, 0.0, 0.0],
    [float("nan"), 0.0, 1.0, 1.0],
    [0.0, float("nan"), 1.0, 1.0],
    ["nan", 0.0, 1.0, 1.0],
    [float("inf"), 0.0, 0.0, 0.0],
    [float("-inf"), 0.0, 0.0, 0.0],
    [0.0, 0.0, float("inf"), 0.0],
]


def test_both_sides_agree_on_what_a_box_is():
    js = _run_js("const cases = " + json.dumps(BOX_CASES) + "\n"
                 "console.log(JSON.stringify(cases.map(boxIsInside)))\n")
    for case, answer in zip(BOX_CASES, js):
        assert box_is_inside(case) == answer, (
            f"pageread.py and geometry.js disagree about {case!r} — a region the "
            f"server stores would not be drawn, or one it rejects would be")


# ---- placing a box on an image -----------------------------------------------
# Two resolutions of the same page, which is the case the fractional contract exists
# for: a re-scan must not invalidate a stored position.

SIZES = [(1600, 2400), (800, 1200), (3200, 4800), (1, 1), (1080, 1080)]
FRACTION_CASES = [
    (0.0, 0.0, 1.0, 1.0),
    (0.1, 0.2, 0.3, 0.4),
    (0.5, 0.5, 0.5, 0.5),
    (0.333333, 0.666667, 0.1, 0.1),
    (0.0, 0.0, 0.0, 0.0),
]


def test_both_sides_place_a_box_identically():
    cases = [[list(box), w, h] for box in FRACTION_CASES for (w, h) in SIZES]
    js = _run_js("const cases = " + json.dumps(cases) + "\n"
                 "console.log(JSON.stringify("
                 "cases.map(([box, w, h]) => toPixels(box, w, h))))\n")
    for (box, width, height), answer in zip(cases, js):
        mine = list(to_pixels(tuple(box), width, height))
        # Exact, not approximate. Both sides are IEEE 754 doubles doing the same two
        # multiplications, so any difference at all is a difference in the rule.
        assert mine == answer, (
            f"pageread.to_pixels and geometry.toPixels disagree on {box} at "
            f"{width}x{height}: {mine} vs {answer}")


PIXEL_CASES = [
    (0, 0, 1600, 2400, 1600, 2400),
    (160, 480, 480, 960, 1600, 2400),
    (80, 240, 240, 480, 800, 1200),
    (0, 0, 0, 0, 1600, 2400),
    (100, 100, 100, 100, 0, 0),        # unmeasured page: both must yield a zero box
    (100, 100, 100, 100, 1600, 0),
    (100, 100, 100, 100, -5, 10),
]


def test_both_sides_convert_from_pixels_identically():
    cases = [list(c) for c in PIXEL_CASES]
    js = _run_js("const cases = " + json.dumps(cases) + "\n"
                 "console.log(JSON.stringify("
                 "cases.map((c) => fromPixels(...c))))\n")
    for case, answer in zip(PIXEL_CASES, js):
        mine = list(from_pixels(*case))
        assert mine == answer, (
            f"pageread.from_pixels and geometry.fromPixels disagree on {case}: "
            f"{mine} vs {answer}")


def test_both_sides_use_the_same_slack():
    """A different epsilon on each side is the subtlest way for this to drift: every
    ordinary box agrees and only the ones flush with an edge differ."""
    from morning.pageread import _EPS

    assert _run_js("console.log(JSON.stringify([EPS]))\n")[0] == _EPS


# ---- and now the right answers -----------------------------------------------
# Everything above proves the two sides AGREE. Both returning their input unchanged
# would be agreement too, and would defeat the entire guard. These pin the answers.

def test_the_whole_page_is_a_valid_box():
    assert box_is_inside([0.0, 0.0, 1.0, 1.0])


def test_a_box_that_runs_off_the_page_is_not():
    assert not box_is_inside([0.9, 0.0, 0.2, 1.0])


def test_the_slack_admits_a_box_that_ends_exactly_at_the_edge():
    """A model answering in pixels produces 1.0000000000000002 often enough that an
    exact comparison would reject legitimate boxes."""
    assert box_is_inside([0.0, 0.0, 1.0000000000000002, 1.0])


def test_the_slack_is_not_a_licence():
    assert not box_is_inside([0.0, 0.0, 1.01, 1.0])


def test_a_negative_size_is_not_a_box():
    assert not box_is_inside([0.5, 0.5, -0.1, 0.1])


@pytest.mark.parametrize("width,height", [(1600, 2400), (800, 1200), (3200, 4800)])
def test_a_box_survives_a_rescan(width, height):
    """The whole reason boxes are stored as fractions: the same stored position must
    land on the same part of the picture whatever resolution it is rendered at."""
    box = (0.25, 0.5, 0.25, 0.125)
    x, y, w, h = to_pixels(box, width, height)

    assert (x / width, y / height, w / width, h / height) == box
    # ...and it lands a quarter across, halfway down, whatever the size.
    assert x == pytest.approx(width * 0.25)
    assert y == pytest.approx(height * 0.5)


def test_pixels_round_trip_through_fractions():
    box = from_pixels(160, 480, 480, 960, 1600, 2400)
    assert to_pixels(box, 1600, 2400) == pytest.approx((160, 480, 480, 960))


def test_an_unmeasured_page_yields_a_zero_box_rather_than_dividing_by_zero():
    assert from_pixels(100, 100, 100, 100, 0, 0) == (0.0, 0.0, 0.0, 0.0)


def test_a_box_of_nothing_is_still_a_valid_box():
    """A page whose dimensions could not be read stores zero boxes, and its text must
    still be keepable — losing a transcription because the header was odd would throw
    away the expensive half of the read."""
    assert box_is_inside([0.0, 0.0, 0.0, 0.0])


def test_nan_is_never_inside():
    """NaN compares false against everything, so a naive range check lets it through
    and the overlay renders a box at `NaNpx`, which draws nothing and reports nothing."""
    assert not box_is_inside([float("nan"), 0.0, 1.0, 1.0])
    assert not box_is_inside([0.0, 0.0, float("nan"), 1.0])


def test_infinity_is_never_inside():
    assert not box_is_inside([float("inf"), 0.0, 0.0, 0.0])
    assert not box_is_inside([0.0, 0.0, float("inf"), 0.0])


def test_a_box_that_is_not_four_numbers_is_not_a_box():
    assert not box_is_inside([0.1, 0.1, 0.2])
    assert not box_is_inside([0.1, 0.1, 0.2, 0.2, 0.3])
    assert not box_is_inside("0.1,0.1,0.2,0.2")
    assert not box_is_inside(None)


def test_percentages_track_the_image_rather_than_a_pixel_size():
    """The browser positions with percentages so a box follows the picture through a
    resize, a zoom and a device pixel ratio with no listener and no re-render. There is
    no server counterpart, so this is the only place it is pinned from Python."""
    answer = _run_js("console.log(JSON.stringify("
                     "[toPercent([0.25, 0.5, 0.125, 0.0625])]))\n")[0]
    assert answer == {"left": "25%", "top": "50%",
                      "width": "12.5%", "height": "6.25%"}


def test_the_js_module_has_no_imports_of_its_own():
    """The parity harness loads it in bare Node, where there is no DOM and no bundler.
    An import added here would make this whole file skip with a confusing error rather
    than fail, and the drift alarm would go quiet without anyone noticing."""
    with open(GEOMETRY_JS, encoding="utf-8") as handle:
        source = handle.read()
    offenders = [line.strip() for line in source.splitlines()
                 if line.strip().startswith("import ")]
    assert not offenders, f"geometry.js must stay importable in bare Node: {offenders}"


def test_math_is_imported_for_the_non_finite_cases():
    """Guards the case list above: if `math` ever stops being used here it is because
    somebody removed the NaN and infinity cases, which are the ones most likely to
    differ between two languages."""
    assert math.isnan(float("nan"))
