"""Seeing and correcting what the page reader read — Night Reader's page check, carried.

Before this, a page the reader was unsure about could only be accepted or skipped from
a thumbnail. Nothing showed the Japanese it had read, or why it was unsure, and a bad
read had one fix: pay to read it again, with no way to say what went wrong.

What is pinned here, most expensive first:

* **A correction is what the book is built from** — the novel's source and a manga's
  lines both use the corrected words, and a manga line translated from the old words
  goes stale rather than passing as current.
* **It lives BESIDE the read**, and is applied only while the region still says what
  the model read. Region ids are positions, not identities; a fix applied to words the
  human never saw is the same mistake as a stale reading order.
* **It never quietly undoes a human's reading order**, which is matched on the words.
* **It is refused while the page is being read.** Night Reader accepted it, and the
  read landed on top and silently replaced it.
* **Reading again replaces corrections, as in Night Reader** — and the note for the
  reader travels with that request, so it cannot arrive after the read started.

No test makes a real model call. Japanese is invented.
"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from morning.pageread import (
    KIND_BODY, KIND_BUBBLE, KIND_PAGE_NUMBER, PageMeta, PageRead, Region, region_hash,
)
from server import jobs, pages as pages_mod, projects as pj
from server.app import app
from tests.test_images import jpeg

W, H = 1600, 2400
FIRST = "電車はまだ来ない。"
SECOND = "彼女はホームに立っていた。"
MISREAD = "彼女はホームに立つていた。"      # つ read where the page has っ
THIRD = "雨が降り始めた。"


class FakeReader:
    """Answers every page with the same three regions — two paragraphs and a page
    number — the way the model does: pixels, its own order, meta with notes.

    ``gate`` makes a read wait until the test releases it, so a test can act on a page
    while its read is in flight, which is exactly when the races live.
    """

    calls: list = []
    regions = [SECOND, FIRST]          # in the model's LIST order; order puts FIRST first
    notes: list = ["the bottom line is faint"]
    confidence = "medium"
    gate: threading.Event | None = None

    def __init__(self, *_a, **_kw):
        pass

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        FakeReader.calls.append({"user": user_text})
        if FakeReader.gate is not None:
            assert FakeReader.gate.wait(10), "the test never released the read"
        second, first = FakeReader.regions
        return json.dumps({
            "width": W, "height": H,
            "regions": [
                {"box": [100, 900, 1400, 600], "text": second, "kind": "body",
                 "order": 1, "join_prev": "paragraph"},
                {"box": [100, 100, 1400, 600], "text": first, "kind": "body",
                 "order": 0},
                {"box": [760, 2300, 80, 40], "text": "—26—", "kind": "body",
                 "order": 2},
            ],
            "meta": {"confidence": FakeReader.confidence, "heading": None,
                     "notes": list(FakeReader.notes)},
        }, ensure_ascii=False), {"input_tokens": 50}, 0.02


@pytest.fixture(autouse=True)
def fake_reader(monkeypatch):
    FakeReader.calls = []
    FakeReader.regions = [MISREAD, FIRST]
    FakeReader.notes = ["the bottom line is faint"]
    FakeReader.confidence = "medium"
    FakeReader.gate = None
    monkeypatch.setattr(jobs, "Translator", FakeReader)
    yield FakeReader
    if FakeReader.gate is not None:
        FakeReader.gate.set()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _await_idle(client, pid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never finished")


def _await_status(pid, page_id, status, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = pages_mod.find_page(pages_mod.load_pages(pid), page_id)
        if page and page.get("status") == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"page never reached {status!r}")


def _project(kind=pj.KIND_NOVEL):
    return pj.create_project("scans", kind=kind, ingest=pj.INGEST_IMAGES)["id"]


def _upload(client, pid, count=1):
    files = [("files", (f"p{i}.jpg", io.BytesIO(jpeg(W, H + i)), "image/jpeg"))
             for i in range(count)]
    return [a["id"] for a in client.post(f"/api/projects/{pid}/pages",
                                         files=files).json()["added"]]


def _read(client, pid, ids=None, **body):
    response = client.post(f"/api/projects/{pid}/pages/read",
                           json={"ids": ids, **body} if ids else body)
    assert response.status_code == 200, response.text
    _await_idle(client, pid)


@pytest.fixture
def read_page(client):
    """A novel with one page, read: regions r0 (MISREAD, second in reading order),
    r1 (FIRST, first) and r2 (a page number)."""
    pid = _project()
    (page_id,) = _upload(client, pid)
    _read(client, pid)
    return pid, page_id


def _correct(client, pid, page_id, region_id, text):
    return client.post(f"/api/projects/{pid}/pages/{page_id}/regions/{region_id}",
                       json={"text": text})


def _stored(pid, page_id):
    return pages_mod.find_page(pages_mod.load_pages(pid), page_id)


# ---- what the check screen is given ---------------------------------------------

class TestWhatThePageShows:
    def test_the_regions_come_in_reading_order_with_what_they_say(
            self, client, read_page):
        pid, page_id = read_page
        body = client.get(f"/api/projects/{pid}/pages/{page_id}").json()

        assert [r["id"] for r in body["regions"]] == ["r1", "r0", "r2"]
        assert [r["text"] for r in body["regions"]] == [FIRST, MISREAD, "—26—"]
        assert body["regions"][2]["kind"] == KIND_PAGE_NUMBER
        assert not any(r["corrected"] for r in body["regions"])

    def test_it_says_why_the_reader_was_unsure(self, client, read_page):
        """Recorded on every read and shown nowhere, so "Check this" gave nobody
        anything to check against."""
        pid, page_id = read_page
        body = client.get(f"/api/projects/{pid}/pages/{page_id}").json()

        assert body["notes"] == ["the bottom line is faint"]
        assert body["page"]["status"] == pages_mod.STATUS_NEEDS_CHECK

    def test_it_knows_its_neighbours(self, client):
        pid = _project()
        ids = _upload(client, pid, count=3)

        middle = client.get(f"/api/projects/{pid}/pages/{ids[1]}").json()["position"]
        first = client.get(f"/api/projects/{pid}/pages/{ids[0]}").json()["position"]

        assert middle == {"index": 1, "total": 3, "prev": ids[0], "next": ids[2]}
        assert first["prev"] is None

    def test_an_unread_page_has_nothing_to_show_yet(self, client):
        pid = _project()
        (page_id,) = _upload(client, pid)
        body = client.get(f"/api/projects/{pid}/pages/{page_id}").json()

        assert body["regions"] == [] and body["notes"] == []

    def test_an_unknown_page_is_a_clean_404(self, client, read_page):
        pid, _ = read_page
        assert client.get(f"/api/projects/{pid}/pages/ffffffff").status_code == 404


# ---- correcting --------------------------------------------------------------------

class TestCorrecting:
    def test_a_correction_changes_what_the_page_says(self, client, read_page):
        pid, page_id = read_page
        response = _correct(client, pid, page_id, "r0", SECOND)

        assert response.status_code == 200, response.text
        region = next(r for r in client.get(
            f"/api/projects/{pid}/pages/{page_id}").json()["regions"] if r["id"] == "r0")
        assert region == {**region, "text": SECOND, "original": MISREAD,
                          "corrected": True}

    def test_a_corrected_page_counts_as_checked(self, client, read_page):
        """Night Reader's rule: a hand-edited page is the reader's word."""
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)

        assert _stored(pid, page_id)["status"] == pages_mod.STATUS_EDITED
        assert pages_mod.STATUS_EDITED in pages_mod.APPROVED_STATUSES

    def test_the_model_reading_is_kept_beside_it(self, client, read_page):
        """Stored beside the read, never in it — a re-read replaces `read` wholesale."""
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)

        stored = _stored(pid, page_id)
        assert stored["read"]["regions"][0]["text"] == MISREAD
        assert stored["corrections"]["r0"]["from"] == MISREAD
        assert stored["corrections"]["r0"]["to"] == SECOND

    def test_putting_the_model_words_back_removes_the_correction(
            self, client, read_page):
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)
        _correct(client, pid, page_id, "r0", MISREAD)

        assert pages_mod.corrections_of(_stored(pid, page_id)) == {}

    def test_the_page_list_counts_them(self, client, read_page):
        """So "Read again" can say, before anything is spent, what it will replace."""
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)

        row = client.get(f"/api/projects/{pid}/pages").json()["pages"][0]
        assert row["corrections"] == 1

    def test_a_region_can_be_emptied(self, client, read_page):
        """A line the reader invented out of a smudge. Emptied, it is no longer text."""
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", "")

        assert pages_mod.page_text(_stored(pid, page_id)) == FIRST

    def test_it_is_refused_while_the_page_is_being_read(self, client, read_page):
        """Night Reader accepted it, and the read then landed on top and silently
        replaced the correction."""
        pid, page_id = read_page
        for status in (pages_mod.STATUS_QUEUED, pages_mod.STATUS_RUNNING):
            with pages_mod.mutate_pages(pid) as doc:
                pages_mod.find_page(doc, page_id)["status"] = status

            response = _correct(client, pid, page_id, "r0", SECOND)

            assert response.status_code == 409
            assert "being read" in response.json()["detail"]["title"]
            assert pages_mod.corrections_of(_stored(pid, page_id)) == {}

    def test_an_unread_page_has_nothing_to_correct(self, client):
        pid = _project()
        (page_id,) = _upload(client, pid)

        assert _correct(client, pid, page_id, "r0", SECOND).status_code == 400

    def test_unknown_pages_and_regions_are_clean_404s(self, client, read_page):
        pid, page_id = read_page
        assert _correct(client, pid, "ffffffff", "r0", SECOND).status_code == 404
        assert _correct(client, pid, page_id, "r9", SECOND).status_code == 404

    def test_a_runaway_paste_is_refused(self, client, read_page):
        pid, page_id = read_page
        assert _correct(client, pid, page_id, "r0", "字" * 20_001).status_code == 413


# ---- what it reaches ---------------------------------------------------------------

class TestWhatACorrectionReaches:
    def test_the_novel_is_built_from_the_corrected_words(self, client, read_page):
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)

        assert client.post(f"/api/projects/{pid}/pages/build").status_code == 200

        (chapter,) = pj.load_source(pid)
        assert chapter.paragraphs == [FIRST, SECOND]

    def test_the_novel_follows_a_human_reading_order(self, client, read_page):
        """`page_text` read the RAW read, so a novel built after a human reordered a
        page's columns came out in the model's order anyway."""
        pid, page_id = read_page
        client.post(f"/api/projects/{pid}/pages/{page_id}/order",
                    json={"ids": ["r0", "r1", "r2"]})

        assert pages_mod.page_text(_stored(pid, page_id)) == f"{MISREAD}\n\n{FIRST}"

    def test_a_correction_does_not_undo_a_human_reading_order(self, client, read_page):
        """The saved order is matched on the words the model read. Applied the other way
        round, fixing a typo made the order stop matching and silently drop out."""
        pid, page_id = read_page
        client.post(f"/api/projects/{pid}/pages/{page_id}/order",
                    json={"ids": ["r0", "r1", "r2"]})
        _correct(client, pid, page_id, "r0", SECOND)

        read, note = pages_mod.effective_read(_stored(pid, page_id))
        assert note == ""
        assert read.order_source == "user"
        assert [r.text for r in read.in_order()] == [SECOND, FIRST, "—26—"]

    def test_an_order_set_after_a_correction_still_applies(self, client, read_page):
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)
        client.post(f"/api/projects/{pid}/pages/{page_id}/order",
                    json={"ids": ["r0", "r1", "r2"]})

        read, note = pages_mod.effective_read(_stored(pid, page_id))
        assert note == ""
        assert [r.text for r in read.in_order()] == [SECOND, FIRST, "—26—"]

    def test_a_manga_line_translated_from_the_old_words_goes_stale(self, client):
        pid = _project(pj.KIND_MANGA)
        (page_id,) = _upload(client, pid)
        _read(client, pid)
        with pages_mod.mutate_pages(pid) as doc:
            page = pages_mod.find_page(doc, page_id)
            read, _ = pages_mod.effective_read(page)
            r0 = next(r for r in read.regions if r.id == "r0")
            pages_mod.set_line(page, "r0", english="She stood.",
                               source_hash=region_hash(r0))
        assert pages_mod.line_counts(_stored(pid, page_id))["stale"] == 0

        _correct(client, pid, page_id, "r0", SECOND)

        assert pages_mod.line_counts(_stored(pid, page_id))["stale"] == 1


# ---- reading again -----------------------------------------------------------------

class TestReadingAgain:
    def test_the_note_reaches_the_reader_with_the_request(self, client, read_page):
        pid, page_id = read_page
        _read(client, pid, [page_id], force=True, hint="  下の二行が切れている ")

        assert "下の二行が切れている" in FakeReader.calls[-1]["user"]
        assert _stored(pid, page_id)["hint"] == "", "acted on, so cleared"

    def test_a_new_reading_replaces_the_corrections(self, client, read_page):
        pid, page_id = read_page
        _correct(client, pid, page_id, "r0", SECOND)
        FakeReader.regions = [SECOND, FIRST]
        FakeReader.confidence = "high"

        _read(client, pid, [page_id], force=True)

        stored = _stored(pid, page_id)
        assert pages_mod.corrections_of(stored) == {}
        assert stored["status"] == pages_mod.STATUS_OK
        assert pages_mod.page_text(stored) == f"{FIRST}\n\n{SECOND}"

    def test_a_note_is_not_written_onto_a_page_already_in_flight(
            self, client, read_page):
        """The worker read its snapshot when the item started: the note would miss
        this read and then be wiped by it."""
        pid, page_id = read_page
        with pages_mod.mutate_pages(pid) as doc:
            page = pages_mod.find_page(doc, page_id)
            page["status"], page["hint"] = pages_mod.STATUS_RUNNING, "earlier note"

        client.post(f"/api/projects/{pid}/pages/read",
                    json={"ids": [page_id], "force": True, "hint": "later note"})

        assert _stored(pid, page_id)["hint"] == "earlier note"

    def test_a_page_skipped_while_queued_is_never_read(self, client):
        """Reading it anyway spent the allowance on a page the human had just said was
        not text, and the result overwrote the skip."""
        pid = _project()
        first, second = _upload(client, pid, count=2)
        FakeReader.gate = threading.Event()
        client.post(f"/api/projects/{pid}/pages/read", json={})
        _await_status(pid, first, pages_mod.STATUS_RUNNING)

        client.post(f"/api/projects/{pid}/pages/{second}/status",
                    json={"status": "skipped"})
        FakeReader.gate.set()
        _await_idle(client, pid)

        assert len(FakeReader.calls) == 1
        stored = _stored(pid, second)
        assert stored["status"] == pages_mod.STATUS_SKIPPED
        assert stored["read"] is None

    def test_a_page_skipped_while_being_read_stays_skipped(self, client):
        """The read is kept — it is paid for — but "not part of the text" stands."""
        pid = _project()
        (page_id,) = _upload(client, pid)
        FakeReader.gate = threading.Event()
        client.post(f"/api/projects/{pid}/pages/read", json={})
        _await_status(pid, page_id, pages_mod.STATUS_RUNNING)

        client.post(f"/api/projects/{pid}/pages/{page_id}/status",
                    json={"status": "skipped"})
        FakeReader.gate.set()
        _await_idle(client, pid)

        stored = _stored(pid, page_id)
        assert stored["status"] == pages_mod.STATUS_SKIPPED
        assert stored["read"] is not None


# ---- putting a page back -----------------------------------------------------------

class TestPuttingAPageBack:
    def test_a_read_page_goes_back_to_be_checked(self, client, read_page):
        """As "new" it was stuck: the sweep skips a page with a read, it was not
        approved, and "Looks right" was only offered on pages to check — so the one
        way back into the book was a paid re-read."""
        pid, page_id = read_page
        client.post(f"/api/projects/{pid}/pages/{page_id}/status",
                    json={"status": "skipped"})

        body = client.post(f"/api/projects/{pid}/pages/{page_id}/status",
                           json={"status": "new"}).json()

        assert body["status"] == pages_mod.STATUS_NEEDS_CHECK
        assert _stored(pid, page_id)["status"] == pages_mod.STATUS_NEEDS_CHECK

    def test_an_unread_page_goes_back_to_new(self, client):
        pid = _project()
        (page_id,) = _upload(client, pid)
        client.post(f"/api/projects/{pid}/pages/{page_id}/status",
                    json={"status": "skipped"})

        body = client.post(f"/api/projects/{pid}/pages/{page_id}/status",
                           json={"status": "new"}).json()

        assert body["status"] == pages_mod.STATUS_NEW


# ---- the store, directly -----------------------------------------------------------

def _page(regions, **extra) -> dict:
    read = PageRead(width=W, height=H, meta=PageMeta(confidence="high"),
                    regions=regions).to_dict()
    return {"id": "0000abcd", "seq": 1, "status": "ok", "read": read, **extra}


def _region(rid, text, order, kind=KIND_BODY):
    return Region(id=rid, box=(0.1, 0.1 + 0.2 * order, 0.8, 0.15), text=text,
                  kind=kind, order=order)


class TestTheStore:
    def test_a_correction_whose_words_are_gone_is_not_applied_and_says_so(self):
        """Region ids are positions. Put onto whatever now sits at r0, a fix would
        land on words the human never saw."""
        page = _page([_region("r0", MISREAD, 0)])
        pages_mod.set_correction(page, "r0", SECOND)
        page["read"]["regions"][0]["text"] = THIRD      # read again, differently

        read, note = pages_mod.effective_read(page)

        assert read.regions[0].text == THIRD
        assert "no longer applies" in note

    def test_a_damaged_order_or_corrections_value_does_not_break_the_page(self):
        """Every screen that shows a page goes through here, and one bad value in a
        hand-edited manifest was a 500 on all of them."""
        for damage in ({"order": "x"}, {"order": ["x"]}, {"order": 5},
                       {"corrections": ["x"]}, {"corrections": {"r0": "x"}},
                       {"order": {"texts": "not a list"}}):
            page = _page([_region("r0", FIRST, 0)], **damage)

            read, _note = pages_mod.effective_read(page)

            assert [r.text for r in read.regions] == [FIRST], damage

    def test_the_stored_read_objects_are_not_changed(self):
        page = _page([_region("r0", MISREAD, 0)])
        pages_mod.set_correction(page, "r0", SECOND)

        pages_mod.effective_read(page)

        assert page["read"]["regions"][0]["text"] == MISREAD

    def test_a_correction_to_a_bubble_is_what_the_translator_is_given(self):
        page = _page([_region("r0", "なんで", 0, KIND_BUBBLE)])
        pages_mod.set_correction(page, "r0", "なんで？")

        assert [r.text for r in pages_mod.translatable_regions(page)] == ["なんで？"]
