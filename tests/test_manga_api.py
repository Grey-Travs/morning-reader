"""The manga routes, and — just as important — the ones that refuse.

A manga reaching the prose pipeline is the expensive failure in step 4. "Translate
everything" would run ``process_chapter`` over concatenated bubble text: paid for, with
no per-bubble mapping for the overlay to draw, and unrecoverable. ``prepare`` would
write a record per manga chapter into ``state.json``, which is keyed in the same
integer space a page seq already occupies — the exact collision ``queue_key``'s
namespacing exists to prevent, reintroduced in the file rather than in the queue.

So the refusals are tested by name rather than left to happen by accident.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from morning.pageread import KIND_BUBBLE, KIND_SFX, Region
from morning.prompts import NEW_TERMS_DELIMITER
from server import jobs, pages as pages_mod, projects as pj
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
        return answer, {"input_tokens": 10, "output_tokens": 5}, 0.02


@pytest.fixture(autouse=True)
def fake_translator(monkeypatch):
    FakeScriptTranslator.answers = []
    monkeypatch.setattr(jobs, "Translator", FakeScriptTranslator)
    yield FakeScriptTranslator


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def region(rid: str, text: str, order: int, *, box=None, kind: str = KIND_BUBBLE
           ) -> dict:
    return Region(id=rid, box=box or (0.1, 0.1 + order * 0.3, 0.2, 0.2), text=text,
                  kind=kind, order=order).to_dict()


def _page(seq: int, regions: list[dict], *, status: str = "ok", join: str = "",
          heading=None) -> dict:
    return {
        "id": f"{seq:08x}", "seq": seq, "name": f"page-{seq}.jpg", "status": status,
        "width": 1600, "height": 2400, "join_prev": join,
        "read": {"width": 1600, "height": 2400, "regions": regions,
                 "order_source": "model",
                 "meta": {"confidence": "high", "heading": heading, "notes": []}},
    }


@pytest.fixture
def manga(client):
    """A manga with two pages, not yet built into chapters."""
    pid = pj.create_project("scans", kind=pj.KIND_MANGA,
                            ingest=pj.INGEST_IMAGES)["id"]
    with pages_mod.mutate_pages(pid) as doc:
        doc["pages"] = [
            _page(1, [region("r0", Q, 0), region("r1", A, 1)]),
            _page(2, [region("r0", "次の日", 0)], join="chapter", heading="第2話"),
        ]
    return pid


@pytest.fixture
def built(client, manga):
    client.post(f"/api/projects/{manga}/pages/build")
    return manga


def _await_idle(client, pid, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/active-job").json()["job_id"] is None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker never finished")


# ---- building ----------------------------------------------------------------

class TestBuilding:
    def test_it_writes_chapters_into_the_manifest(self, client, manga):
        body = client.post(f"/api/projects/{manga}/pages/build").json()

        assert body["chapters"] == 2
        chapters = pages_mod.load_pages(manga)["chapters"]
        assert [c["title"] for c in chapters] == ["Chapter 1", "第2話"]
        assert chapters[0]["page_ids"] == ["00000001"]

    def test_it_never_writes_source_json(self, client, manga):
        """The load-bearing refusal. A source.json would make the prose Translate
        reachable, and that is a paid, wrong translation with nothing to draw."""
        client.post(f"/api/projects/{manga}/pages/build")

        assert pj.load_source(manga) == []
        assert client.get(f"/api/projects/{manga}").json()[
            "project"]["chapter_count"] == 0

    def test_rebuilding_keeps_a_chapter_that_was_already_translated(
            self, client, built):
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)

        client.post(f"/api/projects/{built}/pages/build")

        chapter = pages_mod.load_pages(built)["chapters"][0]
        assert chapter["status"] == pages_mod.STATUS_OK
        assert chapter.get("cost_usd") == pytest.approx(0.02)

    def test_a_manga_with_no_pages_is_told_so(self, client):
        pid = pj.create_project("empty", kind=pj.KIND_MANGA,
                                ingest=pj.INGEST_IMAGES)["id"]
        assert client.post(f"/api/projects/{pid}/pages/build").status_code == 400

    def test_the_chapter_rows_carry_no_prose_fields(self, client, built):
        """`class` and `paragraph_count` are what the prose UI keys on. A row without
        them structurally cannot be offered the prose Translate."""
        rows = client.get(f"/api/projects/{built}").json()["chapters"]

        assert rows and "class" not in rows[0] and "paragraph_count" not in rows[0]
        assert rows[0]["lines"] == 2 and rows[0]["pages"] == 1


# ---- the refusals ------------------------------------------------------------

class TestRefusals:
    def test_the_prose_translate_is_refused_by_name(self, client, built):
        response = client.post(f"/api/projects/{built}/run",
                               json={"kind": "translate"})

        assert response.status_code == 400
        assert "manga" in response.json()["detail"]["what"].lower() or \
            "manga" in response.json()["detail"]["title"].lower()

    def test_prepare_is_refused_too(self, client, built):
        """`prepare` writes a record per chapter into state.json, which is keyed in the
        same integer space a page seq already occupies."""
        assert client.post(f"/api/projects/{built}/run",
                           json={"kind": "prepare"}).status_code == 400

    def test_the_prose_reader_points_at_the_manga_one(self, client, built):
        response = client.get(f"/api/projects/{built}/read/1")

        assert response.status_code == 404
        assert "manga" in str(response.json()["detail"]).lower()

    def test_accepting_a_prose_chapter_is_refused(self, client, built):
        assert client.post(
            f"/api/projects/{built}/chapters/1/accept", json={}).status_code == 404

    def test_the_manga_reader_refuses_a_novel(self, client):
        from tests.test_pages_api import _upload
        from tests.test_images import jpeg

        pid = pj.create_project("novel", ingest=pj.INGEST_IMAGES)["id"]
        _upload(client, pid, jpeg(800, 1200))

        response = client.get(f"/api/projects/{pid}/manga/1")
        assert response.status_code == 404
        assert "novel" in str(response.json()["detail"]).lower()

    def test_translating_a_novel_as_a_manga_is_refused(self, client):
        pid = pj.create_project("novel", ingest=pj.INGEST_IMAGES)["id"]
        assert client.post(f"/api/projects/{pid}/manga/translate",
                           json={}).status_code == 404

    def test_translating_before_building_is_refused(self, client, manga):
        response = client.post(f"/api/projects/{manga}/manga/translate", json={})
        assert response.status_code == 400
        assert "build" in str(response.json()["detail"]).lower()


# ---- translating -------------------------------------------------------------

class TestTranslating:
    def test_a_sweep_skips_a_chapter_that_is_already_done(self, client, built):
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)

        body = client.post(f"/api/projects/{built}/manga/translate", json={}).json()

        assert 1 not in body["queued"]

    def test_force_redoes_it(self, client, built):
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)

        body = client.post(f"/api/projects/{built}/manga/translate",
                           json={"force": True}).json()

        assert 1 in body["queued"]

    def test_nothing_left_to_do_queues_nothing(self, client, built):
        client.post(f"/api/projects/{built}/manga/translate", json={})
        _await_idle(client, built)

        assert client.post(f"/api/projects/{built}/manga/translate",
                           json={}).json()["queued"] == []


# ---- the reading order -------------------------------------------------------

class TestTheOrderRoute:
    def test_a_human_order_is_recorded_and_marked_as_theirs(self, client, manga):
        response = client.post(f"/api/projects/{manga}/pages/00000001/order",
                               json={"ids": ["r1", "r0"]})

        assert response.status_code == 200
        assert response.json()["order_check"]["order_source"] == "user"
        page = pages_mod.load_pages(manga)["pages"][0]
        assert page["order"]["texts"] == [A, Q]

    def test_a_partial_order_is_refused_with_a_sentence(self, client, manga):
        response = client.post(f"/api/projects/{manga}/pages/00000001/order",
                               json={"ids": ["r1"]})

        assert response.status_code == 400
        assert "exactly once" in str(response.json()["detail"])

    def test_clearing_it_returns_the_page_to_the_model(self, client, manga):
        client.post(f"/api/projects/{manga}/pages/00000001/order",
                    json={"ids": ["r1", "r0"]})

        client.post(f"/api/projects/{manga}/pages/00000001/order",
                    json={"ids": None})

        assert pages_mod.load_pages(manga)["pages"][0]["order"] is None

    def test_an_unknown_page_is_a_clean_404(self, client, manga):
        assert client.post(f"/api/projects/{manga}/pages/deadbeef/order",
                           json={"ids": []}).status_code == 404


# ---- editing a line ----------------------------------------------------------

class TestEditingALine:
    def test_an_edit_is_marked_as_the_human_s(self, client, built):
        response = client.post(
            f"/api/projects/{built}/pages/00000001/lines/r0",
            json={"english": "My own wording."})

        line = response.json()["line"]
        assert line["english"] == "My own wording."
        assert line["english_source"] == "user"

    def test_an_edit_is_not_born_stale(self, client, built):
        """The edit is made while looking at the words on the page NOW, so it is
        stamped against those. Without that it would appear immediately as needing
        attention it does not need."""
        client.post(f"/api/projects/{built}/pages/00000001/lines/r0",
                    json={"english": "Mine."})

        chapter = client.get(f"/api/projects/{built}/manga/1").json()
        line = next(r for r in chapter["pages"][0]["regions"] if r["id"] == "r0")
        assert line["stale"] is False

    def test_a_speaker_can_be_set_without_touching_the_english(self, client, built):
        client.post(f"/api/projects/{built}/pages/00000001/lines/r0",
                    json={"english": "Mine."})
        client.post(f"/api/projects/{built}/pages/00000001/lines/r0",
                    json={"speaker": "Aoi"})

        line = pages_mod.load_pages(built)["pages"][0]["lines"]["r0"]
        assert line["english"] == "Mine." and line["speaker"] == "Aoi"
        assert line["speaker_source"] == "user"

    def test_an_empty_request_changes_nothing(self, client, built):
        assert client.post(f"/api/projects/{built}/pages/00000001/lines/r0",
                           json={}).status_code == 400

    def test_an_unknown_region_is_a_clean_404(self, client, built):
        assert client.post(f"/api/projects/{built}/pages/00000001/lines/r99",
                           json={"english": "x"}).status_code == 404


# ---- what the reader is given ------------------------------------------------

class TestTheChapterPayload:
    def test_it_carries_the_boxes_as_fractions(self, client, built):
        body = client.get(f"/api/projects/{built}/manga/1").json()

        box = body["pages"][0]["regions"][0]["box"]
        assert len(box) == 4 and all(0 <= v <= 1 for v in box)

    def test_it_says_which_regions_can_be_drawn(self, client, manga):
        """Computed server-side so the reader's "N lines could not be placed" banner
        and what it actually draws cannot disagree."""
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"][0]["read"]["regions"] = [
                region("r0", Q, 0, box=(0.0, 0.0, 0.0, 0.0)),
                region("r1", A, 1, box=(0.1, 0.1, 0.2, 0.2)),
            ]
        client.post(f"/api/projects/{manga}/pages/build")

        regions = client.get(f"/api/projects/{manga}/manga/1").json()[
            "pages"][0]["regions"]
        assert [r["drawable"] for r in regions] == [False, True]

    def test_it_groups_the_regions_into_panels(self, client, built):
        body = client.get(f"/api/projects/{built}/manga/1").json()
        page = body["pages"][0]
        assert page["panels"]
        assert all(r["panel"] >= 1 for r in page["regions"])

    def test_it_marks_a_line_whose_japanese_changed(self, client, built):
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)

        with pages_mod.mutate_pages(built) as doc:
            doc["pages"][0]["read"]["regions"][0]["text"] = "まったく違う"

        regions = client.get(f"/api/projects/{built}/manga/1").json()[
            "pages"][0]["regions"]
        assert next(r for r in regions if r["id"] == "r0")["stale"] is True

    def test_it_says_when_a_page_looks_backwards(self, client, manga):
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"][0]["read"]["regions"] = [
                region("tl", "a", 0, box=(0.05, 0.05, 0.40, 0.40)),
                region("tr", "b", 1, box=(0.55, 0.05, 0.40, 0.40)),
                region("bl", "c", 2, box=(0.05, 0.55, 0.40, 0.40)),
                region("br", "d", 3, box=(0.55, 0.55, 0.40, 0.40)),
            ]
        client.post(f"/api/projects/{manga}/pages/build")

        page = client.get(f"/api/projects/{manga}/manga/1").json()["pages"][0]
        assert page["order_check"]["looks_reversed"] is True

    def test_a_sound_effect_is_translatable_and_a_page_number_is_not(
            self, client, manga):
        from morning.pageread import KIND_PAGE_NUMBER

        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"][0]["read"]["regions"] = [
                region("r0", "ドン", 0, kind=KIND_SFX),
                region("r1", "42", 1, kind=KIND_PAGE_NUMBER),
            ]
        client.post(f"/api/projects/{manga}/pages/build")

        regions = client.get(f"/api/projects/{manga}/manga/1").json()[
            "pages"][0]["regions"]
        assert [r["translatable"] for r in regions] == [True, False]

    def test_it_links_to_the_next_chapter(self, client, built):
        body = client.get(f"/api/projects/{built}/manga/1").json()
        assert body["prev"] is None and body["next"] == 2

    def test_an_unknown_chapter_is_a_clean_404(self, client, built):
        assert client.get(f"/api/projects/{built}/manga/99").status_code == 404

    def test_reordering_after_translating_is_noted_not_re_billed(self, client, built):
        """No bubble's Japanese changed, so nothing goes stale and nothing is charged
        again — but a line translated believing it followed line A now follows line B,
        and Japanese subject omission means that can change the English."""
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)
        assert client.get(f"/api/projects/{built}/manga/1").json()[
            "order_changed"] is False

        client.post(f"/api/projects/{built}/pages/00000001/order",
                    json={"ids": ["r1", "r0"]})

        body = client.get(f"/api/projects/{built}/manga/1").json()
        assert body["order_changed"] is True
        assert all(not r["stale"] for r in body["pages"][0]["regions"])


# ---- what a chapter's pages ARE, and whether it still needs work --------------
# Four bugs with one root: a chapter's contents were read from the `page_ids` list in
# the order it was written, and its done-ness was read off a stored status. Both go
# stale the moment the pages change underneath them.

class TestTheChapterFollowsItsPages:
    def test_reordering_pages_changes_the_reading_order(self, client, built):
        """The owner's most explicit statement about order. The pages screen obeyed it
        and the reader did not — two screens silently disagreeing, with the one that
        obeyed being the one nobody reads in."""
        with pages_mod.mutate_pages(built) as doc:
            doc["chapters"] = [{"index": 1, "title": "all", "start_seq": 1,
                                "end_seq": 2,
                                "page_ids": ["00000001", "00000002"], "status": ""}]

        before = client.get(f"/api/projects/{built}/manga/1").json()
        assert [p["seq"] for p in before["pages"]] == [1, 2]

        client.post(f"/api/projects/{built}/pages/reorder",
                    json={"ids": ["00000002", "00000001"]})

        after = client.get(f"/api/projects/{built}/manga/1").json()
        assert [p["seq"] for p in after["pages"]] == [2, 1]

    def test_a_deleted_page_leaves_the_chapter(self, client, built):
        with pages_mod.mutate_pages(built) as doc:
            doc["chapters"] = [{"index": 1, "title": "all", "start_seq": 1,
                                "end_seq": 2,
                                "page_ids": ["00000001", "00000002"], "status": ""}]

        client.post(f"/api/projects/{built}/pages/delete", json={"ids": ["00000001"]})

        body = client.get(f"/api/projects/{built}/manga/1").json()
        assert [p["seq"] for p in body["pages"]] == [2]


class TestWhetherAChapterStillNeedsTranslating:
    def test_deleting_a_page_and_rebuilding_does_not_re_bill(self, client, built):
        """Demonstrated before the fix: translate ($0.02), delete one page, rebuild —
        and the chapter read as never translated while its surviving pages still held
        their English. A plain sweep then charged for it again."""
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)
        first = pages_mod.load_pages(built)["totals"]["cost_usd"]

        client.post(f"/api/projects/{built}/pages/delete", json={"ids": ["00000002"]})
        client.post(f"/api/projects/{built}/pages/build")

        queued = client.post(f"/api/projects/{built}/manga/translate",
                             json={}).json()["queued"]
        _await_idle(client, built)

        assert queued == [], "a chapter that is already translated was queued again"
        assert pages_mod.load_pages(built)["totals"]["cost_usd"] == first

    def test_a_line_typed_in_by_hand_completes_the_chapter(self, client, built,
                                                           fake_translator):
        """A chapter with one missing line stayed 'partly translated' forever and was
        re-billed IN FULL by every sweep — even after the owner supplied the line."""
        # Twice: half a chapter missing trips `should_retry`, and the retry must get
        # the same incomplete answer rather than the fake's helpful default.
        partial = f"1:r0{TAB}Aoi{TAB}No more.\n{NEW_TERMS_DELIMITER}\n[]"
        fake_translator.answers = [partial, partial]
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)
        assert 1 in client.post(f"/api/projects/{built}/manga/translate",
                                json={}).json()["queued"]

        client.post(f"/api/projects/{built}/pages/00000001/lines/r1",
                    json={"english": "Really?"})

        # Chapter 2 is untranslated and still belongs in a sweep; chapter 1 does not.
        assert 1 not in client.post(f"/api/projects/{built}/manga/translate",
                                    json={}).json()["queued"]

    def test_a_blank_english_is_not_a_delivered_line(self, client, built,
                                                     fake_translator):
        """An empty field counted as a delivered line, so a chapter with bubbles still
        in Japanese was marked ok and the sweep never went back for them."""
        blank = (f"1:r0{TAB}Aoi{TAB}No more.\n1:r1{TAB}Kenji{TAB}\n"
                 f"{NEW_TERMS_DELIMITER}\n[]")
        fake_translator.answers = [blank, blank]
        client.post(f"/api/projects/{built}/manga/translate", json={"indices": [1]})
        _await_idle(client, built)

        chapter = pages_mod.load_pages(built)["chapters"][0]
        assert chapter["status"] == pages_mod.STATUS_NEEDS_CHECK
        assert "1:r1" in chapter["missing"]
        assert 1 in client.post(f"/api/projects/{built}/manga/translate",
                                json={}).json()["queued"]

    def test_unread_pages_are_picked_up_once_they_are_read(self, client, manga):
        """Building and translating before the pages were read marked every chapter
        'Translated' permanently, because there were no lines to fail — and the sweep
        then refused them forever."""
        with pages_mod.mutate_pages(manga) as doc:
            for page in doc["pages"]:
                page["read"] = None
                page["status"] = "new"
        client.post(f"/api/projects/{manga}/pages/build")
        client.post(f"/api/projects/{manga}/manga/translate", json={})
        _await_idle(client, manga)

        # The pages are read at last, so there is something to translate.
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"][0]["read"] = {
                "width": 1600, "height": 2400,
                "regions": [region("r0", Q, 0)], "order_source": "model",
                "meta": {"confidence": "high", "heading": None, "notes": []}}
            doc["pages"][0]["status"] = "ok"

        assert client.post(f"/api/projects/{manga}/manga/translate",
                           json={}).json()["queued"], (
            "a chapter whose pages have now been read was never offered again")


class TestTheBackwardsPageWarning:
    def test_a_read_writes_the_order_check(self, client, manga, fake_translator):
        """It was written only when a human set an order — so on a page nobody had
        reordered it was absent, and the pages grid's warning and
        `summary.reversed_pages` were both dead. That warning is the one thing in the
        app that catches a page transcribed backwards."""
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"] = [_page(1, [])]
            doc["pages"][0]["read"] = None
            doc["pages"][0]["status"] = "new"
            doc["pages"][0]["file"] = "page-0001.jpg"

        rows = client.get(f"/api/projects/{manga}/pages").json()["pages"]
        assert "looks_reversed" in rows[0]


# ---- seams are proposed between the pages that will actually be built ---------
# `propose_joins` filtered to APPROVED_STATUSES and zipped consecutive SURVIVORS. A
# manga chapter-title page is stylised art with a huge vertical title — exactly the
# page the reader marks `needs-check` — so it was dropped from the pairing, its printed
# heading never became a `chapter` seam, and two chapters built as one. Worse, the pair
# that WAS evaluated jumped the gap, so the page after it was stamped `gap` and the
# build warned about a page that was never missing.

class TestSeamProposal:
    def test_an_unchecked_title_page_still_starts_a_chapter(self, client, manga):
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"] = [
                _page(1, [region("r0", Q, 0)]),
                _page(2, [region("r0", "第2話", 0)], status="needs-check",
                      heading="第2話"),
                _page(3, [region("r0", "次の日", 0)]),
            ]

        client.post(f"/api/projects/{manga}/pages/propose-joins")

        doc = pages_mod.load_pages(manga)
        assert doc["pages"][1]["join_prev"] == "chapter", (
            "the title page was skipped, so its heading never became a seam")

        built = client.post(f"/api/projects/{manga}/pages/build").json()
        assert built["chapters"] == 2

    def test_no_false_missing_page_warning_is_raised_over_it(self, client, manga):
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"] = [
                _page(1, [region("r0", Q, 0)]),
                _page(2, [region("r0", "第2話", 0)], status="needs-check",
                      heading="第2話"),
                _page(3, [region("r0", "次の日", 0)]),
            ]

        client.post(f"/api/projects/{manga}/pages/propose-joins")

        assert pages_mod.load_pages(manga)["pages"][2]["join_prev"] != "gap"

    def test_a_page_marked_not_text_is_still_skipped(self, client, manga):
        """`skipped` means "deliberately not part of the book" — that one really does
        leave the pairing."""
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"] = [
                _page(1, [region("r0", Q, 0)]),
                _page(2, [region("r0", "裏表紙", 0)], status="skipped"),
                _page(3, [region("r0", "次の日", 0)], heading="第2話"),
            ]

        client.post(f"/api/projects/{manga}/pages/propose-joins")

        doc = pages_mod.load_pages(manga)
        assert doc["pages"][1]["join_prev"] == ""      # never paired
        assert doc["pages"][2]["join_prev"] == "chapter"

    def test_a_page_that_was_never_read_contributes_no_seam(self, client, manga):
        with pages_mod.mutate_pages(manga) as doc:
            doc["pages"] = [_page(1, [region("r0", Q, 0)]), _page(2, [])]
            doc["pages"][1]["read"] = None
            doc["pages"][1]["status"] = "new"

        client.post(f"/api/projects/{manga}/pages/propose-joins")

        assert pages_mod.load_pages(manga)["pages"][1]["join_prev"] == ""
