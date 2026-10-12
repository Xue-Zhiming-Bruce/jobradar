"""`GET /runs/{id}`: the read a client does to see what became of a tick.

Every test drives the real app through `TestClient` -- no server, no port -- with
`DATABASE_URL` pointed at a temporary SQLite file, so the suite leaves no
`backend/data/jobradar.db` behind and `git status --short` stays empty.

The route is the read side of the tick, so what it must not do is the point: it
does not require the tick's secret (reading one's own run is what the owner's
browser does, and this app has no accounts), it invents no field that is not on
the `run` row, and it answers `404` for an id that is not there rather than an
empty body or a row made up on the spot. A `run` row is seeded directly through
the app's own session where a run's end state is what the test is about; the
`202` and the `GET` are then compared against each other so the two routes that
answer `RunSummary` cannot drift apart.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.models import Run

# The seven columns of a run, and therefore the whole body this route may answer
# with. A field that is not here is a field the route invented.
RUN_FIELDS = {
    "id",
    "status",
    "started_at",
    "finished_at",
    "error",
    "postings_seen",
    "matches_created",
}

# Values that must not reach the wire: the shared secret a tick would carry, and
# the two kinds of private value the row must never have held in the first place.
SECRET = "tick-secret-sentinel-7f4c1d9ab38e"
RESUME_TEXT = "RESUME-SENTINEL-TEXT-THAT-MUST-NOT-BE-SERVED"
DATABASE_URL = "postgresql+psycopg://user:password@db.example.test/jobradar"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with its database pointed at this test's temporary SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    with TestClient(app) as test_client:
        yield test_client


def seed(**values: Any) -> int:
    """One `run` row written straight through the app's own session, and its id."""
    with db.session_scope() as session:
        run = Run(**values)
        session.add(run)
        session.flush()
        return run.id


def read(client: TestClient, run_id: object) -> Any:
    """One read of a run, with no header at all: the route asks for no secret."""
    return client.get(f"/runs/{run_id}")


def test_a_running_run_reads_back_exactly_as_its_row_holds_it(client: TestClient) -> None:
    run_id = seed(status="running")

    response = read(client, run_id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RUN_FIELDS
    assert body["id"] == run_id
    assert body["status"] == "running"
    assert body["finished_at"] is None
    assert body["error"] is None
    assert body["postings_seen"] == 0
    assert body["matches_created"] == 0
    assert datetime.fromisoformat(body["started_at"]) is not None


@pytest.mark.parametrize(
    ("status", "error"),
    [("succeeded", None), ("failed", "the board refused: acme")],
    ids=["succeeded", "failed"],
)
def test_a_finished_run_reads_its_end_state_and_counts(
    client: TestClient, status: str, error: str | None
) -> None:
    finished_at = datetime.now(UTC)
    run_id = seed(
        status=status,
        finished_at=finished_at,
        error=error,
        postings_seen=7,
        matches_created=3,
    )

    response = read(client, run_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["error"] == error
    assert body["postings_seen"] == 7
    assert body["matches_created"] == 3
    assert datetime.fromisoformat(body["finished_at"]) == finished_at


def test_the_read_answers_for_the_id_it_was_asked_for(client: TestClient) -> None:
    first = seed(status="succeeded")
    second = seed(status="failed", error="a board was down")

    assert read(client, first).json()["id"] == first
    assert read(client, second).json()["id"] == second


def test_an_id_with_no_row_is_404(client: TestClient) -> None:
    run_id = seed(status="succeeded")

    response = read(client, run_id + 1)

    assert response.status_code == 404
    assert response.json()["detail"], "a refusal must say what it refused"


@pytest.mark.parametrize("value", ["abc", "1.5", "one"])
def test_a_path_value_that_is_not_a_run_id_is_422(client: TestClient, value: str) -> None:
    response = client.get(f"/runs/{value}")

    assert response.status_code == 422


def test_reading_a_run_needs_no_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secret guards starting a run, not reading one; and no secret is even configured."""
    monkeypatch.delenv("TICK_SECRET", raising=False)
    run_id = seed(status="running")

    response = read(client, run_id)

    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_the_body_carries_no_secret_no_resume_text_and_no_database_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body is the row's seven columns: nothing private can ride along on it."""
    monkeypatch.setenv("TICK_SECRET", SECRET)
    run_id = seed(status="failed", error="the board refused: acme")

    response = read(client, run_id)

    assert response.status_code == 200
    for forbidden in (SECRET, RESUME_TEXT, DATABASE_URL):
        assert forbidden not in response.text
    assert set(response.json()) == RUN_FIELDS


def test_the_body_is_exactly_the_run_summary_the_api_declares(client: TestClient) -> None:
    """The read renders `RunSummary`, so a field not on that model is not on the wire."""
    from app.routes.runs import RunSummary

    run_id = seed(status="running")

    body = read(client, run_id).json()

    assert set(body) == set(RunSummary.model_fields)
    assert set(body) == RUN_FIELDS
