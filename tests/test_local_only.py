"""This server answers to this machine, and to nothing else.

``launch.py`` binds loopback, which stops a REMOTE attacker. It does nothing about the
owner's own browser, which is the delivery vehicle for both problems here:

* **DNS rebinding.** A domain that rebinds to 127.0.0.1 is SAME-ORIGIN to the browser.
  A page the owner has open could then list their projects, read every translation, and
  start runs that spend their Claude plan. Loopback is no defence at all, because the
  request really does come from loopback. Refusing a Host header this server does not
  recognise is what defeats it.
* **Cross-origin state change.** Some routes take no body and no project id, which
  makes them "simple requests" a foreign page can send with no preflight.
  ``POST /api/google/disconnect`` is the sharp one: it returns 200 and unlinks the
  saved refresh token.

The guard is deliberately narrow. A missing Origin is allowed, because the frontend is
served same-origin from ``web/dist`` and same-origin requests often send none — a
stricter rule would break the app to defend against nothing.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from server import app as app_mod
from server.app import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---- the Host header ----------------------------------------------------------

class TestTheHost:
    def test_a_foreign_host_is_refused(self, client):
        """The DNS-rebinding case. The request genuinely comes from loopback, so
        nothing below this layer can tell it apart from the real app."""
        response = client.get("/api/health", headers={"host": "attacker.example"})

        assert response.status_code == 421
        assert "localhost" in response.json()["detail"]["what"].lower() or \
            "attacker.example" in response.json()["detail"]["what"]

    def test_a_rebinding_host_cannot_read_the_library_either(self, client):
        assert client.get("/api/projects",
                          headers={"host": "rebind.evil.test"}).status_code == 421

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:8100",
                                      "127.0.0.1:8100", "[::1]", "[::1]:8100"])
    def test_the_machine_s_own_names_are_fine(self, client, host):
        """IPv6 arrives bracketed, so splitting on ':' first would return '[' and
        refuse a perfectly good local request."""
        assert client.get("/api/health", headers={"host": host}).status_code == 200

    def test_a_request_with_no_host_at_all_is_allowed(self, client):
        """Not every client sends one, and refusing them buys nothing — the attack
        needs a host that RESOLVES somewhere, which is exactly the case caught above."""
        assert client.get("/api/health", headers={"host": ""}).status_code == 200


# ---- the Origin header ---------------------------------------------------------

class TestTheOrigin:
    def test_the_disconnect_route_cannot_be_driven_from_another_site(self, client):
        """The specific exploit: no body, no project id, a simple content type — so a
        foreign page can send it with no preflight, and it unlinks the saved Google
        token."""
        response = client.post(
            "/api/google/disconnect",
            headers={"origin": "https://evil.example",
                     "content-type": "text/plain;charset=UTF-8"},
            content="")

        assert response.status_code == 403
        assert "another site" in response.json()["detail"]["title"].lower()

    def test_creating_a_project_from_another_site_is_refused(self, client):
        response = client.post("/api/projects/upload",
                               headers={"origin": "https://evil.example"},
                               files=[("file", ("x.txt", io.BytesIO(b"hi"),
                                                "text/plain"))])
        assert response.status_code == 403

    def test_the_app_s_own_origin_is_fine(self, client):
        response = client.post("/api/projects/text",
                               headers={"origin": "http://127.0.0.1:8100"},
                               json={"title": "n", "text": "第1話\n\n日本語の本文。"})
        assert response.status_code == 200

    def test_a_request_with_no_origin_is_fine(self, client):
        """Same-origin requests often send none, and the frontend is served
        same-origin from web/dist."""
        response = client.post("/api/projects/text",
                               json={"title": "n", "text": "第1話\n\n日本語の本文。"})
        assert response.status_code == 200

    def test_a_foreign_origin_may_still_READ(self, client):
        """Deliberate. The browser will not hand the body to the foreign page — no
        Access-Control-Allow-Origin is ever sent — so refusing reads adds nothing while
        risking breaking an ordinary local tool."""
        response = client.get("/api/health",
                              headers={"origin": "https://evil.example"})

        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


# ---- the allow-list itself -----------------------------------------------------

def test_the_allow_list_holds_only_local_names():
    """A routable hostname in here would defeat the whole guard."""
    routable = {h for h in app_mod.ALLOWED_HOSTS
                if "." in h and not h.startswith("127.") and h != "0.0.0.0"}
    # `testserver` is added by tests/conftest.py and is not a resolvable name.
    assert not routable, f"a routable name is in the allow-list: {sorted(routable)}"


def test_the_guard_can_actually_refuse():
    """Anti-vacuity: with the allow-list emptied, a local request is refused too — so
    the passes above mean "the host matched", not "the check does nothing"."""
    original = set(app_mod.ALLOWED_HOSTS)
    app_mod.ALLOWED_HOSTS.clear()
    try:
        with TestClient(app) as c:
            assert c.get("/api/health").status_code == 421
    finally:
        app_mod.ALLOWED_HOSTS.update(original)


# ---- uploads are bounded -------------------------------------------------------

def test_an_oversized_text_upload_is_refused(client, monkeypatch):
    """`read()` with no argument materialises the whole part before the size check can
    run, so a stray multi-gigabyte file is committed to RAM and only then rejected —
    with a paid job possibly in flight."""
    monkeypatch.setattr(app_mod, "MAX_SOURCE_BYTES", 32)

    response = client.post("/api/projects/upload",
                           files=[("file", ("big.txt", io.BytesIO(b"x" * 5000),
                                            "text/plain"))])

    assert response.status_code == 413


def test_an_oversized_image_is_refused(client, monkeypatch):
    from tests.test_images import jpeg

    # Below the synthetic JPEG's 53 bytes, or it is legitimately accepted.
    monkeypatch.setattr(app_mod, "MAX_IMAGE_BYTES", 32)
    pid = client.post("/api/projects/scan", json={"title": "m"}).json()["project"]["id"]

    body = client.post(f"/api/projects/{pid}/pages",
                       files=[("files", ("p.jpg", io.BytesIO(jpeg(800, 1200)),
                                         "image/jpeg"))]).json()

    assert body["added"] == []
    assert body["rejected"] and "larger than" in body["rejected"][0]["reason"]
