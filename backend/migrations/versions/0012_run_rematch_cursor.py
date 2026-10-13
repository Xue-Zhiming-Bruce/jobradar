"""A re-match's resume position: the next candidate, as one nullable column.

Revision ID: 0012_run_rematch_cursor
Revises: 0011_run_postings_not_rematched
Create Date: 2026-10-16 10:00:00.000000

`run.rematch_cursor` (`Integer`) records where the next re-match starts: the
`posting.id` of the first candidate the last press did not decide
(`pipeline.rematch_stored_postings`), written by
`services.pipeline._record_not_rematched` beside the count, in one unit of work.
It is nullable and defaulted by nothing, so it carries two readings rather than
one: NULL means no press stopped here -- a tick, a row written before this column
existed, or a press that decided every candidate it found -- and a positive value
is the `posting.id` the next press must decide first. Without it a press reads the
same candidate list every time (`_postings_to_rematch` reads columns no line of
that path writes) and its cap can never reach the postings past it.

Nothing is backfilled. A row written before this revision ran with no such
position, so the honest reading is a NULL, and the next press starts at the oldest
eligible posting exactly as it did before.

`upgrade()` adds the column only when it is missing, the way 0011 and the
revisions before it add theirs only when missing: a database `app.db.init_db()`
built already has it, from the models, and `alembic upgrade head` on that database
still applies cleanly instead of failing on a duplicate column. It is added with a
plain `ADD COLUMN`, which both engines express without rebuilding the table.

`downgrade()` drops the column, and the position goes with it. The run rows
themselves -- their status, their end, their error and their other counts -- stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_run_rematch_cursor"
down_revision: str | Sequence[str] | None = "0011_run_postings_not_rematched"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN = "run"
_REMATCH_CURSOR = "rematch_cursor"


def _has_the_column() -> bool:
    """Whether the column is already there, so a database `init_db()` built is adopted."""
    return any(
        column["name"] == _REMATCH_CURSOR
        for column in sa.inspect(op.get_bind()).get_columns(_RUN)
    )


def upgrade() -> None:
    if _has_the_column():
        return
    op.add_column(_RUN, sa.Column(_REMATCH_CURSOR, sa.Integer(), nullable=True))


def downgrade() -> None:
    if not _has_the_column():
        return
    op.drop_column(_RUN, _REMATCH_CURSOR)
