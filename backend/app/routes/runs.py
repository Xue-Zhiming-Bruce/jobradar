"""`GET /runs/{id}`: the read a client does to see what became of a tick.

A tick answers `202` with a run row while the work is still ahead of it, so this
route is how a client -- the owner's browser, and #12's UI -- reads the rest:
`running` while the run is in flight, then `succeeded` or `failed` with the
counts the run earned and the failure's message when there was one. Nothing here
is computed or invented: the body is the row's own ten columns plus the per-company
outcomes of the run, because the row is the whole progress record this app keeps,
and `app.models.Run` and `app.models.RunCompany` are where their meaning is
declared.

No secret is required, because reading one's own run is what the browser does and
this app has no accounts (`_docs/plan.md`). The body carries what the row holds
and nothing else -- no secret, no resume text, no database URL -- which is
exactly the `RunSummary` model below: a field that is not on the row, nor in the
`run_company` rows the row's id names, is not on the wire either.

`RunSummary` and `run_summary()` are also what `POST /internal/tick` answers its
`202` with, so a client that started a run reads the same shape twice and the
run's two routes cannot drift apart.

A tick's guard is the process's (`app.routes.internal`), so a process that is
redeployed or killed mid-run leaves its row `running` and finishes nothing. That
row is reaped here, beside the read, because it is the same row and the same
module: `reap_stale_runs()` is the one writer of a terminal status on a row no
process is running, it is called once from `app.main`'s lifespan, and it is not
a route -- the app finishes those rows while it starts, before it serves.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import db
from app.models import Company, Run, RunCompany

router = APIRouter()


class RunCompanyOutcome(BaseModel):
    """One company's outcome in one run, as this API renders it.

    One entry per company the run's walk attempted, in the order it attempted them
    (#59): `outcome` is `'fetched'`, `'not_modified'` or `'failed'`, and `error` is
    the failure's own message on a `'failed'` row and NULL on every other. The slug
    is the company's own, read through the key the row holds, so a client reads
    which board failed without a second request. A company the run's budget never
    reached has no entry here: #56's `companies_not_fetched` counts those.
    """

    company_id: int
    slug: str
    outcome: str
    error: str | None


class RunSummary(BaseModel):
    """One `run` row as this API renders it: its ten columns and its per-company outcomes.

    The same body a `202` from `POST /internal/tick` carries. `finished_at` and
    `error` are absent until the run ends, and the two counts the walk earns are
    0 from the moment the row is written: what the boards returned and how many
    matches `record_score` wrote (#34), never a score computed here.
    `matches_emailed` is the third: how many matches this run's own digest emailed
    (#36), NULL until that digest records it -- which is the honest value for a
    run still in flight, a row written before the column existed, and a run whose
    send failed, and a different answer from a run that really did email none.
    `companies_not_fetched` is NULL for a run the board-call budget did not stop,
    which includes a run still in flight, and holds the count of active companies
    the walk left for a later run when it did (#56). `postings_not_rematched` is
    the same shape for a re-match: NULL for every tick and every row written
    before the column existed, `0` for a re-match that decided every candidate it
    found, and a positive count of the passers its cap left for a later run (#64).
    `company_outcomes` is one entry per company the run attempted and is empty for
    a run that has not attempted one yet -- the `202`'s fresh row -- so a client
    reads the same shape from both routes at every point in a run's life.
    """

    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    postings_seen: int
    matches_created: int
    matches_emailed: int | None
    companies_not_fetched: int | None
    postings_not_rematched: int | None
    company_outcomes: list[RunCompanyOutcome]


# The refusals this route answers with, named in the contract the way
# `app.routes.watchlist` names its own.
RUN_NOT_FOUND_RESPONSE = {404: {"description": "No run has that id"}}
UNUSABLE_ID_RESPONSE = {422: {"description": "The path value is not a run id"}}
READ_RESPONSES = RUN_NOT_FOUND_RESPONSE | UNUSABLE_ID_RESPONSE

# The most runs one read answers: `?limit=` is bounded by this, so the read
# cannot be asked for an unbounded page. The same bound `app.routes.matches`
# puts on its own `?limit=`, spelled as this module's own constant.
RUNS_MAX_LIMIT = 200

# What `?limit=` is when the caller names none: the page the on-call check and
# the runs screen read by default. Constant beside `RUNS_MAX_LIMIT` rather than
# configuration: it is not a deployment's to change.
RUNS_DEFAULT_LIMIT = 50


def run_summary(run: Run) -> RunSummary:
    """One run as this API renders it, read while its session is still open.

    The per-company outcomes are read through that same session, in the order the
    walk recorded them, so the run's own row and the companies it attempted are one
    read. Both callers hold a live session: `read_run` reads a stored row and
    `_claim_run` reads the row it just flushed, whose outcome list is empty because
    no company has been attempted yet.
    """
    session = Session.object_session(run)
    outcomes = [
        RunCompanyOutcome(
            company_id=row.company_id, slug=slug, outcome=row.outcome, error=row.error
        )
        for row, slug in session.execute(
            select(RunCompany, Company.slug)
            .join(Company, RunCompany.company_id == Company.id)
            .where(RunCompany.run_id == run.id)
            .order_by(RunCompany.id)
        )
    ]
    return RunSummary(
        id=run.id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        postings_seen=run.postings_seen,
        matches_created=run.matches_created,
        matches_emailed=run.matches_emailed,
        companies_not_fetched=run.companies_not_fetched,
        postings_not_rematched=run.postings_not_rematched,
        company_outcomes=outcomes,
    )


# What a reaped row is finished with. One fixed sentence, and it names no
# resume, no credential and no database URL: `GET /runs/{id}` serves `error`
# back to a client, and this repository is public.
REAPED_RUN_ERROR = "The process ended before the run finished"


def reap_stale_runs() -> int:
    """Finish every run row a dead process left in `running`, and answer how many there were.

    One statement in one `db.session_scope()`, written the way
    `app.services.resume.record_active_resume` retires its previous active row: an
    `UPDATE` whose `WHERE` names the only rows a reap may touch, so a finished run
    -- `succeeded` or `failed`, with the `finished_at`, the `error` and the counts
    it earned -- is not written at all. It is called once, from `app.main`'s
    lifespan after `init_db()`, before the app serves any request: a tick's guard
    is the process's, so at startup a `running` row is by definition a row no
    process is running.

    The row ends `failed` with `REAPED_RUN_ERROR` and `finished_at` in the aware
    UTC form `models.UTCDateTime` requires. Its counts are left exactly as the
    dead run wrote them -- they are the record of how far it got -- and no other
    table is read or written. The row keeps its `id`: nothing is deleted and no
    row is reused.

    The return value is the `UPDATE`'s row count, which is what a caller -- the
    lifespan, and the test that starts the app twice -- reads to tell a reap that
    finished rows from one that found nothing left to finish.
    """
    with db.session_scope() as session:
        return session.execute(
            update(Run)
            .where(Run.status == "running")
            .values(
                status="failed",
                finished_at=datetime.now(UTC),
                error=REAPED_RUN_ERROR,
            )
        ).rowcount


@router.get(
    "/runs",
    response_model=list[RunSummary],
    tags=["runs"],
    summary="List digest runs, newest first",
)
def list_runs(limit: int = Query(RUNS_DEFAULT_LIMIT, ge=1, le=RUNS_MAX_LIMIT)) -> list[RunSummary]:
    """The newest `limit` runs, newest first, and `[]` when the repository holds none.

    One statement, ordered `started_at` descending with the id descending under it,
    so two runs that started in the same instant -- which `started_at`'s default
    makes possible -- still have one order and the newest N is never an arbitrary N.
    `limit` defaults to `RUNS_DEFAULT_LIMIT` and is bounded by `RUNS_MAX_LIMIT`; `0`,
    a negative value or one above the bound is refused as `422` before this function
    runs. The body is the same `RunSummary` `GET /runs/{id}` answers, rendered by the
    same `run_summary`, so the two routes cannot drift apart.
    """
    statement = select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(limit)
    with db.session_scope() as session:
        return [run_summary(run) for run in session.scalars(statement)]


@router.get(
    "/runs/{id}",
    response_model=RunSummary,
    tags=["runs"],
    summary="Read one digest run",
    responses=READ_RESPONSES,
)
def read_run(id: int) -> RunSummary:
    """The run with that id, in whichever status it is in, exactly as the row holds it.

    The id is a path parameter and an `int`: a value that is not one never
    reaches this function and is refused by the path's own validation as `422`.
    An id that finds no row is a `404` and invents nothing.
    """
    with db.session_scope() as session:
        run = session.get(Run, id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"There is no run with id {id}")
        return run_summary(run)
