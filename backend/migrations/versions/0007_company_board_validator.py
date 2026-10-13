"""The board validator: two nullable columns on `company`, and no backfill.

Revision ID: 0007_company_board_validator
Revises: 0006_run_matches_emailed
Create Date: 2026-10-13 20:05:38.199501

`company.etag` and `company.last_modified` (both `Text`) hold the validator the
board's last stored answer carried, written by `services.pipeline` after
`ingest_postings` returned and sent back by the next run as `If-None-Match` and
`If-Modified-Since`. `Text` and not `String(n)`, because both values are the
server's own opaque strings -- an `ETag` keeps its quotes, a `Last-Modified` is
echoed as the date string it arrived as -- and nothing parses either into a
`datetime`. Both are nullable and defaulted by neither side: NULL means this
company has no validator yet, which is every row written before this revision.

Nothing is backfilled. A row written before this revision has no validator, so
its next run asks an unconditional question and stores whatever the answer
carries -- the honest reading, and one unconditional request rather than a board
skipped for a validator this app never had.

`upgrade()` adds each column only when it is missing, the way the baseline creates
a table only when it is missing and 0002, 0003, 0005 and 0006 add their columns
only when they are missing: a database `app.db.init_db()` built already has both,
from the models, and `alembic upgrade head` on that database still applies cleanly
instead of failing on a duplicate column. Both are added with a plain
`ADD COLUMN`, which both engines express without rebuilding the table.

`downgrade()` drops both columns, and the validators go with them. The companies
themselves -- their name, slug, source, active flag and postings -- stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_company_board_validator"
down_revision: str | Sequence[str] | None = "0006_run_matches_emailed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPANY = "company"

# The two columns this revision adds: the board's validator, verbatim and unbounded.
_COLUMNS = ("etag", "last_modified")


def _present_columns() -> set[str]:
    """Which of this revision's columns the table already has, so a built database is adopted."""
    declared = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_COMPANY)}
    return {name for name in _COLUMNS if name in declared}


def upgrade() -> None:
    present = _present_columns()
    for name in _COLUMNS:
        if name not in present:
            op.add_column(_COMPANY, sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    present = _present_columns()
    for name in _COLUMNS:
        if name in present:
            op.drop_column(_COMPANY, name)
