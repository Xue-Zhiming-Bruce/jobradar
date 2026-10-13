"""The deterministic gate a posting passes before anything is scored, and its config.

`prefilter(posting)` answers one question and has no side effect: the posting
trips one of four configured rule lists, or it does not. `prefilter` opens no
session, writes no row, makes no request and reads no file -- it is a pure
function of the posting and the four variables below, so the same posting always
yields the same decision. `record_rejection(posting_id, decision)` is this
module's only writer: it opens one `db.session_scope()` and records the rejection
on the `match` row keyed by `posting_id`. Which posting is offered to either, and
when a rejection is recorded, is #9's tick.

The rules, in the fixed order the recorded reason keeps:

- title -- on `posting.title`;
- seniority -- on `posting.title` too, because a seniority word is what a title
  carries;
- location -- on `posting.location`;
- must-have -- on the title and the description together.

Matching is a case-insensitive substring, never a word boundary, a token split or
a regex, and `None` reads as the empty string: a posting without a location can
never trip the location rule, and one without a description is judged by its
title alone.

A rejection is told from a score by the row's own `outcome = 'rejected'`, and by
`REJECTION_REASON_PREFIX`, which is the first text of the stored reason. The
column is the reader's marker (#30 gave the row one, in place of the prefix #10
used to string-match); the prefix stays, because other modules and #31 read it,
and because it is what a reader of the reason sees.

Each of the four variables is a comma-separated list whose entries are trimmed,
lower-cased and stripped of empties. The format has one limit: a term cannot
contain a comma. An unset or empty variable is the declared default -- no terms
-- so a JobRadar with none of them set rejects nothing and raises nothing. The
value is read per call and never at import, so a deployment changes the rules by
setting the variable and a test sees the next call change.
"""

import os
from dataclasses import dataclass

from sqlalchemy import select

from app import db
from app.models import Match
from app.sources.base import SourcePosting

# The four variables, named here and read in no other module, the way the resume
# service reads its own settings in one place.
PREFILTER_TITLE_TERMS = "PREFILTER_TITLE_TERMS"
PREFILTER_SENIORITY_TERMS = "PREFILTER_SENIORITY_TERMS"
PREFILTER_LOCATION_TERMS = "PREFILTER_LOCATION_TERMS"
PREFILTER_MUST_HAVE_KEYWORDS = "PREFILTER_MUST_HAVE_KEYWORDS"

# What an unset or empty variable means: no terms, so its rule rejects nothing.
DEFAULT_TERMS: tuple[str, ...] = ()

# The marker #10 reads, and the first text of every stored rejection, which then
# reads "Prefiltered out: title term 'intern'; missing must-have 'rust'".
REJECTION_REASON_PREFIX = "Prefiltered out: "

# The clause each rule contributes, one per matched term.
_TITLE_CLAUSE = "title term '{}'"
_SENIORITY_CLAUSE = "seniority term '{}'"
_LOCATION_CLAUSE = "location term '{}'"
_MUST_HAVE_CLAUSE = "missing must-have '{}'"


@dataclass(frozen=True)
class PrefilterDecision:
    """Whether a posting passed, and every rule clause that says why it did not."""

    reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """No clause means it passed: "passed" and "no reasons" cannot disagree."""
        return not self.reasons


def title_terms() -> tuple[str, ...]:
    """`PREFILTER_TITLE_TERMS`, trimmed and lower-cased, empty when unset or empty."""
    return _terms(os.environ.get(PREFILTER_TITLE_TERMS, ""))


def seniority_terms() -> tuple[str, ...]:
    """`PREFILTER_SENIORITY_TERMS`, trimmed and lower-cased, empty when unset or empty."""
    return _terms(os.environ.get(PREFILTER_SENIORITY_TERMS, ""))


def location_terms() -> tuple[str, ...]:
    """`PREFILTER_LOCATION_TERMS`, trimmed and lower-cased, empty when unset or empty."""
    return _terms(os.environ.get(PREFILTER_LOCATION_TERMS, ""))


def must_have_keywords() -> tuple[str, ...]:
    """`PREFILTER_MUST_HAVE_KEYWORDS`, trimmed and lower-cased, empty when unset or empty."""
    return _terms(os.environ.get(PREFILTER_MUST_HAVE_KEYWORDS, ""))


def prefilter(posting: SourcePosting) -> PrefilterDecision:
    """Every rule `posting` trips, in the fixed order, and nothing else.

    Reads the posting and the four variables and writes nothing anywhere: no
    session, no row, no request, no file.
    """
    title = posting.title.lower()
    reasons: list[str] = []
    for term in title_terms():
        if term in title:
            reasons.append(_TITLE_CLAUSE.format(term))
    for term in seniority_terms():
        if term in title:
            reasons.append(_SENIORITY_CLAUSE.format(term))
    location = (posting.location or "").lower()
    for term in location_terms():
        if term in location:
            reasons.append(_LOCATION_CLAUSE.format(term))
    description = (posting.description or "").lower()
    for term in must_have_keywords():
        if term not in title and term not in description:
            reasons.append(_MUST_HAVE_CLAUSE.format(term))
    return PrefilterDecision(reasons=tuple(reasons))


def record_rejection(posting_id: int, decision: PrefilterDecision) -> None:
    """Record one rejection on the `match` row keyed by `posting_id`.

    One `session_scope()` per call, written through the ORM so the model's own
    defaults fill `created_at`, `state` and `gaps` on a first rejection. Both
    paths write `outcome = 'rejected'`, the row's own marker of which of the
    three situations it records, and the reason keeps `REJECTION_REASON_PREFIX`
    for the readers that still match on it. A posting rejected a second time --
    after its title changed, say -- is the same row: `match.posting_id` is
    unique, so the row is looked up and its `score`, `reason`, `gaps` and
    `outcome` are written again, where inserting blind would be an
    `IntegrityError` the second time. `gaps` is written as NULL on that path,
    because a rejection row has one spelling of "no gaps" whichever path wrote
    it -- the insert path below leaves the column to the model's own default,
    which is that same NULL. A score's gap list describes the score the row no
    longer carries, and `digest._gaps` and `routes/matches.py` decode NULL to
    `[]` anyway.

    A decision that passed is refused: a score-0 row for a posting that passed is
    a rejection the digest must never see.
    """
    if decision.passed:
        raise ValueError(
            f"Posting {posting_id} passed the prefilter: there is no rejection to record"
        )
    reason = REJECTION_REASON_PREFIX + "; ".join(decision.reasons)
    with db.session_scope() as session:
        row = session.scalar(select(Match).where(Match.posting_id == posting_id))
        if row is None:
            session.add(
                Match(posting_id=posting_id, score=0, reason=reason, outcome="rejected")
            )
        else:
            # The owner's `state` and a digest's `notified_at` are left alone:
            # the verdict on a posting is the owner's and not the prefilter's to
            # overwrite, and a rejection does not un-send an email.
            row.score = 0
            row.reason = reason
            row.gaps = None
            row.outcome = "rejected"


def _terms(configured: str) -> tuple[str, ...]:
    """One comma-separated variable as its terms, in the order it lists them.

    Trimmed, lower-cased and stripped of empties, so `"intern,  Apprentice ,"`
    reads as `("intern", "apprentice")`, and an unset or empty variable is the
    declared default.
    """
    if not configured.strip():
        return DEFAULT_TERMS
    return tuple(
        term for entry in configured.split(",") if (term := entry.strip().lower())
    )
