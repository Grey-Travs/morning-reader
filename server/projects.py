"""Project storage: one self-contained folder per work.

Each novel or manga is a project under ``projects/<id>/`` with its own source
snapshot, state, and outputs. Global ``config.toml`` holds shared defaults (model,
effort, validation). A project's *effective* config is the global config with the
per-project paths overlaid, so the whole engine works per-project unchanged.

Project ids are server-generated hex (never user-controlled), so a pasted title can
never become a filesystem path — no traversal, and no surprise when a title contains
a slash, a colon, or a full-width character Windows refuses.

On disk::

    projects/<pid>/
      project.json     what this work is: title, kind, ingestion, timestamps
      source.json      the source, as ordered chapter records
      state.json       per-chapter status / hash / usage / cost
      chapters/        English output (step 2)
      pages/           page images (step 3)
      pages.json       the page manifest, each page carrying its PageRead (step 3)
      audit/           per-chapter audit trail (step 2)

**Kind is on the record from day one.** A project is a novel or a manga; they share
everything above the page-read contract and fork below it. Adding the discriminator
later would mean guessing the kind of every project already on the shelf.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from morning.atomic import atomic_write_json
from morning.chapters import Chapter, chapters_to_records, records_to_chapters
from morning.config import Config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = PROJECT_ROOT / "projects"

# What this work is. The fork below the page-read contract: a novel is pages of prose
# that flatten into chapters; a manga is pages of regions that stay regions.
KIND_NOVEL = "novel"
KIND_MANGA = "manga"
KINDS = (KIND_NOVEL, KIND_MANGA)

# How the source got here. Four paths are planned; step 1 ships the first.
INGEST_TEXT = "text"       # pasted text or an uploaded .txt
INGEST_DOCS = "docs"       # a Google Doc                     (step 2)
INGEST_IMAGES = "images"   # photographs or scans             (step 3)
INGEST_EXPORT = "export"   # a Japanese site export           (step 5)
INGESTS = (INGEST_TEXT, INGEST_DOCS, INGEST_IMAGES, INGEST_EXPORT)

# Where a work is in its life, as the person running this app thinks of it. Purely a
# label: nothing in the pipeline reads it. It exists because a shelf of sixty works
# gives no way to tell the three you are actively reading from the fifty that are done.
STATUSES = ("ongoing", "completed", "hiatus")
DEFAULT_STATUS = "ongoing"

# Fields a user may edit at runtime; everything else is server-managed. `kind` is
# deliberately absent: a novel cannot become a manga, and letting it change would
# leave the project's stored pages meaning something they do not.
EDITABLE_FIELDS = {"title", "style_note", "instructions", "honorific_note",
                   "archived", "status"}

_PROJECT_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_dir(pid: str) -> Path:
    return PROJECTS_DIR / pid


def _read_json(path: Path):
    """Read a JSON file, or None if it is missing or unreadable.

    A truncated/half-written project.json must not turn every request for that project
    into a 500 — it is treated as missing, exactly as the library listing treats it.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None


def list_projects() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []
    out = []
    for path in PROJECTS_DIR.glob("*/project.json"):
        record = _read_json(path)
        if isinstance(record, dict) and record.get("id"):
            out.append(record)
    out.sort(key=lambda p: p.get("created_at", ""))
    return out


def get_project(pid: str) -> dict | None:
    if not _PROJECT_ID_RE.match(pid or ""):
        return None
    record = _read_json(project_dir(pid) / "project.json")
    return record if isinstance(record, dict) else None


def _write_project(project: dict) -> dict:
    atomic_write_json(project_dir(project["id"]) / "project.json", project)
    return project


def create_project(title: str, *, kind: str = KIND_NOVEL, ingest: str = INGEST_TEXT,
                   pid: str | None = None) -> dict:
    """Create an empty project folder and its record.

    Raises ``ValueError`` on an unknown kind or ingestion rather than defaulting to
    novel: a manga silently created as a novel would take the flatten path and lose
    every region it ever reads.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    if ingest not in INGESTS:
        raise ValueError(f"unknown ingestion {ingest!r}; expected one of {INGESTS}")
    pid = pid or uuid.uuid4().hex[:12]
    if not _PROJECT_ID_RE.match(pid):
        raise ValueError("invalid project id")

    pdir = project_dir(pid)
    (pdir / "chapters").mkdir(parents=True, exist_ok=True)
    (pdir / "audit").mkdir(parents=True, exist_ok=True)
    if ingest == INGEST_IMAGES:
        (pdir / "pages").mkdir(parents=True, exist_ok=True)

    return _write_project({
        "id": pid,
        "title": (title or "").strip() or "Untitled",
        "kind": kind,
        "ingest": ingest,
        "status": DEFAULT_STATUS,
        "archived": False,
        "chapter_count": 0,
        "created_at": _now(),
    })


def update_project(pid: str, **fields) -> dict:
    """Patch editable project fields. Unknown keys are ignored, not an error — the
    frontend sends whole records back and a new server field must not 400 an old UI."""
    project = get_project(pid)
    if project is None:
        raise KeyError(pid)
    for key, value in fields.items():
        if key not in EDITABLE_FIELDS:
            continue
        if key == "title":
            text = (value or "").strip() if isinstance(value, str) else ""
            project["title"] = text or project.get("title") or "Untitled"
        elif key == "status":
            if value in STATUSES:
                project["status"] = value
        elif key == "archived":
            project["archived"] = bool(value)
        else:
            project[key] = (value or "").strip() if isinstance(value, str) else value
    project["updated_at"] = _now()
    return _write_project(project)


def delete_project(pid: str) -> bool:
    """Remove a project and everything in it. Returns False if there was nothing there.

    Guarded by the id pattern *and* by resolving inside PROJECTS_DIR, because this is
    the one operation in the app where a bad id is unrecoverable.
    """
    if not _PROJECT_ID_RE.match(pid or ""):
        return False
    pdir = project_dir(pid)
    try:
        resolved = pdir.resolve()
    except OSError:
        return False
    if resolved.parent != PROJECTS_DIR.resolve() or not resolved.is_dir():
        return False
    import shutil
    shutil.rmtree(resolved)
    return True


# ---- the source snapshot -----------------------------------------------------

def source_file(pid: str) -> Path:
    return project_dir(pid) / "source.json"


def save_source(pid: str, chapters: list[Chapter]) -> None:
    """Snapshot the source so the project folder is fully self-contained: it can be
    read, copied or backed up on any machine without re-fetching wherever it came
    from. Atomic, so a crash mid-write never leaves a half-written snapshot."""
    atomic_write_json(source_file(pid), chapters_to_records(chapters))


def load_source(pid: str) -> list[Chapter]:
    records = _read_json(source_file(pid))
    return records_to_chapters(records) if isinstance(records, list) else []


def set_chapter_count(pid: str, count: int) -> dict | None:
    project = get_project(pid)
    if project is None:
        return None
    project["chapter_count"] = int(count)
    project["updated_at"] = _now()
    return _write_project(project)


# ---- effective config --------------------------------------------------------

def project_config(global_cfg: Config, project: dict) -> Config:
    """Global config with this project's paths and overrides overlaid."""
    cfg = global_cfg.model_copy(deep=True)
    pdir = project_dir(project["id"])
    cfg.paths.output_dir = pdir / "chapters"
    cfg.paths.state_file = pdir / "state.json"
    cfg.paths.audit_dir = pdir / "audit"
    cfg.paths.glossary_json = pdir / "glossary.json"
    cfg.paths.glossary_pending = pdir / "glossary_pending.json"
    # Per-project overrides. Novels and manga are separate works with no shared
    # glossary and no shared framing, which is why these live on the project rather
    # than being global.
    if project.get("style_note"):
        cfg.translation.style_note = project["style_note"]
    if project.get("instructions"):
        cfg.translation.extra_instruction = project["instructions"]
    if project.get("honorific_note"):
        cfg.translation.honorific_note = project["honorific_note"]
    return cfg
