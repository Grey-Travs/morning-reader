"""Joining pages into chapters.

A scanned novel arrives as a stack of pages; the pipeline wants chapters. This is the
seam between the two, and it does two things:

* **Propose how each page follows the one before it** — the same
  ``sentence | paragraph | chapter | gap`` vocabulary the regions already use, one
  level up. The proposal is a guess and is labelled as one; a human can overrule it,
  and once they have, :func:`server.pages.set_join` never lets a later read undo them.
* **Assemble the approved pages into chapters**, honouring those seams.

The seam that matters is ``sentence``. A page break in the middle of a sentence is
extremely common and completely invisible in the text — the page simply stops. Join it
as a paragraph and the novel gains a false break in the middle of a line; join a real
paragraph break as a sentence and two paragraphs weld together. Both are wrong, but
they are wrong in different directions, and the model has already been asked the only
question that distinguishes them: did this page end mid-sentence, and did the next one
start mid-sentence?

Once assembled, a scanned novel is an ordinary list of
:class:`~morning.chapters.Chapter` objects and nothing downstream can tell it from a
paste. That convergence is the point of the Chapter contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .chapters import Chapter
from .pageread import (
    GLUE_NONE, GLUE_SPACE, JOIN_CHAPTER, JOIN_GAP, JOIN_PARAGRAPH, JOIN_SENTENCE,
)
from .textsource import SEP_RE, looks_like_heading

# A page whose text ends with one of these ended a sentence, whatever the model said
# about it. Both the Japanese and the ASCII forms, because a transcription can carry
# either.
_ENDS_SENTENCE = re.compile("[。！？.!?」』”)）]\\s*$")

# ...and one that starts with a closing bracket or a continuation particle is very
# likely carrying on from the previous page.
_STARTS_CONTINUATION = re.compile("^\\s*[」』、，をがに"
                                  "へとのもは]")


@dataclass
class Join:
    """A proposed seam, with why. ``confidence`` is 0-1; a low one is worth a look."""

    kind: str = JOIN_PARAGRAPH
    glue: str = GLUE_NONE
    confidence: float = 0.4
    reason: str = ""


@dataclass
class Assembly:
    """The chapters built from a run of pages, and anything worth saying about them."""

    chapters: list[Chapter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages_used: int = 0


def _page_meta(page: dict) -> dict:
    read = page.get("read") or {}
    meta = read.get("meta") if isinstance(read, dict) else None
    return meta if isinstance(meta, dict) else {}


def _heading_of(page: dict) -> str:
    return str(_page_meta(page).get("heading") or "").strip()


def propose_join(previous_text: str, following_text: str,
                 previous: dict, following: dict) -> Join:
    """How ``following`` continues from ``previous``.

    Ordered most-certain first. Each rule is a different KIND of evidence, so the
    order is not arbitrary: a printed chapter heading beats any inference about
    sentence flow, and a word cut in half beats a full stop that is merely absent.
    """
    meta_prev = _page_meta(previous)
    meta_next = _page_meta(following)
    heading = _heading_of(following)

    # 1. A chapter heading printed on the page. The strongest evidence there is —
    #    the book itself is saying where the boundary falls.
    if heading:
        return Join(JOIN_CHAPTER, GLUE_NONE, 0.95,
                    f"the page begins a chapter: {heading}")

    # 2. A word split across the break. Nothing else can explain that, and joining it
    #    as anything but a continuation leaves half a word at the end of a paragraph.
    if meta_prev.get("ends_mid_word"):
        return Join(JOIN_SENTENCE, GLUE_NONE, 0.95, "a word is split across the break")

    # 3. Both sides agree the sentence runs on.
    if meta_prev.get("ends_mid_sentence") and meta_next.get("starts_mid_sentence"):
        return Join(JOIN_SENTENCE, GLUE_NONE, 0.9,
                    "the sentence continues across the break")

    # 4. They disagree, which usually means a page was never photographed: one side
    #    stops mid-sentence and the other starts cleanly. Reported rather than
    #    silently smoothed over, because a missing page is the one thing a reader
    #    cannot recover by re-reading.
    if meta_prev.get("ends_mid_sentence") and not meta_next.get("starts_mid_sentence"):
        if not _STARTS_CONTINUATION.search(following_text or ""):
            return Join(JOIN_GAP, GLUE_NONE, 0.6,
                        "the previous page stops mid-sentence but this one starts "
                        "fresh — a page may be missing")
        return Join(JOIN_SENTENCE, GLUE_NONE, 0.6,
                    "this page opens with a continuation")

    # 5. The text itself ends a sentence. Trusted over an absent flag, because a model
    #    that forgot to set one still transcribed the full stop it could see.
    if _ENDS_SENTENCE.search(previous_text or ""):
        return Join(JOIN_PARAGRAPH, GLUE_NONE, 0.75,
                    "the previous page ends a sentence")

    # 6. Nothing conclusive. Paragraph is the safe default for the same reason it is
    #    inside a page: a spurious break is visible and fixable, a missing one welds
    #    two paragraphs together where nobody will notice.
    return Join(JOIN_PARAGRAPH, GLUE_NONE, 0.4, "no clear signal either way")


@dataclass
class Span:
    """A manga chapter: a contiguous run of pages, not a string of prose."""

    index: int
    title: str
    start_seq: int
    end_seq: int
    page_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"index": self.index, "title": self.title,
                "start_seq": self.start_seq, "end_seq": self.end_seq,
                "page_ids": list(self.page_ids)}


@dataclass
class SpanAssembly:
    spans: list[Span] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages_used: int = 0


def assemble_spans(pages: list[dict]) -> SpanAssembly:
    """Group an ordered run of pages into manga chapters.

    The same seam vocabulary as :func:`assemble`, asked of the same field — a manga's
    chapter boundary is the same question as a novel's. What differs is what comes out:
    a novel gets prose, a manga gets a RANGE OF PAGES, because a manga chapter is its
    art and its art never becomes a string.

    **Which pages are in a span is decided differently from the novel path, on
    purpose.** ``assemble`` takes only ``APPROVED_STATUSES``, excluding a page the
    reader was unsure about, because once prose is concatenated into a chapter there is
    no way to point at the un-reviewed part ever again. In a manga every line stays
    bolted to its page and its box forever, so it CAN be pointed at — and it is, by the
    reader and the script view, which carry each page's status. So the gate moves from
    *exclude* to *label*: a span holds every non-skipped page in its run.

    Excluding an unchecked page here would put a hole in the middle of the book. A
    wordless action page has no text to review at all and is frequently the climax; a
    page dropped from a manga is a scene the reader never sees, which is far worse than
    un-reviewed transcription they can see and correct.

    A book with no chapter seam anywhere becomes one chapter of every page, which is
    the right answer for a single-volume scan.
    """
    spans: list[Span] = []
    warnings: list[str] = []
    current: list[dict] = []
    title = ""
    used = 0

    def flush() -> None:
        nonlocal current, title
        if current:
            spans.append(Span(
                index=len(spans) + 1,
                title=title or f"Chapter {len(spans) + 1}",
                start_seq=int(current[0].get("seq") or 0),
                end_seq=int(current[-1].get("seq") or 0),
                page_ids=[str(p.get("id") or "") for p in current]))
        current, title = [], ""

    for position, page in enumerate(pages):
        if page.get("status") == "skipped":
            continue
        used += 1
        heading = _heading_of(page)
        kind = str(page.get("join_prev") or "")

        if kind == JOIN_GAP:
            label = page.get("name") or f"page {page.get('seq', position + 1)}"
            warnings.append(
                f"A page may be missing just before {label}. Check whether one went "
                f"unphotographed — a missing page is a scene nobody will see.")

        if current and kind == JOIN_CHAPTER:
            flush()
        if not current and heading:
            title = heading
        current.append(page)

    flush()
    return SpanAssembly(spans=spans, warnings=warnings, pages_used=used)


def assemble(pages: list[dict], *, text_of) -> Assembly:
    """Build chapters from an ordered run of pages.

    ``text_of`` turns one page into its prose — injected rather than imported so the
    engine does not reach into the server's storage module, and so a manga can pass a
    different flattening without this needing to know about it.

    A ``gap`` is joined as a paragraph and REPORTED. It is the only place a page you
    never photographed can surface, and swallowing it would leave a novel with a
    silent hole in the middle of a scene.
    """
    chapters: list[Chapter] = []
    warnings: list[str] = []
    buffer = ""
    title = ""
    used = 0

    def flush() -> None:
        nonlocal buffer, title
        paragraphs = [p.strip() for p in SEP_RE.split(buffer) if p.strip()]
        if paragraphs:
            chapters.append(Chapter(index=len(chapters) + 1,
                                    title=title or f"Chapter {len(chapters) + 1}",
                                    paragraphs=paragraphs))
        buffer, title = "", ""

    for position, page in enumerate(pages):
        text = (text_of(page) or "").strip()
        if not text:
            continue
        used += 1

        if not buffer:
            # The first page of a chapter. Its printed heading, if it has one, names
            # the chapter — and if that heading is also the first line of the text,
            # it is not repeated.
            heading = _heading_of(page)
            if heading:
                title = heading
                if not looks_like_heading(text.split("\n", 1)[0]):
                    text = f"{heading}\n\n{text}"
            buffer = text
            continue

        kind = str(page.get("join_prev") or JOIN_PARAGRAPH)
        glue = str(page.get("join_glue") or GLUE_NONE)

        if kind == JOIN_GAP:
            label = page.get("name") or f"page {page.get('seq', position + 1)}"
            warnings.append(
                f"A page may be missing just before {label} — the text does not "
                f"follow on. Check whether one went unphotographed.")
            kind = JOIN_PARAGRAPH

        if kind == JOIN_CHAPTER:
            flush()
            heading = _heading_of(page)
            if heading:
                title = heading
                if not looks_like_heading(text.split("\n", 1)[0]):
                    text = f"{heading}\n\n{text}"
            buffer = text
            continue

        if kind == JOIN_SENTENCE:
            buffer = buffer.rstrip() + ("" if glue == GLUE_NONE else " ") + text.lstrip()
        else:
            buffer = buffer.rstrip() + "\n\n" + text.lstrip()

    flush()
    return Assembly(chapters=chapters, warnings=warnings, pages_used=used)
