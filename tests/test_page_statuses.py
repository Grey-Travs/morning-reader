"""A page's status while it is read: queued -> reading -> ok | needs-check | failed.

The pages grid is the only place the owner learns what the worker is doing to a stack
of photographs, and every one of these states is a promise about money:

  - "queued" says the press was accepted. Without it a page sits on "new" and the
    owner presses Read again, which buys the same page twice once the first job
    reaches it.
  - "reading" says the model has it NOW. A page that jumps straight from "queued" to
    done cannot be told apart from a queue that is stuck.
  - "ok" and "needs-check" split on the model's own confidence. Collapsing them sends
    unreviewed transcription into the novel, which is the same mistake as reading an
    unaccepted translation.
  - "failed", WITH the reason on the page. A failure recorded as "ok" is a page that
    is silently missing from the book; one recorded without a reason is a page the
    owner re-reads blind, paying again for the same failure.

And one bad page must never take the rest of the batch with it: a sweep of two hundred
pages that dies at page twelve leaves a hundred and eighty-eight on "queued" with no
job running, which reads as work in flight.

"reading" is observed deterministically: the fake reader parks inside the model call on
an Event, the test looks at the manifest through the same route the pages screen polls,
and only then lets it go. Every wait has a timeout, so a regression fails rather than
hangs.

No real model call — ``server.jobs.Translator`` is replaced, the same seam the other
page tests use. Image headers are built by hand; Japanese fixtures are invented.
"""

from __future__ import annotations

import io
import json
import threading
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from server import jobs, pages as pages_mod, projects as pj
from server.app import app
from tests.test_images import jpeg

W, H = 1600, 2400
TEXT = "窓の外で猫が鳴いた。"
BETTER = "朝の光が机に落ちていた。"
UNUSABLE = "すみません、画像が見えません。"   # an answer with no JSON in it at all
GATE_TIMEOUT = 10.0


class ScriptedPageReader:
    """Stands in for the translator at the seam the worker builds through.

    What it does is keyed by the page's FILE NAME, which is in the message the reader
    is sent, so a test says exactly which page fails or is held open without depending
    on the order the worker happens to take them in.

    A plan entry may carry:
      ``confidence`` — what the model says about its own read (default "high")
      ``text``       — the page's one region (default TEXT)
      ``raise``      — an exception the call itself raises, as a crashed reader does
      ``answer``     — raw text returned instead of JSON, as a confused model does
      ``hold``       — park inside the call until the test releases it
    """

    plan: dict[str, dict] = {}
    calls: list[str] = []          # file names, one per model call, in order
    entered = threading.Event()    # set once a held call is in progress
    release = threading.Event()    # set by the test to let a held call finish

    def __init__(self, *_a, **_kw):
        pass

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        name = next((n for n in ScriptedPageReader.plan if n in user_text), "")
        ScriptedPageReader.calls.append(name)
        step = ScriptedPageReader.plan.get(name, {})

        if step.get("hold"):
            ScriptedPageReader.entered.set()
            if not ScriptedPageReader.release.wait(GATE_TIMEOUT):
                raise TimeoutError("the test never released the held read")
        if "raise" in step:
            raise step["raise"]
        if "answer" in step:
            return step["answer"], {"input_tokens": 50}, 0.02
        return json.dumps({
            "width": W, "height": H,
            "regions": [{"box": [50, 50, 200, 400], "text": step.get("text", TEXT),
                         "kind": "body", "order": 0}],
            "meta": {"confidence": step.get("confidence", "high"), "heading": None,
                     "starts_mid_sentence": False, "ends_mid_sentence": False,
                     "ends_mid_word": False, "notes": []},
        }, ensure_ascii=False), {"input_tokens": 50}, 0.02


@pytest.fixture(autouse=True)
def reader(monkeypatch):
    ScriptedPageReader.plan = {}
    ScriptedPageReader.calls = []
    ScriptedPageReader.entered = threading.Event()
    ScriptedPageReader.release = threading.Event()
    monkeypatch.setattr(jobs, "Translator", ScriptedPageReader)
    yield ScriptedPageReader
    ScriptedPageReader.release.set()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
        # Never leave a worker parked inside the fake while the app shuts down.
        ScriptedPageReader.release.set()


@pytest.fixture
def project():
    return pj.create_project("scans", ingest=pj.INGEST_IMAGES)["id"]


def _upload(client, pid, count: int) -> list[str]:
    """``count`` distinct pages (the padding makes each image's bytes unique, so none
    is taken for a duplicate), returned as page ids in upload order."""
    files = [("files", (f"page{i}.jpg", io.BytesIO(jpeg(W, H, padding=i)),
                        "image/jpeg"))
             for i in range(1, count + 1)]
    body = client.post(f"/api/projects/{pid}/pages", files=files).json()
    assert len(body["added"]) == count, body
    return [a["id"] for a in body["added"]]


def _file(pid: str, page_id: str) -> str:
    """The name the reader is shown for this page — what the fake keys its plan on."""
    return pages_mod.find_page(pages_mod.load_pages(pid), page_id)["file"]


def _rows(client, pid) -> dict[str, dict]:
    """The pages screen's own view: id -> row."""
    rows = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    return {r["id"]: r for r in rows}


def _page(client, pid, page_id) -> dict:
    return client.get(f"/api/projects/{pid}/pages/{page_id}").json()["page"]


def _await_idle(client, pid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never finished")


def _read(client, pid, body=None) -> dict:
    response = client.post(f"/api/projects/{pid}/pages/read", json=body or {})
    assert response.status_code == 200, response.text
    return response.json()


def _events(job_id: str, kind: str) -> list[dict]:
    return [e for e in jobs.get_job(job_id).history if e.get("type") == kind]


@contextmanager
def _held_open(client, pid, body=None):
    """Start a read and hold the worker INSIDE the model call until the block ends.

    The status is written before the call is made, so once the fake has been entered
    the manifest is guaranteed to show what the worker set — no sleeping and hoping.
    """
    started = _read(client, pid, body)
    assert ScriptedPageReader.entered.wait(GATE_TIMEOUT), \
        "the reader was never called, so nothing was ever reading"
    try:
        yield started
    finally:
        ScriptedPageReader.release.set()
    _await_idle(client, pid)


# ---- queued, then reading ----------------------------------------------------

def test_a_page_reads_as_reading_while_the_model_has_it(client, project, reader):
    """Guards the worker marking the page before the call. Without it the page goes
    straight from "queued" to done, and a read in progress looks like a stuck queue."""
    (page_id,) = _upload(client, project, 1)
    reader.plan = {_file(project, page_id): {"hold": True}}

    with _held_open(client, project):
        during = _page(client, project, page_id)
        assert during["status"] == pages_mod.STATUS_RUNNING
        # What it was resting at is remembered, so abandoned work can go back to it
        # rather than sitting on "reading" with no job behind it.
        assert during.get("last_status") == pages_mod.STATUS_NEW

    assert _page(client, project, page_id)["status"] == pages_mod.STATUS_OK


def test_the_pages_behind_it_read_as_queued_until_their_turn(client, project, reader):
    """Guards the request marking every page it accepted. A page left on "new" while
    it waits invites a second press of Read, which pays for it twice."""
    first, second, third = _upload(client, project, 3)
    reader.plan = {_file(project, first): {"hold": True}}

    with _held_open(client, project) as started:
        assert started["queued"] == [1, 2, 3]
        rows = _rows(client, project)
        assert rows[first]["status"] == pages_mod.STATUS_RUNNING
        assert rows[second]["status"] == pages_mod.STATUS_QUEUED
        assert rows[third]["status"] == pages_mod.STATUS_QUEUED

    rows = _rows(client, project)
    assert [rows[p]["status"] for p in (first, second, third)] == [
        pages_mod.STATUS_OK] * 3


def test_a_page_the_sweep_did_not_take_is_not_marked_queued(client, project, reader):
    """Guards queueing only what was selected. A read page flipped to "queued" by a
    sweep that will never reach it stays there with nothing running."""
    done, fresh = _upload(client, project, 2)
    _read(client, project, {"ids": [done]})
    _await_idle(client, project)
    reader.plan = {_file(project, fresh): {"hold": True}}

    with _held_open(client, project) as started:
        assert started["queued"] == [2]
        rows = _rows(client, project)
        assert rows[done]["status"] == pages_mod.STATUS_OK
        assert rows[fresh]["status"] == pages_mod.STATUS_RUNNING


# ---- how a read lands --------------------------------------------------------

@pytest.mark.parametrize("confidence,expected", [
    ("high", pages_mod.STATUS_OK),
    ("medium", pages_mod.STATUS_NEEDS_CHECK),
    ("low", pages_mod.STATUS_NEEDS_CHECK),
])
def test_the_models_confidence_decides_ok_or_needs_check(client, project, reader,
                                                         confidence, expected):
    """Guards the split at "high". Letting "medium" through as ok builds a page nobody
    looked at into the novel; sending "high" to a human buries the owner in checks."""
    (page_id,) = _upload(client, project, 1)
    reader.plan = {_file(project, page_id): {"confidence": confidence}}

    _read(client, project)
    _await_idle(client, project)

    page = _page(client, project, page_id)
    assert page["status"] == expected
    assert page["read"]["regions"][0]["text"] == TEXT


# ---- failing -----------------------------------------------------------------

@pytest.mark.parametrize("step,message", [
    ({"raise": RuntimeError("the reader process exited with code 1")},
     "the reader process exited with code 1"),
    ({"answer": UNUSABLE}, "did not return usable JSON"),
], ids=["the-call-raises", "the-answer-is-unusable"])
def test_a_read_that_raises_marks_the_page_failed_with_a_readable_error(
        client, project, reader, step, message):
    """Guards the failure path recording what happened. A failure swallowed into "ok"
    is a page silently missing from the book; one with no reason is re-read blind."""
    (page_id,) = _upload(client, project, 1)
    reader.plan = {_file(project, page_id): step}

    started = _read(client, project)
    _await_idle(client, project)

    page = _page(client, project, page_id)
    assert page["status"] == pages_mod.STATUS_FAILED
    assert message in (page["error"] or "")
    # Nothing half-written: a failed page has no read to build from.
    assert not page["read"]
    # The grid shows the reason too, not only the full page view.
    assert message in (_rows(client, project)[page_id]["error"] or "")

    item = _events(started["job_id"], "item")[0]
    assert item["status"] == pages_mod.STATUS_FAILED
    assert message in (item.get("error") or "")
    assert item["explain"]["title"], "the failure dialog would have nothing to say"


def test_one_failing_page_never_stops_the_rest_of_the_batch(client, project, reader):
    """Guards isolation. Without it the pages after the bad one never run, stay on
    "queued" with no job behind them, and the job ends without saying so."""
    first, bad, last = _upload(client, project, 3)
    reader.plan = {
        _file(project, first): {"text": TEXT},
        _file(project, bad): {"raise": RuntimeError("the reader crashed")},
        _file(project, last): {"text": BETTER},
    }

    started = _read(client, project)
    _await_idle(client, project)

    rows = _rows(client, project)
    assert [rows[p]["status"] for p in (first, bad, last)] == [
        pages_mod.STATUS_OK, pages_mod.STATUS_FAILED, pages_mod.STATUS_OK]
    assert _page(client, project, last)["read"]["regions"][0]["text"] == BETTER
    assert len(reader.calls) == 3, "every page must have been attempted"
    assert _events(started["job_id"], "done"), "the job must still finish visibly"


@pytest.mark.parametrize("retry", [
    {},                                  # the default sweep: pages with no read yet
    "selected",                          # what the pages screen sends for a selection
], ids=["sweep", "selected"])
def test_a_failed_page_can_be_read_again_and_the_error_is_cleared(
        client, project, reader, retry):
    """Guards recovery. A failed page must be offered again and, once it reads, carry
    no stale error — a page showing "ok" beside an old failure is not trusted."""
    good, bad = _upload(client, project, 2)
    reader.plan = {_file(project, bad): {"raise": RuntimeError("the reader crashed")}}
    _read(client, project)
    _await_idle(client, project)
    assert _page(client, project, bad)["status"] == pages_mod.STATUS_FAILED

    reader.plan = {_file(project, bad): {"text": BETTER}}
    body = {"ids": [bad], "force": True} if retry == "selected" else retry
    again = _read(client, project, body)
    _await_idle(client, project)

    # Only the failed page was taken: the good one is never bought twice.
    assert again["queued"] == [2]
    page = _page(client, project, bad)
    assert page["status"] == pages_mod.STATUS_OK
    assert page["error"] is None
    assert page["read"]["regions"][0]["text"] == BETTER
    assert _page(client, project, good)["status"] == pages_mod.STATUS_OK


def test_an_unusable_answer_is_still_counted_as_spent(client, project, reader):
    """Guards the spend record. The call was made and billed; only its answer was
    useless. Leaving it out of the totals makes a failing book look cheaper than it
    was, and the owner cannot see what the failures are costing them.

    It was left out: `ocr.read_page` raised after `_call` had returned its usage and
    cost, and nothing credited them. The manga and prose paths already did."""
    (page_id,) = _upload(client, project, 1)
    reader.plan = {_file(project, page_id): {"answer": UNUSABLE}}

    _read(client, project)
    _await_idle(client, project)

    assert _page(client, project, page_id)["status"] == pages_mod.STATUS_FAILED
    summary = client.get(f"/api/projects/{project}/pages").json()["summary"]
    assert summary["totals"]["cost_usd"] == 0.02
