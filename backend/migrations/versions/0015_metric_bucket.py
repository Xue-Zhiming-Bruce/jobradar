"""The eighth table: `metric_bucket`, the metrics window as per-route, per-minute buckets.

Revision ID: 0015_metric_bucket
Revises: 0014_run_trace_id
Create Date: 2026-10-18 22:55:00.000000

`metric_bucket(id, route, bucket_start, count, errors, p95_ms)` is the whole-span
table #90 added, folded into the unit a chart reads a series in (#105): one row per
route and `app.telemetry.METRICS_BUCKET` -- one clock minute -- holding how many of
that route's spans started in it, how many of them answered 500 or more, and the
nearest-rank 95th percentile of their durations in whole milliseconds.
`UniqueConstraint("route", "bucket_start", name="uq_metric_bucket_route_start")` is
what makes "one row per route and minute" the database's rule rather than the
writer's, the way `uq_run_company_outcome` does for `run_company`. `route` holds a
route template or the `app.telemetry.UNMATCHED_ROUTE` sentinel and never a raw path,
and the three numbers are counted from stored spans, so no caller-chosen value and no
resume text has a column to reach.

`bucket_start` is this table's one timestamp and it is minute-aligned, and this
revision adds no column to `telemetry_span`: the span table stays byte for byte as
#90 left it. The table is written by `app.telemetry.store_span`, in the same
transaction as the span it counts, so a span write that failed leaves no bucket
behind; read by `GET /telemetry/metrics/buckets`; and deleted by
`app.telemetry.prune_spans` with the spans it counts, which is what keeps it as
bounded as `telemetry_span`.

`upgrade()` creates the table only when it is missing, the guard 0003 and every
revision since established: a database `app.db.init_db()` built already has the table
from the models, and `alembic upgrade head` on that database applies cleanly instead
of failing on a table that is already there. The `create_table` call is what
`uv run alembic revision --autogenerate` wrote, read and kept as it stands -- the
unique key included -- and the guard that reads the engine's tables is hand-written,
because autogenerate detects no guard. The column type is spelled the way 0013 spelled
it, `sa.DateTime(timezone=True)`, which is what `app.models.UTCDateTime` stores.

`downgrade()` drops the table and nothing else. No foreign key points at it, so no row
of `telemetry_span` or of any other table is deleted or rewritten by either direction.
Nothing is backfilled in either direction: no bucket existed before this revision ran,
and the spans to count one from are still there if it is re-applied.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_metric_bucket"
down_revision: str | Sequence[str] | None = "0014_run_trace_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METRIC_BUCKET = "metric_bucket"
_ROUTE_START_UNIQUE = "uq_metric_bucket_route_start"


def _has_the_table() -> bool:
    """Whether the table is already there, so a database `init_db()` built is adopted."""
    return _METRIC_BUCKET in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_the_table():
        return
    op.create_table(
        _METRIC_BUCKET,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("p95_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("route", "bucket_start", name=_ROUTE_START_UNIQUE),
    )


def downgrade() -> None:
    if not _has_the_table():
        return
    op.drop_table(_METRIC_BUCKET)
