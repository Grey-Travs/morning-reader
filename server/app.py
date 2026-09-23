"""FastAPI backend for Morning Reader — Japanese novels and manga.

Each work is a project (``projects/<id>/``) with its own source snapshot, state and
outputs. Shared defaults live in the global ``config.toml``. Long-running work streams
progress over Server-Sent Events.

Run from the project root::

    uvicorn server.app:app --port 8100

**There is no publishing surface here, by design.** Morning Reader translates and
reads; nothing in it writes to an external site. That is not merely unimplemented —
``tests/test_scope_guards.py`` fails the build if an outbound HTTP call site appears
anywhere in the tree. Night Reader's posting path was ~6,000 lines and produced nearly
every incident the project ever had; dropping it is the single largest reason this app
is expected to be quiet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from morning import docs_source, google_auth
from morning.chapter_files import (
    has_previous, output_total, read_audit_translation, read_chapter,
)
from morning.chapters import classify
from morning.config import Config
from morning.images import MAX_IMAGE_BYTES, inspect, unsupported_reason
from morning.page_build import assemble, assemble_spans, propose_join
from morning.pageread import TRANSLATED_KINDS, is_drawable, region_hash
from morning.reading_order import propose_order, propose_panels
from morning.glossary import (
    VALID_TYPES, Glossary, GlossaryEntry, glossary_lock, load_pending, save_pending,
)
from morning.pipeline import accept_chapter
from morning.state import State
from morning.textsource import decode_upload, split_text_into_chapters

from . import errors, jobs, pages as pages_mod, projects as pj, tasks as task_mod
from .locks import file_lock

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.toml"
DIST_DIR = ROOT / "web" / "dist"

# A pasted novel is large but not unbounded. 20 MB of UTF-8 Japanese is roughly ten
# million characters — far past any real book — and capping it here stops one request
# from ballooning the process.
MAX_SOURCE_BYTES = 20 * 1024 * 1024

app = FastAPI(title="Morning Reader")


# Every error leaves through one of these two handlers, so the frontend always
# receives `detail` as the SAME object shape (see server/errors.Explained) and the
# terminal gets one compact line instead of an uncaught-500 traceback dump.
@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    explained = errors.from_http_detail(exc.detail, exc.status_code)
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": errors.as_dict(explained)},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    explained = errors.explain(exc)
    errors.log_error(explained, where=request.url.path)
    return JSONResponse(status_code=explained.status,
                        content={"detail": errors.as_dict(explained)})


# ---- helpers -----------------------------------------------------------------

def load_global_config() -> Config:
    """The shared config, or defaults when there is none yet.

    Deliberately NOT an error when absent: step 1 makes no model call, so a first run
    must work before the user has configured anything. A malformed file is still a
    clean 400 rather than a 500, so the UI can prompt a fix instead of appearing
    broken.
    """
    if not CONFIG_PATH.exists():
        return Config()
    try:
        return Config.load(CONFIG_PATH)
    except Exception as exc:
        raise HTTPException(400, f"config.toml is invalid: {exc}") from exc


def require_project(pid: str) -> dict:
    project = pj.get_project(pid)
    if project is None:
        raise HTTPException(404, "That project does not exist.")
    return project


def project_cfg(pid: str) -> tuple[dict, Config]:
    project = require_project(pid)
    return project, pj.project_config(load_global_config(), project)


def _chapter_rows(pid: str, cfg: Config) -> list[dict]:
    """One row per chapter: what it is, and what has been done to it.

    Joins the source snapshot with state.json. The classification is computed live
    rather than read back from state, so a chapter that has never been prepared still
    shows what it is — otherwise a fresh project is a list of rows that say nothing.
    """
    chapters = pj.load_source(pid)
    state = State.load(cfg.paths.state_file)
    rows = []
    for chapter in chapters:
        metrics = chapter.metrics
        rec = state.get(chapter.index) or {}
        rows.append({
            "index": chapter.index,
            "title": chapter.title,
            "paragraph_count": metrics.paragraph_count,
            "dialogue_count": metrics.dialogue_count,
            "char_count": metrics.char_count,
            "source_fraction": metrics.source_fraction,
            "source_hash": metrics.content_hash,
            "class": classify(chapter, cfg.translation.min_source_fraction),
            "status": rec.get("status", ""),
            "error": rec.get("error"),
            "cost_usd": rec.get("cost_usd", 0.0),
            "updated_at": rec.get("updated_at"),
            # Whether the record on disk describes the text that is there NOW. A
            # chapter edited after being prepared is stale, and the UI has to be able
            # to say so — a status alone would claim work that no longer applies.
            "stale": bool(rec.get("source_hash")
                          and rec.get("source_hash") != metrics.content_hash),
        })
    return rows


# ---- health ------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    cfg = load_global_config()
    return {
        "ok": True,
        "app": "morning-reader",
        "configured": CONFIG_PATH.exists(),
        "model": cfg.anthropic.model,
        "effort": cfg.anthropic.effort,
        # Stated in the API because it is a scope decision, not an implementation
        # detail, and anything built against this server should be able to see it.
        "publishing": False,
        "kinds": list(pj.KINDS),
        "task_kinds": list(task_mod.TASK_KINDS),
        "ratio_band_calibrated": cfg.validation.ratio_band_calibrated,
    }


# ---- projects ----------------------------------------------------------------

def _project_totals(pid: str, cfg) -> dict:
    """Everything this project has cost, wherever it was recorded.

    A prose chapter's spend is in ``state.json``; a page read's and a manga
    translation's are in ``pages.json``, because that file is keyed by page seq and
    manga chapter and ``state.json`` is keyed by prose chapter index. Summing them here
    is what stops a scanned work — which spends ALL of its money on the manifest side —
    from reporting $0.00 everywhere the owner actually looks.
    """
    totals = dict(State.load(cfg.paths.state_file).totals() or {})
    manifest = pages_mod.load_pages(pid).get("totals") or {}
    for key, value in manifest.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            totals[key] = (totals.get(key) or 0) + value
    if "cost_usd" in totals:
        totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    return totals


@app.get("/api/projects")
def list_projects() -> dict:
    out = []
    for project in pj.list_projects():
        cfg = pj.project_config(load_global_config(), project)
        out.append({**project, "totals": _project_totals(project["id"], cfg)})
    return {"projects": out}


class CreateTextProject(BaseModel):
    title: str = ""
    kind: str = pj.KIND_NOVEL
    text: str = ""
    mode: str = "separator"       # separator | heading | single
    separator: str = "---"


@app.post("/api/projects/text")
def create_text_project(req: CreateTextProject) -> dict:
    """Create a project from pasted text. The step-1 ingestion path."""
    return _create_from_text(req.title, req.kind, req.text, req.mode, req.separator)


@app.post("/api/projects/upload")
async def create_upload_project(file: UploadFile, title: str = "",
                                kind: str = pj.KIND_NOVEL, mode: str = "heading",
                                separator: str = "---") -> dict:
    """Create a project from an uploaded ``.txt``.

    The same path as pasting, with a decode in front — Japanese plain text still
    arrives as Shift_JIS or EUC-JP often enough that assuming UTF-8 would turn a
    perfectly good file into mojibake. See ``morning.textsource.decode_upload``.
    """
    data = await file.read()
    if len(data) > MAX_SOURCE_BYTES:
        raise HTTPException(413, "That file is larger than Morning Reader accepts "
                                 "(20 MB). Split it and upload the parts.")
    text = decode_upload(data)
    name = title or Path(file.filename or "Untitled").stem
    return _create_from_text(name, kind, text, mode, separator)


def _create_from_text(title: str, kind: str, text: str, mode: str,
                      separator: str) -> dict:
    if len((text or "").encode("utf-8")) > MAX_SOURCE_BYTES:
        raise HTTPException(413, "That text is larger than Morning Reader accepts "
                                 "(20 MB). Split it and add the parts separately.")
    chapters = split_text_into_chapters(text, mode=mode, separator=separator)
    if not chapters:
        raise HTTPException(400, "There was no text to read in that — it came through "
                                 "empty.")
    try:
        project = pj.create_project(title, kind=kind, ingest=pj.INGEST_TEXT)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Write the source BEFORE announcing the project. A project record that exists
    # while its source does not is the "orphaned creating intent" shape: the UI opens
    # an empty work, the user re-runs the import, and the second run fills the first
    # project with the second import's text. Creating the folder first is unavoidable
    # (the source lives inside it), so the ordering that matters is source-then-count.
    pj.save_source(project["id"], chapters)
    project = pj.set_chapter_count(project["id"], len(chapters)) or project
    return {"project": project, "chapters": len(chapters)}


class CreateScanProject(BaseModel):
    title: str = ""
    kind: str = pj.KIND_NOVEL


@app.post("/api/projects/scan")
def create_scan_project(req: CreateScanProject) -> dict:
    """Create a project whose source is photographs or scans.

    Unlike every other ingestion path this one creates a project with NO chapters:
    the text does not exist yet, and will not until the pages have been read and
    built. That is why `ingest` is stored rather than inferred — an empty project
    whose source is a stack of photographs looks exactly like a failed text import,
    and the two need entirely different things offered to the user.
    """
    try:
        project = pj.create_project(req.title, kind=req.kind, ingest=pj.INGEST_IMAGES)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"project": project, "chapters": 0}


def _manga_chapter_rows(pid: str) -> list[dict]:
    """A manga's chapters, in the shape the work page already renders.

    ``class`` and ``paragraph_count`` are deliberately ABSENT. They are what the prose
    UI keys on, and a row without them cannot be offered the prose Translate — the
    refusal is structural rather than a check somebody has to remember.
    """
    doc = pages_mod.load_pages(pid)
    rows = []
    for chapter in doc.get("chapters") or []:
        counts = pages_mod.chapter_counts(doc, chapter)
        rows.append({
            "index": int(chapter.get("index") or 0),
            "title": chapter.get("title", ""),
            "status": chapter.get("status", ""),
            "pages": counts["pages"],
            "lines": counts["lines"],
            "translated": counts["translated"],
            "stale": counts["stale"],
            "unchecked_pages": counts["unchecked_pages"],
            "silent_pages": counts["silent_pages"],
            "error": chapter.get("error"),
            "cost_usd": chapter.get("cost_usd", 0.0),
        })
    return rows


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    project, cfg = project_cfg(pid)
    rows = (_manga_chapter_rows(pid) if project.get("kind") == pj.KIND_MANGA
            else _chapter_rows(pid, cfg))
    return {"project": project, "chapters": rows,
            "totals": _project_totals(pid, cfg)}


class UpdateProject(BaseModel):
    title: str | None = None
    style_note: str | None = None
    instructions: str | None = None
    honorific_note: str | None = None
    status: str | None = None
    archived: bool | None = None


@app.patch("/api/projects/{pid}")
def update_project(pid: str, req: UpdateProject) -> dict:
    require_project(pid)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    return {"project": pj.update_project(pid, **fields)}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    require_project(pid)
    if jobs.active_job(pid) is not None:
        raise HTTPException(409, "Something is still running on this project. Stop it "
                                 "first, then delete.")
    return {"deleted": pj.delete_project(pid)}


# ---- source ------------------------------------------------------------------

@app.get("/api/projects/{pid}/chapters/{index}")
def get_chapter(pid: str, index: int) -> dict:
    project, cfg = project_cfg(pid)
    chapter = next((c for c in pj.load_source(pid) if c.index == index), None)
    if chapter is None:
        raise HTTPException(404, "There is no chapter with that number in this project.")
    rec = State.load(cfg.paths.state_file).get(index) or {}
    metrics = chapter.metrics
    return {
        "index": chapter.index,
        "title": chapter.title,
        # The field is `source`, never a language name. Every consumer of this API
        # reads it under this name, which is what makes the rename impossible to
        # forget later.
        "source": chapter.paragraphs,
        "source_hash": metrics.content_hash,
        "source_fraction": metrics.source_fraction,
        "char_count": metrics.char_count,
        "class": classify(chapter, cfg.translation.min_source_fraction),
        "state": rec,
    }


class ReplaceSource(BaseModel):
    text: str
    mode: str = "separator"
    separator: str = "---"


@app.put("/api/projects/{pid}/source")
def replace_source(pid: str, req: ReplaceSource) -> dict:
    """Re-paste a project's whole source.

    Refused while work is in flight. The worker re-reads the source per item precisely
    so an edit lands, but replacing every chapter underneath a running sweep would
    have it prepare chapter 4 of the old text and chapter 5 of the new one, and the
    stored hashes would describe a document that never existed.
    """
    require_project(pid)
    if jobs.active_job(pid) is not None:
        raise HTTPException(409, "Something is still running on this project. Stop it "
                                 "first, then replace the source.")
    chapters = split_text_into_chapters(req.text, mode=req.mode,
                                        separator=req.separator)
    if not chapters:
        raise HTTPException(400, "There was no text to read in that — it came through "
                                 "empty.")
    pj.save_source(pid, chapters)
    pj.set_chapter_count(pid, len(chapters))
    return {"chapters": len(chapters)}


# ---- Google Docs ingestion ---------------------------------------------------
# One of the four source paths. Reading only: the token's scopes are read-only, so
# writing to a document would be refused at Google's end even if this code tried, and
# tests/test_scope_guards.py refuses the method names outright.

@app.get("/api/google/status")
def google_status() -> dict:
    cfg = load_global_config()
    return {
        "connected": google_auth.is_connected(cfg.google.token_file),
        # Distinguished because the fixes are different: no client file means "set one
        # up in the Cloud console", no token means "click connect".
        "credentials_present": Path(cfg.google.credentials_file).exists(),
        "scopes": list(google_auth.SCOPES),
    }


@app.post("/api/google/connect")
async def google_connect() -> dict:
    """Run the consent flow. Opens a browser and blocks until it is answered.

    Only ever reached from an explicit click — never from a page load or a
    translation, which would pop a browser window at someone mid-run.
    """
    cfg = load_global_config()
    try:
        await run_in_threadpool(google_auth.sign_in, cfg.google.credentials_file,
                                cfg.google.token_file)
    except google_auth.GoogleAuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"connected": True}


@app.post("/api/google/disconnect")
def google_disconnect() -> dict:
    cfg = load_global_config()
    return {"disconnected": google_auth.forget(cfg.google.token_file)}


def _google_credentials(cfg: Config):
    creds = google_auth.saved_credentials(cfg.google.token_file)
    if creds is None:
        raise HTTPException(401, "Morning Reader is not connected to Google yet. "
                                 "Connect it, then try again.")
    return creds


def _fetch_doc_chapters(cfg: Config, doc_id: str) -> list:
    try:
        return docs_source.load_chapters(
            _google_credentials(cfg), doc_id,
            flatten_children=cfg.google.flatten_child_tabs)
    except docs_source.DocumentError as exc:
        raise HTTPException(400, str(exc)) from exc


class CreateDocsProject(BaseModel):
    # A pasted address bar is fine; the id is pulled out of it.
    document: str
    title: str = ""
    kind: str = pj.KIND_NOVEL


@app.post("/api/projects/docs")
async def create_docs_project(req: CreateDocsProject) -> dict:
    """Create a project from a Google Doc — one chapter per tab."""
    cfg = load_global_config()
    doc_id = docs_source.extract_doc_id(req.document)
    if not doc_id:
        raise HTTPException(400, "That does not look like a Google Doc link or id. "
                                 "Paste the document's address, or its id.")
    if req.kind not in pj.KINDS:
        raise HTTPException(400, f"Unknown kind {req.kind!r}.")

    chapters = await run_in_threadpool(_fetch_doc_chapters, cfg, doc_id)
    if not chapters:
        raise HTTPException(400, "That document has no chapters to read.")

    project = pj.create_project(req.title or f"Document {doc_id[:8]}",
                                kind=req.kind, ingest=pj.INGEST_DOCS)
    # Source first, then the count — see _create_from_text for why the ordering
    # matters. The document id is recorded so the source can be re-fetched later.
    pj.save_source(project["id"], chapters)
    pj.set_source_document(project["id"], doc_id)
    project = pj.set_chapter_count(project["id"], len(chapters)) or project
    return {"project": project, "chapters": len(chapters)}


@app.post("/api/projects/{pid}/source/refresh")
async def refresh_source(pid: str) -> dict:
    """Re-read the document this project came from.

    The point of this existing at all: a Doc gets edited. Without a refresh, fixing
    the Japanese and re-translating produces the SAME English, because the worker
    reads the stored snapshot and the content hash never moves.
    """
    project, cfg = project_cfg(pid)
    doc_id = project.get("source_document")
    if not doc_id:
        raise HTTPException(400, "This project did not come from a Google Doc, so "
                                 "there is nothing to refresh.")
    if jobs.active_job(pid) is not None:
        raise HTTPException(409, "Something is still running on this project. Stop it "
                                 "first, then refresh.")

    chapters = await run_in_threadpool(_fetch_doc_chapters, cfg, doc_id)
    if not chapters:
        raise HTTPException(400, "That document has no chapters to read.")
    pj.save_source(pid, chapters)
    pj.set_chapter_count(pid, len(chapters))
    return {"chapters": len(chapters)}


# ---- scanned pages -----------------------------------------------------------

@app.get("/api/projects/{pid}/pages")
def list_pages(pid: str) -> dict:
    project = require_project(pid)
    doc = pages_mod.load_pages(pid)
    rows = []
    for page in doc.get("pages", []):
        read = page.get("read") or {}
        rows.append({
            "id": page.get("id"), "seq": page.get("seq"), "name": page.get("name"),
            "status": page.get("status"), "width": page.get("width"),
            "height": page.get("height"), "bytes": page.get("bytes"),
            "batch": page.get("batch"), "error": page.get("error"),
            "hint": page.get("hint", ""),
            "join_prev": page.get("join_prev", ""),
            "join_glue": page.get("join_glue", "none"),
            "join_prev_source": page.get("join_prev_source", ""),
            "join_reason": page.get("join_reason", ""),
            "regions": len(read.get("regions") or []),
            "confidence": (page.get("ocr") or {}).get("confidence", ""),
            "chars": pages_mod.page_chars(page),
            # The manga side. Line TEXT is deliberately never included: this route is
            # polled every time a job item finishes, and shipping the English would
            # send megabytes per poll on a two-hundred-page project.
            "order_source": (page.get("order_check") or {}).get("order_source", "model"),
            "looks_reversed": bool(
                (page.get("order_check") or {}).get("looks_reversed")),
            **{k: v for k, v in pages_mod.line_counts(page).items()
               if k in ("lines", "translated", "stale")},
        })
    # The project's kind travels with the payload so the pages screen can branch. It
    # could not before, which is how a manga kept a primary Build button that wrote a
    # prose source.json.
    return {"pages": rows, "summary": pages_mod.summary(doc),
            "kind": project.get("kind", pj.KIND_NOVEL)}


@app.get("/api/projects/{pid}/pages/{page_id}")
def get_page(pid: str, page_id: str) -> dict:
    """One page in full, including its regions — what the overlay reader draws."""
    require_project(pid)
    page = pages_mod.find_page(pages_mod.load_pages(pid), page_id)
    if page is None:
        raise HTTPException(404, "There is no page with that id in this project.")
    return {"page": page, "text": pages_mod.page_text(page)}


@app.get("/api/projects/{pid}/pages/{page_id}/image")
def get_page_image(pid: str, page_id: str):
    """The image itself. Resolved through the MANIFEST — a URL segment never becomes
    a path component."""
    require_project(pid)
    path = pages_mod.resolve_page_file(pid, page_id)
    if path is None:
        raise HTTPException(404, "That page's image is not on disk.")
    return FileResponse(str(path))


@app.post("/api/projects/{pid}/pages")
async def upload_pages(pid: str, files: list[UploadFile],
                       label: str = "") -> dict:
    """Add page images.

    Every file is identified and measured from its own BYTES — the content type is
    advisory and the filename is a guess. A file already in this project is reported
    as a duplicate rather than stored twice: re-uploading a folder is ordinary, and
    paying to read the same page again is not.
    """
    require_project(pid)
    added, duplicates, rejected = [], [], []

    with pages_mod.mutate_pages(pid) as doc:
        if len(doc.get("pages", [])) + len(files) > pages_mod.MAX_PAGES_PER_PROJECT:
            raise HTTPException(413, f"That would take this project past "
                                     f"{pages_mod.MAX_PAGES_PER_PROJECT} pages.")
        batch = pages_mod.new_batch(doc, label)
        folder = pages_mod.pages_dir(pid)
        folder.mkdir(parents=True, exist_ok=True)

        for upload in files:
            data = await upload.read()
            name = upload.filename or ""
            if len(data) > MAX_IMAGE_BYTES:
                rejected.append({"name": name, "reason": "That image is larger than "
                                                         "Morning Reader accepts."})
                continue
            info = inspect(data)
            if not info.ok:
                rejected.append({"name": name,
                                 "reason": unsupported_reason(data[:64])})
                continue
            digest = pages_mod.sha256_of(data)
            existing = pages_mod.find_by_hash(doc, digest)
            if existing is not None:
                duplicates.append({"name": name, "seq": existing.get("seq")})
                continue
            page = pages_mod.add_page(doc, info=info, data_len=len(data),
                                      digest=digest, batch=batch, name=name)
            (folder / page["file"]).write_bytes(data)
            added.append({"id": page["id"], "seq": page["seq"],
                          "measured": info.measured})

    return {"added": added, "duplicates": duplicates, "rejected": rejected}


class PageSelection(BaseModel):
    ids: list[str] | None = None   # None = every page that still needs it
    force: bool = False


@app.post("/api/projects/{pid}/pages/read")
async def read_pages(pid: str, req: PageSelection = PageSelection()) -> dict:
    """Queue pages for reading. Must be async: starting a worker schedules a task."""
    _, cfg = project_cfg(pid)
    doc = pages_mod.load_pages(pid)
    wanted = set(req.ids or [])

    seqs: list[int] = []
    for page in doc.get("pages", []):
        if page.get("status") == pages_mod.STATUS_SKIPPED:
            continue
        if wanted:
            if str(page.get("id")) in wanted:
                seqs.append(int(page.get("seq")))
            continue
        # A sweep takes only pages that have never been read. Re-reading one that has
        # been is what `force` is for, and doing it by default would re-bill the lot.
        if req.force or not page.get("read"):
            seqs.append(int(page.get("seq")))

    if not seqs:
        return {"queued": [], "job_id": None}

    with pages_mod.mutate_pages(pid) as fresh:
        for seq in seqs:
            page = pages_mod.find_by_seq(fresh, seq)
            if page is not None:
                pages_mod.set_status(fresh, page["id"], pages_mod.STATUS_QUEUED)

    items = [(seq, req.force, task_mod.TASK_READ_PAGE) for seq in seqs]
    return jobs.enqueue(pid, cfg, items)


class ReorderRequest(BaseModel):
    ids: list[str]


@app.post("/api/projects/{pid}/pages/reorder")
def reorder_pages(pid: str, req: ReorderRequest) -> dict:
    require_project(pid)
    with pages_mod.mutate_pages(pid) as doc:
        if not pages_mod.reorder(doc, req.ids):
            raise HTTPException(400, "That ordering does not list every page exactly "
                                     "once, so it was not applied.")
    return {"ok": True}


@app.post("/api/projects/{pid}/pages/delete")
def delete_pages(pid: str, req: ReorderRequest) -> dict:
    require_project(pid)
    if jobs.active_job(pid) is not None:
        raise HTTPException(409, "Something is still running on this project. Stop it "
                                 "first, then delete pages.")
    with pages_mod.mutate_pages(pid) as doc:
        removed = pages_mod.delete_pages(doc, req.ids)
    for page in removed:
        try:
            (pages_mod.pages_dir(pid) / str(page.get("file"))).unlink()
        except OSError:
            pass  # the manifest is the record; a missing file is not a failure
    return {"removed": len(removed)}


class JoinRequest(BaseModel):
    kind: str
    glue: str = "none"


@app.post("/api/projects/{pid}/pages/{page_id}/join")
def set_page_join(pid: str, page_id: str, req: JoinRequest) -> dict:
    """Record how this page follows the previous one, as a HUMAN decision.

    Marked as theirs, and never revisited by a later read — see
    ``server.pages.set_join``.
    """
    require_project(pid)
    with pages_mod.mutate_pages(pid) as doc:
        page = pages_mod.find_page(doc, page_id)
        if page is None:
            raise HTTPException(404, "There is no page with that id in this project.")
        if not pages_mod.set_join(page, req.kind, glue=req.glue,
                                  reason="you decided", by_user=True):
            raise HTTPException(400, f"{req.kind!r} is not a way pages can join.")
    return {"ok": True}


class PageStatusRequest(BaseModel):
    status: str


@app.post("/api/projects/{pid}/pages/{page_id}/status")
def set_page_status(pid: str, page_id: str, req: PageStatusRequest) -> dict:
    """Mark a page as checked, or as not part of the text at all."""
    require_project(pid)
    if req.status not in (pages_mod.STATUS_EDITED, pages_mod.STATUS_OK,
                          pages_mod.STATUS_SKIPPED, pages_mod.STATUS_NEW):
        raise HTTPException(400, f"A page cannot be set to {req.status!r}.")
    with pages_mod.mutate_pages(pid) as doc:
        if pages_mod.set_status(doc, page_id, req.status) is None:
            raise HTTPException(404, "There is no page with that id in this project.")
    return {"status": req.status}


@app.post("/api/projects/{pid}/pages/propose-joins")
def propose_joins(pid: str) -> dict:
    """Work out how each page follows the one before it.

    A proposal, and labelled as one. A page whose seam a human has already decided is
    left alone — ``set_join`` refuses to overwrite it.
    """
    require_project(pid)
    proposed = 0
    with pages_mod.mutate_pages(pid) as doc:
        usable = [p for p in doc.get("pages", [])
                  if p.get("status") in pages_mod.APPROVED_STATUSES]
        for previous, following in zip(usable, usable[1:]):
            join = propose_join(pages_mod.page_text(previous),
                                pages_mod.page_text(following), previous, following)
            if pages_mod.set_join(following, join.kind, glue=join.glue,
                                  reason=join.reason):
                proposed += 1
    return {"proposed": proposed}


def _build_manga_chapters(pid: str, doc: dict) -> dict:
    """Group a manga's pages into chapters, in pages.json and nowhere else.

    A manga's chapters must NOT go through ``pj.save_source``. The prose path would
    then be reachable: "Translate everything" would run ``process_chapter`` over
    concatenated bubble text — paid, wrong, with no per-bubble mapping for the overlay
    to draw — and ``prepare`` would write a record per chapter into ``state.json``,
    which is keyed in the same integer space a page seq already occupies.

    Re-building keeps a chapter's translation status when its pages have not changed,
    so adding a seam near the end of a volume does not offer to re-bill the chapters
    before it.
    """
    built = assemble_spans(doc.get("pages") or [])
    if not built.spans:
        raise HTTPException(400, "There are no pages to build chapters from yet.")

    previous = {tuple(c.get("page_ids") or []): c for c in (doc.get("chapters") or [])}
    chapters = []
    for span in built.spans:
        record = span.to_dict()
        kept = previous.get(tuple(record["page_ids"]))
        if kept is not None:
            # Same pages, same English: carry the translation forward rather than
            # presenting a paid-for chapter as untranslated.
            for key in ("status", "at", "attempts", "lines", "missing", "order_hash",
                        "chunks", "warnings", "usage", "cost_usd", "last_status"):
                if key in kept:
                    record[key] = kept[key]
        else:
            record["status"] = pages_mod.CHAPTER_NEW
        chapters.append(record)

    with pages_mod.mutate_pages(pid) as fresh:
        fresh["chapters"] = chapters
        fresh["build"] = {"at": pages_mod.now_iso(), "chapters": len(chapters),
                          "pages": built.pages_used, "warnings": built.warnings}
    return {"chapters": len(chapters), "pages_used": built.pages_used,
            "warnings": built.warnings}


@app.post("/api/projects/{pid}/pages/build")
def build_from_pages(pid: str) -> dict:
    """Turn the read pages into this project's chapters.

    After this the project is an ordinary one — nothing downstream can tell it was
    ever a stack of photographs.
    """
    project, _cfg = project_cfg(pid)
    if jobs.active_job(pid) is not None:
        raise HTTPException(409, "Something is still running on this project. Stop it "
                                 "first, then build.")
    doc = pages_mod.load_pages(pid)

    if project.get("kind") == pj.KIND_MANGA:
        return _build_manga_chapters(pid, doc)

    usable = [p for p in doc.get("pages", [])
              if p.get("status") in pages_mod.APPROVED_STATUSES]
    if not usable:
        raise HTTPException(400, "No pages are ready yet. Read them first, and check "
                                 "any the reader was unsure about.")

    built = assemble(usable, text_of=pages_mod.page_text)
    if not built.chapters:
        raise HTTPException(400, "Those pages produced no text to build from.")

    pj.save_source(pid, built.chapters)
    pj.set_chapter_count(pid, len(built.chapters))
    with pages_mod.mutate_pages(pid) as fresh:
        fresh["build"] = {"at": pages_mod.now_iso(), "chapters": len(built.chapters),
                          "pages": built.pages_used, "warnings": built.warnings}
    return {"chapters": len(built.chapters), "pages_used": built.pages_used,
            "warnings": built.warnings}


# ---- manga -------------------------------------------------------------------
#
# A manga project never writes source.json. Its chapters are runs of pages recorded in
# pages.json, its English is per-region, and its reader is the overlay — so every route
# below is a fork rather than a generalisation of the prose ones. The refusals matter
# as much as the routes: a manga reaching the prose translate path would spend the
# owner's allowance producing a wall of text with no per-bubble mapping, which the
# overlay cannot draw and which nothing can recover.

def _require_manga(pid: str) -> tuple[dict, object]:
    project, cfg = project_cfg(pid)
    if project.get("kind") != pj.KIND_MANGA:
        raise HTTPException(404, "This work is a novel. Its chapters are read in the "
                                 "ordinary reader.")
    return project, cfg


class MangaTranslateRequest(BaseModel):
    indices: list[int] | None = None   # None = every chapter that still needs it
    force: bool = False


@app.post("/api/projects/{pid}/manga/translate")
async def translate_manga(pid: str,
                          req: MangaTranslateRequest = MangaTranslateRequest()) -> dict:
    """Queue manga chapters for translation. Must be async: it starts a worker."""
    _project, cfg = _require_manga(pid)
    doc = pages_mod.load_pages(pid)
    chapters = doc.get("chapters") or []
    if not chapters:
        raise HTTPException(400, "This manga has no chapters yet. Build them from the "
                                 "pages first.")

    wanted = {int(i) for i in (req.indices or [])}
    picked: list[int] = []
    for chapter in chapters:
        index = int(chapter.get("index") or 0)
        if wanted:
            if index in wanted:
                picked.append(index)
            continue
        # A sweep takes only chapters that are not already done. Re-translating one
        # that is, is what `force` is for, and doing it by default would re-bill the
        # whole volume.
        if req.force or chapter.get("status") != pages_mod.STATUS_OK:
            picked.append(index)

    if not picked:
        return {"queued": [], "job_id": None}

    with pages_mod.mutate_pages(pid) as fresh:
        for index in picked:
            pages_mod.set_chapter_status(fresh, index, pages_mod.CHAPTER_QUEUED)

    items = [(i, req.force, task_mod.TASK_TRANSLATE_SCRIPT) for i in picked]
    return jobs.enqueue(pid, cfg, items)


class OrderRequest(BaseModel):
    # None clears a saved order and returns the page to the model's. That is the only
    # way back, and it is deliberately explicit: a human's order is never reverted
    # silently, but it can be revoked on purpose.
    ids: list[str] | None = None


@app.post("/api/projects/{pid}/pages/{page_id}/order")
def set_page_order(pid: str, page_id: str,
                   req: OrderRequest = OrderRequest()) -> dict:
    """Record a human's reading order for one page.

    The WHOLE permutation is sent, never a delta — the same rule the page reorder
    already follows, one level down. The server refuses anything that is not a
    permutation, so a delta could not corrupt the book, but it would look like a
    broken button.
    """
    require_project(pid)
    with pages_mod.mutate_pages(pid) as doc:
        page = pages_mod.find_page(doc, page_id)
        if page is None:
            raise HTTPException(404, "There is no page with that id in this project.")
        if not pages_mod.set_order(page, req.ids):
            raise HTTPException(400, "That ordering does not list every region on the "
                                     "page exactly once, so it was not applied.")
        check = dict(page.get("order_check") or {})
    return {"ok": True, "order_check": check}


class LineEdit(BaseModel):
    english: str | None = None
    speaker: str | None = None


@app.post("/api/projects/{pid}/pages/{page_id}/lines/{region_id}")
def edit_line(pid: str, page_id: str, region_id: str,
              req: LineEdit = LineEdit()) -> dict:
    """Edit one translated line as a human.

    Whatever is set here is marked ``user`` and a later re-translate never overwrites
    it — the same rule as ``join_prev_source`` and ``order_source``. Editing the
    English also re-stamps the line's ``source_hash`` against the words that are on the
    page NOW, because the person is looking at those words: without that their edit
    would be born stale and marked as needing attention it does not need.
    """
    require_project(pid)
    with pages_mod.mutate_pages(pid) as doc:
        page = pages_mod.find_page(doc, page_id)
        if page is None:
            raise HTTPException(404, "There is no page with that id in this project.")
        read, _note = pages_mod.effective_read(page)
        region = next((r for r in read.regions if r.id == region_id), None)
        if region is None:
            raise HTTPException(404, "There is no region with that id on this page.")

        fields: dict = {}
        if req.english is not None:
            fields["english"] = req.english
            fields["english_source"] = "user"
            fields["source_hash"] = region_hash(region)
        if req.speaker is not None:
            fields["speaker"] = req.speaker
            fields["speaker_source"] = "user"
        if not fields:
            raise HTTPException(400, "There was nothing to change in that request.")
        line = dict(pages_mod.set_line(page, region_id, **fields))
    return {"line": line}


def _manga_page_payload(page: dict) -> dict:
    """One page as the overlay and the script view need it."""
    read, note = pages_mod.effective_read(page)
    lines = page.get("lines")
    lines = lines if isinstance(lines, dict) else {}
    panels = propose_panels(read.regions)
    panel_of = {rid: i + 1 for i, panel in enumerate(panels) for rid in panel}

    regions = []
    for region in read.in_order():
        record = lines.get(region.id)
        record = record if isinstance(record, dict) else {}
        english = str(record.get("english") or "")
        regions.append({
            "id": region.id,
            "box": [round(float(v), 6) for v in region.box],
            "kind": region.kind,
            "order": region.order,
            "text": region.text,
            "panel": panel_of.get(region.id, 0),
            "english": english,
            "english_source": record.get("english_source", ""),
            "speaker": record.get("speaker", ""),
            "speaker_source": record.get("speaker_source", ""),
            "translatable": region.kind in TRANSLATED_KINDS,
            # Both computed server-side so the reader's "N lines could not be placed"
            # banner and what it actually draws cannot disagree. The browser mirrors
            # `is_drawable`, and tests/test_geometry_parity.py pins the pair.
            "drawable": is_drawable(region.box),
            "stale": bool(english.strip())
            and record.get("source_hash") != region_hash(region),
        })

    return {
        "id": page.get("id"),
        "seq": page.get("seq"),
        "name": page.get("name", ""),
        "status": page.get("status", ""),
        "width": page.get("width", 0),
        "height": page.get("height", 0),
        "regions": regions,
        "panels": panels,
        "order_source": read.order_source,
        "order_note": note,
        "order_check": pages_mod.order_check(page),
        # The geometry's own reading of the page, shipped so the reader can offer a
        # one-click fix and preview it before applying. Free — the boxes are already
        # stored and laying them out is arithmetic — and it is the only thing in the
        # app that can catch a page read left-to-right, which is backwards sentence by
        # sentence while every sentence stays fluent English.
        "order_proposal": propose_order(read.regions),
        "counts": pages_mod.line_counts(page),
    }


@app.get("/api/projects/{pid}/manga/{index}")
def read_manga_chapter(pid: str, index: int) -> dict:
    """One manga chapter as the reader and the script view need it."""
    _project, _cfg = _require_manga(pid)
    doc = pages_mod.load_pages(pid)
    chapter = pages_mod.find_chapter(doc, index)
    if chapter is None:
        raise HTTPException(404, "There is no chapter with that number in this manga.")

    by_id = {str(p.get("id")): p for p in doc.get("pages", [])}
    wanted = [str(i) for i in (chapter.get("page_ids") or [])]
    pages_out = [_manga_page_payload(by_id[i]) for i in wanted if i in by_id]

    indices = [int(c.get("index") or 0) for c in (doc.get("chapters") or [])]
    position = indices.index(index) if index in indices else -1

    counts = pages_mod.chapter_counts(doc, chapter)
    warnings = list(chapter.get("warnings") or [])
    # A reordering does not stale a line — no bubble's Japanese changed — but a line
    # translated believing it followed line A now follows line B, and Japanese subject
    # omission means that CAN change the English. Worth saying; not worth re-billing.
    current = hashlib.sha256("|".join(
        f"{p['seq']}:{r['id']}" for p in pages_out for r in p["regions"]
        if r["translatable"]).encode()).hexdigest()
    order_changed = bool(chapter.get("order_hash")) and \
        chapter["order_hash"] != current

    return {
        "index": index,
        "title": chapter.get("title", ""),
        "status": chapter.get("status", ""),
        "pages": pages_out,
        "counts": counts,
        "warnings": warnings,
        "order_changed": order_changed,
        "missing": chapter.get("missing", []),
        "cost_usd": chapter.get("cost_usd", 0.0),
        "prev": indices[position - 1] if position > 0 else None,
        "next": (indices[position + 1]
                 if 0 <= position and position + 1 < len(indices) else None),
    }


# ---- reading -----------------------------------------------------------------

def _require_chapter(pid: str, index: int) -> tuple[list, object]:
    # A manga has no prose chapters at all, so this would 404 anyway — but with
    # "there is no chapter with that number", which reads as data loss rather than as
    # the reader pointing somewhere else.
    project = pj.get_project(pid)
    if project is not None and project.get("kind") == pj.KIND_MANGA:
        raise HTTPException(404, "This work is a manga. Its chapters are read with "
                                 "the English over the art, not as prose.")
    chapters = pj.load_source(pid)
    chapter = next((c for c in chapters if c.index == index), None)
    if chapter is None:
        raise HTTPException(404, "There is no chapter with that number in this project.")
    return chapters, chapter


@app.get("/api/projects/{pid}/read/{index}")
def read_chapter_view(pid: str, index: int) -> dict:
    """One chapter as it should be read: the English, with its source beside it.

    ``from_audit`` is the field that matters. A chapter that failed its checks has its
    translation only in the audit copy, and showing that prose without saying so would
    present un-reviewed work as finished — which is exactly the confusion the
    chapters/-versus-audit/ split exists to prevent.
    """
    _, cfg = project_cfg(pid)
    chapters, chapter = _require_chapter(pid, index)
    total = output_total(Path(cfg.paths.output_dir), len(chapters))
    record = State.load(cfg.paths.state_file).get(index) or {}

    english = read_chapter(Path(cfg.paths.output_dir), index, total)
    from_audit = False
    if english is None:
        english = read_audit_translation(Path(cfg.paths.audit_dir), index, total)
        from_audit = english is not None

    indices = [c.index for c in chapters]
    position = indices.index(index)
    return {
        "index": index,
        "title": chapter.title,
        "english": english,
        "source": chapter.paragraphs,
        "status": record.get("status", ""),
        "from_audit": from_audit,
        "accepted": bool(record.get("accepted")),
        "failures": record.get("failures", []),
        "warnings": record.get("warnings", []),
        "leak_findings": record.get("leak_findings", []),
        "validation": record.get("validation", {}),
        "has_previous_version": has_previous(Path(cfg.paths.output_dir), index, total),
        "prev": indices[position - 1] if position > 0 else None,
        "next": indices[position + 1] if position + 1 < len(indices) else None,
    }


class AcceptRequest(BaseModel):
    # Lets the reviewer fix a line before promoting it. Omitted means "promote what is
    # in the audit copy unchanged".
    english: str | None = None


@app.post("/api/projects/{pid}/chapters/{index}/accept")
def accept(pid: str, index: int, req: AcceptRequest = AcceptRequest()) -> dict:
    """Promote a reviewed chapter into ``chapters/`` so it can be read.

    The human half of the review gate: the chapter failed a check, someone looked, and
    they are saying it is fine anyway. Why it was flagged stays on the record.
    """
    _, cfg = project_cfg(pid)
    chapters, chapter = _require_chapter(pid, index)
    if jobs.active_job(pid) is not None:
        raise HTTPException(409, "Something is still running on this project. Stop it "
                                 "first, then accept.")
    total = output_total(Path(cfg.paths.output_dir), len(chapters))
    try:
        with jobs.mutate_state(cfg.paths.state_file) as state:
            status = accept_chapter(chapter, total, cfg, state, english=req.english)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"index": index, "status": status}


# ---- the glossary ------------------------------------------------------------

@app.get("/api/projects/{pid}/glossary")
def get_glossary(pid: str) -> dict:
    """Locked terms, and the ones waiting on a human.

    Both in one response because the review screen shows them together: deciding
    whether a proposed term is new usually means looking at what is already there.
    """
    _, cfg = project_cfg(pid)
    glossary = Glossary.load(cfg.paths.glossary_json)
    return {
        "entries": [asdict(e) for e in glossary.entries()],
        "pending": load_pending(cfg.paths.glossary_pending),
    }


@app.post("/api/projects/{pid}/glossary/approve")
def approve_term(pid: str, body: dict) -> dict:
    """Move a proposed term into the glossary, with whatever edits the reviewer made.

    The body is the WHOLE entry rather than a reference to the queued one, because
    correcting a wrong reading is the main reason this gate exists — a name's kanji
    usually have several possible readings, and the model picked one.

    A plain dict rather than a Pydantic model on purpose. ``GlossaryEntry.from_dict``
    already coerces and validates every field, and it is what the queue and the engine
    both go through — a second schema here would be a second place for the two to
    drift, and one of its field names (``register``) shadows a BaseModel attribute.
    """
    _, cfg = project_cfg(pid)
    if not isinstance(body, dict):
        raise HTTPException(400, "A glossary entry must be an object.")
    entry = GlossaryEntry.from_dict(body)
    if not (entry.source or entry.english):
        raise HTTPException(400, "A glossary entry needs a source term or an English "
                                 "spelling.")
    if entry.type not in VALID_TYPES:
        raise HTTPException(400, f"Unknown term type {entry.type!r}.")
    # Held across read → modify → write so a second approval cannot interleave and
    # drop the first. The lock is the shared registry's, so a save from the worker
    # cannot interleave either.
    with glossary_lock(cfg.paths.glossary_json):
        glossary = Glossary.load(cfg.paths.glossary_json)
        glossary.add(entry)
        glossary.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    _drop_pending(cfg, str(body.get("source") or entry.source))
    return {"approved": entry.source or entry.english,
            "entries": len(Glossary.load(cfg.paths.glossary_json))}


class TermRef(BaseModel):
    source: str


@app.post("/api/projects/{pid}/glossary/reject")
def reject_term(pid: str, req: TermRef) -> dict:
    """Drop a proposed term without locking it.

    It can be proposed again by a later chapter, which is correct: rejecting means
    "not this spelling", not "never mention this again".
    """
    _, cfg = project_cfg(pid)
    removed = _drop_pending(cfg, req.source)
    return {"rejected": removed}


@app.post("/api/projects/{pid}/glossary/remove")
def remove_term(pid: str, req: TermRef) -> dict:
    """Unlock a term that is already in the glossary."""
    _, cfg = project_cfg(pid)
    with glossary_lock(cfg.paths.glossary_json):
        glossary = Glossary.load(cfg.paths.glossary_json)
        removed = glossary.remove(req.source)
        if removed:
            glossary.save(cfg.paths.glossary_json, cfg.paths.glossary_md)
    return {"removed": removed}


def _drop_pending(cfg: Config, source: str) -> bool:
    """Take one term off the pending queue. Returns whether it was there."""
    path = cfg.paths.glossary_pending
    with file_lock(path):
        queue = load_pending(path)
        remaining = [item for item in queue
                     if str(item.get("source", "")) != source]
        if len(remaining) == len(queue):
            return False
        save_pending(path, remaining)
        return True


# ---- the queue ---------------------------------------------------------------

class RunRequest(BaseModel):
    kind: str = task_mod.TASK_PREPARE
    indices: list[int] | None = None  # None = every applicable chapter not yet done
    force: bool = False


@app.post("/api/projects/{pid}/run")
async def start_run(pid: str, req: RunRequest = RunRequest()) -> dict:
    """Queue work. Must be async: starting a worker schedules an asyncio task."""
    project, cfg = project_cfg(pid)
    if req.kind not in task_mod.TASK_KINDS:
        raise HTTPException(400, f"There is nothing called {req.kind!r} to do.")
    # Refused by NAME, not by accident. Today a manga happens to 400 here because its
    # source.json is empty — which is the right outcome reached by luck, and luck is
    # not good enough on the one route that spends the owner's allowance. The prose
    # path would translate concatenated bubble text: paid, wrong, and with no
    # per-bubble mapping for the overlay to draw.
    if project.get("kind") == pj.KIND_MANGA:
        raise HTTPException(400, "This is a manga. Its pages are read on the pages "
                                 "screen and its chapters are translated from there — "
                                 "the prose pipeline does not apply to it.")
    chapters = pj.load_source(pid)
    if not chapters:
        raise HTTPException(400, "This project has no source text yet.")
    state = State.load(cfg.paths.state_file)
    items = task_mod.resolve_items(chapters, state, req.kind, cfg,
                                   indices=req.indices, force=req.force)
    return jobs.enqueue(pid, cfg, items)


class CancelRequest(BaseModel):
    # Default false preserves the gentler semantics ("clear the queue, let the current
    # item finish"); true also stops the item in flight.
    stop_current: bool = False


@app.post("/api/projects/{pid}/run/cancel")
def cancel_run(pid: str, req: CancelRequest = CancelRequest()) -> dict:
    require_project(pid)
    return jobs.cancel(pid, stop_current=req.stop_current)


@app.post("/api/projects/{pid}/run/resume")
def resume_run(pid: str) -> dict:
    require_project(pid)
    return jobs.resume(pid)


@app.get("/api/queue")
def queue_overview() -> dict:
    """Live view of every project's queue — this is what Activity renders."""
    out = []
    for pid, job in jobs.running_jobs():
        project = pj.get_project(pid) or {}
        out.append({"pid": pid, "title": project.get("title", "Untitled"),
                    "kind_of_work": project.get("kind", pj.KIND_NOVEL),
                    "job_id": job.id, **job.queue_state()})
    return {"jobs": out}


@app.get("/api/projects/{pid}/active-job")
def active_job(pid: str) -> dict:
    """The in-flight job for this project, if any — lets the UI reattach its live
    progress stream (and current queue) after a reload or navigating away."""
    require_project(pid)
    job = jobs.active_job(pid)
    return {"job_id": job.id, **job.queue_state()} if job else {"job_id": None}


@app.get("/api/projects/{pid}/run/{job_id}/stream")
async def stream_job(pid: str, job_id: str, request: Request) -> StreamingResponse:
    job = jobs.get_job(job_id)
    if job is None or job.pid != pid:
        raise HTTPException(404, "There is no job with that id.")

    async def gen():
        # Register our own queue FIRST so no event slips through between replay and
        # live (subscribe-then-snapshot); every consumer gets its own copy of events.
        q: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(q)
        try:
            # Replay history so a reconnecting consumer (reload / second tab / the gap
            # between POST and connect) catches up. If the job already finished,
            # history ends with its terminal event.
            for ev in list(job.history):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("type") in ("done", "paused"):
                    return
            # Progress is deliberately not in history (it would evict the real
            # events), so catch a mid-item consumer up with one snapshot — otherwise a
            # reload during a long chapter lands on an empty console.
            frame = job.live_frame()
            if frame is not None:
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
            while True:
                # Wake periodically even with no events, so a client that navigated
                # away is detected and its subscriber queue released — otherwise this
                # coroutine blocks forever and publish() grows the list without bound.
                # The comment line doubles as a keep-alive through proxies.
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "paused"):
                    break
        finally:
            if q in job.subscribers:
                job.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


# ---- static (production) -----------------------------------------------------
if DIST_DIR.exists():
    # Serve the built interface when present (the launcher builds it). The frontend
    # uses client-side routing, so an unknown path must return index.html rather than
    # 404 — otherwise a refresh or a deep link breaks. Real built assets are served
    # from disk; everything else falls through to index.html. The API routes above are
    # registered first, so they always take precedence over this catch-all.
    _INDEX_HTML = DIST_DIR / "index.html"
    _DIST_RESOLVED = DIST_DIR.resolve()

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # Never shadow the API: an unmatched /api/* path should 404, not return HTML.
        if full_path == "api" or full_path.startswith("api/"):
            return Response(status_code=404)
        candidate = DIST_DIR / full_path
        try:
            if (full_path and candidate.is_file()
                    and candidate.resolve().is_relative_to(_DIST_RESOLVED)):
                return FileResponse(str(candidate))
        except (OSError, ValueError):
            pass
        return FileResponse(str(_INDEX_HTML))
