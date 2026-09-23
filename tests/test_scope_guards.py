"""Guards on the two scope decisions this app exists to keep.

These scan the source tree rather than exercising behaviour, which makes them unusual
— but both decisions are the kind that erode one convenient import at a time, and by
the time the erosion is visible it is expensive to undo. Night Reader's publishing
path reached ~6,000 lines and produced nearly every incident the project ever had; its
persisted ``"korean"`` strings are the single reason this is a separate app rather
than a branch.

A test is the cheapest way to keep a decision honest.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("morning", "server")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for package in PACKAGES:
        files.extend(sorted((ROOT / package).rglob("*.py")))
    files.extend(sorted((ROOT / "tests").rglob("*.py")))
    files.extend(p for p in ROOT.glob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def _source_files() -> list[Path]:
    """Every file a human wrote here — Python, JavaScript, config, docs."""
    out = list(_python_files())
    web = ROOT / "web" / "src"
    if web.exists():
        for pattern in ("*.js", "*.jsx", "*.ts", "*.tsx", "*.css", "*.html"):
            out.extend(sorted(web.rglob(pattern)))
    for name in ("requirements.txt", "config.example.toml", "README.md"):
        if (ROOT / name).exists():
            out.append(ROOT / name)
    return out


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


# ---- no publishing -----------------------------------------------------------

# Modules that exist to talk to a remote host. Importing any of them is how a
# publishing path starts, so the import itself is what is refused — not a later call.
_NETWORK_MODULES = {
    "requests", "httpx", "aiohttp", "urllib3", "http.client", "httplib2",
    "urllib.request", "socketserver", "ftplib", "smtplib", "telnetlib",
    "selenium", "playwright", "pyppeteer", "mechanize", "websocket", "websockets",
}

# ``httpx`` is a test-only dependency: fastapi.testclient drives the app in-process
# through it, which is the opposite of an outbound call. Allowed only under tests/.
_TEST_ONLY_NETWORK = {"httpx"}

# Reading a Google Doc is INGESTION, not publishing — the plan lists it as one of four
# source paths. But its client is an HTTP client, and an import cannot tell "read a
# document you own" from "post to a site".
#
# So the ban is not lifted, it is MOVED: the client may be imported in exactly these
# two modules, and `test_the_google_client_is_only_ever_used_to_read` then checks what
# those modules actually CALL. That is a stricter test than the blanket import ban it
# replaces, because it inspects the operation rather than the dependency.
_GOOGLE_MODULES = {"morning/google_auth.py", "morning/docs_source.py"}
_GOOGLE_CLIENTS = {"googleapiclient", "google", "google_auth_oauthlib", "httplib2",
                   "google_auth_httplib2"}

# Methods that change something on Google's side. None of them may appear in the two
# modules above. `documents().get()` is the only call this app makes, and the token's
# scope is read-only, so any of these would fail at Google anyway — refusing the NAME
# means it never gets written in the first place.
_MUTATING_METHODS = {
    "batchUpdate", "batchCreate", "batchDelete", "create", "insert", "update",
    "patch", "delete", "trash", "copy", "move", "publish",
}

# Drive is a different surface with a different blast radius, and this app has no
# business on it at all: it reads one document by id.
_FORBIDDEN_SERVICES = {"files", "drive", "permissions", "revisions"}


def _imported_modules(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        pytest.fail(f"{_relative(path)} could not be parsed")
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_no_module_imports_an_outbound_http_client():
    """The scope decision, enforced. Morning Reader translates and reads; nothing in
    it writes to an external site.

    Checked at the import, because that is the last point where the decision is still
    one line. Once a client is imported, adding the call that uses it looks like an
    ordinary change.
    """
    offences: list[str] = []
    for path in _python_files():
        is_test = "tests" in path.parts
        is_google_module = _relative(path) in _GOOGLE_MODULES
        for imported in _imported_modules(path):
            root = imported.split(".")[0]
            if is_google_module and root in _GOOGLE_CLIENTS:
                continue  # allowed here, and checked by the operation test below
            if imported in _NETWORK_MODULES or root in _NETWORK_MODULES:
                if is_test and root in _TEST_ONLY_NETWORK:
                    continue
                offences.append(f"{_relative(path)} imports {imported}")

    assert not offences, (
        "Morning Reader does not publish. An outbound HTTP client appeared:\n  "
        + "\n  ".join(offences))


def test_the_google_client_is_confined_to_the_two_reading_modules():
    """The exemption above is narrow by construction, so it has to stay narrow.

    If a third module ever imports the client, this fails — which is the point: the
    operation check below only inspects those two files, so a client imported anywhere
    else would be unguarded.
    """
    offences = []
    for path in _python_files():
        if _relative(path) in _GOOGLE_MODULES or "tests" in path.parts:
            continue
        for imported in _imported_modules(path):
            if imported.split(".")[0] in _GOOGLE_CLIENTS:
                offences.append(f"{_relative(path)} imports {imported}")

    assert not offences, (
        "A Google client may only be imported by "
        + ", ".join(sorted(_GOOGLE_MODULES)) + ". Found:\n  " + "\n  ".join(offences))


def test_the_google_client_is_only_ever_used_to_read():
    """The operation-level guard that replaces a blanket import ban.

    Reading a document is ingestion; writing one would be publishing. An import cannot
    tell those apart, so this checks the METHOD NAMES the two allowed modules call.
    ``documents().get()`` is the only Google call this app makes.

    Stricter than the ban it replaces: a blanket import ban would have been satisfied
    by not importing the client, while saying nothing about what code that DID import
    it went on to do.
    """
    offences: list[str] = []
    for name in sorted(_GOOGLE_MODULES):
        path = ROOT / name
        assert path.exists(), f"{name} is listed as a Google module but does not exist"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in _MUTATING_METHODS:
                offences.append(f"{name}:{node.lineno}: calls .{node.attr}()")
            if node.attr in _FORBIDDEN_SERVICES:
                offences.append(f"{name}:{node.lineno}: reaches .{node.attr}()")

    assert not offences, (
        "Morning Reader reads documents and never writes them. Found:\n  "
        + "\n  ".join(offences))


def _mutating_calls(source: str) -> list[str]:
    """The banned method names a piece of source calls. Shared by the guard above and
    by the test below that proves the guard can actually fail."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            if node.attr in _MUTATING_METHODS or node.attr in _FORBIDDEN_SERVICES:
                found.append(node.attr)
    return found


@pytest.mark.parametrize("source,expected", [
    ("service.documents().get(documentId=x).execute()", []),
    ("service.documents().batchUpdate(body=x).execute()", ["batchUpdate"]),
    ("drive.files().create(body=x).execute()", ["files", "create"]),
    ("drive.files().delete(fileId=x).execute()", ["files", "delete"]),
    ("service.presentations().batchUpdate(body=x)", ["batchUpdate"]),
])
def test_the_operation_guard_can_actually_fail(source, expected):
    """A guard that has never seen a violation might be passing vacuously.

    This runs the same check against source that SHOULD trip it, so the passing
    result above means "nothing mutating is called", not "the check does nothing".
    """
    assert sorted(_mutating_calls(source)) == sorted(expected)


def test_the_google_scopes_are_read_only():
    """The strongest form of the promise: enforced by Google, not by our discipline.

    A token granted only ``documents.readonly`` cannot write to a document even if
    this code tried — the refusal happens at their end. Asking for less is the one
    place "nothing publishes" can be made true by something other than a test.
    """
    from morning.google_auth import SCOPES

    assert SCOPES, "there must be an explicit scope list, not an empty default"
    for scope in SCOPES:
        assert scope.endswith(".readonly"), f"{scope} is not a read-only scope"
        assert "drive" not in scope, f"{scope} reaches Drive; this app reads one doc"


def test_no_publishing_vocabulary_survives_in_module_names():
    """The shapes that carried it last time: a posting module, site adapters, a
    ledger, run claiming, a browser extension."""
    banned = ("posting.py", "publish.py", "adapters", "ledger.py", "extension")
    present = [_relative(p) for p in ROOT.rglob("*")
               if p.name in banned and "node_modules" not in p.parts
               and ".git" not in p.parts]

    assert not present, f"a publishing-shaped module appeared: {present}"


def test_no_route_writes_to_an_external_site():
    """Every declared route, read off the app itself rather than off the source.

    The cheapest possible check that the scope decision is still true of the running
    server, not merely of the files.
    """
    from server.app import app

    paths = [getattr(r, "path", "") for r in app.routes]
    for path in paths:
        assert not any(word in path for word in ("publish", "post-to", "upload-to",
                                                 "site", "remote")), path


def test_the_api_states_that_it_does_not_publish():
    """A scope decision anything built against this server should be able to see."""
    from fastapi.testclient import TestClient

    from server.app import app

    with TestClient(app) as client:
        assert client.get("/api/health").json()["publishing"] is False


# ---- the field is `source`, never a language name ----------------------------

# Words that name the source language rather than its ROLE. Any of them appearing in
# a persisted key, a field name or an API response is the mistake this app was
# separated in order to avoid.
_BANNED_NAMES = re.compile(
    r"\b("
    r"korean|hangul"                      # the app this one was split from; scope-guard: ok
    r"|japanese_fraction|japanese_hash"    # the same mistake in the new language; scope-guard: ok
    r"|jp_fraction|ja_fraction"  # scope-guard: ok
    r")\b",
    re.IGNORECASE,
)

# Prose may discuss the decision — that is how it stays understood. Only lines that
# would become an identifier or a stored value are checked. A line is treated as prose
# when it is a comment or sits inside a docstring.
#
# A line that genuinely must name the language — an assertion that the name is ABSENT,
# which is how the rule is tested — marks itself with this pragma. Per-line rather than
# per-file on purpose: exempting a whole test file would blind the guard to a real
# violation elsewhere in it, and these files are long.
_PRAGMA = "scope-guard: ok"


# How a comment starts, per file type. Markdown is prose end to end and is not
# scanned at all — the decision is explained there, which is the point of explaining it.
_COMMENT_MARKERS = {".js": "//", ".jsx": "//", ".ts": "//", ".tsx": "//", ".css": "/*",
                    ".toml": "#", ".txt": "#", ".html": "<!--"}
_PROSE_SUFFIXES = {".md"}


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments and docstrings removed, so only real code is examined.

    The pragma is looked for on the RAW line, before comments are stripped — it lives
    in a comment, so stripping first would erase the very marker being checked for.
    """
    if path.suffix in _PROSE_SUFFIXES:
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if path.suffix != ".py":
        marker = _COMMENT_MARKERS.get(path.suffix, "//")
        return [(i, line.split(marker)[0]) for i, line in enumerate(lines, 1)
                if _PRAGMA not in line]

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:  # pragma: no cover
        return list(enumerate(lines, 1))

    blanked = set()
    for node in ast.walk(tree):
        # Docstrings and any other bare string expression are prose.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            blanked.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    out = []
    for i, line in enumerate(lines, 1):
        if i in blanked or _PRAGMA in line:
            continue
        out.append((i, line.split("#")[0]))
    return out


def test_no_identifier_names_the_source_language():
    """``source``, never ``korean`` and never ``japanese``.

    The role, not the language. ``source_hash`` and ``source_fraction`` are written to
    disk on every record; Night Reader's ``hangul_fraction`` is on ~3,967 of them and
    cannot be cheaply renamed. Prose is exempt — discussing the decision is how it
    stays understood — but nothing that becomes an identifier or a stored value may
    name a language.
    """
    offences: list[str] = []
    for path in _source_files():
        for lineno, line in _code_lines(path):
            match = _BANNED_NAMES.search(line)
            if match:
                offences.append(f"{_relative(path)}:{lineno}: {match.group(0)}"
                                f"  |  {line.strip()[:80]}")

    assert not offences, (
        "The source language must be called `source`. Found:\n  "
        + "\n  ".join(offences))


def test_the_classification_values_are_language_free():
    from morning.chapters import CLASSES

    assert set(CLASSES) == {"source", "english", "empty"}


def test_the_persisted_state_keys_are_language_free():
    """What actually lands in state.json for a real chapter."""
    from morning.chapters import Chapter
    from morning.config import Config
    from morning.state import State
    from server.tasks import TaskContext, prepare_chapter

    result = prepare_chapter(
        Chapter(index=1, title="第1話",
                paragraphs=["電車はまだ来ない。"]),
        TaskContext(cfg=Config(), state=State(), total=1))

    assert "source_hash" in result.fields
    assert "source_fraction" in result.fields
    for key in result.fields:
        assert not _BANNED_NAMES.search(key), key


def test_the_api_calls_the_original_text_source():
    """The name reaches the browser too, which is what makes it impossible to forget:
    every consumer reads it under this name."""
    from fastapi.testclient import TestClient

    from server import projects as pj
    from server.app import app

    with TestClient(app) as client:
        created = client.post("/api/projects/text", json={
            "title": "x",
            "text": "電車はまだ来ない。",
        }).json()
        pid = created["project"]["id"]
        chapter = client.get(f"/api/projects/{pid}/chapters/1").json()

    assert "source" in chapter
    assert "source_hash" in chapter
    assert "source_fraction" in chapter
    for key in chapter:
        assert not _BANNED_NAMES.search(key), key
    assert pj.get_project(pid) is not None
