"""A run's board-call budget: how many companies it left unfetched, as one nullable column.

Revision ID: 0008_run_companies_not_fetched
Revises: 0007_company_board_validator
Create Date: 2026-10-14 09:12:44.000000

`run.companies_not_fetched` (`Integer`) records how many active companies the
run's own board-call budget (`RUN_REQUEST_BUDGET`) stopped it from fetching,
written by `services.pipeline` when, and only when, the bound really stopped the
walk. It is nullable and defaulted by nothing, so it carries three readings rather
than two: NULL means the budget did not stop this walk -- the run reached the last
active company, failed on a board before the bound, or is a row written before
this column existed -- and a positive value means that many active companies were
not fetched and are left for a later run. `0` is never written, because a run that
reached the end of its watchlist has nothing to report.

Nothing is backfilled. A row written before this revision ran with no budget, so
the honest reading is a NULL, and a `NOT NULL` default of `0` would make every one
of them claim its walk was bounded and complete.

`upgrade()` adds the column only when it is missing, the way the baseline creates
a table only when it is missing and 0002, 0003, 0005, 0006 and 0007 add their
columns only when they are missing: a database `app.db.init_db()` built already
has it, from the models, and `alembic upgrade head` on that database still applies
cleanly instead of failing on a duplicate column. It is added with a plain
`ADD COLUMN`, which both engines express without rebuilding the table.

`downgrade()` drops the column, and the counts go with it. The run rows themselves
-- their status, their end, their error and their other counts -- stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_run_companies_not_fetched"
down_revision: str | Sequence[str] | None = "0007_company_board_validator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN = "run"
_COMPANIES_NOT_FETCHED = "companies_not_fetched"


def _has_the_column() -> bool:
    """Whether the column is already there, so a database `init_db()` built is adopted."""
    return any(
        column["name"] == _COMPANIES_NOT_FETCHED
        for column in sa.inspect(op.get_bind()).get_columns(_RUN)
    )


def upgrade() -> None:
    if _has_the_column():
        return
    op.add_column(_RUN, sa.Column(_COMPANIES_NOT_FETCHED, sa.Integer(), nullable=True))


def downgrade() -> None:
    if not _has_the_column():
        return
    op.drop_column(_RUN, _COMPANIES_NOT_FETCHED)
