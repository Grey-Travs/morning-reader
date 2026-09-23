"""Scanned pages end to end: upload, read, join, build.

The acceptance test the plan asks for lives at the bottom: upload → regions → flatten
→ chapters, after which the project is an ordinary one and nothing downstream can tell
it was ever a stack of photographs.

No test here makes a real model call — ``server.jobs.Translator`` is replaced, which
is the same seam the worker builds through. Image headers are built by hand; Japanese
fixtures are invented.
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from fastapi.testclient import TestClient

from morning.pageread import (
    GLUE_NONE, JOIN_SENTENCE, KIND_BODY, PageMeta, PageRead, Region, from_pixels,
)
from server import jobs, pages as pages_mod, projects as pj
from server.app import app
from tests.test_images import jpeg, png

W, H = 1600, 2400
HALF_ONE = "電車はまだ"
HALF_TWO = "来ない。"
WHOLE = HALF_ONE + HALF_TWO
LATER = "彼女はホームに立っていた。"


class FakePageReader:
    """Stands in for the translator at the seam the worker builds through.

    Returns one region per page, walking the script it was given, so a test can make
    page 1 end mid-sentence and page 2 continue it.

    The counter is on CALLS, not on instances. One translator is built per JOB by
    design — building one per page would add a process launch to every page in a
    sweep — so counting instances left every page reading the same script entry.
    """

    script: list[dict] = []
    calls: list = []      # one entry per page read, for assertions
    instances: list = []  # one entry per job, to prove the above

    def __init__(self, *_a, **_kw):
        FakePageReader.instances.append(self)

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        import json

        index = len(FakePageReader.calls)
        FakePageReader.calls.append({"user": user_text, "tools": tools, "cwd": cwd})
        entry = FakePageReader.script[min(index, len(FakePageReader.script) - 1)]
        return json.dumps({
            "width": W, "height": H,
            # A box that fits every page size used in this file. A box sized for a
            # 1600x2400 page falls OUTSIDE an 800x1200 one, which validate() catches
            # and which forces the read's confidence to low — correct behaviour, and
            # it cost two tests before the fixture was fixed rather than the code.
            "regions": [{"box": [50, 50, 200, 400], "text": entry["text"],
                         "kind": "body", "order": 0}],
            "meta": {"confidence": entry.get("confidence", "high"),
                     "heading": entry.get("heading"),
                     "starts_mid_sentence": entry.get("starts_mid", False),
                     "ends_mid_sentence": entry.get("ends_mid", False),
                     "ends_mid_word": False, "notes": []},
        }, ensure_ascii=False), {"input_tokens": 50}, 0.02


@pytest.fixture(autouse=True)
def fake_reader(monkeypatch):
    FakePageReader.calls = []
    FakePageReader.instances = []
    FakePageReader.script = [{"text": WHOLE}]
    monkeypatch.setattr(jobs, "Translator", FakePageReader)
    yield FakePageReader


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project():
    return pj.create_project("scans", ingest=pj.INGEST_IMAGES)["id"]


def _upload(client, pid, *images, label=""):
    files = [("files", (f"page{i}.jpg", io.BytesIO(data), "image/jpeg"))
             for i, data in enumerate(images, start=1)]
    return client.post(f"/api/projects/{pid}/pages?label={label}", files=files)


def _await_idle(client, pid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never finished")


# ---- uploading ------------------------------------------------------------------

def test_a_page_is_identified_and_measured_on_upload(client, project):
    response = _upload(client, project, jpeg(W, H))

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["added"]) == 1
    assert body["added"][0]["measured"] is True

    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert (rows[0]["width"], rows[0]["height"]) == (W, H)
    assert rows[0]["status"] == pages_mod.STATUS_NEW


def test_the_same_image_twice_is_a_duplicate_not_a_second_page(client, project):
    """Re-uploading a folder is ordinary. Paying to read the same page again is not."""
    data = jpeg(W, H)
    _upload(client, project, data)
    body = _upload(client, project, data).json()

    assert body["added"] == []
    assert body["duplicates"][0]["seq"] == 1
    assert len(client.get(f"/api/projects/{project}/pages").json()["pages"]) == 1


def test_an_unsupported_file_is_rejected_with_a_reason(client, project):
    files = [("files", ("photo.heic",
                        io.BytesIO(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40),
                        "image/heic"))]
    body = client.post(f"/api/projects/{project}/pages", files=files).json()

    assert body["added"] == []
    assert "iPhone" in body["rejected"][0]["reason"]


def test_a_mixed_upload_keeps_the_good_ones(client, project):
    """One bad file in a folder of forty must not lose the other thirty-nine."""
    files = [
        ("files", ("a.jpg", io.BytesIO(jpeg(W, H)), "image/jpeg")),
        ("files", ("b.pdf", io.BytesIO(b"%PDF-1.7\n" + b"\x00" * 40), "application/pdf")),
        ("files", ("c.png", io.BytesIO(png(800, 1200)), "image/png")),
    ]
    body = client.post(f"/api/projects/{project}/pages", files=files).json()

    assert len(body["added"]) == 2
    assert len(body["rejected"]) == 1


def test_an_unmeasurable_image_is_still_stored(client, project):
    """It can hold text; it just cannot anchor an overlay until it is re-scanned."""
    body = _upload(client, project, jpeg(W, H)[:6]).json()

    assert body["added"][0]["measured"] is False
    assert client.get(f"/api/projects/{project}/pages").json()["summary"]["unmeasured"] == 1


def test_the_image_is_served_back(client, project):
    data = jpeg(W, H)
    page_id = _upload(client, project, data).json()["added"][0]["id"]

    response = client.get(f"/api/projects/{project}/pages/{page_id}/image")

    assert response.status_code == 200
    assert response.content == data


@pytest.mark.parametrize("page_id", ["../../secret", "zzzz", "a" * 40])
def test_a_hostile_page_id_cannot_reach_a_file(client, project, page_id):
    assert client.get(f"/api/projects/{project}/pages/{page_id}/image"
                      ).status_code == 404


# ---- reading ---------------------------------------------------------------------

def test_reading_a_page_stores_its_regions(client, project):
    page_id = _upload(client, project, jpeg(W, H)).json()["added"][0]["id"]

    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    body = client.get(f"/api/projects/{project}/pages/{page_id}").json()
    assert body["page"]["status"] == pages_mod.STATUS_OK
    assert body["page"]["read"]["regions"][0]["text"] == WHOLE
    assert body["text"] == WHOLE
    # Boxes are fractions of the page, not the pixels the model answered in.
    assert body["page"]["read"]["regions"][0]["box"][0] < 1


def test_the_page_spend_is_recorded_in_the_manifest_not_state_json(client, project):
    """state.json is keyed by CHAPTER index; a page sequence number written there
    would corrupt a real chapter's totals."""
    _upload(client, project, jpeg(W, H))
    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    summary = client.get(f"/api/projects/{project}/pages").json()["summary"]
    assert summary["totals"]["cost_usd"] == 0.02
    assert client.get(f"/api/projects/{project}").json()["totals"]["cost_usd"] == 0.0


def test_a_page_the_reader_was_unsure_about_goes_to_a_human(client, project,
                                                            fake_reader):
    fake_reader.script = [{"text": WHOLE, "confidence": "low"}]
    _upload(client, project, jpeg(W, H))

    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert rows[0]["status"] == pages_mod.STATUS_NEEDS_CHECK


def test_a_sweep_does_not_re_read_a_page_that_has_been_read(client, project):
    """Doing it by default would re-bill the whole book."""
    _upload(client, project, jpeg(W, H))
    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    again = client.post(f"/api/projects/{project}/pages/read", json={}).json()
    assert again["queued"] == []


def test_forcing_re_reads_it(client, project):
    _upload(client, project, jpeg(W, H))
    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    again = client.post(f"/api/projects/{project}/pages/read",
                        json={"force": True}).json()
    assert again["queued"] == [1]


def test_a_skipped_page_is_never_queued(client, project):
    page_id = _upload(client, project, jpeg(W, H)).json()["added"][0]["id"]
    client.post(f"/api/projects/{project}/pages/{page_id}/status",
                json={"status": pages_mod.STATUS_SKIPPED})

    assert client.post(f"/api/projects/{project}/pages/read",
                       json={"force": True}).json()["queued"] == []


def test_cancelling_releases_queued_pages(client, project):
    """Otherwise they sit on "queued" forever and the default sweep can never see them
    again."""
    _upload(client, project, jpeg(W, H), jpeg(800, 1200), png(700, 900))
    client.post(f"/api/projects/{project}/pages/read", json={})
    client.post(f"/api/projects/{project}/run/cancel", json={"stop_current": True})
    _await_idle(client, project)

    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert not any(r["status"] == pages_mod.STATUS_QUEUED for r in rows)


def test_a_page_and_a_chapter_with_the_same_number_both_queue(client, project):
    """Pages and chapters share a worker but not a number space. Deduping on the bare
    index made queueing a page silently drop a chapter."""
    from server import tasks as task_mod

    job = jobs.Job("j", project)
    added = job.enqueue([(1, False, task_mod.TASK_READ_PAGE),
                         (1, False, task_mod.TASK_PREPARE)])

    assert added == [1, 1]
    assert len(job.snapshot_pending()) == 2


# ---- ordering, joins and status ------------------------------------------------------

def test_pages_can_be_reordered(client, project):
    ids = [a["id"] for a in _upload(client, project, jpeg(W, H),
                                    jpeg(800, 1200)).json()["added"]]

    assert client.post(f"/api/projects/{project}/pages/reorder",
                       json={"ids": list(reversed(ids))}).status_code == 200
    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_a_partial_reorder_is_refused(client, project):
    ids = [a["id"] for a in _upload(client, project, jpeg(W, H),
                                    jpeg(800, 1200)).json()["added"]]

    response = client.post(f"/api/projects/{project}/pages/reorder",
                           json={"ids": ids[:1]})

    assert response.status_code == 400
    assert "every page exactly once" in response.json()["detail"]["title"]


def test_a_human_join_is_marked_as_theirs(client, project):
    ids = [a["id"] for a in _upload(client, project, jpeg(W, H),
                                    jpeg(800, 1200)).json()["added"]]

    client.post(f"/api/projects/{project}/pages/{ids[1]}/join",
                json={"kind": JOIN_SENTENCE, "glue": GLUE_NONE})

    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert rows[1]["join_prev"] == JOIN_SENTENCE
    assert rows[1]["join_prev_source"] == "user"


def test_proposing_joins_leaves_a_human_decision_alone(client, project, fake_reader):
    """A re-read must not silently undo a correction — the person would have no reason
    to look again."""
    fake_reader.script = [{"text": HALF_ONE, "ends_mid": True},
                          {"text": HALF_TWO, "starts_mid": True}]
    ids = [a["id"] for a in _upload(client, project, jpeg(W, H),
                                    jpeg(800, 1200)).json()["added"]]
    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    from morning.pageread import JOIN_CHAPTER
    client.post(f"/api/projects/{project}/pages/{ids[1]}/join",
                json={"kind": JOIN_CHAPTER})
    client.post(f"/api/projects/{project}/pages/propose-joins")

    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert rows[1]["join_prev"] == JOIN_CHAPTER
    assert rows[1]["join_prev_source"] == "user"


def test_deleting_removes_the_record_and_the_file(client, project):
    page_id = _upload(client, project, jpeg(W, H)).json()["added"][0]["id"]
    path = pages_mod.resolve_page_file(project, page_id)

    client.post(f"/api/projects/{project}/pages/delete", json={"ids": [page_id]})

    assert client.get(f"/api/projects/{project}/pages").json()["pages"] == []
    assert not path.exists()


def test_a_page_can_be_marked_checked(client, project):
    page_id = _upload(client, project, jpeg(W, H)).json()["added"][0]["id"]

    client.post(f"/api/projects/{project}/pages/{page_id}/status",
                json={"status": pages_mod.STATUS_EDITED})

    rows = client.get(f"/api/projects/{project}/pages").json()["pages"]
    assert rows[0]["status"] == pages_mod.STATUS_EDITED


def test_a_page_cannot_be_forced_into_an_internal_state(client, project):
    page_id = _upload(client, project, jpeg(W, H)).json()["added"][0]["id"]

    response = client.post(f"/api/projects/{project}/pages/{page_id}/status",
                           json={"status": pages_mod.STATUS_RUNNING})

    assert response.status_code == 400


# ---- building -------------------------------------------------------------------------

def test_building_before_anything_is_read_says_so(client, project):
    _upload(client, project, jpeg(W, H))

    response = client.post(f"/api/projects/{project}/pages/build")

    assert response.status_code == 400
    assert "Read them first" in response.json()["detail"]["title"]


def test_a_scanned_novel_becomes_an_ordinary_project(client, project, fake_reader):
    """The acceptance test: upload -> regions -> flatten -> chapters.

    A sentence is deliberately split across the page break, because that is the case
    the whole seam machinery exists for. After the build, the project is an ordinary
    one and nothing downstream can tell it was ever photographs.
    """
    fake_reader.script = [
        {"text": HALF_ONE, "heading": "第1話", "ends_mid": True},
        {"text": HALF_TWO + "\n\n" + LATER, "starts_mid": True},
    ]
    _upload(client, project, jpeg(W, H), jpeg(800, 1200))

    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)
    client.post(f"/api/projects/{project}/pages/propose-joins")

    built = client.post(f"/api/projects/{project}/pages/build").json()
    assert built["chapters"] == 1
    assert built["pages_used"] == 2

    # ...and it now reads exactly like a pasted project.
    detail = client.get(f"/api/projects/{project}").json()
    assert len(detail["chapters"]) == 1
    assert detail["chapters"][0]["title"] == "第1話"
    assert detail["chapters"][0]["class"] == "source"

    chapter = client.get(f"/api/projects/{project}/chapters/1").json()
    # The sentence split across the page break was rejoined, not broken in two.
    assert WHOLE in "\n\n".join(chapter["source"])
    assert chapter["source_hash"]


def test_the_build_is_recorded_with_its_warnings(client, project, fake_reader):
    fake_reader.script = [{"text": HALF_ONE, "ends_mid": True},
                          {"text": LATER, "starts_mid": False}]
    _upload(client, project, jpeg(W, H), jpeg(800, 1200))
    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)
    client.post(f"/api/projects/{project}/pages/propose-joins")

    built = client.post(f"/api/projects/{project}/pages/build").json()

    assert built["warnings"], "a possible missing page must be surfaced"
    assert "may be missing" in built["warnings"][0]
    summary = client.get(f"/api/projects/{project}/pages").json()["summary"]
    assert summary["build"]["chapters"] == built["chapters"]


def test_building_is_refused_while_work_is_in_flight(client, project):
    _upload(client, project, jpeg(W, H))
    job = jobs.Job("held", project)
    jobs._jobs[job.id] = job
    jobs._active_job_by_project[project] = job.id
    try:
        assert client.post(f"/api/projects/{project}/pages/build").status_code == 409
    finally:
        job.done = True


def test_a_page_that_needs_checking_is_not_built_in(client, project, fake_reader):
    """Building with it would put un-reviewed transcription into the novel — the same
    mistake as reading an unaccepted translation."""
    fake_reader.script = [{"text": WHOLE, "confidence": "low"}]
    _upload(client, project, jpeg(W, H))
    client.post(f"/api/projects/{project}/pages/read", json={})
    _await_idle(client, project)

    response = client.post(f"/api/projects/{project}/pages/build")

    assert response.status_code == 400
