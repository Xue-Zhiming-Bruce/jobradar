"""A re-match's model cap: how many passers it left, as one nullable column.

Revision ID: 0011_run_postings_not_rematched
Revises: 0010_run_company_outcomes
Create Date: 2026-10-16 09:00:00.000000

`run.postings_not_rematched` (`Integer`) records how many passers a re-match's
own `SCORER_MAX_POSTINGS` cap (`pipeline.rematch_stored_postings`) left for a
later run, written by `services.pipeline._record_not_rematched` when, and only
when, the cap really left at least one. It is nullable and defaulted by nothing,
so it carries three readings rather than two: NULL means the cap left no passer
-- the ordinary re-match, and every row written by a tick or before this column
existed -- and a positive value means that many passers, in candidate order, were
left for a later re-match. `0` is never written, because a re-match that offered
every passer has nothing to report.

Nothing is backfilled. A row written before this revision ran with no cap of this
kind, so the honest reading is a NULL, and a `NOT NULL` default of `0` would make
every one of them claim a cap left nothing.

`upgrade()` adds the column only when it is missing, the way the baseline creates
a table only when it is missing and 0002, 0003, 0005, 0006, 0007, 0008 and 0010
add their own only when missing: a database `app.db.init_db()` built already has
it, from the models, and `alembic upgrade head` on that database still applies
cleanly instead of failing on a duplicate column. It is added with a plain
`ADD COLUMN`, which both engines express without rebuilding the table.

`downgrade()` drops the column, and the count goes with it. The run rows
themselves -- their status, their end, their error and their other counts -- stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_run_postings_not_rematched"
down_revision: str | Sequence[str] | None = "0010_run_company_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN = "run"
_POSTINGS_NOT_REMATCHED = "postings_not_rematched"


def _has_the_column() -> bool:
    """Whether the column is already there, so a database `init_db()` built is adopted."""
    return any(
        column["name"] == _POSTINGS_NOT_REMATCHED
        for column in sa.inspect(op.get_bind()).get_columns(_RUN)
    )


def upgrade() -> None:
    if _has_the_column():
        return
    op.add_column(_RUN, sa.Column(_POSTINGS_NOT_REMATCHED, sa.Integer(), nullable=True))


def downgrade() -> None:
    if not _has_the_column():
        return
    op.drop_column(_RUN, _POSTINGS_NOT_REMATCHED)
