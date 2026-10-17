"""The built UI is mounted at `/`, and the API is still reachable underneath it.

`#13` serves the frontend's build from the same app object as the API. A mount at
`/` matches every path, so where it is registered decides whether `/healthz` and
the routes answer at all -- which is why the mount comes after the
`include_router` calls in `app.main`, and why a wrong order is a suite failure
rather than something only a containerised build would notice.

No build is needed here: `STATIC_DIR` points the app at a scratch directory
holding an `index.html` and one hashed asset, and the module is reloaded because
the decision to mount is taken when it is imported -- the same way a real import
takes it. The paths checked are the committed contract's own, read from
`backend/openapi.json`, so a route the mount swallowed cannot go unnoticed.

`#40` adds two claims about that mount: the hashed asset answers a long-lived
`cache-control` and the page does not, and a path in neither the contract nor the
build is a `404` from the mount rather than an unhandled error. Both are asserted
here, against a path that is not in the contract, because a future mount that
falls back to `index.html` for every unknown path (what client-side routing
would need) is exactly what the second one would catch.
"""

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main

BACKEND_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_JSON = BACKEND_ROOT / "openapi.json"

# The hashed asset the scratch build references, in the shape Vite emits.
ASSET_NAME = "index-B6OjL1iC.js"
INDEX_HTML = (
    "<!doctype html><html><body><div id=\"root\"></div>"
    f"<script type=\"module\" src=\"/assets/{ASSET_NAME}\"></script></body></html>"
)

# One request per path in the committed contract, with a method that path serves
# and the answer only its own route gives. The mount answers an unknown path with
# `{"detail":"Not Found"}`, so a body the route wrote is what proves the routers
# were matched first; `None` means the status and the JSON content type are the
# whole claim (`POST /resume` with no file part is the route's own `422`, and a
# tick with no secret its own `401`).
_CONTRACT_REQUESTS = [
    ("GET", "/healthz", None, 200, {"status": "ok"}),
    ("POST", "/resume", None, 422, None),
    ("PATCH", "/resume/1", {"is_active": True}, 404, {"detail": "There is no resume with id 1"}),
    ("GET", "/resume/1/file", None, 404, {"detail": "There is no resume with id 1"}),
    ("POST", "/resume/parse", None, 404, {"detail": "No active resume: upload one before parsing it"}),
    ("GET", "/watchlist", None, 200, []),
    ("DELETE", "/watchlist/1", None, 404, {"detail": "There is no watched company with id 1"}),
    ("POST", "/internal/tick", None, 401, None),
    ("POST", "/internal/rematch", None, 401, None),
    ("GET", "/runs", None, 200, []),
    ("GET", "/runs/1", None, 404, {"detail": "There is no run with id 1"}),
    ("GET", "/matches", None, 200, []),
    ("PATCH", "/matches/1", {"state": "saved"}, 404, {"detail": "There is no match with id 1"}),
    ("GET", "/telemetry/metrics", None, 200, []),
    ("GET", "/telemetry/traces", None, 200, []),
]


def _built_ui(directory: Path) -> None:
    """A scratch build: an index carrying one `/assets/...` reference, and that file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    assets = directory / "assets"
    assets.mkdir()
    (assets / ASSET_NAME).write_text("export {}\n", encoding="utf-8")


def _app_with_static_dir(monkeypatch: pytest.MonkeyPatch, static_dir: Path, tmp_path: Path):
    """Reload `app.main` with `STATIC_DIR` set, and return the app it built.

    The mount is registered -- or not -- while the module is imported, so a test
    that wants the served case has to import it again with the variable set.
    `DATABASE_URL` is pinned at a scratch SQLite file and `TICK_SECRET` removed:
    the routes below read the database, and none of them is the thing under test.
    """
    monkeypatch.setenv(main.STATIC_DIR_VARIABLE, str(static_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.delenv("TICK_SECRET", raising=False)
    return importlib.reload(main).app


def test_the_static_root_answers_the_page_it_holds(monkeypatch, tmp_path: Path) -> None:
    _built_ui(tmp_path / "static")
    app = _app_with_static_dir(monkeypatch, tmp_path / "static", tmp_path)

    with TestClient(app) as client:
        page = client.get("/")
        asset = client.get(f"/assets/{ASSET_NAME}")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert f"/assets/{ASSET_NAME}" in page.text, "the page did not come from the mounted build"
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]


def test_healthz_answers_json_and_is_not_shadowed_by_the_mount(monkeypatch, tmp_path: Path) -> None:
    _built_ui(tmp_path / "static")
    app = _app_with_static_dir(monkeypatch, tmp_path / "static", tmp_path)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_hashed_assets_are_cached_forever_and_the_page_is_not(monkeypatch, tmp_path: Path) -> None:
    """A hashed asset can be kept for a year; `index.html` names the hashes, so it may not."""
    _built_ui(tmp_path / "static")
    app = _app_with_static_dir(monkeypatch, tmp_path / "static", tmp_path)

    with TestClient(app) as client:
        asset = client.get(f"/assets/{ASSET_NAME}")
        page = client.get("/")

    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    # `no-cache` still stores the page, it revalidates it: a header that says
    # "keep this for as long as the asset" is the failure this catches.
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["cache-control"] != asset.headers["cache-control"]


def test_an_unknown_path_is_the_mount_404_and_the_api_still_answers(monkeypatch, tmp_path: Path) -> None:
    """Nothing is built at `/no/such/path`, so the mount answers, and `/healthz` is untouched.

    There is no client-side router in the UI, so a deep link has no `index.html`
    to fall back to: the mount's own `404` is the answer until one exists.
    """
    _built_ui(tmp_path / "static")
    app = _app_with_static_dir(monkeypatch, tmp_path / "static", tmp_path)

    with TestClient(app) as client:
        unknown = client.get("/no/such/path")
        health = client.get("/healthz")

    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/json")
    assert unknown.json() == {"detail": "Not Found"}
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

@pytest.mark.parametrize(("method", "path", "payload", "status", "body"), _CONTRACT_REQUESTS)
def test_every_contract_path_is_answered_by_its_own_route(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    status: int,
    body: dict[str, object] | list[object] | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every path the contract names is answered by the app, not by the mount.

    The mount answers an unknown path with the same `404` an unmatched API path
    gets, so the status and the body together are the claim: the route's own
    answer, which the mount cannot produce.
    """
    _built_ui(tmp_path / "static")
    app = _app_with_static_dir(monkeypatch, tmp_path / "static", tmp_path)

    with TestClient(app) as client:
        response = client.request(method, path, json=payload)

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    if body is None:
        assert response.json() != {"detail": "Not Found"}, f"{path} was answered by the mount"
    else:
        assert response.json() == body, f"{path} was answered by the mount: {response.text}"


def test_the_contract_paths_are_the_ones_the_contract_names() -> None:
    """The requests above are the committed contract's paths, not a copy that drifted."""
    document = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    requested = {path.replace("/1", "/{id}") for _, path, _, _, _ in _CONTRACT_REQUESTS}

    assert requested == set(document["paths"])


def test_without_a_build_no_mount_is_registered_and_the_api_still_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`uv run uvicorn` and the suite run on a clone with no `frontend/dist`."""
    app = _app_with_static_dir(monkeypatch, tmp_path / "nothing-built-here", tmp_path)

    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/healthz")

    assert root.status_code == 404
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
