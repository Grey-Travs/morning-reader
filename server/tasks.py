"""What a queued item asks the worker to do, and the code that does it.

Everything the user can start runs through this registry and therefore through the one
Job — which is what puts it in the Activity view, streams it to the live console, and
gives it Stop and the rate-limit auto-resume for free, without any of that being
reimplemented per operation.

A task is a plain blocking function taking ``(chapter, context)``. It is always called
via ``run_in_threadpool``, so it may do file I/O and long model calls, and it must NOT
touch asyncio. It reports progress and learns about a stop through the handles on its
context.

Step 3 adds the page tasks. They plug in here and inherit the whole spine.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from morning.chapters import KIND_EMPTY, KIND_ENGLISH, Chapter, classify
from morning.config import Config
from morning.exceptions import TaskAborted, TaskRefused
from morning.glossary import Glossary
from morning.pipeline import process_chapter, queue_new_terms
from morning.state import (
    STATUS_EMPTY, STATUS_ENGLISH, STATUS_PREPARED, State,
)
from morning.translator import RETRY_REMINDER, StreamHooks, Translator
from morning import manga, ocr

from . import pages

# ---- task kinds --------------------------------------------------------------
TASK_PREPARE = "prepare"      # segment, measure and classify one chapter — no model call
TASK_TRANSLATE = "translate"  # translate, validate, retry, record
TASK_READ_PAGE = "read-page"  # read one scanned page into regions
TASK_TRANSLATE_SCRIPT = "translate-script"  # one manga chapter's whole script
TASK_KINDS = (TASK_PREPARE, TASK_TRANSLATE, TASK_READ_PAGE, TASK_TRANSLATE_SCRIPT)

# Kinds that COST MONEY. The UI warns before starting one of these, and the terminal
# reports the model and effort that will be used. Kept as a list rather than inferred
# so a future free task cannot accidentally inherit the warning, or a paid one escape it.
BILLED_TASK_KINDS = (TASK_TRANSLATE, TASK_READ_PAGE, TASK_TRANSLATE_SCRIPT)

# Kinds whose index is a PAGE sequence number rather than a chapter index.
#
# Pages and chapters share one worker but NOT one number space: once a scanned work
# has been built, page 5 and chapter 5 both exist and are different things. The dedup
# key is namespaced against exactly this, and Night Reader shipped the bug first — a
# duplicate guard keyed on the bare index made queueing a page silently drop a chapter.
PAGE_TASK_KINDS: tuple[str, ...] = (TASK_READ_PAGE,)

# Kinds whose index is a MANGA CHAPTER number — a third number space again, because a
# manga chapter is a run of pages recorded in pages.json and has nothing to do with a
# prose chapter in state.json or with a page seq.
SCRIPT_TASK_KINDS: tuple[str, ...] = (TASK_TRANSLATE_SCRIPT,)

# Everything whose bookkeeping lives in pages.json rather than state.json. This is the
# set that matters when a job is cancelled: work abandoned on EITHER axis has to be put
# back, and a release that knows only about pages leaves manga chapters reading
# "translating" forever with no job running.
MANIFEST_TASK_KINDS: tuple[str, ...] = PAGE_TASK_KINDS + SCRIPT_TASK_KINDS

# How each kind is described in the UI and the terminal.
TASK_LABEL = {
    TASK_PREPARE: "Preparing",
    TASK_TRANSLATE: "Translating",
    TASK_READ_PAGE: "Reading page",
    TASK_TRANSLATE_SCRIPT: "Translating chapter",
}


def describe(kind: str, index: int) -> str:
    return f"{TASK_LABEL.get(kind, 'Working on')} {index}"


def queue_key(index: int, kind: str) -> str:
    """Dedup key for the pending set.

    Namespaced by number space AND by kind: two different things asked of the same
    chapter should both be able to queue, but the same page and the same chapter
    number must never collide. ``queue_state()`` still reports plain integer indices,
    so the Activity views and /api/queue are unaffected.
    """
    space = ("mg" if kind in SCRIPT_TASK_KINDS
             else "pg" if kind in PAGE_TASK_KINDS
             else "ch")
    return f"{space}:{index}:{kind}"


# ---- the handles a task gets --------------------------------------------------

@dataclass
class Progress:
    """How a task reports progress and learns it should stop.

    Runs on a WORKER THREAD. ``on_progress`` marshals to the event loop itself (see
    ``jobs._build_progress``); a task must not assume anything about where it lands
    and must not block in it.
    """

    on_progress: Callable[[int, int], None] | None = None
    abort: threading.Event | None = None

    def aborted(self) -> bool:
        return self.abort is not None and self.abort.is_set()

    def check(self) -> None:
        """Raise if the user pressed Stop.

        Cooperative, because a threadpool thread cannot be killed from outside. A task
        that never calls this simply runs to completion, which is correct but means
        Stop appears to do nothing — so call it at every natural boundary.
        """
        if self.aborted():
            raise TaskAborted("stopped")

    def step(self, done: int, total: int) -> None:
        """Report progress and check for a stop in one call, since every loop wants
        both and separating them is how one gets forgotten."""
        self.check()
        if self.on_progress is not None:
            try:
                self.on_progress(done, total)
            except Exception:  # noqa: BLE001 — a display hook never fails a task
                pass


@dataclass
class TaskContext:
    """Everything a task needs that is not the chapter itself.

    Built once per job rather than per item, because a Translator spawns a CLI process
    and a Glossary reads a file — doing either per chapter would add a process launch
    and a disk read to every item in a sweep.

    ``state`` is the worker's in-memory copy. A task may mutate it; the WORKER owns
    saving it, because the worker is the one holding the lock and merging against
    concurrent edits to other chapters.
    """

    cfg: Config
    state: State
    total: int
    # Needed by the page tasks, whose state lives in the project's own manifest
    # rather than in state.json — see server/pages.py for why the two are separate.
    pid: str = ""
    glossary: Glossary = field(default_factory=Glossary)
    translator: Translator | None = None
    progress: Progress = field(default_factory=Progress)
    hooks: StreamHooks = field(default_factory=StreamHooks)


@dataclass
class TaskResult:
    """What a task hands back for the worker to persist.

    ``fields`` is merged into the item's state record. ``usage``/``cost_usd`` are
    accumulated additively rather than assigned, because one chapter can be worked on
    several times and the project's total is all of it.
    """

    status: str
    fields: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    units: int = 0                # how much work this turned out to be
    # Already written to state by the task itself, so the worker must not write it
    # again — a second add_usage would double-count the spend.
    state_written: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- prepare -----------------------------------------------------------------

def prepare_chapter(chapter: Chapter, ctx: TaskContext) -> TaskResult:
    """Segment, measure and classify one chapter, and record what it is.

    The first thing that happens to a chapter, and the precursor to translating it. It
    makes no model call and costs nothing — which is why it can exercise every branch
    of the worker (skip-as-done, stop, refuse, fail, succeed) without spending a token
    or depending on a network.

    What it records is genuinely needed downstream: ``source_hash`` is the resumability
    key for every later task, and the classification decides whether this chapter is
    translated at all.
    """
    metrics = chapter.metrics
    kind = classify(chapter, ctx.cfg.translation.min_source_fraction)

    # Walk the paragraphs rather than reading the metrics and returning. Two reasons,
    # both load-bearing: it gives Stop somewhere to take effect on a long chapter, and
    # it gives the progress bar real units instead of a fake 0->100.
    total = max(1, len(chapter.paragraphs))
    longest = 0
    for i, paragraph in enumerate(chapter.paragraphs, start=1):
        longest = max(longest, len(paragraph))
        ctx.progress.step(i, total)

    if kind == KIND_EMPTY:
        # Not a failure: a blank placeholder is an ordinary thing to find in a source
        # document, and marking it failed would put it in the user's face forever.
        status = STATUS_EMPTY
    elif kind == KIND_ENGLISH:
        status = STATUS_ENGLISH
    else:
        status = STATUS_PREPARED

    return TaskResult(
        status=status,
        fields={
            "title": chapter.title,
            "source_hash": metrics.content_hash,
            "paragraph_count": metrics.paragraph_count,
            "dialogue_count": metrics.dialogue_count,
            "char_count": metrics.char_count,
            # The persisted language measure. Named for its role, never for a
            # language — see morning/japanese.py.
            "source_fraction": metrics.source_fraction,
            "longest_paragraph": longest,
            "class": kind,
            "error": None,
        },
        units=total,
    )


# ---- translate ---------------------------------------------------------------

def translate_chapter(chapter: Chapter, ctx: TaskContext) -> TaskResult:
    """Translate one chapter. The only task here that spends money.

    All of the decisions live in :func:`morning.pipeline.process_chapter` — where the
    prose lands, when to retry, what to queue — because those are engine concerns and
    the CLI will want them too. This is the thin adapter that hands it the worker's
    context and turns its outcome back into a TaskResult.
    """
    if ctx.translator is None:
        # Refused rather than failed: nothing was written and nothing broke.
        raise TaskRefused("the translation engine is not available")

    ctx.progress.check()
    outcome = process_chapter(chapter, ctx.total, ctx.translator, ctx.glossary,
                              ctx.cfg, ctx.state, hooks=ctx.hooks)
    return TaskResult(
        status=outcome.status,
        usage=outcome.usage,
        cost_usd=outcome.cost_usd,
        units=max(1, len(chapter.paragraphs)),
        # process_chapter has already written the record and the spend.
        state_written=True,
        failures=outcome.failures,
        warnings=outcome.warnings,
    )


# ---- reading a page ----------------------------------------------------------

def read_page_task(page: dict, ctx: TaskContext) -> dict:
    """Read one scanned page into regions.

    Returns the fields to merge into the page's record in ``pages.json`` — NOT a
    TaskResult, because a page's bookkeeping lives in its own manifest. state.json is
    keyed by chapter index, and a page sequence number written there would corrupt a
    real chapter's totals.
    """
    if ctx.translator is None:
        raise TaskRefused("the page reader is not available")

    image = pages.resolve_page_file(ctx.pid, str(page.get("id") or ""))
    if image is None:
        # Refused, not failed: the manifest and the folder disagree, which is a
        # storage problem rather than a bad page.
        raise TaskRefused("that page's image file is missing")

    ctx.progress.check()
    read = ocr.read_page(
        ctx.translator, image,
        width=int(page.get("width") or 0), height=int(page.get("height") or 0),
        hint=str(page.get("hint") or ""), hooks=ctx.hooks)

    attempts = int(((page.get("ocr") or {}).get("attempts") or 0)) + 1
    return {
        "read": read.to_dict(),
        # A page the model itself was unsure about goes to a human, not straight into
        # the novel. Everything else is provisionally fine.
        "status": (pages.STATUS_OK if read.meta.confidence == "high"
                   else pages.STATUS_NEEDS_CHECK),
        # Cleared: whatever the note said, it has now been acted on, and leaving it
        # would send the same hint again on every later re-read.
        "hint": "",
        "error": None,
        "ocr": {"at": pages.now_iso(), "attempts": attempts,
                "confidence": read.meta.confidence,
                "regions": len(read.regions),
                "usage": read.usage, "cost_usd": read.cost_usd},
        "_usage": read.usage,
        "_cost": read.cost_usd,
    }


# ---- translating a manga chapter ---------------------------------------------

def _order_hash(lines) -> str:
    """The ordered line ids this call was built from.

    Kept so a later reordering can be NOTED. It deliberately does not stale anything:
    no bubble's Japanese changed, and re-billing a whole chapter because two bubbles in
    one panel swapped is disproportionate. But a line translated believing it followed
    line A now follows line B, and Japanese subject omission means that can genuinely
    change the English — so it is worth saying, and worth offering a re-translate.
    """
    return hashlib.sha256("|".join(line.id for line in lines).encode()).hexdigest()


def translate_script_task(chapter: dict, ctx: TaskContext) -> dict:
    """Translate one manga chapter's whole script.

    Like :func:`read_page_task` this returns FIELDS for the caller to merge, not a
    TaskResult — a manga's bookkeeping lives in ``pages.json`` and a chapter index
    written into ``state.json`` would collide with a prose chapter's.

    The per-line English is returned keyed by page id so the worker can write it under
    one lock, in one atomic write, together with the chapter's own record. Writing them
    separately would leave a window where the chapter says it is translated and the
    pages have no English on them.
    """
    if ctx.translator is None:
        raise TaskRefused("the translation engine is not available")

    doc = pages.load_pages(ctx.pid)
    wanted = [str(i) for i in (chapter.get("page_ids") or [])]
    by_id = {str(p.get("id")): p for p in doc.get("pages", [])}
    records = [by_id[i] for i in wanted if i in by_id]
    if not records:
        raise TaskRefused("that chapter's pages are no longer in this project")

    reads = []
    for record in records:
        read, _note = pages.effective_read(record)
        reads.append((int(record.get("seq") or 0), read))

    lines = manga.collect_lines(reads)
    if not lines:
        # Not a failure. A chapter of wordless action pages is an ordinary thing to
        # find in a manga, and marking it failed would put it in the user's face
        # forever.
        return {"chapter": {"status": pages.STATUS_OK, "at": pages.now_iso(),
                            "lines": 0, "warnings": [],
                            "order_hash": "", "usage": {}, "cost_usd": 0.0},
                "lines": {}, "_usage": {}, "_cost": 0.0}

    ctx.progress.step(0, len(lines))
    glossary_block = ctx.glossary.to_prompt_block(
        "\n".join(line.text for line in lines))

    result = manga.translate_script(
        ctx.translator, lines, glossary_block=glossary_block,
        title=str(chapter.get("title") or ""), hooks=ctx.hooks)
    attempts = 1

    if manga.should_retry(lines, result):
        # Told WHY, which is the only thing that makes a retry worth spending: an
        # identical prompt tends to produce an identical answer.
        ctx.progress.check()
        if ctx.hooks is not None:
            ctx.hooks.reset("retry")
        again = manga.translate_script(
            ctx.translator, lines, glossary_block=glossary_block,
            title=str(chapter.get("title") or ""), hooks=ctx.hooks,
            retry_hint=RETRY_REMINDER)
        attempts += 1
        # Merge rather than replace. The second attempt can be worse than the first,
        # and a line the first call got and the second dropped must not be lost.
        merged = dict(again.lines)
        for line_id, produced in result.lines.items():
            merged.setdefault(line_id, produced)
        for key, value in (again.usage or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result.usage[key] = result.usage.get(key, 0) + value
        result.lines = merged
        result.cost_usd = round(result.cost_usd + again.cost_usd, 6)
        result.warnings = result.warnings + [f"retry: {w}" for w in again.warnings]
        result.missing = [line.id for line in lines if line.id not in merged]

    seq_to_id = {int(r.get("seq") or 0): str(r.get("id")) for r in records}
    writes: dict[str, dict] = {}
    for index, line in enumerate(lines, start=1):
        produced = result.lines.get(line.id)
        if produced is None or not (produced.english or "").strip():
            continue
        page_id = seq_to_id.get(line.page_seq)
        if not page_id:
            continue
        writes.setdefault(page_id, {})[line.region_id] = {
            "english": produced.english,
            "source_hash": line.source_hash,
            "english_source": "model",
            "speaker": produced.speaker,
            "speaker_source": "model" if produced.speaker else "",
            "chapter": chapter.get("index"),
        }
        if index % 10 == 0:
            ctx.progress.step(index, len(lines))

    warnings = list(result.warnings)
    status = (pages.STATUS_NEEDS_CHECK if result.missing else pages.STATUS_OK)

    # Names harvested from a manga go through exactly the same human gate as a novel's.
    # A wrong reading of a name propagates to every later chapter and is expensive to
    # unpick — and Japanese makes wrong readings easy, since most kanji have several.
    # Only from a chapter that mostly came back, for the same reason the prose path
    # only harvests from a translation good enough to trust.
    queued_terms = 0
    if status == pages.STATUS_OK and result.new_terms:
        queued_terms = queue_new_terms(ctx.cfg, ctx.glossary, result.new_terms)
    return {
        "chapter": {
            "status": status,
            "at": pages.now_iso(),
            "attempts": attempts,
            "lines": len(lines),
            "missing": result.missing,
            "order_hash": _order_hash(lines),
            "chunks": result.chunks,
            "warnings": warnings,
            "new_terms_queued": queued_terms,
            "usage": result.usage,
            "cost_usd": result.cost_usd,
            "error": None,
        },
        "lines": writes,
        "_usage": result.usage,
        "_cost": result.cost_usd,
    }


# ---- dispatch ----------------------------------------------------------------

_REGISTRY: dict[str, Callable[[Chapter, TaskContext], TaskResult]] = {
    TASK_PREPARE: prepare_chapter,
    TASK_TRANSLATE: translate_chapter,
}


def run_task(kind: str, chapter: Chapter, ctx: TaskContext) -> TaskResult:
    """Run one queued CHAPTER item. Blocking — always via ``run_in_threadpool``.

    Page items do not come through here: their index is a page sequence number and
    their state lives in a different file, so the worker routes them to
    ``read_page_task`` instead.
    """
    handler = _REGISTRY.get(kind)
    if handler is None:
        # An unknown kind is refused rather than failed: nothing was written and
        # nothing broke, so the chapter keeps whatever status it had.
        raise TaskRefused(f"there is nothing called {kind!r} to do")
    return handler(chapter, ctx)


def applies_to(kind: str, chapter: Chapter, cfg: Config) -> bool:
    """Whether ``kind`` is a meaningful thing to do to this chapter at all.

    Kind-aware on purpose. ``prepare`` applies to everything, because prepare is what
    DECIDES whether a chapter is source, English or empty — filtering it by a
    classification it has not computed yet would mean an English chapter never gets
    classified and sits on "pending" forever.

    Every later kind translates, so it applies only to source chapters: queuing an
    already-English one would spend the user's allowance translating English into
    English, and queuing an empty one would ask the model to translate nothing.
    """
    if kind == TASK_PREPARE:
        return True
    return classify(chapter, cfg.translation.min_source_fraction) not in (
        KIND_EMPTY, KIND_ENGLISH)


def resolve_items(chapters: list[Chapter], state, kind: str, cfg: Config, *,
                  indices: list[int] | None = None,
                  force: bool = False) -> list[tuple[int, bool, str]]:
    """Which (index, force, kind) triples a request turns into.

    Three rules, each of which Night Reader had to learn:

    * An explicit list of indices is honoured exactly, including chapters the sweep
      would have skipped. The user asked for those specifically.
    * A FORCED sweep means "redo the whole project", so the already-done filter must
      NOT apply — otherwise every finished chapter is skipped and the request appears
      to succeed while having queued nothing.
    * A force still respects ``applies_to``. Forcing a translate on an empty chapter
      has no meaning.
    """
    if indices:
        wanted = {int(i) for i in indices}
        return [(c.index, force, kind) for c in chapters if c.index in wanted]

    todo = [c for c in chapters if applies_to(kind, c, cfg)]
    if force:
        return [(c.index, True, kind) for c in todo]
    return [(c.index, False, kind) for c in todo
            if not state.is_done(c.index, c.metrics.content_hash, kind)]
