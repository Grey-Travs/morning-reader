"""One chapter, end to end: translate, validate, retry, record.

This is the only place that decides what happens to a chapter, and the rule it exists
to enforce is where the finished prose is allowed to land:

* **A chapter that validates** is written to ``chapters/`` and read.
* **A chapter that does not** is written ONLY to its audit copy.

That split is the whole safety property. A questionable translation must never be
mistaken for a finished one, and the audit copy is what the review screen reads and
what Accept later promotes — so the prose is never lost, it is just not yet trusted.

Not here, deliberately:

* **No export-cruft stripping.** The Korean app removes a site export's header and
  footer before the model sees them. The Japanese equivalent is step 5 and has to be
  derived from real samples, not guessed; writing a speculative one now would silently
  delete prose from a paste that never had cruft in the first place.
* **No second "deep check" model call.** The fast checks decide, and a failure goes to
  a human. Adding a model call to re-judge the model's own output doubles the cost of
  every flagged chapter for a verdict nobody has calibrated yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from .chapter_files import (
    read_audit_translation, write_audit, write_chapter_file,
)
from .chapters import KIND_EMPTY, KIND_ENGLISH, Chapter, classify
from .config import Config
from .glossary import Glossary, load_pending, merge_pending, save_pending
from .state import (
    STATUS_EMPTY, STATUS_ENGLISH, STATUS_NEEDS_REVIEW, STATUS_VALIDATED, State,
)
from .translator import RETRY_REMINDER, StreamHooks, TranslationResult, Translator
from .validate import ValidationResult, validate_translation


@dataclass
class ChapterOutcome:
    """What happened to one chapter. Returned so the worker can report it without
    re-reading state."""

    status: str
    usage: dict
    cost_usd: float
    failures: list[str]
    warnings: list[str]
    attempts: int = 0
    new_terms_queued: int = 0


def _fewer_failures(a: ValidationResult, b: ValidationResult) -> bool:
    return len(a.failures) < len(b.failures)


def translate_with_retry(chapter: Chapter, translator: Translator, cfg: Config,
                         glossary: Glossary, hooks: StreamHooks | None = None
                         ) -> tuple[TranslationResult, ValidationResult, int]:
    """Translate, and try once more if the result fails its checks.

    The retry is told WHY the first attempt failed, which is the only thing that makes
    it worth spending: an identical prompt tends to produce an identical answer.

    Whichever attempt validates is kept. If none does, the one with the fewest
    failures is kept rather than the last — a second attempt can be worse than the
    first, and silently preferring the newest would throw away the better translation.
    Usage and cost accumulate across every attempt, because every attempt was billed.
    """
    glossary_block = glossary.to_prompt_block(chapter.text)
    best: TranslationResult | None = None
    best_validation: ValidationResult | None = None
    total_usage: dict = {}
    total_cost = 0.0
    attempts = 0

    for attempt in range(max(1, cfg.translation.validation_retries + 1)):
        if hooks is not None and attempt > 0:
            # The whole chapter is being redone, so a live view must drop everything
            # it has shown rather than appending a second copy underneath the first.
            hooks.reset("retry")

        result = translator.translate_chapter(
            chapter, glossary_block=glossary_block, hooks=hooks,
            retry_hint=(RETRY_REMINDER if attempt else ""),
        )
        attempts += 1
        for key, value in (result.usage or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total_usage[key] = total_usage.get(key, 0) + value
        total_cost += result.cost_usd

        validation = validate_translation(chapter, result.english, cfg.validation,
                                          glossary=glossary)
        if best is None or _fewer_failures(validation, best_validation):
            best, best_validation = result, validation
        if validation.ok:
            break

    best.usage = total_usage
    best.cost_usd = round(total_cost, 6)
    return best, best_validation, attempts


def queue_new_terms(cfg: Config, glossary: Glossary, terms: list[dict]) -> int:
    """Put newly proposed terms in the pending queue. Returns how many were added.

    Never writes to the glossary itself. A wrong reading of a name propagates to every
    later chapter and is expensive to unpick, and Japanese makes wrong readings easy —
    most kanji have several. The human gate is the point.
    """
    if not terms:
        return 0
    path = cfg.paths.glossary_pending
    existing = load_pending(path)
    queue, added = merge_pending(existing, terms, glossary=glossary)
    if added:
        save_pending(path, queue)
    return added


def accept_chapter(chapter: Chapter, total: int, cfg: Config, state: State,
                   english: str | None = None) -> str:
    """Promote a reviewed chapter's translation into ``chapters/``.

    This is the other half of the audit copy's purpose. A chapter that failed its
    checks was written only to audit/, precisely so a questionable translation could
    not be mistaken for a finished one; accepting it is the human saying "I looked, and
    it is fine". Only then does it become readable.

    ``english`` overrides what is on disk, so the reviewer can fix a line first. When
    it is omitted the audit copy is promoted unchanged.

    Raises ``FileNotFoundError`` when there is nothing to accept, rather than writing
    an empty chapter — which would look exactly like a successful accept.
    """
    prose = english if english is not None else read_audit_translation(
        cfg.paths.audit_dir, chapter.index, total)
    if not (prose or "").strip():
        raise FileNotFoundError(
            f"there is no saved translation for chapter {chapter.index} to accept")

    write_chapter_file(cfg.paths.output_dir, chapter.index, total, prose)
    state.update(
        chapter.index,
        status=STATUS_VALIDATED,
        title=chapter.title,
        source_hash=chapter.metrics.content_hash,
        # Kept, not cleared. Why it was flagged is still true and still worth seeing
        # later; what changed is that a human decided it was acceptable anyway.
        accepted=True,
        error=None,
    )
    return STATUS_VALIDATED


def process_chapter(chapter: Chapter, total: int, translator: Translator,
                    glossary: Glossary, cfg: Config, state: State,
                    hooks: StreamHooks | None = None) -> ChapterOutcome:
    """Run one chapter through translate → validate → retry → record.

    ``state`` is mutated but NOT saved — the caller owns persistence, because it is the
    one holding the lock and merging against concurrent edits.
    """
    metrics = chapter.metrics

    kind = classify(chapter, cfg.translation.min_source_fraction)
    if kind == KIND_EMPTY:
        state.update(chapter.index, status=STATUS_EMPTY, title=chapter.title,
                     source_hash=metrics.content_hash,
                     note="no prose to translate")
        return ChapterOutcome(STATUS_EMPTY, {}, 0.0, [], [])

    if kind == KIND_ENGLISH and cfg.translation.skip_non_source:
        state.update(chapter.index, status=STATUS_ENGLISH, title=chapter.title,
                     source_hash=metrics.content_hash,
                     note=f"already English ({metrics.source_fraction:.0%} Japanese) "
                          f"— left as it is")
        return ChapterOutcome(STATUS_ENGLISH, {}, 0.0, [], [])

    result, validation, attempts = translate_with_retry(
        chapter, translator, cfg, glossary, hooks)

    status = STATUS_VALIDATED if validation.ok else STATUS_NEEDS_REVIEW

    # The audit copy is written for EVERY chapter, and for a needs-review one it is the
    # only copy there is — see the module docstring.
    write_audit(cfg.paths.audit_dir, chapter.index, total, chapter=chapter,
                english=result.english,
                notes=validation.failures + validation.warnings)

    queued = 0
    if validation.ok:
        write_chapter_file(cfg.paths.output_dir, chapter.index, total, result.english)
        # Terms are queued only from a translation good enough to trust. Harvesting
        # names out of a chapter that failed its checks fills the review queue with
        # readings taken from prose nobody has accepted.
        queued = queue_new_terms(cfg, glossary, result.new_terms)

    state.update(
        chapter.index,
        status=status,
        title=chapter.title,
        source_hash=metrics.content_hash,
        source_chars=metrics.char_count,
        source_fraction=metrics.source_fraction,
        chunks=result.chunks,
        attempts=attempts,
        validation=validation.metrics,
        failures=validation.failures,
        warnings=validation.warnings,
        leak_findings=validation.leak_findings,
        new_terms_queued=queued,
        error=None,
    )
    state.add_usage(chapter.index, result.usage, result.cost_usd)

    return ChapterOutcome(
        status=status, usage=result.usage, cost_usd=result.cost_usd,
        failures=validation.failures,
        warnings=validation.warnings + result.warnings,
        attempts=attempts, new_terms_queued=queued,
    )
