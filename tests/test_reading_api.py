"""Reading a chapter, accepting a reviewed one, and working the glossary queue.

The property most of these exist for: **un-reviewed prose is never presented as
finished.** A chapter that failed its checks has its translation only in the audit
copy, and the reader has to say so — otherwise the chapters/-versus-audit/ split buys
nothing, because the reader shows both the same way.

Japanese fixtures are invented.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from morning.chapter_files import read_chapter, write_audit, write_chapter_file
from morning.chapters import Chapter
from morning.config import Config
from morning.glossary import Glossary, GlossaryEntry, load_pending, save_pending
from morning.state import STATUS_NEEDS_REVIEW, STATUS_VALIDATED, State
from server import jobs, projects as pj
from server.app import app

JA_A = "電車はまだ来ない。"
JA_B = "彼女はホームの端に立っていた。"
EN = "The train still had not come."
SASAKI = "佐々木"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project():
    record = pj.create_project("朝の駅")
    pj.save_source(record["id"], [
        Chapter(index=1, title="第1話", paragraphs=[JA_A, JA_B]),
        Chapter(index=2, title="第2話", paragraphs=[JA_A, JA_B]),
        Chapter(index=3, title="第3話", paragraphs=[JA_A, JA_B]),
    ])
    return record["id"], pj.project_config(Config(), record)


# ---- reading ------------------------------------------------------------------

def test_a_chapter_reads_with_its_source_beside_it(client, project):
    pid, cfg = project
    write_chapter_file(cfg.paths.output_dir, 1, 3, EN)

    body = client.get(f"/api/projects/{pid}/read/1").json()

    assert body["english"].strip() == EN
    assert body["source"] == [JA_A, JA_B]
    assert body["title"] == "第1話"
    assert body["from_audit"] is False


def test_navigation_points_at_the_neighbouring_chapters(client, project):
    pid, _cfg = project

    first = client.get(f"/api/projects/{pid}/read/1").json()
    middle = client.get(f"/api/projects/{pid}/read/2").json()
    last = client.get(f"/api/projects/{pid}/read/3").json()

    assert (first["prev"], first["next"]) == (None, 2)
    assert (middle["prev"], middle["next"]) == (1, 3)
    assert (last["prev"], last["next"]) == (2, None)


def test_an_untranslated_chapter_reads_as_having_no_english(client, project):
    pid, _cfg = project
    body = client.get(f"/api/projects/{pid}/read/1").json()

    assert body["english"] is None
    assert body["source"] == [JA_A, JA_B]


def test_a_reviewed_chapters_prose_is_shown_but_marked_as_unaccepted(client, project):
    """The property this file exists for. The prose is there — it was paid for — but
    the reader must not present it as finished."""
    pid, cfg = project
    write_audit(cfg.paths.audit_dir, 1, 3,
                chapter=Chapter(index=1, title="t", paragraphs=[JA_A]), english=EN)

    body = client.get(f"/api/projects/{pid}/read/1").json()

    assert body["english"] == EN
    assert body["from_audit"] is True
    assert body["accepted"] is False


def test_the_accepted_copy_wins_over_the_audit_copy(client, project):
    pid, cfg = project
    write_audit(cfg.paths.audit_dir, 1, 3,
                chapter=Chapter(index=1, title="t", paragraphs=[JA_A]),
                english="the older review copy")
    write_chapter_file(cfg.paths.output_dir, 1, 3, EN)

    body = client.get(f"/api/projects/{pid}/read/1").json()

    assert body["english"].strip() == EN
    assert body["from_audit"] is False


def test_the_reader_carries_why_a_chapter_was_flagged(client, project):
    pid, cfg = project
    with jobs.mutate_state(cfg.paths.state_file) as state:
        state.update(1, status=STATUS_NEEDS_REVIEW,
                     failures=["paragraph count 1 vs source 20 (tolerance 2)"],
                     leak_findings=[{"text": JA_A, "line": 3, "score": 5,
                                     "reasons": ["ends a sentence"]}])

    body = client.get(f"/api/projects/{pid}/read/1").json()

    assert body["status"] == STATUS_NEEDS_REVIEW
    assert body["failures"]
    assert body["leak_findings"][0]["line"] == 3


def test_reading_an_unknown_chapter_is_a_clean_404(client, project):
    pid, _cfg = project

    assert client.get(f"/api/projects/{pid}/read/99").status_code == 404


# ---- accepting -----------------------------------------------------------------

def test_accepting_promotes_the_audit_copy_into_chapters(client, project):
    pid, cfg = project
    write_audit(cfg.paths.audit_dir, 1, 3,
                chapter=Chapter(index=1, title="t", paragraphs=[JA_A]), english=EN)

    response = client.post(f"/api/projects/{pid}/chapters/1/accept", json={})

    assert response.status_code == 200
    assert response.json()["status"] == STATUS_VALIDATED
    assert read_chapter(cfg.paths.output_dir, 1, 3).strip() == EN
    assert client.get(f"/api/projects/{pid}/read/1").json()["from_audit"] is False


def test_accepting_records_that_a_human_decided(client, project):
    """Why it was flagged stays on the record. What changed is that someone looked."""
    pid, cfg = project
    write_audit(cfg.paths.audit_dir, 1, 3,
                chapter=Chapter(index=1, title="t", paragraphs=[JA_A]), english=EN)
    with jobs.mutate_state(cfg.paths.state_file) as state:
        state.update(1, status=STATUS_NEEDS_REVIEW, failures=["length ratio"])

    client.post(f"/api/projects/{pid}/chapters/1/accept", json={})

    record = State.load(cfg.paths.state_file).get(1)
    assert record["status"] == STATUS_VALIDATED
    assert record["accepted"] is True
    assert record["failures"] == ["length ratio"], "the reason must not be erased"


def test_a_reviewer_can_fix_a_line_before_accepting(client, project):
    pid, cfg = project
    write_audit(cfg.paths.audit_dir, 1, 3,
                chapter=Chapter(index=1, title="t", paragraphs=[JA_A]), english=EN)

    client.post(f"/api/projects/{pid}/chapters/1/accept",
                json={"english": "The train had still not come."})

    assert read_chapter(cfg.paths.output_dir, 1, 3).strip() == \
        "The train had still not come."


def test_accepting_nothing_is_a_404_rather_than_an_empty_chapter(client, project):
    """Writing an empty chapter would look exactly like a successful accept."""
    pid, _cfg = project

    response = client.post(f"/api/projects/{pid}/chapters/1/accept", json={})

    assert response.status_code == 404
    assert "no saved translation" in response.json()["detail"]["title"]


def test_accepting_is_refused_while_work_is_in_flight(client, project):
    """The worker owns state.json while it runs. An accept landing mid-sweep would
    race the record the worker is about to write."""
    pid, cfg = project
    write_audit(cfg.paths.audit_dir, 1, 3,
                chapter=Chapter(index=1, title="t", paragraphs=[JA_A]), english=EN)
    job = jobs.Job("held", pid)
    jobs._jobs[job.id] = job
    jobs._active_job_by_project[pid] = job.id
    try:
        response = client.post(f"/api/projects/{pid}/chapters/1/accept", json={})
        assert response.status_code == 409
    finally:
        job.done = True


# ---- the glossary --------------------------------------------------------------

def test_the_glossary_and_its_queue_come_back_together(client, project):
    """The review screen shows both: deciding whether a proposed term is new usually
    means looking at what is already there."""
    pid, cfg = project
    Glossary([GlossaryEntry(source="ホーム", english="platform")]).save(
        cfg.paths.glossary_json)
    save_pending(cfg.paths.glossary_pending,
                 [{"source": SASAKI, "english": "Sasaki", "type": "name"}])

    body = client.get(f"/api/projects/{pid}/glossary").json()

    assert [e["english"] for e in body["entries"]] == ["platform"]
    assert body["pending"][0]["source"] == SASAKI


def test_approving_locks_the_term_and_clears_it_from_the_queue(client, project):
    pid, cfg = project
    save_pending(cfg.paths.glossary_pending,
                 [{"source": SASAKI, "english": "Sasaki", "type": "name"}])

    response = client.post(f"/api/projects/{pid}/glossary/approve",
                           json={"source": SASAKI, "english": "Sasaki",
                                 "type": "name", "pronoun": "she"})

    assert response.status_code == 200
    glossary = Glossary.load(cfg.paths.glossary_json)
    assert glossary.get(SASAKI).pronoun == "she"
    assert load_pending(cfg.paths.glossary_pending) == []


def test_a_reviewer_can_correct_the_reading_while_approving(client, project):
    """The main reason this gate exists. Most kanji have several readings and the
    model picked one; locking a wrong one propagates it to every later chapter."""
    pid, cfg = project
    save_pending(cfg.paths.glossary_pending,
                 [{"source": SASAKI, "english": "Sazaki", "type": "name"}])

    client.post(f"/api/projects/{pid}/glossary/approve",
                json={"source": SASAKI, "english": "Sasaki", "type": "name",
                      "reading": "ささき",
                      "variants": ["ササキ"]})

    entry = Glossary.load(cfg.paths.glossary_json).get(SASAKI)
    assert entry.english == "Sasaki"
    assert entry.reading == "ささき"
    assert "ササキ" in entry.spellings()
    assert load_pending(cfg.paths.glossary_pending) == []


def test_an_approved_term_is_then_found_in_a_chapter(client, project):
    """End of the chain: approving is what makes the term reach the next prompt."""
    pid, cfg = project
    client.post(f"/api/projects/{pid}/glossary/approve",
                json={"source": SASAKI, "english": "Sasaki", "type": "name"})

    glossary = Glossary.load(cfg.paths.glossary_json)
    assert glossary.relevant_to(f"{SASAKI}は笑った。")


def test_rejecting_drops_it_without_locking_anything(client, project):
    """It can be proposed again by a later chapter, which is right: rejecting means
    "not this spelling", not "never mention this again"."""
    pid, cfg = project
    save_pending(cfg.paths.glossary_pending,
                 [{"source": SASAKI, "english": "Sasaki"}])

    response = client.post(f"/api/projects/{pid}/glossary/reject",
                           json={"source": SASAKI})

    assert response.json()["rejected"] is True
    assert load_pending(cfg.paths.glossary_pending) == []
    assert len(Glossary.load(cfg.paths.glossary_json)) == 0


def test_rejecting_something_that_is_not_queued_says_so(client, project):
    pid, _cfg = project

    assert client.post(f"/api/projects/{pid}/glossary/reject",
                       json={"source": "誰もいない"}
                       ).json()["rejected"] is False


def test_a_locked_term_can_be_unlocked(client, project):
    pid, cfg = project
    Glossary([GlossaryEntry(source=SASAKI, english="Sasaki")]).save(
        cfg.paths.glossary_json)

    response = client.post(f"/api/projects/{pid}/glossary/remove",
                           json={"source": SASAKI})

    assert response.json()["removed"] is True
    assert len(Glossary.load(cfg.paths.glossary_json)) == 0


@pytest.mark.parametrize("body,expected", [
    ({}, "needs a source term"),
    ({"source": "x", "english": "X", "type": "weapon"}, "Unknown term type"),
])
def test_a_bad_entry_is_refused_with_an_explanation(client, project, body, expected):
    pid, _cfg = project
    response = client.post(f"/api/projects/{pid}/glossary/approve", json=body)

    assert response.status_code == 400
    assert expected in response.json()["detail"]["title"]


def test_the_markdown_companion_is_written_on_approval(client, project):
    """So a name can be checked at a glance without opening JSON."""
    pid, cfg = project
    client.post(f"/api/projects/{pid}/glossary/approve",
                json={"source": SASAKI, "english": "Sasaki", "type": "name"})

    assert "Sasaki" in cfg.paths.glossary_md.read_text(encoding="utf-8")


def test_approving_twice_does_not_duplicate_the_entry(client, project):
    pid, cfg = project
    for _ in range(2):
        client.post(f"/api/projects/{pid}/glossary/approve",
                    json={"source": SASAKI, "english": "Sasaki", "type": "name"})

    assert len(Glossary.load(cfg.paths.glossary_json)) == 1


def test_glossary_endpoints_404_on_an_unknown_project(client):
    for path, body in (("glossary", None), ("glossary/approve", {"source": "x",
                                                                 "english": "X"}),
                       ("glossary/reject", {"source": "x"}),
                       ("glossary/remove", {"source": "x"})):
        response = (client.get(f"/api/projects/0123456789ab/{path}") if body is None
                    else client.post(f"/api/projects/0123456789ab/{path}", json=body))
        assert response.status_code == 404, path
