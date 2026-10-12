"""`GET /runs/{id}`: the read a client does to see what became of a tick.

A tick answers `202` with a run row while the work is still ahead of it, so this
route is how a client -- the owner's browser, and #12's UI -- reads the rest:
`running` while the run is in flight, then `succeeded` or `failed` with the
counts the run earned and the failure's message when there was one. Nothing here
is computed or invented: the body is the row's own seven columns, because the row
is the whole progress record this app keeps, and `app.models.Run` is where their
meaning is declared.

No secret is required, because reading one's own run is what the browser does and
this app has no accounts (`_docs/plan.md`). The body carries what the row holds
and nothing else -- no secret, no resume text, no database URL -- which is
exactly the `RunSummary` model below: a field that is not on the row is not on
the wire either.

`RunSummary` and `run_summary()` are also what `POST /internal/tick` answers its
`202` with, so a client that started a run reads the same shape twice and the
run's two routes cannot drift apart.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db
from app.models import Run

router = APIRouter()


class RunSummary(BaseModel):
    """One `run` row as this API renders it: its seven columns, and no derived field.

    The same body a `202` from `POST /internal/tick` carries. `finished_at` and
    `error` are absent until the run ends, and both counts are 0 from the moment
    the row is written: what the boards returned and how many matches
    `record_score` wrote (#34), never a score computed here.
    """

    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    postings_seen: int
    matches_created: int


# The refusals this route answers with, named in the contract the way
# `app.routes.watchlist` names its own.
RUN_NOT_FOUND_RESPONSE = {404: {"description": "No run has that id"}}
UNUSABLE_ID_RESPONSE = {422: {"description": "The path value is not a run id"}}
READ_RESPONSES = RUN_NOT_FOUND_RESPONSE | UNUSABLE_ID_RESPONSE


def run_summary(run: Run) -> RunSummary:
    """One run as this API renders it, read while its session is still open."""
    return RunSummary(
        id=run.id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        postings_seen=run.postings_seen,
        matches_created=run.matches_created,
    )


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
