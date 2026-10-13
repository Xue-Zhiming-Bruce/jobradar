"""What a run's digest emailed: one nullable column on `run`, and no backfill.

Revision ID: 0006_run_matches_emailed
Revises: 0005_match_provenance
Create Date: 2026-10-13 12:40:00.000000

`run.matches_emailed` (`Integer`) records how many matches that run's own digest
emailed, written by `services.digest.send_digest` on the row its run id names. It
is nullable and defaulted by nothing, so it carries three states rather than two:
`0` means the run emailed nothing, a positive value means that many, and NULL
means "unknown" -- a row written before this column existed, and a run whose send
failed, where the digest writes nothing at all.

Nothing is backfilled. A row written before this revision had no way to say how
many matches it emailed, so a `NOT NULL` default of `0` would make every one of
them claim it emailed nothing -- a lie for any run that did -- and the NULL is
the honest reading.

`upgrade()` adds the column only when it is missing, the way the baseline creates
a table only when it is missing and 0002, 0003 and 0005 add their columns only
when they are missing: a database `app.db.init_db()` built already has it, from
the models, and `alembic upgrade head` on that database still applies cleanly
instead of failing on a duplicate column.

`downgrade()` drops the column, and the counts go with it. The run rows
themselves -- their status, their end, their error and both other counts -- stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_run_matches_emailed"
down_revision: str | Sequence[str] | None = "0005_match_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN = "run"
_MATCHES_EMAILED = "matches_emailed"


def _has_the_column() -> bool:
    """Whether the column is already there, so a database `init_db()` built is adopted."""
    return any(
        column["name"] == _MATCHES_EMAILED
        for column in sa.inspect(op.get_bind()).get_columns(_RUN)
    )


def upgrade() -> None:
    if _has_the_column():
        return
    op.add_column(_RUN, sa.Column(_MATCHES_EMAILED, sa.Integer(), nullable=True))


def downgrade() -> None:
    if not _has_the_column():
        return
    op.drop_column(_RUN, _MATCHES_EMAILED)
