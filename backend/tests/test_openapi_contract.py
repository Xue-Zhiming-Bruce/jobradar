"""The committed contract must be what the app generates right now.

Nothing here writes to the working tree — the document is compared in memory —
so `git status --short` is still empty after the suite has run.
"""

import json
import socket
from pathlib import Path
from typing import get_args

from app.export_openapi import CONTRACT_PATH, render_contract
from app.main import app
from app.models import Company
from app.routes.matches import MATCHES_MAX_LIMIT, MatchState


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
    # The four refusals #3 shipped, plus the scan's two: no key configured, and a
    # call or an answer that failed. Every status this route answers, and no
    # other, so a new one cannot be added without a line here.
    assert set(upload["responses"]) == {"201", "413", "415", "422", "502", "503"}
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
        "/resume/{id}",
        "/resume/{id}/file",
        "/resume/parse",
        "/watchlist",
        "/watchlist/{id}",
        "/internal/tick",
        "/runs/{id}",
        "/matches",
        "/matches/{id}",
    ]


def test_contract_describes_the_resume_history_a_client_reads_and_changes() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    listed = contract["paths"]["/resume"]["get"]
    activated = contract["paths"]["/resume/{id}"]["patch"]
    removed = contract["paths"]["/resume/{id}"]["delete"]

    # One shape for a resume wherever a client meets one: the list's items are
    # the body the activation answers with, and that body is the row's five
    # public fields -- no text, and no path where the server put the file.
    array_schema = listed["responses"]["200"]["content"]["application/json"]["schema"]
    assert array_schema["type"] == "array"
    summary_ref = array_schema["items"]["$ref"]
    summary = schemas[summary_ref.rsplit("/", 1)[-1]]
    assert set(summary["properties"]) == {
        "id",
        "filename",
        "content_type",
        "uploaded_at",
        "is_active",
    }
    assert summary["required"] == list(summary["properties"])
    assert summary["properties"]["uploaded_at"]["format"] == "date-time"
    assert {"text", "stored_path"} & set(summary["properties"]) == set()
    assert activated["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ] == summary_ref
    # The upload keeps answering with #3's own body, which this task leaves alone.
    created = contract["paths"]["/resume"]["post"]["responses"]["201"]
    assert created["content"]["application/json"]["schema"]["$ref"].endswith("ResumeUploaded")

    # Every status each of the three routes answers, and a delete that answers
    # with no body at all.
    assert set(listed["responses"]) == {"200"}
    assert set(activated["responses"]) == {"200", "404", "422"}
    assert set(removed["responses"]) == {"204", "404", "422"}
    assert "content" not in removed["responses"]["204"]

    # The body an activation takes expresses one thing only: make this one
    # active. A deactivation is not in `_docs/plan.md`, so it is not expressible.
    assert activated["requestBody"]["required"] is True
    body_ref = activated["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body = schemas[body_ref.rsplit("/", 1)[-1]]
    only_true = {
        key: value
        for key, value in body["properties"]["is_active"].items()
        if key in {"const", "enum"}
    }
    assert only_true in ({"const": True}, {"enum": [True]})
    assert body["required"] == ["is_active"]
    assert body["additionalProperties"] is False

    # Both ids are path parameters and integers, so `abc` is refused by the path.
    for route in (activated, removed):
        (parameter,) = route["parameters"]
        assert parameter["in"] == "path"
        assert parameter["schema"]["type"] == "integer"


def test_contract_describes_the_stored_file_a_client_downloads() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    downloaded = contract["paths"]["/resume/{id}/file"]["get"]

    # The bytes are the body, so the `200` declares a binary media type: the
    # JSON every other answer in this family carries would be a lie here, and a
    # client that generated a parser from it would decode a resume as JSON.
    served = downloaded["responses"]["200"]["content"]
    assert set(served) == {"application/octet-stream"}
    assert served["application/octet-stream"]["schema"]["type"] == "string"

    # Every status this route answers, and no other: the file that was served,
    # the id that has no row (or whose file is not in the uploads directory),
    # and the path value that is not an id -- refused by the path's own
    # validation, which is why the parameter is an integer here.
    assert set(downloaded["responses"]) == {"200", "404", "422"}
    (parameter,) = downloaded["parameters"]
    assert parameter["name"] == "id"
    assert parameter["in"] == "path"
    assert parameter["schema"]["type"] == "integer"


def test_contract_describes_the_parse_body_a_client_reads() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    parsed = contract["paths"]["/resume/parse"]["post"]["responses"]["200"]

    # The parse's own answer is the one place a resume becomes fields, and the row
    # id beside the two fields the prompt asks for is the whole answer: a third
    # parsed field would have no consumer and fails here.
    parsed_ref = parsed["content"]["application/json"]["schema"]["$ref"]
    assert parsed_ref.endswith("ResumeParsed")
    schema = contract["components"]["schemas"][parsed_ref.rsplit("/", 1)[-1]]
    assert set(schema["properties"]) == {"id", "name", "skills"}
    assert schema["required"] == list(schema["properties"])


def test_contract_describes_the_watchlist_routes() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    listed = contract["paths"]["/watchlist"]["get"]
    created = contract["paths"]["/watchlist"]["post"]
    updated = contract["paths"]["/watchlist/{id}"]["patch"]
    removed = contract["paths"]["/watchlist/{id}"]["delete"]

    array_schema = listed["responses"]["200"]["content"]["application/json"]["schema"]
    assert array_schema["type"] == "array"
    company_ref = array_schema["items"]["$ref"]
    company = schemas[company_ref.rsplit("/", 1)[-1]]
    assert set(company["properties"]) == {"id", "name", "slug", "is_active", "source"}
    # The new field is additive: a client that reads the four it knows is not
    # broken, so `source` carries a default and is not a required property.
    assert "source" not in company.get("required", [])

    # The same company body `POST` answers with, and the refusals it answers instead.
    assert created["requestBody"]["required"] is True
    body_ref = created["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert schemas[body_ref.rsplit("/", 1)[-1]]["required"] == ["name", "slug"]
    assert set(created["responses"]) == {"201", "404", "409", "422", "502"}
    assert "source" in created["responses"]["409"]["description"]  # the pair, not the slug
    assert "slug" in created["responses"]["409"]["description"]
    assert created["responses"]["201"]["content"]["application/json"]["schema"]["$ref"] == (
        company_ref
    )

    # The update joins the existing path -- no eleventh path -- and answers with
    # the same company body the list and the addition do.
    assert set(contract["paths"]["/watchlist/{id}"]) == {"delete", "patch"}
    assert set(updated["responses"]) == {"200", "404", "422"}
    assert updated["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ] == company_ref
    # The three fields it may carry, all optional, and nothing else: `slug` is
    # refused as an unknown field, and a body naming `source` reaches the route,
    # which answers the rule in words. The rendered schema carries no `required`
    # key at all.
    assert updated["requestBody"]["required"] is True
    update_ref = updated["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    edit = schemas[update_ref.rsplit("/", 1)[-1]]
    assert set(edit["properties"]) == {"name", "is_active", "source"}
    assert edit["additionalProperties"] is False
    assert not edit.get("required")
    # The label is bounded by the column it lands in -- 255, the length of
    # `company.name` -- so a longer one is the `422` here and not the `500` the
    # driver answers on PostgreSQL.
    name_max_length = Company.__table__.c.name.type.length
    assert edit["properties"]["name"]["anyOf"][0]["maxLength"] == name_max_length

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
    # body the read answers with, and that body is the row's nine columns.
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
        "matches_emailed",
        "companies_not_fetched",
    }
    assert run["required"] == list(run["properties"])
    # The two fields that may be null by the wire's own reckoning: `0` is a run
    # that emailed nothing and NULL is a run that never recorded a count (#36),
    # and NULL is also every run whose board-call budget stopped no walk (#56).
    assert run["properties"]["matches_emailed"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]
    assert run["properties"]["companies_not_fetched"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]

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
    # body the update answers with, and that body is the row's own ten fields.
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
        "outcome",
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

    # The four filters the read takes, these names and no others, each of them an
    # optional query parameter whose bounds and vocabulary are the route's own --
    # so a fifth parameter, a renamed one, a required one or a widened ceiling
    # fails here. The 200 body above is the one both routes answer with, unchanged;
    # the `422` is the framework's own refusal of a value these parameters cannot
    # take, and it is in the contract because these parameters are.
    parameters = listed["parameters"]
    assert [parameter["name"] for parameter in parameters] == [
        "min_score",
        "state",
        "company_id",
        "limit",
    ]
    assert all(parameter["in"] == "query" for parameter in parameters)
    assert all(parameter["required"] is False for parameter in parameters)
    assert {parameter["name"]: parameter["schema"]["anyOf"] for parameter in parameters} == {
        "min_score": [
            {"type": "integer", "minimum": 0, "maximum": 100},
            {"type": "null"},
        ],
        "state": [{"enum": list(get_args(MatchState)), "type": "string"}, {"type": "null"}],
        "company_id": [{"type": "integer"}, {"type": "null"}],
        "limit": [
            {"type": "integer", "minimum": 1, "maximum": MATCHES_MAX_LIMIT},
            {"type": "null"},
        ],
    }

    # The only values a verdict may carry, and every status each route answers.
    assert set(listed["responses"]) == {"200", "422"}
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
