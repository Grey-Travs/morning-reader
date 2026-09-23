"""Master glossary, relevant-entry injection, and the human review queue.

The glossary is the single source of truth for names and terms. Only entries that
actually occur in a chapter are injected into that chapter's prompt, which keeps the
prompt lean as the glossary grows. Newly encountered terms are never auto-committed —
they land in a pending queue behind an approve/edit/reject gate, so a misclassified
name or a wrong reading cannot silently propagate through a whole novel.

Two things here are deliberately NOT what the Korean app does, and both are because
of how Japanese is written.

**Variants, not one key.** Night Reader stores one ``korean`` string per entry and
matches it as a plain substring (``glossary.py:118-121``). That works for Korean,
where a name has essentially one spelling. A Japanese name does not: 佐々木 in the
narration is ささき in a child's dialogue and ササキ on a sign, and they are the same
person. One stored key matches one spelling and silently misses the others — the model
then re-guesses the romanization and the character's name drifts mid-novel. So an
entry holds a LIST of spellings and any of them can match.

**A length floor.** With variants, a one-character entry becomes actively dangerous:
森 ("Mori", a perfectly ordinary surname) occurs inside 森林 (forest), 青森 (Aomori)
and 森田 (Morita). A bare substring test injects the wrong character into the prompt
for a chapter that never mentions them. See ``_matches`` for the rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic import (
    atomic_write_json, atomic_write_text, quarantine_unreadable,
    read_text_retrying,
)
from .locks import file_lock

VALID_TYPES = {"name", "place", "skill", "term", "other"}
VALID_PRONOUNS = {"he", "she", "they"}

# How a character refers to THEMSELVES. This has no Korean equivalent worth storing
# and is one of the strongest characterisation signals Japanese has: 俺 reads rough
# and masculine, 僕 softer and boyish, 私 neutral or formal, あたし casually feminine,
# わたくし stiffly formal. English has only "I", so the distinction has to be carried
# in register and word choice instead — which the model can only do consistently if it
# is told, every chapter, which one this character uses.
COMMON_FIRST_PERSON = ("俺", "僕", "私", "あたし",
                       "わたくし", "うち", "小生")

# Register, in the terms Japanese actually marks it in.
COMMON_REGISTERS = ("plain", "polite", "honorific", "humble", "rough", "archaic")

# Kanji. Used only by the one-character rule below.
_KANJI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

# Below this many characters, a variant is matched with the neighbour rule rather
# than as a bare substring. Two is the floor because two-character compounds are
# already specific enough to be safe in practice, while one character is not.
MIN_PLAIN_MATCH_LEN = 2


def normalize_pronoun(value: object) -> str:
    """Reduce a model-proposed pronoun to "he"/"she"/"they", or "" for anything else.

    Only MODEL output goes through this. A value a human typed is stored as they typed
    it — the gate exists to stop the model inventing a category, not to overrule a
    person who knows the character.
    """
    p = str(value or "").strip().lower()
    return p if p in VALID_PRONOUNS else ""


@dataclass
class GlossaryEntry:
    """One locked term.

    ``source`` is the primary spelling; ``variants`` are the other spellings that mean
    the same thing. The field is ``source`` and never a language name — see
    ``morning/__init__.py`` for why that is not negotiable.
    """

    source: str = ""
    english: str = ""
    variants: list[str] = field(default_factory=list)
    type: str = "other"
    note: str = ""
    # Character hints, most useful on `name` entries. Japanese omits pronouns
    # constantly and encodes social register in verb endings, so pinning these keeps
    # the English voice steady across chapters instead of being re-guessed each time.
    pronoun: str = ""       # he | she | they
    register: str = ""      # see COMMON_REGISTERS
    first_person: str = ""  # 俺 / 僕 / 私 / あたし … see COMMON_FIRST_PERSON
    # The reading, in kana. Worth its own field rather than being one more variant:
    # it is what disambiguates a name whose kanji have several possible readings, and
    # it is exactly what furigana on a scanned page provides (see morning/pageread.py,
    # KIND_FURIGANA).
    reading: str = ""

    def spellings(self) -> list[str]:
        """Every spelling this entry answers to, primary first, de-duplicated.

        The reading counts as a spelling: a novel that introduces 御堂筋 with furigana
        and then writes みどうすじ in a letter is writing the same name twice.
        """
        out: list[str] = []
        for candidate in [self.source, *self.variants, self.reading]:
            text = (candidate or "").strip()
            if text and text not in out:
                out.append(text)
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "GlossaryEntry":
        d = d if isinstance(d, dict) else {}
        raw_variants = d.get("variants")
        variants = ([str(v).strip() for v in raw_variants if str(v).strip()]
                    if isinstance(raw_variants, list) else [])
        return cls(
            source=str(d.get("source", "")).strip(),
            english=str(d.get("english", "")).strip(),
            variants=variants,
            type=(str(d.get("type", "other")).strip().lower() or "other"),
            note=str(d.get("note", "")).strip(),
            pronoun=str(d.get("pronoun", "")).strip(),
            register=str(d.get("register", "")).strip(),
            first_person=str(d.get("first_person", "")).strip(),
            reading=str(d.get("reading", "")).strip(),
        )


def _matches(spelling: str, text: str) -> bool:
    """Whether ``spelling`` occurs in ``text`` as this entry rather than by accident.

    Two or more characters: a plain substring test. Japanese has no inter-word spaces,
    so substring IS the right mechanism — this is the part of the Korean design that
    transfers unchanged, and the reason it transfers is that neither language
    delimits words.

    Exactly one character: substring, but only where neither neighbour is a kanji.
    A single-kanji surname is followed by a particle (森は…), an honorific (森さん) or
    punctuation, whereas the same character inside a compound is flanked by more kanji
    (森林, 青森, 森田). Checking the neighbours separates those two cases without
    needing a dictionary, and errs toward matching — a false injection costs a few
    tokens of prompt, while a miss costs the character's name.
    """
    if not spelling or not text:
        return False
    if len(spelling) >= MIN_PLAIN_MATCH_LEN:
        return spelling in text

    start = 0
    while True:
        i = text.find(spelling, start)
        if i == -1:
            return False
        before = text[i - 1] if i > 0 else ""
        after = text[i + 1] if i + 1 < len(text) else ""
        if not _KANJI_RE.match(before or " ") and not _KANJI_RE.match(after or " "):
            return True
        start = i + 1


class Glossary:
    """In-memory view of ``glossary.json``.

    Holds two kinds of entry in one ordered list:

    * *mapped* — a source term locked to an English spelling (``source`` set).
    * *canonical English name* — a spelling established from already-English chapters
      whose source form is not known yet (``source`` empty). These are injected as a
      "match these spellings" reference so a freshly translated chapter stays
      consistent with the English ones already on disk. When the source form is later
      learned, the mapped entry supersedes the placeholder automatically.
    """

    def __init__(self, entries: list[GlossaryEntry] | None = None):
        self._entries: list[GlossaryEntry] = []
        self._by_source: dict[str, GlossaryEntry] = {}
        for e in entries or []:
            self.add(e)

    # ---- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        path = Path(path)
        if not path.exists():
            return cls([])
        # A read failure and a parse failure are different things — see State.load.
        # Could not READ it: retry, then raise, because a caller that degraded to an
        # empty glossary would save that back over every locked name in the work.
        raw = read_text_retrying(path)
        try:
            data = json.loads(raw) if raw.strip() else []
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            # A corrupt glossary must not break translation or library loading. But
            # the next approval loads this empty list, adds one term and saves it back
            # — so a MOMENTARY read failure would wipe every locked name, and the
            # novel's spellings would drift from the next chapter onward. Keep the
            # bytes so the real glossary is recoverable.
            quarantine_unreadable(path)
            data = []
        if not isinstance(data, list):
            data = []
        return cls([GlossaryEntry.from_dict(d) for d in data if isinstance(d, dict)])

    def save(self, json_path: str | Path, md_path: str | Path | None = None) -> None:
        entries = self.entries()
        # Atomic writes: a crash mid-save must never truncate glossary.json to empty,
        # which would silently wipe every locked term. Write the source-of-truth JSON
        # LAST so a failure leaves the previous good JSON intact.
        if md_path is not None:
            atomic_write_text(Path(md_path), self.to_markdown())
        atomic_write_json(Path(json_path), [asdict(e) for e in entries])

    # ---- access ------------------------------------------------------------
    def entries(self) -> list[GlossaryEntry]:
        return sorted(self._entries, key=lambda e: (e.type, e.english or e.source))

    def __contains__(self, source: str) -> bool:
        return source in self._by_source

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, source: str) -> GlossaryEntry | None:
        """An entry by any of its spellings, not just its primary one."""
        direct = self._by_source.get(source)
        if direct is not None:
            return direct
        for entry in self._entries:
            if source in entry.spellings():
                return entry
        return None

    def relevant_to(self, source_text: str) -> list[GlossaryEntry]:
        """Mapped entries that actually occur in this chapter's source.

        Checks EVERY spelling, which is the whole point: a chapter that writes a name
        only in katakana must still pull in the entry whose primary form is kanji.
        """
        hits = [e for e in self._entries
                if e.source and any(_matches(s, source_text) for s in e.spellings())]
        return sorted(hits, key=lambda e: (e.type, e.source))

    def canonical(self) -> list[GlossaryEntry]:
        """English-only canonical names, for the "match these spellings" block.

        Mapped entries are excluded on purpose: ``relevant_to`` already injects those
        (with their source spellings) whenever they appear, so including them here
        would duplicate them and bloat the prompt. The value of this list is names
        whose source form is not known yet.
        """
        return sorted((e for e in self._entries if not e.source and e.english),
                      key=lambda e: (e.type, e.english))

    # ---- mutation ----------------------------------------------------------
    def _drop_english_only(self, english: str) -> None:
        low = english.lower()
        self._entries = [e for e in self._entries
                         if not (not e.source and e.english.lower() == low)]

    def add(self, entry: GlossaryEntry) -> None:
        """Add or replace an entry.

        Adding a mapped entry removes any English-only placeholder for the same
        spelling: once the source form is known, the mapped entry supersedes it, and
        leaving both would inject the same name twice.
        """
        if not entry.english and not entry.source:
            return
        if entry.source:
            old = self._by_source.get(entry.source)
            if old is not None and old in self._entries:
                self._entries.remove(old)
            if entry.english:
                self._drop_english_only(entry.english)
            self._by_source[entry.source] = entry
        else:
            # An English-only placeholder never displaces a mapped entry.
            if any(e.english.lower() == entry.english.lower() and e.source
                   for e in self._entries):
                return
            self._drop_english_only(entry.english)
        self._entries.append(entry)

    def add_variant(self, source: str, variant: str) -> bool:
        """Record another spelling for an existing entry.

        Returns False when there is no such entry, or when the spelling is already
        known — so a caller can tell "learned something" from "already knew that"
        without comparing lists itself.
        """
        entry = self.get(source)
        variant = (variant or "").strip()
        if entry is None or not variant or variant in entry.spellings():
            return False
        entry.variants.append(variant)
        return True

    def remove(self, source: str) -> bool:
        entry = self.get(source)
        if entry is None:
            return False
        self._entries.remove(entry)
        self._by_source.pop(entry.source, None)
        return True

    # ---- rendering ---------------------------------------------------------
    def to_markdown(self) -> str:
        """A human-readable companion to the JSON. Written for someone checking a
        name at a glance, not for a parser — the JSON is the source of truth."""
        lines = ["# Glossary", ""]
        by_type: dict[str, list[GlossaryEntry]] = {}
        for entry in self.entries():
            by_type.setdefault(entry.type, []).append(entry)
        for type_name in sorted(by_type):
            lines.append(f"## {type_name}")
            lines.append("")
            for entry in by_type[type_name]:
                spellings = entry.spellings()
                head = " / ".join(spellings) if spellings else "(English only)"
                parts = [f"- **{entry.english or '?'}** — {head}"]
                extras = []
                if entry.pronoun:
                    extras.append(entry.pronoun)
                if entry.first_person:
                    extras.append(f"says {entry.first_person}")
                if entry.register:
                    extras.append(entry.register)
                if extras:
                    parts.append(f" ({', '.join(extras)})")
                if entry.note:
                    parts.append(f" — {entry.note}")
                lines.append("".join(parts))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def to_prompt_block(self, source_text: str) -> str:
        """The glossary text injected into one chapter's prompt.

        Only what is relevant to THIS chapter, plus the canonical English names. A
        glossary of four hundred entries would otherwise dominate the prompt and push
        the actual prose out of the model's attention.
        """
        relevant = self.relevant_to(source_text)
        canonical = self.canonical()
        if not relevant and not canonical:
            return ""

        lines: list[str] = []
        if relevant:
            lines.append("Locked terms — use these English spellings exactly:")
            for entry in relevant:
                spellings = " / ".join(entry.spellings())
                line = f"  {spellings} = {entry.english}"
                hints = []
                if entry.pronoun:
                    hints.append(entry.pronoun)
                if entry.first_person:
                    hints.append(f"refers to self as {entry.first_person}")
                if entry.register:
                    hints.append(entry.register)
                if hints:
                    line += f"  [{'; '.join(hints)}]"
                if entry.note:
                    line += f"  ({entry.note})"
                lines.append(line)
        if canonical:
            if lines:
                lines.append("")
            lines.append("Names already established in English — match these spellings:")
            for entry in canonical:
                lines.append(f"  {entry.english}"
                             + (f"  ({entry.note})" if entry.note else ""))
        return "\n".join(lines)


def glossary_lock(path: str | Path):
    """The lock guarding a glossary file.

    Exported so an approval endpoint can hold it across read → modify → write. The
    registry is shared with every other file lock in the process, so a save from the
    pipeline and an approval from a request thread cannot interleave.
    """
    return file_lock(path)


# ---- the pending queue -------------------------------------------------------
# Terms the model proposed, waiting on a human. Nothing here is in effect: the
# glossary is what the prompt reads, and an entry only reaches it by being approved.
# The gate exists because a wrong reading of a name — which Japanese makes easy, since
# most kanji have several — would otherwise propagate silently through every
# subsequent chapter and be expensive to unpick.

def load_pending(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    # Read failure vs parse failure — see State.load.
    raw = read_text_retrying(path)
    try:
        data = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        quarantine_unreadable(path)
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def save_pending(path: str | Path, items: list[dict]) -> None:
    atomic_write_json(Path(path), items)


def merge_pending(existing: list[dict], proposed: list[dict], *,
                  glossary: Glossary | None = None) -> tuple[list[dict], int]:
    """Add newly proposed terms to the queue, skipping what is already known.

    Returns ``(queue, added_count)``. Three things are filtered out, and each would
    otherwise produce a queue nobody wants to read:

    * terms already IN the glossary — they are settled;
    * terms already IN the queue — the same name appears in thirty chapters;
    * a proposal that only adds a spelling of an entry we already have, which is
      recorded as a variant rather than queued as a new term.
    """
    seen = {str(item.get("source", "")).strip() for item in existing}
    seen.discard("")
    out = list(existing)
    added = 0

    for raw in proposed or []:
        if not isinstance(raw, dict):
            continue
        entry = GlossaryEntry.from_dict(raw)
        if not entry.source or entry.source in seen:
            continue
        if glossary is not None:
            known = glossary.get(entry.source)
            if known is not None:
                continue
            # A different spelling of something already locked is a variant, not a
            # new term. Recording it here is what stops the same character being
            # queued once per script they are written in.
            matched = next((e for e in glossary.entries()
                            if e.english and entry.english
                            and e.english.lower() == entry.english.lower()), None)
            if matched is not None:
                glossary.add_variant(matched.source or matched.english, entry.source)
                continue
        record = asdict(entry)
        record["pronoun"] = normalize_pronoun(entry.pronoun)
        out.append(record)
        seen.add(entry.source)
        added += 1

    return out, added
