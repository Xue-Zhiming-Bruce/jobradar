"""`POST /internal/tick`: one shared-header call starts one digest run, and answers at once.

The header is `X-Tick-Secret` and the value it is compared against is the
`TICK_SECRET` in the environment; both are spelled once, as the constants below.
Neither is read at import: the variable is read per request the way
`app.services.resume.uploads_dir()` reads its own, so a deployment sets it in the
environment and a test sets it with `monkeypatch.setenv`. A request without the
header, with an empty one or with a different one is `401`, and a process whose
`TICK_SECRET` is unset or empty refuses every tick -- this endpoint is never open
by default, and an empty variable is not a password. The two values are compared
with `hmac.compare_digest`, never with `==`, and neither is logged, stored on a
row, echoed in a detail or answered with: the value is compared and dropped.

A valid tick does four things and nothing else, in this order. It claims the
guard without blocking, writes the run row (`running`, both counts 0), hands the
work to `get_run_starter()` and answers `202` with that row. The work itself is
`get_pipeline()(run_id)`, and it runs somewhere that is not this request's
thread, because the pipeline (#34) fetches a board per company and scores each
survivor: a request that waited for it would outlive the client's timeout, which
is the shape recorded in `_docs/decisions/2026-10-11-deploy-and-repo.md`. What a
client reads instead is `GET /runs/{id}` (`app.routes.runs`), the same body the
`202` carries.

Three seams, all zero-argument `Depends` providers declared the way `get_source`
is in `app.routes.watchlist`: `get_run_starter()` returns the callable that takes
the work off this thread, `get_pipeline()` returns the work itself -- a callable
whose only argument is the run's `id` -- and `get_mailer()` returns the mailer
#10's digest sends through. The suite replaces all three with
`app.dependency_overrides[...]`, so no test needs a board, a model, a key, a
credential or a socket, and no test depends on timing. #34 and #10 are what the
second seam returns: the walk over the watchlist in `app.services.pipeline`,
which fetches a board per company, stores what it finds, scores each survivor
and counts what it did, and then #10's digest, which emails the matches that
qualify and marks them notified. The order matters: a pipeline that raises sends
no email at all, so the matches it scored before a board failure go out with the
next successful run.

The row's seven columns are the whole progress record, and this module is the
only writer of `status`, `finished_at` and `error`: the row is inserted `running`
with both counts 0, the pipeline writes the counts as the run earns them, and the
run's end writes `succeeded` or `failed` with the failure's own message in
`error`. The request answered `202` long before any of that, so the row is where
a failure has to be readable -- never a traceback to a client that is gone.

The guard against two runs at once is this process's, not the database's. The
decision doc keeps the poller in the one container, so a module-level lock is the
whole rule: `acquire(blocking=False)` makes the second tick a `409` rather than a
wait, and the work releases it in a `finally`, whichever way it ended. A `run` row
another process left in `running` deliberately does not refuse a tick: a process
that dies mid-run would otherwise answer `409` to every later tick until someone
edited the database by hand, and closing those rows properly is #35.
"""

import hmac
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app import db
from app.matching.scorer import LlmScorer
from app.models import Run
from app.routes.runs import RunSummary, run_summary
from app.services.digest import Mailer, SmtpMailer, send_digest
from app.services.pipeline import run_pipeline
from app.sources.greenhouse import fetch

logger = logging.getLogger(__name__)

router = APIRouter()

# The header a caller sends and the variable it is compared against, each spelled
# once. The route reads this variable and no other module does.
TICK_SECRET_HEADER = "X-Tick-Secret"
TICK_SECRET_VARIABLE = "TICK_SECRET"

# The vocabulary `run.status` accepts in `app.models.Run`, spelled the way the
# check constraint spells it: this module writes `running` and one of the two.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

# One run at a time in this process, claimed without blocking. Held for the whole
# life of a run and released in `_run`'s `finally`.
_run_in_progress = threading.Lock()

# The two seams #34 plugs into, named as types so a provider and a test's override
# cannot disagree about their shape.
RunStarter = Callable[[Callable[[], None]], None]
Pipeline = Callable[[int], None]

# The answers this route gives, named in the contract the way
# `app.routes.watchlist` names its own.
STARTED_RESPONSE = {202: {"description": "A run started: the run row this request wrote"}}
UNAUTHORISED_RESPONSE = {
    401: {"description": "The request's shared internal header is missing, empty or wrong"}
}
BUSY_RESPONSE = {409: {"description": "A run is already in progress in this process"}}
UNRECORDED_RESPONSE = {
    500: {"description": "The run could not be recorded, or its work could not be started"}
}
TICK_RESPONSES = (
    STARTED_RESPONSE | UNAUTHORISED_RESPONSE | BUSY_RESPONSE | UNRECORDED_RESPONSE
)


def tick_secret() -> str:
    """`TICK_SECRET`, or an empty string when it is unset or empty.

    Read per request rather than at import, so a deployment changes it by setting
    the variable and an unset one is a refusal rather than a variable nobody read.
    """
    return os.environ.get(TICK_SECRET_VARIABLE, "").strip()


def get_run_starter() -> RunStarter:
    """The one seam between this route and where a run executes: the provider tests replace.

    Zero arguments, so it carries no configuration, and resolved per request by
    `Depends`, so the suite installs `app.dependency_overrides[get_run_starter]`
    without a board, a model or a key. It returns `_start_on_a_daemon_thread`, the
    callable with the starter's shape: the unit of work in, this thread back.
    """
    return _start_on_a_daemon_thread


def get_mailer() -> Mailer:
    """The third seam: the one place a `SmtpMailer` is constructed.

    Zero arguments, so it carries no configuration, and resolved per request by
    `Depends` -- through `get_pipeline`, whose parameter it is -- so the suite
    installs `app.dependency_overrides[get_mailer]` and #10's digest then sends
    through a fake without a host, a port or a credential. It is the mailer
    *type* that is chosen here: which relay, sender and recipient a real send
    uses is `app.services.digest`'s own configuration, read per send.
    """
    return SmtpMailer()


def get_pipeline(mailer: Mailer = Depends(get_mailer)) -> Pipeline:
    """The second seam: the run's work, a callable taking the run's `id`.

    It returns the real walk over the watchlist (#34) followed by the digest
    (#10), wired here and in no other module: `greenhouse.fetch` as the source, a
    fresh `LlmScorer` per run, because a scorer instance counts the postings it
    was offered and that count is the run's cap, and the mailer its own provider
    hands it. `Depends` resolves this provider on every request, so a later tick
    gets a scorer with a cap of its own.

    The mailer arrives as a `Depends` default rather than as a required argument,
    so this provider is still callable with no arguments: #9's tests call
    `get_pipeline()` to inspect the shape of the work it returns.
    """
    scorer = LlmScorer()

    def pipeline(run_id: int) -> None:
        """One run's work, in the shape the starter and the row lifecycle take."""
        run_pipeline(run_id, source=fetch, scorer=scorer)
        send_digest(mailer)

    return pipeline


def _start_on_a_daemon_thread(work: Callable[[], None]) -> None:
    """The default starter: give the work a thread of its own and return at once.

    A daemon thread, because the process outliving the run is the ordinary case --
    a container restarted mid-run has nowhere to write anyway -- and nothing here
    joins it: the request has already answered `202`, and the row is what a client
    reads. This one line is what keeps the endpoint's promise, instead of the work
    running inside the request where a client waits for the whole pipeline.
    """
    threading.Thread(target=work, daemon=True).start()


def _require_tick_secret(provided: str | None) -> None:
    """Refuse the caller who did not send the configured value, and name the header.

    The comparison is over both values' bytes, so it is constant-time rather than
    stopping at the first difference, and a header a client sent as non-ASCII text
    is a wrong value rather than an exception. The refusal is inert: it logs the
    header's name, never either value, and it writes no row.
    """
    configured = tick_secret()
    offered = provided or ""
    if not configured or not hmac.compare_digest(
        offered.encode("utf-8"), configured.encode("utf-8")
    ):
        logger.warning(
            "Refused a tick: the %s header is missing, empty or wrong", TICK_SECRET_HEADER
        )
        raise HTTPException(
            status_code=401,
            detail=f"The {TICK_SECRET_HEADER} header is missing, empty or wrong",
        )


def _claim_run() -> RunSummary:
    """Write this run's row, `running` and empty, and read it back with its id.

    One session, flushed before it returns, so the id and both counts exist before
    the work is handed over: a client that reads the `202` can read the run, and
    the work never runs without a row to report on.
    """
    with db.session_scope() as session:
        run = Run(status=RUNNING)
        session.add(run)
        session.flush()  # the row's id, and the columns' defaults, exist here
        return run_summary(run)  # read before the commit expires it


def _without_the_secret(message: str | None) -> str | None:
    """The failure's message with the configured secret taken out of it.

    The row's `error` is the pipeline's own free text, and `GET /runs/{id}` serves
    it back to a client, so a message that somehow quoted the request's secret
    would put it in a column, a response and a log. The configured value is
    replaced before anything is written; the rest of the message is unchanged.
    """
    secret = tick_secret()
    if message is None or not secret:
        return message
    return message.replace(secret, "[redacted]")


def _end_run(run_id: int, status: str, error: str | None) -> None:
    """Write how the run ended on its row: the status, the end and the message.

    The counts are left alone: the pipeline wrote them as the run earned them, so
    a run that failed after scoring three postings goes on saying three. A row
    that is no longer there -- deleted while the run was in flight -- is nothing
    to write and not a failure.
    """
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.error = _without_the_secret(error)


def _run(run_id: int, pipeline: Pipeline) -> None:
    """Run the pipeline for that row, record how it ended, and free the guard.

    Everything the pipeline raises lands on the row instead of on a thread nobody
    is watching: the request answered `202` a long time ago, so the row is where a
    failure has to be readable, and the message stored is the failure's own -- the
    pipeline's text, never a traceback. The release is in a `finally` after that
    write, so no path through a run leaves the guard held.
    """
    try:
        pipeline(run_id)
    except Exception as error:  # the work's whole failure family is the row's business
        _end_run(run_id, FAILED, str(error))
    else:
        _end_run(run_id, SUCCEEDED, None)
    finally:
        logger.info("Run %s is over", run_id)
        _run_in_progress.release()


@router.post(
    "/internal/tick",
    response_model=RunSummary,
    status_code=202,
    tags=["internal"],
    summary="Start one digest run",
    responses=TICK_RESPONSES,
)
def tick(
    request: Request,
    starter: Annotated[RunStarter, Depends(get_run_starter)],
    pipeline: Annotated[Pipeline, Depends(get_pipeline)],
) -> RunSummary:
    """Start one run, and answer `202` with the row this request wrote.

    The caller is checked first, then the guard is claimed, then the row is
    written, then the work is handed over -- and the answer carries that row in
    status `running` rather than waiting for the work, which is what
    `GET /runs/{id}` is for. Nothing before the hand-over can leave the guard
    held: a row that cannot be written, like work that cannot be started, is a
    `500` and gives the guard back, so the endpoint recovers instead of refusing
    every later tick.
    """
    _require_tick_secret(request.headers.get(TICK_SECRET_HEADER))

    if not _run_in_progress.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="A run is already in progress in this process"
        )

    try:
        run = _claim_run()
    except Exception as error:
        _run_in_progress.release()
        raise HTTPException(
            status_code=500, detail="The run could not be recorded in the database"
        ) from error

    try:
        starter(lambda: _run(run.id, pipeline))
    except Exception as error:
        _run_in_progress.release()
        raise HTTPException(status_code=500, detail="The run could not be started") from error

    logger.info("Started run %s", run.id)
    return run
