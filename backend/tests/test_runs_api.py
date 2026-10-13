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

A `running` row a dead process left behind is finished here as well, because it is
the same row this module reads: `reap_stale_runs()` is called once from the app's
lifespan, and the cases below start the real app around a seeded `running` row to
see what it left. The row-level cases run on both engines through the shared
`engine` fixture; the startup and tick cases drive `TestClient`, the way every
other case in this file does. One subject, one file, and the reap is not a route,
so the contract does not move.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db, main
from app.main import app
from app.models import Company, Match, Posting, Resume, Run
from app.routes import internal
from app.routes.internal import get_pipeline, get_run_starter
from app.routes.runs import REAPED_RUN_ERROR, reap_stale_runs

# The eight columns of a run, and therefore the whole body this route may answer
# with. A field that is not here is a field the route invented.
RUN_FIELDS = {
    "id",
    "status",
    "started_at",
    "finished_at",
    "error",
    "postings_seen",
    "matches_created",
    "matches_emailed",
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
    assert body["matches_emailed"] is None  # no digest of this run has recorded one yet
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
        matches_emailed=2,
    )

    response = read(client, run_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["error"] == error
    assert body["postings_seen"] == 7
    assert body["matches_created"] == 3
    assert body["matches_emailed"] == 2
    assert datetime.fromisoformat(body["finished_at"]) == finished_at


# What a run's row says about its own email: `0`, a count and NULL are three
# different answers, and the row is where a client reads them (#36, `_docs/plan.md`).


def test_the_body_tells_a_run_that_emailed_nothing_from_one_that_did(engine: Engine) -> None:
    """The read serves the row's own value, and `0` is not the same answer as NULL.

    Three finished runs, seeded through the app's own session: one that emailed
    nothing, one that emailed two matches, and one that never recorded a count
    because it was written before the column existed. Each is told apart by its
    own row, and the read answers each with that row's value -- no mailer and no
    walk over `match` is involved here.

    The read is driven over the database the `engine` fixture chose, so the case
    runs on SQLite and on PostgreSQL, and the app is started around the rows the
    way `test_a_running_row_left_by_a_dead_process_is_failed_when_the_app_starts`
    starts it.
    """
    ended = datetime.now(UTC)
    emailed_nothing = seed(status="succeeded", finished_at=ended, matches_emailed=0)
    emailed_two = seed(status="succeeded", finished_at=ended, matches_emailed=2)
    never_recorded = seed(status="succeeded", finished_at=ended)

    assert [run_row(run_id).matches_emailed for run_id in (emailed_nothing, never_recorded)] == [
        0,
        None,
    ]

    with TestClient(app) as read_client:
        answered = {
            run_id: read(read_client, run_id).json()["matches_emailed"]
            for run_id in (emailed_nothing, emailed_two, never_recorded)
        }

    assert answered == {emailed_nothing: 0, emailed_two: 2, never_recorded: None}


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
    """The body is the row's eight columns: nothing private can ride along on it."""
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


# The reap: what a dead process left in `running`, and what it must leave alone.


def run_row(run_id: int) -> Run:
    """One `run` row, read through the app's own session and detached for the caller."""
    with db.session_scope() as session:
        row = session.get(Run, run_id)
        assert row is not None
        session.expunge(row)
        return row


def run_rows() -> list[dict[str, Any]]:
    """Every `run` row as its own column values, in id order, so a stray write is visible."""
    with db.session_scope() as session:
        columns = [column.name for column in Run.__table__.columns]
        return [
            {name: getattr(row, name) for name in columns}
            for row in session.scalars(select(Run).order_by(Run.id))
        ]


def table_counts() -> dict[str, int]:
    """How many rows each of the four tables the reap must not write now holds."""
    with db.session_scope() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (Company, Posting, Match, Resume)
        }


def seed_a_posting_a_match_and_a_resume() -> None:
    """One row in each table that is not `run`.

    "No other table is written" needs rows in those tables to be worth asserting.
    """
    with db.session_scope() as session:
        company = Company(name="Acme", slug="acme")
        session.add(company)
        session.flush()
        posting = Posting(
            company_id=company.id,
            external_id="1",
            title="Backend Engineer",
            url="https://example.test/1",
        )
        session.add(posting)
        session.flush()
        session.add(Match(posting_id=posting.id, score=80, reason="a fit"))
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text="Synthetic resume text, written for this test and for no person",
                stored_path="/tmp/synthetic-resume.txt",
            )
        )


def test_the_reap_finishes_a_running_row_with_a_reason_an_end_and_the_counts_it_left(
    engine: Engine,
) -> None:
    run_id = seed(status="running", postings_seen=5, matches_created=2, matches_emailed=3)

    assert reap_stale_runs() == 1

    row = run_row(run_id)
    assert row.id == run_id  # still the row that was seeded: not deleted, not replaced
    assert row.status == "failed"
    assert row.error == REAPED_RUN_ERROR
    assert row.finished_at is not None
    assert row.finished_at >= row.started_at  # an aware UTC instant, never before the start
    # The record of how far it got, the digest's own count included: the reap
    # writes the end and nothing else (#35), and the end of a run writes no count
    # of its own (#36).
    assert (row.postings_seen, row.matches_created, row.matches_emailed) == (5, 2, 3)


def test_a_running_row_left_by_a_dead_process_is_failed_when_the_app_starts(
    engine: Engine,
) -> None:
    """Seeded before the app starts, and read back as `failed` once it has."""
    run_id = seed(status="running")

    with TestClient(app) as client:
        body = read(client, run_id).json()

    assert body["status"] == "failed"
    assert body["error"] == REAPED_RUN_ERROR
    assert datetime.fromisoformat(body["finished_at"]) >= datetime.fromisoformat(
        body["started_at"]
    )


def test_the_reap_touches_no_finished_row_and_no_other_table(engine: Engine) -> None:
    ended = datetime.now(UTC)
    seed(status="succeeded", finished_at=ended, postings_seen=7, matches_created=3)
    seed(status="failed", finished_at=ended, error="the board refused: acme")
    stale = seed(status="running")
    seed_a_posting_a_match_and_a_resume()
    before = run_rows()
    counts = table_counts()

    assert reap_stale_runs() == 1

    after = run_rows()
    # A finished run keeps its status, its end, its error and its counts.
    assert after[:2] == before[:2]
    (reaped,) = after[2:]
    assert reaped["id"] == stale
    assert reaped["status"] == "failed"
    assert reaped["error"] == REAPED_RUN_ERROR
    assert reaped["finished_at"] >= reaped["started_at"]
    assert table_counts() == counts


def test_a_second_process_start_reaps_again_and_finds_nothing_left_to_reap(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(status="running")

    with TestClient(app):
        pass  # the first start finishes the row

    counts: list[int] = []
    real_reap = main.reap_stale_runs

    def counted_reap() -> int:
        """The reap, with the row count this start saw kept where this test can read it."""
        counts.append(real_reap())
        return counts[-1]

    monkeypatch.setattr(main, "reap_stale_runs", counted_reap)
    with TestClient(app):
        pass  # so the second start has nothing to reap

    assert counts == [0]
    assert [row["status"] for row in run_rows()] == ["failed"]


def test_a_tick_after_the_reap_is_accepted_and_the_reaped_row_keeps_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv(internal.TICK_SECRET_VARIABLE, SECRET)
    db.init_db()  # the process that died had already created the tables
    stale = seed(status="running")
    work: list[Callable[[], None]] = []
    app.dependency_overrides[get_run_starter] = lambda: work.append
    app.dependency_overrides[get_pipeline] = lambda: lambda run_id: None
    try:
        with TestClient(app) as client:
            assert run_row(stale).status == "failed"  # the startup reap, before any request

            response = client.post(
                "/internal/tick", headers={internal.TICK_SECRET_HEADER: SECRET}
            )

            assert response.status_code == 202
            assert response.json()["status"] == "running"
            assert response.json()["id"] != stale  # the reaped row's id is not reused
            work[0]()  # the work this tick was handed, run inline on this test's thread
    finally:
        app.dependency_overrides.pop(get_run_starter, None)
        app.dependency_overrides.pop(get_pipeline, None)
        if internal._run_in_progress.locked():  # the guard is the process's, not this test's
            internal._run_in_progress.release()

    assert [row["status"] for row in run_rows()] == ["failed", "succeeded"]
