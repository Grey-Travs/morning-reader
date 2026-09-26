"""Reading a Google Doc into chapters.

**No test here reaches Google.** The extractor is fed document dictionaries shaped
like the API's responses, and the endpoints are driven with the fetch replaced. That
covers everything except the network call itself, which is one line.

Japanese fixtures are invented for these tests.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from morning import docs_source, google_auth
from morning.config import Config
from server import jobs, projects as pj
from server.app import app

JA_A = "電車はまだ来ない。"
JA_B = "彼女はホームの端に立っていた。"


# ---- building document dictionaries the way the API returns them ---------------

def _para(text: str) -> dict:
    return {"paragraph": {"elements": [{"textRun": {"content": text}}]}}


def _tab(title: str, *paragraphs: str, children: list | None = None) -> dict:
    tab = {
        "tabProperties": {"title": title},
        "documentTab": {"body": {"content": [_para(p) for p in paragraphs]}},
    }
    if children:
        tab["childTabs"] = children
    return tab


def _document(*tabs: dict, title: str = "A novel") -> dict:
    return {"title": title, "tabs": list(tabs)}


# ---- the document id -----------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit",
     "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
    ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit#heading=x",
     "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
    ("1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
    ("  1AbCdEfGhIjKlMnOpQrStUvWxYz012345  ", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
])
def test_an_id_is_found_in_whatever_was_pasted(given, expected):
    """People paste the whole address bar. Asking them to find the id inside it is a
    step that exists only because the code could not be bothered."""
    assert docs_source.extract_doc_id(given) == expected


@pytest.mark.parametrize("given", ["", "   ", "not a link", "https://example.com",
                                   "short", None])
def test_junk_yields_no_id(given):
    assert docs_source.extract_doc_id(given) is None


# ---- extracting chapters --------------------------------------------------------

def test_each_top_level_tab_is_one_chapter():
    chapters = docs_source.extract_chapters(_document(
        _tab("第1話", JA_A, JA_B),
        _tab("第2話", JA_A)))

    assert [c.index for c in chapters] == [1, 2]
    assert [c.title for c in chapters] == ["第1話", "第2話"]
    assert chapters[0].paragraphs == [JA_A, JA_B]


def test_a_tab_without_a_title_gets_a_numbered_one():
    chapters = docs_source.extract_chapters(_document(
        {"documentTab": {"body": {"content": [_para(JA_A)]}}}))

    assert chapters[0].title == "Chapter 1"


def test_nested_tabs_are_flattened_into_their_parent():
    """A tab split into sub-sections is still one chapter as far as the reader is
    concerned."""
    chapters = docs_source.extract_chapters(_document(
        _tab("第1話", JA_A,
             children=[_tab("part b", JA_B),
                       _tab("part c", "続き")])))

    assert len(chapters) == 1
    assert chapters[0].paragraphs == [JA_A, JA_B, "続き"]


def test_nesting_goes_all_the_way_down():
    chapters = docs_source.extract_chapters(_document(
        _tab("top", "a", children=[_tab("mid", "b", children=[_tab("deep", "c")])])))

    assert chapters[0].paragraphs == ["a", "b", "c"]


def test_nested_tabs_can_be_kept_separate():
    chapters = docs_source.extract_chapters(
        _document(_tab("第1話", JA_A, children=[_tab("part b", JA_B)])),
        flatten_children=False)

    assert chapters[0].paragraphs == [JA_A]


def test_table_cells_are_read_as_prose():
    """Some documents lay a chapter out in a table. Skipping tables would silently
    drop the whole chapter."""
    document = _document({"tabProperties": {"title": "t"}, "documentTab": {"body": {
        "content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [_para(JA_A)]},
                            {"content": [_para(JA_B)]}]}]}}]}}})

    assert docs_source.extract_chapters(document)[0].paragraphs == [JA_A, JA_B]


def test_blank_paragraphs_are_dropped():
    """A document written with an empty line between every paragraph would otherwise
    double every gap, and the paragraph count would be twice the real one."""
    chapters = docs_source.extract_chapters(_document(
        _tab("t", JA_A, "", "   ", "\n", JA_B)))

    assert chapters[0].paragraphs == [JA_A, JA_B]


def test_invisible_characters_are_stripped():
    """Copy-protection watermarks. Left in, they move the content hash and re-bill a
    chapter that did not change."""
    chapters = docs_source.extract_chapters(_document(
        _tab("t", JA_A[:3] + "​﻿" + JA_A[3:])))

    assert chapters[0].paragraphs == [JA_A]


def test_structural_elements_that_carry_no_prose_are_skipped():
    document = _document({"tabProperties": {"title": "t"}, "documentTab": {"body": {
        "content": [{"sectionBreak": {}}, _para(JA_A), {"tableOfContents": {}}]}}})

    assert docs_source.extract_chapters(document)[0].paragraphs == [JA_A]


def test_a_document_with_no_tabs_says_what_is_wrong():
    """One chapter per tab is the contract, so a document that does not use them
    cannot be read — and the message has to say that rather than "no chapters"."""
    with pytest.raises(docs_source.DocumentError, match="tabs"):
        docs_source.extract_chapters({"title": "x"})


def test_an_empty_tab_becomes_an_empty_chapter_not_an_error():
    """A blank placeholder tab is ordinary. The pipeline classifies it as empty and
    skips it; refusing the whole document over one would be wrong."""
    chapters = docs_source.extract_chapters(_document(_tab("blank")))

    assert chapters[0].paragraphs == []


def test_the_chapters_are_the_ordinary_contract():
    """A Doc produces exactly what a paste produces, which is what lets everything
    downstream work on either without knowing which it was."""
    chapter = docs_source.extract_chapters(_document(_tab("t", JA_A, JA_B)))[0]

    assert chapter.metrics.content_hash
    assert chapter.metrics.source_fraction > 0.8


# ---- signing in ------------------------------------------------------------------

def test_the_scopes_are_read_only_and_do_not_reach_drive():
    """The strongest form of "nothing publishes": enforced at Google's end, not by
    our own discipline. A read-only token cannot write even if this code tried."""
    assert google_auth.SCOPES == [
        "https://www.googleapis.com/auth/documents.readonly"]


def test_not_being_connected_is_an_ordinary_state(tmp_path):
    """Most projects never touch Google at all, so "no token" returns None rather
    than raising."""
    assert google_auth.saved_credentials(tmp_path / "token.json") is None
    assert google_auth.is_connected(tmp_path / "token.json") is False


def test_a_corrupt_token_is_the_same_as_no_token(tmp_path):
    """There is nothing in it worth preserving, and failing a request over it would
    be worse than asking the user to sign in again."""
    path = tmp_path / "token.json"
    path.write_text("{ truncated", encoding="utf-8")

    assert google_auth.load_token(path) is None
    assert google_auth.saved_credentials(path) is None


def test_a_token_without_the_scopes_is_not_usable(tmp_path):
    """The check reads the token's OWN scopes, which only works because load_token
    passes no scope list — otherwise the credential claims whatever it was given."""
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "token": "x", "refresh_token": "y", "client_id": "c", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets.readonly"],
    }), encoding="utf-8")

    creds = google_auth.load_token(path)
    assert creds is not None
    assert google_auth.missing_scopes(creds) == google_auth.SCOPES
    assert google_auth.saved_credentials(path) is None


def test_signing_in_without_a_client_file_explains_what_to_do(tmp_path):
    with pytest.raises(google_auth.GoogleAuthError, match="Cloud console"):
        google_auth.sign_in(tmp_path / "client_secret.json", tmp_path / "token.json")


def test_forgetting_removes_the_token(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{}", encoding="utf-8")

    assert google_auth.forget(path) is True
    assert not path.exists()
    assert google_auth.forget(path) is False


# ---- the endpoints ----------------------------------------------------------------

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def connected(monkeypatch):
    """Stand in for a signed-in user, and for the fetch."""
    chapters = docs_source.extract_chapters(_document(
        _tab("第1話", JA_A, JA_B), _tab("第2話", JA_A)))
    monkeypatch.setattr(google_auth, "saved_credentials", lambda token_file: object())
    monkeypatch.setattr(docs_source, "load_chapters",
                        lambda creds, doc_id, **kw: list(chapters))
    return chapters


DOC_URL = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit"


def test_the_status_distinguishes_no_client_file_from_no_token(client):
    """The fixes are different: one means "set up an OAuth client", the other means
    "click connect"."""
    body = client.get("/api/google/status").json()

    assert body["connected"] is False
    assert "credentials_present" in body
    assert body["scopes"] == google_auth.SCOPES


def test_a_project_is_created_from_a_document(client, connected):
    response = client.post("/api/projects/docs",
                           json={"document": DOC_URL, "title": "From a doc"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chapters"] == 2
    assert body["project"]["ingest"] == "docs"
    assert body["project"]["source_document"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"

    rows = client.get(f"/api/projects/{body['project']['id']}").json()["chapters"]
    assert [r["title"] for r in rows] == ["第1話", "第2話"]


def test_creating_without_being_connected_is_a_401(client, monkeypatch):
    monkeypatch.setattr(google_auth, "saved_credentials", lambda token_file: None)
    response = client.post("/api/projects/docs", json={"document": DOC_URL})

    assert response.status_code == 401
    assert "not connected" in response.json()["detail"]["title"]


def test_a_link_that_is_not_a_document_is_a_clean_400(client, connected):
    response = client.post("/api/projects/docs", json={"document": "not a link"})

    assert response.status_code == 400
    assert "Google Doc link" in response.json()["detail"]["title"]


def test_a_manga_is_refused_before_google_is_asked_anything(client, monkeypatch):
    """A manga is its pictures. Made from a Doc it would get a prose source and no
    pages — a work nothing can translate or read. Refused before the fetch, so the
    answer does not wait on Google or depend on being signed in."""
    monkeypatch.setattr(google_auth, "saved_credentials", lambda token_file: object())

    def must_not_fetch(*args, **kw):
        raise AssertionError("the document was fetched for a manga")

    monkeypatch.setattr(docs_source, "load_chapters", must_not_fetch)
    response = client.post("/api/projects/docs",
                           json={"document": DOC_URL, "kind": "manga"})

    assert response.status_code == 400
    assert "page images" in response.json()["detail"]["title"]
    assert client.get("/api/projects").json()["projects"] == []


def test_a_document_without_tabs_is_reported_as_such(client, monkeypatch):
    monkeypatch.setattr(google_auth, "saved_credentials", lambda token_file: object())

    def no_tabs(creds, doc_id, **kw):
        raise docs_source.DocumentError("That document has no tabs.")

    monkeypatch.setattr(docs_source, "load_chapters", no_tabs)
    response = client.post("/api/projects/docs", json={"document": DOC_URL})

    assert response.status_code == 400
    assert "no tabs" in response.json()["detail"]["title"]


def test_refreshing_re_reads_the_document(client, connected, monkeypatch):
    """The reason refresh exists: a Doc gets edited. Without it, fixing the Japanese
    and re-translating produces the SAME English, because the worker reads the stored
    snapshot and the content hash never moves."""
    pid = client.post("/api/projects/docs",
                      json={"document": DOC_URL}).json()["project"]["id"]

    edited = docs_source.extract_chapters(_document(
        _tab("第1話", "電車は遅れている。"),
        _tab("第2話", JA_A), _tab("第3話", JA_B)))
    monkeypatch.setattr(docs_source, "load_chapters",
                        lambda creds, doc_id, **kw: list(edited))

    response = client.post(f"/api/projects/{pid}/source/refresh")

    assert response.json()["chapters"] == 3
    rows = client.get(f"/api/projects/{pid}").json()["chapters"]
    assert len(rows) == 3
    assert "遅れている" in \
        client.get(f"/api/projects/{pid}/chapters/1").json()["source"][0]


def test_refreshing_a_pasted_project_says_there_is_nothing_to_refresh(client):
    pid = client.post("/api/projects/text",
                      json={"title": "t", "text": JA_A}).json()["project"]["id"]

    response = client.post(f"/api/projects/{pid}/source/refresh")

    assert response.status_code == 400
    assert "not come from a Google Doc" in response.json()["detail"]["title"]


def test_refreshing_is_refused_while_work_is_in_flight(client, connected):
    """Replacing every chapter underneath a running sweep would have it translate
    chapter 4 of the old text and chapter 5 of the new one."""
    pid = client.post("/api/projects/docs",
                      json={"document": DOC_URL}).json()["project"]["id"]
    job = jobs.Job("held", pid)
    jobs._jobs[job.id] = job
    jobs._active_job_by_project[pid] = job.id
    try:
        assert client.post(f"/api/projects/{pid}/source/refresh").status_code == 409
    finally:
        job.done = True


def test_disconnecting_reports_whether_there_was_a_token(client):
    assert client.post("/api/google/disconnect").json()["disconnected"] in (True, False)
