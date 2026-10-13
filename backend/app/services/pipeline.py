"""The work one digest run does: the walk, the fetch, the store, the gate, the score, the counts.

`run_pipeline(run_id, *, sources, scorer, sleep=time.sleep)` is what #9's
`get_pipeline()` provider returns, and the run row the id names already exists in
`running`: the run's lifecycle, the concurrency guard and the thread are #9's, and
this module touches none of them. It writes no `status`, no `finished_at` and no
`error` -- a run that fails leaves the row as it found it and #9's `_run` records
the failure, the message included -- and it opens no socket and sends no email:
the sources and the scorer arrive as arguments, and the only configuration it
reads is the two settings a run has for itself, below. It writes one row of its
own, the `run_company` outcome of each company it attempted.

One run does this, in this order, for every company whose `is_active` is true, in
ascending `company.id` order:

- `sources[company.source](company.slug)` -- once per company, through the adapter
  the row names and with the slug the row stores, never a value rebuilt from the
  company's name and never another company's adapter. The company's stored
  validator rides with the call as `etag`/`last_modified`, so a board that has not
  changed answers `304` and the walk skips that company entirely: `NotModified`
  records `'not_modified'` and returns to the top before the guard, before
  `_add_counts` and before the ingest, so a board that was not read closes nothing
  and is counted by nobody;
- `_refuse_shrunk_board`, which refuses that list when it names fewer than half
  the postings the company already has open, and at least
  `SHRINK_GUARD_MIN_OPEN_POSTINGS` of them are open: a `BoardShrunk`, raised before
  the ingest, so a refused board closes nothing and counts nothing;
- `ingest.ingest_postings(company_id, postings)` for the list that board
  returned, so every `posting` row -- the insert, the update in place, the close
  and the reopen -- is #6's, and this module writes no `posting` column; then
  `_store_validator`, which writes the answer's own validator on the company row,
  and `'fetched'` on that company's outcome row;
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
score lands -- a prefilter rejection is not a match and is not counted. A skipped
`304` board adds to neither: it contributed no posting at all, so a run over one
`304` board and one `200` board of N postings ends with `postings_seen == N`. Both
are written as they are earned rather than once at the end, so a run that fails
half-way says how far it got: every write committed in its own
`db.session_scope()`, so nothing already written is rolled back by a later
failure, and the next tick re-runs the whole thing idempotently on #6's dedupe.

A run asks a bounded number of boards. `request_budget()` is read once, before the
first call, and the walk stops before the call that would be the (`budget + 1`)st,
in ascending `company.id` order: a company past the bound is asked nothing at all,
so no `posting` row of its is inserted, updated, closed or reopened, no `match` row
of its changes, no count of its reaches either column, and `_store_validator`
leaves its `etag`/`last_modified` exactly as they were. One unit is one board call
-- `200` and `304` each spend one, because a `304` is a request to a board, and a
retry inside `fetch_body` spends nothing extra -- so a company is either fetched
completely or not fetched at all, and a run's worst case is `budget` times
`RETRY_ATTEMPTS` requests. The bound stopping the walk is not a failure: the run
still stores, scores and (through #10) emails what it fetched, and writes how many
companies it left for a later run on `run.companies_not_fetched`, which is `NULL`
for every run the bound did not stop.

A run also paces its calls. `request_interval_seconds()` is read the same way, and
`sleep` -- the walk's own keyword-only seam, `time.sleep` by default -- is called
exactly once with that interval per gap between two consecutive board calls: never
before the first call and never after the last, so a walk over `k` boards records
`[interval] * (k - 1)` waits and a company the budget refused is never waited for.
`0.0` is legitimate and calls the seam the same number of times, so a deployment
that wants no pacing says so. The waits `fetch_body` itself makes on a retry are
#54's and are not this.

Six things are deliberately not done here:

- a posting that was already stored is offered to neither the prefilter nor the
  scorer by *this* walk: only `created_ids` are matched. A reopened posting already
  has the one `match` row `match.posting_id` allows -- possibly a `state` the owner
  saved, and a `notified_at` a digest already set -- so re-offering it would rewrite
  a row the owner acted on. The round trip through `closed_at` stays #6's business.
  Re-deciding a stored posting deliberately is the other half of #64, and it is
  `rematch_stored_postings` below: a path of its own, entered by its own endpoint,
  which decides *which* rows may be offered again -- the open ones the owner has not
  resolved and no digest has emailed -- rather than widening this walk.
- the resume is read once, before the first board is called, and a run with no
  active resume fails without a single request: its text and the parsed skills the
  must-have rule reads (`active_resume_text` and `active_resume_skills`, the second
  passed to every `prefilter` call) are one read each per run, never one per
  posting. `LlmScorer.score` raises for an empty resume, so fetching first would
  store postings that can never be scored. Both values are user data: they go to
  the prefilter and the scorer and into no log line, no row and no message.
- a board that fails is that company's failure, and the walk goes on. The fetch
  and the guard sit in one per-company `try`: the first `SourceError` either of
  them raises is recorded as that company's `'failed'` outcome, with the message
  naming the company's slug -- and never the board's response body, which the
  adapters already keep out of their own messages -- and the next company is
  walked. A refused company is unchanged where it matters: its `posting` rows are
  not inserted, updated, closed or reopened, its `match` rows are untouched and
  its validator stays as it was, because the guard raises before `ingest_postings`
  is called for it at all. The counts keep what the companies that did answer
  earned, and the run's own status is derived from these rows by #9's `_run`. A
  failure that is not a board's -- the resume read below, a scorer or an
  `ingest_postings` -- still escapes this function and fails the run.
- a board that lists far fewer postings than the company has open is refused the
  same way, as `BoardShrunk`: the guard reads `posting.closed_at`, adds no column
  and stores no history, so a board that genuinely shrank stays refused until the
  owner deactivates the company (#29).
- a `304` is not a failure and does not work like one: the board answered that
  nothing changed, so the walk records `'not_modified'` for that company and goes
  on. The company's stored validator is left exactly as it was -- it is still the
  validator of the body this app holds -- and a run in which every board answers
  `304` is an ordinary success with both counts 0.
- the validator is written only after the payload was trusted and stored: an
  unwritten validator costs one more unconditional fetch, while one written too
  early buys a board skipped forever. So a payload the guard refuses and an
  `ingest_postings` that raises both leave the stored validator untouched, and the
  next run asks the same question again.
"""

import os
import time
from collections.abc import Callable, Mapping

from sqlalchemy import func, or_, select

from app import db
from app.matching.prefilter import prefilter, record_rejection
from app.matching.scorer import MatchScorer, max_postings, record_score
from app.models import Company, Match, Posting, Run, RunCompany
from app.services import resume
from app.services.ingest import ingest_postings
from app.sources.base import (
    BoardPostings,
    BoardShrunk,
    NotModified,
    Source,
    SourceError,
    SourcePosting,
)

# The one failure this module raises of its own, and the message a run row keeps:
# a run with nothing to score against would otherwise fetch and store postings it
# can never match.
NO_ACTIVE_RESUME = "No active resume: upload one before a run can score postings"

# The vocabulary `RunCompany.outcome` accepts, spelled the way the check constraint
# spells it: this module is the one writer of the column, and #9's `_run` derives a
# run's status from the `'failed'` rows it finds.
FETCHED = "fetched"
NOT_MODIFIED = "not_modified"
FAILED = "failed"

# How many of a company's postings have to be open before its board's answer is
# held to the ratio below. Under this many, a board is free to close everything it
# has: a company with three postings is a company whose board just changed.
SHRINK_GUARD_MIN_OPEN_POSTINGS = 4

# The two settings a run has for itself: how many boards one walk may ask, and how
# long it waits between two calls. Both are read once per run, at the top of
# `run_pipeline` and never at import, so a run obeys what the deployment held when
# the walk started and a later tick obeys what it holds then.
RUN_REQUEST_BUDGET_VARIABLE = "RUN_REQUEST_BUDGET"
DEFAULT_REQUEST_BUDGET = 200
RUN_REQUEST_INTERVAL_SECONDS_VARIABLE = "RUN_REQUEST_INTERVAL_SECONDS"
DEFAULT_REQUEST_INTERVAL_SECONDS = 1.0


def request_budget() -> int:
    """`RUN_REQUEST_BUDGET`, or `DEFAULT_REQUEST_BUDGET` when it is unset or empty.

    A whole number of board calls, `0` included: `0` asks no board at all this
    run. A negative value and a value that is not a number are refused rather
    than read as zero, with a message naming the variable and the value it could
    not read. Read before the first board call, so a misconfigured run fails
    loudly on its row instead of silently fetching nothing, and costs no request.
    """
    configured = os.environ.get(RUN_REQUEST_BUDGET_VARIABLE, "").strip()
    if not configured:
        return DEFAULT_REQUEST_BUDGET
    refused = (
        f"{RUN_REQUEST_BUDGET_VARIABLE} must be a whole number of board calls, 0 or"
        f" more, not '{configured}'"
    )
    try:
        budget = int(configured)
    except ValueError:
        raise ValueError(refused) from None
    if budget < 0:
        raise ValueError(refused)
    return budget


def request_interval_seconds() -> float:
    """`RUN_REQUEST_INTERVAL_SECONDS`, or `DEFAULT_REQUEST_INTERVAL_SECONDS` unset.

    The wait between two board calls, `0.0` included: `0.0` waits nothing. A
    negative value and a value that is not a number are refused rather than read
    as zero, with a message naming the variable and the value it could not read,
    for the reason `request_budget()` gives.
    """
    configured = os.environ.get(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "").strip()
    if not configured:
        return DEFAULT_REQUEST_INTERVAL_SECONDS
    refused = (
        f"{RUN_REQUEST_INTERVAL_SECONDS_VARIABLE} must be a number of seconds, 0 or"
        f" more, not '{configured}'"
    )
    try:
        interval = float(configured)
    except ValueError:
        raise ValueError(refused) from None
    if interval < 0:
        raise ValueError(refused)
    return interval


def run_pipeline(
    run_id: int,
    *,
    sources: Mapping[str, Source],
    scorer: MatchScorer,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Walk the watchlist once for the run `run_id` names, and count what it did.

    Both collaborators are arguments -- `sources` maps a stored source name to a
    board adapter and the scorer is one per run, the way its own cap requires --
    so this module reaches no socket, knows no payload shape and reads the run's
    own two settings and nothing else. `sleep` is the pacing seam: the wait before
    every board call but the first, `time.sleep` by default and a recorder in
    every test. Anything either collaborator raises escapes this function the way
    it was raised, with one exception: a board's own `SourceError` is recorded
    against the company it names and the walk goes on. #9's `_run` is what turns
    an escaping failure into the run row's failure.
    """
    budget = request_budget()
    interval = request_interval_seconds()
    resume_text = resume.active_resume_text()
    if resume_text is None or not resume_text.strip():
        raise RuntimeError(NO_ACTIVE_RESUME)
    resume_must_haves = tuple(resume.active_resume_skills())

    companies = _active_companies()
    for index, (company_id, slug, source, etag, last_modified) in enumerate(companies):
        if index >= budget:
            # The bound, not a failure: the companies past it are not called, not
            # probed and skipped, and the row says how many were left for a later run.
            _record_not_fetched(run_id, len(companies) - budget)
            break
        if index:
            sleep(interval)  # one wait per gap, and none before the first call
        try:
            fetched = _fetched(sources[source], slug, etag=etag, last_modified=last_modified)
            if isinstance(fetched, NotModified):
                # The board has not changed: nothing to store, close or count.
                _record_outcome(run_id, company_id, NOT_MODIFIED)
                continue
            postings = fetched.postings
            _refuse_shrunk_board(company_id, slug, postings)
        except SourceError as error:
            # That company's board failed, and no other company pays for it: the
            # message is recorded against the company it names and the walk goes on.
            _record_outcome(run_id, company_id, FAILED, str(error))
            continue
        _add_counts(run_id, postings_seen=len(postings))
        created_ids = ingest_postings(company_id, postings).created_ids
        _store_validator(company_id, fetched.etag, fetched.last_modified)
        _record_outcome(run_id, company_id, FETCHED)
        for posting_id, posting in _stored_postings(created_ids):
            decision = prefilter(posting, resume_must_haves=resume_must_haves)
            if not decision.passed:
                record_rejection(posting_id, decision)
                continue
            record_score(posting_id, scorer.score(posting, resume_text))
            _add_counts(run_id, matches_created=1)


def rematch_stored_postings(run_id: int, *, scorer: MatchScorer) -> None:
    """Re-decide the stored postings the owner has not resolved, under the configuration now.

    The work `POST /internal/rematch` hands over, beside `run_pipeline` and the
    other half of #64: where the tick decides only the postings its board just
    created, this decides the ones an earlier run stored -- the resume the rules
    read, or the prefilter configuration the process holds, changed after that run.
    It takes no `sources` and no `sleep`: no adapter is constructed and none is
    called, no `posting` column moves (`closed_at` included) and no `company.etag` /
    `last_modified` moves, so `postings_seen` stays 0 and the run's own `run_company`
    rows are the walk's -- this path writes none, `_recorded_failure` finds none, and
    the run ends `succeeded`. No digest runs here either: the next tick emails what
    the re-match produced, through the digest's own `notified_at IS NULL` selection.

    The candidates are every open posting whose `match` row is missing or the owner
    has neither acted on nor been told about (`state = 'new'` and
    `notified_at IS NULL`), oldest `posting.id` first, read in one statement by
    `_postings_to_rematch`. Each is offered to the prefilter first -- a rejection is
    written through `record_rejection`, a passer through `record_score` -- the
    tick's own writers in the tick's own order, so a stored rejection can flip to a
    score and a stale score can be rewritten. The active resume's text and parsed
    skills are read once each, before the first decision, and no active resume
    raises the module's own `NO_ACTIVE_RESUME` before any row is touched.

    One press decides at most the run's own `max_postings()` candidates -- the
    reader `LlmScorer.score` uses for its own cap -- read once before the first
    decision, and the candidates past the cap keep the verdicts they have. That is
    the one thing this path does that the tick does not, and the reason it must:
    handing a capped scorer a passer would replace a stored score with its own
    `NOT_SCORED_PREFIX` refusal, which is not a re-decision. What the cap left is
    written on the run's row by `_record_not_rematched`, so a truncated pass says
    so rather than looking complete. `matches_created` is counted the way the tick
    counts it, one per `record_score` write.

    Every write updates the row `match.posting_id` already keys -- both writers
    look the row up rather than inserting -- so a second run touches the same rows,
    and the candidate rule (which reads only `posting.closed_at`, `match.state` and
    `match.notified_at`, none of them written here) yields the same list: the run is
    idempotent, and no `match` or `posting` row is inserted or deleted.
    """
    resume_text = resume.active_resume_text()
    if resume_text is None or not resume_text.strip():
        raise RuntimeError(NO_ACTIVE_RESUME)
    resume_must_haves = tuple(resume.active_resume_skills())

    limit = max_postings()
    candidates = _postings_to_rematch()
    decided = candidates[:limit] if limit > 0 else []
    for posting_id, posting in decided:
        decision = prefilter(posting, resume_must_haves=resume_must_haves)
        if not decision.passed:
            record_rejection(posting_id, decision)
            continue
        record_score(posting_id, scorer.score(posting, resume_text))
        _add_counts(run_id, matches_created=1)
    # The count is written on every re-match, a `0` included: a pass that decided
    # every candidate is an answer, not a missing one (#56's `0` reading does not
    # exist, because that walk always stops for another reason).
    _record_not_rematched(run_id, len(candidates) - len(decided))


def _postings_to_rematch() -> list[tuple[int, SourcePosting]]:
    """Every open posting the owner has not resolved, oldest `posting.id` first.

    One statement: the postings with no `closed_at`, left-joined to the one `match`
    row `match.posting_id` allows -- that key is unique, so the join cannot
    duplicate a posting -- and kept only when that row is missing or the owner has
    neither acted on nor been told about it. The two `match` columns this reads,
    `state` and `notified_at`, are written by no line of this path, so the list is
    the same on a second run. Each row is read back as a `SourcePosting` while the
    session is open, because a row handed out of `db.session_scope()` is expired by
    its commit.
    """
    with db.session_scope() as session:
        rows = session.scalars(
            select(Posting)
            .outerjoin(Match, Match.posting_id == Posting.id)
            .where(
                Posting.closed_at.is_(None),
                or_(
                    Match.id.is_(None),
                    (Match.state == "new") & Match.notified_at.is_(None),
                ),
            )
            .order_by(Posting.id)
        )
        return [(row.id, _source_posting(row)) for row in rows]


def _active_companies() -> list[tuple[int, str, str, str | None, str | None]]:
    """Every active company's `id`, stored `slug`, `source` and stored validator.

    All five values are read while the session is open, because a row handed out
    of `db.session_scope()` is expired by its commit. An inactive company is
    simply not in this list, so the source is never asked about a board this app
    has stopped watching.
    """
    with db.session_scope() as session:
        return [
            (row.id, row.slug, row.source, row.etag, row.last_modified)
            for row in session.scalars(
                select(Company).where(Company.is_active.is_(True)).order_by(Company.id)
            )
        ]


def _fetched(
    source: Source, slug: str, *, etag: str | None, last_modified: str | None
) -> BoardPostings | NotModified:
    """What that board answered, or a `SourceError` naming the company's slug.

    The company's stored validator goes out with the call and the answer comes back
    as it is: either the postings and the validator that body arrived with, or the
    `NotModified` that says there is nothing to store. The source's own message is
    kept -- an adapter's failure message carries a status or the slug, never a
    response body -- and the slug is added here, because the message is recorded
    against the company whose board failed and a caller cannot otherwise tell two
    boards' failures apart. `NotModified` is not a `SourceError`, so it passes this
    seam through rather than being turned into a failure.
    """
    try:
        return source(slug, etag=etag, last_modified=last_modified)
    except SourceError as error:
        raise SourceError(
            f"The board for company '{slug}' could not be fetched: {error}"
        ) from error


def _store_validator(company_id: int, etag: str | None, last_modified: str | None) -> None:
    """Write the validator the stored payload came with, in its own unit of work.

    Called once `ingest_postings` has returned, so the two columns always describe
    the body of the last stored payload -- `None` included, which is what a board
    that sent no validator leaves behind. A row that is no longer there -- deleted
    while the run was in flight -- is nothing to write and not a failure, the way
    `_add_counts` treats it.
    """
    with db.session_scope() as session:
        company = session.get(Company, company_id)
        if company is None:
            return
        company.etag = etag
        company.last_modified = last_modified


def _refuse_shrunk_board(company_id: int, slug: str, postings: list[SourcePosting]) -> None:
    """Refuse a board that listed far fewer postings than the company has open.

    A ratio rather than an absolute count, and the walk's rule rather than an
    adapter's: one adapter sees a payload and cannot know what was stored before
    it. Fewer than half of the company's open postings, with at least
    `SHRINK_GUARD_MIN_OPEN_POSTINGS` of them open, is a `BoardShrunk` raised before
    `ingest_postings` is called -- so a refused board closes nothing, is counted by
    nobody, and reaches that company's own outcome row as `'failed'`. Integer
    arithmetic, so exactly half is allowed: a company that closes half its postings
    is a board's ordinary business. The message names the company's slug and both
    numbers, and nothing from the payload.
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


def _record_outcome(
    run_id: int, company_id: int, outcome: str, error: str | None = None
) -> None:
    """Record what this run did with that company, in its own unit of work.

    One row per company the walk attempted, written as the walk reaches it -- so a
    company the budget never reached gets no row (#56 counts it instead) and a
    company the walk failed on keeps the failure's own message. `error` is a
    failure's message or nothing: a company that was fetched stored no error. The
    unique key on `(run_id, company_id)` is the database's, so a second write for
    one company in one run is an `IntegrityError` rather than a second row.
    """
    with db.session_scope() as session:
        session.add(
            RunCompany(run_id=run_id, company_id=company_id, outcome=outcome, error=error)
        )


def _record_not_fetched(run_id: int, count: int) -> None:
    """Write how many active companies the run's budget stopped it from asking.

    Written by the walk, which is the one place that knows the bound stopped it;
    #9's `_end_run` keeps writing the status, the end and the error. One
    `db.session_scope()`, the way `_add_counts` writes a step of its own, and a row
    that is no longer there is nothing to write and not a failure.
    """
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.companies_not_fetched = count


def _record_not_rematched(run_id: int, count: int) -> None:
    """Write how many eligible postings this re-match's model cap left for a later one.

    Written by `rematch_stored_postings`, the one place that knows the cap stopped
    the offers; #9's `_end_run` keeps writing the status, the end and the error.
    Written on every re-match, a `0` included -- a pass that decided every candidate
    is a real answer and not a missing one -- and it is one `db.session_scope()`,
    the way `_record_not_fetched` writes a step of its own. A row that is no longer
    there is nothing to write and not a failure. A tick never calls this, so a run
    that did not re-match keeps the column's NULL.
    """
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.postings_not_rematched = count


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
