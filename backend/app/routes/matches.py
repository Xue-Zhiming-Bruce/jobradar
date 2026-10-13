"""`/matches`: the scores the pipeline stored, and the owner's verdict on one.

`GET /matches` is the read #12's matches screen and #17's "list matches" tool both
need: every `match` row the caller asks for, newest first, rendered as the
`MatchSummary` below -- the company's name, the posting's title and url, and the
match's own score, outcome, reason, gaps, state and `created_at`. `PATCH /matches/{id}`
records the owner's `saved` or `dismissed` verdict on one of them and answers with
the body the read answers with, so the two routes cannot drift apart: one
`MatchSummary` model and one `match_summary()` renderer, the `RunSummary` +
`run_summary` pattern of `app.routes.runs`.

Three things decide the shape of this module. The list is every row unless the
caller narrows it: `min_score`, `state`, `company_id` and `limit` are the
caller's own filters (`#44`), the default request applies none of them and is
exactly what this route answered before they existed, and nothing here hides a
row on its own -- a prefiltered posting and a not-scored one are in that default
answer exactly as they were stored, so `min_score=0` still finds them. Each body
carries the row's own `outcome` (#30's column), so #12 can hide or label one
without parsing `reason` -- keeping a row out of an email is #10's rule
(`digest` selects `outcome = 'scored'`) and whether the
screen hides them is #12's decision, taken on this body. `gaps` is stored as
JSON text (#8's `record_score` writes `json.dumps(result.gaps)`), so it is decoded
here and a row no writer in this app produces -- NULL, text that is not JSON,
JSON that is not a list of strings -- renders `[]` rather than the raw column or
a `500`: a read endpoint stays up on a row it does not recognise. And the write
is the owner's verdict alone: the request model accepts `saved` and `dismissed`
and nothing else, so `"new"` is refused `422` -- "new again" is not in
`_docs/plan.md` -- and no refused value reaches the `state` column, whose own
vocabulary (`new`/`saved`/`dismissed`, `ck_match_state_values`) is unchanged. The
write touches `state` and nothing else: `notified_at` and `outcome` are left alone, so
this endpoint cannot change which matches #10's digest alerts on.

No secret is required, because browsing one's own matches is what the browser
does and this app has no accounts (`_docs/plan.md`). The body carries what the
rows hold and nothing else -- no secret, no resume text, no database URL.
"""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app import db
from app.models import Company, Match, Posting

router = APIRouter()


class MatchSummary(BaseModel):
    """One `match` row as this API renders it: the row, plus the names it points at.

    The same body both routes carry, so a client reads one shape for a match
    wherever it meets one. Ten fields and no eleventh: `notified_at` stays on the
    row, because when an alert went out is the digest's business and not the
    screen's, and nothing here is computed -- `score`, `reason`, `state` and
    `outcome` are the row's own, `gaps` is the list #8 stored as JSON text, and
    `id` is what `PATCH /matches/{id}` takes.
    """

    id: int
    company_name: str
    title: str
    url: str
    score: int
    reason: str
    gaps: list[str]
    state: str
    # The row's own marker of which situation it records -- `'scored'`,
    # `'rejected'` or `'not_scored'` -- rendered verbatim: #12 hides or labels a
    # non-scored row on this field rather than by parsing `reason`.
    outcome: str
    created_at: datetime


class MatchVerdict(BaseModel):
    """Body of a verdict: the one value this endpoint records, and no other field.

    `state` is a `Literal` of the two verdicts the owner has, so anything else --
    `"new"`, `""`, a number, nothing at all -- is refused by this model's own
    validation as `422` and never reaches the row. Re-opening a match as `new` is
    not in `_docs/plan.md`, and the column's own vocabulary is the model's
    (`ck_match_state_values`) rather than this endpoint's.
    """

    state: Literal["saved", "dismissed"]


# The vocabulary `?state=` accepts: the three values the column's own check
# (`ck_match_state_values`, `app.models.Match`) enforces and no fourth, so
# `hidden` and `""` are refused as `422` by the parameter's own validation.
# `MatchVerdict` above is not this: a write takes the two verdicts and never
# `new`, which is a value a row is written with rather than one it is given.
MatchState = Literal["new", "saved", "dismissed"]


# The refusals this router answers with, named in the contract the way
# `app.routes.watchlist` names its own: a client reads the status codes it has to
# handle without having to try them out.
NOT_FOUND_RESPONSE = {404: {"description": "No match has that id"}}
UNUSABLE_REQUEST_RESPONSE = {
    422: {"description": "The body, or the path, is not something this route can use"}
}
UPDATE_RESPONSES = NOT_FOUND_RESPONSE | UNUSABLE_REQUEST_RESPONSE

# The most rows one read answers: `?limit=` is bounded by this, so the read
# cannot be asked for an unbounded page. A constant beside the dictionaries
# above rather than configuration: it is not a deployment's to change.
MATCHES_MAX_LIMIT = 200


def match_summary(match: Match) -> MatchSummary:
    """One match as this API renders it, read while its session is still open.

    The company's name and the posting's title and url are not columns of
    `match`, so they are read through the match's own session -- the one
    `db.session_scope()` opened for this request -- by the posting's id and the
    company's. The gap strings are decoded here too, because the column holds the
    JSON text #8 wrote rather than a list.
    """
    session = object_session(match)
    posting = session.get(Posting, match.posting_id)
    company = session.get(Company, posting.company_id)
    return MatchSummary(
        id=match.id,
        company_name=company.name,
        title=posting.title,
        url=posting.url,
        score=match.score,
        reason=match.reason,
        gaps=_gaps(match.gaps),
        state=match.state,
        outcome=match.outcome,
        created_at=match.created_at,
    )


def _gaps(stored: str | None) -> list[str]:
    """The gap strings #8 wrote as JSON text, and `[]` for anything else.

    `"[]"` renders no gaps and `'["Rust", "Kubernetes"]'` renders those two in
    that order. A NULL column, text that is not JSON and JSON that is not a list
    of strings all render `[]` as well: a row no writer in this app produces must
    not answer a client with the raw column or with a `500`. #10's stricter
    `digest._gaps` is the digest's own and is not what a read uses.
    """
    try:
        decoded = json.loads(stored) if stored else None
    except ValueError:
        return []
    if not isinstance(decoded, list) or not all(isinstance(gap, str) for gap in decoded):
        return []
    return decoded


@router.get(
    "/matches",
    response_model=list[MatchSummary],
    tags=["matches"],
    summary="List stored matches, newest first",
)
def list_matches(
    min_score: int | None = Query(None, ge=0, le=100),
    state: MatchState | None = Query(None),
    company_id: int | None = Query(None),
    limit: int | None = Query(None, ge=1, le=MATCHES_MAX_LIMIT),
) -> list[MatchSummary]:
    """Every `match` row the query leaves, newest first, and `[]` when it leaves none.

    No parameter means no filter, which is every row and what this route has
    always answered; each parameter narrows that answer and none of them hides a
    row on its own. `min_score` is a floor on the row's own score -- `0` includes
    the prefiltered and the not-scored rows, because which of those to show is
    #12's decision and not this read's -- `state` is one value of the column's
    vocabulary, `company_id` is the company the posting belongs to (an id no row
    names matches nothing and answers `[]`, not a `404`), and `limit` is the
    newest N, at most `MATCHES_MAX_LIMIT`. Values outside a parameter's own range
    or vocabulary are refused as `422` before this function runs and write
    nothing.

    The answer is one statement: the filters are `.where(...)` clauses on the read
    that was already there, so they compose with AND and none of them is applied
    after the rows have been read. The company filter is the posting's own column,
    which is what the join is for: a match has exactly one posting (`match`'s
    foreign key is `NOT NULL`), so the join drops no row. The order is `created_at`
    descending with the id descending under it, so two rows written in the same
    instant -- which `created_at`'s default makes possible -- still have one order,
    newest row first; `limit` is applied after that ordering, so it is the newest
    N and never an arbitrary N.
    """
    statement = (
        select(Match)
        .join(Posting, Match.posting_id == Posting.id)
        .order_by(Match.created_at.desc(), Match.id.desc())
    )
    if min_score is not None:
        statement = statement.where(Match.score >= min_score)
    if state is not None:
        statement = statement.where(Match.state == state)
    if company_id is not None:
        statement = statement.where(Posting.company_id == company_id)
    if limit is not None:
        statement = statement.limit(limit)
    with db.session_scope() as session:
        return [match_summary(match) for match in session.scalars(statement)]


@router.patch(
    "/matches/{id}",
    response_model=MatchSummary,
    tags=["matches"],
    summary="Record the owner's verdict on one match",
    responses=UPDATE_RESPONSES,
)
def record_verdict(id: int, body: MatchVerdict) -> MatchSummary:
    """Write the owner's `saved` or `dismissed` verdict on that match, and answer with it.

    The id is a path parameter and an `int`: a value that is not one never
    reaches this function and is refused by the path's own validation as `422`.
    A body whose `state` is neither of the two verdicts is refused by
    `MatchVerdict` the same way. An id that finds no row is a `404` and writes
    nothing. What is written is `state` and nothing else, and the answer is the
    row as `GET /matches` renders it, read after the write -- so the same verdict
    sent twice is the same answer twice and one row the whole time.
    """
    with db.session_scope() as session:
        match = session.get(Match, id)
        if match is None:
            raise HTTPException(status_code=404, detail=f"There is no match with id {id}")
        match.state = body.state
        return match_summary(match)
