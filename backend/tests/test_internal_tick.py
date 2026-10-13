"""`POST /internal/tick`: the secret, the row it writes, the thread off this one, and the guard.

Every test drives the real app through `TestClient` -- no server, no port, no
board, no model, no key -- with `DATABASE_URL` pointed at a temporary SQLite
file, so the suite leaves no `backend/data/jobradar.db` behind and `git status
--short` stays empty. Both seams `app.routes.internal` declares are replaced
through `app.dependency_overrides`, exactly as `routes/watchlist.py`'s
`get_source` is.

The tests are deterministic in two ways, and neither one sleeps or polls. Where
the criterion is about the response and the row, the starter is replaced by one
that records the work without running it: the `202` and the row are observed
while the work has provably not run, and the recorded callable is then called
inline, on this test's own thread. Where the criterion is about the thread
itself, the default starter runs a fake pipeline that signals one
`threading.Event` when it starts and blocks on a second until the test releases
it.

The run guard is the process's -- `internal._run_in_progress` -- so a test that
answered `202` and never called the recorded work would leave it held and make
the next test's tick a `409`. The `captured` fixture gives it back at teardown.
"""

import inspect
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select

from app import db
from app.main import app
from app.models import Run
from app.routes import internal
from app.routes.internal import (
    TICK_SECRET_HEADER,
    get_pipeline,
    get_rematch,
    get_run_starter,
)
from app.sources.base import SourceError

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = BACKEND_ROOT / "app"

# A value that could not appear in a response, a log line or a row by accident:
# what these tests assert is that it reaches none of them from where it lives.
SECRET = "tick-secret-sentinel-7f4c1d9ab38e"

# The ceiling on a wait that should not happen, so a broken run fails this suite
# instead of hanging it. Nothing sleeps for it; the two events do the signalling.
THREAD_TIMEOUT_SECONDS = 5.0

_REFUSED_HEADERS = [
    pytest.param(None, id="no header at all"),
    pytest.param("", id="an empty header"),
    pytest.param("   ", id="a header that is only spaces"),
    pytest.param(f"{SECRET}-not-it", id="a different value"),
    pytest.param(SECRET.upper(), id="the right value in the wrong case"),
]

_UNSET_SECRETS = [
    pytest.param(None, id="no variable"),
    pytest.param("", id="an empty variable"),
    pytest.param("   ", id="a variable that is only spaces"),
]


class FakePipeline:
    """Stands in for `get_pipeline()`: records the run id it is handed, then returns or raises."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.run_ids: list[int] = []
        self.error = error

    def __call__(self, run_id: int) -> None:
        self.run_ids.append(run_id)
        if self.error is not None:
            raise self.error


@pytest.fixture(autouse=True)
def configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared secret, in the environment, set per request the way a deployment sets it."""
    monkeypatch.setenv(internal.TICK_SECRET_VARIABLE, SECRET)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with its database pointed at this test's temporary SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    with TestClient(app) as test_client:
        yield test_client


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
def captured() -> Iterator[list[Callable[[], None]]]:
    """The starter replaced by one that records the work and returns without running it.

    The recorded callable is what releases the process's guard, so the teardown
    gives the guard back when a test did not: the guard is the process's and not
    this test's, and leaving it held would refuse the next test's tick.
    """
    work: list[Callable[[], None]] = []
    app.dependency_overrides[get_run_starter] = lambda: work.append
    try:
        yield work
    finally:
        app.dependency_overrides.pop(get_run_starter, None)
        if internal._run_in_progress.locked():
            internal._run_in_progress.release()


def tick(client: TestClient, offered: str | None = SECRET) -> Response:
    """One tick, with the header a caller sends -- or with no such header at all."""
    headers = {} if offered is None else {TICK_SECRET_HEADER: offered}
    return client.post("/internal/tick", headers=headers)


def refusal(response: Response, status: int) -> str:
    """The message a refusal answered with, having checked it is a JSON body carrying one."""
    assert response.status_code == status, response.text
    assert response.json()["detail"], "a refusal must say what it refused"
    return response.json()["detail"]


def run_rows() -> list[dict[str, Any]]:
    """Every `run` row as its own columns, read through the app's own session."""
    with db.session_scope() as session:
        columns = [column.name for column in Run.__table__.columns]
        return [
            {name: getattr(run, name) for name in columns}
            for run in session.scalars(select(Run).order_by(Run.id))
        ]


def log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


# The two seams #34 plugs into, and the shape the rest of the module overrides.


def test_the_seams_take_the_work_and_hand_it_the_run_id() -> None:
    assert list(inspect.signature(get_run_starter()).parameters) == ["work"]
    assert list(inspect.signature(get_pipeline()).parameters) == ["run_id"]


# The secret: one header, one variable, one module, constant-time, and 401 for everything else.


def test_the_secret_is_named_the_one_way_and_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in APP_ROOT.rglob("*.py")
        if internal.TICK_SECRET_VARIABLE in path.read_text(encoding="utf-8")
    )

    assert readers == ["internal.py"]
    assert TICK_SECRET_HEADER == "X-Tick-Secret"
    assert internal.TICK_SECRET_VARIABLE == "TICK_SECRET"


def test_the_secret_is_compared_constant_time_and_never_with_equality() -> None:
    """A plain `==` stops at the first differing byte, which is what this header must not do."""
    text = (APP_ROOT / "routes" / "internal.py").read_text(encoding="utf-8")
    named = ("provided", "offered", "configured")

    assert "hmac.compare_digest(" in text
    assert [
        line.strip() for line in text.splitlines() if "==" in line and any(n in line for n in named)
    ] == []


@pytest.mark.parametrize("offered", _REFUSED_HEADERS)
def test_a_tick_without_the_configured_secret_is_401_and_does_nothing(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
    offered: str | None,
) -> None:
    response = tick(client, offered)

    message = refusal(response, 401)
    assert TICK_SECRET_HEADER in message  # the refusal names the header it wanted
    assert SECRET not in response.text
    assert run_rows() == []  # a refusal is inert
    assert captured == []
    assert pipeline.run_ids == []


@pytest.mark.parametrize("configured", _UNSET_SECRETS)
def test_a_process_with_no_configured_secret_refuses_every_tick(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    """The endpoint is never open by default, and an empty variable is not a password."""
    if configured is None:
        monkeypatch.delenv(internal.TICK_SECRET_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(internal.TICK_SECRET_VARIABLE, configured)

    for offered in (None, "", SECRET):
        message = refusal(tick(client, offered), 401)
        assert TICK_SECRET_HEADER in message

    assert run_rows() == []
    assert captured == []
    assert pipeline.run_ids == []


# The secret leaves in no response, in no log line and in no column of a run row.


def test_the_secret_reaches_no_response_body_and_no_log_record(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every status this route answers with, including the one the request cannot recover from."""
    caplog.set_level(logging.DEBUG)

    responses = [
        tick(client, f"{SECRET}-not-it"),  # 401
        tick(client),  # 202
        tick(client),  # 409, while the run above is in progress
    ]
    assert [response.status_code for response in responses] == [401, 202, 409]
    captured[0]()  # the recorded work runs inline, which releases the guard

    def the_database_is_gone(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database is gone")

    real_session_scope = db.session_scope
    monkeypatch.setattr("app.db.session_scope", the_database_is_gone)
    responses.append(tick(client))  # 500
    assert responses[-1].status_code == 500
    monkeypatch.setattr("app.db.session_scope", real_session_scope)

    assert caplog.records  # an empty stream would make the absence below meaningless
    assert [line for line in log_lines(caplog.records) if SECRET in line] == []
    assert [response.text for response in responses if SECRET in response.text] == []


def test_the_secret_is_in_no_column_of_a_run_row_after_a_success_and_after_a_failure(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
) -> None:
    assert tick(client).status_code == 202
    captured[0]()  # a run that succeeds
    pipeline.error = RuntimeError(f"the board refused while {SECRET} was configured")

    assert tick(client).status_code == 202
    captured[1]()  # and a run that fails, whose `error` is the column most at risk

    rows = run_rows()
    assert [row["status"] for row in rows] == ["succeeded", "failed"]
    assert rows[1]["error"], "the failed row carries a message, so its absence below means something"
    assert [name for row in rows for name, value in row.items() if SECRET in str(value)] == []


# A valid tick: the row first, the work second, the 202 carries the row.


def test_a_valid_tick_answers_202_with_the_running_row_it_wrote_before_the_work_started(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
) -> None:
    response = tick(client)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "started_at",
        "finished_at",
        "error",
        "postings_seen",
        "matches_created",
        "matches_emailed",
        "companies_not_fetched",
        "postings_not_rematched",
        "company_outcomes",
    }
    assert body["status"] == "running"
    assert (body["finished_at"], body["error"]) == (None, None)
    assert (body["postings_seen"], body["matches_created"]) == (0, 0)
    # #36's field: the row is written before any digest of it ran, so no count yet.
    assert body["matches_emailed"] is None
    # #64's field: a tick is not a re-match, so the column the re-match writes is NULL.
    assert body["postings_not_rematched"] is None
    # #59's field: no company has been attempted yet, so the list is empty and the
    # `202` and the read answer one shape.
    assert body["company_outcomes"] == []

    assert len(captured) == 1  # exactly one unit of work was handed over
    assert pipeline.run_ids == []  # and it has provably not run yet
    (row,) = run_rows()
    assert row["id"] == body["id"]  # the row was written before the hand-over
    assert row["status"] == "running"
    assert (row["finished_at"], row["error"]) == (None, None)
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert client.get(f"/runs/{body['id']}").json() == body  # the 202 body is the read's body

    captured[0]()  # the work, inline, on this test's own thread

    assert pipeline.run_ids == [body["id"]]  # handed the id the row already had
    (finished,) = run_rows()
    assert finished["id"] == body["id"] and finished["status"] == "succeeded"


# The real starter: a thread of its own, while the request has already answered.


def test_the_default_starter_runs_the_work_off_the_request_thread(client: TestClient) -> None:
    """The 202 goes out while the work is still blocked, on a thread that is not this one."""
    started = threading.Event()
    release = threading.Event()
    observed: dict[str, Any] = {}

    def work(run_id: int) -> None:
        observed["thread"] = threading.current_thread()
        observed["run_id"] = run_id
        started.set()
        release.wait(timeout=THREAD_TIMEOUT_SECONDS)

    app.dependency_overrides[get_pipeline] = lambda: work
    try:
        response = tick(client)

        assert response.status_code == 202
        assert started.wait(timeout=THREAD_TIMEOUT_SECONDS), "the work never started"
        worker = observed["thread"]
        assert worker is not threading.main_thread()
        assert observed["run_id"] == response.json()["id"]
        (in_flight,) = run_rows()
        assert in_flight["status"] == "running"  # still running while the work is blocked

        release.set()
        worker.join(timeout=THREAD_TIMEOUT_SECONDS)
        assert not worker.is_alive()  # no thread outlives this test
    finally:
        release.set()
        app.dependency_overrides.pop(get_pipeline, None)

    (finished,) = run_rows()
    assert finished["status"] == "succeeded"


# How a run ends: the counts it was given, the error it raised, and the row either way.


def test_a_run_that_ends_writes_its_outcome_and_the_counts_the_work_wrote(
    client: TestClient, captured: list[Callable[[], None]]
) -> None:
    def work(run_id: int) -> None:
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            run.postings_seen = 7
            run.matches_created = 3

    app.dependency_overrides[get_pipeline] = lambda: work
    try:
        assert tick(client).status_code == 202
        captured[0]()
    finally:
        app.dependency_overrides.pop(get_pipeline, None)

    (row,) = run_rows()
    assert row["status"] == "succeeded"
    assert row["error"] is None
    assert row["finished_at"] >= row["started_at"]
    assert (row["postings_seen"], row["matches_created"]) == (7, 3)


def test_a_run_that_wrote_nothing_succeeds_with_both_counts_zero(
    client: TestClient, captured: list[Callable[[], None]], pipeline: FakePipeline
) -> None:
    """The ordinary "nothing new" run: no counts written, and not a failure."""
    assert tick(client).status_code == 202
    captured[0]()

    (row,) = run_rows()
    assert row["status"] == "succeeded"
    assert row["error"] is None
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert pipeline.run_ids == [row["id"]]


@pytest.mark.parametrize(
    "error",
    [
        SourceError("The board could not be read: HTTP 503"),
        RuntimeError("the database is gone"),
    ],
    ids=["a source error", "an unexpected error"],
)
def test_a_failure_inside_the_run_lands_on_the_row_and_leaves_the_process_up(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
    error: Exception,
) -> None:
    pipeline.error = error

    assert tick(client).status_code == 202
    captured[0]()  # the failure does not escape the work: this call returns

    (row,) = run_rows()
    assert row["status"] == "failed"
    assert row["error"] == str(error)
    assert row["finished_at"] is not None
    assert "Traceback" not in row["error"]  # the message, never the traceback

    assert client.get("/healthz").status_code == 200  # the process is up
    assert tick(client).status_code == 202  # and a later tick is still accepted
    captured[1]()
    assert [failed["status"] for failed in run_rows()] == ["failed", "failed"]


# The guard: one run at a time in this process, given back on every path.


def test_a_second_tick_while_a_run_is_in_progress_is_409_and_starts_nothing(
    client: TestClient, captured: list[Callable[[], None]], pipeline: FakePipeline
) -> None:
    assert tick(client).status_code == 202
    before = run_rows()

    message = refusal(tick(client), 409)

    assert "in progress" in message
    assert run_rows() == before  # no second row, and the first one is untouched
    assert len(captured) == 1  # no second unit of work
    assert pipeline.run_ids == []

    captured[0]()  # the run in progress is unaffected and still finishes
    assert run_rows()[0]["status"] == "succeeded"


def test_the_next_tick_is_accepted_after_a_success_and_after_a_failure(
    client: TestClient, captured: list[Callable[[], None]], pipeline: FakePipeline
) -> None:
    assert tick(client).status_code == 202
    captured[0]()  # a run that succeeds and gives the guard back
    pipeline.error = RuntimeError("the board refused")

    assert tick(client).status_code == 202  # accepted again after a success
    captured[1]()  # a run that fails and gives the guard back
    pipeline.error = None

    assert tick(client).status_code == 202  # accepted again after a failure
    captured[2]()

    assert [row["status"] for row in run_rows()] == ["succeeded", "failed", "succeeded"]


def test_a_running_row_this_process_did_not_create_does_not_refuse_a_tick(
    client: TestClient, captured: list[Callable[[], None]], pipeline: FakePipeline
) -> None:
    """What a process that died mid-run leaves behind is #35's; it refuses nothing here.

    The run's own work is the fake above, as in every other case here that runs the
    recorded work: this case is about the guard, and the walk #34 wired into the real
    provider needs an active resume this test's database does not have.
    """
    with db.session_scope() as session:
        session.add(Run(status=internal.RUNNING))

    assert tick(client).status_code == 202
    assert [row["status"] for row in run_rows()] == ["running", "running"]

    captured[0]()
    assert [row["status"] for row in run_rows()] == ["running", "succeeded"]


def test_a_row_that_cannot_be_written_is_500_and_the_guard_is_given_back(
    client: TestClient, captured: list[Callable[[], None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that cannot be recorded must not refuse every later tick."""
    real_session_scope = db.session_scope

    def the_database_is_gone(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr("app.db.session_scope", the_database_is_gone)

    response = tick(client)

    refusal(response, 500)

    monkeypatch.setattr("app.db.session_scope", real_session_scope)

    assert run_rows() == []
    assert captured == []
    assert tick(client).status_code == 202  # the guard was released on that path too
    assert len(captured) == 1


# The re-match (#64): the tick's own machinery one door over -- the same secret,
# the same guard, the same row, the same `202` and read -- with its own work seam.


@pytest.fixture
def rematch_work() -> Iterator[FakePipeline]:
    """The re-match's work, installed over the route's own `get_rematch`, for one test."""
    fake = FakePipeline()
    app.dependency_overrides[get_rematch] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_rematch, None)


def rematch(client: TestClient, offered: str | None = SECRET) -> Response:
    """One re-match, with the header a caller sends -- or with no such header at all."""
    headers = {} if offered is None else {TICK_SECRET_HEADER: offered}
    return client.post("/internal/rematch", headers=headers)


def test_the_rematch_provider_is_a_zero_argument_pipeline_of_the_tick_s_shape() -> None:
    """`get_rematch()` carries no configuration and returns a `Callable[[int], None]`."""
    assert list(inspect.signature(get_rematch).parameters) == []
    assert list(inspect.signature(get_rematch()).parameters) == ["run_id"]


@pytest.mark.parametrize("offered", _REFUSED_HEADERS)
def test_a_rematch_without_the_configured_secret_is_401_and_does_nothing(
    client: TestClient,
    captured: list[Callable[[], None]],
    rematch_work: FakePipeline,
    offered: str | None,
) -> None:
    response = rematch(client, offered)

    message = refusal(response, 401)
    assert TICK_SECRET_HEADER in message  # the refusal names the header it wanted
    assert SECRET not in response.text
    assert run_rows() == []  # a refusal is inert
    assert captured == []
    assert rematch_work.run_ids == []


def test_a_valid_rematch_answers_202_with_the_running_row_before_the_work_started(
    client: TestClient,
    captured: list[Callable[[], None]],
    rematch_work: FakePipeline,
) -> None:
    response = rematch(client)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "started_at",
        "finished_at",
        "error",
        "postings_seen",
        "matches_created",
        "matches_emailed",
        "companies_not_fetched",
        "postings_not_rematched",
        "company_outcomes",
    }
    assert body["status"] == "running"
    assert (body["postings_seen"], body["matches_created"]) == (0, 0)
    assert (body["matches_emailed"], body["postings_not_rematched"]) == (None, None)
    assert body["company_outcomes"] == []  # no board is asked, so no company outcome

    assert len(captured) == 1  # exactly one unit of work was handed over
    assert rematch_work.run_ids == []  # and it has provably not run yet
    (row,) = run_rows()
    assert row["id"] == body["id"]  # the row was written before the hand-over
    assert row["status"] == "running"
    assert client.get(f"/runs/{body['id']}").json() == body  # the 202 body is the read's body

    captured[0]()  # the work, inline, on this test's own thread

    assert rematch_work.run_ids == [body["id"]]  # handed the id the row already had
    (finished,) = run_rows()
    assert finished["id"] == body["id"] and finished["status"] == "succeeded"


def test_a_rematch_while_a_tick_runs_is_409_and_a_tick_while_a_rematch_runs_is_409(
    client: TestClient,
    captured: list[Callable[[], None]],
    pipeline: FakePipeline,
    rematch_work: FakePipeline,
) -> None:
    """The guard is the process's and the two routes share it, in both directions."""
    assert tick(client).status_code == 202  # a tick claims the one run-at-a-time guard

    message = refusal(rematch(client), 409)  # the re-match is refused rather than queued
    assert "in progress" in message
    assert run_rows() and len(run_rows()) == 1  # no second row
    assert pipeline.run_ids == [] and rematch_work.run_ids == []
    assert len(captured) == 1  # no second unit of work

    captured[0]()  # the tick's run ends and gives the guard back

    assert rematch(client).status_code == 202  # a re-match now claims the guard

    message = refusal(tick(client), 409)  # and the tick is refused
    assert "in progress" in message
    assert len(captured) == 2  # the refused tick handed over no work

    captured[1]()
    assert [row["status"] for row in run_rows()] == ["succeeded", "succeeded"]


def test_a_rematch_row_that_cannot_be_written_is_500_and_the_guard_is_given_back(
    client: TestClient,
    captured: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that cannot be recorded must not refuse every later re-match."""
    real_session_scope = db.session_scope

    def the_database_is_gone(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database is gone")

    monkeypatch.setattr("app.db.session_scope", the_database_is_gone)

    refusal(rematch(client), 500)

    monkeypatch.setattr("app.db.session_scope", real_session_scope)

    assert run_rows() == []
    assert captured == []
    assert rematch(client).status_code == 202  # the guard was released on that path too
    assert len(captured) == 1


# The second admission (#77): the app's own page, proven by `Origin` naming the
# request's own `Host`. One guard, `_require_admitted_caller`, is the whole rule,
# so every case below runs against both doors.

_RUN_PATHS = [
    pytest.param("/internal/tick", id="the tick"),
    pytest.param("/internal/rematch", id="the re-match"),
]

# One `Origin` and the `Host` it names: a default port omitted or spelled out, a
# non-default port, and http and https -- the scheme the container never sees.
_SAME_HOSTS = [
    pytest.param("https://jobs.example", "jobs.example", id="a default port omitted"),
    pytest.param("https://jobs.example", "jobs.example:443", id="a default port spelled out"),
    pytest.param("http://jobs.example", "jobs.example:80", id="http on its own default"),
    pytest.param("http://localhost:8000", "localhost:8000", id="a non-default port"),
]


@pytest.fixture
def any_work() -> Iterator[FakePipeline]:
    """One fake over both work providers, so a case can drive either route's door."""
    fake = FakePipeline()
    app.dependency_overrides[get_pipeline] = lambda: fake
    app.dependency_overrides[get_rematch] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_pipeline, None)
        app.dependency_overrides.pop(get_rematch, None)


def press(client: TestClient, path: str, headers: dict[str, str]) -> Response:
    """One press of a run route with exactly the headers a case chose."""
    return client.post(path, headers=headers)


@pytest.mark.parametrize(
    ("origin", "host", "expected"),
    [
        ("https://jobs.example", "jobs.example", True),
        ("https://jobs.example", "jobs.example:443", True),
        ("http://jobs.example", "jobs.example:80", True),
        ("http://localhost:8000", "localhost:8000", True),
        ("https://jobs.example", "jobs.example:8443", False),
        ("http://jobs.example:8000", "jobs.example:8001", False),
        ("https://evil.example", "jobs.example", False),
        ("null", "jobs.example", False),
        ("https://jobs.example", None, False),
        (None, "jobs.example", False),
        ("https://jobs.example:not-a-port", "jobs.example", False),
        ("https://jobs.example", "jobs.example:not-a-port", False),
    ],
)
def test_same_origin_ignores_the_scheme_and_resolves_the_default_port(
    origin: str | None, host: str | None, expected: bool
) -> None:
    """The scheme is ignored, a scheme-default port omitted equals one spelled out, else it is text."""
    assert internal._same_origin(origin, host) is expected


@pytest.mark.parametrize("path", _RUN_PATHS)
@pytest.mark.parametrize(("origin", "host"), _SAME_HOSTS)
def test_a_same_origin_press_with_no_secret_is_202_and_starts_the_work(
    client: TestClient,
    captured: list[Callable[[], None]],
    any_work: FakePipeline,
    path: str,
    origin: str,
    host: str,
) -> None:
    """The app's own page needs no secret: its `Origin` names the request's own `Host`."""
    response = press(client, path, {internal.ORIGIN_HEADER: origin, internal.HOST_HEADER: host})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert len(captured) == 1  # exactly one unit of work was handed over
    assert any_work.run_ids == []  # and it has provably not run yet
    (row,) = run_rows()
    assert row["id"] == body["id"] and row["status"] == "running"

    captured[0]()  # the work, inline, on this test's own thread

    assert any_work.run_ids == [body["id"]]
    assert run_rows()[0]["status"] == "succeeded"


@pytest.mark.parametrize("path", _RUN_PATHS)
def test_an_origin_naming_another_host_is_401_and_writes_no_row(
    client: TestClient, captured: list[Callable[[], None]], any_work: FakePipeline, path: str
) -> None:
    """A page the browser carries a press to is admitted only for its own host."""
    response = press(
        client,
        path,
        {internal.ORIGIN_HEADER: "https://evil.example", internal.HOST_HEADER: "jobs.example"},
    )

    message = refusal(response, 401)
    assert TICK_SECRET_HEADER in message  # the refusal names the header it wanted
    assert run_rows() == []  # a refusal is inert
    assert captured == []
    assert any_work.run_ids == []


@pytest.mark.parametrize("path", _RUN_PATHS)
def test_no_origin_and_no_secret_is_401(
    client: TestClient, captured: list[Callable[[], None]], any_work: FakePipeline, path: str
) -> None:
    message = refusal(press(client, path, {}), 401)
    assert TICK_SECRET_HEADER in message
    assert run_rows() == [] and captured == [] and any_work.run_ids == []


@pytest.mark.parametrize("path", _RUN_PATHS)
def test_a_valid_secret_with_no_origin_is_202(
    client: TestClient, captured: list[Callable[[], None]], any_work: FakePipeline, path: str
) -> None:
    """curl, the Actions workflow and `mcp/` send the header and see today's answers."""
    response = press(client, path, {TICK_SECRET_HEADER: SECRET})

    assert response.status_code == 202
    assert len(captured) == 1
    captured[0]()
    assert run_rows()[0]["status"] == "succeeded"


@pytest.mark.parametrize("path", _RUN_PATHS)
def test_a_same_origin_press_while_a_run_is_in_progress_is_409(
    client: TestClient, captured: list[Callable[[], None]], any_work: FakePipeline, path: str
) -> None:
    headers = {internal.ORIGIN_HEADER: "https://jobs.example", internal.HOST_HEADER: "jobs.example"}

    assert press(client, path, headers).status_code == 202
    message = refusal(press(client, path, headers), 409)

    assert "in progress" in message
    assert len(captured) == 1  # no second unit of work
    assert len(run_rows()) == 1  # no second row
    captured[0]()
    assert run_rows()[0]["status"] == "succeeded"


@pytest.mark.parametrize("path", _RUN_PATHS)
def test_a_starter_that_cannot_start_the_work_is_500_and_gives_the_guard_back(
    client: TestClient, any_work: FakePipeline, path: str
) -> None:
    """The row is written before the hand-over, so a starter that raises leaves it and frees the guard."""

    def the_thread_will_not_start(work: Callable[[], None]) -> None:
        raise RuntimeError("no thread")

    app.dependency_overrides[get_run_starter] = lambda: the_thread_will_not_start
    try:
        response = press(
            client,
            path,
            {internal.ORIGIN_HEADER: "https://jobs.example", internal.HOST_HEADER: "jobs.example"},
        )
    finally:
        app.dependency_overrides.pop(get_run_starter, None)

    refusal(response, 500)
    assert any_work.run_ids == []  # the work never ran
    assert [row["status"] for row in run_rows()] == ["running"]  # the row it wrote first
    assert not internal._run_in_progress.locked()  # the guard was given back


@pytest.mark.parametrize("path", _RUN_PATHS)
def test_with_no_configured_secret_only_the_instance_s_own_page_is_admitted(
    client: TestClient,
    captured: list[Callable[[], None]],
    any_work: FakePipeline,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """`TICK_SECRET` unset is "open to its own page only", not open to everyone."""
    monkeypatch.delenv(internal.TICK_SECRET_VARIABLE, raising=False)
    page = {internal.ORIGIN_HEADER: "https://jobs.example", internal.HOST_HEADER: "jobs.example"}

    assert press(client, path, {}).status_code == 401
    assert TICK_SECRET_HEADER in refusal(press(client, path, {TICK_SECRET_HEADER: SECRET}), 401)
    assert run_rows() == [] and captured == []

    assert press(client, path, page).status_code == 202  # its own page is still admitted
    captured[0]()
    assert run_rows()[0]["status"] == "succeeded"
