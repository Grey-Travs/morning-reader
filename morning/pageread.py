"""The page-read contract: what reading one page image returns, for BOTH kinds.

This is the decision the whole app hinges on, and the one that is expensive to defer,
so it is fixed here on day one and nothing is allowed to assume flat text.

    {
      "width": 1600, "height": 2400,          # pixels of the image this was read from
      "regions": [
        {"id": "r1", "box": [x, y, w, h],     # FRACTIONS of width/height, never pixels
         "text": "...", "kind": "body", "order": 0,
         "join_prev": "paragraph", "join_glue": "none"}
      ],
      "order_source": "model",                # or "user" once a human has reordered
      "meta": {"confidence": "high", "heading": null,
               "starts_mid_sentence": false, "ends_mid_sentence": true,
               "ends_mid_word": false, "notes": []}
    }

**Novels flatten it.** ``flatten()`` concatenates the prose regions in ``order`` and
hands back exactly the string a flat-text read would have produced, so the prose
machinery — page seams, chapter assembly, the whole translate/validate pipeline —
applies unchanged. Regions therefore cost the novel path nothing.

**Manga keeps it.** ``box`` drives the overlay, ``order`` drives the script view.

Why this shape, point by point:

* **Fractions, not pixels.** A page re-scanned at a different resolution must not
  invalidate every stored position. Fractions survive it; pixels do not.
  ``from_pixels`` is the only sanctioned way to build a box from a model that answered
  in pixels.
* **``order`` is an explicit field, not list position.** It is a *proposal* from the
  model — right-to-left, top-to-bottom, panel-aware order is genuinely hard — and a
  human must be able to correct it. Detection order stays as the list order so a
  correction is a reordering of stable ids rather than a rewrite.
* **``order_source``.** Once a human has fixed the reading order, a re-read must not
  silently revert it. Night Reader learned this at the page-seam level, where
  ``join_prev_source: "user"`` is never revisited; the same rule, one level down.
* **Seam fields per region.** A paragraph that runs across a column break is the same
  problem as one that runs across a page break, and Night Reader already has a proven,
  language-neutral vocabulary for it: ``sentence | paragraph | chapter | gap`` with a
  glue of ``space | none``. Reusing it here is what makes ``flatten()`` able to
  reproduce flat-text output exactly — a region list that threw the seam away could
  only guess, and guessing is how you get a false paragraph break mid-sentence.
* **Glue defaults to ``none``.** Japanese does not put spaces between words. The
  Korean app defaults to ``space`` for the same reason in reverse. Getting this
  backwards inserts a space into every column break in the book.
* **Not every region is prose.** A page number, a running head or a furigana gloss is
  on the page but is not part of the novel's text. Flat-text OCR never had to decide
  this because the prompt threw the layout away; keeping regions means deciding it,
  and deciding it wrong means page numbers translated as dialogue.

Not decided here, deliberately: how ``width``/``height`` are obtained. That belongs
with the page harness. The recommendation on the record: sniff them from the image
header bytes server-side (JPEG SOF, PNG IHDR, WebP VP8/VP8L/VP8X all carry dimensions
in the first few KB), which needs no Pillow and does not trust the client.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

# How a region (or a page) follows the one before it. Language-neutral: it describes
# structure, not script, which is why this vocabulary carries over from the Korean app
# untouched.
JOIN_SENTENCE = "sentence"    # same sentence/paragraph continues across the break
JOIN_PARAGRAPH = "paragraph"  # a new paragraph starts here
JOIN_CHAPTER = "chapter"      # a chapter boundary lands here
JOIN_GAP = "gap"              # content is missing between the two (an unphotographed page)
JOIN_KINDS = (JOIN_SENTENCE, JOIN_PARAGRAPH, JOIN_CHAPTER, JOIN_GAP)

GLUE_SPACE = "space"
GLUE_NONE = "none"
GLUE_KINDS = (GLUE_SPACE, GLUE_NONE)

# ---- region kinds ------------------------------------------------------------
# Novel pages.
KIND_BODY = "body"                  # running prose
KIND_HEADING = "heading"            # a chapter title or number printed on the page
KIND_CAPTION = "caption"            # text attached to an illustration
KIND_NOTE = "note"                  # footnote, translator note, margin gloss
KIND_FURIGANA = "furigana"          # ruby: a reading printed beside/above a kanji
KIND_PAGE_NUMBER = "page-number"
KIND_RUNNING_HEAD = "running-head"  # the title repeated in the margin of every page

# Manga pages.
KIND_BUBBLE = "bubble"              # speech
KIND_THOUGHT = "thought"            # thought balloon
KIND_NARRATION = "narration"        # caption box
KIND_SFX = "sfx"                    # onomatopoeia drawn into the art
KIND_SIGN = "sign"                  # text inside the scene: signs, letters, screens
KIND_ASIDE = "aside"                # small marginal text, often the author's

REGION_KINDS = (
    KIND_BODY, KIND_HEADING, KIND_CAPTION, KIND_NOTE, KIND_FURIGANA,
    KIND_PAGE_NUMBER, KIND_RUNNING_HEAD,
    KIND_BUBBLE, KIND_THOUGHT, KIND_NARRATION, KIND_SFX, KIND_SIGN, KIND_ASIDE,
)

# The kinds that ARE the novel's text. Everything else is on the page without being
# part of it.
#
# Two exclusions worth stating, because both look wrong at a glance:
#
#   * ``furigana`` is a pronunciation gloss printed beside a kanji, not a separate
#     sentence. Splicing it into the prose corrupts every line it appears on. It is
#     still captured, because it is the best evidence in the book for how a name is
#     actually read — which is precisely what the glossary needs when one character is
#     written in kanji in the narration and katakana in dialogue.
#   * ``heading`` is excluded because a chapter heading is handled by the chapter
#     seam (``JOIN_CHAPTER`` plus ``meta.heading``), exactly as the page-level
#     assembler already does it. Flattening it inline as well would print the title
#     twice.
PROSE_KINDS = frozenset({KIND_BODY, KIND_CAPTION, KIND_NOTE})

# The kinds that carry dialogue or narration on a manga page — what the script view
# lists and what the overlay draws. ``sign`` and ``sfx`` are translated too, but the
# reader treats them differently (a sign is scenery; an sfx sits on the art), so they
# are named separately rather than lumped in.
SCRIPT_KINDS = frozenset({KIND_BUBBLE, KIND_THOUGHT, KIND_NARRATION, KIND_ASIDE})

# The kinds a manga translation call is GIVEN. Wider than SCRIPT_KINDS because a sign
# and a sound effect are translated too — the docstring above already says so — while
# the script view lists only what is spoken. Two sets, because "what is translated" and
# "what is listed as dialogue" are different questions that happen to overlap.
#
# Page furniture is excluded on purpose: paying to translate a page number is the
# clearest possible waste, and a translated running head would then be drawn over the
# art on every page of the book.
TRANSLATED_KINDS = SCRIPT_KINDS | {KIND_SIGN, KIND_SFX}

CONFIDENCE_LEVELS = ("high", "medium", "low")

# Float slack for containment checks. A model that answers in pixels and a box that
# ends exactly at the right edge produce 1.0000000000000002 often enough that an exact
# comparison rejects legitimate boxes.
_EPS = 1e-6


@dataclass
class Region:
    """One block of text found on a page, with where it sits."""

    id: str
    box: tuple[float, float, float, float]  # (x, y, w, h), fractions of the page
    text: str = ""
    kind: str = KIND_BODY
    order: int = 0
    # How this region follows the previous one IN READING ORDER. The first region's
    # join is ignored. Empty means "not stated" and is read as `paragraph`, which is
    # the safe default: a spurious paragraph break is visible and fixable, a missing
    # one silently welds two paragraphs together.
    join_prev: str = ""
    join_glue: str = GLUE_NONE

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "box": [round(float(v), 6) for v in self.box],
            "text": self.text,
            "kind": self.kind,
            "order": self.order,
            "join_prev": self.join_prev,
            "join_glue": self.join_glue,
        }


@dataclass
class PageMeta:
    """Page-level judgements from the read — the same set the page seams already use."""

    confidence: str = "low"
    heading: str | None = None
    starts_mid_sentence: bool = False
    ends_mid_sentence: bool = False
    ends_mid_word: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "confidence": self.confidence,
            "heading": self.heading,
            "starts_mid_sentence": self.starts_mid_sentence,
            "ends_mid_sentence": self.ends_mid_sentence,
            "ends_mid_word": self.ends_mid_word,
            "notes": list(self.notes),
        }


@dataclass
class PageRead:
    """Everything one page image yielded. The unit both kinds are built from."""

    width: int = 0
    height: int = 0
    regions: list[Region] = field(default_factory=list)
    meta: PageMeta = field(default_factory=PageMeta)
    # "model" until a human reorders, then "user" — and never revisited after that.
    order_source: str = "model"
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "regions": [r.to_dict() for r in self.regions],
            "order_source": self.order_source,
            "meta": self.meta.to_dict(),
        }

    def in_order(self) -> list[Region]:
        """Regions in reading order.

        Sorted by ``order``, with the list position as the tie-break so a model that
        returned duplicate order values still yields a stable, total ordering rather
        than an arbitrary one that changes between runs.
        """
        return [r for _, r in sorted(enumerate(self.regions),
                                     key=lambda pair: (pair[1].order, pair[0]))]


# ---- building ----------------------------------------------------------------

def from_pixels(x: float, y: float, w: float, h: float, width: int, height: int
                ) -> tuple[float, float, float, float]:
    """Convert a pixel box to the stored fractional one.

    The only sanctioned way in. A model asked for coordinates answers in pixels of the
    image it was shown, and storing those means every position in the book is wrong the
    moment the page is re-scanned at another resolution.

    Zero dimensions yield a zero box rather than raising: a page whose dimensions
    could not be determined must still be able to store its text.
    """
    if width <= 0 or height <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (x / width, y / height, w / width, h / height)


def to_pixels(box: tuple[float, float, float, float], width: int, height: int
              ) -> tuple[float, float, float, float]:
    """The inverse: a stored box placed on an image of the given size.

    This is what the overlay reader draws with, and it is why the round-trip has to be
    exact — see ``tests/test_pageread.py``, which pins it at two resolutions.
    """
    x, y, w, h = box
    return (x * width, y * height, w * width, h * height)


# ---- validation --------------------------------------------------------------

def box_is_inside(box) -> bool:
    """Whether a box is a well-formed fraction box that fits on the page."""
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    if any(v != v for v in (x, y, w, h)):  # NaN compares false against everything
        return False
    if w < 0 or h < 0:
        return False
    return (x >= -_EPS and y >= -_EPS
            and x + w <= 1 + _EPS and y + h <= 1 + _EPS)


def validate(page: PageRead) -> list[str]:
    """Everything wrong with this page read, in plain English. Empty means valid.

    Returns problems rather than raising, because a page that reads badly is still
    worth keeping and showing to a human — the same discipline as the tolerant parsers
    elsewhere. The caller decides whether to store it, flag it, or refuse it.
    """
    problems: list[str] = []

    if page.width <= 0 or page.height <= 0:
        problems.append("the page has no recorded pixel dimensions, so nothing can be "
                        "anchored to it")

    seen_ids: set[str] = set()
    for i, r in enumerate(page.regions):
        where = f"region {r.id or i}"
        if not r.id:
            problems.append(f"{where}: has no id")
        elif r.id in seen_ids:
            problems.append(f"{where}: duplicate id")
        seen_ids.add(r.id)
        if not box_is_inside(r.box):
            problems.append(f"{where}: box {list(r.box)} is not inside the page")
        if r.kind not in REGION_KINDS:
            problems.append(f"{where}: unknown kind {r.kind!r}")
        if r.join_prev and r.join_prev not in JOIN_KINDS:
            problems.append(f"{where}: unknown join {r.join_prev!r}")
        if r.join_glue not in GLUE_KINDS:
            problems.append(f"{where}: unknown glue {r.join_glue!r}")

    # Reading order must be a PERMUTATION of the regions: 0..n-1 exactly once. A
    # duplicate or a hole means two regions claim the same place in the reading, which
    # the script view would render as a repeated or dropped line.
    orders = sorted(r.order for r in page.regions)
    if orders != list(range(len(page.regions))):
        problems.append(f"reading order is not a permutation of the regions: {orders}")

    if page.meta.confidence not in CONFIDENCE_LEVELS:
        problems.append(f"unknown confidence {page.meta.confidence!r}")
    if page.order_source not in ("model", "user"):
        problems.append(f"unknown order source {page.order_source!r}")

    return problems


def is_valid(page: PageRead) -> bool:
    return not validate(page)


def reorder(page: PageRead, ids: list[str]) -> PageRead:
    """Apply a human's reading order and mark it as theirs.

    ``ids`` must be a permutation of the page's region ids — a partial list would
    silently drop regions from the reading, so it is refused. Returns a NEW PageRead;
    the caller persists it under a lock.

    ``order_source`` becomes "user" and, per the rule at the top of this module,
    nothing may set it back to "model".
    """
    current = [r.id for r in page.regions]
    if sorted(str(i) for i in ids) != sorted(current):
        raise ValueError("the new order must list every region exactly once")
    position = {str(rid): i for i, rid in enumerate(ids)}
    return replace(
        page,
        regions=[replace(r, order=position[r.id]) for r in page.regions],
        order_source="user",
    )


def region_hash(region: Region) -> str:
    """This region's text, as the key that decides whether a translation is still true.

    The per-region analogue of ``ChapterMetrics.content_hash``, and it is used the same
    way: the presence of a translation says the work finished, this hash says it
    finished against the words that are there *now*. A re-read that changes a bubble's
    Japanese makes its English stale and visible instead of silently wrong.

    **Text and nothing else.** Not the box, not the kind. A box nudged by a re-read is
    the same words in almost the same place, and re-billing a line for that would make
    every re-read cost a full re-translation. A region relabelled ``bubble`` -> ``sign``
    is also the same words; what changed is how the reader draws it.

    Stripped first, so trailing whitespace from one read and not another is not a
    difference — the same normalisation ``flatten`` applies.
    """
    return hashlib.sha256((region.text or "").strip().encode("utf-8")).hexdigest()


def is_drawable(box) -> bool:
    """Whether this box can actually have something drawn on it.

    Distinct from :func:`box_is_inside`, which asks whether a box is WELL FORMED. A box
    can be perfectly well formed and still be undrawable: a zero-size box is valid (an
    unmeasured page stores nothing but those) and draws nothing at all. An overlay that
    silently renders nothing for such a region shows the reader a bubble with no
    English and no explanation, which reads as "the app missed this one".

    So this is the question the overlay actually asks, and the answer has to be the
    same on both sides — the server counts undrawable regions for its banner and the
    browser decides what to skip. If the two disagree the banner lies. Mirrored by
    ``isDrawable`` in ``web/src/geometry.js`` and pinned by
    ``tests/test_geometry_parity.py``.

    A box is drawable when every value is finite, it has positive area, and it overlaps
    the page. Overhanging the edge is allowed — the wrapper clips it — but a box
    entirely off the page is not.
    """
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x, y, w, h)):
        return False
    if w <= 0 or h <= 0:
        return False
    # The visible part: the box intersected with the page.
    left, top = max(0.0, x), max(0.0, y)
    right, bottom = min(1.0, x + w), min(1.0, y + h)
    return right - left > 0 and bottom - top > 0


def apply_text_order(page: PageRead, texts: list[str]) -> PageRead | None:
    """Re-apply a human's saved reading order to a page that has been read again.

    The problem this solves: **region ids are not an identity.** ``ocr._region_from``
    assigns ``r0``, ``r1``… from the model's list position and the prompt never asks for
    an id, so a second read of the same page can hand the same bubble a different id.
    Re-applying a saved order by id would take a human's correct ordering of the OLD
    boxes and scramble a possibly-correct new one with it.

    Matching on the words is what survives, because the words are what the human was
    looking at when they decided. Each saved text is consumed against the first region
    that still has it, so duplicates resolve by list position.

    Returns ``None`` when the page no longer says the same things — a different number
    of regions, or different words. That is the honest answer: an order a human chose
    for text they never saw is worse than no order at all, because it looks decided.
    The caller then falls back to the model's order and says so.
    """
    remaining = list(page.regions)
    ids: list[str] = []
    for text in texts:
        wanted = (text or "").strip()
        match = next((r for r in remaining if (r.text or "").strip() == wanted), None)
        if match is None:
            return None
        remaining.remove(match)
        ids.append(match.id)
    if remaining:
        return None
    try:
        return reorder(page, ids)
    except ValueError:
        return None


def order_texts(page: PageRead) -> list[str]:
    """The page's region texts in its current reading order — what a saved order stores.

    Stored rather than ids for the reason in :func:`apply_text_order`.
    """
    return [(r.text or "").strip() for r in page.in_order()]


# ---- flattening (the novel path) ---------------------------------------------

def flatten(page: PageRead, *, kinds: frozenset[str] = PROSE_KINDS) -> str:
    """The page's prose as one string — what a flat-text read would have returned.

    This is the whole reason novels pay nothing for carrying regions. Concatenate the
    prose regions in reading order, honouring each one's seam, and the result goes into
    the existing page-seam and chapter-assembly machinery unchanged.

    The join rules are deliberately identical to the page-level assembler's, because a
    paragraph crossing a column break and one crossing a page break are the same event
    at two scales, and two implementations of one rule drift.
    """
    out = ""
    for region in page.in_order():
        if region.kind not in kinds:
            continue
        text = (region.text or "").strip()
        if not text:
            continue
        if not out:
            out = text
            continue
        # An unstated seam reads as `paragraph`: a spurious break is visible and a
        # human can delete it, whereas a missing one welds two paragraphs together
        # where nobody will notice.
        kind = region.join_prev or JOIN_PARAGRAPH
        if kind == JOIN_SENTENCE:
            out += ("" if region.join_glue == GLUE_NONE else " ") + text
        else:
            # `chapter` and `gap` both separate as paragraphs here. Their extra
            # meaning — split the chapter, warn about a missing page — belongs to the
            # build step, which reads the fields directly; swallowing it into the
            # string would lose it.
            out += "\n\n" + text
    return out


def gaps(page: PageRead) -> list[str]:
    """Region seams that claim content is missing, as warnings for the build step."""
    return [f"text may be missing just before region {r.id}"
            for r in page.in_order() if r.join_prev == JOIN_GAP]


# ---- serialization -----------------------------------------------------------
# Tolerant, like every other loader here: a malformed record degrades to something
# usable rather than raising. A page transcribed at real cost must not become
# unreadable because one field came back the wrong type.

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


def _as_box(value) -> tuple[float, float, float, float]:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(float(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            pass
    return (0.0, 0.0, 0.0, 0.0)


def region_from_dict(data: dict, fallback_order: int = 0) -> Region:
    data = data if isinstance(data, dict) else {}
    try:
        order = int(data.get("order", fallback_order))
    except (TypeError, ValueError):
        order = fallback_order
    glue = str(data.get("join_glue") or GLUE_NONE)
    return Region(
        id=str(data.get("id") or f"r{fallback_order}"),
        box=_as_box(data.get("box")),
        text=str(data.get("text") or ""),
        kind=str(data.get("kind") or KIND_BODY),
        order=order,
        join_prev=str(data.get("join_prev") or ""),
        join_glue=glue if glue in GLUE_KINDS else GLUE_NONE,
    )


def page_from_dict(data: dict) -> PageRead:
    data = data if isinstance(data, dict) else {}
    raw_regions = data.get("regions")
    regions = [region_from_dict(r, i)
               for i, r in enumerate(raw_regions if isinstance(raw_regions, list) else [])]
    meta_raw = data.get("meta")
    meta_raw = meta_raw if isinstance(meta_raw, dict) else {}
    heading = meta_raw.get("heading")

    def _int(key: str) -> int:
        try:
            return max(0, int(data.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return PageRead(
        width=_int("width"),
        height=_int("height"),
        regions=regions,
        order_source=("user" if data.get("order_source") == "user" else "model"),
        meta=PageMeta(
            confidence=str(meta_raw.get("confidence") or "low"),
            heading=str(heading) if heading else None,
            starts_mid_sentence=_as_bool(meta_raw.get("starts_mid_sentence")),
            ends_mid_sentence=_as_bool(meta_raw.get("ends_mid_sentence")),
            ends_mid_word=_as_bool(meta_raw.get("ends_mid_word")),
            notes=_as_notes(meta_raw.get("notes")),
        ),
    )
