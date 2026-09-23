"""Reading a Google Doc into chapters — one tab per chapter.

The second of exactly two modules allowed to import a Google client, and like the
other it only ever reads. ``tests/test_scope_guards.py`` enforces both halves: the
import is confined here, and neither module may call a mutating Docs or Drive method.

Fetched with ``includeTabsContent=True``, which is what populates ``document.tabs``.
Each TOP-LEVEL tab is one chapter, in order; nested ``childTabs`` are flattened
depth-first into their parent, because a tab split into sub-sections is still one
chapter as far as the reader is concerned.

The output is the ordinary :class:`~morning.chapters.Chapter`, so everything
downstream — classify, translate, validate, read — works on a Doc exactly as it works
on a paste. That convergence is the whole point of the Chapter contract.
"""

from __future__ import annotations

import re

from googleapiclient.discovery import build

from .chapters import Chapter, strip_invisibles

_DOC_ID_IN_URL = re.compile(r"/d/([a-zA-Z0-9_-]{20,})")
_BARE_DOC_ID = re.compile(r"^[a-zA-Z0-9_-]{20,}$")


class DocumentError(RuntimeError):
    """The document could not be read as a set of chapters."""


def extract_doc_id(url_or_id: str) -> str | None:
    """Pull a document id out of a pasted URL, or accept a bare id.

    People paste the whole address bar. Asking them to find the id inside it is a
    step that exists only because the code could not be bothered.
    """
    text = (url_or_id or "").strip()
    match = _DOC_ID_IN_URL.search(text)
    if match:
        return match.group(1)
    return text if _BARE_DOC_ID.match(text) else None


def _read_paragraph(paragraph: dict) -> str:
    """One paragraph's text runs, concatenated."""
    parts = []
    for element in paragraph.get("elements", []):
        run = element.get("textRun")
        if run and "content" in run:
            parts.append(run["content"])
    return strip_invisibles("".join(parts)).strip("\n")


def _read_elements(elements: list[dict]) -> list[str]:
    """Walk a body's structural elements into paragraph strings.

    Handles paragraphs and tables, recursing into cells. Blank paragraphs are dropped
    so that the blank-line separation between paragraphs stays meaningful — a Doc
    written with an empty line between every paragraph would otherwise double every
    gap. Table-of-contents and section-break elements carry no prose and are skipped.
    """
    paragraphs: list[str] = []
    for element in elements:
        if "paragraph" in element:
            text = _read_paragraph(element["paragraph"])
            if text.strip():
                paragraphs.append(text)
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    paragraphs.extend(_read_elements(cell.get("content", [])))
    return paragraphs


def _tab_paragraphs(tab: dict, *, flatten_children: bool) -> list[str]:
    body = tab.get("documentTab", {}).get("body", {})
    paragraphs = _read_elements(body.get("content", []))
    if flatten_children:
        for child in tab.get("childTabs", []):
            paragraphs.extend(_tab_paragraphs(child, flatten_children=True))
    return paragraphs


def _tab_title(tab: dict, position: int) -> str:
    title = tab.get("tabProperties", {}).get("title", "").strip()
    return title or f"Chapter {position}"


def extract_chapters(document: dict, *, flatten_children: bool = True) -> list[Chapter]:
    """Turn a fetched document into ordered chapters, one per top-level tab."""
    tabs = document.get("tabs")
    if not tabs:
        raise DocumentError(
            "That document has no tabs. Morning Reader reads one chapter per tab, so "
            "the document needs to use them — and it must be fetched with tab content "
            "included.")

    chapters: list[Chapter] = []
    for position, tab in enumerate(tabs, start=1):
        paragraphs = _tab_paragraphs(tab, flatten_children=flatten_children)
        chapters.append(Chapter(index=position, title=_tab_title(tab, position),
                                paragraphs=paragraphs))
    return chapters


def build_docs_service(creds):
    """The Docs client.

    ``cache_discovery=False`` because the default file cache warns noisily under a
    server and buys nothing here — this builds once per fetch, not once per request.
    """
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def fetch_document(service, doc_id: str) -> dict:
    """Read one document, tabs included.

    ``documents().get`` is the ONLY Google call this app makes. There is no
    ``batchUpdate`` here and there must never be one: the token is read-only, so it
    would fail anyway, and the scope guard refuses the method name outright.
    """
    return service.documents().get(documentId=doc_id,
                                   includeTabsContent=True).execute()


def load_chapters(creds, doc_id: str, *, flatten_children: bool = True) -> list[Chapter]:
    """Fetch and extract in one step — what the server actually calls."""
    document = fetch_document(build_docs_service(creds), doc_id)
    return extract_chapters(document, flatten_children=flatten_children)


def document_title(document: dict) -> str:
    return str(document.get("title") or "").strip()
