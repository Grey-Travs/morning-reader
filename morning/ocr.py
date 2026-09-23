"""Reading one page image into regions.

The call that fills the contract in :mod:`morning.pageread`. It returns regions with
geometry, for a novel and a manga alike — a novel flattens them and a manga keeps
them, and that is the whole reason the contract was fixed before anything used it.

Two things here are unlike the translation call.

**The model needs a tool.** An image cannot be put in a prompt as text, so the agent
is given a path and the ``Read`` tool, scoped by ``cwd``/``add_dirs`` to the one
folder that holds this project's pages. Everything else stays blocked.

**The answer is JSON, not prose.** Translation deliberately returns raw prose because
escaping thousands of Japanese characters invites truncation bugs — but regions are
structured by nature, and interleaving coordinates with text has no prose form. The
per-region strings are short, which is what makes the trade acceptable, and the parser
below is tolerant enough that a truncated answer yields the regions that did arrive
rather than nothing.

**Reading order is the hard part**, and it is why ``order`` is a proposal rather than
a fact. Japanese novels are typeset vertically, read right-to-left: the FIRST column
is the RIGHTMOST one. A model that defaults to left-to-right produces a page that is
backwards, sentence by sentence, in a way that is invisible unless you read Japanese.
The prompt says so explicitly, and the UI lets a human fix it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .pageread import (
    CONFIDENCE_LEVELS, GLUE_KINDS, GLUE_NONE, JOIN_KINDS, KIND_BODY, REGION_KINDS,
    PageMeta, PageRead, Region, from_pixels, validate,
)

# What the model is asked to produce. JSON, for the reason in the module docstring.
PAGE_SYSTEM_PROMPT = """\
You read one page of a Japanese book or comic and report what is written on it, and
where.

**Read the image at the path you are given.** Report only what is actually printed —
never invent, complete or correct text you cannot see.

**Regions.** Split the page into the blocks of text that are actually on it. A block is
a run of text that belongs together: one column or paragraph of prose, one speech
bubble, one caption, one sign. Do not split a block per line — a line ends because the
column ran out, not because the sentence did.

**Reading order is right-to-left.** Japanese books are typeset vertically and read from
the RIGHT side of the page to the left; a two-page spread reads right page first. Set
`order` to 0 for the first block a reader would read, 1 for the next, and so on. For a
page laid out horizontally (left-to-right, as some modern books are), use that order
instead — judge it from the text itself.

**Coordinates.** Give each region's box in PIXELS of the image you read, as
`[x, y, width, height]` from the top-left corner. Be approximate but honest: the box
should contain the text and not much else.

**Kinds.** Label each region:
- `body` — running prose
- `heading` — a chapter title or number printed on the page
- `caption` — text attached to an illustration
- `note` — a footnote or margin gloss
- `furigana` — a small reading printed beside or above a kanji
- `page-number`, `running-head` — page furniture
- `bubble`, `thought`, `narration`, `sfx`, `sign`, `aside` — comic text

**Seams.** For every region after the first, say how it follows the previous one in
reading order:
- `sentence` — the same sentence or paragraph continues across the break
- `paragraph` — a new paragraph starts
- `chapter` — a chapter begins here
- `gap` — something is missing between them
Use `glue: "none"` for Japanese (it has no spaces between words) and `glue: "space"`
only when the join is between words of a language that needs one.

**Output.** A single JSON object and nothing else — no preamble, no code fence, no
commentary:

{"width": <image width in px>, "height": <image height in px>,
 "regions": [{"box": [x, y, w, h], "text": "...", "kind": "body", "order": 0,
              "join_prev": "paragraph", "join_glue": "none"}],
 "meta": {"confidence": "high|medium|low",
          "heading": "<a chapter heading printed on this page, else null>",
          "starts_mid_sentence": true|false,
          "ends_mid_sentence": true|false,
          "ends_mid_word": true|false,
          "notes": ["anything that made this page hard to read"]}}

Set `confidence` to `low` whenever the page is blurred, skewed, partly cut off, or
anything was guessed. A page you were unsure about goes to a human; a page you claim
to be sure about does not."""

_USER_TEMPLATE = """\
Read the page image at this path and report it as JSON:

{path}
{hint}"""

# A fenced block, a preamble, a trailing apology — the parser strips all of it by
# taking the outermost braces rather than trusting the response to be bare JSON.
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")


class PageReadError(RuntimeError):
    """The page could not be read at all."""


def build_user_message(image_path: str | Path, hint: str = "") -> str:
    note = (f"\nThe person scanning this added a note: {hint.strip()}"
            if hint.strip() else "")
    return _USER_TEMPLATE.format(path=str(image_path), hint=note)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _as_notes(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _slice_json(text: str) -> dict | None:
    """The outermost JSON object in a response, or None.

    Taking the outermost braces rather than parsing the whole string is what makes a
    fenced block, a preamble or a trailing remark harmless. A response that is
    genuinely truncated fails here and is reported as such, rather than yielding half
    a page that looks complete.
    """
    cleaned = _FENCE_RE.sub("", text or "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _region_from(raw: dict, index: int, width: int, height: int) -> Region | None:
    """One region, with its box converted from pixels to fractions of the page.

    Returns None for an entry that carries no text: an empty region contributes
    nothing to the reading and would only make the order a non-permutation.
    """
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None

    box = raw.get("box")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            x, y, w, h = (float(v) for v in box)
        except (TypeError, ValueError):
            x = y = w = h = 0.0
    else:
        x = y = w = h = 0.0

    kind = str(raw.get("kind") or KIND_BODY)
    join = str(raw.get("join_prev") or "")
    glue = str(raw.get("join_glue") or GLUE_NONE)
    try:
        order = int(raw.get("order", index))
    except (TypeError, ValueError):
        order = index

    return Region(
        id=str(raw.get("id") or f"r{index}"),
        # The only sanctioned way in. A model answers in pixels of the image it saw,
        # and storing those makes every position wrong the moment the page is
        # re-scanned at another resolution.
        box=from_pixels(x, y, w, h, width, height),
        text=text,
        kind=kind if kind in REGION_KINDS else KIND_BODY,
        order=order,
        join_prev=join if join in JOIN_KINDS else "",
        join_glue=glue if glue in GLUE_KINDS else GLUE_NONE,
    )


def _renumber(regions: list[Region]) -> list[Region]:
    """Make ``order`` a permutation of 0..n-1 while keeping the model's sequence.

    The contract requires a permutation, and a model reliably gets the SEQUENCE right
    while skipping or repeating a number. Renumbering preserves its judgement — which
    is the part that is hard — and fixes only the bookkeeping, which is the part that
    is not. Ties keep list position, so the result is stable.
    """
    ordered = sorted(enumerate(regions), key=lambda pair: (pair[1].order, pair[0]))
    for position, (_, region) in enumerate(ordered):
        region.order = position
    return regions


def parse_page_response(raw: str, *, width: int = 0, height: int = 0) -> PageRead:
    """Turn the model's answer into a PageRead.

    ``width``/``height`` are what the SERVER measured from the file's bytes. They win
    over whatever the model reports: the model is estimating from what it was shown,
    and a wrong page size rescales every box on the page.

    Raises :class:`PageReadError` only when there is no usable JSON at all. Anything
    short of that degrades — a page transcribed at real cost must not be thrown away
    because one field came back the wrong type.
    """
    data = _slice_json(raw)
    if data is None:
        raise PageReadError("the page reader did not return usable JSON")

    def _int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    # Prefer the measured size; fall back to the model's only when there is none.
    page_width = width or _int(data.get("width"))
    page_height = height or _int(data.get("height"))

    raw_regions = data.get("regions")
    regions = []
    if isinstance(raw_regions, list):
        for index, entry in enumerate(raw_regions):
            region = _region_from(entry, index, page_width, page_height)
            if region is not None:
                regions.append(region)
    _renumber(regions)

    meta_raw = data.get("meta")
    meta_raw = meta_raw if isinstance(meta_raw, dict) else {}
    confidence = str(meta_raw.get("confidence") or "low").strip().lower()
    heading = meta_raw.get("heading")

    return PageRead(
        width=page_width,
        height=page_height,
        regions=regions,
        meta=PageMeta(
            confidence=confidence if confidence in CONFIDENCE_LEVELS else "low",
            heading=str(heading).strip() if heading else None,
            starts_mid_sentence=_as_bool(meta_raw.get("starts_mid_sentence")),
            ends_mid_sentence=_as_bool(meta_raw.get("ends_mid_sentence")),
            ends_mid_word=_as_bool(meta_raw.get("ends_mid_word")),
            notes=_as_notes(meta_raw.get("notes")),
        ),
    )


def read_page(translator, image_path: str | Path, *, width: int = 0, height: int = 0,
              hint: str = "", hooks=None) -> PageRead:
    """Read one page image. Blocking — always called via ``run_in_threadpool``.

    The ``Read`` tool is scoped to the image's own folder, so a call that can open a
    file can only open the pages of the project it was given.

    A read whose result does not satisfy the contract is returned anyway, with its
    problems recorded in ``meta.notes`` and its confidence forced down. It still holds
    the text, which is the expensive part; what it has lost is the right to be trusted
    without a human looking.
    """
    path = Path(image_path)
    folder = path.parent

    raw, usage, cost = translator._call(
        PAGE_SYSTEM_PROMPT,
        build_user_message(path.name, hint),
        max_turns=4,          # read the file, then answer
        hooks=hooks,
        tools=["Read"],
        cwd=folder,
        add_dirs=[folder],
    )

    page = parse_page_response(raw, width=width, height=height)
    page.usage = usage
    page.cost_usd = cost

    problems = validate(page)
    if problems:
        page.meta.notes = list(page.meta.notes) + problems
        page.meta.confidence = "low"
    return page
