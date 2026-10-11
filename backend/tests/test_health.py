"""Tests for `GET /healthz`.

The suite never starts a server: `TestClient` drives the ASGI app object
in-process, so these tests need no port, no network and no configuration.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_200_json_object_without_request_headers() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}
