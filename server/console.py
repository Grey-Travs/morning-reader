"""Live job progress printed to the server's own terminal window.

The launcher leaves a console open for the whole session, and uvicorn is pinned to
``log_level="warning"``, so without this a job that takes minutes produces total
silence. Every job event funnels through here (see ``jobs.Job.publish`` /
``publish_live``), so the terminal mirrors exactly what the in-app console shows::

    | morning-reader ------------------------
    > ch 3  "第3話 目覚め"  4,812 ch
      | opus · effort high
      | ########......  12/47 ¶          <- redrawn in place, never scrolls
      + ok prepared  0.4s

Design constraints worth keeping in mind when editing:

* This is called from the asyncio event loop. Printing is cheap but not free, so the
  streaming path only ever rewrites ONE line and rate-limits itself.
* Japanese titles mean UTF-8 stdout is mandatory (``term.force_utf8_stdio``) and width
  maths must count Kanji and Kana as two columns (``term.visible_width``).
* Colour is optional and degrades to plain text; never assume ANSI works.
* Nothing here may raise. A cosmetic line is never worth failing a job over, which is
  why ``print_event`` swallows everything.
"""

from __future__ import annotations

import sys
import time

from morning.term import force_utf8_stdio, paint, supports_color, truncate, visible_width

# Resolved once at import: reconfigure stdio for UTF-8 before anything prints a title.
force_utf8_stdio()
_COLOR = supports_color()

_BAR_WIDTH = 14
_TITLE_WIDTH = 32
_HEADER = "▌ morning-reader "

# Per-item render state, keyed by project id, so two projects running at once do not
# interleave into an unreadable mess.
_state: dict[str, dict] = {}
_last_flush: dict[str, float] = {}
_open_line = False        # True when the cursor sits on an unfinished \r progress line
_banner_shown = False

_MIN_REDRAW_INTERVAL = 0.2  # seconds between progress-line repaints

# Hoisted out of the f-strings below: a backslash escape inside an f-string
# expression only became legal in 3.12, and this module has no reason to need it.
_ARROW = "▸"


def _c(text: str, *styles: str) -> str:
    return paint(text, *styles, enabled=_COLOR)


def _write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a closed/redirected console never breaks a run
        pass


def _end_open_line() -> None:
    """Terminate an in-place progress line so the next real line starts clean."""
    global _open_line
    if _open_line:
        _write("\n")
        _open_line = False


def _banner() -> None:
    global _banner_shown
    if _banner_shown:
        return
    _banner_shown = True
    _write(_c(_HEADER + "─" * 40, "grey") + "\n")


def _bar(done: int, total: int) -> str:
    if total <= 0:
        return "░" * _BAR_WIDTH
    filled = max(0, min(_BAR_WIDTH, round(_BAR_WIDTH * done / total)))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


_STATUS_STYLE = {
    "prepared": ("✓", "green"),
    "validated": ("✓", "green"),
    "translated": ("✓", "cyan"),
    "needs-review": ("⚠", "yellow"),
    "failed": ("✗", "red"),
    "pending": ("·", "grey"),
    "empty": ("·", "grey"),
    "english-source": ("·", "grey"),
}


def print_event(pid: str, ev: dict) -> None:
    """Render one job event. Safe to call for every event type; unknown types are
    ignored so adding an event elsewhere can never crash a job."""
    try:
        _render(pid, ev)
    except Exception:  # noqa: BLE001 — cosmetic output is never worth failing a job over
        pass


def _render(pid: str, ev: dict) -> None:
    global _open_line
    kind = ev.get("type")

    if kind == "start":
        _banner()
        _end_open_line()
        _state[pid] = {"index": ev.get("index"), "done": 0,
                       "total": ev.get("units") or 0, "started": time.time()}
        title = truncate(str(ev.get("title") or ""), _TITLE_WIDTH)
        label = ev.get("label") or f"item {ev.get('index')}"
        head = f"{_c(_ARROW, 'cyan')} {label}  {_c(title, 'bold')}"
        chars = ev.get("chars")
        if chars:
            head += _c(f"  {_fmt_count(int(chars))} ch", "grey")
        _write(head + "\n")
        model, effort = ev.get("model"), ev.get("effort")
        if model:
            _write(_c(f"  ├ {model} · effort {effort}", "grey") + "\n")
        return

    if kind == "progress":
        st = _state.get(pid)
        if st is None:
            return
        st["done"] = int(ev.get("done") or 0)
        st["total"] = int(ev.get("total") or st.get("total") or 0)
        now = time.time()
        if now - _last_flush.get(pid, 0.0) < _MIN_REDRAW_INTERVAL:
            return
        _last_flush[pid] = now
        line = (f"  ├ {_bar(st['done'], st['total'])}  "
                f"{st['done']}/{st['total']} ¶")
        # Trailing blanks wipe the tail of a longer previous line; \r alone would
        # leave its last few characters on screen.
        _write("\r" + line + "        ")
        _open_line = True
        return

    if kind == "item":
        st = _state.pop(pid, None)
        _last_flush.pop(pid, None)
        _end_open_line()
        status = str(ev.get("status") or "")
        mark, colour = _STATUS_STYLE.get(status, ("·", "grey"))
        parts = [f"  └ {_c(mark, colour)} {_c(status, colour)}"]
        if st:
            parts.append(_c(f"{time.time() - st['started']:.1f}s", "grey"))
        if ev.get("skipped"):
            parts.append(_c("(already done)", "grey"))
        if ev.get("refused"):
            parts.append(_c("(not applied)", "yellow"))
        if ev.get("aborted"):
            parts.append(_c("(stopped)", "grey"))
        if ev.get("error"):
            parts.append(_c(truncate(str(ev["error"]), 60), "red"))
        _write("  ".join(parts) + "\n")
        return

    if kind == "waiting":
        _end_open_line()
        _write(_c(f"  ⏸ waiting out a usage limit — {ev.get('message', '')}",
                  "yellow") + "\n")
        return

    if kind == "resumed":
        _end_open_line()
        _write(_c("  ▶ resumed", "green") + "\n")
        return

    if kind in ("done", "paused"):
        _state.pop(pid, None)
        _last_flush.pop(pid, None)
        _end_open_line()
        word = "finished" if kind == "done" else "paused"
        line = _c(f"└ {word}", "grey")
        totals = ev.get("totals") or {}
        if totals.get("cost_usd"):
            line += _c(f"  ${totals['cost_usd']:.4f}", "grey")
        _write(line + "\n")
        return


def print_error(title: str, detail: str = "") -> None:
    """One compact line for a failure. The traceback goes to logs/errors.log."""
    try:
        _end_open_line()
        _write(_c(f"✗ {title}", "red") + (_c(f"  {truncate(detail, 100)}", "grey")
                                               if detail else "") + "\n")
    except Exception:  # noqa: BLE001
        pass


def print_line(text: str, *styles: str) -> None:
    """A plain line from outside the job machinery (startup notices, and the like)."""
    try:
        _end_open_line()
        _write(_c(text, *styles) + "\n")
    except Exception:  # noqa: BLE001
        pass


__all__ = ["print_event", "print_error", "print_line", "visible_width"]
