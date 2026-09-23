"""One manga chapter, end to end: collect its lines, translate them, read the answer.

The manga counterpart of :mod:`morning.pipeline`, and deliberately much smaller,
because the interesting decisions were already made elsewhere:

* **The unit is the chapter, and it is one call.** A 20-page chapter is roughly 150
  lines and a few thousand Japanese characters — comfortably under the chunk threshold
  the prose path already uses — while reading those same 20 pages cost 20 vision calls
  in step 3. So translating is a few percent of a chapter's total spend, and buying
  more context is nearly free.

  That matters because a speech bubble in isolation is frequently untranslatable.
  Japanese omits the subject, and the referent is routinely established on the
  *previous page*: a question asked on page 11 is answered on page 12, and the answer's
  subject appears nowhere in the answer. A per-region call cannot resolve that at all.
  A per-page call cannot resolve it across the page turn — which is exactly where manga
  speech breaks most often. Register is the same problem at chapter scale: a character
  who is 俺-blunt on page 3 and 私-formal on page 17 reads as two people unless one
  call sees both.

* **The voice contract is inherited, not forked.** ``build_system_prompt`` takes a
  swappable ``output_contract`` precisely so a different output shape can keep
  everything above it — the glossary block, the canonical spellings, the
  ``[he]``/``[she]``/``[they]`` pins, the ``[refers to self as 俺]`` register tags, the
  honorific rules, the onomatopoeia rules, the per-project style note. Forking the
  prompt would ship bubbles without the rules that make the tags they are fed mean
  anything, on the text where honorifics and register carry the most weight — and
  every one of those failures is fluent English.

* **Tab-separated output, not JSON.** The opposite of :mod:`morning.ocr`'s choice, for
  the opposite reason. A page read has to interleave coordinates with text and has no
  prose form; a script is a flat list. A truncated JSON array yields nothing, whereas
  every complete record before the cut is usable — and a chapter is a hundred and fifty
  lines the owner already paid to have read.

Nothing here writes a file. The caller persists, exactly as ``process_chapter`` leaves
persistence to the worker that holds the lock.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .pageread import PageRead, Region, TRANSLATED_KINDS, region_hash
from .prompts import (
    MANGA_OUTPUT_CONTRACT, NEW_TERMS_DELIMITER, build_manga_user_message,
    build_system_prompt,
)
from .reading_order import propose_panels
from .translator import chunk_paragraphs

# A fenced block around the whole answer. Stripped by line rather than by regex over the
# body, because a record's English could legitimately contain a backtick.
_FENCE_RE = re.compile(r"^\s*```(?:\w+)?\s*$")


@dataclass(frozen=True)
class ScriptLine:
    """One translatable region, addressed the way the model will address it back."""

    page_seq: int
    region_id: str
    kind: str
    text: str
    source_hash: str
    panel: int = 0          # 1-based within its page; 0 when the page has no geometry

    @property
    def id(self) -> str:
        """``<page seq>:<region id>``.

        The seq prefix is required. Region ids are page-local (``r0``, ``r1``…) and
        repeat on every page, so ``r0`` alone names a hundred different bubbles in a
        volume. ``seq`` is monotonic and never reused — ``add_page`` only increments
        ``next_seq`` and ``delete_pages`` never decrements it — so this is stable for
        the life of the project.
        """
        return f"{self.page_seq}:{self.region_id}"


@dataclass
class LineResult:
    """What came back for one line."""

    english: str = ""
    speaker: str = ""


@dataclass
class ScriptResult:
    lines: dict[str, LineResult] = field(default_factory=dict)
    new_terms: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    chunks: int = 1
    missing: list[str] = field(default_factory=list)


# ---- collecting --------------------------------------------------------------

def collect_lines(reads: list[tuple[int, PageRead]], *,
                  kinds: frozenset[str] | set[str] = TRANSLATED_KINDS
                  ) -> list[ScriptLine]:
    """The chapter's translatable lines, in reading order.

    ``reads`` is ``(page seq, PageRead)`` in page order, and the PageRead is the
    EFFECTIVE one — a human's corrected reading order already applied. This module
    never looks at storage, so whose order won is settled before it is called.

    Panels are renumbered by first appearance rather than kept as the geometry emitted
    them. The two can disagree once a human has reordered a page, and a script whose
    headers ran "panel 2, panel 1, panel 2" would read as an error rather than as the
    reading order it faithfully is.
    """
    out: list[ScriptLine] = []
    for seq, read in reads:
        geometric = propose_panels(read.regions)
        panel_of = {rid: i for i, panel in enumerate(geometric) for rid in panel}
        seen: dict[int, int] = {}
        for region in read.in_order():
            if region.kind not in kinds:
                continue
            text = (region.text or "").strip()
            if not text:
                continue
            group = panel_of.get(region.id)
            if group is None:
                number = 0
            else:
                number = seen.setdefault(group, len(seen) + 1)
            out.append(ScriptLine(
                page_seq=seq, region_id=region.id, kind=region.kind, text=text,
                source_hash=region_hash(region), panel=number))
    return out


def page_blocks(lines: list[ScriptLine]) -> list[tuple[str, list[str]]]:
    """One block per page, each paired with the line ids inside it.

    Blocks rather than individual lines because a block is where a chunk boundary
    belongs. Splitting a chapter mid-page would hand the model half a conversation and
    separate a page header from the lines under it.

    The ids are carried alongside rather than recovered from the rendered text later.
    Searching the block for ``"1:r0\\t"`` would also match ``"11:r0\\t"``, so a chapter
    long enough to chunk would silently tell one call it was responsible for another
    call's lines — and those lines would then be reported missing.
    """
    blocks: list[tuple[str, list[str]]] = []
    current_seq: int | None = None
    current_panel: int | None = None
    parts: list[str] = []
    ids: list[str] = []

    for line in lines:
        if line.page_seq != current_seq:
            if parts:
                blocks.append(("\n".join(parts), ids))
            parts, ids = [], []
            current_seq, current_panel = line.page_seq, None
        if line.panel != current_panel:
            current_panel = line.panel
            parts.append(f"Page {line.page_seq}" if not current_panel
                         else f"Page {line.page_seq} — panel {current_panel}")
        parts.append(f"{line.id}\t[{line.kind}]\t{line.text}")
        ids.append(line.id)

    if parts:
        blocks.append(("\n".join(parts), ids))
    return blocks


def render_pages(lines: list[ScriptLine]) -> list[str]:
    """Just the rendered blocks — what the model is shown."""
    return [block for block, _ in page_blocks(lines)]


def continuity_tail(lines: list[ScriptLine], produced: dict[str, LineResult],
                    count: int) -> str:
    """The end of what has been translated so far, as context for the next chunk.

    Japanese with its English beside it, because the English is what tells the model
    which voice it has already given a character — the whole reason register survives a
    chunk boundary.
    """
    # `lines[-0:]` is `lines[0:]` — the WHOLE chapter. Asking for no continuity has to
    # return none of it, not all of it, or setting `continuity_paragraphs` to 0 would
    # quietly re-send every line as context on every chunk.
    count = max(0, count)
    if not count:
        return ""
    out: list[str] = []
    for line in lines[-count:]:
        result = produced.get(line.id)
        english = (result.english if result else "").strip()
        out.append(f"{line.text}\t->\t{english}" if english else line.text)
    return "\n".join(out)


# ---- reading the answer ------------------------------------------------------

def _terms_from(tail: str, warnings: list[str]) -> list[dict]:
    start, end = tail.find("["), tail.rfind("]")
    if start == -1 or end <= start:
        warnings.append("the new-terms block held no JSON array")
        return []
    try:
        parsed = json.loads(tail[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        warnings.append("could not parse the new-terms JSON block")
        return []
    if not isinstance(parsed, list):
        warnings.append("the new-terms block was not a JSON array")
        return []
    return [d for d in parsed if isinstance(d, dict)]


def parse_script_response(raw: str, expected_ids: list[str]
                          ) -> tuple[dict[str, LineResult], list[dict], list[str]]:
    """Turn the model's answer into per-line English, tolerantly.

    Every record that parsed is kept, whatever else went wrong. A chapter is a hundred
    and fifty lines of work the owner has already paid to have read, and throwing all
    of it away because the model added a preamble or dropped two records would be the
    expensive mistake.

    Deliberately NOT run through ``sanitize.strip_meta``, unlike the prose path. That
    function reasons about paragraphs of prose; a tab-separated record is not one, and
    a bubble consisting of a single short exclamation is exactly the shape its
    commentary heuristics look for. Chatter needs no stripping here anyway — a line
    with no tab, or with an id this chapter does not contain, is dropped by the parse
    itself, which is a stricter filter than any heuristic.
    """
    warnings: list[str] = []
    text = raw or ""

    body, found, tail = text.partition(NEW_TERMS_DELIMITER)
    if not found:
        warnings.append(f"response had no {NEW_TERMS_DELIMITER} block")
        tail = ""

    wanted = set(expected_ids)
    lines: dict[str, LineResult] = {}
    unexpected: list[str] = []
    duplicates: list[str] = []
    ignored = 0

    for row in body.splitlines():
        if not row.strip() or _FENCE_RE.match(row):
            continue
        if "\t" not in row:
            ignored += 1
            continue
        parts = row.split("\t")
        line_id = parts[0].strip()
        if len(parts) >= 3:
            speaker = parts[1].strip()
            english = "\t".join(parts[2:]).strip()
        else:
            speaker, english = "", parts[1].strip()

        if line_id not in wanted:
            unexpected.append(line_id)
            continue
        if not english:
            # A record with an empty English field is not a delivered line. Counting it
            # as one marked the chapter "ok" with bubbles still in Japanese, and the
            # default sweep — which skips finished chapters — then never went back for
            # them. Treated as missing, which is what it is.
            continue
        if line_id in lines:
            # Keep the first. A repeated record is far more often a duplicate than a
            # correction, and silently preferring the later one would let a stray echo
            # overwrite the real translation.
            duplicates.append(line_id)
            continue
        lines[line_id] = LineResult(english=english, speaker=speaker)

    missing = [i for i in expected_ids if i not in lines]
    if missing:
        warnings.append(f"{len(missing)} line(s) did not come back and are still "
                        f"in Japanese")
    if unexpected:
        warnings.append(f"{len(unexpected)} record(s) named a line that is not in "
                        f"this chapter and were ignored")
    if duplicates:
        warnings.append(f"{len(duplicates)} line(s) came back twice; the first was "
                        f"kept")
    if ignored:
        warnings.append(f"{ignored} line(s) of the answer were not records and were "
                        f"ignored")

    return lines, _terms_from(tail, warnings) if found else [], warnings


# ---- the call ----------------------------------------------------------------

def _accumulate(total: dict, part: dict) -> None:
    for key, value in (part or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def translate_script(translator, lines: list[ScriptLine], *, glossary_block: str = "",
                     title: str = "", hooks=None, retry_hint: str = "") -> ScriptResult:
    """Translate one chapter's script. Blocking — always called via a threadpool.

    Chunks only when a chapter is genuinely oversized, through the same
    ``chunk_paragraphs`` the prose path uses, with the same continuity discipline. For
    an ordinary chapter this is one call.
    """
    result = ScriptResult()
    if not lines:
        return result

    system_text = build_system_prompt(
        style_note=translator.tcfg.style_note,
        honorific_note=translator.tcfg.honorific_note,
        glossary_block=glossary_block,
        output_contract=MANGA_OUTPUT_CONTRACT,
    )

    by_id = {line.id: line for line in lines}
    blocks = page_blocks(lines)
    chunks = chunk_paragraphs([block for block, _ in blocks],
                              translator.tcfg.chunk_threshold)
    result.chunks = len(chunks)

    if hooks is not None:
        hooks.source([line.text for line in lines])

    done: list[ScriptLine] = []
    cursor = 0
    for index, block_group in enumerate(chunks, start=1):
        if hooks is not None:
            hooks.chunk(index, len(chunks))

        # `chunk_paragraphs` keeps the blocks in order and drops none, so a chunk is a
        # contiguous run and the ids come straight off the same slice.
        wanted = [line_id for _, group in blocks[cursor:cursor + len(block_group)]
                  for line_id in group]
        cursor += len(block_group)
        user_text = build_manga_user_message(
            block_group,
            title=title,
            chunk_index=index,
            chunk_total=len(chunks),
            previous_tail=continuity_tail(
                done, result.lines, translator.tcfg.continuity_paragraphs),
            extra_instruction=translator.tcfg.extra_instruction,
        )
        if retry_hint:
            user_text += retry_hint

        raw, usage, cost = translator._call(system_text, user_text, hooks=hooks)
        produced, terms, warnings = parse_script_response(raw, wanted)

        result.lines.update(produced)
        result.new_terms.extend(terms)
        result.warnings.extend(
            (f"part {index}/{len(chunks)}: {w}" if len(chunks) > 1 else w)
            for w in warnings)
        _accumulate(result.usage, usage)
        result.cost_usd += cost
        done.extend(by_id[i] for i in wanted if i in by_id)

    result.cost_usd = round(result.cost_usd, 6)
    result.missing = [line.id for line in lines if line.id not in result.lines]
    return result


def should_retry(lines: list[ScriptLine], result: ScriptResult, *,
                 miss_fraction: float = 0.2) -> bool:
    """Whether this chapter is worth paying for a second time.

    Recovering two missing lines out of a hundred and fifty by re-billing the whole
    chapter is a worse trade than showing those two bubbles in Japanese and letting the
    owner ask for them — the same reasoning ``pipeline.py`` uses for capping the prose
    retry at one and for refusing a second model call to re-judge the first.

    So: only when the answer produced nothing at all, or when a substantial share of
    the chapter failed to come back.
    """
    if not lines:
        return False
    if not result.lines:
        return True
    return len(result.missing) / len(lines) > miss_fraction


__all__ = [
    "LineResult", "ScriptLine", "ScriptResult", "collect_lines", "continuity_tail",
    "parse_script_response", "render_pages", "should_retry", "translate_script",
]
