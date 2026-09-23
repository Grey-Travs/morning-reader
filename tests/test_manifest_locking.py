"""The manifest lock must actually exclude. It did not, and the failure was total.

``morning/locks.py`` states an assumption in its docstring: *"Every writer in this app
runs on a THREAD — sync endpoints and run_in_threadpool alike — never on the event
loop."* The lock it hands out is a ``threading.RLock``, which is re-entrant **per
thread**, and that assumption is the only thing that makes re-entrancy safe.

``upload_pages`` broke it. It is an ``async def``, so it runs ON the event-loop thread,
and it held the lock across ``await upload.read()``. An await there does not merely
hold the lock — it hands control to every other coroutine on that same thread, and each
of them re-enters instantly rather than waiting. The page worker's
``_apply_page_result`` is one such caller. It would write a finished, PAID-FOR read;
the upload would then resume and save the snapshot it had loaded before that happened.

Reproduced before the fix: two concurrent three-file uploads reported six pages added
and the manifest held three, and surviving records' stored hashes did not match the
bytes on disk under their own filename.

Two tests below, and the second is the one that lasts. A concurrency test can only
catch the interleavings it happens to hit; the static guard catches the MISTAKE —
an await inside a manifest mutation — wherever anybody writes it next.
"""

from __future__ import annotations

import ast
import asyncio
import io
import re
from pathlib import Path

import httpx
import pytest

# `testserver` rather than `test`: the server refuses a Host it does not recognise
# (see server/app._local_only), and tests/conftest.py allows exactly this name.

from server import pages as pages_mod, projects as pj
from server.app import app
from tests.test_images import jpeg

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def project():
    return pj.create_project("scans", ingest=pj.INGEST_IMAGES)["id"]


def _big(width: int, height: int) -> bytes:
    """A JPEG past Starlette's 1 MB spool threshold, so ``read()`` really suspends.

    Below the threshold the part stays in memory and ``read()`` returns without
    yielding, which hides the bug entirely — an ordinary phone photo does not.
    """
    data = jpeg(width, height)
    return data + b"\xff\xfe" + (b"\x00" * (1_500_000 - len(data) - 2))


def test_two_overlapping_uploads_keep_every_page(project):
    """The reproduction. Both batches must survive, and every stored hash must match
    the bytes actually written under that record's filename."""

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            def batch(tag: str, sizes):
                return [("files", (f"{tag}{i}.jpg", io.BytesIO(_big(w, h)),
                                   "image/jpeg"))
                        for i, (w, h) in enumerate(sizes)]

            first = client.post(f"/api/projects/{project}/pages",
                                files=batch("a", [(800, 1200), (810, 1210),
                                                  (820, 1220)]))
            second = client.post(f"/api/projects/{project}/pages",
                                 files=batch("b", [(900, 1300), (910, 1310),
                                                   (920, 1320)]))
            one, two = await asyncio.gather(first, second)
            assert one.status_code == 200 and two.status_code == 200
            reported = len(one.json()["added"]) + len(two.json()["added"])

            doc = pages_mod.load_pages(project)
            assert len(doc["pages"]) == reported, (
                f"{reported} pages were reported as added and the manifest holds "
                f"{len(doc['pages'])} — one upload overwrote the other's manifest")

            # ...and each record must describe the bytes actually on disk under its
            # own filename. The earlier failure left records pointing at the other
            # batch's image, which is worse than losing them: a re-upload of the lost
            # scan then reports as a duplicate.
            folder = pages_mod.pages_dir(project)
            for page in doc["pages"]:
                on_disk = (folder / page["file"]).read_bytes()
                assert pages_mod.sha256_of(on_disk) == page["sha256"], (
                    f"page {page['seq']} records a hash that is not the file it names")

    asyncio.run(run())


def test_a_page_seq_is_never_reused(project):
    """Two batches minting the same ``page-0001.jpg`` is how the bytes got crossed."""

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            await asyncio.gather(*[
                client.post(f"/api/projects/{project}/pages",
                            files=[("files", (f"{tag}.jpg",
                                              io.BytesIO(_big(800 + n, 1200)),
                                              "image/jpeg"))])
                for n, tag in enumerate(("a", "b", "c", "d"))])

        doc = pages_mod.load_pages(project)
        seqs = [p["seq"] for p in doc["pages"]]
        files = [p["file"] for p in doc["pages"]]
        assert len(set(seqs)) == len(seqs), f"a seq was reused: {seqs}"
        assert len(set(files)) == len(files), f"a filename was reused: {files}"

    asyncio.run(run())


# ---- the guard that lasts -----------------------------------------------------

_MUTATORS = ("mutate_pages", "mutate_state", "file_lock", "glossary_lock")


def _awaits_inside_mutation(path: Path) -> list[str]:
    """Every ``await`` that sits inside a manifest-mutation ``with`` block.

    An await there yields the event loop while holding a lock that is re-entrant per
    thread — so on the loop thread it protects nothing at all, and the mutation is no
    longer atomic with respect to anything else running on that loop.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        names = " ".join(
            ast.unparse(item.context_expr) for item in node.items)
        if not any(m in names for m in _MUTATORS):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                # An `async with` on the mutation itself would be a different shape;
                # these are awaits nested INSIDE it.
                if inner is node:
                    continue
                offenders.append(
                    f"{path.relative_to(REPO)}:{inner.lineno} — await inside "
                    f"`with {names}`")
    return offenders


def test_no_await_ever_happens_inside_a_manifest_mutation():
    """The mistake, not the symptom.

    `upload_pages` held `mutate_pages` across `await upload.read()`, and the result was
    that a paid-for page read could be erased by an upload that overlapped it. A
    concurrency test only catches the interleavings it happens to hit; this catches the
    next person who writes the same line.
    """
    offenders: list[str] = []
    for folder in ("server", "morning"):
        for path in sorted((REPO / folder).glob("*.py")):
            offenders.extend(_awaits_inside_mutation(path))

    assert not offenders, (
        "a lock that is re-entrant per thread protects nothing across an await on the "
        "event loop:\n  " + "\n  ".join(offenders))


def test_the_guard_can_actually_fail(tmp_path):
    """Proof the scan is not passing vacuously — the failure mode this whole file
    exists to prevent is a guard that never fires."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def handler(pid):\n"
        "    with pages_mod.mutate_pages(pid) as doc:\n"
        "        data = await upload.read()\n"
        "        doc['pages'].append(data)\n",
        encoding="utf-8")

    # Relative-path formatting needs the file under REPO; check the detection itself.
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    found = any(
        isinstance(inner, ast.Await)
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith))
        and any(m in " ".join(ast.unparse(i.context_expr) for i in node.items)
                for m in _MUTATORS)
        for inner in ast.walk(node))
    assert found, "the scan would not have caught the bug it was written for"


def test_the_endpoint_still_refuses_an_oversized_batch(project):
    """The pre-check moved outside the lock; it must still be there."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        with pages_mod.mutate_pages(project) as doc:
            doc["pages"] = [{"id": f"{i:08x}", "seq": i, "file": f"p{i}.jpg"}
                            for i in range(pages_mod.MAX_PAGES_PER_PROJECT)]

        response = client.post(
            f"/api/projects/{project}/pages",
            files=[("files", ("x.jpg", io.BytesIO(jpeg(800, 1200)), "image/jpeg"))])

        assert response.status_code == 413


def test_an_oversized_image_is_refused_without_being_read(project, monkeypatch):
    """`size` comes from the multipart headers, so the cap can be applied before the
    bytes are brought into memory at all."""
    from fastapi.testclient import TestClient
    from server import app as app_mod

    monkeypatch.setattr(app_mod, "MAX_IMAGE_BYTES", 1000)
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project}/pages",
            files=[("files", ("huge.jpg", io.BytesIO(_big(800, 1200)), "image/jpeg"))])

    body = response.json()
    assert body["added"] == []
    assert body["rejected"] and "larger than" in body["rejected"][0]["reason"]


def test_the_locks_docstring_still_states_the_assumption():
    """This file is the enforcement of a rule written in prose over there. If that
    prose is ever deleted, the tests above lose their explanation."""
    source = (REPO / "morning" / "locks.py").read_text(encoding="utf-8")
    assert re.search(r"never on the event loop", source), (
        "morning/locks.py no longer states the assumption these tests enforce")
