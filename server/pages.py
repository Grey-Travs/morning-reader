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
from datetime import datetime, timezone
from pathlib import Path

from morning.atomic import (
    atomic_write_json, quarantine_unreadable, read_text_retrying,
)
from morning.images import ImageInfo
from morning.pageread import (
    GLUE_NONE, JOIN_KINDS, PROSE_KINDS, TRANSLATED_KINDS, apply_text_order, flatten,
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
    """
    read = page.get("read")
    if not read:
        return ""
    return flatten(page_from_dict(read), kinds=PROSE_KINDS)


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
# That is why ``order`` (a human's corrected reading order) and ``lines`` (English the
# owner paid for) are siblings. It also fixes a live bug: ``order_source: "user"``
# stored inside ``read`` is wiped by a re-read today, which the region contract's own
# docstring forbids.

def effective_read(page: dict):
    """The page as it should actually be read, and a note when something was lost.

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
    saved = (page.get("order") or {}).get("texts")
    if not saved:
        return read, ""
    applied = apply_text_order(read, [str(t) for t in saved])
    if applied is None:
        return read, ("your reading order no longer applies — the words on this page "
                      "changed when it was read again")
    return applied, ""


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
    read, _ = effective_read(page)
    try:
        reordered = reorder_regions(read, [str(i) for i in ids])
    except ValueError:
        return False
    page["order"] = {"texts": order_texts(reordered), "source": "user",
                     "at": now_iso()}
    page["order_check"] = order_check(page)
    return True


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
            "untranslated": total - translated - stale, "undrawable": undrawable}


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


def chapter_counts(doc: dict, chapter: dict) -> dict:
    """How much of this chapter has usable English on it."""
    wanted = {str(i) for i in (chapter.get("page_ids") or [])}
    counts = {"lines": 0, "translated": 0, "stale": 0, "untranslated": 0,
              "undrawable": 0, "pages": 0, "unchecked_pages": 0,
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
        if page_counts["lines"] == 0:
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
              "undrawable": 0}
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
