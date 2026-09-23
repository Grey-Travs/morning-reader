"""A manga chapter on the shared worker: the third number space, and the cancel path.

A manga chapter translation rides the same Job as a page read and a prose chapter, so
it inherits Stop, the live console, Activity and the rate-limit ride-out without any of
that being written again. What it must NOT inherit is the page axis's bookkeeping.

The cancel tests are the point of this file. ``release_queued_items`` exists because
work abandoned mid-job otherwise sits on "queued" forever and the default sweep can
never see it again — and step 4 adds a SECOND axis with exactly that hazard. A release
that knew only about pages would leave every translate-queued chapter reading
"translating" with no job running, which a user reads as work in flight: they do not
press Translate again, and those pages never get English.

No real model call. ``jobs.Translator`` is replaced, the same seam the page tests use.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from morning.pageread import KIND_BUBBLE, Region, region_hash
from morning.prompts import NEW_TERMS_DELIMITER
from server import jobs, pages as pages_mod, projects as pj, tasks as task_mod
from server.app import app

TAB = "\t"
Q = "もう無理だって"
A = "……そう?"


class FakeScriptTranslator:
    """Stands in at the seam the worker builds through.

    It answers the ids it was actually ASKED for, parsed out of the rendered user
    message, rather than a fixed script. That matters: a fake that always replies with
    page 1's ids makes every other chapter look like it came back empty, which then
    looks like a bug in the sweep rather than in the double.

    ``answers`` overrides it entirely, for the tests that need a specific bad answer.
    """

    calls: list = []
    answers: list = []

    # Keyed by the Japanese, so a line's English does not depend on which chapter or
    # which position it happened to land in.
    ENGLISH = {"もう無理だって": ("Aoi", "No more."),
               "……そう?": ("Kenji", "Really?")}

    def __init__(self, *_a, **_kw):
        from morning.config import TranslationConfig

        self.tcfg = TranslationConfig()

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        FakeScriptTranslator.calls.append(user_text)
        if FakeScriptTranslator.answers:
            answer = FakeScriptTranslator.answers.pop(0)
        else:
            rows = []
            for row in user_text.splitlines():
                parts = row.split(TAB)
                if len(parts) < 3 or not re.match(r"^\d+:\S+$", parts[0]):
                    continue
                speaker, english = FakeScriptTranslator.ENGLISH.get(
                    parts[2].strip(), ("", f"[{parts[2].strip()}]"))
                rows.append(TAB.join([parts[0], speaker, english]))
            answer = "\n".join(rows) + f"\n{NEW_TERMS_DELIMITER}\n[]"
        return answer, {"input_tokens": 100, "output_tokens": 50}, 0.02


@pytest.fixture(autouse=True)
def fake_translator(monkeypatch):
    FakeScriptTranslator.calls = []
    FakeScriptTranslator.answers = []
    monkeypatch.setattr(jobs, "Translator", FakeScriptTranslator)
    yield FakeScriptTranslator


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def region(rid: str, text: str, order: int) -> dict:
    return Region(id=rid, box=(0.1, 0.1 + order * 0.3, 0.2, 0.2), text=text,
                  kind=KIND_BUBBLE, order=order).to_dict()


@pytest.fixture
def manga_project():
    """A manga with one page of two bubbles and one built chapter."""
    pid = pj.create_project("scanned manga", kind=pj.KIND_MANGA,
                            ingest=pj.INGEST_IMAGES)["id"]
    with pages_mod.mutate_pages(pid) as doc:
        doc["pages"] = [{
            "id": "aaaa1111", "seq": 1, "name": "page-1.jpg", "status": "ok",
            "width": 1600, "height": 2400, "join_prev": "",
            "read": {"width": 1600, "height": 2400,
                     "regions": [region("r0", Q, 0), region("r1", A, 1)],
                     "order_source": "model",
                     "meta": {"confidence": "high", "heading": None, "notes": []}},
        }]
        doc["chapters"] = [{"index": 1, "title": "第1話", "start_seq": 1,
                            "end_seq": 1, "page_ids": ["aaaa1111"], "status": ""}]
    return pid


def _cfg(pid: str):
    from server.app import project_cfg

    return project_cfg(pid)[1]


def _await_idle(client, pid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never finished")


# ---- the third number space --------------------------------------------------

class TestTheNumberSpace:
    def test_a_manga_chapter_and_a_page_with_the_same_number_do_not_collide(self):
        """Once a scanned work is built, page 5 and manga chapter 5 both exist and are
        different things. Night Reader shipped the un-namespaced version of this bug
        and queueing one silently dropped the other."""
        keys = {task_mod.queue_key(5, task_mod.TASK_TRANSLATE_SCRIPT),
                task_mod.queue_key(5, task_mod.TASK_READ_PAGE),
                task_mod.queue_key(5, task_mod.TASK_TRANSLATE)}
        assert len(keys) == 3

    def test_translating_a_chapter_is_billed(self):
        assert task_mod.TASK_TRANSLATE_SCRIPT in task_mod.BILLED_TASK_KINDS

    def test_it_is_not_a_page_task(self):
        """It must never reach the page store: its index is a chapter number, and
        looking it up as a page seq would find the wrong record entirely."""
        assert task_mod.TASK_TRANSLATE_SCRIPT not in task_mod.PAGE_TASK_KINDS
        assert task_mod.TASK_TRANSLATE_SCRIPT in task_mod.MANIFEST_TASK_KINDS

    def test_both_manifest_axes_are_in_the_release_set(self):
        assert set(task_mod.PAGE_TASK_KINDS) <= set(task_mod.MANIFEST_TASK_KINDS)
        assert set(task_mod.SCRIPT_TASK_KINDS) <= set(task_mod.MANIFEST_TASK_KINDS)


# ---- translating -------------------------------------------------------------

class TestTranslating:
    def test_a_chapter_goes_through_the_shared_worker(self, client, manga_project):
        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        doc = pages_mod.load_pages(manga_project)
        lines = doc["pages"][0]["lines"]
        assert lines["r0"]["english"] == "No more."
        assert lines["r1"]["speaker"] == "Kenji"

    def test_each_line_records_the_hash_of_the_japanese_it_came_from(
            self, client, manga_project):
        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        doc = pages_mod.load_pages(manga_project)
        stored = doc["pages"][0]["lines"]["r0"]["source_hash"]
        assert stored == region_hash(Region(id="r0", box=(0, 0, 0, 0), text=Q))

    def test_the_chapter_records_what_happened(self, client, manga_project):
        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        chapter = pages_mod.load_pages(manga_project)["chapters"][0]
        assert chapter["status"] == pages_mod.STATUS_OK
        assert chapter["lines"] == 2 and chapter["order_hash"]

    def test_spend_lands_in_the_manifest_not_in_state_json(
            self, client, manga_project):
        """state.json is keyed by CHAPTER INDEX in the prose number space. A manga
        chapter's spend written there would corrupt a real chapter's totals."""
        from morning.state import State

        cfg = _cfg(manga_project)
        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        doc = pages_mod.load_pages(manga_project)
        assert doc["totals"]["cost_usd"] == pytest.approx(0.02)
        assert State.load(cfg.paths.state_file).chapters == {}

    def test_a_chapter_the_model_did_not_finish_goes_to_a_human(
            self, client, manga_project, fake_translator):
        fake_translator.answers = [
            f"1:r0{TAB}Aoi{TAB}No more.\n{NEW_TERMS_DELIMITER}\n[]",
            f"1:r0{TAB}Aoi{TAB}No more.\n{NEW_TERMS_DELIMITER}\n[]",
        ]
        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        chapter = pages_mod.load_pages(manga_project)["chapters"][0]
        assert chapter["status"] == pages_mod.STATUS_NEEDS_CHECK
        assert chapter["missing"] == ["1:r1"]

    def test_a_humans_english_is_never_overwritten_by_a_retranslate(
            self, client, manga_project):
        """The same rule as `set_join`'s user flag and `order_source`. Their line can
        still go stale if the Japanese changes; what it cannot do is vanish because
        the chapter was translated again."""
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_line(doc["pages"][0], "r0", english="MY WORDING",
                               english_source="user", source_hash="mine")

        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        lines = pages_mod.load_pages(manga_project)["pages"][0]["lines"]
        assert lines["r0"]["english"] == "MY WORDING"
        assert lines["r1"]["english"] == "Really?"   # the rest still landed

    def test_a_humans_speaker_survives_but_their_english_need_not(
            self, client, manga_project):
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_line(doc["pages"][0], "r0", speaker="Someone Else",
                               speaker_source="user")

        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        line = pages_mod.load_pages(manga_project)["pages"][0]["lines"]["r0"]
        assert line["speaker"] == "Someone Else"
        assert line["english"] == "No more."

    def test_a_chapter_whose_pages_are_gone_is_refused_not_failed(
            self, client, manga_project):
        """Refused, because nothing was written and nothing broke — a storage
        disagreement rather than a bad chapter."""
        with pages_mod.mutate_pages(manga_project) as doc:
            doc["pages"] = []

        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1]})
        _await_idle(client, manga_project)

        chapter = pages_mod.load_pages(manga_project)["chapters"][0]
        assert chapter["status"] != pages_mod.STATUS_FAILED


# ---- the cancel path ---------------------------------------------------------

class TestCancelling:
    def test_a_stranded_chapter_is_put_back(self, manga_project):
        """The ship-blocker. Both cancel paths used to collapse the drain to bare
        integers, so the kind was thrown away before the release could use it — and a
        translate-queued chapter (whose PAGE status is a perfectly healthy "ok") was
        skipped by a release hardcoded to the page axis."""
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_chapter_status(doc, 1, pages_mod.CHAPTER_QUEUED)

        jobs.release_queued_items(
            manga_project, {(1, task_mod.TASK_TRANSLATE_SCRIPT)})

        chapter = pages_mod.load_pages(manga_project)["chapters"][0]
        assert chapter["status"] == pages_mod.CHAPTER_NEW

    def test_a_chapter_mid_translation_is_put_back(self, manga_project):
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_chapter_status(doc, 1, pages_mod.CHAPTER_RUNNING)

        jobs.release_queued_items(
            manga_project, {(1, task_mod.TASK_TRANSLATE_SCRIPT)})

        assert pages_mod.load_pages(manga_project)["chapters"][0]["status"] == \
            pages_mod.CHAPTER_NEW

    def test_a_chapter_translated_before_goes_back_to_needing_a_look(
            self, manga_project):
        """Not back to "never translated" — it HAS English on it, and saying otherwise
        would offer to re-bill a chapter that is already paid for."""
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_chapter_status(doc, 1, pages_mod.STATUS_OK,
                                         at="2026-01-01T00:00:00Z")
            pages_mod.set_chapter_status(doc, 1, pages_mod.CHAPTER_QUEUED)

        jobs.release_queued_items(
            manga_project, {(1, task_mod.TASK_TRANSLATE_SCRIPT)})

        assert pages_mod.load_pages(manga_project)["chapters"][0]["status"] == \
            pages_mod.STATUS_OK

    def test_a_finished_chapter_is_left_alone(self, manga_project):
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_chapter_status(doc, 1, pages_mod.STATUS_OK)

        jobs.release_queued_items(
            manga_project, {(1, task_mod.TASK_TRANSLATE_SCRIPT)})

        assert pages_mod.load_pages(manga_project)["chapters"][0]["status"] == \
            pages_mod.STATUS_OK

    def test_releasing_a_page_does_not_touch_a_chapter_with_the_same_number(
            self, manga_project):
        """The two axes share an integer and must not be confused for each other."""
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_chapter_status(doc, 1, pages_mod.CHAPTER_QUEUED)
            pages_mod.set_status(doc, "aaaa1111", pages_mod.STATUS_QUEUED)

        jobs.release_queued_items(manga_project, {(1, task_mod.TASK_READ_PAGE)})

        doc = pages_mod.load_pages(manga_project)
        assert doc["pages"][0]["status"] != pages_mod.STATUS_QUEUED
        assert doc["chapters"][0]["status"] == pages_mod.CHAPTER_QUEUED

    def test_and_the_other_way_round(self, manga_project):
        with pages_mod.mutate_pages(manga_project) as doc:
            pages_mod.set_chapter_status(doc, 1, pages_mod.CHAPTER_QUEUED)
            pages_mod.set_status(doc, "aaaa1111", pages_mod.STATUS_QUEUED)

        jobs.release_queued_items(
            manga_project, {(1, task_mod.TASK_TRANSLATE_SCRIPT)})

        doc = pages_mod.load_pages(manga_project)
        assert doc["pages"][0]["status"] == pages_mod.STATUS_QUEUED
        assert doc["chapters"][0]["status"] == pages_mod.CHAPTER_NEW

    def test_stopping_a_run_releases_the_queued_chapters(
            self, client, manga_project):
        """End to end through the real cancel route, which is where the kind used to
        be discarded."""
        with pages_mod.mutate_pages(manga_project) as doc:
            doc["chapters"].append({"index": 2, "title": "第2話", "start_seq": 1,
                                    "end_seq": 1, "page_ids": ["aaaa1111"],
                                    "status": ""})
            pages_mod.set_chapter_status(doc, 1, pages_mod.CHAPTER_QUEUED)
            pages_mod.set_chapter_status(doc, 2, pages_mod.CHAPTER_QUEUED)

        client.post(f"/api/projects/{manga_project}/manga/translate",
                    json={"indices": [1, 2]})
        client.post(f"/api/projects/{manga_project}/run/cancel",
                    json={"stop_current": True})
        _await_idle(client, manga_project)

        statuses = [c["status"]
                    for c in pages_mod.load_pages(manga_project)["chapters"]]
        assert pages_mod.CHAPTER_QUEUED not in statuses
        assert pages_mod.CHAPTER_RUNNING not in statuses

    def test_nothing_to_release_is_not_an_error(self, manga_project):
        jobs.release_queued_items(manga_project, set())
        jobs.release_queued_items(manga_project, {(99, "prepare")})
