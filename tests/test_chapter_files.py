"""Tests for chapter output files.

Nearly all of these are about one failure: the canonical filename depends on the
chapter COUNT, so it moves when a work crosses a digit boundary, and anything holding
a count captured earlier writes at the old width. That produced two files for one
chapter, a reader that showed the wrong one, a snapshot of neither, and a translation
that could be neither read nor recovered.

Every lookup resolves by index. These pin that.
"""

from __future__ import annotations

import threading

from morning.chapter_files import (
    AUDIT_TRANSLATION_HEADING, chapter_filename, chapter_path, existing_chapter_file,
    has_previous, output_total, pad_width, previous_chapter_path,
    read_audit_translation, read_chapter, write_audit, write_chapter_file,
)
from morning.chapters import Chapter

JA = "電車はまだ来ない。"  # invented
EN = "The train has not come yet."


# ---- naming ------------------------------------------------------------------

def test_the_width_follows_the_works_size():
    assert pad_width(9) == 2       # a floor, so a short work still sorts
    assert pad_width(99) == 2
    assert pad_width(100) == 3
    assert pad_width(1000) == 4


def test_the_filename_is_padded_to_that_width():
    assert chapter_filename(7, 99) == "chapter-07.md"
    assert chapter_filename(7, 100) == "chapter-007.md"
    assert chapter_filename(250, 1000) == "chapter-0250.md"


def test_the_canonical_name_changes_when_the_work_grows():
    """The root of the whole problem, stated as a test so it is not a surprise."""
    assert chapter_filename(50, 99) != chapter_filename(50, 100)


# ---- resolving by index ------------------------------------------------------

def test_a_chapter_is_found_at_a_width_it_was_not_written_at(tmp_path):
    """The defect. A file written when the work had 99 chapters must still be found
    once it has 100 — otherwise a finished translation goes invisible."""
    (tmp_path / "chapter-50.md").write_text(EN, encoding="utf-8")

    assert existing_chapter_file(tmp_path, 50).name == "chapter-50.md"
    assert chapter_path(tmp_path, 50, 100).name == "chapter-50.md"
    assert read_chapter(tmp_path, 50, 100) == EN


def test_writing_with_a_stale_count_reuses_the_existing_file(tmp_path):
    """The consequence that cost a translation: a worker holding an old count must
    not create a SECOND file for a chapter that already has one."""
    write_chapter_file(tmp_path, 50, 100, "first version")     # chapter-050.md
    write_chapter_file(tmp_path, 50, 99, "second version")     # stale count

    files = sorted(p.name for p in tmp_path.glob("chapter-*.md"))
    assert files == ["chapter-050.md"], f"a second file was created: {files}"
    assert read_chapter(tmp_path, 50, 99) == "second version\n"


def test_the_canonical_name_wins_when_both_somehow_exist(tmp_path):
    """Belt and braces for a folder restored from a backup that already has both."""
    (tmp_path / "chapter-50.md").write_text("old width", encoding="utf-8")
    (tmp_path / "chapter-050.md").write_text("canonical", encoding="utf-8")

    assert chapter_path(tmp_path, 50, 100).name == "chapter-050.md"


def test_an_absent_chapter_resolves_to_the_canonical_name_to_create(tmp_path):
    assert chapter_path(tmp_path, 3, 100).name == "chapter-003.md"


def test_a_missing_directory_does_not_raise(tmp_path):
    assert existing_chapter_file(tmp_path / "nope", 1) is None
    assert read_chapter(tmp_path / "nope", 1, 10) is None


def test_files_that_are_not_chapters_are_ignored(tmp_path):
    (tmp_path / "chapter-notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "chapter-.md").write_text("x", encoding="utf-8")

    assert existing_chapter_file(tmp_path, 1) is None


def test_output_total_never_narrows_below_what_is_on_disk(tmp_path):
    """A folder can hold files from when the work was LARGER — chapters removed from
    the source, or a restored backup. Narrowing the width while those exist would
    create a second file for a chapter that already has one."""
    (tmp_path / "chapter-0250.md").write_text("x", encoding="utf-8")

    assert output_total(tmp_path, 12) == 250
    assert output_total(tmp_path, 900) == 900


def test_output_total_on_an_empty_folder_is_the_count(tmp_path):
    assert output_total(tmp_path, 42) == 42


# ---- writing -----------------------------------------------------------------

def test_a_chapter_is_written_and_read_back(tmp_path):
    path = write_chapter_file(tmp_path, 1, 10, EN)

    assert path.name == "chapter-01.md"
    assert read_chapter(tmp_path, 1, 10) == EN + "\n"


def test_the_directory_is_created(tmp_path):
    write_chapter_file(tmp_path / "chapters", 1, 10, EN)

    assert (tmp_path / "chapters" / "chapter-01.md").exists()


def test_curly_quotes_are_preserved(tmp_path):
    """The prompt asks for them specifically. Any normalisation here would silently
    undo the thing the prompt spent a paragraph requesting."""
    prose = "“Late, is it not,” he said. ‘Yes,’ she thought…"
    write_chapter_file(tmp_path, 1, 10, prose)

    assert read_chapter(tmp_path, 1, 10).startswith(prose)


def test_trailing_whitespace_is_trimmed_to_exactly_one_newline(tmp_path):
    write_chapter_file(tmp_path, 1, 10, EN + "\n\n\n   \n")

    assert read_chapter(tmp_path, 1, 10) == EN + "\n"


def test_overwriting_snapshots_the_previous_version(tmp_path):
    """So the reader can show old-vs-new and offer a revert."""
    chapters = tmp_path / "chapters"
    write_chapter_file(chapters, 1, 10, "first")
    write_chapter_file(chapters, 1, 10, "second")

    assert has_previous(chapters, 1, 10)
    assert previous_chapter_path(chapters, 1, 10).read_text(encoding="utf-8") == "first\n"
    assert read_chapter(chapters, 1, 10) == "second\n"


def test_the_snapshot_lives_outside_the_chapters_folder(tmp_path):
    """Kept out of chapters/ so it never matches the chapter-*.md globs a build or an
    export uses — otherwise every chapter would appear twice in the finished book."""
    chapters = tmp_path / "chapters"
    write_chapter_file(chapters, 1, 10, "first")
    write_chapter_file(chapters, 1, 10, "second")

    assert sorted(p.name for p in chapters.glob("chapter-*.md")) == ["chapter-01.md"]
    assert (tmp_path / "previous" / "chapter-01.md").exists()


def test_the_first_write_snapshots_nothing(tmp_path):
    chapters = tmp_path / "chapters"
    write_chapter_file(chapters, 1, 10, "first")

    assert not has_previous(chapters, 1, 10)


def test_snapshot_can_be_declined(tmp_path):
    """For edits that keep their OWN history. Letting each per-paragraph pick
    overwrite the single previous/ slot would destroy the whole-chapter snapshot taken
    before the last translation after one click."""
    chapters = tmp_path / "chapters"
    write_chapter_file(chapters, 1, 10, "first")
    write_chapter_file(chapters, 1, 10, "second")
    write_chapter_file(chapters, 1, 10, "third", snapshot=False)

    assert previous_chapter_path(chapters, 1, 10).read_text(encoding="utf-8") == "first\n"


def test_a_snapshot_taken_at_another_width_is_still_found(tmp_path):
    """Resolved by index for the same reason as everything else: a snapshot carries
    the width that was current when it was taken, and looking for today's width would
    report "no previous version" for one sitting right there."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "chapter-50.md").write_text("old\n", encoding="utf-8")

    assert has_previous(chapters, 50, 100)
    assert previous_chapter_path(chapters, 50, 100).read_text(encoding="utf-8") == "old\n"


def test_concurrent_writes_to_one_chapter_do_not_interleave(tmp_path):
    """Guarded inside the writer rather than at each call site, because there will be
    several call sites and one that forgets silently loses a translation."""
    barrier = threading.Barrier(6)
    raised: list[BaseException] = []

    def write(i: int) -> None:
        barrier.wait()
        try:
            write_chapter_file(tmp_path, 1, 10, f"version {i}\n" * 200)
        except BaseException as exc:  # noqa: BLE001
            raised.append(exc)

    pool = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    assert not raised, f"a concurrent write raised: {raised[0]!r}"
    # Whichever won, the file holds exactly ONE version, not a splice of several.
    text = read_chapter(tmp_path, 1, 10)
    assert len({line for line in text.split("\n") if line.strip()}) == 1


# ---- the audit trail ---------------------------------------------------------

def _chapter() -> Chapter:
    return Chapter(index=1, title="第1話", paragraphs=[JA, JA])


def test_the_audit_records_source_and_translation_together(tmp_path):
    write_audit(tmp_path, 1, 10, chapter=_chapter(), english=EN)
    text = (tmp_path / "chapter-01.md").read_text(encoding="utf-8")

    assert "第1話" in text
    assert JA in text
    assert EN in text


def test_the_translation_is_recoverable_from_the_audit(tmp_path):
    """For a needs-review chapter this is the ONLY copy — such a chapter never reaches
    chapters/, precisely so a questionable translation cannot be mistaken for a
    finished one. Without this it would be invisible and un-acceptable."""
    write_audit(tmp_path, 1, 10, chapter=_chapter(), english=EN)

    assert read_audit_translation(tmp_path, 1, 10) == EN


def test_a_multi_paragraph_translation_survives_the_round_trip(tmp_path):
    english = "First paragraph.\n\nSecond paragraph.\n\nThird."
    write_audit(tmp_path, 1, 10, chapter=_chapter(), english=english)

    assert read_audit_translation(tmp_path, 1, 10) == english


def test_notes_are_recorded_when_given(tmp_path):
    write_audit(tmp_path, 1, 10, chapter=_chapter(), english=EN,
                notes=["length ratio outside the band"])
    text = (tmp_path / "chapter-01.md").read_text(encoding="utf-8")

    assert "length ratio outside the band" in text
    # ...and they must not be mistaken for the translation.
    assert read_audit_translation(tmp_path, 1, 10) == EN


def test_an_audit_written_at_another_width_is_still_readable(tmp_path):
    write_audit(tmp_path, 50, 99, chapter=_chapter(), english=EN)

    assert read_audit_translation(tmp_path, 50, 100) == EN


def test_a_missing_audit_reads_as_none(tmp_path):
    assert read_audit_translation(tmp_path, 1, 10) is None


def test_an_audit_without_the_heading_reads_as_none(tmp_path):
    """Rather than returning the whole file, which would put the source text into the
    reader as though it were the translation."""
    (tmp_path / "chapter-01.md").write_text("# something else", encoding="utf-8")

    assert read_audit_translation(tmp_path, 1, 10) is None


def test_the_heading_is_the_one_the_reader_looks_for():
    """Changing it orphans every needs-review chapter already on disk."""
    assert AUDIT_TRANSLATION_HEADING == "## Translation (English)"
