"""Flask serves the built SPA, because the deployment target cannot run Vite.

That box has Node 10; Vite 6 needs 18+, so `npm run dev` dies on `import {`
before it starts and `preview`/`build` are out too. Shipping the built `dist/`
and serving it here means the server needs no Node at all — and makes the app
same-origin, so the `/api` proxy that only ever existed in `server.proxy`
(dev-server only, absent from `preview`) stops being load-bearing.
"""
import pytest

import server


@pytest.fixture()
def dist(tmp_path, monkeypatch):
    """A minimal build tree."""
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(
        '<!doctype html><html><body><script src="/assets/app.js"></script></body></html>')
    (d / "assets" / "app.js").write_text("console.log('hi')")
    monkeypatch.setattr(server, "_FRONTEND_DIST", d)
    return d


@pytest.fixture()
def client():
    return server.app.test_client()


def test_root_serves_the_app(dist, client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<!doctype html" in r.get_data().lower()


def test_assets_are_served(dist, client):
    r = client.get("/assets/app.js")
    assert r.status_code == 200
    assert r.get_data() == b"console.log('hi')"


def test_api_routes_are_not_shadowed(dist, client):
    """The catch-all is registered last and rejects `api/` explicitly. A
    typo'd API path must 404, not quietly return the SPA — which would read
    as a broken page rather than a wrong URL."""
    assert client.get("/api/definitely-not-a-route").status_code == 404


def test_unknown_path_falls_back_to_the_app(dist, client):
    r = client.get("/some/unknown/path")
    assert r.status_code == 200
    assert b"<!doctype html" in r.get_data().lower()


@pytest.mark.parametrize("attempt", [
    "/../../etc/passwd",
    "/assets/../../../../etc/passwd",
    "/..%2f..%2fetc%2fpasswd",
    "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
])
def test_no_path_traversal(dist, client, attempt):
    """`Path(root) / '../x'` resolves outside the build and `.is_file()` says
    yes, so containment is checked on the RESOLVED path rather than trusting
    that `..` was normalised out upstream."""
    body = client.get(attempt).get_data()
    assert b"root:" not in body and b"/bin/" not in body


def test_a_file_outside_the_build_is_not_served(dist, client, tmp_path):
    """The sibling of the build directory is still outside it."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-serve")

    assert b"do-not-serve" not in client.get("/../secret.txt").get_data()
    assert server._frontend_file("../secret.txt") is None


def test_a_missing_build_explains_itself(tmp_path, monkeypatch, client):
    """The usual cause is a deploy that shipped source without running the
    build; a bare 404 would send you looking in the wrong place."""
    monkeypatch.setattr(server, "_FRONTEND_DIST", tmp_path / "nope")
    r = client.get("/")
    assert r.status_code == 404
    assert b"npm run build" in r.get_data()


def test_api_still_works_without_a_build(tmp_path, monkeypatch, client):
    """No frontend must not mean no backend — the API is the half that
    can't be rebuilt elsewhere."""
    monkeypatch.setattr(server, "_FRONTEND_DIST", tmp_path / "nope")
    assert client.get("/api/cases").status_code == 200
