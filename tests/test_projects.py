"""Tests for project storage.

Project ids are server-generated hex and never user-controlled, which is what stops a
pasted title from becoming a filesystem path. The other thing pinned here is that
``kind`` is required and validated: a manga silently created as a novel would take the
flatten path and lose every region it ever reads.
"""

from __future__ import annotations

import json

import pytest

from morning.chapters import Chapter
from morning.config import Config
from server import projects as pj

JA = "電車はまだ来ない。"


def _chapters(n: int = 2) -> list[Chapter]:
    return [Chapter(index=i, title=f"第{i}話", paragraphs=[JA])
            for i in range(1, n + 1)]


# ---- creating ----------------------------------------------------------------

def test_a_new_project_gets_a_server_generated_id():
    project = pj.create_project("朝の駅")

    assert len(project["id"]) == 12
    assert all(c in "0123456789abcdef" for c in project["id"])


def test_a_pasted_title_never_becomes_a_path():
    """The reason ids are generated rather than slugged. A title can contain a slash,
    a colon, a null, or characters Windows refuses outright."""
    project = pj.create_project("../../etc/passwd: 朝の駅 <>|?*")

    assert pj.project_dir(project["id"]).parent == pj.PROJECTS_DIR
    assert pj.get_project(project["id"])["title"].startswith("../../etc")


def test_an_empty_title_gets_a_placeholder():
    assert pj.create_project("   ")["title"] == "Untitled"


def test_both_kinds_can_be_created():
    novel = pj.create_project("a novel", kind=pj.KIND_NOVEL)
    manga = pj.create_project("a manga", kind=pj.KIND_MANGA, ingest=pj.INGEST_IMAGES)

    assert novel["kind"] == "novel"
    assert manga["kind"] == "manga"


@pytest.mark.parametrize("ingest", [pj.INGEST_TEXT, pj.INGEST_DOCS, pj.INGEST_EXPORT])
def test_a_manga_is_only_ever_made_from_its_pictures(ingest):
    """A manga made from text gets a prose source and no pages: the manga routes read
    pages and the prose routes refuse a manga, so it could be neither translated nor
    read, and nothing would say why."""
    with pytest.raises(ValueError, match="page images"):
        pj.create_project("x", kind=pj.KIND_MANGA, ingest=ingest)

    assert pj.list_projects() == []


@pytest.mark.parametrize("ingest", [pj.INGEST_TEXT, pj.INGEST_DOCS, pj.INGEST_IMAGES])
def test_a_novel_can_come_from_anywhere(ingest):
    assert pj.create_project("x", ingest=ingest)["ingest"] == ingest


def test_an_unknown_kind_is_refused_rather_than_defaulted():
    """Defaulting would give a manga the novel pipeline, which flattens regions away —
    the loss would be silent and total."""
    with pytest.raises(ValueError, match="unknown kind"):
        pj.create_project("x", kind="comic")


def test_an_unknown_ingestion_is_refused():
    with pytest.raises(ValueError, match="unknown ingestion"):
        pj.create_project("x", ingest="telepathy")


def test_a_new_project_has_its_folders():
    project = pj.create_project("x")
    pdir = pj.project_dir(project["id"])

    assert (pdir / "chapters").is_dir()
    assert (pdir / "audit").is_dir()
    assert (pdir / "project.json").is_file()


def test_an_image_project_also_gets_a_pages_folder():
    project = pj.create_project("x", ingest=pj.INGEST_IMAGES)

    assert (pj.project_dir(project["id"]) / "pages").is_dir()


def test_a_new_project_starts_ongoing_and_unarchived():
    project = pj.create_project("x")

    assert project["status"] == "ongoing"
    assert project["archived"] is False


# ---- reading -----------------------------------------------------------------

def test_a_project_can_be_read_back():
    created = pj.create_project("朝の駅", kind=pj.KIND_MANGA, ingest=pj.INGEST_IMAGES)

    assert pj.get_project(created["id"]) == created


def test_an_unknown_id_reads_as_missing():
    assert pj.get_project("0123456789ab") is None


def test_a_malformed_id_reads_as_missing_without_touching_the_disk():
    """The id pattern is the traversal guard. Anything that is not twelve hex
    characters must not reach the filesystem at all."""
    for bad in ("../../etc", "..", "", "x" * 12, "0123456789ABCD", None):
        assert pj.get_project(bad) is None


def test_a_truncated_project_file_reads_as_missing(monkeypatch):
    """A half-written project.json must not turn every request for that project into
    a 500 — it is treated as missing, exactly as the library listing treats it."""
    project = pj.create_project("x")
    (pj.project_dir(project["id"]) / "project.json").write_text(
        '{"id": "abc", trunc', encoding="utf-8")

    assert pj.get_project(project["id"]) is None
    assert pj.list_projects() == []


def test_listing_is_ordered_by_creation():
    first = pj.create_project("first")
    second = pj.create_project("second")

    assert [p["id"] for p in pj.list_projects()] == [first["id"], second["id"]]


def test_listing_skips_unrelated_folders():
    pj.create_project("real")
    (pj.PROJECTS_DIR / "not-a-project").mkdir()

    assert len(pj.list_projects()) == 1


# ---- updating ----------------------------------------------------------------

def test_editable_fields_are_patched():
    project = pj.create_project("before")
    updated = pj.update_project(project["id"], title="after",
                                style_note="a light novel", status="completed",
                                archived=True)

    assert updated["title"] == "after"
    assert updated["style_note"] == "a light novel"
    assert updated["status"] == "completed"
    assert updated["archived"] is True


def test_server_managed_fields_cannot_be_patched():
    """`kind` especially: a novel cannot become a manga, and letting it change would
    leave the project's stored pages meaning something they do not."""
    project = pj.create_project("x", kind=pj.KIND_NOVEL)
    updated = pj.update_project(project["id"], kind=pj.KIND_MANGA,
                                id="ffffffffffff", created_at="1999")

    assert updated["kind"] == pj.KIND_NOVEL
    assert updated["id"] == project["id"]
    assert updated["created_at"] == project["created_at"]


def test_an_unknown_status_is_ignored_rather_than_stored():
    project = pj.create_project("x")
    updated = pj.update_project(project["id"], status="on fire")

    assert updated["status"] == "ongoing"


def test_blanking_the_title_keeps_the_old_one():
    project = pj.create_project("a real title")

    assert pj.update_project(project["id"], title="   ")["title"] == "a real title"


def test_updating_an_unknown_project_raises():
    with pytest.raises(KeyError):
        pj.update_project("0123456789ab", title="x")


def test_unknown_keys_are_ignored_not_rejected():
    """The frontend sends whole records back; a new server field must not 400 an old
    interface."""
    project = pj.create_project("x")
    pj.update_project(project["id"], something_new_in_a_later_version=True)


# ---- deleting ----------------------------------------------------------------

def test_a_project_can_be_deleted():
    project = pj.create_project("x")

    assert pj.delete_project(project["id"]) is True
    assert pj.get_project(project["id"]) is None


def test_deleting_an_unknown_project_reports_false():
    assert pj.delete_project("0123456789ab") is False


def test_delete_refuses_anything_that_is_not_a_project_id():
    """The one operation here that is unrecoverable, so it is guarded by the id
    pattern AND by resolving inside the projects directory."""
    outside = pj.PROJECTS_DIR.parent / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("important", encoding="utf-8")

    for bad in ("../precious", "..", "", "x" * 12, "/etc"):
        assert pj.delete_project(bad) is False

    assert (outside / "keep.txt").exists()


# ---- the source snapshot -----------------------------------------------------

def test_source_round_trips():
    project = pj.create_project("x")
    pj.save_source(project["id"], _chapters(3))
    back = pj.load_source(project["id"])

    assert [c.index for c in back] == [1, 2, 3]
    assert [c.title for c in back] == ["第1話", "第2話", "第3話"]
    assert back[0].paragraphs == [JA]


def test_source_is_written_unescaped():
    """The whole shelf is Japanese; an escaped file would be three times the size and
    unreadable to anyone diagnosing a problem by opening it."""
    project = pj.create_project("x")
    pj.save_source(project["id"], _chapters(1))

    raw = pj.source_file(project["id"]).read_text(encoding="utf-8")
    assert JA in raw
    assert "\\u" not in raw


def test_a_project_with_no_source_reads_as_empty():
    project = pj.create_project("x")
    assert pj.load_source(project["id"]) == []


def test_a_corrupt_source_reads_as_empty_rather_than_raising():
    project = pj.create_project("x")
    pj.save_source(project["id"], _chapters(2))
    pj.source_file(project["id"]).write_text("{ truncated", encoding="utf-8")

    assert pj.load_source(project["id"]) == []


def test_chapter_count_is_recorded_on_the_project():
    project = pj.create_project("x")
    pj.save_source(project["id"], _chapters(5))
    updated = pj.set_chapter_count(project["id"], 5)

    assert updated["chapter_count"] == 5
    assert pj.get_project(project["id"])["chapter_count"] == 5


# ---- effective config --------------------------------------------------------

def test_project_config_points_every_path_inside_the_project():
    project = pj.create_project("x")
    cfg = pj.project_config(Config(), project)
    pdir = pj.project_dir(project["id"])

    assert cfg.paths.state_file == pdir / "state.json"
    assert cfg.paths.output_dir == pdir / "chapters"
    assert cfg.paths.audit_dir == pdir / "audit"
    assert cfg.paths.glossary_json == pdir / "glossary.json"


def test_project_overrides_are_overlaid():
    project = pj.create_project("x")
    pj.update_project(project["id"], style_note="a light novel",
                      instructions="render sound effects in italics",
                      honorific_note="keep -senpai")
    cfg = pj.project_config(Config(), pj.get_project(project["id"]))

    assert cfg.translation.style_note == "a light novel"
    assert cfg.translation.extra_instruction == "render sound effects in italics"
    assert cfg.translation.honorific_note == "keep -senpai"


def test_project_config_does_not_mutate_the_global():
    """Two projects open at once must not see each other's framing."""
    globals_ = Config()
    a = pj.create_project("a")
    pj.update_project(a["id"], style_note="a light novel")
    pj.project_config(globals_, pj.get_project(a["id"]))

    assert globals_.translation.style_note == ""
    assert globals_.paths.state_file.name == "state.json"


def test_two_projects_never_share_a_glossary():
    """Novels and manga are unrelated works, so there is no cross-project merge and
    no shared glossary — by construction, not by policy."""
    a = pj.create_project("a")
    b = pj.create_project("b", kind=pj.KIND_MANGA, ingest=pj.INGEST_IMAGES)
    cfg_a = pj.project_config(Config(), a)
    cfg_b = pj.project_config(Config(), b)

    assert cfg_a.paths.glossary_json != cfg_b.paths.glossary_json


def test_the_project_record_is_written_atomically():
    """Same discipline as everything else persisted here."""
    project = pj.create_project("x")
    path = pj.project_dir(project["id"]) / "project.json"

    json.loads(path.read_text(encoding="utf-8"))
    assert list(pj.project_dir(project["id"]).glob("*.tmp")) == []
