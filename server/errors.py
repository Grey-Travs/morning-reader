"""Turn an exception into something a reader — not a developer — can act on.

Without this, a failure takes one of two useless shapes: an unhandled exception
reaches FastAPI as a bare 500 and uvicorn dumps a traceback across the console, and
the frontend, finding no JSON ``detail`` to parse, shows the user the words "Internal
Server Error".

``explain()`` maps what this codebase can actually raise onto a title, a plain-English
description, and ordered fix steps. The full traceback is still captured — it goes to
``logs/errors.log`` and behind a "Technical details" disclosure, not into the user's
face.

Two rules this module lives by:

* **It never raises.** Explaining a failure must not add one. Every entry point is
  wrapped, down to ``str(exc)``, which a custom ``__str__`` can itself fail at.
* **The rules are ORDERED and specific-before-broad.** The first predicate that
  matches wins, so a rule that keys on an HTTP status must be narrowed by who threw
  it. Night Reader learned this the hard way: a Google Docs 429 was reported as the
  Claude plan's usage limit, sending the user to wait for a window that was never the
  problem. Step 1 has no second API to confuse with the first — when Docs ingestion
  lands in step 2, that discrimination has to come back with it.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "errors.log"
_LOG_MAX_BYTES = 1_000_000  # keep the tail; this is a breadcrumb trail, not an archive

# UI affordances the frontend knows how to render as a button.
ACTION_RETRY = "retry"
ACTION_SETTINGS = "settings"
ACTION_SETUP = "setup"


@dataclass
class Explained:
    code: str                                       # stable slug for the frontend to switch on
    title: str                                      # one short line, shown as the heading
    what: str = ""                                  # plain-English description
    fixes: list[str] = field(default_factory=list)  # ordered, actionable steps
    action: str | None = None                       # optional button the UI can offer
    retryable: bool = False                         # whether "try again" is likely to help
    detail: str = ""                                # exception type + message, one line
    trace: str = ""                                 # full traceback, for the copy button
    status: int = 500                               # HTTP status to answer with


def as_dict(e: Explained) -> dict:
    return asdict(e)


def _message(exc: BaseException) -> str:
    """``str(exc)`` can itself raise — a custom ``__str__`` that fails, or a lazily
    formatted message. Since this module exists to make failures legible, it must
    never add one."""
    try:
        return str(exc)
    except Exception:  # noqa: BLE001
        return "<unprintable error>"


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_message(exc)}".strip()


def _trace(exc: BaseException) -> str:
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:  # noqa: BLE001
        return _detail(exc)


def explain(exc: BaseException) -> Explained:
    """Classify ``exc``. Never raises — an unrecognised error still yields a usable
    Explained with the raw detail and a copyable traceback."""
    try:
        return _classify(exc)
    except Exception:  # noqa: BLE001 — explaining a failure must not fail
        return Explained(
            code="unknown", title="Something went wrong",
            what="Morning Reader hit an error it could not identify.",
            fixes=["Try the action again.",
                   "If it keeps happening, copy the report below."],
            retryable=True, detail=_detail(exc), trace=_trace(exc),
        )


def _classify(exc: BaseException) -> Explained:
    name = type(exc).__name__
    low = _message(exc).lower()
    detail, trace = _detail(exc), _trace(exc)

    def out(**kw) -> Explained:
        return Explained(detail=detail, trace=trace, **kw)

    # ---- the queue's own outcomes ---------------------------------------------
    # These come first because they are the only ones that are not failures at all,
    # and misreporting a deliberate Stop as a crash is how a user stops trusting the
    # Stop button.
    if name == "TaskAborted":
        return out(
            code="stopped", title="Stopped",
            what="This item was stopped before it finished. Nothing was overwritten.",
            fixes=["Start it again whenever you are ready."],
            action=ACTION_RETRY, retryable=True, status=409,
        )

    if name == "TaskRefused":
        return out(
            code="refused", title="Nothing was changed",
            what="The task decided it had nothing safe to write, so the item was left "
                 "exactly as it was.",
            fixes=["Check the reason shown beside the item.",
                   "Nothing was lost — the previous version is still there."],
            retryable=False, status=409,
        )

    if name == "RateLimited" or "usage limit" in low or "rate limit" in low:
        return out(
            code="rate-limited", title="Your Claude plan's limit was reached",
            what="The subscription's usage window is exhausted. Progress is saved — "
                 "finished work is never redone, so nothing is lost.",
            fixes=["Wait for the window to reset; queued work resumes by itself.",
                   "Switch to a lighter model or a lower effort in Settings to stretch "
                   "the allowance further."],
            action=ACTION_SETTINGS, retryable=False, status=429,
        )

    # ---- Claude Code CLI: startup / availability -------------------------------
    if "control request timeout" in low or "initialize timeout" in low:
        return out(
            code="claude-start-timeout", title="Claude Code did not start in time",
            what="Morning Reader launched Claude Code but it did not finish starting "
                 "up before the timeout. This is usually a slow cold start — "
                 "antivirus scanning the process, or the machine being busy — rather "
                 "than a real fault, so the same action often works on a second try.",
            fixes=["Try again — a warm start is much faster.",
                   "Check you are still signed in: run `claude` in a terminal. If it "
                   "asks you to log in, do that and retry.",
                   "If it keeps timing out, close any leftover `claude` or `node` "
                   "processes, then restart Morning Reader."],
            action=ACTION_RETRY, retryable=True, status=503,
        )

    if name == "CLINotFoundError" or "claude code not found" in low or "enoent" in low:
        return out(
            code="claude-missing", title="Claude Code is not installed",
            what="The translation engine drives the Claude Code CLI, and it could not "
                 "be found on this machine.",
            fixes=["Install it: `npm install -g @anthropic-ai/claude-code`",
                   "Run `claude` once in a terminal and sign in.",
                   "Come back and run setup again to re-check the connection."],
            action=ACTION_SETUP, retryable=False, status=503,
        )

    if name in ("CLIConnectionError", "ProcessError") or "cli connection" in low:
        return out(
            code="claude-connection", title="Lost the connection to Claude",
            what="The Claude Code process stopped responding partway through.",
            fixes=["Try again — this is usually transient.",
                   "Check your internet connection, and any VPN or proxy.",
                   "If it happens every time, restart Morning Reader."],
            action=ACTION_RETRY, retryable=True, status=502,
        )

    # ---- local files ------------------------------------------------------------
    # Ordered inside this group too: the disk-full and permission cases are specific
    # kinds of OSError, so they have to be tested before the general one.
    if name in ("OSError", "IOError", "PermissionError") and (
            "no space" in low or "errno 28" in low):
        return out(
            code="disk-full", title="The disk is full",
            what="There is not enough free space to save.",
            fixes=["Free up some disk space, then try again."],
            action=ACTION_RETRY, retryable=True, status=507,
        )

    if name == "PermissionError" or "winerror 32" in low or "being used by another" in low:
        return out(
            code="file-locked", title="A file could not be written",
            what="Something else on this machine is holding one of this project's "
                 "files open. On Windows an antivirus scan or a file-sync client is "
                 "the usual cause, and it normally clears within a second or two.",
            fixes=["Try again.",
                   "Close the file if you have it open in another program.",
                   "If the folder syncs (OneDrive, Dropbox), pause syncing and retry."],
            action=ACTION_RETRY, retryable=True, status=500,
        )

    if name == "FileNotFoundError":
        return out(
            code="missing-file", title="A file is missing",
            what="Something this project needs is not on disk where it was expected.",
            fixes=["Reload the page — Morning Reader rebuilds what it can.",
                   "If a project looks empty, restore it from a backup copy of its "
                   "folder."],
            action=ACTION_RETRY, retryable=True, status=404,
        )

    if name == "JSONDecodeError" or "expecting value" in low:
        return out(
            code="corrupt-data", title="A saved file could not be read",
            what="One of this project's data files is unreadable — usually because "
                 "the app was killed mid-write. A copy of the bad bytes was kept "
                 "beside it, so nothing is gone for good.",
            fixes=["Reload the page; Morning Reader rebuilds what it can.",
                   "Look for a file ending `.unreadable-…` in the project folder — "
                   "that is the original content."],
            action=ACTION_RETRY, retryable=True, status=500,
        )

    if name in ("OSError", "IOError"):
        return out(
            code="file-error", title="A file could not be read or written",
            what="The operating system refused a file operation this project needed.",
            fixes=["Try again.", "Check there is free disk space.",
                   "Check the projects folder is readable and writable."],
            action=ACTION_RETRY, retryable=True, status=500,
        )

    # ---- programmer errors -------------------------------------------------------
    if name in ("ValueError", "KeyError", "TypeError", "AttributeError"):
        return out(
            code="bad-data", title="Something did not look the way it should",
            what="Morning Reader received data in a shape it did not expect. This is "
                 "a bug rather than anything you did wrong.",
            fixes=["Try the action again.",
                   "Use Copy report and include that text when reporting this."],
            retryable=True, status=500,
        )

    # ---- fallback ----------------------------------------------------------------
    return out(
        code="unknown", title="Something went wrong",
        what="Morning Reader hit an unexpected error. The technical details below say "
             "exactly what happened.",
        fixes=["Try the action again.",
               "If it keeps happening, use Copy report and include that text when "
               "asking for help."],
        retryable=True, status=500,
    )


def from_http_detail(detail, status: int) -> Explained:
    """Wrap an ``HTTPException`` detail in the same shape as ``explain()``.

    The endpoints raise HTTPException with hand-written, already-friendly messages.
    Those are good copy, so they become the title verbatim — the point here is only
    that the frontend gets ONE payload shape to parse rather than sometimes-a-string
    and sometimes-an-object.
    """
    if isinstance(detail, dict) and "code" in detail:
        return Explained(**{k: v for k, v in detail.items()
                            if k in Explained.__dataclass_fields__})
    text = detail if isinstance(detail, str) else str(detail)
    code = {400: "bad-request", 403: "forbidden", 404: "not-found",
            409: "conflict", 413: "too-large", 429: "rate-limited"}.get(status,
                                                                        "request-failed")
    return Explained(
        code=code, title=text, retryable=status in (429, 502, 503),
        detail=text, status=status,
    )


def log_error(e: Explained, where: str = "") -> None:
    """One compact line to the terminal; the full traceback to ``logs/errors.log``.

    Splitting these two is the whole point: the console stays readable while the
    traceback is still available (and copyable) when something needs diagnosing.
    """
    from . import console

    console.print_error(e.title, f"{where} {e.detail}".strip())
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Cheap size cap: once over the limit, keep the newest half and carry on.
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
            tail = LOG_FILE.read_text(encoding="utf-8",
                                      errors="replace")[-_LOG_MAX_BYTES // 2:]
            LOG_FILE.write_text(tail, encoding="utf-8")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n{'=' * 70}\n[{stamp}] {e.code} - {e.title}\n"
                     f"{('at ' + where) if where else ''}\n{e.trace or e.detail}\n")
    except Exception:  # noqa: BLE001 — logging must never break the response
        pass
