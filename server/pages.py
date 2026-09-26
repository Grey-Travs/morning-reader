"""Scanned-page storage: the uploads, and the manifest that orders them.

A project whose source is images keeps its uploads in ``projects/<pid>/pages/`` and
one manifest, ``projects/<pid>/pages.json``, holding the ordered page list and each
page's read. Building chapters turns that manifest into the ordinary ``source.json``,
after which the project is indistinguishable from a pasted one and the whole
translation pipeline runs unchanged.

Why a separate file from ``state.json``: that one is per-CHAPTER translation state,
overlaid one chapter at a time by the worker. Page records have a different key space
and a different lifecycle, and folding them in would break that merge.

**Each page record carries its whole PageRead** — regions, geometry, dimensions — not
flat text. That is the contract fixed in ``morning/pageread.py`` on day one; a novel
flattens it at build time, a manga keeps it. Storing flat text here and adding regions
later would mean re-reading every page already transcribed.

Ordering note: a page's filename carries its ``seq``, a monotonic counter, never its
position. Reordering rewrites the manifest only — it never renames a file, which would
break image caching in the browser and race an in-flight read holding a path.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from morning.atomic import (
    atomic_write_json, quarantine_unreadable, read_text_retrying,
)
from morning.images import ImageInfo
from morning.pageread import (
    FURNITURE_KINDS, GLUE_NONE, JOIN_KINDS, KIND_FURIGANA, KIND_HEADING, PROSE_KINDS,
    TRANSLATED_KINDS, apply_text_order, flatten, furniture_kind,
    is_drawable, order_texts, page_from_dict, region_hash,
    # Aliased: this module defines its own `reorder` for the PAGE order, and a
    # bare import would be shadowed by it — silently sending a list of region
    # ids to the function that reorders pages.
    reorder as reorder_regions,
)
from morning.reading_order import disagreement, looks_reversed

from . import projects as pj
from .locks import file_lock

PAGES_FILENAME = "pages.json"
PAGES_DIRNAME = "pages"

MAX_PAGES_PER_PROJECT = 2000

# The eight states a page moves through.
STATUS_NEW = "new"                  # uploaded, never read
STATUS_QUEUED = "queued"            # waiting for the worker
STATUS_RUNNING = "reading"          # the model has it now
STATUS_OK = "ok"                    # read, and the model was confident
STATUS_NEEDS_CHECK = "needs-check"  # read, but a human should look
STATUS_EDITED = "edited"            # a human has corrected it
STATUS_SKIPPED = "skipped"          # deliberately not part of the text
STATUS_FAILED = "failed"

STATUSES = (STATUS_NEW, STATUS_QUEUED, STATUS_RUNNING, STATUS_OK, STATUS_NEEDS_CHECK,
            STATUS_EDITED, STATUS_SKIPPED, STATUS_FAILED)

# Statuses whose text is ready to go into a chapter. A page that merely needs checking
# is deliberately excluded: building with it would put un-reviewed transcription into
# the novel, which is the same mistake as reading an unaccepted translation.
APPROVED_STATUSES = (STATUS_OK, STATUS_EDITED)

# Statuses a page returns to when work on it is abandoned. Restoring to `status`
# itself would leave a page reading "queued" forever, because that is what it was set
# to when the work was accepted.
def resting_status(page: dict) -> str:
    """What this page should read as when its queued work is dropped."""
    if page.get("read"):
        return page.get("last_status") or STATUS_NEEDS_CHECK
    return STATUS_NEW


_PAGE_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_UNSAFE_LABEL_RE = re.compile(r"[^\w .\-()\[\]　-鿿＀-￯]", re.UNICODE)


# ---- paths -------------------------------------------------------------------

def pages_dir(pid: str) -> Path:
    # Through the module, never a from-imported copy: a `from .projects import
    # PROJECTS_DIR` binds at import time, so the path could not be redirected —
    # which meant tests wrote into the real library instead of a scratch folder.
    return pj.PROJECTS_DIR / pid / PAGES_DIRNAME


def pages_file(pid: str) -> Path:
    return pj.PROJECTS_DIR / pid / PAGES_FILENAME


def resolve_page_file(pid: str, page_id: str, doc: dict | None = None) -> Path | None:
    """The image file for a page, or None.

    The path is resolved through the MANIFEST only — a URL segment never becomes a
    path component. The containment check afterwards is belt-and-braces against a
    manifest that was hand-edited or restored from a tampered backup.
    """
    if not _PAGE_ID_RE.match(page_id or ""):
        return None
    doc = doc if doc is not None else load_pages(pid)
    page = find_page(doc, page_id)
    if not page or not page.get("file"):
        return None
    folder = pages_dir(pid).resolve()
    try:
        target = (folder / str(page["file"])).resolve()
    except OSError:
        return None
    if target.parent != folder or not target.is_file():
        return None
    return target


# ---- the manifest ------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_doc() -> dict:
    return {"version": 1, "next_seq": 1, "pages": [], "batches": [],
            # A manga's chapters: contiguous runs of pages, written by the build and
            # never into source.json. A novel leaves this empty.
            "chapters": [],
            "build": None, "totals": {"cost_usd": 0.0}}


def load_pages(pid: str) -> dict:
    """Read the manifest.

    A missing or CORRUPT file reads as empty — one bad file must not take down the
    project (the same rule as State and Glossary). A file that cannot be READ does
    not: that raises, because "I could not open it" and "it is empty" are different
    claims, and only one of them is safe to save back.
    """
    path = pages_file(pid)
    if not path.exists():
        return new_doc()
    # Could not READ it — see morning/state.py for the full reasoning. Degrading here
    # is what would turn a momentary sharing violation into the permanent loss of every
    # page transcribed from a photo, because `mutate_pages` would save the empty
    # document straight back over it. The read retries and then RAISES; `mutate_pages`
    # never reaches its save when the body raises, which is the point.
    raw = read_text_retrying(path)
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        # Could not PARSE it: the file really is corrupt. Keep the bytes aside and
        # degrade, so one bad file cannot take the whole project down.
        quarantine_unreadable(path)
        return new_doc()
    if not isinstance(doc, dict):
        return new_doc()
    doc.setdefault("version", 1)
    doc.setdefault("pages", [])
    doc.setdefault("batches", [])
    doc.setdefault("build", None)
    doc.setdefault("chapters", [])
    doc.setdefault("totals", {"cost_usd": 0.0})
    if not isinstance(doc["chapters"], list):
        doc["chapters"] = []
    if not isinstance(doc["pages"], list):
        doc["pages"] = []
    doc.setdefault("next_seq",
                   max((int(p.get("seq") or 0) for p in doc["pages"]), default=0) + 1)
    return doc


def save_pages(pid: str, doc: dict) -> None:
    atomic_write_json(pages_file(pid), doc)


@contextmanager
def mutate_pages(pid: str):
    """Load -> mutate -> save the manifest with no other thread interleaving.

    Yields a FRESHLY LOADED manifest: anything read before the lock was taken is
    already stale. Keep the body short — a page worker may be waiting on it.
    """
    with file_lock(pages_file(pid)):
        doc = load_pages(pid)
        yield doc
        save_pages(pid, doc)


# ---- records -----------------------------------------------------------------

def find_page(doc: dict, page_id: str) -> dict | None:
    for page in doc.get("pages", []):
        if page.get("id") == page_id:
            return page
    return None


def find_by_seq(doc: dict, seq: int) -> dict | None:
    for page in doc.get("pages", []):
        if page.get("seq") == seq:
            return page
    return None


def find_by_hash(doc: dict, digest: str) -> dict | None:
    for page in doc.get("pages", []):
        if page.get("sha256") == digest:
            return page
    return None


def safe_label(name: str, limit: int = 120) -> str:
    """A client filename reduced to a display label. NEVER used as a path.

    Japanese characters are kept — a scan is quite likely to be named in Japanese, and
    stripping that would leave the user with a page called "1".
    """
    return _UNSAFE_LABEL_RE.sub("", (name or "").strip())[:limit]


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_batch(doc: dict, label: str = "") -> str:
    """A group of pages uploaded together. Worth recording because a batch is usually
    one physical session — one book, one sitting — and it is the unit a person thinks
    in when something went wrong with "the ones I did last night"."""
    batch_id = uuid.uuid4().hex[:8]
    doc.setdefault("batches", []).append(
        {"id": batch_id, "label": safe_label(label), "at": now_iso(), "count": 0})
    return batch_id


def _touch_batch(doc: dict, batch_id: str) -> None:
    for batch in doc.get("batches", []):
        if batch.get("id") == batch_id:
            batch["count"] = sum(1 for p in doc.get("pages", [])
                                 if p.get("batch") == batch_id)
            return


def add_page(doc: dict, *, info: ImageInfo, data_len: int, digest: str,
             batch: str, name: str = "") -> dict:
    """Append a page record and return it.

    The caller writes the file itself, using the returned ``file`` name. The record
    carries the dimensions from the moment it exists, because a page whose size is
    unknown cannot anchor an overlay and there is no later opportunity to measure the
    bytes without re-reading them.
    """
    seq = int(doc.get("next_seq") or 1)
    doc["next_seq"] = seq + 1
    page = {
        "id": uuid.uuid4().hex[:8],
        "seq": seq,
        "file": f"page-{seq:04d}.{info.kind}",
        "name": safe_label(name),
        "bytes": data_len,
        "sha256": digest,
        "width": info.width,
        "height": info.height,
        "batch": batch,
        "added_at": now_iso(),
        "status": STATUS_NEW,
        # The whole PageRead, once there is one — regions with geometry, not flat
        # text. See the module docstring.
        "read": None,
        # How this page joins the previous one. `join_prev_source` is "user" once a
        # human has decided, and is then NEVER revisited: a later re-read proposing
        # something different must not silently undo their correction.
        "join_prev": "",
        "join_prev_source": "",
        "join_glue": GLUE_NONE,
        "join_reason": "",
        "hint": "",
        "ocr": None,
        "error": None,
    }
    doc.setdefault("pages", []).append(page)
    _touch_batch(doc, batch)
    return page


def page_text(page: dict) -> str:
    """This page's prose, flattened from its regions.

    The novel path. A manga keeps the regions; this is what makes a scanned novel
    indistinguishable from a pasted one by the time it reaches the pipeline.

    From the EFFECTIVE read: a human's corrections and reading order are what the
    novel is built from. It used the raw read, so a novel built after a human reordered
    a page's columns came out in the model's order anyway.
    """
    if not page.get("read"):
        return ""
    read, _note = effective_read(page)
    return flatten(read, kinds=PROSE_KINDS)


def page_heading(page: dict) -> str:
    """The chapter heading printed on this page, as corrected by a human — what the
    page builder names, splits and opens chapters with."""
    if not page.get("read"):
        return ""
    read, _note = effective_read(page)
    return (read.meta.heading or "").strip()


def page_chars(page: dict) -> int:
    return len(page_text(page))


def set_join(page: dict, kind: str, *, glue: str = GLUE_NONE, reason: str = "",
             by_user: bool = False) -> bool:
    """Record how this page follows the previous one.

    A human's decision is final. Once ``join_prev_source`` is "user", a proposal from
    a later re-read is ignored — otherwise re-reading a page would silently undo a
    correction somebody made, and they would have no reason to look again.
    """
    if not by_user and page.get("join_prev_source") == "user":
        return False
    if kind not in JOIN_KINDS:
        return False
    page["join_prev"] = kind
    page["join_glue"] = glue
    page["join_reason"] = reason
    page["join_prev_source"] = "user" if by_user else "model"
    return True


def reorder(doc: dict, ids: list[str]) -> bool:
    """Reorder pages. ``ids`` must be a PERMUTATION of the existing ids.

    A partial list would silently drop pages from the book, so it is refused rather
    than applied. Nothing is renamed — see the module docstring.
    """
    pages = doc.get("pages", [])
    current = [str(p.get("id")) for p in pages]
    if sorted(str(i) for i in ids) != sorted(current):
        return False
    by_id = {str(p.get("id")): p for p in pages}
    doc["pages"] = [by_id[str(i)] for i in ids]
    return True


def delete_pages(doc: dict, ids: list[str]) -> list[dict]:
    """Remove pages from the manifest and return the removed records, so the caller
    can unlink their files."""
    wanted = {str(i) for i in ids}
    removed = [p for p in doc.get("pages", []) if str(p.get("id")) in wanted]
    doc["pages"] = [p for p in doc.get("pages", []) if str(p.get("id")) not in wanted]
    for batch in doc.get("batches", []):
        _touch_batch(doc, batch.get("id", ""))
    return removed


def set_status(doc: dict, page_id: str, status: str, **fields) -> dict | None:
    page = find_page(doc, page_id)
    if page is None:
        return None
    # Remember what it was resting at, so abandoned work can put it back rather than
    # leaving it on "queued" forever.
    if status in (STATUS_QUEUED, STATUS_RUNNING) and page.get("status") not in (
            STATUS_QUEUED, STATUS_RUNNING):
        page["last_status"] = page.get("status")
    page["status"] = status
    page.update(fields)
    return page


# ---- the manga side ----------------------------------------------------------
#
# Three keys live BESIDE ``read`` on a page record, never inside it, and that placement
# is the design rather than a detail. ``jobs._apply_page_result`` merges the page
# reader's output with ``record.update(fields)``, and those fields carry ``"read"`` —
# the whole new PageRead. So anything stored INSIDE ``read`` is silently destroyed by
# the next re-read, while a sibling survives untouched.
#
# That is why ``order`` (a human's corrected reading order), ``lines`` (English the
# owner paid for) and ``corrections`` (Japanese a human fixed) are siblings. It also
# fixes a live bug: ``order_source: "user"`` stored inside ``read`` is wiped by a
# re-read today, which the region contract's own docstring forbids.

def _ordered_read(page: dict):
    """The model's read with the human's reading order applied — the words still as
    the MODEL read them. What a saved order is stored against and matched on.

    A human's saved reading order is stored as the TEXTS in their order, not as region
    ids, because ids are not an identity — ``ocr._region_from`` assigns them from the
    model's list position, so a second read can hand the same bubble a different id.
    Matching on the words survives a re-read; matching on ids would take a human's
    correct ordering of the OLD boxes and scramble the new ones with it.

    When the page no longer says the same things the saved order is NOT forced on. That
    would be the genuinely dangerous outcome — a human's ordering applied to bubbles
    they never saw, looking decided. The model's order comes back instead, and the note
    says so by name.
    """
    read = page_from_dict(page.get("read") or {})
    # Guarded: a hand-edited or damaged manifest can hold anything here, and this is
    # called by every screen that shows the page — one bad value was a 500 on all of
    # them.
    stored = page.get("order")
    stored = stored if isinstance(stored, dict) else {}
    saved = stored.get("texts")
    if not saved or not isinstance(saved, list):
        return read, ""
    ids = stored.get("ids")
    ids = ids if isinstance(ids, list) else []
    # Ids are passed as a HINT alongside the words: they disambiguate two bubbles
    # saying exactly the same thing, and are ignored the moment the text disagrees.
    # An order stored before ids were kept simply has none, and matches on words alone.
    applied = apply_text_order(read, [str(t) for t in saved], [str(i) for i in ids])
    if applied is None:
        return read, ("your reading order no longer applies — the words on this page "
                      "changed when it was read again")
    return applied, ""


def corrections_of(page: dict) -> dict:
    """A page's corrections, ``{region_id: {"from", "to", "at"}}``, or ``{}``."""
    stored = page.get("corrections")
    if not isinstance(stored, dict):
        return {}
    return {str(rid): c for rid, c in stored.items()
            if isinstance(c, dict) and isinstance(c.get("to"), str)
            and isinstance(c.get("from"), str)}


def _apply_corrections(read, corrections: dict):
    """``read`` with a human's corrected text put in, and how many could not be.

    A correction replaces one region's text, and only while that region still says
    what the model read when the human corrected it — ``from`` is kept for exactly
    that check. Region ids are positions, not identities, so a correction whose words
    no longer match is NOT applied: putting a human's fix onto text they never saw is
    the same mistake a stale reading order would be.
    """
    if not corrections:
        return read, 0
    lost = 0
    regions = []
    used = set()
    by_id = {r.id: r for r in read.regions}
    for rid, correction in corrections.items():
        region = by_id.get(rid)
        if region is None or (region.text or "").strip() != correction["from"].strip():
            lost += 1
            continue
        used.add(rid)
    meta = read.meta
    for region in read.regions:
        if region.id in used:
            corrected = corrections[region.id]["to"]
            # The page's reported heading is the same words as its heading region, and
            # it is what names the chapter, splits it, and opens it. Correcting the
            # region left the misread title in all three; emptying one the reader
            # invented still split a chapter there.
            if (region.kind == KIND_HEADING and meta.heading
                    and meta.heading.strip() == (region.text or "").strip()):
                meta = replace(meta, heading=corrected.strip() or None)
            # The furniture rule ran on what the MODEL read. A page number it misread
            # as 一26一, corrected to —26—, would otherwise go into the novel as a
            # paragraph. The rule only relabels shapes story text never has.
            kind = (region.kind if region.kind in FURNITURE_KINDS
                    else furniture_kind(corrected) or region.kind)
            region = replace(region, text=corrected, kind=kind)
        regions.append(region)
    return replace(read, regions=regions, meta=meta), lost


def effective_read(page: dict):
    """The page as it should actually be read, and a note when something was lost.

    The model's read, in the human's reading order, with the human's corrections. In
    THAT order: a saved reading order is matched on the words the model read, so
    correcting a typo must not make the order stop matching and drop out.
    """
    read, note = _ordered_read(page)
    read, lost = _apply_corrections(read, corrections_of(page))
    if lost:
        lost_note = (f"{lost} of your corrections no longer appl"
                     f"{'ies' if lost == 1 else 'y'} — the words they corrected are "
                     f"not on this page any more")
        note = f"{note}; {lost_note}" if note else lost_note
    return read, note


def set_correction(page: dict, region_id: str, text: str) -> dict | None:
    """Record a human's correction of one region's text, or remove it.

    Returns the region as it now reads — ``{"id", "text", "original", "corrected"}``
    — or None when the page has no such region. Text equal to what the model read
    removes the correction rather than storing a no-op, which is also how a human
    takes one back.
    """
    read, _note = _ordered_read(page)
    region = next((r for r in read.regions if r.id == str(region_id)), None)
    if region is None:
        return None
    corrections = corrections_of(page)
    original = region.text or ""
    had = corrections.get(region.id, {}).get("to")
    if text.strip() == original.strip():
        corrections.pop(region.id, None)
    else:
        corrections[region.id] = {"from": original, "to": text, "at": now_iso()}
    page["corrections"] = corrections
    # When the page's words last changed by hand — taking a correction BACK included,
    # since a built novel still holds it. The rebuild warning is judged by this, on
    # the server's clock, not by whether a correction is still standing.
    if corrections.get(region.id, {}).get("to") != had:
        page["corrections_at"] = now_iso()
    return {"id": region.id, "text": corrections.get(region.id, {}).get("to", original),
            "original": original, "corrected": region.id in corrections}


def order_check(page: dict) -> dict:
    """What the page's own geometry thinks of its reading order.

    Free: the boxes are already stored and laying them out is arithmetic. It is also
    the only thing in the app that can catch a page read left-to-right, which is
    backwards sentence by sentence while every sentence stays fluent English.
    """
    read, _ = effective_read(page)
    return {"looks_reversed": looks_reversed(read),
            "disagreement": disagreement(read),
            "order_source": read.order_source}


def set_order(page: dict, ids: list[str] | None) -> bool:
    """Record a human's reading order for one page, or clear it.

    ``ids`` must be a permutation of the page's regions; a partial list would drop
    bubbles out of the reading, so it is refused rather than applied. ``None`` clears
    the saved order and returns the page to the model's — the only way back, and
    deliberately explicit: a human's order is never reverted silently, but it can be
    revoked on purpose.
    """
    if ids is None:
        page["order"] = None
        page["order_check"] = order_check(page)
        return True
    # The MODEL's words, not the corrected ones: the order is matched against the
    # read before corrections go in, so storing corrected text would never match.
    read, _ = _ordered_read(page)
    try:
        reordered = reorder_regions(read, [str(i) for i in ids])
    except ValueError:
        return False
    page["order"] = {"texts": order_texts(reordered),
                     # Both. The words are what survives a re-read; the ids are what
                     # tell two identical bubbles apart within THIS read.
                     "ids": [r.id for r in reordered.in_order()],
                     "source": "user", "at": now_iso()}
    page["order_check"] = order_check(page)
    return True


def carry_lines(page: dict) -> int:
    """After a re-read, move each translated line onto the region that now says its
    words. Returns how many moved.

    Lines are stored by region id, and ids are positions in the model's list — so a
    re-read that lists the same bubbles in another order handed every bubble its
    neighbour's English. Marked stale, so never passed off as right, but the chapter
    had to be paid for again, and English a human typed sat on the wrong bubble for
    good, since a re-translate never overwrites a human's line.

    The same rule the saved reading order follows: match on the WORDS (the line's
    ``source_hash``), prefer the id it already had, and never force a line onto words
    it was not translated from. A line whose words are gone stays where it was, stale,
    unless another line has rightfully claimed that id.
    """
    lines = page.get("lines")
    if not isinstance(lines, dict) or not lines:
        return 0
    read, _note = effective_read(page)
    by_id = {r.id: r for r in read.regions}
    by_hash: dict[str, list[str]] = {}
    for region in read.in_order():
        by_hash.setdefault(region_hash(region), []).append(region.id)

    def words(line) -> str:
        return line.get("source_hash") if isinstance(line, dict) else ""

    carried: dict = {}
    waiting = []
    for rid, line in lines.items():
        region = by_id.get(rid)
        if words(line) and region is not None and region_hash(region) == words(line):
            carried[rid] = line
        else:
            waiting.append((rid, line))

    moved = 0
    stranded = []
    for rid, line in waiting:
        target = next((i for i in by_hash.get(words(line), []) if i not in carried),
                      None) if words(line) else None
        if target is None:
            stranded.append((rid, line))
            continue
        carried[target] = line
        moved += target != rid
    for rid, line in stranded:
        carried.setdefault(rid, line)
    page["lines"] = carried
    return moved


def set_line(page: dict, region_id: str, **fields) -> dict:
    """Write one translated line onto a page record."""
    lines = page.setdefault("lines", {})
    if not isinstance(lines, dict):
        lines = page["lines"] = {}
    line = lines.setdefault(str(region_id), {})
    line.update(fields)
    line["at"] = now_iso()
    return line


def translatable_regions(page: dict) -> list:
    """The regions of this page a manga translation call would be given."""
    read, _ = effective_read(page)
    return [r for r in read.in_order()
            if r.kind in TRANSLATED_KINDS and (r.text or "").strip()]


def line_counts(page: dict) -> dict:
    """How much of this page has usable English on it.

    **Fails closed, uniquely in this file.** Every other loader here degrades open — a
    malformed record becomes something usable. A line whose ``source_hash`` is missing,
    empty or the wrong type is counted STALE, never translated. Failing open would show
    paid-for English on a bubble whose Japanese has since changed, which reads as
    correct and cannot be spotted by looking at it.
    """
    lines = page.get("lines") or {}
    total = translated = stale = undrawable = 0

    # Regions that carry text and are NOT translated. Since step 5 that is only a kind
    # this app does not know — a stored region keeps whatever kind is on disk. `body`,
    # `caption`, `note` and `heading` used to land here too, and a page made only of
    # them had the reader print "Nothing is said on this page" over a page covered in
    # Japanese; they are translated now. Counted so the text never silently vanishes.
    read, _note = effective_read(page)
    #
    # Furniture and furigana are not counted: a page number or a reading gloss is not
    # story text somebody failed to translate, and counting them would make every page
    # with a page number look unfinished.
    other = sum(1 for r in read.in_order()
                if r.kind not in TRANSLATED_KINDS
                and r.kind not in FURNITURE_KINDS and r.kind != KIND_FURIGANA
                and (r.text or "").strip())

    for region in translatable_regions(page):
        total += 1
        if not is_drawable(region.box):
            undrawable += 1
        record = lines.get(region.id) if isinstance(lines, dict) else None
        if not isinstance(record, dict) or not str(record.get("english") or "").strip():
            continue
        if record.get("source_hash") == region_hash(region):
            translated += 1
        else:
            stale += 1
    return {"lines": total, "translated": translated, "stale": stale,
            "untranslated": total - translated - stale, "undrawable": undrawable,
            "other": other}


# ---- manga chapters ----------------------------------------------------------
# The same lifecycle a page has, one level up. Named separately rather than reusing the
# page constants wholesale, because "reading" and "translating" are different work and
# a console that said "reading" while translating would be lying.

CHAPTER_NEW = ""                       # never translated
CHAPTER_QUEUED = STATUS_QUEUED
CHAPTER_RUNNING = "translating"
CHAPTER_STATUSES = (CHAPTER_NEW, CHAPTER_QUEUED, CHAPTER_RUNNING, STATUS_OK,
                    STATUS_NEEDS_CHECK, STATUS_FAILED)


def find_chapter(doc: dict, index: int) -> dict | None:
    for chapter in doc.get("chapters") or []:
        if int(chapter.get("index") or 0) == int(index):
            return chapter
    return None


def resting_chapter_status(chapter: dict) -> str:
    """What a chapter should read as when its queued work is dropped.

    Restoring to ``status`` itself would leave it on "queued" forever, because that is
    what it was set to when the work was accepted — the same trap
    :func:`resting_status` exists for on the page axis.
    """
    if chapter.get("at"):
        return chapter.get("last_status") or STATUS_NEEDS_CHECK
    return CHAPTER_NEW


def set_chapter_status(doc: dict, index: int, status: str, **fields) -> dict | None:
    chapter = find_chapter(doc, index)
    if chapter is None:
        return None
    if status in (CHAPTER_QUEUED, CHAPTER_RUNNING) and chapter.get("status") not in (
            CHAPTER_QUEUED, CHAPTER_RUNNING):
        chapter["last_status"] = chapter.get("status", CHAPTER_NEW)
    chapter["status"] = status
    chapter.update(fields)
    return chapter


def chapter_pages(doc: dict, chapter: dict) -> list[dict]:
    """This chapter's pages, in the order the MANIFEST has them NOW.

    ``page_ids`` records MEMBERSHIP. The manifest records ORDER. The two disagree the
    moment somebody reorders pages after a build — and reading the stored order then
    showed the book in an order the owner had explicitly changed, while the pages
    screen showed the new one. Two screens, silently disagreeing, with the one that
    obeyed being the one nobody reads in.

    A page deleted since the build is simply gone: the stored list still names it, and
    there is nothing to show.
    """
    wanted = {str(i) for i in (chapter.get("page_ids") or [])}
    return [p for p in doc.get("pages", []) if str(p.get("id")) in wanted]


def chapter_needs_translating(doc: dict, chapter: dict) -> bool:
    """Whether this chapter has bubbles with no English on them.

    DERIVED from the lines, never from the stored status, and that is the point. The
    status describes the last RUN; the lines are the work itself, and they live on the
    pages. A stored status went wrong in three ways at once: deleting a page and
    rebuilding reset a paid-for chapter to "never translated" and a sweep re-billed
    it; building and translating before the pages were read marked every chapter
    "translated" permanently, because there were no lines to fail; and a chapter with
    one missing line stayed "partly translated" forever, re-billed in full by every
    sweep even after the owner typed that line in by hand.

    Derivation fixes all three, because it asks the only question that matters: is
    there a bubble here with nothing on it?
    """
    return chapter_counts(doc, chapter)["untranslated"] > 0


def chapter_counts(doc: dict, chapter: dict) -> dict:
    """How much of this chapter has usable English on it."""
    wanted = {str(i) for i in (chapter.get("page_ids") or [])}
    counts = {"lines": 0, "translated": 0, "stale": 0, "untranslated": 0,
              "undrawable": 0, "other": 0, "pages": 0, "unchecked_pages": 0,
              "unmeasured_pages": 0, "silent_pages": 0}
    for page in doc.get("pages", []):
        if str(page.get("id")) not in wanted:
            continue
        counts["pages"] += 1
        if page.get("status") == STATUS_NEEDS_CHECK:
            counts["unchecked_pages"] += 1
        if not (page.get("width") and page.get("height")):
            counts["unmeasured_pages"] += 1
        page_counts = line_counts(page)
        if page_counts["lines"] == 0 and page_counts["other"] == 0:
            # A page with nothing said on it. Counted rather than ignored: in a manga
            # it is usually a splash or an action beat, and "0 of 0 translated" reading
            # as complete would hide a page that was never read at all.
            counts["silent_pages"] += 1
        for key, value in page_counts.items():
            counts[key] += value
    return counts


def summary(doc: dict) -> dict:
    """Counts the pages screen reads, so it does not compute them five ways."""
    pages = doc.get("pages", [])
    by_status: dict[str, int] = {}
    for page in pages:
        status = str(page.get("status") or STATUS_NEW)
        by_status[status] = by_status.get(status, 0) + 1
    counts = {"lines": 0, "translated": 0, "stale": 0, "untranslated": 0,
              "undrawable": 0, "other": 0}
    for page in pages:
        for key, value in line_counts(page).items():
            counts[key] += value
    return {
        "total": len(pages),
        "by_status": by_status,
        "ready": sum(1 for p in pages if p.get("status") in APPROVED_STATUSES),
        "unmeasured": sum(1 for p in pages
                          if not (p.get("width") and p.get("height"))),
        "chars": sum(page_chars(p) for p in pages),
        "totals": doc.get("totals", {"cost_usd": 0.0}),
        "build": doc.get("build"),
        # The manga side. A novel's pages carry no lines, so these come back zero and
        # the pages screen shows nothing extra.
        "chapters": len(doc.get("chapters") or []),
        # Counted only where the human has NOT already decided the order: nagging a
        # page somebody has settled is what `set_join`'s user rule exists to prevent.
        "reversed_pages": sum(
            1 for p in pages
            if (p.get("order_check") or {}).get("looks_reversed")
            and (p.get("order_check") or {}).get("order_source") != "user"),
        **counts,
    }
