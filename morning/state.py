"""Per-item run state for idempotent, resumable, non-re-billing runs.

``state.json`` tracks each chapter's status, source content hash, token usage, cost
and timestamps. Re-running skips items that are already done unless the source hash
changed (or the user forces a redo), so a crash or a rate limit never forces a full,
re-billed re-run.

**The hash field is ``source_hash``.** Night Reader's equivalent files key on the
language of the original in three places, and that is the single reason this is a
separate app. Nothing on disk here may name a language.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .atomic import atomic_write_json, quarantine_unreadable, read_text_retrying

# Lifecycle. Step 1 reaches `prepared`; the translate pipeline (step 2) adds the rest,
# which is why they are declared here now — `DONE_STATUSES` has to be a single list
# that every writer agrees on, and splitting it across two releases is how a chapter
# ends up "done" to one reader and "pending" to another.
STATUS_PENDING = "pending"          # known, nothing done to it yet
STATUS_PREPARED = "prepared"        # segmented, measured, classified — ready to translate
STATUS_TRANSLATED = "translated"    # English written, not yet validated   (step 2)
STATUS_VALIDATED = "validated"      # English written and checked          (step 2)
STATUS_NEEDS_REVIEW = "needs-review"
STATUS_FAILED = "failed"
STATUS_EMPTY = "empty"              # no prose (a blank placeholder) — nothing to do
STATUS_ENGLISH = "english-source"   # already English — skipped, not translated

STATUSES = (
    STATUS_PENDING, STATUS_PREPARED, STATUS_TRANSLATED, STATUS_VALIDATED,
    STATUS_NEEDS_REVIEW, STATUS_FAILED, STATUS_EMPTY, STATUS_ENGLISH,
)

# Statuses that count as "done" for a given task kind: reaching one means the work
# won't be redone unless forced or the source hash changed.
#
# Keyed by kind rather than being one global set, because "done" is not a property of
# the item — it is a property of the *question being asked about* the item. A chapter
# that is `validated` is done for both prepare and translate; one that is merely
# `prepared` is done for prepare and emphatically not for translate. Night Reader got
# away with one set because it had one task that could be skipped; adding the second
# without this distinction is exactly how a queued translate silently no-ops.
_TERMINAL = frozenset({STATUS_EMPTY, STATUS_ENGLISH})
DONE_STATUSES: dict[str, frozenset[str]] = {
    "prepare": frozenset({STATUS_PREPARED, STATUS_TRANSLATED, STATUS_VALIDATED}) | _TERMINAL,
    "translate": frozenset({STATUS_VALIDATED}) | _TERMINAL,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, data: dict | None = None):
        chapters = (data or {}).get("chapters")
        self.chapters: dict[str, dict] = chapters if isinstance(chapters, dict) else {}

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls()
        # Two different failures, and conflating them was a bug that could erase a
        # whole novel's history.
        #
        # **Could not READ it** — an antivirus or a file-sync client holding it open
        # for a moment, which is ordinary on Windows. That is not the same as "it is
        # empty", and degrading here is how a momentary failure became permanent: the
        # caller mutates the empty state and ``mutate_state`` saves it back, erasing
        # every chapter's status, usage and cost, after which the novel reads as
        # entirely untranslated and re-bills to redo it. So the read retries (see
        # ``atomic.read_text_retrying``) and then RAISES. ``mutate_state`` never
        # reaches its save when the body raises, which is exactly the point.
        #
        # **Could not PARSE it** — truncated or garbled, e.g. the process was killed
        # mid-write. The file really is unusable, so keep the bytes aside and start
        # fresh rather than taking the whole library down for one bad file.
        raw = read_text_retrying(path)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            quarantine_unreadable(path)
            data = {}
        return cls(data if isinstance(data, dict) else {})

    def save(self, path: str | Path) -> None:
        # Atomic write: serialize to a temp file in the same dir, then os.replace — a
        # concurrent reader (common, since work can be queued while a job runs) never
        # sees a half-written file, only the old or the new one. The server saves this
        # file from several threads at once, so the temp name MUST be unique per call;
        # atomic_write_json owns that (see morning/atomic.py).
        atomic_write_json(path, {"chapters": self.chapters})

    def get(self, index: int) -> dict | None:
        rec = self.chapters.get(str(index))
        return rec if isinstance(rec, dict) else None

    def is_done(self, index: int, source_hash: str, kind: str = "translate") -> bool:
        """Whether ``kind`` has already been done to this item at this exact source.

        Both halves matter. The status says the work finished; the hash says it
        finished against the text that is there *now*. Edit the source and the hash
        moves, so the item stops being done and re-runs — which is the whole
        resumability contract, and the reason nothing else may key on a timestamp.
        """
        rec = self.get(index)
        done = DONE_STATUSES.get(kind)
        if done is None:
            # An unknown kind is never "already done" — skipping work because of a
            # typo'd kind would be silent and unrecoverable.
            return False
        return bool(rec
                    and rec.get("status") in done
                    and rec.get("source_hash") == source_hash)

    def update(self, index: int, **fields) -> dict:
        rec = self.chapters.setdefault(str(index), {})
        rec.update(fields)
        rec["updated_at"] = _now()
        rec.setdefault("created_at", rec["updated_at"])
        return rec

    def add_usage(self, index: int, usage: dict, cost: float) -> None:
        """Accumulate spend. Additive, never assignment: one chapter can be translated,
        then repaired, then re-checked, and the novel's cost is all of it."""
        rec = self.chapters.setdefault(str(index), {})
        acc = rec.setdefault("usage", {})
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                acc[k] = acc.get(k, 0) + v
        rec["cost_usd"] = round(rec.get("cost_usd", 0.0) + float(cost or 0.0), 6)

    def totals(self) -> dict:
        total_cost = 0.0
        total_tokens: dict[str, int] = {}
        for rec in self.chapters.values():
            if not isinstance(rec, dict):
                continue
            total_cost += rec.get("cost_usd", 0.0) or 0.0
            for k, v in (rec.get("usage") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total_tokens[k] = total_tokens.get(k, 0) + v
        return {"cost_usd": round(total_cost, 4), "tokens": total_tokens}
