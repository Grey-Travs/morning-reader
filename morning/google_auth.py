"""Signing in to Google, for reading a document and nothing else.

This is one of exactly two modules allowed to import a Google client (the other is
:mod:`morning.docs_source`), and ``tests/test_scope_guards.py`` enforces that — along
with a check that neither of them ever calls a mutating Docs or Drive method.

**The scopes are read-only, and that is the real guarantee.** A token granted
``documents.readonly`` cannot write to a document even if this code tried: the refusal
happens at Google, not here. Morning Reader does not publish, and this is the one place
where that promise can be made by something other than our own discipline — so it is.

Two details that look like fussiness and are not:

* **Loading a token passes NO scope list.** ``Credentials.from_authorized_user_file``
  takes the list it is GIVEN rather than reading the token's own, so passing the
  current scopes makes an old narrow token claim permissions the user never granted —
  and the next refresh writes that claim back to disk. Passing nothing means the token
  reports what it actually has, which is what ``missing_scopes`` needs to be true.
* **An expired refresh token is not an error to show the user.** An OAuth app still in
  "Testing" has refresh tokens that expire in a week. That should send them back
  through consent, not report a failure they cannot act on.
"""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Read a document. Nothing else, ever.
#
# There is deliberately no write scope and no Drive scope in this app. Night Reader
# has both, because it creates documents; this one only ever reads, and asking for
# less means a token that leaks is a token that can only read.
SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]


class GoogleAuthError(RuntimeError):
    """Signing in failed in a way the user has to do something about."""


def missing_scopes(creds: Credentials, scopes: list[str] | None = None) -> list[str]:
    """Which of ``scopes`` this token was never granted.

    Reads the token's OWN scopes, which only works because ``load_token`` deliberately
    passes no scope list — see the module docstring.
    """
    have = set(creds.scopes or [])
    return [s for s in (scopes or SCOPES) if s not in have]


def load_token(token_file: str | Path) -> Credentials | None:
    """The saved credential, or None. Never runs a consent flow."""
    path = Path(token_file)
    if not path.exists():
        return None
    try:
        # No scope list, on purpose. See the module docstring.
        return Credentials.from_authorized_user_file(str(path))
    except (ValueError, json.JSONDecodeError, OSError, KeyError):
        # A corrupt token is the same as no token: the user signs in again. It is not
        # worth failing a request over, and there is nothing in it to preserve.
        return None


def save_token(token_file: str | Path, creds: Credentials) -> None:
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    # NOT the shared atomic writer: this file holds a refresh token, and the atomic
    # writer leaves a temp file beside its target for a moment. Writing it directly,
    # to a path the .gitignore already covers, keeps the secret in exactly one place.
    path.write_text(creds.to_json(), encoding="utf-8")


def saved_credentials(token_file: str | Path) -> Credentials | None:
    """Cached credentials, refreshed if they have expired. No consent flow.

    Returns None rather than raising when there is nothing usable, because "not signed
    in" is an ordinary state for this app — most projects never touch Google at all.
    """
    creds = load_token(token_file)
    if creds is None or missing_scopes(creds):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001 — a revoked or expired grant is not an error
            return None
        save_token(token_file, creds)
        return creds
    return None


def is_connected(token_file: str | Path) -> bool:
    return saved_credentials(token_file) is not None


def sign_in(credentials_file: str | Path, token_file: str | Path) -> Credentials:
    """Return usable credentials, running the consent flow if needed.

    Blocking, and it opens a browser — so it is only ever called from an explicit
    "connect Google" action, never from a translation or a page load.
    """
    creds = saved_credentials(token_file)
    if creds is not None:
        return creds

    path = Path(credentials_file)
    if not path.exists():
        raise GoogleAuthError(
            f"{path.name} is missing. Create an OAuth client (Desktop app) in the "
            f"Google Cloud console under APIs & Services > Credentials, download it, "
            f"and save it as {path}.")

    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
    creds = flow.run_local_server(port=0)
    save_token(token_file, creds)
    return creds


def forget(token_file: str | Path) -> bool:
    """Sign out by deleting the cached token. Returns whether there was one."""
    path = Path(token_file)
    if not path.exists():
        return False
    path.unlink()
    return True
