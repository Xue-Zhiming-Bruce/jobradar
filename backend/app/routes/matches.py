"""`/matches`: the scores the pipeline stored, and the owner's verdict on one.

`GET /matches` is the read #12's matches screen and #17's "list matches" tool both
need: every `match` row, unfiltered, newest first, rendered as the `MatchSummary`
below -- the company's name, the posting's title and url, and the match's own
score, outcome, reason, gaps, state and `created_at`. `PATCH /matches/{id}`
records the owner's `saved` or `dismissed` verdict on one of them and answers with
the body the read answers with, so the two routes cannot drift apart: one
`MatchSummary` model and one `match_summary()` renderer, the `RunSummary` +
`run_summary` pattern of `app.routes.runs`.

Three things decide the shape of this module. The list is every row and applies
no filter: a prefiltered posting and a not-scored one are in it exactly as they
were stored, and each body carries the row's own `outcome` (#30's column), so
#12 can hide or label one without parsing `reason` -- keeping a row out of an
email is #10's rule (`digest` selects `outcome = 'scored'`) and whether the
screen hides them is #12's decision, taken on this body. `gaps` is stored as
JSON text (#8's `record_score` writes `json.dumps(result.gaps)`), so it is decoded
here and a row no writer in this app produces -- NULL, text that is not JSON,
JSON that is not a list of strings -- renders `[]` rather than the raw column or
a `500`: a read endpoint stays up on a row it does not recognise. And the write
is the owner's verdict alone: the request model accepts `saved` and `dismissed`
and nothing else, so `"new"` is refused `422` -- "new again" is not in
`_docs/plan.md` -- and
no refused value reaches the `state` column, whose own vocabulary
(`new`/`saved`/`dismissed`, `ck_match_state_values`) is unchanged. The write
touches `state` and nothing else: `notified_at` and `outcome` are left alone, so
this endpoint cannot change which matches #10's digest alerts on.

No secret is required, because browsing one's own matches is what the browser
does and this app has no accounts (`_docs/plan.md`). The body carries what the
rows hold and nothing else -- no secret, no resume text, no database URL.
"""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
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


# The refusals this router answers with, named in the contract the way
# `app.routes.watchlist` names its own: a client reads the status codes it has to
# handle without having to try them out.
NOT_FOUND_RESPONSE = {404: {"description": "No match has that id"}}
UNUSABLE_REQUEST_RESPONSE = {
    422: {"description": "The body, or the path, is not something this route can use"}
}
UPDATE_RESPONSES = NOT_FOUND_RESPONSE | UNUSABLE_REQUEST_RESPONSE


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
    summary="List every stored match",
)
def list_matches() -> list[MatchSummary]:
    """Every `match` row, newest first, and `[]` when there are none.

    No filter, no search and no paging: `_docs/plan.md` asks only that the UI
    browse matches, and one user's matches are few. The order is `created_at`
    descending with the id descending under it, so two rows written in the same
    instant -- which `created_at`'s default makes possible -- still have one
    order, newest row first.
    """
    with db.session_scope() as session:
        return [
            match_summary(match)
            for match in session.scalars(
                select(Match).order_by(Match.created_at.desc(), Match.id.desc())
            )
        ]


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
