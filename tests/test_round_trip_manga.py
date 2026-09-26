"""A manga page, photograph to English over the art, as one chain over HTTP.

The plan's verification line for the manga path is "upload → regions → order →
translate → overlay", asserting that every region has a box inside the page, that
order is a permutation of the regions, and that a re-scan at another resolution leaves
positions valid. ``tests/test_manga_api.py`` covers each route, but every test there
injects regions straight into the manifest — so none of them shows that what the page
reader actually answers, boxes in PIXELS of the image it was shown, reaches the overlay
as fractions that sit on the art. This one starts from an uploaded image.

Each link can pass its own tests while the chain still draws the wrong thing. A box
divided by the wrong side of the page lands off the art. A human's corrected order that
is recorded but never applied hands the translator the page backwards, which comes out
as fluent English that makes no sense. A re-scan that moves every box, or makes every
paid-for line look stale, turns a better photograph into a re-bill. The payload the
reader draws from is the one place all of those show at once.

**The re-scan.** The app has no route that replaces a page's image, so the swap is done
at the storage layer, the way such a route would do it: the new bytes go into the
page's own file, and the new size is MEASURED from them by ``morning.images.inspect``.
Everything after that is the real route: the re-read, where its reply lands, and what
the reader is given.

No test here makes a real model call. ``server.jobs.Translator`` is replaced with one
stand-in that both jobs build through. It answers a page read (the only call given the
``Read`` tool) as JSON in pixels of the image actually on disk, the way the model does.
It answers a script call line by line, for the ids it was actually asked about, which
is the pattern from ``tests/test_manga_api.py``. Japanese fixtures are invented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from morning.images import inspect
from morning.pageread import KIND_BUBBLE, KIND_PAGE_NUMBER, from_pixels
from morning.prompts import NEW_TERMS_DELIMITER
from server import jobs, pages as pages_mod, projects as pj
from server.app import app
from tests.test_images import jpeg
from tests.test_pages_api import _await_idle, _upload

TAB = "\t"
W, H = 1600, 2400                 # the first scan
RESCAN_W, RESCAN_H = 800, 1200    # the same page, photographed again at half the size

# One page: four bubbles in a two-by-two grid of panels, and a printed page number.
PAGE_NUMBER = "—7—"
TOP_RIGHT, TOP_LEFT = "傘、持ってきた？", "あ、忘れた"
BOTTOM_RIGHT, BOTTOM_LEFT = "しょうがないなあ", "入れてくれるの？"

# As the model answers it: in DETECTION order, the order it happens to list them in,
# with boxes in pixels of a 1600x2400 scan. The numbers are odd on purpose. At half size
# they do not halve exactly, so the re-scan's boxes differ by rounding, as a real
# model's whole-pixel answer would.
#
# Its proposed reading order is LEFT to right, which is backwards on a Japanese page and
# is the mistake the human corrects below.
DETECTED = [  # (text, kind, (x, y, w, h) in pixels, the model's order)
    (PAGE_NUMBER, KIND_PAGE_NUMBER, (741, 2263, 117, 83), 4),
    (TOP_LEFT, KIND_BUBBLE, (201, 163, 385, 477), 0),
    (BOTTOM_LEFT, KIND_BUBBLE, (187, 1301, 403, 461), 2),
    (TOP_RIGHT, KIND_BUBBLE, (1003, 141, 397, 519), 1),
    (BOTTOM_RIGHT, KIND_BUBBLE, (1011, 1257, 389, 503), 3),
]

# Right to left, top to bottom: how the page is actually read.
HUMAN_ORDER = [TOP_RIGHT, TOP_LEFT, BOTTOM_RIGHT, BOTTOM_LEFT, PAGE_NUMBER]
SPOKEN = [t for t in HUMAN_ORDER if t != PAGE_NUMBER]   # what gets translated

ENGLISH = {
    TOP_RIGHT: "Did you bring an umbrella?",
    TOP_LEFT: "Oh. I forgot.",
    BOTTOM_RIGHT: "Honestly, you're hopeless.",
    BOTTOM_LEFT: "You'll share yours?",
}

_PAGE_FILE_RE = re.compile(r"page-\d+\.\w+")
_LINE_ID_RE = re.compile(r"^\d+:\S+$")
_PANEL_RE = re.compile(r"^Page \d+ — panel (\d+)$")


class FakeModel:
    """Stands in for the real Translator at the seam the worker builds through.

    One class for both jobs, because the real one is: a page read and a script
    translation both go through ``_call``. They are told apart by the one thing only a
    page read is given, the ``Read`` tool.

    A page read MEASURES the image it was pointed at and answers in that image's
    pixels, as the model does. So a re-scan at another size is answered in the new
    size's pixels without the test saying so, and a box that comes back wrong is the
    app's arithmetic rather than the double's.
    """

    detected: list = DETECTED
    shown: list = []      # (width, height) of every image a page read was pointed at
    scripts: list = []    # every script message, exactly as the translator saw it

    def __init__(self, *_a, **_kw):
        from morning.config import TranslationConfig

        self.tcfg = TranslationConfig()

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        if tools:
            return self._read_page(user_text, cwd)
        return self._translate(user_text)

    def _read_page(self, user_text, cwd):
        name = _PAGE_FILE_RE.search(user_text).group(0)
        info = inspect((Path(cwd) / name).read_bytes())
        FakeModel.shown.append((info.width, info.height))
        sx, sy = info.width / W, info.height / H
        regions = [{"box": [round(x * sx), round(y * sy), round(w * sx), round(h * sy)],
                    "text": text, "kind": kind, "order": order}
                   for text, kind, (x, y, w, h), order in FakeModel.detected]
        return json.dumps({
            "width": info.width, "height": info.height, "regions": regions,
            "meta": {"confidence": "high", "heading": None,
                     "starts_mid_sentence": False, "ends_mid_sentence": False,
                     "ends_mid_word": False, "notes": []},
        }, ensure_ascii=False), {"input_tokens": 50}, 0.02

    def _translate(self, user_text):
        FakeModel.scripts.append(user_text)
        rows = []
        for row in user_text.splitlines():
            parts = row.split(TAB)
            if len(parts) < 3 or not _LINE_ID_RE.match(parts[0]):
                continue
            japanese = parts[2].strip()
            english = ENGLISH.get(japanese, f"[{japanese}]")
            rows.append(TAB.join([parts[0], "", english]))
        answer = "\n".join(rows) + f"\n{NEW_TERMS_DELIMITER}\n[]"
        return answer, {"input_tokens": 100, "output_tokens": 50}, 0.02


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    FakeModel.detected = DETECTED
    FakeModel.shown = []
    FakeModel.scripts = []
    monkeypatch.setattr(jobs, "Translator", FakeModel)
    yield FakeModel


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---- helpers ---------------------------------------------------------------------

def _new_manga(client) -> tuple[str, str]:
    """A manga with one uploaded page, through the route the new-work screen calls."""
    pid = client.post("/api/projects/scan", json={
        "title": "傘の日", "kind": pj.KIND_MANGA}).json()["project"]["id"]
    added = _upload(client, pid, jpeg(W, H)).json()["added"]
    assert len(added) == 1 and added[0]["measured"] is True
    return pid, added[0]["id"]


def _read(client, pid: str, **body) -> None:
    started = client.post(f"/api/projects/{pid}/pages/read", json=body)
    assert started.status_code == 200, started.text
    assert started.json()["queued"] == [1], "the page was not queued for reading"
    _await_idle(client, pid)


def _ids_by_text(client, pid: str, page_id: str) -> dict[str, str]:
    stored = client.get(f"/api/projects/{pid}/pages/{page_id}").json()["page"]
    return {r["text"]: r["id"] for r in stored["read"]["regions"]}


def _build(client, pid: str) -> None:
    built = client.post(f"/api/projects/{pid}/pages/build")
    assert built.status_code == 200, built.text
    assert built.json()["chapters"] == 1


def _order_and_translate(client, pid: str, page_id: str) -> list[str]:
    """The human's order, then translate. Returns the order, as region ids."""
    ids = _ids_by_text(client, pid, page_id)
    human = [ids[text] for text in HUMAN_ORDER]
    ordered = client.post(f"/api/projects/{pid}/pages/{page_id}/order",
                          json={"ids": human})
    assert ordered.status_code == 200, ordered.text
    assert ordered.json()["order_check"]["order_source"] == "user"

    started = client.post(f"/api/projects/{pid}/manga/translate", json={})
    assert started.status_code == 200, started.text
    assert started.json()["queued"] == [1]
    _await_idle(client, pid)
    return human


def _replace_image(pid: str, page_id: str, data: bytes) -> None:
    """Swap a page's photograph for a re-scan of it, as a replace route would.

    The new bytes go into the page's own file and the size is MEASURED from them,
    never stated, because a page's size is what every box on it is a fraction of.
    """
    info = inspect(data)
    assert info.measured
    with pages_mod.mutate_pages(pid) as doc:
        page = pages_mod.find_page(doc, page_id)
        (pages_mod.pages_dir(pid) / page["file"]).write_bytes(data)
        page.update(width=info.width, height=info.height, bytes=len(data),
                    sha256=pages_mod.sha256_of(data))


def _told(script: str) -> dict[str, tuple[int, int, str]]:
    """What the translator was handed, by region id: (position, panel, Japanese)."""
    out: dict[str, tuple[int, int, str]] = {}
    panel = 0
    for row in script.splitlines():
        header = _PANEL_RE.match(row.strip())
        if header:
            panel = int(header.group(1))
            continue
        parts = row.split(TAB)
        if len(parts) >= 3 and _LINE_ID_RE.match(parts[0]):
            out[parts[0].split(":", 1)[1]] = (len(out), panel, parts[2].strip())
    return out


def _assert_every_box_is_on_the_page(regions: list[dict]) -> None:
    for r in regions:
        x, y, w, h = r["box"]
        assert 0 <= x and 0 <= y and x + w <= 1 and y + h <= 1, (
            f"{r['text']!r} is not inside the page: {r['box']}")
        assert w > 0 and h > 0 and r["drawable"] is True, (
            f"{r['text']!r} cannot be drawn: {r['box']}")


# ---- the chain -------------------------------------------------------------------

def test_a_scanned_manga_page_reaches_the_overlay_and_survives_a_rescan(client,
                                                                       fake_model):
    """Photograph in, English over the art out, then the same page photographed again.

    Guards the chain as a whole rather than any one link. Each of these still leaves
    every earlier stage looking fine, and each shows up here, in what the overlay draws:
    a pixel box stored as pixels or divided by the wrong side, a human order recorded
    but never applied, panels numbered differently from what the translator was told,
    and a re-scan that moves the boxes or makes a paid-for line look stale.
    """
    pid, page_id = _new_manga(client)

    # ---- regions: the model's pixels, stored as fractions of THIS image ----
    _read(client, pid)
    assert fake_model.shown == [(W, H)]
    stored = client.get(f"/api/projects/{pid}/pages/{page_id}").json()["page"]
    boxes = {r["text"]: r["box"] for r in stored["read"]["regions"]}
    for text, _kind, (x, y, w, h), _order in DETECTED:
        # Stored rounded to six places, hence the tolerance.
        assert boxes[text] == pytest.approx(list(from_pixels(x, y, w, h, W, H)),
                                            abs=1e-6)
    assert stored["status"] == pages_mod.STATUS_OK, stored["read"]["meta"]["notes"]

    # The model read it left to right, and the page's own geometry says so.
    assert client.get(f"/api/projects/{pid}/pages").json()["pages"][0][
        "looks_reversed"] is True

    # ---- build: before anyone corrects it, the page is shown in the model's order,
    # with its panels numbered in THAT order rather than by raw geometry ----
    _build(client, pid)
    as_read = client.get(f"/api/projects/{pid}/manga/1").json()["pages"][0]["regions"]
    assert [r["text"] for r in as_read if r["translatable"]] == [
        TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT]
    assert [r["panel"] for r in as_read if r["translatable"]] == [1, 2, 3, 4]

    # ---- order and translate ----
    human = _order_and_translate(client, pid, page_id)

    # The translator was handed the page in the HUMAN's order, and nothing else.
    told = _told(fake_model.scripts[-1])
    assert [text for _pos, _panel, text in sorted(told.values())] == SPOKEN

    # ---- overlay ----
    chapter = client.get(f"/api/projects/{pid}/manga/1").json()
    page = chapter["pages"][0]
    regions = page["regions"]

    _assert_every_box_is_on_the_page(regions)
    assert sorted(r["order"] for r in regions) == list(range(len(DETECTED))), (
        "reading order is not a permutation of the regions")
    assert [r["id"] for r in regions] == human, "the human's order is not the one shown"
    assert (page["order_source"], page["order_note"]) == ("user", "")

    spoken = [r for r in regions if r["translatable"]]
    assert [r["text"] for r in spoken] == SPOKEN
    assert [r["english"] for r in spoken] == [ENGLISH[t] for t in SPOKEN]
    assert [r["stale"] for r in spoken] == [False] * len(SPOKEN)
    # Four panels, numbered in reading order, and each line shows the panel the
    # translator was told it is in.
    assert [r["panel"] for r in spoken] == [1, 2, 3, 4]
    assert {r["id"]: r["panel"] for r in spoken} == {
        rid: panel for rid, (_pos, panel, _text) in told.items()}
    assert chapter["order_changed"] is False
    assert (chapter["counts"]["translated"], chapter["counts"]["stale"]) == (4, 0)
    before = {r["text"]: r["box"] for r in regions}
    ids = _ids_by_text(client, pid, page_id)

    # ---- re-scan: the same page at half the size, read again ----
    _replace_image(pid, page_id, jpeg(RESCAN_W, RESCAN_H))
    _read(client, pid, ids=[page_id], force=True)
    assert fake_model.shown[-1] == (RESCAN_W, RESCAN_H)
    stored = client.get(f"/api/projects/{pid}/pages/{page_id}").json()["page"]
    read_size = (stored["read"]["width"], stored["read"]["height"])
    assert read_size == (RESCAN_W, RESCAN_H), (
        "the re-read never landed, so nothing below says anything about a re-scan")
    assert stored["status"] == pages_mod.STATUS_OK, stored["read"]["meta"]["notes"]
    # Listed in the same order as the first read, so every region kept its id. A re-read
    # that lists them in another order is the xfail test below.
    assert _ids_by_text(client, pid, page_id) == ids

    chapter = client.get(f"/api/projects/{pid}/manga/1").json()
    page = chapter["pages"][0]
    regions = page["regions"]

    _assert_every_box_is_on_the_page(regions)
    # The same place on the art, to within the one pixel the smaller scan can resolve.
    one_pixel = 1 / min(RESCAN_W, RESCAN_H)
    for r in regions:
        assert r["box"] == pytest.approx(before[r["text"]], abs=one_pixel), (
            f"{r['text']!r} moved when the page was re-scanned")

    # The human's order survives the re-read, and the English is still true.
    assert [r["text"] for r in regions] == HUMAN_ORDER
    assert (page["order_source"], page["order_note"]) == ("user", "")
    spoken = [r for r in regions if r["translatable"]]
    assert [r["english"] for r in spoken] == [ENGLISH[t] for t in SPOKEN]
    assert [r["stale"] for r in spoken] == [False] * len(SPOKEN), (
        "no bubble's Japanese changed, so no line may be marked stale")
    assert [r["panel"] for r in spoken] == [1, 2, 3, 4]
    assert chapter["order_changed"] is False
    assert (chapter["counts"]["translated"], chapter["counts"]["stale"]) == (4, 0)

    # And so nothing is offered for translating again.
    assert client.post(f"/api/projects/{pid}/manga/translate",
                       json={}).json()["queued"] == []


def test_a_rescan_that_lists_the_bubbles_in_another_order_keeps_each_line_on_its_bubble(
        client, fake_model):
    """A model lists regions in whatever order it detects them, and a second read of
    the same page need not list them the way the first did.
    ``pageread.apply_text_order`` says so, and re-matches a human's saved order on the
    words for that reason.

    Lines were not re-matched — they are keyed by region id, and ids are list
    positions — so after such a re-scan a bubble showed another bubble's English,
    flagged stale, or none, and the only way back was to pay to translate again a
    chapter whose Japanese never changed. ``pages.carry_lines`` moves each line to the
    region that says its words.
    """
    pid, page_id = _new_manga(client)
    _read(client, pid)
    _build(client, pid)
    _order_and_translate(client, pid, page_id)

    fake_model.detected = list(reversed(DETECTED))
    _replace_image(pid, page_id, jpeg(RESCAN_W, RESCAN_H))
    _read(client, pid, ids=[page_id], force=True)

    chapter = client.get(f"/api/projects/{pid}/manga/1").json()
    spoken = [r for r in chapter["pages"][0]["regions"] if r["translatable"]]
    assert [r["text"] for r in spoken] == SPOKEN     # the saved order did survive
    assert {r["text"]: r["english"] for r in spoken} == {t: ENGLISH[t] for t in SPOKEN}
    assert [r["stale"] for r in spoken] == [False] * len(SPOKEN)
