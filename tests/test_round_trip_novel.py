"""A scanned novel, photographs to English, as one chain over HTTP.

The plan's verification line for the novel path is "upload → regions → flatten →
chapters → translate → read". ``tests/test_pages_api.py`` walks it as far as chapters;
this carries on through a real queued translation and the reader, because the claim
that matters is about the far end: **a book that arrived as photographs is read exactly
like a pasted one.** Each link can pass its own tests while the chain still delivers
the wrong prose — a page number welded into a sentence, a column read left to right,
a validated chapter shown as an unaccepted review copy — and only the reader shows it.

It also pins the plan's "regions cost the novel path nothing": reading a page into
positioned regions must build the same chapter, byte for byte, as a flat-text read of
the same page would have. If it did not, carrying geometry for manga would be quietly
taxing every novel.

No test here makes a real model call. ``server.jobs.Translator`` is replaced with one
stand-in that both jobs build through — its ``_call`` answers page reads, as JSON with
boxes in PIXELS the way the model answers, and its ``translate_chapter`` returns fixed
English that genuinely passes ``morning.validate``. Japanese fixtures are invented.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from morning.pageread import JOIN_SENTENCE, KIND_PAGE_NUMBER, from_pixels
from morning.state import STATUS_VALIDATED
from morning.translator import TranslationResult
from server import jobs, pages as pages_mod, projects as pj
from server.app import app
from tests.test_images import jpeg
from tests.test_pages_api import _await_idle, _upload

W, H = 1600, 2400

# The novel, as a reader would have it: three paragraphs. The second is broken across
# the page turn; the third across a column break on page two.
OPENING = "朝の光が窓から差し込んだ。"
CAT_HEAD, CAT_TAIL = "猫は机の上で", "丸くなって眠っていた。"
SAID_HEAD, SAID_TAIL = "「起きて」と彼女は", "小さく言った。"
PARAGRAPHS = [OPENING, CAT_HEAD + CAT_TAIL, SAID_HEAD + SAID_TAIL]

# What a flat-text read of each page returns: its prose in reading order, paragraphs
# separated by a blank line, no page number.
PAGE_ONE_TEXT = f"{OPENING}\n\n{CAT_HEAD}"
PAGE_TWO_TEXT = f"{CAT_TAIL}\n\n{SAID_HEAD}{SAID_TAIL}"

ENGLISH = {
    PARAGRAPHS[0]: "Morning light slanted in through the window.",
    PARAGRAPHS[1]: "The cat lay curled up asleep on the desk.",
    PARAGRAPHS[2]: "“Wake up,” she said quietly.",
}
ENGLISH_TEXT = "\n\n".join(ENGLISH[p] for p in PARAGRAPHS)

# Page one, as the model answers it: pixels, and in DETECTION order rather than reading
# order. Vertical text reads right to left, so the right-hand column (listed last) is
# read first. The page number is listed first and read nowhere.
REGION_READ = {
    1: {"regions": [
            {"box": [750, 2250, 100, 80], "text": "12", "kind": KIND_PAGE_NUMBER,
             "order": 2},
            {"box": [300, 200, 300, 1900], "text": CAT_HEAD, "kind": "body",
             "order": 1, "join_prev": "paragraph", "join_glue": "none"},
            {"box": [1200, 200, 300, 1900], "text": OPENING, "kind": "body",
             "order": 0},
        ],
        "meta": {"ends_mid_sentence": True}},
    2: {"regions": [
            # A column break in the middle of a sentence: joined with no glue.
            {"box": [200, 200, 250, 1900], "text": SAID_TAIL, "kind": "body",
             "order": 2, "join_prev": "sentence", "join_glue": "none"},
            {"box": [1200, 200, 300, 1900], "text": CAT_TAIL, "kind": "body",
             "order": 0},
            # A decorated page number the model mislabelled as prose. Its SHAPE
            # proves what it is, which is the rule `pageread.mark_furniture` applies.
            {"box": [740, 2250, 120, 80], "text": "—13—", "kind": "body",
             "order": 3, "join_prev": "paragraph"},
            {"box": [700, 200, 300, 1900], "text": SAID_HEAD, "kind": "body",
             "order": 1, "join_prev": "paragraph", "join_glue": "none"},
        ],
        "meta": {"starts_mid_sentence": True}},
}

# The same two pages read the old way: one block of flat text each, same judgements.
FLAT_READ = {
    1: {"regions": [{"box": [300, 200, 1200, 1900], "text": PAGE_ONE_TEXT,
                     "kind": "body", "order": 0}],
        "meta": {"ends_mid_sentence": True}},
    2: {"regions": [{"box": [200, 200, 1300, 1900], "text": PAGE_TWO_TEXT,
                     "kind": "body", "order": 0}],
        "meta": {"starts_mid_sentence": True}},
}

_PAGE_FILE_RE = re.compile(r"page-(\d+)\.")


class FakeEngine:
    """Stands in for the real Translator at the seam the worker builds through.

    One class for both jobs, because the real one is: a page read goes through
    ``_call`` and a chapter through ``translate_chapter``. The page answered is picked
    by the image file NAMED in the message (``page-0001.jpg`` is seq 1), so what comes
    back depends on which page was actually shown rather than on call order.
    """

    script: dict[int, dict] = {}
    translated: list = []   # the chapters handed to the model, in order

    def __init__(self, *_a, **_kw):
        pass

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        seq = int(_PAGE_FILE_RE.search(user_text).group(1))
        entry = FakeEngine.script[seq]
        meta = {"confidence": "high", "heading": None, "starts_mid_sentence": False,
                "ends_mid_sentence": False, "ends_mid_word": False, "notes": [],
                **entry["meta"]}
        return json.dumps({"width": W, "height": H, "regions": entry["regions"],
                           "meta": meta}, ensure_ascii=False), \
            {"input_tokens": 50}, 0.02

    def translate_chapter(self, chapter, *, glossary_block="", hooks=None,
                          retry_hint="", spend=None):
        FakeEngine.translated.append(chapter)
        # A paragraph it was not written for comes back untranslated, as a lazy model's
        # would — and the validator's residue check then fails it, loudly.
        english = "\n\n".join(ENGLISH.get(p, p) for p in chapter.paragraphs)
        return TranslationResult(english=english,
                                 usage={"input_tokens": 100, "output_tokens": 50},
                                 cost_usd=0.05, chunks=1)


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    FakeEngine.script = REGION_READ
    FakeEngine.translated = []
    monkeypatch.setattr(jobs, "Translator", FakeEngine)
    yield FakeEngine


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _scan_novel(client, title: str) -> tuple[str, list[str]]:
    """upload → regions → flatten → chapters, through the routes the screens call.

    Two pages of the same size. The second carries a little padding so the bytes
    differ; otherwise it is a duplicate and never stored.
    """
    pid = client.post("/api/projects/scan", json={"title": title}).json()["project"]["id"]
    ids = [a["id"] for a in _upload(client, pid, jpeg(W, H),
                                    jpeg(W, H, padding=32)).json()["added"]]
    assert len(ids) == 2, "both pages must be stored"

    client.post(f"/api/projects/{pid}/pages/read", json={})
    _await_idle(client, pid)
    client.post(f"/api/projects/{pid}/pages/propose-joins")
    built = client.post(f"/api/projects/{pid}/pages/build")
    assert built.status_code == 200, built.text
    return pid, ids


# ---- the chain ------------------------------------------------------------------

def test_a_scanned_novel_reaches_the_reader_as_validated_english(client, fake_engine):
    """Photographs in, English out, through every route the owner's screens call.

    Guards the whole chain rather than any one link: a page number reaching the prose,
    regions read in detection order, a page turn left as a paragraph break, or a good
    chapter landing only in the audit copy each still leaves every earlier stage
    looking fine — and each shows up here, in the chapter the owner reads.
    """
    pid, ids = _scan_novel(client, "朝の猫")
    assert client.get(f"/api/projects/{pid}").json()["project"]["kind"] == pj.KIND_NOVEL

    # ---- regions: read, stored as fractions, trusted ----
    rows = client.get(f"/api/projects/{pid}/pages").json()["pages"]
    assert [r["status"] for r in rows] == [pages_mod.STATUS_OK] * 2
    first = client.get(f"/api/projects/{pid}/pages/{ids[0]}").json()
    opening = next(r for r in first["page"]["read"]["regions"] if r["text"] == OPENING)
    # Stored rounded to six places, hence the tolerance.
    assert opening["box"] == pytest.approx(
        list(from_pixels(1200, 200, 300, 1900, W, H)), abs=1e-6)

    # ---- flatten: reading order, prose only ----
    assert first["text"] == PAGE_ONE_TEXT
    assert client.get(f"/api/projects/{pid}/pages/{ids[1]}").json()["text"] == \
        PAGE_TWO_TEXT

    # ---- joins: the page turn is a sentence running on, not a missing page ----
    assert rows[1]["join_prev"] == JOIN_SENTENCE
    assert client.get(f"/api/projects/{pid}/pages").json()["summary"]["build"][
        "warnings"] == []

    # ---- chapters: one, and it is exactly the flattened text ----
    chapter = client.get(f"/api/projects/{pid}/chapters/1").json()
    assert chapter["source"] == PARAGRAPHS, (
        "the page number must be absent and the sentence across the page turn must "
        "be one paragraph, with no break and no space where the page ended")
    assert chapter["class"] == "source"

    # ---- translate: queued through the same route as a pasted novel ----
    started = client.post(f"/api/projects/{pid}/run", json={"kind": "translate"})
    assert started.status_code == 200, started.text
    assert started.json()["queued"] == [1]
    _await_idle(client, pid)

    # What the model was handed is the flattened prose and nothing else.
    assert [c.paragraphs for c in fake_engine.translated] == [PARAGRAPHS]

    # ---- read: validated English, not a review copy ----
    body = client.get(f"/api/projects/{pid}/read/1").json()
    assert body["english"].strip() == ENGLISH_TEXT
    assert body["from_audit"] is False
    assert body["status"] == STATUS_VALIDATED
    assert body["failures"] == []
    assert body["stale"] is False
    assert body["source"] == PARAGRAPHS


# ---- regions cost the novel path nothing -------------------------------------------

def test_a_region_read_builds_the_same_chapter_a_flat_text_read_would(client,
                                                                    fake_engine):
    """The plan's promise that carrying geometry for manga costs a novel nothing.

    The same two pages, read once into positioned regions and once as a block of flat
    text each, must build a byte-identical chapter — same paragraphs, same content
    hash. A difference means region flattening joins, orders or filters differently
    from what the prose machinery was built on, and every scanned novel inherits it:
    a changed hash alone re-bills a chapter that did not change.
    """
    fake_engine.script = REGION_READ
    regions_pid, regions_ids = _scan_novel(client, "regions")
    fake_engine.script = FLAT_READ
    flat_pid, flat_ids = _scan_novel(client, "flat")

    def page_text(pid, page_id):
        return client.get(f"/api/projects/{pid}/pages/{page_id}").json()["text"]

    for region_id, flat_id in zip(regions_ids, flat_ids):
        assert page_text(regions_pid, region_id) == page_text(flat_pid, flat_id)

    from_regions = client.get(f"/api/projects/{regions_pid}/chapters/1").json()
    from_flat = client.get(f"/api/projects/{flat_pid}/chapters/1").json()
    assert from_regions["source"] == from_flat["source"] == PARAGRAPHS
    assert from_regions["source_hash"] == from_flat["source_hash"]
    assert from_regions["title"] == from_flat["title"]
