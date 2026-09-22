"""What a queued item asks the worker to do, and the code that does it.

Everything the user can start runs through this registry and therefore through the one
Job — which is what puts it in the Activity view, streams it to the live console, and
gives it Stop and the rate-limit auto-resume for free, without any of that being
reimplemented per operation.

A task is a plain blocking function. It is always called via ``run_in_threadpool``, so
it may do file I/O and long CPU work, and it must NOT touch asyncio. It reports
progress and checks for a stop through the ``Progress`` handle it is given.

Step 1 ships one task, ``prepare``. Step 2 adds translate/resolve/pronouns; step 3
adds the page tasks. They plug in here and inherit the whole spine.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from morning.chapters import KIND_EMPTY, KIND_ENGLISH, Chapter, classify
from morning.config import Config
from morning.exceptions import TaskAborted, TaskRefused
from morning.state import STATUS_EMPTY, STATUS_ENGLISH, STATUS_PREPARED

# ---- task kinds --------------------------------------------------------------
TASK_PREPARE = "prepare"      # segment, measure and classify one chapter
TASK_KINDS = (TASK_PREPARE,)

# Kinds whose index is a PAGE sequence number rather than a chapter index.
#
# Empty in step 1 and deliberately not deleted. Pages and chapters share one worker
# but NOT one number space: once a scanned work has been built, page 5 and chapter 5
# both exist and are different things. The dedup key is namespaced against exactly
# this, and Night Reader shipped the bug first — a duplicate guard keyed on the bare
# index made queueing a page silently drop a chapter.
PAGE_TASK_KINDS: tuple[str, ...] = ()

# How each kind is described in the UI and the terminal.
TASK_LABEL = {
    TASK_PREPARE: "Preparing",
}


def describe(kind: str, index: int) -> str:
    return f"{TASK_LABEL.get(kind, 'Working on')} {index}"


def queue_key(index: int, kind: str) -> str:
    """Dedup key for the pending set.

    Namespaced by number space, not by task kind: two different things asked of the
    same chapter should both be able to queue, but the same page and the same chapter
    number must never collide. ``queue_state()`` still reports plain integer indices,
    so the Activity views and /api/queue are unaffected.
    """
    space = "pg" if kind in PAGE_TASK_KINDS else "ch"
    return f"{space}:{index}:{kind}"


# ---- the handle a task gets --------------------------------------------------

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
    units: int = 0  # how much work this turned out to be, for the progress bar


# ---- prepare -----------------------------------------------------------------

def prepare_chapter(chapter: Chapter, cfg: Config, progress: Progress) -> TaskResult:
    """Segment, measure and classify one chapter, and record what it is.

    The first real thing that happens to a chapter, and the precursor to translating
    it. It makes no model call and costs nothing, which is exactly why it is the task
    the spine is proved with: every branch of the worker — skip-as-done, stop, refuse,
    fail, succeed — is exercised without spending a token or depending on a network.

    What it records is genuinely needed downstream: ``source_hash`` is the
    resumability key for every later task, and the classification decides whether this
    chapter is translated at all.
    """
    metrics = chapter.metrics
    kind = classify(chapter, cfg.translation.min_source_fraction)

    # Walk the paragraphs rather than reading the metrics and returning. Two reasons,
    # both load-bearing: it gives Stop somewhere to take effect on a long chapter, and
    # it gives the progress bar real units instead of a fake 0->100.
    total = max(1, len(chapter.paragraphs))
    longest = 0
    for i, paragraph in enumerate(chapter.paragraphs, start=1):
        longest = max(longest, len(paragraph))
        progress.step(i, total)

    if kind == KIND_EMPTY:
        # Not a failure: a blank placeholder is a perfectly ordinary thing to find in
        # a source document, and marking it failed would put it in the user's face
        # forever.
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


# ---- dispatch ----------------------------------------------------------------

def run_task(kind: str, chapter: Chapter, cfg: Config, progress: Progress) -> TaskResult:
    """Run one queued item. Blocking — always called via ``run_in_threadpool``."""
    if kind == TASK_PREPARE:
        return prepare_chapter(chapter, cfg, progress)
    # An unknown kind is refused rather than failed: nothing was written and nothing
    # broke, so the chapter keeps whatever status it had.
    raise TaskRefused(f"there is nothing called {kind!r} to do")


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
