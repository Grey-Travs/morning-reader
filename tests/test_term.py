"""Tests for the terminal width maths, and for the renderer that depends on it.

The server leaves a console window open for the whole session and mirrors every job
into it: a header line per chapter carrying its title, a progress bar redrawn in place
with ``\\r``, an outcome line. The titles are Japanese, and in a monospace console a
Kanji or a Kana takes TWO columns. Everything that keeps that display on its rows (the
title cut to a fixed slot, an error cut to one line) asks ``morning/term.py`` how
wide a string is.

Count characters instead of columns and a twenty-Kanji title is forty columns in a
thirty-two-column slot. The line wraps, the ``\\r`` redraw lands on the wrong row, and
the console smears into an unreadable mess. Nothing raises, so nothing but a test
notices, and conftest silences the renderer for every other test in the suite.

Pinned here:

* which characters are wide (East Asian Width W and F: Kanji, Kana, full-width forms)
  and which are not (ASCII, and half-width katakana, which really is one column);
* that truncation cuts by columns, never overflows its limit, never lets a wide
  character straddle the edge, and marks the cut with a one-column ellipsis;
* the colour and UTF-8 plumbing that lives in the same module;
* the renderer itself (``server/console.py``) using all of it.

Not pinned: ``visible_width`` measures plain text and does not skip ANSI escapes. No
caller hands it painted text (the renderer truncates first and paints after, which
is pinned at the end), so there is no behaviour there to hold anyone to.

The Japanese here is invented: single characters and short made-up titles.
"""

from __future__ import annotations

import io
import re
import sys
import types
import unicodedata

import pytest

from morning.term import (
    _char_width, force_utf8_stdio, paint, supports_color, truncate, visible_width,
)
from server import console

# ---- invented fixtures -------------------------------------------------------
# Twenty wide characters: forty columns, but only twenty CHARACTERS, so it slips under
# a 32-wide limit if anything counts characters. The case truncate()'s docstring names.
TITLE = "雨の夜に古い時計を直す少年と白い猫の物語"
SHORT = "雨の夜の橋"            # five wide characters, ten columns
MIXED = "No.3 雨の夜"          # five narrow then three wide: eleven columns
HALF_KANA = "ｶﾀｶﾅﾉﾖﾙ"          # seven half-width katakana: seven columns
PLAIN = "The third night on the bridge"
ERROR = "ページの読み取りに失敗しました" * 3   # 45 characters, 90 columns

ELLIPSIS = "…"


def _columns(text: str) -> int:
    """A column count taken straight from the Unicode table, NOT from morning.term.

    The sweep tests below measure truncate()'s output with this, so a broken width
    function in the module under test cannot vouch for its own output. The table
    itself is pinned separately, character by character.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def test_the_fixtures_are_the_shape_the_tests_rely_on():
    """Guards the guards: the title must be under 32 characters yet over 32 columns,
    or the column-versus-character tests below stop discriminating."""
    assert len(TITLE) == 20 and _columns(TITLE) == 40
    assert _columns(SHORT) == 10
    assert _columns(MIXED) == 11
    assert len(HALF_KANA) == 7 and _columns(HALF_KANA) == 7
    assert len(ERROR) == 45 and _columns(ERROR) == 90


# ---- which characters are wide -----------------------------------------------

@pytest.mark.parametrize("label,char", [
    ("ASCII letter", "a"),
    ("ASCII digit", "7"),
    ("ASCII space", " "),
    ("ASCII punctuation", "!"),
    ("half-width katakana", "ｶ"),          # U+FF76, East Asian Width H
    ("half-width voiced mark", "ﾞ"),       # U+FF9E, H
    ("half-width corner bracket", "｢"),    # U+FF62, H
])
def test_narrow_characters_take_one_column(label, char):
    """Counting a narrow character as two leaves every line short and the right-hand
    edge of the display ragged."""
    assert _char_width(char) == 1, label


@pytest.mark.parametrize("label,char", [
    ("kanji", "雨"),
    ("hiragana", "あ"),
    ("katakana", "カ"),
    ("long-vowel mark", "ー"),             # U+30FC, W
    ("corner bracket", "「"),              # U+300C, W
    ("ideographic full stop", "。"),       # U+3002, W
    ("ideographic space", "　"),       # F: the space in 第1話　Title headings
    ("full-width digit", "１"),            # U+FF11, F
    ("full-width Latin letter", "Ａ"),     # U+FF21, F
    ("full-width exclamation", "！"),      # U+FF01, F
    ("syllable block", "한"),              # W: the maths came over from the other app
])
def test_wide_characters_take_two_columns(label, char):
    """Counting one of these as one column is the bug this module exists to prevent:
    a Japanese title overruns its slot by as many columns as it has characters."""
    assert _char_width(char) == 2, label


def test_half_width_katakana_really_is_half_width():
    """The tempting shortcut, "anything outside ASCII is wide", gets this wrong. The
    same four katakana are four columns half-width and eight full-width."""
    assert visible_width("ｶﾀｶﾅ") == 4
    assert visible_width("カタカナ") == 8


@pytest.mark.parametrize("text,columns", [
    ("", 0),
    ("Chapter 3", 9),
    ("雨の日", 6),
    ("第2章　夜の橋", 13),
    (MIXED, 11),
    (HALF_KANA, 7),
])
def test_visible_width_counts_columns_not_characters(text, columns):
    """The sum over a whole string: what the renderer compares against its slots."""
    assert visible_width(text) == columns


def test_the_ellipsis_is_one_column():
    """truncate() holds back exactly one column for the ellipsis, so the ellipsis has
    to BE one column, and one character: three full stops would cost three."""
    assert len(ELLIPSIS) == 1
    assert visible_width(ELLIPSIS) == 1


# ---- truncation --------------------------------------------------------------

@pytest.mark.parametrize("text,width,expected", [
    # ASCII: one column each, one held back for the ellipsis.
    ("abcdefghij", 10, "abcdefghij"),
    ("abcdefghij", 9, "abcdefgh…"),
    ("abcdefghij", 5, "abcd…"),
    # All wide. An odd limit lands on a character boundary once the ellipsis is
    # counted; an even one leaves a column unused rather than overflowing by one.
    (SHORT, 10, SHORT),
    (SHORT, 9, "雨の夜の…"),
    (SHORT, 8, "雨の夜…"),
    (SHORT, 4, "雨…"),
    (SHORT, 3, "雨…"),
    (SHORT, 2, "…"),
    # Mixed: the wide character that would straddle the edge is dropped whole.
    (MIXED, 11, MIXED),
    (MIXED, 8, "No.3 雨…"),
    (MIXED, 7, "No.3 …"),
    # Half-width katakana fits exactly where full-width would not.
    (HALF_KANA, 7, HALF_KANA),
    (HALF_KANA, 5, "ｶﾀｶﾅ…"),
])
def test_truncate_cuts_by_display_columns(text, width, expected):
    """The exact cut for each shape of text. A character-slicing truncate returns SHORT
    untouched at width 9 (five characters, ten columns); a width function that calls
    everything narrow does the same."""
    assert truncate(text, width) == expected


def test_a_twenty_kanji_title_is_cut_to_a_32_column_slot():
    """The case the docstring names: twenty characters fit under 32 if characters are
    what is counted, and forty columns then smear the progress display over two rows.
    Fifteen Kanji are thirty columns, plus the ellipsis is thirty-one."""
    out = truncate(TITLE, 32)

    assert _columns(out) <= 32
    assert out == TITLE[:15] + ELLIPSIS


@pytest.mark.parametrize("text", [TITLE, SHORT, MIXED, HALF_KANA, PLAIN])
def test_no_limit_is_ever_overflowed_and_no_room_is_wasted(text):
    """Every limit from 2 up to past the text's full width, for every shape of text.

    Three things at once: the result never exceeds the limit (a wide character is
    never allowed to straddle the edge); text that fits comes back untouched with no
    ellipsis; and a cut keeps as much as fits, so the next character really would
    not have.
    """
    full = _columns(text)
    for width in range(2, full + 3):
        out = truncate(text, width)

        assert _columns(out) <= width, (width, out)
        if full <= width:
            assert out == text, (width, out)
            continue
        assert out.endswith(ELLIPSIS), (width, out)
        kept = out[:-1]
        assert text.startswith(kept), (width, out)
        next_char = text[len(kept)]
        assert _columns(kept) + _columns(next_char) + 1 > width, (
            f"width {width}: {next_char!r} would still have fitted before the ellipsis")


def test_text_that_fits_gains_no_ellipsis():
    """An ellipsis on a title that was never cut reads as "there is more", which is a
    lie, and costs a column that was not needed."""
    assert truncate(SHORT, 10) == SHORT
    assert truncate(SHORT, 40) == SHORT
    assert truncate("", 5) == ""
    assert ELLIPSIS not in truncate(PLAIN, 100)


@pytest.mark.parametrize("width", [0, 1])
def test_a_one_column_limit_is_still_respected(width):
    """The docstring promises the result fits in ``width`` columns. The budget used to
    floor at one column before the ellipsis, so at width 1 an ASCII first character
    survived and the result was two columns. No caller asks for a limit that small
    today (they use 32, 60 and 100); the promise holds anyway."""
    assert _columns(truncate("abc", width)) <= width
    assert _columns(truncate("雨の夜", width)) <= width


# ---- colour ------------------------------------------------------------------

def test_colour_off_returns_the_text_untouched():
    """A log file, or a console that does not interpret ANSI, must not collect
    escape codes."""
    assert paint("雨の夜", "bold", "red", enabled=False) == "雨の夜"
    assert paint("雨の夜") == "雨の夜"


def test_a_painted_string_always_switches_its_colour_off_again():
    """A missing reset leaves every later line of the console bold and red."""
    assert paint("雨の夜", "bold", "red") == "\033[1m\033[31m雨の夜\033[0m"


def test_an_unknown_style_adds_no_escape_codes_at_all():
    """Not even a lone reset: a typo in a style name should cost the colour, not put
    stray codes in the output."""
    assert paint("雨の夜", "sparkly") == "雨の夜"


class _Stdout:
    """Stands in for sys.stdout where only "is this a terminal?" matters."""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def test_no_color_turns_colour_off_even_on_a_terminal(monkeypatch):
    """The NO_COLOR convention: someone who set it does not want escape codes."""
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=True))
    monkeypatch.setenv("NO_COLOR", "1")

    assert supports_color() is False


def test_redirected_output_gets_no_colour(monkeypatch):
    """A log file should not collect escape codes."""
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=False))
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert supports_color() is False


def test_a_terminal_gets_colour_and_windows_is_switched_into_vt_mode(monkeypatch):
    """Without the colorama call an older conhost prints the escapes literally."""
    calls = []
    fake = types.SimpleNamespace(just_fix_windows_console=lambda: calls.append("fixed"))
    monkeypatch.setitem(sys.modules, "colorama", fake)
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=True))
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert supports_color() is True
    assert calls == ["fixed"]


def test_an_older_colorama_is_initialised_the_old_way(monkeypatch):
    """colorama before 0.4.6 has no just_fix_windows_console; init() is its spelling
    of the same thing."""
    calls = []
    fake = types.SimpleNamespace(init=lambda: calls.append("init"))
    monkeypatch.setitem(sys.modules, "colorama", fake)
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=True))
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert supports_color() is True
    assert calls == ["init"]


def _refusing_colorama():
    def just_fix_windows_console():
        raise RuntimeError("the console refused VT mode")

    return types.SimpleNamespace(just_fix_windows_console=just_fix_windows_console)


@pytest.mark.parametrize("label,colorama", [
    ("not installed", None),   # a None entry in sys.modules makes the import fail
    ("raises", _refusing_colorama()),
])
def test_colorama_is_optional(monkeypatch, label, colorama):
    """A slim install without colorama still runs, and still gets colour: raw ANSI
    works on any modern terminal. Nothing about colour is worth failing startup."""
    monkeypatch.setitem(sys.modules, "colorama", colorama)
    monkeypatch.setattr(sys, "stdout", _Stdout(tty=True))
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert supports_color() is True, label


# ---- UTF-8 on a Windows console ----------------------------------------------

def test_a_cp1252_console_can_print_japanese_after_the_fix(monkeypatch):
    """The Windows hazard: the console defaults to cp1252, and printing a Japanese
    title raises UnicodeEncodeError out of whatever tried to print it."""
    streams = {}
    for name in ("stdout", "stderr"):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with pytest.raises(UnicodeEncodeError):   # the fake really has the hazard
            stream.write("雨")
        streams[name] = stream
        monkeypatch.setattr(sys, name, stream)

    force_utf8_stdio()

    for name, stream in streams.items():
        stream.write("雨の夜")
        stream.flush()
        assert stream.buffer.getvalue() == "雨の夜".encode("utf-8"), name


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(monkeypatch):
    """Under a test runner, an IDE or pythonw, stdout may be something with no
    reconfigure(), or one that refuses. That must not stop the server starting."""
    class Refuses(_Stdout):
        def reconfigure(self, **kw):
            raise ValueError("cannot reconfigure this stream")

    monkeypatch.setattr(sys, "stdout", _Stdout(tty=False))   # no reconfigure at all
    monkeypatch.setattr(sys, "stderr", Refuses(tty=False))

    force_utf8_stdio()


# ---- the renderer that uses all of this --------------------------------------
# conftest silences server/console.py for every other test (so job tests do not paint
# progress bars into pytest's output). These are the tests that exercise it directly.

@pytest.fixture
def terminal(monkeypatch):
    """Capture what the renderer writes, with colour off and fresh render state."""
    written: list[str] = []
    monkeypatch.setattr(console, "_write", written.append)
    monkeypatch.setattr(console, "_COLOR", False)
    monkeypatch.setattr(console, "_banner_shown", True)
    monkeypatch.setattr(console, "_state", {})
    monkeypatch.setattr(console, "_last_flush", {})
    monkeypatch.setattr(console, "_open_line", False)
    return written


def _start(title: str) -> dict:
    return {"type": "start", "index": 3, "label": "ch 3", "title": title}


def test_a_long_japanese_title_fits_its_slot_in_the_terminal_header(terminal):
    """The whole reason the width maths exists, seen where it is used."""
    console.print_event("p1", _start(TITLE))

    head = "".join(terminal).splitlines()[0]
    shown = head.split("ch 3  ", 1)[1]
    assert _columns(shown) <= console._TITLE_WIDTH
    assert shown == TITLE[:15] + ELLIPSIS


def test_a_long_error_is_cut_to_sixty_columns(terminal):
    """A failed chapter's error is one line in the terminal; the full text is in the
    app and in logs/errors.log. Forty-five characters is under sixty, but ninety
    columns is a line and a half."""
    console.print_event("p1", _start("雨の夜"))
    terminal.clear()

    console.print_event("p1", {"type": "item", "status": "failed", "error": ERROR})

    line = "".join(terminal).rstrip("\n")
    shown = line.rsplit("  ", 1)[1]
    assert _columns(shown) <= 60
    assert shown == ERROR[:29] + ELLIPSIS


def test_a_painted_title_is_cut_before_it_is_painted(terminal, monkeypatch):
    """Cut after painting and the escape codes are counted as columns, and the cut can
    land on the reset, leaving the rest of the console bold. This order is also why
    visible_width never needs to skip escapes."""
    monkeypatch.setattr(console, "_COLOR", True)

    console.print_event("p1", _start(TITLE))

    painted = re.search(r"\x1b\[1m(.*?)\x1b\[0m", "".join(terminal))
    assert painted, "the title lost its reset code"
    assert _columns(painted.group(1)) <= console._TITLE_WIDTH
    assert painted.group(1) == TITLE[:15] + ELLIPSIS


def test_a_malformed_event_never_raises_out_of_the_renderer(terminal):
    """Terminal output is cosmetic. A bad value in an event must cost a line on screen,
    never the job that published it."""
    console.print_event("p1", _start("雨の夜"))

    console.print_event("p1", {"type": "progress", "done": "twelve", "total": 47})
    console.print_event("p1", {"type": "no-such-event"})
    console.print_event("p1", {})
