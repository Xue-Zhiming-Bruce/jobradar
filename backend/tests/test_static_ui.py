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
    `DATABASE_URL` is pinned at a scratch SQLite file: the routes below read the
    database, and this test is about the mount, not about an engine.
    """
    monkeypatch.setenv(main.STATIC_DIR_VARIABLE, str(static_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    return importlib.reload(main).app


def _contract_paths() -> list[str]:
    """Every path in the committed contract, with its path parameters filled in."""
    document = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    return [path.replace("{id}", "1") for path in document["paths"]]


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


@pytest.mark.parametrize("path", _contract_paths())
def test_no_route_in_the_committed_contract_answers_html(
    path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every path the contract names is answered by the app, not by the mount.

    The static mount answers an unknown path with `text/plain`, so a JSON answer
    -- a `200`, a `404` or a `405` -- is what proves the API was matched first.
    """
    _built_ui(tmp_path / "static")
    app = _app_with_static_dir(monkeypatch, tmp_path / "static", tmp_path)

    with TestClient(app) as client:
        response = client.get(path)

    assert response.headers["content-type"].startswith("application/json"), (
        f"{path} was answered by the mount: {response.headers.get('content-type')}"
    )


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
