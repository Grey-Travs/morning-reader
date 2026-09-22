"""The Chapter contract: one unit of prose, however it was ingested.

Every ingestion path — pasted text, a .txt file, a Google Doc, a site export, a stack
of scanned page images — converges on this shape, and everything downstream (classify,
translate, validate, read) only ever sees this. That convergence is what lets the four
ingestion paths be built independently: same output contract, no shared files.

``content_hash`` is the resumability key. It hashes the prose and nothing else, so
retitling a chapter does not re-bill its translation, while editing one word does.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .japanese import source_fraction

# Characters that open a line of dialogue. Japanese prose uses the corner brackets;
# the straight and curly doubles show up in text that has been through a western
# editor or a site export. Night Reader's list already carried 「」『』 — they are
# used in Korean prose too — so this transfers unchanged.
_QUOTE_CHARS = "\"\u201c\u201d\u300c\u300d\u300e\u300f"
_QUOTE_RE = re.compile(f"[{re.escape(_QUOTE_CHARS)}]")

# Zero-width / invisible formatting characters — often a copy-protection watermark
# pasted into web-novel text. They carry no meaning but inflate char/paragraph counts,
# confuse the model, and (worse) change the content hash, so an invisible character
# drifting in on a re-paste would re-bill a chapter that did not change. Stripped at
# extraction, before anything is hashed.
_INVISIBLE_RE = re.compile(
    "["
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e"
    "\u200b\u200c\u200d\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064"
    "\u2066\u2067\u2068\u2069\u206a\u206b\u206c\u206d\u206e\u206f"
    "\u3164\ufeff\uffa0"
    "]"
)

_WS_RE = re.compile(r"\s")


def strip_invisibles(text: str) -> str:
    return _INVISIBLE_RE.sub("", text or "")


@dataclass
class ChapterMetrics:
    paragraph_count: int
    dialogue_count: int
    char_count: int          # non-whitespace only — see source_fraction's reasoning
    source_fraction: float   # how much of it is Japanese script
    content_hash: str


@dataclass
class Chapter:
    index: int  # 1-based, in document order
    title: str
    paragraphs: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def metrics(self) -> ChapterMetrics:
        text = self.text
        return ChapterMetrics(
            paragraph_count=len(self.paragraphs),
            dialogue_count=sum(1 for p in self.paragraphs if _QUOTE_RE.search(p)),
            char_count=len(_WS_RE.sub("", text)),
            source_fraction=round(source_fraction(text), 4),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )


# ---- classification ----------------------------------------------------------
# What kind of tab this is, as a PERSISTED string. The three values are deliberately
# language-free: "source" means "written in this project's source language", not
# "Japanese". Night Reader's equivalent returns the literal "korean", which is stored
# on every page record and is one of the three contracts that made adding a second
# language uneconomic. Getting this right on day one is the whole reason this app
# exists separately, so it is spelled out rather than left to a convention.
KIND_SOURCE = "source"     # needs translating
KIND_ENGLISH = "english"   # already English — skip, don't waste a call on it
KIND_EMPTY = "empty"       # no prose at all (a blank placeholder tab)

CLASSES = (KIND_SOURCE, KIND_ENGLISH, KIND_EMPTY)


def classify(chapter: Chapter, min_source_fraction: float) -> str:
    """Which of the three kinds this chapter is.

    The threshold is a config value rather than a constant because the right cutoff
    depends on the material: a chapter of mostly-dialogue Japanese scores near 1.0,
    but one quoting a long English song lyric can dip a long way, and a novel with
    heavy furigana or romanized names sits lower still.
    """
    if not chapter.paragraphs:
        return KIND_EMPTY
    if chapter.metrics.source_fraction < min_source_fraction:
        return KIND_ENGLISH
    return KIND_SOURCE


def chapters_to_records(chapters: list[Chapter]) -> list[dict]:
    """Serialize for on-disk storage (source.json)."""
    return [{"title": c.title, "paragraphs": c.paragraphs} for c in chapters]


def records_to_chapters(records: list[dict]) -> list[Chapter]:
    """Rebuild from stored records, in order.

    Indices are re-derived from POSITION rather than read back from the record. They
    are a position in the document and nothing else; storing them would let a
    hand-edited or partially-written source.json produce two chapters claiming index
    4, which state.json (keyed by index) would then merge into one.
    """
    out: list[Chapter] = []
    for i, rec in enumerate(records or [], start=1):
        if not isinstance(rec, dict):
            continue
        paragraphs = rec.get("paragraphs")
        out.append(Chapter(
            index=i,
            title=str(rec.get("title") or f"Chapter {i}"),
            paragraphs=[str(p) for p in paragraphs] if isinstance(paragraphs, list) else [],
        ))
    # Re-index after skipping any malformed record, so indices stay contiguous.
    for i, ch in enumerate(out, start=1):
        ch.index = i
    return out
