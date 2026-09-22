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
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from morning.chapter_files import (
    has_previous, output_total, read_audit_translation, read_chapter,
)
from morning.chapters import classify
from morning.config import Config
from morning.glossary import (
    VALID_TYPES, Glossary, GlossaryEntry, glossary_lock, load_pending, save_pending,
)
from morning.pipeline import accept_chapter
from morning.state import State
from morning.textsource import decode_upload, split_text_into_chapters

from . import errors, jobs, projects as pj, tasks as task_mod
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

@app.get("/api/projects")
def list_projects() -> dict:
    out = []
    for project in pj.list_projects():
        cfg = pj.project_config(load_global_config(), project)
        state = State.load(cfg.paths.state_file)
        out.append({**project, "totals": state.totals()})
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


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    project, cfg = project_cfg(pid)
    state = State.load(cfg.paths.state_file)
    return {"project": project, "chapters": _chapter_rows(pid, cfg),
            "totals": state.totals()}


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


# ---- reading -----------------------------------------------------------------

def _require_chapter(pid: str, index: int) -> tuple[list, object]:
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
    _, cfg = project_cfg(pid)
    if req.kind not in task_mod.TASK_KINDS:
        raise HTTPException(400, f"There is nothing called {req.kind!r} to do.")
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
