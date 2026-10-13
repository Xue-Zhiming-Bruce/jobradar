"""The score's provenance: two nullable columns on `match`, and no backfill.

Revision ID: 0005_match_provenance
Revises: 0004_match_outcome
Create Date: 2026-10-13 11:40:00.000000

`match.model` (`String(100)`) and `match.prompt_version` (`String(50)`) record
which model produced a stored score and which version of the scoring prompt it was
read with, both written by `scorer.record_score` for a scored row and a not-scored
one alike. Both are nullable and neither is defaulted, so NULL means "unknown": a
rejection row, which no model produced, and every row written before these columns
existed both keep it, and it is not the same as "none".

Nothing is backfilled and no existing text is rewritten. A row written before this
revision had no way to say which model or prompt produced it, so inventing a value
would be a lie; the NULL is the honest reading, and #12 can ask for the columns
for rows written from now on.

`upgrade()` adds each column only when it is missing, the way the baseline creates
a table only when it is missing and 0002 and 0003 add their columns only when they
are missing: a database `app.db.init_db()` built already has both, from the models,
and `alembic upgrade head` on that database still applies cleanly instead of
failing on a duplicate column.

`downgrade()` drops both columns, and the provenance goes with them. The rows
themselves, their scores, their reasons and every other column stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_match_provenance"
down_revision: str | Sequence[str] | None = "0004_match_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MATCH = "match"
_MODEL = "model"
_PROMPT_VERSION = "prompt_version"

# The two columns this revision adds, with the length the models declare.
_COLUMNS = ((_MODEL, 100), (_PROMPT_VERSION, 50))


def _present_columns() -> set[str]:
    """Which of this revision's columns the table already has, so a built database is adopted."""
    declared = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_MATCH)}
    return {name for name, _ in _COLUMNS if name in declared}


def upgrade() -> None:
    present = _present_columns()
    for name, length in _COLUMNS:
        if name not in present:
            op.add_column(_MATCH, sa.Column(name, sa.String(length=length), nullable=True))


def downgrade() -> None:
    present = _present_columns()
    for name, _ in _COLUMNS:
        if name in present:
            op.drop_column(_MATCH, name)
