"""The digest journey's trace, its spans, its log lines and what must never reach them.

Every test drives the real app through `TestClient` -- no server, no port, no board,
no model, no key -- with `DATABASE_URL` pointed at a temporary SQLite file, so the
suite leaves no `backend/data/jobradar.db` behind and `git status --short` stays
empty. The two seams `app.routes.internal` declares are replaced through
`app.dependency_overrides`, exactly as `tests/test_internal_tick.py` replaces them:
the run's work is a fake that records what it was handed, and the starter is
replaced only where the test is about the response rather than about the thread.

Three things are measured here and nothing else. The trace id: it is adopted from
`X-Request-ID` when it may be, generated when it may not, carried back on every
status, and shared by the tick's request, the run it starts and every log line
either of them writes. The span: one row per HTTP request whatever its outcome, one
for the run, seven facts and no eighth, and a route template that is never a raw
path. And the two reads: the metrics window and the newest-first traces, which are
a contract for #91, #92 and #93.

The last group is the one that must never be skipped: no resume text, no resume file
name, no `Authorization` value, no `X-Tick-Secret`, no `DATABASE_URL` and no request
body reaches a span, a log line or a read body, and telemetry never fails the request
it observes. Those cases upload synthetic text only; the repository is public and
holds no real resume.
"""

import json
import logging
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from app import db, telemetry
from app.main import app
from app.models import Run, TelemetrySpan
from app.routes import internal
from app.routes.internal import TICK_SECRET_HEADER, get_pipeline, get_run_starter
from app.routes.matches import MATCHES_MAX_LIMIT

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# A value that could not appear in a span, a read body or a log line by accident:
# what these tests assert is that it reaches none of them from where it lives.
SECRET = "tick-secret-sentinel-7f4c1d9ab38e"

# The text and the file name every upload case uses: synthetic, and distinctive
# enough that its appearance anywhere outside the resume row is the failure.
RESUME_SENTINEL = "SENTINEL-RESUME-LINE: this text must stay in the database."
RESUME_FILENAME = "sentinel-resume-9f3c.txt"

# The id a caller sends and this app adopts: characters the pattern accepts.
OFFERED_ID = "trace-id-the-caller-sent.9f3c"

# The ceiling on a wait that should not happen, so a broken run fails this suite
# instead of hanging it. Nothing sleeps for it; the events do the signalling.
THREAD_TIMEOUT_SECONDS = 5.0

# The pattern-matching modulo: a generated id and a refused one both have to be
# something this module accepts and a caller-chosen value is not.
GENERATED_ID_LENGTH = 32

# An id a caller may not have adopted: empty, only spaces, too long, and four
# values holding a character the pattern does not accept. None of them may be
# echoed, and none may reach the column.
_REFUSED_IDS = [
    pytest.param("", id="an empty value"),
    pytest.param("   ", id="a value that is only spaces"),
    pytest.param("x" * (telemetry.REQUEST_ID_LENGTH + 1), id="one character too long"),
    pytest.param("a b", id="a space"),
    pytest.param("id!", id="an exclamation mark"),
    pytest.param("<script>alert(1)</script>", id="markup"),
    pytest.param("id;drop table", id="a semicolon"),
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with a temporary database, a temporary uploads directory and a secret."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("TICK_SECRET", SECRET)
    with TestClient(app) as test_client:
        yield test_client


class FakePipeline:
    """Stands in for `get_pipeline()`: records the run id and the trace id it is under."""

    def __init__(self) -> None:
        self.run_ids: list[int] = []
        self.trace_ids: list[str | None] = []
        self.threads: list[threading.Thread] = []
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    def __call__(self, run_id: int) -> None:
        self.run_ids.append(run_id)
        self.trace_ids.append(telemetry.current_trace_id())
        self.threads.append(threading.current_thread())
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=THREAD_TIMEOUT_SECONDS)


@pytest.fixture
def pipeline() -> Iterator[FakePipeline]:
    """The run's work, installed over the route's own provider, for the length of one test."""
    fake = FakePipeline()
    app.dependency_overrides[get_pipeline] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_pipeline, None)


@pytest.fixture
def started() -> Iterator[list[object]]:
    """The starter replaced by one that records the work and returns without running it.

    The recorded callable is what releases the process's guard, so the teardown gives
    the guard back when a test did not -- the guard is the process's and not this
    test's, and leaving it held would refuse the next test's tick.
    """
    work: list[object] = []
    app.dependency_overrides[get_run_starter] = lambda: work.append
    try:
        yield work
    finally:
        app.dependency_overrides.pop(get_run_starter, None)
        if internal._run_in_progress.locked():
            internal._run_in_progress.release()


@pytest.fixture
def inline_starter() -> Iterator[None]:
    """The starter replaced by one that runs the work on this thread, so nothing races.

    Where a case is about a response rather than about the thread, this removes the
    one thing the default starter leaves behind: a run in flight while the assertion
    runs, which would hold the process's guard into the next request.
    """
    app.dependency_overrides[get_run_starter] = lambda: run_inline
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_run_starter, None)


@contextmanager
def app_log_lines() -> Iterator[list[dict[str, object]]]:
    """Every line the app's own loggers write, as the app's own formatter renders it.

    The handler hangs on `app`, the parent of every module logger this app has, so a
    record propagates to it and is formatted in the thread that wrote it -- which is
    what makes `trace_id` in the parsed line the value that thread was under.
    """
    lines: list[dict[str, object]] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(json.loads(telemetry.JsonFormatter().format(record)))

    handler = Collect()
    parent = logging.getLogger("app")
    parent.addHandler(handler)
    try:
        yield lines
    finally:
        parent.removeHandler(handler)


def stored_spans() -> list[dict[str, object]]:
    """Every stored span as one dictionary per row, read while its session is open."""
    with db.session_scope() as session:
        columns = [column.name for column in TelemetrySpan.__table__.columns]
        return [
            {name: getattr(span, name) for name in columns}
            for span in session.scalars(select(TelemetrySpan).order_by(TelemetrySpan.id))
        ]


def run_inline(work: object) -> None:
    """A starter that runs the work on this thread and returns when it is over."""
    work()  # type: ignore[operator]


def store_a_span(**overrides: object) -> None:
    """Write one span directly, with plausible facts a caller overrides one at a time.

    Used by the cases about what the reads answer: driving real requests would pile
    their own spans into the window the assertion is about.
    """
    fields: dict[str, object] = {
        "trace_id": "trace-stored-directly",
        "name": telemetry.HTTP_SPAN_NAME,
        "route": "/healthz",
        "method": "GET",
        "status_code": 200,
        "duration_ms": 5,
        "started_at": datetime.now(UTC),
    }
    fields.update(overrides)
    telemetry.store_span(telemetry.Span(**fields))  # type: ignore[arg-type]


def forget_every_span() -> None:
    """Empty `telemetry_span`, so a case starts from a table with nothing in it."""
    with db.session_scope() as session:
        session.execute(delete(TelemetrySpan))


def tick(client: TestClient, offered: str | None = SECRET, request_id: str | None = None) -> Response:
    """One tick, with the secret and the trace id a caller sends -- or with neither."""
    headers: dict[str, str] = {}
    if offered is not None:
        headers[TICK_SECRET_HEADER] = offered
    if request_id is not None:
        headers[telemetry.REQUEST_ID_HEADER] = request_id
    return client.post("/internal/tick", headers=headers)


def drive(client: TestClient, outcome: str, request_id: str | None = None) -> Response:
    """One request whose response status is `outcome`, through the app's own routes."""
    if outcome == "200":
        return client.get("/healthz", headers=request_id_headers(request_id))
    if outcome in {"202", "401", "500"}:
        offered = SECRET if outcome != "401" else "not-the-secret"
        return tick(client, offered=offered, request_id=request_id)
    if outcome == "404":
        return client.get("/no-route-here-9f3c", headers=request_id_headers(request_id))
    if outcome == "422":
        return client.get("/runs/abc", headers=request_id_headers(request_id))
    raise AssertionError(f"no request in this file answers {outcome}")


def request_id_headers(request_id: str | None) -> dict[str, str]:
    """The `X-Request-ID` header, or no headers at all when the case sends none."""
    return {} if request_id is None else {telemetry.REQUEST_ID_HEADER: request_id}


def a_session_that_cannot_be_opened(*_: object, **__: object) -> None:
    """A `db.session_scope()` that fails the way a database that is down fails."""
    raise RuntimeError("the database is gone")


def a_writer_that_cannot_store(_: object) -> None:
    """A `telemetry.store_span` that fails after taking a session, and gives it back."""
    with db.session_scope():
        raise OperationalError("INSERT INTO telemetry_span", {}, Exception("the database is down"))


def as_text(value: object) -> str:
    """A value as JSON text, for the cases that search it: datetimes rendered as themselves."""
    return json.dumps(value, default=str)


def is_the_pattern(value: str) -> bool:
    """Whether this value is one `adopted_trace_id` accepts, as the app's own rule reads it."""
    return telemetry.REQUEST_ID_PATTERN.fullmatch(value) is not None


# AC1: one trace id per request, adopted or generated, and back on every status.


def test_ac1_the_trace_id_is_the_one_the_caller_sent() -> None:
    """AC1: a value of at most 64 characters of the accepted class is adopted verbatim."""
    assert telemetry.adopted_trace_id(OFFERED_ID) == OFFERED_ID
    assert telemetry.adopted_trace_id("x" * telemetry.REQUEST_ID_LENGTH) == (
        "x" * telemetry.REQUEST_ID_LENGTH
    )


@pytest.mark.parametrize("offered", _REFUSED_IDS)
def test_ac1_a_value_that_may_not_be_adopted_is_replaced(
    client: TestClient, offered: str
) -> None:
    """AC1: empty, whitespace, too long or any other character is not adopted and not echoed."""
    response = client.get("/healthz", headers={telemetry.REQUEST_ID_HEADER: offered})

    echoed = response.headers[telemetry.REQUEST_ID_HEADER]
    assert echoed != offered
    assert is_the_pattern(echoed), echoed
    assert len(echoed) == GENERATED_ID_LENGTH  # a generated id, not a mangled offer
    assert [span["trace_id"] for span in stored_spans()] == [echoed]


@pytest.mark.parametrize("outcome", ["200", "202", "401", "404", "422", "500"])
def test_ac1_the_adopted_id_comes_back_whatever_the_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: FakePipeline,
    started: list[object],
    outcome: str,
) -> None:
    """AC1: every response carries the adopted id, the app's own `500` included."""
    if outcome == "500":
        # The one 500 this app produces as a response rather than by raising: the run
        # row cannot be written, so the tick answers `500` through its own handler.
        monkeypatch.setattr("app.db.session_scope", a_session_that_cannot_be_opened)

    response = drive(client, outcome, request_id=OFFERED_ID)

    assert response.status_code == int(outcome), response.text
    assert response.headers[telemetry.REQUEST_ID_HEADER] == OFFERED_ID


def test_ac1_a_request_with_no_header_gets_a_generated_id(client: TestClient) -> None:
    """AC1: nothing sent is not an error; the id is this app's own and it is valid."""
    response = client.get("/healthz")

    echoed = response.headers[telemetry.REQUEST_ID_HEADER]
    assert len(echoed) == GENERATED_ID_LENGTH
    assert is_the_pattern(echoed)


def test_ac1_every_log_line_written_during_a_request_carries_that_id(
    client: TestClient, pipeline: FakePipeline, started: list[object]
) -> None:
    """AC1: the id is in a field of its own on every JSON line the request writes.

    The lines are parsed rather than searched: a request's line has to *carry* the
    id in `trace_id`, and the run's own line is written under the same value.
    """
    with app_log_lines() as lines:
        assert tick(client, request_id=OFFERED_ID).status_code == 202
        started[0]()  # type: ignore[operator]  # the run's own line, on this thread

    written = [line for line in lines if str(line["logger"]).startswith("app.")]
    assert {line["message"] for line in written} >= {"Started run 1", "Run 1 is over"}
    assert [line["trace_id"] for line in written] == [OFFERED_ID] * len(written)


# AC2: the tick hands its trace id to the run it starts, on the real daemon thread.


def test_ac2_the_real_daemon_thread_runs_under_the_requests_trace_id(
    client: TestClient, pipeline: FakePipeline
) -> None:
    """AC2: the value is handed over as a value, so a thread with an empty context has it.

    The default starter is left in place and the work waits on an event, so the
    thread the assertion reads is the real one `_start_on_a_daemon_thread` starts,
    not a test's synchronous starter.
    """
    pipeline.started = threading.Event()
    pipeline.release = threading.Event()

    with app_log_lines() as lines:
        response = tick(client, request_id=OFFERED_ID)
        assert response.status_code == 202
        assert pipeline.started.wait(timeout=THREAD_TIMEOUT_SECONDS), "the work never started"
        worker = pipeline.threads[0]
        assert worker is not threading.main_thread()
        assert telemetry.current_trace_id() is None  # this thread is under no trace
        pipeline.release.set()
        worker.join(timeout=THREAD_TIMEOUT_SECONDS)
        assert not worker.is_alive()

    assert pipeline.trace_ids == [OFFERED_ID]
    run_id = response.json()["id"]

    # The run's own line, written on that thread, carries the request's id...
    over = [
        line for line in lines if line["message"] == f"Run {run_id} is over"
    ]
    assert [line["trace_id"] for line in over] == [OFFERED_ID]

    # ...and one trace holds the request's span and the run's own, in that order.
    run_span = [span for span in stored_spans() if span["name"] == telemetry.RUN_SPAN_NAME]
    request_span = [span for span in stored_spans() if span["route"] == "/internal/tick"]
    assert [span["trace_id"] for span in run_span] == [OFFERED_ID]
    assert [span["trace_id"] for span in request_span] == [OFFERED_ID]
    assert len({str(span["trace_id"]) for span in run_span + request_span}) == 1


def test_ac2_no_run_column_carries_the_trace_id(client: TestClient) -> None:
    """AC2: the hand-off is the whole of it; the `run` table gains no column (#102)."""
    assert "trace_id" not in {column.name for column in Run.__table__.columns}
    reflected = {column["name"] for column in inspect(db.get_engine()).get_columns("run")}
    assert "trace_id" not in reflected


# AC3: what one stored span holds, and what it holds nothing of.


def test_ac3_a_span_holds_the_seven_facts_and_no_eighth() -> None:
    """AC3: the table's own id beside trace id, name, route, method, status, duration, start."""
    assert {column.name for column in TelemetrySpan.__table__.columns} == {
        "id",
        "trace_id",
        "name",
        "route",
        "method",
        "status_code",
        "duration_ms",
        "started_at",
    }


def test_ac3_the_route_is_the_template_and_never_the_raw_path(
    client: TestClient, pipeline: FakePipeline, started: list[object]
) -> None:
    """AC3: `/runs/{id}` and never `/runs/7`; a query string is not part of a route."""
    run_id = tick(client, request_id=OFFERED_ID).json()["id"]
    assert client.get(f"/runs/{run_id}").status_code == 200
    assert client.get("/matches?limit=5").status_code == 200

    routes = {span["route"]: span for span in stored_spans()}
    assert routes["/runs/{id}"]["method"] == "GET"
    assert routes["/runs/{id}"]["status_code"] == 200
    assert routes["/matches"]["status_code"] == 200
    assert f"/runs/{run_id}" not in routes


def test_ac3_the_runs_span_is_not_an_http_span(
    client: TestClient, pipeline: FakePipeline, started: list[object]
) -> None:
    """AC3: route, method and status code are NULL on `run`, and the other four are set."""
    tick(client, request_id=OFFERED_ID)
    started[0]()  # type: ignore[operator]

    (run_span,) = [span for span in stored_spans() if span["name"] == telemetry.RUN_SPAN_NAME]
    assert (run_span["route"], run_span["method"], run_span["status_code"]) == (None, None, None)
    assert run_span["trace_id"] == OFFERED_ID
    assert run_span["id"] is not None
    assert isinstance(run_span["duration_ms"], int) and run_span["duration_ms"] >= 0
    assert run_span["started_at"] is not None


# AC4: one window of per-route metrics.


def test_ac4_the_nearest_rank_p95_of_a_known_set_is_one_known_number() -> None:
    """AC4: `ceil(0.95 * n)` in ascending order, so a set of durations answers one number."""
    assert telemetry.nearest_rank_p95([10, 20, 30, 40]) == 40
    assert telemetry.nearest_rank_p95(list(range(1, 11))) == 10
    assert telemetry.nearest_rank_p95([7]) == 7
    assert telemetry.nearest_rank_p95([40, 10, 30, 20]) == 40  # unsorted input


def test_ac4_the_window_answers_count_errors_and_p95_per_route(client: TestClient) -> None:
    """AC4: every span of a route is a count, the 500s are errors, and p95 is its own rank."""
    for duration in (10, 20, 30, 40):
        store_a_span(route="/matches", duration_ms=duration)
    store_a_span(route="/matches", duration_ms=50, status_code=500)
    store_a_span(route="/healthz", duration_ms=3)

    body = client.get("/telemetry/metrics").json()

    assert body == [
        {"route": "/healthz", "count": 1, "errors": 0, "p95_ms": 3},
        {"route": "/matches", "count": 5, "errors": 1, "p95_ms": 50},
    ]


def test_ac4_a_span_older_than_the_window_is_not_counted(client: TestClient) -> None:
    """AC4: the window is a constant here, and a span that started before it is excluded."""
    aged = datetime.now(UTC) - telemetry.METRICS_WINDOW - timedelta(minutes=1)
    store_a_span(route="/old", started_at=aged)
    store_a_span(route="/new", started_at=datetime.now(UTC) - timedelta(minutes=1))

    body = client.get("/telemetry/metrics").json()

    assert [row["route"] for row in body] == ["/new"]


def test_ac4_a_window_with_no_spans_answers_an_empty_list(client: TestClient) -> None:
    """AC4: nothing in the window is `[]`, not a row per route with zeroes."""
    forget_every_span()

    assert client.get("/telemetry/metrics").json() == []


def test_ac4_the_runs_own_span_is_not_a_route(client: TestClient) -> None:
    """AC4: `run` has no route, so no metric row is invented for it."""
    store_a_span(name=telemetry.RUN_SPAN_NAME, route=None, method=None, status_code=None)

    assert client.get("/telemetry/metrics").json() == []


# AC5: the newest traces, grouped by trace id.


def test_ac5_traces_are_grouped_newest_first_with_spans_in_start_order(client: TestClient) -> None:
    """AC5: `{trace_id, spans}`, spans by start time, traces by their latest span."""
    first = datetime.now(UTC) - timedelta(minutes=5)
    store_a_span(trace_id="trace-older", route="/healthz", started_at=first)
    store_a_span(trace_id="trace-older", route="/matches", started_at=first + timedelta(seconds=1))
    store_a_span(trace_id="trace-newer", route="/runs/{id}", started_at=first + timedelta(minutes=1))

    body = client.get("/telemetry/traces").json()

    assert [trace["trace_id"] for trace in body] == ["trace-newer", "trace-older"]
    assert [span["route"] for span in body[1]["spans"]] == ["/healthz", "/matches"]
    # The ids are the stored rows' own, in the order the spans started.
    stored = [row for row in stored_spans() if row["trace_id"] == "trace-older"]
    stored.sort(key=lambda row: row["started_at"])
    assert [span["id"] for span in body[1]["spans"]] == [row["id"] for row in stored]
    assert set(body[0]["spans"][0]) == {
        "id",
        "name",
        "route",
        "method",
        "status_code",
        "duration_ms",
        "started_at",
    }


def test_ac5_the_limit_counts_traces(client: TestClient) -> None:
    """AC5: the newest N trace ids and every span each one holds, not the newest N spans."""
    now = datetime.now(UTC)
    for index in range(3):
        store_a_span(trace_id=f"trace-{index}", started_at=now + timedelta(seconds=index))
        store_a_span(trace_id=f"trace-{index}", name=telemetry.RUN_SPAN_NAME, route=None)

    body = client.get("/telemetry/traces?limit=2").json()

    assert [trace["trace_id"] for trace in body] == ["trace-2", "trace-1"]
    assert [len(trace["spans"]) for trace in body] == [2, 2]


def test_ac5_the_limit_is_bounded_by_the_constant_that_mirrors_the_matches_one() -> None:
    """AC5: one bound, spelled once in `app.telemetry`, and the same number as `/matches`."""
    assert telemetry.TRACES_MAX_LIMIT == MATCHES_MAX_LIMIT


@pytest.mark.parametrize("limit", ["0", "-1", "201"])
def test_ac5_a_limit_outside_its_range_is_refused(client: TestClient, limit: str) -> None:
    """AC5: `ge=1` and no more than the constant, refused by the parameter itself."""
    assert client.get(f"/telemetry/traces?limit={limit}").status_code == 422


def test_ac5_no_stored_trace_answers_an_empty_list(client: TestClient) -> None:
    """AC5: nothing stored is `[]`, which is what a fresh database answers."""
    assert client.get("/telemetry/traces").json() == []


# AC6: nothing a request or a resume carries reaches any of the three storages.


def test_ac6_no_resume_text_and_no_secret_reaches_a_span_a_line_or_a_read(
    client: TestClient, pipeline: FakePipeline, started: list[object]
) -> None:
    """AC6: the upload's sentinel, its file name and the tick's secret reach none of them.

    The journey is the rubric's own: upload a resume, tick the app with the shared
    header, then read both telemetry bodies. What is asserted is the absence of the
    sentinel and the secret from every stored span, every line the journey wrote, and
    both bodies -- the resume row itself is where the text is *supposed* to be.
    """
    with app_log_lines() as lines:
        uploaded = client.post(
            "/resume",
            files={"file": (RESUME_FILENAME, RESUME_SENTINEL.encode(), "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        assert tick(client, request_id=OFFERED_ID).status_code == 202
        started[0]()  # type: ignore[operator]
        metrics = client.get("/telemetry/metrics")
        traces = client.get("/telemetry/traces")
        assert (metrics.status_code, traces.status_code) == (200, 200)

    stored = as_text(stored_spans())
    journey = as_text([line for line in lines if str(line["logger"]).startswith("app.")])
    bodies = as_text(metrics.json()) + as_text(traces.json())

    assert journey, "the journey must have logged something, or the case proves nothing"
    for value in (RESUME_SENTINEL, RESUME_FILENAME, SECRET):
        assert value not in stored, value
        assert value not in journey, value
        assert value not in bodies, value

    # The resume row is where the sentinel lives: without this the case could pass
    # because the upload never stored anything at all.
    assert client.get(f"/resume/{uploaded.json()['id']}/file").content == RESUME_SENTINEL.encode()


def test_ac6_a_span_and_a_line_hold_no_query_string_and_no_bearer_value(
    client: TestClient,
) -> None:
    """AC6: a query string and an `Authorization` value are read by nobody, so they are nowhere."""
    bearer = "Bearer eyJhbGciOiJIUzI1NiJ9.sentinel-token-5c1a"

    with app_log_lines() as lines:
        response = client.get(
            "/matches?limit=5&state=sentinel-query-3b7d",
            headers={"Authorization": bearer},
        )

    written = as_text([line for line in lines if str(line["logger"]).startswith("app.")])
    stored = as_text(stored_spans())
    assert response.status_code in {200, 422}
    assert "sentinel-query-3b7d" not in stored
    assert "sentinel-token-5c1a" not in stored
    assert "sentinel-query-3b7d" not in written
    assert "sentinel-token-5c1a" not in written


# AC7: bounded storage, one deleter, called once per process start.


def test_ac7_the_prune_deletes_the_old_span_and_keeps_the_one_inside_retention(
    client: TestClient,
) -> None:
    """AC7: one span older than the retention goes and one inside it stays."""
    aged = datetime.now(UTC) - telemetry.SPAN_RETENTION - timedelta(minutes=1)
    store_a_span(trace_id="trace-aged", started_at=aged)
    store_a_span(trace_id="trace-kept", started_at=datetime.now(UTC))

    pruned = telemetry.prune_spans()

    assert pruned == 1
    assert [span["trace_id"] for span in stored_spans()] == ["trace-kept"]


def test_ac7_retention_is_strictly_longer_than_the_metrics_window() -> None:
    """AC7: a span a metric counted is still stored, so the prune cannot outrun the window."""
    assert telemetry.SPAN_RETENTION > telemetry.METRICS_WINDOW


def test_ac7_the_lifespan_prunes_once_per_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: beside `reap_stale_runs()`, so a process start is what bounds the table."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    db.init_db()
    store_a_span(
        trace_id="trace-aged", started_at=datetime.now(UTC) - telemetry.SPAN_RETENTION * 2
    )
    store_a_span(trace_id="trace-kept", started_at=datetime.now(UTC))

    with TestClient(app):  # entering the lifespan is the startup this criterion is about
        pass

    assert [span["trace_id"] for span in stored_spans()] == ["trace-kept"]


# AC9: the app's own JSON log format, and what installing it leaves alone.


def test_ac9_the_app_installs_its_formatter_once_on_the_root_logger() -> None:
    """AC9: the formatter is the app's own, on the root logger, and installing twice is one."""
    def json_handlers() -> list[logging.Handler]:
        return [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler.formatter, telemetry.JsonFormatter)
        ]

    assert len(json_handlers()) == 1

    telemetry.configure_logging()  # a second install replaces, it does not add

    assert len(json_handlers()) == 1


def test_ac9_an_installed_uvicorn_logs_through_the_app_formatter(tmp_path: Path) -> None:
    """AC9: uvicorn configures its loggers before it imports the app, and they are re-pointed."""
    script = (
        "import json, logging, logging.config, sys;"
        f"sys.path.insert(0, {str(BACKEND_ROOT)!r});"
        "import uvicorn.config;"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG);"
        "import app.main;"
        "print(json.dumps({name: [type(h.formatter).__name__"
        " for h in logging.getLogger(name).handlers]"
        " for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access')}))"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "uvicorn": ["JsonFormatter"],
        "uvicorn.error": [],
        "uvicorn.access": ["JsonFormatter"],
    }


def test_ac9_installing_the_formatter_leaves_sqlalchemy_engine_at_warning() -> None:
    """AC9: `app/db.py`'s floor still holds, so no statement or row parameter is printed."""
    telemetry.configure_logging()

    assert logging.getLogger("sqlalchemy.engine").level >= logging.WARNING
    assert logging.getLogger("sqlalchemy.engine").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("sqlalchemy.engine.Engine").getEffectiveLevel() >= logging.WARNING


def test_ac9_a_line_written_outside_a_trace_renders_a_null_trace_id() -> None:
    """AC9: the startup reap and a startup failure log outside any request, and still render."""
    record = logging.LogRecord("app.services.pipeline", logging.INFO, __file__, 1, "no trace", (), None)

    line = json.loads(telemetry.JsonFormatter().format(record))

    assert line["trace_id"] is None
    assert (line["level"], line["logger"], line["message"]) == (
        "INFO",
        "app.services.pipeline",
        "no trace",
    )


def test_ac9_a_line_carrying_an_exception_is_still_one_json_line() -> None:
    """AC9: the traceback is a field, so the log stream parses line by line."""
    try:
        raise RuntimeError("the board could not be read")
    except RuntimeError:
        record = logging.LogRecord("app.sources", logging.ERROR, __file__, 1, "a failure", (), sys.exc_info())

    text = telemetry.JsonFormatter().format(record)

    assert text.count("\n") == 1 and text.endswith("\n")
    line = json.loads(text)
    assert line["exception"].startswith("Traceback (most recent call last)")
    assert "the board could not be read" in line["exception"]


def test_ac9_a_line_written_inside_a_request_carries_that_requests_trace_id() -> None:
    """AC9: the id is this process's current trace, read where the record is formatted."""
    token = telemetry._trace_id.set(OFFERED_ID)
    try:
        line = json.loads(
            telemetry.JsonFormatter().format(
                logging.LogRecord("app.routes.internal", logging.INFO, __file__, 1, "in a request", (), None)
            )
        )
    finally:
        telemetry._trace_id.reset(token)

    assert line["trace_id"] == OFFERED_ID


# AC10: one span per request, whatever its outcome, and never a raw path.


@pytest.mark.parametrize("outcome", ["200", "202", "401", "404", "422"])
def test_ac10_an_answered_request_writes_exactly_one_span(
    client: TestClient, pipeline: FakePipeline, started: list[object], outcome: str
) -> None:
    """AC10: each outcome writes one span, whose facts are the request's own."""
    forget_every_span()

    response = drive(client, outcome)
    assert response.status_code == int(outcome), response.text

    (span,) = stored_spans()
    assert span["status_code"] == int(outcome)
    assert span["method"] == ("POST" if outcome in {"202", "401"} else "GET")
    assert span["route"] == {
        "200": "/healthz",
        "202": "/internal/tick",
        "401": "/internal/tick",
        "404": telemetry.UNMATCHED_ROUTE,
        "422": "/runs/{id}",
    }[outcome]
    # The read's own span is written after its body, so this is that one route's count.
    assert client.get("/telemetry/metrics").json() == [
        {"route": span["route"], "count": 1, "errors": 0, "p95_ms": span["duration_ms"]}
    ]


def test_ac10_a_path_no_route_matched_is_the_sentinel_and_never_the_path(
    client: TestClient,
) -> None:
    """AC10: the raw path is caller-chosen text, so the sentinel is recorded instead."""
    asked = "/nothing-here-4f2c?a=1"
    forget_every_span()

    assert client.get(asked).status_code == 404

    (span,) = stored_spans()
    assert span["route"] == telemetry.UNMATCHED_ROUTE
    assert "nothing-here-4f2c" not in as_text(span)


def explode() -> None:
    """A `get_run_starter` that raises: the one way a route of this app fails, unwrapped.

    A provider rather than a starter, because the route catches what a *starter*
    raises and answers `500` itself (`internal._start_run`); only an exception
    raised before the route's body escapes it, which is what these two cases read.
    """
    raise RuntimeError("the starter is gone")


def test_ac10_a_raised_exception_is_recorded_as_a_500_and_not_swallowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC10: the span is written and the exception still reaches the server's own handler."""
    monkeypatch.setitem(app.dependency_overrides, get_run_starter, explode)
    forget_every_span()

    with pytest.raises(RuntimeError, match="the starter is gone"):
        tick(client)

    (span,) = stored_spans()
    assert (span["route"], span["method"], span["status_code"]) == (
        "/internal/tick",
        "POST",
        telemetry.ERROR_STATUS_CODE,
    )


def test_ac10_the_answer_to_a_raised_exception_is_the_apps_own_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pipeline: FakePipeline
) -> None:
    """AC10: the middleware re-raises, so what a client gets is the server's own response."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("TICK_SECRET", SECRET)
    monkeypatch.setitem(app.dependency_overrides, get_run_starter, explode)

    with TestClient(app, raise_server_exceptions=False) as forgiving:
        forget_every_span()
        response = forgiving.post("/internal/tick", headers={TICK_SECRET_HEADER: SECRET})

    assert response.status_code == 500
    assert [span["status_code"] for span in stored_spans()] == [telemetry.ERROR_STATUS_CODE]


# AC11: telemetry never fails the request it observes.


def test_ac11_a_writer_that_raises_leaves_the_responses_exactly_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline_starter: None
) -> None:
    """AC11: with the writer failing, every answer is the answer it would have been.

    The control is a second client built the same way with nothing patched: the
    statuses and the bodies are compared, and not asserted to values this file would
    have to keep in step with the routes.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("TICK_SECRET", SECRET)
    monkeypatch.setitem(app.dependency_overrides, get_pipeline, lambda: FakePipeline())

    def run(client: TestClient) -> list[tuple[int, dict[str, object]]]:
        answers: list[tuple[int, dict[str, object]]] = []
        for response in (
            client.get("/healthz"),
            client.get("/healthz", headers={telemetry.REQUEST_ID_HEADER: OFFERED_ID}),
            client.post("/internal/tick", headers={TICK_SECRET_HEADER: SECRET}),
            client.post("/internal/tick", headers={TICK_SECRET_HEADER: "wrong"}),
            client.get("/no-route-here-9f3c"),
        ):
            body = response.json()
            if response.status_code == 202:
                # The run's own id and instant: the two things a second run of the
                # same journey cannot share, and no part of what is being compared.
                body = {**body, "id": "a run id", "started_at": "an instant"}
            answers.append((response.status_code, body))
        return answers

    with monkeypatch.context() as failing:
        failing.setattr(telemetry, "store_span", a_writer_that_cannot_store)
        with TestClient(app) as broken:
            broken_answers = run(broken)

    with TestClient(app) as control:
        control_answers = run(control)

    assert broken_answers == control_answers
    assert [status for status, _ in broken_answers] == [200, 200, 202, 401, 404]
    assert broken_answers[0][1] == {"status": "ok"}
    assert broken_answers[3][1]["detail"], "a refusal still says what it refused"
    assert stored_spans() != []  # the control's own spans, written by the real writer


def test_ac11_the_failure_is_logged_once_naming_the_redacted_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC11: one line per failed write, naming the database the way the app names it.

    `/healthz` reads no configuration and no database, so this case can point
    `DATABASE_URL` at a credential-bearing URL it never opens -- no lifespan, no
    engine -- and assert what the line holds: the redacted form, and not the password.
    """
    password = "notarealpassword-2f9d"
    monkeypatch.setenv("DATABASE_URL", f"postgresql://jobradar:{password}@127.0.0.1:1/jobradar")
    monkeypatch.setattr(telemetry, "store_span", a_writer_that_cannot_store)
    assert db.redacted_database_url().endswith("@127.0.0.1:1/jobradar")

    with app_log_lines() as lines:
        response = TestClient(app).get("/healthz")  # no lifespan: /healthz needs no database

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    warnings = [
        line
        for line in lines
        if line["level"] == "WARNING" and "Could not store" in str(line["message"])
    ]
    assert len(warnings) == 1
    assert db.redacted_database_url() in str(warnings[0]["message"])
    assert password not in as_text(warnings)
    assert "127.0.0.1:1" in str(warnings[0]["message"])  # the failure names the database
    assert "OperationalError" in str(warnings[0]["message"])  # and only the failure's type


def test_ac11_a_failing_writer_leaks_no_connection(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: FakePipeline,
    inline_starter: None,
) -> None:
    """AC11: the writer takes a session and fails inside it, and the connection goes back.

    The `engine` fixture asserts the pool's checked-out count after the test as well;
    this assertion is the one that names why.
    """
    monkeypatch.setattr(telemetry, "store_span", a_writer_that_cannot_store)
    monkeypatch.setenv("TICK_SECRET", SECRET)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/internal/tick", headers={TICK_SECRET_HEADER: SECRET}).status_code == 202
        assert client.get("/matches").status_code == 200

    assert db.get_engine() is engine
    assert engine.pool.checkedout() == 0
