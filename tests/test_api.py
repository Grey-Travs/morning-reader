"""End-to-end tests through the HTTP surface.

This is where step 1 is actually proved: paste a ``.txt`` novel, watch it become a
project with chapters, run the queue over it, and see the result in state, in the
queue overview, and on the event stream — the whole spine, through the same surface
the interface uses.

The Japanese here is invented for these tests.
"""

from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from server import jobs
from server.app import app

# A three-chapter novel. The third is already English, which is what proves the
# classifier keeps its hands off work that is done.
NOVEL = (
    "第1話　朝の駅\n\n"
    "電車はまだ来ない。彼女は"
    "ホームの端に立っていた。\n\n"
    "「遅いね」と彼は言った。\n\n"
    "第2話　雨の午後\n\n"
    "雨が降り始めた。僕は傘を"
    "持っていなかった。\n\n"
    "「入る？」と彼女が訊いた。\n\n"
    "第3話　Already in English\n\n"
    "The third chapter is already translated, so nothing should be spent on it.\n\n"
    "It is here to prove the classifier leaves finished work alone.\n"
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _create(client, text: str = NOVEL, **kw) -> dict:
    body = {"title": "朝の駅", "text": text, "mode": "heading", **kw}
    response = client.post("/api/projects/text", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _run(client, pid: str, **body) -> dict:
    response = client.post(f"/api/projects/{pid}/run", json={"kind": "prepare", **body})
    assert response.status_code == 200, response.text
    return response.json()


def _await_idle(client, pid: str, timeout: float = 15.0) -> None:
    """Wait for the project's worker to finish.

    The TestClient runs the app's event loop on another thread, so the worker really
    is running concurrently with these assertions — which is the point.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never finished")


# ---- health ------------------------------------------------------------------

def test_health_reports_what_this_app_is(client):
    body = client.get("/api/health").json()

    assert body["ok"] is True
    assert body["app"] == "morning-reader"
    assert body["publishing"] is False
    assert set(body["kinds"]) == {"novel", "manga"}
    # The ratio band is carried over from another language pair and has not been
    # re-derived, and the API says so rather than letting the validator be trusted.
    assert body["ratio_band_calibrated"] is False


# ---- the whole spine, end to end ---------------------------------------------

def test_a_pasted_novel_goes_all_the_way_through(client):
    """Step 1's acceptance test.

    Paste -> project -> chapters -> queue -> worker -> state -> read it back.
    """
    created = _create(client)
    pid = created["project"]["id"]
    assert created["chapters"] == 3
    assert created["project"]["kind"] == "novel"
    assert created["project"]["ingest"] == "text"

    # The chapters are there, measured and classified, before anything has run.
    detail = client.get(f"/api/projects/{pid}").json()
    assert [c["index"] for c in detail["chapters"]] == [1, 2, 3]
    assert [c["title"] for c in detail["chapters"]][0] == "第1話　朝の駅"
    assert [c["class"] for c in detail["chapters"]] == ["source", "source", "english"]
    assert all(c["status"] == "" for c in detail["chapters"])

    # Run the queue over the whole project.
    queued = _run(client, pid)
    assert sorted(queued["queued"]) == [1, 2, 3]
    _await_idle(client, pid)

    # Every chapter now has a recorded outcome, and the English one was not touched.
    after = client.get(f"/api/projects/{pid}").json()
    statuses = {c["index"]: c["status"] for c in after["chapters"]}
    assert statuses == {1: "prepared", 2: "prepared", 3: "english-source"}
    assert all(not c["stale"] for c in after["chapters"])
    assert after["totals"] == {"cost_usd": 0.0, "tokens": {}}

    # And one chapter reads back in full, under the names the contract promises.
    chapter = client.get(f"/api/projects/{pid}/chapters/1").json()
    assert chapter["source"], "the original text is returned under `source`"
    assert len(chapter["source_hash"]) == 64
    assert chapter["source_fraction"] > 0.8
    assert chapter["state"]["status"] == "prepared"


def test_running_twice_does_not_redo_finished_work(client):
    """The resumability contract, through the API."""
    pid = _create(client)["project"]["id"]
    _run(client, pid)
    _await_idle(client, pid)

    second = _run(client, pid)
    assert second["queued"] == [], "already-finished chapters should not re-queue"


def test_editing_the_source_marks_the_affected_chapter_stale(client):
    """A status alone would claim work that no longer applies to the text on screen."""
    pid = _create(client)["project"]["id"]
    _run(client, pid)
    _await_idle(client, pid)

    edited = NOVEL.replace("電車はまだ来ない。",
                           "電車は遅れている。")
    response = client.put(f"/api/projects/{pid}/source",
                          json={"text": edited, "mode": "heading"})
    assert response.status_code == 200

    rows = {c["index"]: c for c in client.get(f"/api/projects/{pid}").json()["chapters"]}
    assert rows[1]["stale"] is True
    assert rows[2]["stale"] is False

    # ...and re-running picks up exactly the one that changed.
    assert _run(client, pid)["queued"] == [1]


def test_a_forced_run_redoes_everything_applicable(client):
    pid = _create(client)["project"]["id"]
    _run(client, pid)
    _await_idle(client, pid)

    assert sorted(_run(client, pid, force=True)["queued"]) == [1, 2, 3]


def test_specific_chapters_can_be_queued(client):
    pid = _create(client)["project"]["id"]

    assert _run(client, pid, indices=[2])["queued"] == [2]


# ---- Activity ----------------------------------------------------------------

def test_the_queue_overview_lists_running_work(client):
    """This is what the Activity view renders."""
    pid = _create(client)["project"]["id"]
    assert client.get("/api/queue").json()["jobs"] == []

    _run(client, pid)
    # The worker may already have finished; either way the shape must be right.
    overview = client.get("/api/queue").json()["jobs"]
    for row in overview:
        assert row["pid"] == pid
        assert row["title"] == "朝の駅"
        assert row["kind_of_work"] == "novel"
        assert "pending" in row and "current" in row and "kind" in row

    _await_idle(client, pid)
    assert client.get("/api/queue").json()["jobs"] == []


def test_the_active_job_can_be_reattached_after_a_reload(client):
    pid = _create(client)["project"]["id"]
    started = _run(client, pid)

    reattached = client.get(f"/api/projects/{pid}/active-job").json()
    assert reattached["job_id"] in (started["job_id"], None)

    _await_idle(client, pid)
    assert client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None


def test_the_event_stream_replays_the_whole_run(client):
    """A consumer that connects after the work finished still learns what happened —
    which is what makes a reload, or a second tab, harmless."""
    pid = _create(client)["project"]["id"]
    job_id = _run(client, pid)["job_id"]
    _await_idle(client, pid)

    with client.stream("GET", f"/api/projects/{pid}/run/{job_id}/stream") as stream:
        events = []
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1].get("type") in ("done", "paused"):
                    break

    assert [e["type"] for e in events if e["type"] == "item"].count("item") == 3
    assert events[-1]["type"] == "done"
    # Progress is deliberately NOT replayed — it would evict the real events.
    assert not any(e["type"] == "progress" for e in events)


def test_streaming_an_unknown_job_is_a_clean_404(client):
    pid = _create(client)["project"]["id"]
    response = client.get(f"/api/projects/{pid}/run/deadbeef/stream")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not-found"


# ---- stopping ----------------------------------------------------------------

def test_cancelling_reports_cleanly_even_with_nothing_running(client):
    pid = _create(client)["project"]["id"]
    response = client.post(f"/api/projects/{pid}/run/cancel", json={})

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_resume_reports_whether_anything_was_waiting(client):
    pid = _create(client)["project"]["id"]

    assert client.post(f"/api/projects/{pid}/run/resume").json()["resumed"] is False


# ---- uploading ---------------------------------------------------------------

def test_a_txt_upload_becomes_a_project(client):
    response = client.post(
        "/api/projects/upload?mode=heading",
        files={"file": ("朝の駅.txt", io.BytesIO(NOVEL.encode("utf-8")),
                        "text/plain")})

    assert response.status_code == 200, response.text
    assert response.json()["chapters"] == 3
    assert response.json()["project"]["title"] == "朝の駅"


def test_a_shift_jis_upload_is_decoded_rather_than_mangled(client):
    """Japanese plain text still arrives as CP932. Assuming UTF-8 turns a perfectly
    good file into mojibake, and the user's first experience of the app is garbage."""
    response = client.post(
        "/api/projects/upload?mode=heading",
        files={"file": ("novel.txt", io.BytesIO(NOVEL.encode("cp932")), "text/plain")})

    assert response.status_code == 200, response.text
    pid = response.json()["project"]["id"]
    chapter = client.get(f"/api/projects/{pid}/chapters/1").json()

    assert "電車" in chapter["source"][0]


# ---- projects ----------------------------------------------------------------

def test_a_manga_project_can_be_created(client):
    """Both kinds exist from day one. They share everything above the page-read
    contract and fork below it, so the discriminator has to be on the record now."""
    created = _create(client, kind="manga")

    assert created["project"]["kind"] == "manga"


def test_an_unknown_kind_is_a_clean_400(client):
    response = client.post("/api/projects/text",
                           json={"title": "x", "text": NOVEL, "kind": "comic"})

    assert response.status_code == 400
    assert "unknown kind" in response.json()["detail"]["title"]


def test_empty_text_is_refused_with_an_explanation(client):
    response = client.post("/api/projects/text", json={"title": "x", "text": "   "})

    assert response.status_code == 400
    assert response.json()["detail"]["title"]


def test_oversized_text_is_refused_before_it_is_parsed(client):
    from server.app import MAX_SOURCE_BYTES

    response = client.post("/api/projects/text",
                           json={"title": "x", "text": "a" * (MAX_SOURCE_BYTES + 1)})

    assert response.status_code == 413


def test_projects_can_be_listed_patched_and_deleted(client):
    pid = _create(client)["project"]["id"]

    listed = client.get("/api/projects").json()["projects"]
    assert [p["id"] for p in listed] == [pid]
    assert listed[0]["totals"] == {"cost_usd": 0.0, "tokens": {}}

    patched = client.patch(f"/api/projects/{pid}",
                           json={"title": "renamed", "status": "completed"}).json()
    assert patched["project"]["title"] == "renamed"
    assert patched["project"]["status"] == "completed"

    assert client.delete(f"/api/projects/{pid}").json()["deleted"] is True
    assert client.get("/api/projects").json()["projects"] == []


def test_an_unknown_project_is_a_clean_404_with_a_usable_message(client):
    response = client.get("/api/projects/0123456789ab")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "not-found"
    assert detail["title"] == "That project does not exist."


def test_a_traversal_attempt_in_the_project_id_is_a_404_not_a_crash(client):
    assert client.get("/api/projects/..%2F..%2Fetc").status_code in (404, 400)


def test_an_unknown_chapter_is_a_clean_404(client):
    pid = _create(client)["project"]["id"]

    assert client.get(f"/api/projects/{pid}/chapters/99").status_code == 404


def test_an_unknown_task_kind_is_refused_before_anything_is_queued(client):
    pid = _create(client)["project"]["id"]
    response = client.post(f"/api/projects/{pid}/run", json={"kind": "sing"})

    assert response.status_code == 400
    assert client.get("/api/queue").json()["jobs"] == []


# ---- every error has the same shape -----------------------------------------

def test_an_unhandled_error_is_explained_rather_than_dumped(monkeypatch):
    """The frontend must always receive `detail` as the SAME object, so it never has
    to parse sometimes-a-string and sometimes-an-object.

    ``raise_server_exceptions=False`` is required to observe this at all. Starlette's
    error middleware calls the handler to build the response and then RE-RAISES so the
    server can log it; under the default TestClient that re-raise lands in the test
    instead of the response. Real uvicorn returns the JSON below.
    """
    from server import projects as pj

    def explode(*a, **k):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(pj, "list_projects", explode)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/projects")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "unknown"
    assert detail["title"] and detail["fixes"]
    assert "RuntimeError" in detail["detail"]


def test_every_error_response_carries_the_same_keys(client):
    """One payload shape, whatever went wrong."""
    expected = {"code", "title", "what", "fixes", "action", "retryable", "detail",
                "trace", "status"}
    responses = [
        client.get("/api/projects/0123456789ab"),
        client.post("/api/projects/text", json={"title": "x", "text": ""}),
    ]

    for response in responses:
        assert set(response.json()["detail"]) == expected, response.text


# ---- concurrency -------------------------------------------------------------

def test_replacing_the_source_is_refused_while_work_is_in_flight(client, monkeypatch):
    """Replacing every chapter underneath a running sweep would have it prepare
    chapter 4 of the old text and chapter 5 of the new one, and the stored hashes
    would describe a document that never existed."""
    pid = _create(client)["project"]["id"]

    # Hold a job open so the project is genuinely busy.
    job = jobs.Job("held-open", pid)
    jobs._jobs[job.id] = job
    jobs._active_job_by_project[pid] = job.id
    try:
        response = client.put(f"/api/projects/{pid}/source",
                              json={"text": NOVEL, "mode": "heading"})
        assert response.status_code == 409
        assert client.delete(f"/api/projects/{pid}").status_code == 409
    finally:
        job.done = True


def test_two_projects_run_independently(client):
    """One worker per project, not one per process."""
    first = _create(client)["project"]["id"]
    second = _create(client)["project"]["id"]

    a, b = _run(client, first), _run(client, second)
    assert a["job_id"] != b["job_id"]

    _await_idle(client, first)
    _await_idle(client, second)

    for pid in (first, second):
        rows = client.get(f"/api/projects/{pid}").json()["chapters"]
        assert {r["status"] for r in rows} == {"prepared", "english-source"}
