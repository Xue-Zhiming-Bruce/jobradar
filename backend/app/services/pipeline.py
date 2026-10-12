"""The work one digest run does: the walk, the fetch, the store, the gate, the score, the counts.

`run_pipeline(run_id, *, source, scorer)` is what #9's `get_pipeline()` provider
returns, and the run row the id names already exists in `running`: the run's
lifecycle, the concurrency guard and the thread are #9's, and this module touches
none of them. It writes no `status`, no `finished_at` and no `error` -- a run that
fails leaves the row as it found it and #9's `_run` records the failure, the
message included -- and it opens no socket, sends no email and reads no
configuration of its own: the source and the scorer arrive as arguments.

One run does this, in this order, for every company whose `is_active` is true, in
ascending `company.id` order:

- `source(company.slug)`, once per company, with the slug the row stores -- never
  a value rebuilt from the company's name;
- `ingest.ingest_postings(company_id, postings)` for the list that board
  returned, so every `posting` row -- the insert, the update in place, the close
  and the reopen -- is #6's, and this module writes no `posting` column;
- for each id `IngestResult.created_ids` reported, in that order: the row read
  back as a `SourcePosting` and offered to `prefilter`. A posting the prefilter
  rejects is stored through `record_rejection` and is never offered to the
  scorer; one that passes is scored exactly once, through
  `scorer.score(posting, resume_text)`, and the result it returns is stored
  through `record_score` -- a model outage's `NOT_SCORED_PREFIX` result included,
  because a result the scorer returned is a result to record and not a failure.

`run.postings_seen` is every posting the boards returned, added as each fetch
answers, and `run.matches_created` is one per `record_score` write, added as each
score lands -- a prefilter rejection is not a match and is not counted. Both are
written as they are earned rather than once at the end, so a run that fails
half-way says how far it got: every write committed in its own
`db.session_scope()`, so nothing already written is rolled back by a later
failure, and the next tick re-runs the whole thing idempotently on #6's dedupe.

Three things are deliberately not done here:

- a posting that was already stored is offered to neither the prefilter nor the
  scorer: only `created_ids` are matched. A reopened posting already has the one
  `match` row `match.posting_id` allows -- possibly a `state` the owner saved, and
  a `notified_at` a digest already set -- so re-offering it would rewrite a row
  the owner acted on. The round trip through `closed_at` stays #6's business.
- the resume's text is read once, before the first board is called, and a run
  with no active resume fails without a single request: `LlmScorer.score` raises
  for an empty resume, so fetching first would store postings that can never be
  scored. The text is user data: it goes to the scorer and into no log line, no
  row and no message.
- a board that fails fails the run. The first `SourceError` escaping a company's
  fetch aborts the walk, naming that company's slug -- and never the board's
  response body, which `app.sources.greenhouse` already keeps out of its own
  messages. The postings and matches the earlier companies earned stay written
  and the counts keep them, so a failed run costs a retry. Skipping the company
  or keeping going would be a silent miss, and a per-company column is the
  migration path #21 owns. A second source, retries, backoff and an empty-board
  guard are #28's.
"""

from collections.abc import Callable

from sqlalchemy import select

from app import db
from app.matching.prefilter import prefilter, record_rejection
from app.matching.scorer import MatchScorer, record_score
from app.models import Company, Posting, Run
from app.services import resume
from app.services.ingest import ingest_postings
from app.sources.greenhouse import SourceError, SourcePosting

# The one failure this module raises of its own, and the message a run row keeps:
# a run with nothing to score against would otherwise fetch and store postings it
# can never match.
NO_ACTIVE_RESUME = "No active resume: upload one before a run can score postings"


def run_pipeline(
    run_id: int,
    *,
    source: Callable[[str], list[SourcePosting]],
    scorer: MatchScorer,
) -> None:
    """Walk the watchlist once for the run `run_id` names, and count what it did.

    Both collaborators are arguments -- the source is the callable a board adapter
    exports and the scorer is one per run, because its cap is (`app.matching.scorer`)
    -- so this module reaches no socket, knows no payload shape and reads no
    configuration. Anything either collaborator raises escapes this function the
    way it was raised: #9's `_run` is what turns it into the run row's failure.
    """
    resume_text = resume.active_resume_text()
    if resume_text is None or not resume_text.strip():
        raise RuntimeError(NO_ACTIVE_RESUME)

    for company_id, slug in _active_companies():
        postings = _fetched(source, slug)
        _add_counts(run_id, postings_seen=len(postings))
        created_ids = ingest_postings(company_id, postings).created_ids
        for posting_id, posting in _stored_postings(created_ids):
            decision = prefilter(posting)
            if not decision.passed:
                record_rejection(posting_id, decision)
                continue
            record_score(posting_id, scorer.score(posting, resume_text))
            _add_counts(run_id, matches_created=1)


def _active_companies() -> list[tuple[int, str]]:
    """Every active company's `id` and stored `slug`, in ascending `id` order.

    Both values are read while the session is open, because a row handed out of
    `db.session_scope()` is expired by its commit. An inactive company is simply
    not in this list, so the source is never asked about a board this app has
    stopped watching.
    """
    with db.session_scope() as session:
        return [
            (row.id, row.slug)
            for row in session.scalars(
                select(Company).where(Company.is_active.is_(True)).order_by(Company.id)
            )
        ]


def _fetched(source: Callable[[str], list[SourcePosting]], slug: str) -> list[SourcePosting]:
    """What that board returned, or a `SourceError` naming the company's slug.

    The source's own message is kept -- an adapter's failure message carries a
    status or the slug, never a response body -- and the slug is added here,
    because a run's `error` names the company whose board stopped the run and a
    caller cannot otherwise tell two boards' failures apart.
    """
    try:
        return source(slug)
    except SourceError as error:
        raise SourceError(
            f"The board for company '{slug}' could not be fetched: {error}"
        ) from error


def _stored_postings(created_ids: list[int]) -> list[tuple[int, SourcePosting]]:
    """Each row those ids name, read back as the source's shape, in the order they were reported.

    `ingest_postings` reports ids and the prefilter and the scorer take postings,
    so the row is read back through the id it was reported by: what is offered to
    the rules is the stored posting, not the payload that produced it. The rows
    are read in one session, one unit of work, and an empty list reads nothing.
    """
    if not created_ids:
        return []
    with db.session_scope() as session:
        return [
            (row.id, _source_posting(row))
            for row in (session.get(Posting, posting_id) for posting_id in created_ids)
        ]


def _source_posting(row: Posting) -> SourcePosting:
    """One stored posting in the shape the prefilter and the scorer read."""
    return SourcePosting(
        external_id=row.external_id,
        title=row.title,
        location=row.location,
        url=row.url,
        description=row.description,
    )


def _add_counts(run_id: int, *, postings_seen: int = 0, matches_created: int = 0) -> None:
    """Add one step's counts to the run's row, as the run earns them.

    One `db.session_scope()` per call, one unit of work, committed before this
    returns: a failure later in the run leaves these counts written. A row that is
    no longer there -- deleted while the run was in flight -- is nothing to write
    and not a failure, the way #9's own end-of-run write treats it.
    """
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.postings_seen += postings_seen
        run.matches_created += matches_created
