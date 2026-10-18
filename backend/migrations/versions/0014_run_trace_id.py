"""A run's own trace: the trace id of the request that started it, as one nullable column.

Revision ID: 0014_run_trace_id
Revises: 0013_telemetry_span
Create Date: 2026-10-18 10:00:00.000000

`run.trace_id` (`String(64)`) records the trace id of the `POST /internal/tick`
or `POST /internal/rematch` request that started the run: the value
`telemetry.current_trace_id()` held while `routes.internal._claim_run` wrote the
row, in the same unit of work, so #92's alert and #93's responder can name a
failed run's trace instead of finding it by timestamp. It is the same value the
request's own span and the run's span carry (#90), and `GET /runs/{id}` serves it
back through `RunSummary`.

It is nullable and defaulted by nothing, so it carries two readings rather than
one: NULL means "unknown" -- a row written before this column existed, and a run
a starter was handed without a trace id -- and a value is the id of the trace
that holds the request and the run. A `NOT NULL` default would make every row
written before this revision claim a trace, which is a lie, and NULL is never
backfilled with a guessed or borrowed id.

The value is the one `telemetry.adopted_trace_id` accepted: at most
`REQUEST_ID_LENGTH` (64) characters of `[A-Za-z0-9._-]`, because a header value
is text a client chose and it is served back in the run's answer. That bound is
the column's own length; nothing else about the value is inspected here.

Nothing is backfilled. A row written before this revision ran with no request to
name, so the honest reading is a NULL.

`upgrade()` adds the column only when it is missing, the way 0011 and the
revisions before it add theirs only when missing: a database `app.db.init_db()`
built already has it, from the models, and `alembic upgrade head` on that database
still applies cleanly instead of failing on a duplicate column. It is added with a
plain `ADD COLUMN`, which both engines express without rebuilding the table.

`downgrade()` drops the column, and the trace id goes with it. The run rows
themselves -- their status, their end, their error and their counts -- stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_run_trace_id"
down_revision: str | Sequence[str] | None = "0013_telemetry_span"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN = "run"
_TRACE_ID = "trace_id"


def _has_the_column() -> bool:
    """Whether the column is already there, so a database `init_db()` built is adopted."""
    return any(
        column["name"] == _TRACE_ID for column in sa.inspect(op.get_bind()).get_columns(_RUN)
    )


def upgrade() -> None:
    if _has_the_column():
        return
    op.add_column(_RUN, sa.Column(_TRACE_ID, sa.String(length=64), nullable=True))


def downgrade() -> None:
    if not _has_the_column():
        return
    op.drop_column(_RUN, _TRACE_ID)
