"""Terminal plumbing shared by the CLI and the web server's live console.

Two Windows-specific hazards handled here, both of which otherwise show up only at
runtime with a Japanese work loaded:

* the console defaults to cp1252, so printing a Japanese title raises
  ``UnicodeEncodeError`` unless stdout/stderr are reconfigured to UTF-8;
* ANSI escapes are not interpreted by older conhost sessions unless virtual-terminal
  processing is switched on.

The width maths carries over from the Korean app untouched, because it is about
Unicode's East Asian Width property and not about any one language: Kanji, Kana and
Hangul are all "W" and all take two columns in a monospace console.
"""

from __future__ import annotations

import os
import sys
import unicodedata

_RESET = "\033[0m"
_CODES = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}


def force_utf8_stdio() -> None:
    """Japanese titles and curly quotes are printed to the console; a cp1252 Windows
    console would raise UnicodeEncodeError. Reconfigure to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def supports_color() -> bool:
    """Whether it is safe to emit ANSI. Honours the NO_COLOR convention, skips
    redirected output (a log file should not collect escape codes), and asks colorama
    to switch Windows' console into VT mode when it is importable.

    colorama ships as a transitive uvicorn/click dependency and is declared in
    requirements.txt, but this stays optional so a slim install still runs.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    try:
        import colorama

        just_fix = getattr(colorama, "just_fix_windows_console", None)
        if just_fix is not None:
            just_fix()
        else:  # older colorama
            colorama.init()
    except Exception:  # noqa: BLE001 — raw ANSI still works on modern terminals
        pass
    return True


def paint(text: str, *styles: str, enabled: bool = True) -> str:
    """Wrap ``text`` in ANSI styles, or return it untouched when colour is off."""
    if not enabled or not styles:
        return text
    prefix = "".join(_CODES.get(s, "") for s in styles)
    return f"{prefix}{text}{_RESET}" if prefix else text


def _char_width(c: str) -> int:
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def visible_width(text: str) -> int:
    """Display width, counting CJK as two columns so box art and fixed-width
    truncation line up in a monospace console."""
    return sum(_char_width(c) for c in text)


def truncate(text: str, width: int) -> str:
    """Trim to ``width`` display columns (not characters), appending an ellipsis.

    Columns, not characters, is the whole point: a title of twenty Kanji is forty
    columns wide, so truncating by character count overflows the line and smears the
    progress display across two rows.
    """
    if visible_width(text) <= width:
        return text
    # No room even for the ellipsis alongside a character. The budget below used to
    # floor at one column, so a one-column slot came back two columns wide.
    if width <= 1:
        return "…" if width == 1 else ""
    out, used = [], 0
    for c in text:
        w = _char_width(c)
        if used + w > width - 1:
            break
        out.append(c)
        used += w
    return "".join(out) + "…"
