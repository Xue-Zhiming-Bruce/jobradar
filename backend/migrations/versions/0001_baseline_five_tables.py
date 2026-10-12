"""The five tables, as `app/models.py` declares them, for a database that exists.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-12 00:00:00.000000

The DDL below is written out rather than rendered from the models on purpose: a
revision records what the schema *was*, so the history stays readable after
`app/models.py` moves on, and a run never re-evaluates a model file that has
changed since. `backend/tests/test_migrations.py` is what keeps the two in step:
it runs this revision and compares the result with `Base.metadata` on both
engines, column by column and rule by rule.

`upgrade()` creates a table only when it is missing, so this revision adopts a
database `init_db()` already built -- the local `backend/data/jobradar.db`
included -- instead of failing on a table that is already there. That is why a
plain `alembic upgrade head` is safe on an existing database and no manual
`alembic stamp head` is required.

`downgrade()` drops the five tables, newest dependency first. It is destructive:
the rows go with them. Only the databases that exist today -- the local SQLite
file and the disposable compose volume -- are safe to run it against.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The five tables in dependency order: a table is created after the tables its
# foreign keys point at, and dropped in the reverse order below.
_COMPANY = "company"
_POSTING = "posting"
_MATCH = "match"
_RESUME = "resume"
_RUN = "run"


def _is_missing(table_name: str) -> bool:
    """Whether `table_name` is absent, so an existing database is adopted, not rebuilt."""
    return not sa.inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    if _is_missing(_COMPANY):
        op.create_table(
            _COMPANY,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("slug"),
        )

    if _is_missing(_POSTING):
        op.create_table(
            _POSTING,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("external_id", sa.String(length=255), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("location", sa.String(length=255), nullable=True),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["company_id"], ["company.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "external_id"),
        )

    if _is_missing(_MATCH):
        op.create_table(
            _MATCH,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("posting_id", sa.Integer(), nullable=False),
            sa.Column("score", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("gaps", sa.Text(), nullable=True),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "score >= 0 AND score <= 100", name="ck_match_score_range"
            ),
            sa.CheckConstraint(
                "state IN ('new', 'saved', 'dismissed')", name="ck_match_state_values"
            ),
            sa.ForeignKeyConstraint(["posting_id"], ["posting.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("posting_id"),
        )

    if _is_missing(_RESUME):
        op.create_table(
            _RESUME,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=100), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("stored_path", sa.String(length=500), nullable=False),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        # The predicate is given for both engines, exactly as `app/models.py`
        # declares it: one dialect's predicate alone would leave the other with a
        # plain unique index over `is_active`, which refuses a second *inactive*
        # row -- and several inactive resumes are legitimate history.
        op.create_index(
            "uq_resume_one_active",
            _RESUME,
            ["is_active"],
            unique=True,
            sqlite_where=sa.text("is_active"),
            postgresql_where=sa.text("is_active"),
        )

    if _is_missing(_RUN):
        op.create_table(
            _RUN,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("postings_seen", sa.Integer(), nullable=False),
            sa.Column("matches_created", sa.Integer(), nullable=False),
            sa.CheckConstraint(
                "status IN ('running', 'succeeded', 'failed')",
                name="ck_run_status_values",
            ),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.drop_table(_RUN)
    op.drop_index("uq_resume_one_active", table_name=_RESUME)
    op.drop_table(_RESUME)
    op.drop_table(_MATCH)
    op.drop_table(_POSTING)
    op.drop_table(_COMPANY)
