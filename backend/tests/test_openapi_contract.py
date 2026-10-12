"""The committed contract must be what the app generates right now.

Nothing here writes to the working tree — the document is compared in memory —
so `git status --short` is still empty after the suite has run.
"""

import json
import socket
from pathlib import Path

from app.export_openapi import CONTRACT_PATH, render_contract
from app.main import app


def test_committed_contract_matches_the_app() -> None:
    """Hand-editing openapi.json — a 200 turned into a 201, say — fails here."""
    committed = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert committed == app.openapi()


def test_committed_contract_is_the_rendered_contract_byte_for_byte() -> None:
    assert CONTRACT_PATH.read_bytes() == render_contract(app).encode("utf-8")


def test_contract_describes_the_health_check_and_the_resume_upload() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert {"openapi", "info", "paths"} <= set(contract)

    ok_response = contract["paths"]["/healthz"]["get"]["responses"]["200"]
    assert ok_response["content"]["application/json"]["schema"]

    upload = contract["paths"]["/resume"]["post"]
    assert set(upload["responses"]) == {"201", "413", "415", "422"}
    body_schema = upload["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert upload["requestBody"]["required"] is True
    body = contract["components"]["schemas"][body_schema["$ref"].rsplit("/", 1)[-1]]
    assert body["required"] == ["file"]
    # A file part, spelled the way this stack spells one in OpenAPI 3.1.
    assert body["properties"]["file"]["type"] == "string"
    assert body["properties"]["file"]["contentMediaType"] == "application/octet-stream"
    created = upload["responses"]["201"]["content"]["application/json"]["schema"]
    assert created["$ref"].endswith("ResumeUploaded")


def test_contract_lists_every_registered_path_in_registration_order() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert list(contract["paths"]) == [
        "/healthz",
        "/resume",
        "/watchlist",
        "/watchlist/{id}",
        "/internal/tick",
        "/runs/{id}",
        "/matches",
        "/matches/{id}",
    ]


def test_contract_describes_the_watchlist_routes() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    listed = contract["paths"]["/watchlist"]["get"]
    created = contract["paths"]["/watchlist"]["post"]
    removed = contract["paths"]["/watchlist/{id}"]["delete"]

    array_schema = listed["responses"]["200"]["content"]["application/json"]["schema"]
    assert array_schema["type"] == "array"
    company_ref = array_schema["items"]["$ref"]
    company = schemas[company_ref.rsplit("/", 1)[-1]]
    assert set(company["properties"]) == {"id", "name", "slug", "is_active"}

    # The same company body `POST` answers with, and the refusals it answers instead.
    assert created["requestBody"]["required"] is True
    body_ref = created["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert schemas[body_ref.rsplit("/", 1)[-1]]["required"] == ["name", "slug"]
    assert set(created["responses"]) == {"201", "404", "409", "422", "502"}
    assert created["responses"]["201"]["content"]["application/json"]["schema"]["$ref"] == (
        company_ref
    )

    # The id is a path parameter and an integer, and the delete answers with no body.
    assert set(removed["responses"]) == {"204", "404", "422"}
    assert "content" not in removed["responses"]["204"]
    (parameter,) = removed["parameters"]
    assert parameter["in"] == "path"
    assert parameter["schema"]["type"] == "integer"


def test_contract_describes_the_tick_and_the_run_it_started() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    tick = contract["paths"]["/internal/tick"]["post"]
    read = contract["paths"]["/runs/{id}"]["get"]

    # One shape for a run wherever a client meets one: the `202` answers with the
    # body the read answers with, and that body is the row's seven columns.
    started = tick["responses"]["202"]["content"]["application/json"]["schema"]
    summary = read["responses"]["200"]["content"]["application/json"]["schema"]
    assert started["$ref"] == summary["$ref"]
    run = schemas[started["$ref"].rsplit("/", 1)[-1]]
    assert set(run["properties"]) == {
        "id",
        "status",
        "started_at",
        "finished_at",
        "error",
        "postings_seen",
        "matches_created",
    }
    assert run["required"] == list(run["properties"])

    # Every status each route answers, and the tick's refusals are answers, not
    # omissions: a client reads them here instead of trying them out.
    assert set(tick["responses"]) == {"202", "401", "409", "500"}
    assert set(read["responses"]) == {"200", "404", "422"}

    # The run's id is a path parameter and an integer, and the tick carries no
    # parameter at all: what it must send is checked at the door, not by the
    # request's own validation.
    (parameter,) = read["parameters"]
    assert parameter["in"] == "path"
    assert parameter["schema"]["type"] == "integer"
    assert "parameters" not in tick


def test_contract_describes_the_matches_a_client_reads_and_updates() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    listed = contract["paths"]["/matches"]["get"]
    updated = contract["paths"]["/matches/{id}"]["patch"]

    # One shape for a match wherever a client meets one: the list's items are the
    # body the update answers with, and that body is the row's own nine fields.
    array_schema = listed["responses"]["200"]["content"]["application/json"]["schema"]
    assert array_schema["type"] == "array"
    match_ref = array_schema["items"]["$ref"]
    summary = schemas[match_ref.rsplit("/", 1)[-1]]
    assert set(summary["properties"]) == {
        "id",
        "company_name",
        "title",
        "url",
        "score",
        "reason",
        "gaps",
        "state",
        "created_at",
    }
    assert summary["required"] == list(summary["properties"])
    # The stored JSON text is decoded before it is served, so the field is a list
    # of strings on the wire rather than the column's own `{"type": "string"}`.
    assert summary["properties"]["gaps"]["type"] == "array"
    assert summary["properties"]["gaps"]["items"] == {"type": "string"}
    assert updated["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ] == match_ref

    # The only values a verdict may carry, and every status the update answers.
    assert set(listed["responses"]) == {"200"}
    assert set(updated["responses"]) == {"200", "404", "422"}
    assert updated["requestBody"]["required"] is True
    body_ref = updated["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body = schemas[body_ref.rsplit("/", 1)[-1]]
    assert body["properties"]["state"]["enum"] == ["saved", "dismissed"]
    assert body["required"] == ["state"]

    # The id is a path parameter and an integer, so `abc` is refused by the path.
    (parameter,) = updated["parameters"]
    assert parameter["in"] == "path"
    assert parameter["schema"]["type"] == "integer"


def test_contract_carries_no_environment_specific_values() -> None:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    contract = json.loads(text)

    assert "servers" not in contract
    assert str(Path.home()) not in text
    assert "/Users/" not in text
    assert socket.gethostname() not in text

    lowered = text.lower()
    for secret_like in (
        "secret",
        "token",
        "api_key",
        "apikey",
        "password",
        "database_url",
        "uploads_dir",
    ):
        assert secret_like not in lowered
