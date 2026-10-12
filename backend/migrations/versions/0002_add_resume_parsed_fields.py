"""The parsed resume fields: one nullable column on `resume`, and no backfill.

Revision ID: 0002_parsed_fields
Revises: 0001_baseline
Create Date: 2026-10-12 00:00:01.000000

`resume.parsed_fields` holds the `{"name": ..., "skills": [...]}` object #26's
`POST /resume/parse` stores, as JSON text, and NULL on a row that was never
parsed. A resume uploaded before this revision is therefore left alone: its NULL
is what makes the next parse of it happen, and neither its text nor its active
flag is touched by `upgrade()`.

`upgrade()` adds the column only when it is missing, the way the baseline creates
a table only when it is missing. `app.db.init_db()` builds the current models, so
a database it built -- the local `backend/data/jobradar.db` after this change,
and every test database -- already has the column, and `alembic upgrade head` on
that database still applies cleanly instead of failing on a duplicate column.

`downgrade()` drops the column: every stored parse goes with it, and nothing
else does. It is not destructive of the resume itself -- its text, its file and
its active flag stay -- but a later `upgrade head` does not bring the parses
back: they were model output, and a call is what produces them again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_parsed_fields"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESUME = "resume"
_PARSED_FIELDS = "parsed_fields"


def _has_parsed_fields() -> bool:
    """Whether that column is already there, so a database `init_db()` built is adopted."""
    return any(
        column["name"] == _PARSED_FIELDS
        for column in sa.inspect(op.get_bind()).get_columns(_RESUME)
    )


def upgrade() -> None:
    if not _has_parsed_fields():
        op.add_column(_RESUME, sa.Column(_PARSED_FIELDS, sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_RESUME, _PARSED_FIELDS)
