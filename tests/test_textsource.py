"""Tests for the paste / .txt ingestion path — the one step 1 is proved with.

Its job is to turn arbitrary text into the Chapter contract, so the important cases
are the ones where a naive split loses or invents a chapter. All Japanese here is
invented for these tests.
"""

from __future__ import annotations

from morning.textsource import (
    decode_upload, looks_like_heading, make_chapter, split_text_into_chapters,
)

JA_A = "電車はまだ来ない。"
JA_B = "雨が降り始めた。"


# ---- heading recognition -----------------------------------------------------

def test_japanese_chapter_counters_are_recognised():
    """第?N[話章巻部回] — the direct analogue of the Korean 제?N[화장권부]. Three of the
    four counters are the SAME characters; only 話 differs from 화 in script."""
    for heading in ("第3話", "3話", "第1章", "第2巻",
                    "第4部", "第5回", "第三章",
                    "第十一話"):
        assert looks_like_heading(heading), heading


def test_japanese_kind_words_are_recognised():
    """序章 / 終章 / 外伝 are the same Kanji as 서장 / 종장 / 외전."""
    for heading in ("序章", "終章", "外伝", "間章",
                    "プロローグ", "エピローグ"):
        assert looks_like_heading(heading), heading


def test_english_chapter_conventions_are_recognised():
    for heading in ("Chapter 4", "ch. 7", "Episode 12", "Prologue", "Volume 2"):
        assert looks_like_heading(heading), heading


def test_ordinary_prose_is_not_a_heading():
    for line in (JA_A, "彼女は窓の外を見ていた。",
                 "The train has not come yet.", "", "   "):
        assert not looks_like_heading(line), line


def test_a_heading_with_a_title_after_it_is_still_a_heading():
    assert looks_like_heading("第3話　朝の駅")
    assert looks_like_heading("Chapter 3: The Morning Station")


# ---- heading mode ------------------------------------------------------------

def test_heading_mode_splits_on_headings_and_takes_them_as_titles():
    text = f"第1話\n\n{JA_A}\n\n第2話\n\n{JA_B}"
    chapters = split_text_into_chapters(text, mode="heading")

    assert [c.index for c in chapters] == [1, 2]
    assert [c.title for c in chapters] == ["第1話", "第2話"]
    assert [c.paragraphs for c in chapters] == [[JA_A], [JA_B]]


def test_heading_mode_keeps_a_preamble_before_the_first_heading():
    """An author's note at the top of the file is content, not noise. Dropping it
    silently would lose text the user pasted."""
    text = f"この作品はフィクションです。\n\n第1話\n\n{JA_A}"
    chapters = split_text_into_chapters(text, mode="heading")

    assert len(chapters) == 2
    assert chapters[0].title == "Chapter 1"
    assert chapters[1].title == "第1話"


def test_heading_mode_with_no_headings_falls_back_to_one_chapter():
    """Returning nothing would read to the user as "the paste failed"."""
    chapters = split_text_into_chapters(f"{JA_A}\n\n{JA_B}", mode="heading")

    assert len(chapters) == 1
    assert chapters[0].paragraphs == [JA_A, JA_B]


# ---- separator mode ----------------------------------------------------------

def test_separator_mode_splits_on_marker_lines():
    text = f"{JA_A}\n---\n{JA_B}"
    chapters = split_text_into_chapters(text, mode="separator")

    assert len(chapters) == 2


def test_the_common_marker_runs_all_work_without_configuring_them():
    for marker in ("---", "===", "***", "###", "___", "-------"):
        chapters = split_text_into_chapters(f"{JA_A}\n{marker}\n{JA_B}")
        assert len(chapters) == 2, marker


def test_a_custom_separator_is_honoured():
    chapters = split_text_into_chapters(f"{JA_A}\n<<BREAK>>\n{JA_B}",
                                        separator="<<BREAK>>")
    assert len(chapters) == 2


def test_separator_mode_with_no_separators_yields_one_chapter():
    chapters = split_text_into_chapters(f"{JA_A}\n\n{JA_B}")

    assert len(chapters) == 1
    assert chapters[0].paragraphs == [JA_A, JA_B]


def test_empty_blocks_between_separators_are_dropped():
    """Two markers in a row, or a trailing one, must not produce empty chapters that
    then sit in the list forever saying nothing."""
    chapters = split_text_into_chapters(f"{JA_A}\n---\n---\n{JA_B}\n---\n")

    assert len(chapters) == 2
    assert [c.index for c in chapters] == [1, 2]


def test_a_short_first_line_becomes_the_title():
    chapters = split_text_into_chapters(f"朝の駅\n{JA_A}")

    assert chapters[0].title == "朝の駅"
    assert chapters[0].paragraphs == [JA_A]


def test_a_long_first_line_of_prose_is_not_stolen_as_a_title():
    """The threshold is tighter than the Korean app's, because a Japanese line packs
    far more into the same character count — a generous limit swallows the opening
    sentence of the chapter."""
    prose = JA_A * 4
    chapters = split_text_into_chapters(f"{prose}\n\n{JA_B}")

    assert chapters[0].title == "Chapter 1"
    assert prose in chapters[0].paragraphs


# ---- single mode -------------------------------------------------------------

def test_single_mode_keeps_everything_as_one_chapter():
    text = f"第1話\n\n{JA_A}\n\n第2話\n\n{JA_B}"
    chapters = split_text_into_chapters(text, mode="single")

    assert len(chapters) == 1
    assert len(chapters[0].paragraphs) == 4


# ---- edges -------------------------------------------------------------------

def test_empty_input_yields_no_chapters():
    for text in ("", "   ", "\n\n\n", None):
        assert split_text_into_chapters(text) == []


def test_windows_and_old_mac_line_endings_are_normalised():
    crlf = split_text_into_chapters(f"{JA_A}\r\n---\r\n{JA_B}")
    cr = split_text_into_chapters(f"{JA_A}\r---\r{JA_B}")

    assert len(crlf) == 2 and len(cr) == 2
    assert crlf[0].paragraphs == [JA_A]


def test_a_full_width_space_still_separates_a_heading_from_its_title():
    chapters = split_text_into_chapters(
        f"第1話　朝の駅\n\n{JA_A}", mode="heading")

    assert chapters[0].title == "第1話　朝の駅"


def test_make_chapter_refuses_an_empty_body():
    assert make_chapter(1, "title", "   \n\n  ") is None
    assert make_chapter(1, "title", JA_A) is not None


def test_a_chapter_with_no_title_gets_a_numbered_one():
    assert make_chapter(4, "", JA_A).title == "Chapter 4"


# ---- decoding ----------------------------------------------------------------

def test_utf8_is_decoded():
    assert decode_upload(JA_A.encode("utf-8")) == JA_A


def test_a_utf8_bom_is_stripped():
    """Notepad and Excel both write one. Left in, it becomes an invisible first
    character of the first chapter's title."""
    assert decode_upload(b"\xef\xbb\xbf" + JA_A.encode("utf-8")) == JA_A


def test_shift_jis_is_decoded():
    """Still the Windows Japanese codepage, and still what Notepad-era .txt files
    are. Assuming UTF-8 turns a perfectly good file into mojibake."""
    assert decode_upload(JA_A.encode("cp932")) == JA_A


def test_euc_jp_is_decoded():
    assert decode_upload(JA_A.encode("euc_jp")) == JA_A


def test_undecodable_bytes_still_produce_text_rather_than_raising():
    """A file in none of the three encodings must land as something a human can look
    at and fix, not as a 500."""
    out = decode_upload(b"\xff\xfe\x00\x01\x02\x03plain ascii tail")

    assert isinstance(out, str)
    assert "plain ascii tail" in out


def test_a_decoded_upload_splits_into_chapters():
    """The two halves join up: decode then split is the whole .txt path."""
    text = f"第1話\n\n{JA_A}\n\n第2話\n\n{JA_B}"
    chapters = split_text_into_chapters(decode_upload(text.encode("cp932")),
                                        mode="heading")

    assert [c.title for c in chapters] == ["第1話", "第2話"]
