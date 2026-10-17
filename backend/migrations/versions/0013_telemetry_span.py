"""The seventh table: `telemetry_span`, one row per span the app recorded (#90).

Revision ID: 0013_telemetry_span
Revises: 0012_run_rematch_cursor
Create Date: 2026-10-17 09:00:00.000000

`telemetry_span(id, trace_id, name, route, method, status_code, duration_ms,
started_at)` is where the digest journey's traces are stored: the span of each
HTTP request the app answered and the span of each digest run a tick started,
both under the `X-Request-ID` the request was served with. Seven facts and no
eighth, which is what makes "no request body, query string, header value or
resume text is stored" a property of the table's shape rather than a promise
`app.telemetry` keeps. `route` holds a matched route's template and never a raw
path; it is nullable, with `method` and `status_code`, because the run's own
span -- named `run` -- is not an HTTP request and has none of the three.

`upgrade()` creates the table only when it is missing, the guard 0003 and every
revision since established: a database `app.db.init_db()` built already has the
table from the models, and `alembic upgrade head` on that database applies
cleanly instead of failing on a table that is already there. The `create_table`
call is what `uv run alembic revision --autogenerate` wrote, read and kept as it
stands; the guard that reads the engine's tables is hand-written, because
autogenerate detects no guard. No other table, column or row is touched, and
nothing is backfilled: no span existed before this revision ran.

`downgrade()` drops the table and nothing else. Nothing holds a reference to it
-- no foreign key points at it -- so no row of any other table is deleted or
rewritten by either direction, and the spans are the only thing lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_telemetry_span"
down_revision: str | Sequence[str] | None = "0012_run_rematch_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TELEMETRY_SPAN = "telemetry_span"


def _has_the_table() -> bool:
    """Whether the table is already there, so a database `init_db()` built is adopted."""
    return _TELEMETRY_SPAN in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_the_table():
        return
    op.create_table(
        _TELEMETRY_SPAN,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    if not _has_the_table():
        return
    op.drop_table(_TELEMETRY_SPAN)
