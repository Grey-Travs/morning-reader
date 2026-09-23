"""What a run has already been billed for, kept somewhere an exception cannot lose it.

Every model call is charged the moment it completes, whatever happens next. The
accumulators that tracked that were FRAME LOCALS — ``total_usage`` and ``total_cost``
inside ``pipeline.translate_with_retry``, and ``usage``/``cost`` inside
``Translator.translate_chapter`` — committed onto the result only at the very end. So
any raise between the first completed call and that commit unwound past them, and the
spend simply vanished:

* A chapter fails its checks, the retry starts, and the rate limit lands. Attempt 1 was
  completed and billed; its usage, its cost and its prose all go with the frame. The
  worker rides the window out and re-runs the chapter from scratch, paying again.
* A long chapter translates in four chunks and the connection drops during the fourth.
  Three completed, billed calls are lost the same way.
* Press Stop between the two attempts and the same thing happens.

An accumulator the CALLER owns survives the unwind, because the caller is still holding
it. The task credits whatever is in it before re-raising, and the worker's existing
failure handlers persist the record — so the next attempt starts from a record that
already knows what was paid.

Deliberately not a subclass of dict and deliberately additive: ``State.add_usage`` has
the same rule for the same reason, and one chapter can be worked on several times.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Spend:
    """Usage and cost accumulated so far, by calls that have already completed."""

    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0

    def add(self, usage: dict | None, cost: float | None) -> None:
        """Record one completed call. Additive, never assignment."""
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.usage[key] = self.usage.get(key, 0) + value
        self.cost_usd = round(self.cost_usd + float(cost or 0.0), 6)

    def __bool__(self) -> bool:
        """Whether anything has actually been billed.

        So a caller can write ``if spend:`` and not credit an empty record, which would
        leave a misleading zero-cost entry on a chapter nothing was spent on.
        """
        return bool(self.usage) or self.cost_usd > 0


__all__ = ["Spend"]
