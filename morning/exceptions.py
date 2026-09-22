"""The three ways a queued task can end other than succeeding.

They live in the engine, not the server, because the worker's handling of each is a
*spine* behaviour and has to be identical whoever raises it. Step 1 has no model call,
so nothing raises ``RateLimited`` in production yet — the handling is written and
tested now because retro-fitting a rate-limit ride-out into a worker that never had
one is how you get a half-translated novel with no way to resume it.
"""

from __future__ import annotations


class TaskAborted(Exception):
    """The user pressed Stop and the task unwound cleanly.

    Not a failure: nothing was overwritten, so the item keeps whatever status it had
    before the attempt. A task that was genuinely unfinished goes back to pending so
    it resumes; one that already had good output on disk keeps it.
    """


class TaskRefused(Exception):
    """A task declined to write anything, leaving the item exactly as it was.

    Distinct from a failure: nothing broke and nothing changed, so the item must keep
    its existing status rather than being marked failed. The user should see "not
    applied", with a reason, rather than "failed".
    """


class RateLimitInfo:
    """What the provider told us about when the usage window refreshes.

    ``resets_at`` is epoch seconds, or None when the error carried no reset time (a
    bare 429). The worker treats those two cases differently — see
    ``server.jobs._rate_limit_resume_at`` — so the distinction has to survive.
    """

    __slots__ = ("resets_at", "message")

    def __init__(self, resets_at: float | None = None, message: str = ""):
        self.resets_at = resets_at
        self.message = message


class RateLimited(RuntimeError):
    """The subscription's usage window is exhausted; resume after it resets.

    Carries ``info`` rather than just a message so the worker can sleep until the real
    reset time instead of guessing. Nothing was written when this is raised, which is
    what makes putting the item back on the head of the queue safe.
    """

    def __init__(self, info: RateLimitInfo | None = None, message: str = ""):
        self.info = info or RateLimitInfo(message=message)
        super().__init__(message or self.info.message or "usage limit reached")
