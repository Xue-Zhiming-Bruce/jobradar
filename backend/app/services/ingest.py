"""Turning the postings one board returned into `posting` rows that never double-count.

`ingest_postings(company_id, postings)` stores what a caller already has: one call
covers one company, makes no network call, and knows `SourcePosting` for its shape
only -- no payload key, no board name, no board address, no configuration. #9's
tick is the caller that reads a board; this module never decides when one is read.

A posting's identity is the pair the table already declares,
`UniqueConstraint("company_id", "external_id")`, so a row is always looked up by
the pair and never by the source's id alone: the same `external_id` under two
companies is two postings, and one whose title changed is the same posting.

What one call does, in the order the rules read:

- a pair with no row is inserted, with `first_seen_at` and `updated_at` from the
  models' own defaults;
- a pair with a row has its four source-owned columns -- `title`, `location`,
  `url`, `description` -- written when they differ from what is stored, `NULL`
  included, and nothing else. A row whose columns all match is left untouched, so
  a repeat run emits no `UPDATE` and the models' `onupdate` does not re-stamp
  `updated_at`: "first seen" and "changed" keep their separate meanings;
- a row of this company that the payload no longer lists is closed by setting
  `closed_at`, and one that is already closed is left alone, so "closed since"
  survives every later run. Nothing is ever deleted, which is what keeps a
  `match` row -- and its `notified_at` -- pointing at the posting it belongs to;
- a closed row the payload lists again is reopened by clearing `closed_at` on the
  row that is already there, so its `first_seen_at` and its match survive and the
  digest cannot alert on the same posting twice.

`IngestResult` is the hand-off to #9 and #10: `created_ids` are the postings that
are genuinely new and `reopened_ids` the ones the board lists again, both as
`posting.id` values in the order the payload gave them. A close appears in
neither list. An empty payload is the truth for that company -- it closes that
company's open postings, and the next payload that lists them reopens them on the
same rows. Whether a suddenly empty board deserves a confidence guard is fetch
behaviour, and #28 owns it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app import db
from app.models import Posting
from app.sources.base import SourcePosting

# The columns the source owns. They follow the payload -- and an absent value is
# written as `NULL` rather than skipped -- while `id`, `company_id`,
# `external_id` and `first_seen_at` are written once, on the insert.
SOURCE_COLUMNS = ("title", "location", "url", "description")


@dataclass(frozen=True)
class IngestResult:
    """What one call did: the postings it created, and the ones it reopened."""

    created_ids: list[int]
    reopened_ids: list[int]


def ingest_postings(company_id: int, postings: list[SourcePosting]) -> IngestResult:
    """Store the postings one board returned for one company, and report what changed.

    One `session_scope()` per call, writes through the ORM, and no write at all
    for a row the payload agrees with. A `company_id` with no `company` row is
    refused by the foreign key: the `IntegrityError` reaches the caller and the
    transaction is rolled back, so no posting is left behind.
    """
    with db.session_scope() as session:
        stored = {
            row.external_id: row
            for row in session.scalars(
                select(Posting).where(Posting.company_id == company_id)
            )
        }
        created: list[Posting] = []
        reopened_ids: list[int] = []
        listed: set[str] = set()
        for source in postings:
            listed.add(source.external_id)
            row = stored.get(source.external_id)
            if row is None:
                row = Posting(
                    company_id=company_id,
                    external_id=source.external_id,
                    title=source.title,
                    location=source.location,
                    url=source.url,
                    description=source.description,
                )
                session.add(row)
                stored[source.external_id] = row
                created.append(row)
                continue
            if row.closed_at is not None:
                row.closed_at = None
                reopened_ids.append(row.id)
            for column in SOURCE_COLUMNS:
                value = getattr(source, column)
                if getattr(row, column) != value:
                    setattr(row, column, value)
        closed_at = datetime.now(UTC)
        for external_id, row in stored.items():
            if external_id not in listed and row.closed_at is None:
                row.closed_at = closed_at
        session.flush()  # the new rows' ids, and the foreign key check, happen here
        return IngestResult(
            created_ids=[row.id for row in created], reopened_ids=reopened_ids
        )
