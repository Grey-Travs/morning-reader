"""Build chapters from pasted text or an uploaded ``.txt``.

The cheapest ingestion path and the one step 1 is proved with: no Google account, no
site export, no images. It produces ordinary :class:`~morning.chapters.Chapter`
objects, so everything downstream works on it unchanged — which is also what makes the
other three ingestion paths independent work later. Same output contract, no shared
files.

Splitting modes:

* ``separator`` — a chapter break is a line that is only a marker (``---`` by default;
  ``===``/``***``/``###``/``___`` runs are accepted too).
* ``heading``   — a chapter break is a line that looks like a chapter heading
  (``第3話``, ``3話``, ``第三章``, ``プロローグ``, ``Chapter 3`` ...); that line becomes
  the title.
* ``single``    — the whole text is one chapter.
"""

from __future__ import annotations

import re

from .chapters import Chapter, strip_invisibles

# A paragraph break is a blank line (possibly carrying stray whitespace). Japanese
# web-novel text often indents with a full-width space, so the whitespace class has to
# be the Unicode one, not [ \t].
SEP_RE = re.compile(r"\n\s*\n+")

# Kanji numerals, for headings written 第三話 rather than 第3話. Matching them is all
# that is needed here — this module decides WHERE a chapter starts, not what number it
# is. Reading the number back out (for the chapter-mapping logic) is arithmetic over
# (position, number) pairs and belongs with the pipeline.
_KANJI_DIGITS = "〇一二三四五六七八九十百千"

# A line that introduces a chapter, in Japanese or English convention.
#
# The Japanese counters are the direct analogue of Night Reader's Korean ones, and
# three of the four are the SAME CHARACTERS: 章/巻/部 are 장/권/부. Only 話 differs from
# 화 in script, because Korean writes that one counter in Hangul. Everything that
# consumes a heading downstream is arithmetic over positions and numbers, so it
# transfers untouched.
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:chapter|ch\.?|episode|ep\.?|part|vol\.?|volume)\s*\d+"   # Chapter 3 / Ep. 3
    rf"|第?\s*[0-9{_KANJI_DIGITS}]{{1,6}}\s*[話章巻部回]"  # 第3話 / 3話 / 第三章 / 5回
    r"|プロローグ|エピローグ"              # プロローグ / エピローグ
    r"|序章|終章|外伝|間章|閉幕"           # 序章 / 終章 / 外伝 / 間章 / 閉幕
    r"|prologue|epilogue|interlude"
    r")\s*[:：.\-–「]?\s*.*$",
    re.IGNORECASE,
)

# A line that ends the way a sentence ends. Titles do not: "第1話　朝の駅" and
# "Chapter 3" carry no terminal stop, while "電車はまだ来ない。" and "She waited." do.
_SENTENCE_END_RE = re.compile(r"[。．！？.!?]\s*$")

# A separator line: a run of the same marker char, or the user's own marker.
_DEFAULT_SEP_RE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,}|#{3,}|_{3,})\s*$")

# How long a first line may be and still be taken as a title rather than prose. A
# Japanese line packs far more into the same character count than an English one, so
# the generous 40-char "short line is probably a title" fallback Night Reader uses
# would swallow a real opening sentence. Japanese titles are short; prose is not.
_MAX_TITLE_CHARS = 60
_MAX_SHORT_TITLE_CHARS = 24


def _paragraphs(block: str) -> list[str]:
    """Split a chapter block into paragraphs on blank lines, dropping empties."""
    block = strip_invisibles(block)
    return [p for p in (part.strip() for part in SEP_RE.split(block)) if p]


def _first_line_title(block: str, fallback: str) -> tuple[str, str]:
    """If the block's first line is a heading-like line, use it as the title and drop
    it from the body. Returns ``(title, body)``."""
    lines = block.lstrip("\n").split("\n", 1)
    head = lines[0].strip()
    rest = lines[1] if len(lines) > 1 else ""
    # A block that is ONLY its first line is prose, not a title with nothing under it.
    # Consuming it leaves an empty body, `_make` drops chapters with no paragraphs,
    # and the one-line chapter disappears without a word — after which the caller's
    # "no chapters found" fallback rebuilds the whole text as a single chapter with
    # the separator lines still in it. Found by tests/test_textsource.py.
    if not rest.strip():
        return fallback, block
    if not head or len(head) > _MAX_TITLE_CHARS:
        return fallback, block
    # An explicit chapter marker is a title however it is punctuated.
    if _HEADING_RE.match(head):
        return head, rest
    # A line that ENDS LIKE A SENTENCE is prose, however short it is. Without this,
    # any chapter opening with a brief sentence loses that sentence into its title —
    # silent data loss, and the shorter and more striking the opening line, the more
    # likely it is to be eaten. Japanese dialogue is safe either way because it closes
    # with 」 rather than with the question mark inside it.
    if _SENTENCE_END_RE.search(head):
        return fallback, block
    if len(head) <= _MAX_SHORT_TITLE_CHARS:
        return head, rest
    return fallback, block


def _make(index: int, title: str, body: str) -> Chapter | None:
    paragraphs = _paragraphs(body)
    if not paragraphs:
        return None
    return Chapter(index=index, title=title.strip() or f"Chapter {index}",
                   paragraphs=paragraphs)


def looks_like_heading(line: str) -> bool:
    """Whether one line reads as a chapter heading. Exported because the page-build
    step (step 3) needs the same judgement about a page's first line."""
    return bool(line.strip()) and bool(_HEADING_RE.match(line))


def split_text_into_chapters(text: str, mode: str = "separator",
                             separator: str = "---") -> list[Chapter]:
    """Parse raw text into ordered chapters. Indices are 1-based, in document order."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    if mode == "single":
        chapter = _make(1, "Chapter 1", text)
        return [chapter] if chapter else []

    if mode == "heading":
        return _split_on_headings(text)

    return _split_on_separators(text, separator)


def _split_on_headings(text: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    buf: list[str] = []
    title: str | None = None

    def flush() -> None:
        nonlocal buf, title
        # Text before the first heading is a preamble, not a chapter. Dropping it only
        # when it is blank means an author's note at the top of the file is kept
        # rather than silently deleted.
        if title is None and not any(s.strip() for s in buf):
            buf = []
            return
        chapter = _make(len(chapters) + 1,
                        title or f"Chapter {len(chapters) + 1}", "\n".join(buf))
        if chapter:
            chapters.append(chapter)
        buf = []

    for line in text.split("\n"):
        if looks_like_heading(line):
            flush()
            title = line.strip()
        else:
            buf.append(line)
    flush()

    if chapters:
        return _reindex(chapters)
    # No headings found — one chapter beats returning nothing, which would read to the
    # user as "the paste failed".
    chapter = _make(1, "Chapter 1", text)
    return [chapter] if chapter else []


def _split_on_separators(text: str, separator: str) -> list[Chapter]:
    marker = (separator or "").strip()
    custom_re = re.compile(r"^\s*" + re.escape(marker) + r"\s*$") if marker else None

    def is_separator(line: str) -> bool:
        if custom_re and custom_re.match(line):
            return True
        return bool(_DEFAULT_SEP_RE.match(line))

    blocks: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if is_separator(line):
            blocks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    blocks.append("\n".join(current))

    chapters: list[Chapter] = []
    for block in blocks:
        if not block.strip():
            continue
        title, body = _first_line_title(block, f"Chapter {len(chapters) + 1}")
        chapter = _make(len(chapters) + 1, title, body)
        if chapter:
            chapters.append(chapter)
    if not chapters:  # no separators present -> a single chapter
        chapter = _make(1, "Chapter 1", text)
        return [chapter] if chapter else []
    return _reindex(chapters)


def _reindex(chapters: list[Chapter]) -> list[Chapter]:
    for i, chapter in enumerate(chapters, start=1):
        chapter.index = i
    return chapters


def make_chapter(index: int, title: str, body: str) -> Chapter | None:
    """One chapter from a title and a body, or None when the body has no prose."""
    return _make(index, title, body)


# Half-width Katakana. Legitimate in old exports, but *dominant* only when a
# decode has gone wrong — it is the signature of EUC-JP bytes read as CP932.
_HALFWIDTH_KANA_RE = re.compile("[｡-ﾟ]")

# What plausible Japanese plain text is made of: the Japanese scripts, CJK
# punctuation (、。「」〜 and friends), ASCII, and ordinary whitespace.
_PLAUSIBLE_RE = re.compile(
    r"[　-〿ぁ-ゟ゠-ヿㇰ-ㇿ"
    r"㐀-䶿一-鿿豈-﫿！-～"
    r"\x00-\x7f\s]"
)

# Encodings Japanese plain text actually arrives in, after UTF-8:
#   * CP932 — the Windows Japanese codepage, still the default for Notepad-era .txt
#     files. The superset of Shift_JIS, so it is tried rather than bare shift_jis.
#   * EUC-JP — older Unix tooling and some site exports.
_LEGACY_ENCODINGS = ("cp932", "euc_jp")


def _japanese_plausibility(text: str) -> float:
    """How much this decoding looks like real Japanese text, from 0 to 1.

    Used to CHOOSE between candidate decodings rather than to judge any one of them.
    The half-width penalty is what does the work: EUC-JP bytes misread as CP932
    decode without raising, into a string that is almost entirely half-width Katakana.
    """
    if not text:
        return 0.0
    total = len(text)
    plausible = len(_PLAUSIBLE_RE.findall(text)) / total
    halfwidth = len(_HALFWIDTH_KANA_RE.findall(text)) / total
    return plausible - 2.0 * halfwidth


def decode_upload(data: bytes) -> str:
    """Decode an uploaded .txt, choosing between the encodings Japanese arrives in.

    **Not "the first encoding that does not raise".** That is the obvious
    implementation and it is wrong, because CP932 and EUC-JP have heavily overlapping
    byte validity: a perfectly ordinary EUC-JP sentence is also *valid* CP932, so
    trying CP932 first silently returns mojibake and never looks at EUC-JP. Pinned by
    ``tests/test_textsource.py::test_euc_jp_is_decoded``, which fails against the
    first-that-works version.

    So: UTF-8 wins outright when it decodes, because UTF-8 is self-validating and a
    false positive across a whole file is vanishingly unlikely. Otherwise every legacy
    candidate is decoded and the most plausible-looking result wins.

    The final ``errors="replace"`` means a file in none of them still lands as text a
    human can look at and fix, rather than a 500.
    """
    if data.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM, written by Excel and Notepad
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    best: tuple[float, str] | None = None
    for encoding in _LEGACY_ENCODINGS:
        try:
            candidate = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        score = _japanese_plausibility(candidate)
        if best is None or score > best[0]:
            best = (score, candidate)
    if best is not None:
        return best[1]
    return data.decode("utf-8", errors="replace")
