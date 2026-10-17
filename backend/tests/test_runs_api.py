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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db, main
from app.main import app
from app.models import Company, Match, Posting, Resume, Run, RunCompany
from app.routes import internal
from app.routes.internal import get_pipeline, get_run_starter
from app.routes.runs import RUNS_DEFAULT_LIMIT, RUNS_MAX_LIMIT, REAPED_RUN_ERROR, reap_stale_runs

# The ten columns of a run plus the per-company outcomes it recorded, and
# therefore the whole body this route may answer with. A field that is not here is
# a field the route invented.
RUN_FIELDS = {
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
    assert body["companies_not_fetched"] is None  # and no budget stopped it yet
    assert body["postings_not_rematched"] is None  # and it is a tick, not a re-match
    assert body["company_outcomes"] == []  # and no company has been attempted yet
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


# What a run's row says about the companies its outbound budget left for a later
# run: a count, and NULL for every walk the bound did not stop (#56).


def test_the_body_tells_a_run_the_budget_stopped_from_one_that_reached_the_end(
    engine: Engine,
) -> None:
    """A count and a NULL are two different answers, and the read serves the row's own value.

    Two finished runs, seeded through the app's own session: one the board-call
    budget stopped after two of five companies, and one that walked its whole
    watchlist. Each is told apart by its own row -- no board and no walk is
    involved -- and the read answers each with that row's value.
    """
    ended = datetime.now(UTC)
    stopped = seed(status="succeeded", finished_at=ended, companies_not_fetched=3)
    reached_the_end = seed(status="succeeded", finished_at=ended)

    assert [run_row(run_id).companies_not_fetched for run_id in (stopped, reached_the_end)] == [
        3,
        None,
    ]

    with TestClient(app) as read_client:
        answered = {
            run_id: read(read_client, run_id).json()["companies_not_fetched"]
            for run_id in (stopped, reached_the_end)
        }

    assert answered == {stopped: 3, reached_the_end: None}


# What a re-match's own model cap left: NULL for a tick, `0` for a pass that
# decided every candidate, a count for a truncated one -- three readings, and the
# read serves the row's own value (#64).


def test_the_body_tells_a_re_match_that_left_nothing_from_one_that_left_some_and_a_tick(
    engine: Engine,
) -> None:
    """Three finished runs, seeded through the app's own session: a tick's NULL, a re-match's
    `0`, and a re-match that left three passers. No re-match is run -- the column is the row's own.
    """
    ended = datetime.now(UTC)
    tick = seed(status="succeeded", finished_at=ended)
    decided_everything = seed(status="succeeded", finished_at=ended, postings_not_rematched=0)
    left_three = seed(status="succeeded", finished_at=ended, postings_not_rematched=3)

    assert (
        run_row(tick).postings_not_rematched,
        run_row(decided_everything).postings_not_rematched,
    ) == (None, 0)

    with TestClient(app) as read_client:
        answered = {
            run_id: read(read_client, run_id).json()["postings_not_rematched"]
            for run_id in (tick, decided_everything, left_three)
        }

    assert answered == {tick: None, decided_everything: 0, left_three: 3}


# What a run recorded about each company it attempted: an outcome per company, and
# the failure's own message on the company it names (#59).


def test_a_partly_failed_run_is_served_with_the_company_that_failed_and_why(
    client: TestClient,
) -> None:
    """The failed board by slug with its own message, and the board that answered beside it."""
    run_id = seed(status="failed", error="1 of 2 companies failed: 'beta'")
    with db.session_scope() as session:
        answered = Company(name="Acme", slug="acme")
        failed = Company(name="Beta", slug="beta")
        session.add_all([answered, failed])
        session.flush()
        session.add(RunCompany(run_id=run_id, company_id=answered.id, outcome="fetched"))
        session.add(
            RunCompany(
                run_id=run_id,
                company_id=failed.id,
                outcome="failed",
                error="The board for company 'beta' could not be fetched: HTTP 503",
            )
        )
        answered_id, failed_id = answered.id, failed.id

    body = read(client, run_id).json()

    assert body["status"] == "failed"
    assert body["company_outcomes"] == [
        {"company_id": answered_id, "slug": "acme", "outcome": "fetched", "error": None},
        {
            "company_id": failed_id,
            "slug": "beta",
            "outcome": "failed",
            "error": "The board for company 'beta' could not be fetched: HTTP 503",
        },
    ]


def test_a_run_where_nothing_failed_is_served_with_no_failed_company(client: TestClient) -> None:
    """Every attempted company is on the wire, and not one of them reads as a failure."""
    run_id = seed(status="succeeded")
    with db.session_scope() as session:
        company = Company(name="Acme", slug="acme")
        session.add(company)
        session.flush()
        session.add(RunCompany(run_id=run_id, company_id=company.id, outcome="not_modified"))
        company_id = company.id

    body = read(client, run_id).json()

    assert body["status"] == "succeeded"
    assert body["company_outcomes"] == [
        {"company_id": company_id, "slug": "acme", "outcome": "not_modified", "error": None}
    ]
    assert [row for row in body["company_outcomes"] if row["outcome"] == "failed"] == []


def test_the_read_answers_for_the_id_it_was_asked_for(client: TestClient) -> None:
    first = seed(status="succeeded")
    second = seed(status="failed", error="a board was down")

    assert read(client, first).json()["id"] == first
    assert read(client, second).json()["id"] == second


# `GET /runs`: the newest N runs, newest first, as #92's AC1 asks.


def runs(client: TestClient, query: str = "") -> list[dict[str, Any]]:
    """One read of the runs list, and its status code pinned by the caller's own assertion."""
    response = client.get(f"/runs{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_runs_read_answers_newest_first_by_started_at_then_id(client: TestClient) -> None:
    """`started_at` descending, and the id descending under a shared instant.

    Two runs that started in the same instant -- which `started_at`'s default
    makes possible -- must still have one order, so the newer id is first.
    """
    earlier = seed(status="succeeded", started_at=datetime(2026, 10, 1, 9, tzinfo=UTC))
    later = seed(status="failed", started_at=datetime(2026, 10, 1, 11, tzinfo=UTC))
    tie_low = seed(status="succeeded", started_at=datetime(2026, 10, 1, 10, tzinfo=UTC))
    tie_high = seed(status="succeeded", started_at=datetime(2026, 10, 1, 10, tzinfo=UTC))

    body = runs(client)

    assert [run["id"] for run in body] == [later, max(tie_low, tie_high), min(tie_low, tie_high), earlier]
    assert set(body[0]) == RUN_FIELDS


def test_the_runs_read_defaults_to_the_fifty_newest(client: TestClient) -> None:
    seeded = [
        seed(status="succeeded", started_at=datetime(2026, 10, 1, 9, tzinfo=UTC) + timedelta(minutes=i))
        for i in range(RUNS_DEFAULT_LIMIT + 5)
    ]

    body = runs(client)

    assert len(body) == RUNS_DEFAULT_LIMIT
    assert [run["id"] for run in body] == list(reversed(seeded))[:RUNS_DEFAULT_LIMIT]


def test_a_limit_at_the_bound_is_accepted_and_above_it_is_refused(client: TestClient) -> None:
    seed(status="succeeded")
    seed(status="succeeded", started_at=datetime(2026, 10, 1, 10, tzinfo=UTC))

    assert len(runs(client, f"?limit={RUNS_MAX_LIMIT}")) == 2
    assert len(runs(client, "?limit=1")) == 1

    for value in (RUNS_MAX_LIMIT + 1, 0, -1):
        assert client.get(f"/runs?limit={value}").status_code == 422
    assert client.get("/runs?limit=abc").status_code == 422


def test_a_repository_with_no_run_answers_an_empty_list(client: TestClient) -> None:
    assert runs(client) == []


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
    """The body is the row's ten columns and its per-company outcomes: nothing private rides along."""
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
    """How many rows each of the five tables the reap must not write now holds."""
    with db.session_scope() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (Company, Posting, Match, Resume, RunCompany)
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
