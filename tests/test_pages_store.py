"""The page manifest: uploads, ordering, and the state a page rests at.

The property this file exists for: **a page record carries its whole PageRead, not
flat text.** That is the contract fixed on day one, and storing flat text here would
mean re-reading every page already transcribed once regions were needed.

Japanese fixtures are invented; image headers are built by hand.
"""

from __future__ import annotations

import json

import pytest

from morning.images import ImageInfo, inspect
from morning.pageread import (
    GLUE_NONE, GLUE_SPACE, JOIN_PARAGRAPH, JOIN_SENTENCE, KIND_BODY,
    KIND_PAGE_NUMBER, PageMeta, PageRead, Region, from_pixels,
)
from server import pages as pg
from tests.test_images import jpeg, png

COL_A = "電車はまだ来ない。"
COL_B = "彼女はホームに立っていた。"

W, H = 1600, 2400


@pytest.fixture
def project():
    from server import projects as pj
    return pj.create_project("scans", ingest=pj.INGEST_IMAGES)["id"]


def _add(doc, *, name="page.jpg", data=None, batch="b1"):
    data = data if data is not None else jpeg(W, H)
    return pg.add_page(doc, info=inspect(data), data_len=len(data),
                       digest=pg.sha256_of(data), batch=batch, name=name)


def _read(**meta) -> dict:
    """A PageRead with two prose columns and a page number, as a stored dict."""
    return PageRead(
        width=W, height=H, meta=PageMeta(confidence="high", **meta),
        regions=[
            Region(id="r0", order=0, kind=KIND_BODY, text=COL_A,
                   box=from_pixels(1180, 200, 300, 1900, W, H)),
            Region(id="r1", order=1, kind=KIND_BODY, text=COL_B,
                   join_prev=JOIN_SENTENCE, join_glue=GLUE_NONE,
                   box=from_pixels(820, 200, 300, 1900, W, H)),
            Region(id="r2", order=2, kind=KIND_PAGE_NUMBER, text="12",
                   box=from_pixels(760, 2280, 80, 60, W, H)),
        ],
    ).to_dict()


# ---- the manifest ---------------------------------------------------------------

def test_a_missing_manifest_reads_as_empty(project):
    doc = pg.load_pages(project)

    assert doc["pages"] == []
    assert doc["next_seq"] == 1


def test_the_manifest_round_trips(project):
    with pg.mutate_pages(project) as doc:
        _add(doc, name="朝の駅 001.jpg")

    reloaded = pg.load_pages(project)
    assert len(reloaded["pages"]) == 1
    assert reloaded["pages"][0]["name"] == "朝の駅 001.jpg"


def test_a_japanese_filename_survives_as_a_label(project):
    """A scan is quite likely to be named in Japanese. Stripping that would leave the
    user with a page called "1"."""
    assert pg.safe_label("第一巻 表紙.jpg") == \
        "第一巻 表紙.jpg"


def test_a_label_can_never_become_a_path(project):
    for hostile in ("../../etc/passwd", "a/b\\c", "page\x00.jpg"):
        label = pg.safe_label(hostile)
        assert "/" not in label and "\\" not in label and "\x00" not in label


def test_a_corrupt_manifest_degrades_and_keeps_the_bytes(project):
    """Degrading stops one bad file taking down the project. Quarantining first is
    what stops a momentary read failure from erasing every page transcribed from a
    photo — the caller mutates this and saves it straight back."""
    path = pg.pages_file(project)
    path.write_text('{"pages": [truncated', encoding="utf-8")

    assert pg.load_pages(project)["pages"] == []
    assert len(list(path.parent.glob("pages.json.unreadable-*"))) == 1


def test_a_manifest_of_the_wrong_shape_degrades(project):
    pg.pages_file(project).write_text('["not", "a", "doc"]', encoding="utf-8")

    assert pg.load_pages(project)["pages"] == []


def test_next_seq_is_rebuilt_from_the_pages_when_it_is_missing(project):
    pg.pages_file(project).write_text(
        json.dumps({"pages": [{"id": "a" * 8, "seq": 7}]}), encoding="utf-8")

    assert pg.load_pages(project)["next_seq"] == 8


# ---- adding pages ----------------------------------------------------------------

def test_a_page_carries_its_dimensions_from_the_moment_it_exists(project):
    """A page whose size is unknown cannot anchor an overlay, and there is no later
    chance to measure the bytes without re-reading them."""
    with pg.mutate_pages(project) as doc:
        page = _add(doc)

    assert (page["width"], page["height"]) == (W, H)


def test_the_stored_extension_comes_from_the_bytes(project):
    """Not from the filename. A .jpg that is really a PNG would otherwise be served
    with the wrong type."""
    data = png(100, 200)
    with pg.mutate_pages(project) as doc:
        page = pg.add_page(doc, info=inspect(data), data_len=len(data),
                           digest=pg.sha256_of(data), batch="b", name="scan.jpg")

    assert page["file"].endswith(".png")


def test_the_filename_carries_the_sequence_not_the_position(project):
    """Reordering rewrites the manifest only. Renaming files would break image
    caching in the browser and race an in-flight read holding a path."""
    with pg.mutate_pages(project) as doc:
        first, second = _add(doc), _add(doc)
        pg.reorder(doc, [second["id"], first["id"]])

    pages = pg.load_pages(project)["pages"]
    assert [p["file"] for p in pages] == ["page-0002.jpg", "page-0001.jpg"]


def test_a_page_starts_new_with_no_read(project):
    with pg.mutate_pages(project) as doc:
        page = _add(doc)

    assert page["status"] == pg.STATUS_NEW
    assert page["read"] is None
    assert pg.page_text(page) == ""


def test_the_same_image_uploaded_twice_is_recognised(project):
    """Re-uploading a folder is ordinary. Charging to read the same page twice is
    not."""
    data = jpeg(W, H)
    with pg.mutate_pages(project) as doc:
        _add(doc, data=data)
        assert pg.find_by_hash(doc, pg.sha256_of(data)) is not None
        assert pg.find_by_hash(doc, pg.sha256_of(jpeg(10, 10))) is None


def test_batches_count_their_pages(project):
    with pg.mutate_pages(project) as doc:
        batch = pg.new_batch(doc, "last night")
        _add(doc, batch=batch)
        _add(doc, batch=batch)

    assert pg.load_pages(project)["batches"][0]["count"] == 2


# ---- the page read ----------------------------------------------------------------

def test_a_page_stores_its_whole_read_not_flat_text(project):
    """The contract. Regions with geometry, from day one."""
    with pg.mutate_pages(project) as doc:
        page = _add(doc)
        page["read"] = _read()

    stored = pg.load_pages(project)["pages"][0]["read"]
    assert stored["width"] == W and stored["height"] == H
    assert len(stored["regions"]) == 3
    assert stored["regions"][0]["box"][0] < 1, "boxes are fractions, not pixels"


def test_a_novel_flattens_the_regions_into_prose(project):
    """What makes a scanned novel indistinguishable from a pasted one by the time it
    reaches the pipeline."""
    page = {"read": _read()}

    assert pg.page_text(page) == COL_A + COL_B
    assert "12" not in pg.page_text(page), "the page number is not part of the novel"


def test_a_page_with_no_read_has_no_text():
    assert pg.page_text({"read": None}) == ""
    assert pg.page_chars({}) == 0


def test_a_malformed_read_does_not_crash_the_text():
    """A page transcribed at real cost must not become unreadable because one field
    came back the wrong shape."""
    assert pg.page_text({"read": {"regions": "not a list"}}) == ""


# ---- seams --------------------------------------------------------------------------

def test_a_proposed_seam_is_recorded_with_its_reason(project):
    page = {"join_prev_source": ""}

    assert pg.set_join(page, JOIN_SENTENCE, reason="the sentence runs on") is True
    assert page["join_prev"] == JOIN_SENTENCE
    assert page["join_prev_source"] == "model"


def test_a_human_decision_is_never_overwritten_by_a_later_proposal():
    """The rule carried from the page seams in the app this derives from: once
    `join_prev_source` is "user", a re-read must not silently undo it — the person
    would have no reason to look again."""
    page = {"join_prev_source": ""}
    pg.set_join(page, JOIN_SENTENCE, by_user=True)

    assert pg.set_join(page, JOIN_PARAGRAPH) is False
    assert page["join_prev"] == JOIN_SENTENCE
    assert page["join_prev_source"] == "user"


def test_a_human_can_change_their_own_mind():
    page = {"join_prev_source": ""}
    pg.set_join(page, JOIN_SENTENCE, by_user=True)

    assert pg.set_join(page, JOIN_PARAGRAPH, by_user=True) is True
    assert page["join_prev"] == JOIN_PARAGRAPH


def test_an_unknown_seam_is_refused():
    page = {"join_prev_source": ""}

    assert pg.set_join(page, "column") is False
    assert page.get("join_prev") in (None, "")


def test_the_glue_is_recorded(project):
    page = {"join_prev_source": ""}
    pg.set_join(page, JOIN_SENTENCE, glue=GLUE_SPACE)

    assert page["join_glue"] == GLUE_SPACE


# ---- reordering and deleting ----------------------------------------------------------

def test_reordering_needs_a_full_permutation(project):
    """A partial list would silently drop pages from the book."""
    with pg.mutate_pages(project) as doc:
        a, b, c = _add(doc), _add(doc), _add(doc)
        assert pg.reorder(doc, [c["id"], a["id"]]) is False
        assert pg.reorder(doc, [c["id"], a["id"], b["id"]]) is True

    assert [p["id"] for p in pg.load_pages(project)["pages"]] == \
        [c["id"], a["id"], b["id"]]


def test_reordering_with_an_unknown_id_is_refused(project):
    with pg.mutate_pages(project) as doc:
        a, b = _add(doc), _add(doc)
        assert pg.reorder(doc, [a["id"], "ffffffff"]) is False


def test_deleting_returns_the_records_so_their_files_can_go(project):
    with pg.mutate_pages(project) as doc:
        a, b = _add(doc), _add(doc)
        removed = pg.delete_pages(doc, [a["id"]])

    assert [p["id"] for p in removed] == [a["id"]]
    assert [p["id"] for p in pg.load_pages(project)["pages"]] == [b["id"]]


# ---- resting status ---------------------------------------------------------------

def test_a_page_whose_work_is_dropped_goes_back_to_what_it_was(project):
    """Restoring to `status` itself would leave it reading "queued" forever — that is
    what it was set to when the work was accepted."""
    with pg.mutate_pages(project) as doc:
        page = _add(doc)
        page["read"] = _read()
        page["status"] = pg.STATUS_OK
        pg.set_status(doc, page["id"], pg.STATUS_QUEUED)

    page = pg.load_pages(project)["pages"][0]
    assert page["status"] == pg.STATUS_QUEUED
    assert pg.resting_status(page) == pg.STATUS_OK


def test_an_unread_page_rests_at_new(project):
    assert pg.resting_status({"read": None}) == pg.STATUS_NEW


# ---- path safety ---------------------------------------------------------------------

def test_a_page_file_resolves_only_through_the_manifest(project):
    """A URL segment never becomes a path component."""
    data = jpeg(W, H)
    with pg.mutate_pages(project) as doc:
        page = _add(doc, data=data)
    pg.pages_dir(project).mkdir(parents=True, exist_ok=True)
    (pg.pages_dir(project) / page["file"]).write_bytes(data)

    assert pg.resolve_page_file(project, page["id"]).name == page["file"]


@pytest.mark.parametrize("page_id", ["../../secret", "..", "", "zzzz", "a" * 40, None])
def test_a_hostile_page_id_resolves_to_nothing(project, page_id):
    assert pg.resolve_page_file(project, page_id) is None


def test_a_manifest_pointing_outside_the_folder_is_refused(project):
    """Belt and braces against a hand-edited manifest or a restored backup."""
    with pg.mutate_pages(project) as doc:
        page = _add(doc)
        page["file"] = "../../../escaped.jpg"

    assert pg.resolve_page_file(project, page["id"]) is None


# ---- where the manifest lives ----------------------------------------------------------

def test_the_storage_root_is_read_at_call_time(monkeypatch, tmp_path):
    """A `from .projects import PROJECTS_DIR` binds the path at IMPORT time, so it can
    never be redirected afterwards.

    That is not a stylistic point. It meant this module ignored the test isolation
    entirely and wrote into the real library — fifteen stray project folders before
    anyone noticed. Reading it through the module fixes it, and this asserts the fix
    rather than the syntax.
    """
    from server import projects as pj

    monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path / "elsewhere")

    assert pg.pages_file("abc123456789").is_relative_to(tmp_path / "elsewhere")
    assert pg.pages_dir("abc123456789").is_relative_to(tmp_path / "elsewhere")


def test_no_server_module_from_imports_the_storage_root():
    """The same mistake, caught at the source rather than by its symptoms."""
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent / "server"
    offences = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("projects",
                                                                    "server.projects"):
                for alias in node.names:
                    if alias.name == "PROJECTS_DIR":
                        offences.append(f"{path.name} from-imports PROJECTS_DIR")

    assert not offences, (
        "Import the module and read pj.PROJECTS_DIR at call time; a from-imported "
        "copy cannot be redirected:\n  " + "\n  ".join(offences))


# ---- the summary ---------------------------------------------------------------------

def test_the_summary_counts_what_the_screen_shows(project):
    with pg.mutate_pages(project) as doc:
        ready = _add(doc)
        ready["read"] = _read()
        ready["status"] = pg.STATUS_OK
        pending = _add(doc)
        unmeasured = _add(doc, data=jpeg(W, H)[:6])

    summary = pg.summary(pg.load_pages(project))
    assert summary["total"] == 3
    assert summary["ready"] == 1
    assert summary["unmeasured"] == 1
    assert summary["by_status"][pg.STATUS_NEW] == 2
    assert summary["chars"] > 0


def test_a_page_that_merely_needs_checking_is_not_ready():
    """Building with it would put un-reviewed transcription into the novel, which is
    the same mistake as reading an unaccepted translation."""
    assert pg.STATUS_NEEDS_CHECK not in pg.APPROVED_STATUSES
    assert set(pg.APPROVED_STATUSES) == {pg.STATUS_OK, pg.STATUS_EDITED}
