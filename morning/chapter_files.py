"""Where a translated chapter lives on disk, and how it gets there safely.

Small module, and almost all of it is about one failure that is worth stating plainly
because it cost a paid-for translation in the app this one is derived from.

**The filename depends on the chapter COUNT, so it moves.** A novel of 99 chapters
writes ``chapter-07.md``; the moment it reaches 100 the canonical name becomes
``chapter-007.md``. Anything holding a count captured earlier — a worker keeps one for
its whole run — then writes at the old width, and you get ``chapter-50.md`` sitting
beside an already-re-padded ``chapter-050.md``: two files for one chapter. The reader
showed one of them, ``previous/`` had snapshotted neither (the canonical name did not
exist when the snapshot was attempted, so it was skipped), and the padding normaliser
then refused to reconcile them because its destination was taken. The result was a
translation the user had paid for, marked finished in state.json, that could be
neither read nor recovered.

Every lookup here therefore resolves by **index**, never by the current count. The
count only decides what a NEW file is called.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .atomic import atomic_write_text
from .chapters import Chapter
from .locks import file_lock

# Two digits minimum, so a short work still sorts correctly in a file listing.
_MIN_PAD = 2

_CHAPTER_RE = re.compile(r"^chapter-(\d+)$")

# The audit file records source and translation under fixed headings. It is the ONLY
# place a needs-review chapter's translation is written — such a chapter deliberately
# never reaches chapters/ — so this heading is how that prose is recovered for display
# and for the accept path. Changing it orphans every needs-review chapter on disk.
AUDIT_SOURCE_HEADING = "## Source"
AUDIT_TRANSLATION_HEADING = "## Translation (English)"


def pad_width(total: int) -> int:
    return max(_MIN_PAD, len(str(max(0, total))))


def chapter_filename(index: int, total: int) -> str:
    """The canonical name for a chapter, at the width this work's size implies."""
    return f"chapter-{index:0{pad_width(total)}d}.md"


def existing_chapter_file(directory: Path, index: int) -> Path | None:
    """A file holding this chapter at ANY pad width, or None.

    Resolving by index is what makes a read independent of the count, and that is what
    stops a chapter the user already paid for from silently disappearing when the work
    crosses a digit boundary.
    """
    try:
        candidates = sorted(directory.glob("chapter-*.md"))
    except OSError:
        return None
    for path in candidates:
        match = _CHAPTER_RE.match(path.stem)
        if match and int(match.group(1)) == index:
            return path
    return None


def chapter_path(directory: Path, index: int, total: int) -> Path:
    """Where chapter ``index`` lives.

    The canonical name wins when it exists; a file written at any other width is
    honoured next; otherwise the canonical name is returned so a caller can create it.
    """
    canonical = directory / chapter_filename(index, total)
    if canonical.exists():
        return canonical
    return existing_chapter_file(directory, index) or canonical


def read_chapter(directory: Path, index: int, total: int) -> str | None:
    path = chapter_path(directory, index, total)
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError):
        return None


def write_chapter_file(output_dir: Path, index: int, total: int, prose: str,
                       *, snapshot: bool = True) -> Path:
    """Write a chapter's English, atomically and under a lock.

    The lock is taken HERE rather than at each call site. There will be several —
    translate, repair, a manual save, accept — and one that forgets silently loses a
    translation: two writers each read, each apply their own change, and the last
    write wins. The lock is re-entrant, so a caller doing read-modify-write can hold it
    across all three steps and still call in here.

    Renaming an existing file to the current pad width is deliberately NOT done here.
    That belongs to a normaliser that knows the real count; doing it from a writer that
    may be holding a stale count would drag the file back to an older name.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = chapter_path(output_dir, index, total)
    with file_lock(path):
        return _write_chapter_locked(path, output_dir, index, total, prose, snapshot)


def _write_chapter_locked(path: Path, output_dir: Path, index: int, total: int,
                          prose: str, snapshot: bool) -> Path:
    # Before overwriting an existing translation, snapshot it to a sibling
    # ``previous/`` folder so the reader can show old-vs-new and offer a one-click
    # revert. Kept OUTSIDE chapters/ so it never matches the chapter-*.md globs used
    # by builds and exports.
    #
    # ``snapshot=False`` is for edits that keep their OWN history — a per-paragraph
    # rewrite stores every version separately, and letting each pick overwrite the
    # single ``previous/`` slot would destroy the whole-chapter snapshot taken before
    # the last translation after just one click.
    if snapshot and path.exists():
        previous_dir = output_dir.parent / "previous"
        previous_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, previous_dir / path.name)
        except OSError:
            pass  # a missing backup must never block writing the real translation
    # Curly quotes are preserved — no smart-quote normalisation anywhere, because the
    # prompt asks for them specifically. Written atomically so a crash or a concurrent
    # reader never observes a half-written chapter.
    atomic_write_text(path, prose.rstrip() + "\n")
    return path


def previous_chapter_path(output_dir: Path, index: int, total: int) -> Path:
    """The retained prior translation, in the sibling ``previous/`` folder.

    Resolved by index: a snapshot taken while the work had a different chapter count
    carries THAT count's pad width, and looking for today's width would report "no
    previous version" for a snapshot sitting right there.
    """
    return chapter_path(output_dir.parent / "previous", index, total)


def has_previous(output_dir: Path, index: int, total: int) -> bool:
    return previous_chapter_path(output_dir, index, total).exists()


# ---- the audit trail ---------------------------------------------------------

def write_audit(audit_dir: Path, index: int, total: int, *, chapter: Chapter,
                english: str, notes: list[str] | None = None) -> Path:
    """Record what was sent and what came back, side by side.

    For a validated chapter this is a record. For a needs-review chapter it is the
    ONLY copy of the translation — such a chapter never reaches chapters/, precisely
    so that a questionable translation cannot be mistaken for a finished one, which
    means this file is what the review screen reads and what Accept promotes.
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / chapter_filename(index, total)
    parts = [f"# {chapter.title}", ""]
    if notes:
        parts.append("## Notes")
        parts.extend(f"- {note}" for note in notes)
        parts.append("")
    parts += [AUDIT_SOURCE_HEADING, "", chapter.text, "",
              AUDIT_TRANSLATION_HEADING, "", english.rstrip(), ""]
    with file_lock(path):
        atomic_write_text(path, "\n".join(parts))
    return path


def read_audit_translation(audit_dir: Path, index: int, total: int) -> str | None:
    """Recover a chapter's translated prose from its audit copy.

    By index, not by count — the audit was written at whatever pad width was current
    at the time, and for a needs-review chapter this is the only copy there is. The
    file has a fixed shape, so the prose is everything after the known heading.
    """
    path = chapter_path(audit_dir, index, total)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    marker = text.find(AUDIT_TRANSLATION_HEADING)
    if marker == -1:
        return None
    body = text[marker + len(AUDIT_TRANSLATION_HEADING):].strip()
    return body or None


def output_total(output_dir: Path, chapter_count: int) -> int:
    """The count the pad width should be derived from.

    Normally the work's chapter count. But a folder can hold files written when the
    work was LARGER — chapters deleted from the source, or a restored backup — and
    narrowing the width while those exist would create a second file for a chapter
    that already has one. Take whichever is greater.
    """
    widest = 0
    try:
        for path in output_dir.glob("chapter-*.md"):
            match = _CHAPTER_RE.match(path.stem)
            if match:
                widest = max(widest, int(match.group(1)))
    except OSError:
        pass
    return max(chapter_count, widest)
