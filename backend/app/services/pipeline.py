"""The work one digest run does: the walk, the fetch, the store, the gate, the score, the counts.

`run_pipeline(run_id, *, sources, scorer)` is what #9's `get_pipeline()` provider
returns, and the run row the id names already exists in `running`: the run's
lifecycle, the concurrency guard and the thread are #9's, and this module touches
none of them. It writes no `status`, no `finished_at` and no `error` -- a run that
fails leaves the row as it found it and #9's `_run` records the failure, the
message included -- and it opens no socket, sends no email and reads no
configuration of its own: the sources and the scorer arrive as arguments.

One run does this, in this order, for every company whose `is_active` is true, in
ascending `company.id` order:

- `sources[company.source](company.slug)`, once per company, through the adapter
  the row names and with the slug the row stores -- never a value rebuilt from
  the company's name and never another company's adapter;
- `_refuse_shrunk_board`, which refuses that list when it names fewer than half
  the postings the company already has open, and at least
  `SHRINK_GUARD_MIN_OPEN_POSTINGS` of them are open: a `BoardShrunk`, raised before
  the ingest, so a refused board closes nothing and counts nothing;
- `ingest.ingest_postings(company_id, postings)` for the list that board
  returned, so every `posting` row -- the insert, the update in place, the close
  and the reopen -- is #6's, and this module writes no `posting` column;
- for each id `IngestResult.created_ids` reported, in that order: the row read
  back as a `SourcePosting` and offered to `prefilter` with the active resume's
  parsed skills. A posting the prefilter rejects is stored through
  `record_rejection` and is never offered to the scorer; one that passes is scored
  exactly once, through `scorer.score(posting, resume_text)`, and the result it
  returns is stored through `record_score` -- a model outage's `NOT_SCORED_PREFIX`
  result included, because a result the scorer returned is a result to record and
  not a failure.

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
- the resume is read once, before the first board is called, and a run with no
  active resume fails without a single request: its text and the parsed skills the
  must-have rule reads (`active_resume_text` and `active_resume_skills`, the second
  passed to every `prefilter` call) are one read each per run, never one per
  posting. `LlmScorer.score` raises for an empty resume, so fetching first would
  store postings that can never be scored. Both values are user data: they go to
  the prefilter and the scorer and into no log line, no row and no message.
- a board that fails fails the run. The first `SourceError` escaping a company's
  fetch aborts the walk, naming that company's slug -- and never the board's
  response body, which the adapters already keep out of their own messages. The
  postings and matches the earlier companies earned stay written and the counts
  keep them, so a failed run costs a retry. Skipping the company or keeping going
  would be a silent miss, and a per-company column is the migration path #21
  owns. Retries and backoff are #54's, and live in `app.sources.base`.
- a board that lists far fewer postings than the company has open is refused the
  same way, as `BoardShrunk`: the guard reads `posting.closed_at`, adds no column
  and stores no history, so a board that genuinely shrank stays refused until the
  owner deactivates the company (#29). The price -- the companies after it are not
  fetched and the day's digest does not go out -- is #34's deliberate choice
  against a silent miss, and the escape is #59.
"""

from collections.abc import Mapping

from sqlalchemy import func, select

from app import db
from app.matching.prefilter import prefilter, record_rejection
from app.matching.scorer import MatchScorer, record_score
from app.models import Company, Posting, Run
from app.services import resume
from app.services.ingest import ingest_postings
from app.sources.base import BoardShrunk, Source, SourceError, SourcePosting

# The one failure this module raises of its own, and the message a run row keeps:
# a run with nothing to score against would otherwise fetch and store postings it
# can never match.
NO_ACTIVE_RESUME = "No active resume: upload one before a run can score postings"

# How many of a company's postings have to be open before its board's answer is
# held to the ratio below. Under this many, a board is free to close everything it
# has: a company with three postings is a company whose board just changed.
SHRINK_GUARD_MIN_OPEN_POSTINGS = 4


def run_pipeline(
    run_id: int,
    *,
    sources: Mapping[str, Source],
    scorer: MatchScorer,
) -> None:
    """Walk the watchlist once for the run `run_id` names, and count what it did.

    Both collaborators are arguments -- `sources` maps a stored source name to a
    board adapter and the scorer is one per run, the way its own cap requires --
    so this module reaches no socket, knows no payload shape and reads no
    configuration. Anything either collaborator raises escapes this function the
    way it was raised: #9's `_run` is what turns it into the run row's failure.
    """
    resume_text = resume.active_resume_text()
    if resume_text is None or not resume_text.strip():
        raise RuntimeError(NO_ACTIVE_RESUME)
    resume_must_haves = tuple(resume.active_resume_skills())

    for company_id, slug, source in _active_companies():
        postings = _fetched(sources[source], slug)
        _refuse_shrunk_board(company_id, slug, postings)
        _add_counts(run_id, postings_seen=len(postings))
        created_ids = ingest_postings(company_id, postings).created_ids
        for posting_id, posting in _stored_postings(created_ids):
            decision = prefilter(posting, resume_must_haves=resume_must_haves)
            if not decision.passed:
                record_rejection(posting_id, decision)
                continue
            record_score(posting_id, scorer.score(posting, resume_text))
            _add_counts(run_id, matches_created=1)


def _active_companies() -> list[tuple[int, str, str]]:
    """Every active company's `id`, stored `slug` and stored `source`, in ascending `id` order.

    All three values are read while the session is open, because a row handed out
    of `db.session_scope()` is expired by its commit. An inactive company is
    simply not in this list, so the source is never asked about a board this app
    has stopped watching.
    """
    with db.session_scope() as session:
        return [
            (row.id, row.slug, row.source)
            for row in session.scalars(
                select(Company).where(Company.is_active.is_(True)).order_by(Company.id)
            )
        ]


def _fetched(source: Source, slug: str) -> list[SourcePosting]:
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


def _refuse_shrunk_board(company_id: int, slug: str, postings: list[SourcePosting]) -> None:
    """Refuse a board that listed far fewer postings than the company has open.

    A ratio rather than an absolute count, and the walk's rule rather than an
    adapter's: one adapter sees a payload and cannot know what was stored before
    it. Fewer than half of the company's open postings, with at least
    `SHRINK_GUARD_MIN_OPEN_POSTINGS` of them open, is a `BoardShrunk` raised before
    `ingest_postings` is called -- so a refused board closes nothing, is counted by
    nobody, and reaches the run's row as a failed board. Integer arithmetic, so
    exactly half is allowed: a company that closes half its postings is a board's
    ordinary business. The message names the company's slug and both numbers, and
    nothing from the payload.
    """
    open_count = _open_postings(company_id)
    if open_count >= SHRINK_GUARD_MIN_OPEN_POSTINGS and len(postings) * 2 < open_count:
        raise BoardShrunk(
            f"The board for company '{slug}' listed {len(postings)} postings, but"
            f" {open_count} of its postings are open: refusing to close them"
        )


def _open_postings(company_id: int) -> int:
    """How many postings of that company are open, read in one query."""
    with db.session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(Posting)
            .where(Posting.company_id == company_id, Posting.closed_at.is_(None))
        )


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
        rows = [session.get(Posting, posting_id) for posting_id in created_ids]
        return [(row.id, _source_posting(row)) for row in rows]


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
