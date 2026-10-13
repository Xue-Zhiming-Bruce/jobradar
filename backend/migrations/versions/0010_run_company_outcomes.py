"""One company's outcome in one run: the `run_company` table, and nothing else.

Revision ID: 0010_run_company_outcomes
Revises: 0009_company_source_slug_unique
Create Date: 2026-10-15 10:40:00.000000

`run_company(id, run_id, company_id, outcome, error)` is where a run says what it
did with each company it attempted (#59). `run_id` and `company_id` are each a
foreign key that cascades, so a deleted run or company takes its outcome rows with
it; `outcome` is `String(20)`, `NOT NULL`, checked
`outcome IN ('fetched', 'not_modified', 'failed')` as `ck_run_company_outcome_values`;
`error` is `Text` and nullable, holding a failed company's own message -- a slug and
a status at most, never a response body, a credential or a resume; and
`UniqueConstraint("run_id", "company_id", name="uq_run_company_outcome")` is what
makes one outcome per company per run the database's rule rather than the walk's.

One row is written per company the walk attempted. A company the run's budget never
reached gets no row: it was not attempted, and `run.companies_not_fetched` -- the
column 0008 added -- already counts it. A budget-stopped run is therefore still
`succeeded`, and a failed board is a `'failed'` row that makes its run `failed`.

`upgrade()` creates the table only when it is missing, the guard 0003, 0007, 0008
and 0009 established: a database `app.db.init_db()` built already has the table
from the models, and `alembic upgrade head` on that database applies cleanly
instead of failing on a table that is already there. The `create_table` call is
what autogenerate wrote (`uv run alembic revision --autogenerate -m "run company
outcomes"`), read and kept as it stands -- the check, both cascading keys and the
unique key included -- and the guard that reads the engine's tables is
hand-written, because autogenerate detects no guard. No other table, column or row
is touched: this revision only adds the sixth table.

`downgrade()` drops the table and nothing else. Nothing holds a reference to it --
the two foreign keys point the other way -- so no row of `run`, `company`, `posting`
or `match` is deleted or rewritten by either direction.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_run_company_outcomes"
down_revision: str | Sequence[str] | None = "0009_company_source_slug_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_COMPANY = "run_company"
_OUTCOME_CHECK = "ck_run_company_outcome_values"
_OUTCOME_UNIQUE = "uq_run_company_outcome"


def _has_the_table() -> bool:
    """Whether the table is already there, so a database `init_db()` built is adopted."""
    return _RUN_COMPANY in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_the_table():
        return
    op.create_table(
        _RUN_COMPANY,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('fetched', 'not_modified', 'failed')", name=_OUTCOME_CHECK
        ),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "company_id", name=_OUTCOME_UNIQUE),
    )


def downgrade() -> None:
    if not _has_the_table():
        return
    op.drop_table(_RUN_COMPANY)
