"""The OTLP export of #101: opt-in, beside the write, and carrying nothing extra.

`OTEL_EXPORTER_OTLP_ENDPOINT` set to an endpoint builds one exporter that posts a
span's OTLP/HTTP JSON body; unset or empty builds none, so a deployment that does
not configure one makes no outbound call and behaves exactly as #90 left it. The
export rides beside `telemetry.store_span`, behind the response, and a collector
that is unreachable changes neither the response nor the stored row.

Every case here is hermetic: the exporter's HTTP client is a `httpx2.MockTransport`
that records the request and answers without a socket, so the default suite opens
no connection and `pyproject.toml` gains no dependency. The four things measured
are the four criteria: the exporter exists only when the variable is set; the
payload holds the row's facts and nothing a request or a resume carries; a failing
collector leaves the response and the row as they were; and the app wires the
exporter once, at import.
"""

import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import db, telemetry
from app.main import app
from app.models import TelemetrySpan
from app.routes.internal import TICK_SECRET_HEADER

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# The endpoint the fake collector is said to live at: never dialed, because the
# client below has a `MockTransport` and opens no socket.
ENDPOINT = "http://collector.invalid:4318"

# The trace id a caller sends and the app adopts, so an exported span's trace id
# is this value and not a generated one.
OFFERED_ID = "otel-trace-the-caller-sent.6c1f"

# Values that could not appear in an export by accident: what these cases assert
# is that they reach the payload from none of the places they live.
SECRET = "otel-tick-secret-3f8d"
QUERY_SENTINEL = "sentinel-query-7b2c"
BEARER_SENTINEL = "Bearer sentinel-token-4e9a"
RESUME_SENTINEL = "SENTINEL-RESUME-LINE: this text must never leave over OTLP."
RESUME_FILENAME = "sentinel-otlp-resume-5a1b.txt"


@pytest.fixture(autouse=True)
def no_leftover_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every case starts with the endpoint unset, and leaves no exporter behind."""
    monkeypatch.delenv(telemetry.OTLP_ENDPOINT_VARIABLE, raising=False)
    yield
    telemetry._exporter = None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with a temporary database, a temporary uploads directory and a secret."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("TICK_SECRET", SECRET)
    with TestClient(app) as test_client:
        yield test_client


def recording_client(status_code: int = 200) -> tuple[httpx2.Client, list[httpx2.Request]]:
    """A client that answers `status_code` and records every request, over no socket."""
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status_code)

    return httpx2.Client(transport=httpx2.MockTransport(handle)), requests


def unreachable_client() -> httpx2.Client:
    """A client whose collector cannot be reached, the way a down one fails."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("the collector is unreachable", request=request)

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def an_exporter(client: httpx2.Client) -> telemetry.OtlpExporter:
    """An exporter whose client is the fake above, so nothing leaves the process."""
    return telemetry.OtlpExporter(ENDPOINT, client=client)


def spans_in(body: dict[str, object]) -> list[dict[str, object]]:
    """Every span in one OTLP body, reaching through the two wrapper lists."""
    return [
        span
        for resource_span in body["resourceSpans"]
        for scope_span in resource_span["scopeSpans"]
        for span in scope_span["spans"]
    ]


def exported_spans(requests: list[httpx2.Request]) -> list[dict[str, object]]:
    """Every span in every OTLP body the fake collector was posted."""
    return [span for request in requests for span in spans_in(json.loads(request.content))]


def attributes_of(span: dict[str, object]) -> dict[str, dict[str, object]]:
    """The span's attributes as key -> value object."""
    attributes = span["attributes"]
    assert isinstance(attributes, list)
    return {attribute["key"]: attribute["value"] for attribute in attributes}


def stored_spans() -> list[dict[str, object]]:
    """Every stored span as one dictionary per row, read while its session is open."""
    with db.session_scope() as session:
        columns = [column.name for column in TelemetrySpan.__table__.columns]
        return [
            {name: getattr(span, name) for name in columns}
            for span in session.scalars(select(TelemetrySpan).order_by(TelemetrySpan.id))
        ]


def forget_every_span() -> None:
    """Empty `telemetry_span`, so a case starts from a table with nothing in it."""
    with db.session_scope() as session:
        session.execute(delete(TelemetrySpan))


def a_writer_that_fails(_: object) -> None:
    """A `store_span` that fails the way a database that is down fails."""
    raise RuntimeError("the database is gone")


def an_http_span(**overrides: object) -> telemetry.Span:
    """One plausible HTTP span a case overrides a fact at a time."""
    fields: dict[str, object] = {
        "trace_id": OFFERED_ID,
        "name": telemetry.HTTP_SPAN_NAME,
        "route": "/runs/{id}",
        "method": "GET",
        "status_code": 200,
        "duration_ms": 42,
        "started_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return telemetry.Span(**fields)  # type: ignore[arg-type]


# AC1: the exporter exists only when the endpoint is set, and nowhere else.


@pytest.mark.parametrize("value", ["", "   "])
def test_ac1_an_unset_or_empty_endpoint_builds_no_exporter(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """AC1: empty is the unset case, and no exporter -- so no client -- is built."""
    monkeypatch.setenv(telemetry.OTLP_ENDPOINT_VARIABLE, value)
    built: list[tuple[object, ...]] = []
    monkeypatch.setattr(telemetry, "OtlpExporter", lambda *arguments: built.append(arguments))

    assert telemetry.otlp_endpoint() == ""
    assert telemetry.configure_export() is None
    assert telemetry._exporter is None
    assert built == []


def test_ac1_an_unset_variable_exports_nothing_and_calls_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: with the variable unset, `record_span`'s export half is a no-op."""
    monkeypatch.delenv(telemetry.OTLP_ENDPOINT_VARIABLE, raising=False)
    recorder, requests = recording_client()
    monkeypatch.setattr(telemetry.httpx2, "Client", lambda *arguments, **keywords: recorder)
    assert telemetry.configure_export() is None

    telemetry.export_span(an_http_span())

    assert requests == []


def test_ac1_a_configured_endpoint_posts_to_the_traces_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: one exporter, whose URL is the variable with the OTLP traces path appended."""
    monkeypatch.setenv(telemetry.OTLP_ENDPOINT_VARIABLE, ENDPOINT)
    recorder, requests = recording_client()
    monkeypatch.setattr(telemetry.httpx2, "Client", lambda *arguments, **keywords: recorder)

    exporter = telemetry.configure_export()

    assert exporter is telemetry._exporter
    assert exporter.url == ENDPOINT + "/v1/traces"

    telemetry.export_span(an_http_span())

    assert [(request.method, str(request.url)) for request in requests] == [
        ("POST", ENDPOINT + "/v1/traces")
    ]


# AC2: the payload holds the six facts, and nothing a request or a resume carries.


def test_ac2_the_payload_pins_the_spans_facts_and_nothing_else() -> None:
    """AC2: the exact field names, the attribute keys and a duration equal to the row's."""
    span = an_http_span()

    body = telemetry.otlp_payload(span)

    (otlp_span,) = spans_in(body)
    assert set(body) == {"resourceSpans"}
    assert set(otlp_span) == {
        "traceId",
        "spanId",
        "name",
        "startTimeUnixNano",
        "endTimeUnixNano",
        "attributes",
    }
    assert otlp_span["traceId"] == OFFERED_ID
    assert otlp_span["name"] == telemetry.HTTP_SPAN_NAME
    assert len(str(otlp_span["spanId"])) == 16
    assert attributes_of(otlp_span) == {
        "http.route": {"stringValue": "/runs/{id}"},
        "http.request.method": {"stringValue": "GET"},
        "http.response.status_code": {"intValue": "200"},
    }
    # OTLP carries a duration as end minus start, and that difference is the row's
    # whole milliseconds and nothing else.
    duration_nanoseconds = int(otlp_span["endTimeUnixNano"]) - int(otlp_span["startTimeUnixNano"])
    assert duration_nanoseconds // 1_000_000 == span.duration_ms


def test_ac2_the_runs_span_carries_no_route_method_or_status() -> None:
    """AC2: the three NULL facts have no attribute, so a run's span carries only its own."""
    span = telemetry.Span(
        trace_id=OFFERED_ID,
        name=telemetry.RUN_SPAN_NAME,
        route=None,
        method=None,
        status_code=None,
        duration_ms=7,
        started_at=datetime.now(UTC),
    )

    (otlp_span,) = spans_in(telemetry.otlp_payload(span))

    assert otlp_span["name"] == telemetry.RUN_SPAN_NAME
    assert otlp_span["attributes"] == []


def test_ac2_no_query_string_header_value_or_resume_reaches_the_export(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: the journey's own sentinels reach neither the body, the route nor an attribute."""
    recorder, requests = recording_client()
    monkeypatch.setattr(telemetry, "_exporter", an_exporter(recorder))

    uploaded = client.post(
        "/resume",
        files={"file": (RESUME_FILENAME, RESUME_SENTINEL.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    response = client.get(
        f"/matches?limit=5&state={QUERY_SENTINEL}", headers={"Authorization": BEARER_SENTINEL}
    )
    assert response.status_code in {200, 422}, response.text

    bodies = [request.content.decode() for request in requests]
    assert bodies, "the journey must have exported something, or the case proves nothing"
    for value in (RESUME_SENTINEL, RESUME_FILENAME, SECRET, QUERY_SENTINEL, BEARER_SENTINEL):
        assert all(value not in body for body in bodies), value

    # The list request's span is the matched template and never the path + query.
    routes = [
        attribute["value"]
        for span in exported_spans(requests)
        for attribute in span["attributes"]  # type: ignore[union-attr]
        if attribute["key"] == "http.route"
    ]
    assert {"stringValue": "/matches"} in routes
    assert all("/matches?" not in str(route) for route in routes)


# AC3: the export rides beside the write, and a failing collector changes nothing.


def test_ac3_an_unreachable_collector_leaves_the_response_and_the_stored_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: the request answers what it would have, and the span is still a row."""
    monkeypatch.setattr(telemetry, "_exporter", an_exporter(unreachable_client()))
    forget_every_span()

    response = client.get("/healthz", headers={telemetry.REQUEST_ID_HEADER: OFFERED_ID})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    (span,) = stored_spans()
    assert (span["trace_id"], span["route"], span["status_code"]) == (
        OFFERED_ID,
        "/healthz",
        200,
    )


def test_ac3_a_collector_answering_an_error_still_leaves_the_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: a non-2xx answer is not an exception, and the write already happened."""
    recorder, requests = recording_client(status_code=500)
    monkeypatch.setattr(telemetry, "_exporter", an_exporter(recorder))
    forget_every_span()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert len(requests) == 1
    assert [span["route"] for span in stored_spans()] == ["/healthz"]


def test_ac3_the_export_rides_beside_the_write_and_survives_a_failed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: export is attempted whatever the database write did, and never raises."""
    recorder, requests = recording_client()
    monkeypatch.setattr(telemetry, "_exporter", an_exporter(recorder))
    monkeypatch.setattr(telemetry, "store_span", a_writer_that_fails)

    telemetry.record_span(an_http_span())

    assert len(requests) == 1


# AC4: hermetic, and the export adds no dependency or route.


def test_ac4_the_export_adds_no_runtime_dependency() -> None:
    """AC4: the hand-rolled POST needs no package, so `pyproject.toml` is unchanged."""
    pyproject = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["dependencies"] + pyproject["dependency-groups"]["dev"]
    packages = [
        re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower() for requirement in declared
    ]

    assert [package for package in packages if "otel" in package or "otlp" in package] == []


def test_ac4_the_app_wires_the_exporter_once_at_import(tmp_path: Path) -> None:
    """AC4: `app.main` builds the exporter exactly once, the way it installs the log format."""
    script = (
        "import sys;"
        f"sys.path.insert(0, {str(BACKEND_ROOT)!r});"
        "from app import telemetry;"
        "calls = [];"
        "telemetry.configure_export = lambda: calls.append(1);"
        "import app.main;"
        "print(len(calls))"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "1"


def test_ac4_a_tick_still_needs_the_secret_with_an_exporter_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the export changes no route's behaviour, the tick's `401` included."""
    recorder, requests = recording_client()
    monkeypatch.setattr(telemetry, "_exporter", an_exporter(recorder))

    assert client.post("/internal/tick").status_code == 401
    assert client.post("/internal/tick", headers={TICK_SECRET_HEADER: "wrong"}).status_code == 401

    assert [span["route"] for span in stored_spans()] == ["/internal/tick", "/internal/tick"]
    assert len(requests) == 2
