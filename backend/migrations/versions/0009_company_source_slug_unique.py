"""A company's key is the pair `(source, slug)`: `UNIQUE (slug)` becomes one named constraint.

Revision ID: 0009_company_source_slug_unique
Revises: 0008_run_companies_not_fetched
Create Date: 2026-10-15 09:20:00.000000

`company.slug` carried an inline `unique=True`, which both engines store as an
unnamed `UNIQUE (slug)`, so the same board could be watched on one source only.
The key becomes `UniqueConstraint("source", "slug", name="uq_company_source_slug")`:
the same slug may live on Greenhouse and on Lever at once, and a second row of one
pair is still refused. No column moves, no row is rewritten and nothing is
backfilled -- the old key is a strict subset of the new one, so every row the
narrower rule held is a row the wider one holds too.

SQLite cannot drop a table constraint in place, and the old one is unnamed, which
is why this is a `batch_alter_table` rebuild rather than an `ALTER`. `copy_from`
restates the table as 0008 left it -- its eight columns, the source check, and the
old unique under the name PostgreSQL itself gives an unnamed single-column
`UNIQUE`, `company_slug_key` -- so the drop binds to a name on both engines:
SQLite rebuilds the table and copies its rows, and PostgreSQL, where
`requires_recreate_in_batch` is `False`, runs a plain `DROP CONSTRAINT` plus
`ADD CONSTRAINT` and rebuilds nothing. A `naming_convention` would have named the
constraint for SQLite only and sent PostgreSQL the wrong name.

`upgrade()` reads the engine's own unique constraints first and returns when one
over `["source", "slug"]` is already there, the guard 0003/0007/0008 established:
a database `app.db.init_db()` built already has the new key from the models, and
`alembic upgrade head` on it applies cleanly instead of failing on a duplicate
constraint. Nothing of the old key is dropped on such a database either.

Both directions run with SQLite's foreign keys off. The rebuild drops the real
`company` table and renames its copy into place, and every SQLite connection this
app hands out enforces foreign keys (`app.db`'s `PRAGMA foreign_keys=ON`), so
`posting` and `match` -- which cascade from `company` -- would be deleted with the
table they reference: a migration that wiped the watchlist's postings. The guard
is put back before the revision ends, so nothing else runs unguarded. PostgreSQL
needs none of this: it alters in place.

`downgrade()` restores `UNIQUE (slug)` and refuses rather than half-rebuilds. Two
rows sharing a slug on two sources cannot exist under the narrower key, and a
rebuild that tried fails *after* creating `_alembic_tmp_company`, leaving that
stray table behind while the real table is untouched. So one query runs first --
`SELECT slug FROM company GROUP BY slug HAVING COUNT(*) > 1` -- and a non-empty
answer is a `RuntimeError` naming the slugs, before any DDL. Nothing is deleted
and no row is rewritten on either path.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_company_source_slug_unique"
down_revision: str | Sequence[str] | None = "0008_run_companies_not_fetched"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPANY = "company"
_SLUG = "slug"
_SOURCE = "source"
_SOURCE_CHECK = "ck_company_source_values"
_OLD_SLUG_UNIQUE = "company_slug_key"
_NEW_KEY = "uq_company_source_slug"
_DUPLICATE_SLUGS = "SELECT slug FROM company GROUP BY slug HAVING COUNT(*) > 1 ORDER BY slug"


def _company(unique_name: str, unique_columns: list[str]) -> sa.Table:
    """`company` as one side of the swap leaves it, for `copy_from` to stand in for reflection.

    `copy_from` replaces what Alembic would reflect, and reflection is exactly what
    loses the old key's name: SQLite reports `UNIQUE (slug)` as `{'name': None}`, so
    a `drop_constraint` by name has nothing to bind to.
    """
    return sa.Table(
        _COMPANY,
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(_SLUG, sa.String(length=255), nullable=False),
        sa.Column(_SOURCE, sa.String(length=20), nullable=False, server_default="greenhouse"),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.CheckConstraint("source IN ('greenhouse', 'lever')", name=_SOURCE_CHECK),
        sa.UniqueConstraint(*unique_columns, name=unique_name),
    )


def _has_the_widened_key() -> bool:
    """Whether `(source, slug)` is already the key, so a database `init_db()` built is adopted."""
    return any(
        constraint["column_names"] == [_SOURCE, _SLUG]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(_COMPANY)
    )


def _slugs_on_two_rows() -> list[str]:
    """Every slug two rows share: what `UNIQUE (slug)` below cannot hold.

    Read with SQL rather than the model, which declares a key this schema is only
    about to be given.
    """
    rows = op.get_bind().execute(sa.text(_DUPLICATE_SLUGS))
    return [row[0] for row in rows]


@contextmanager
def _rebuild_without_cascading() -> Iterator[None]:
    """Run a batch rebuild with SQLite's foreign keys off, so dropping `company` cannot cascade.

    `posting` and `match` cascade from `company`, and the batch form drops the real
    table, so with the app's own `PRAGMA foreign_keys=ON` in force the rebuild would
    delete every posting of every company it copied. PostgreSQL alters in place and
    is left alone.

    The pragma is a no-op inside a transaction, and SQLite keeps one open across the
    batch's DDL, so the driver's transaction is ended before the guard goes back on:
    committed when the rebuild went through, rolled back when it did not, because
    SQLite's DDL is transactional and a half-done rebuild must not be made durable.
    The SQLAlchemy-side transaction is deliberately left open: alembic writes this
    revision's `alembic_version` row after this function returns.
    """
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    driver = bind.connection.dbapi_connection
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
    except Exception:
        driver.rollback()
        raise
    finally:
        driver.commit()
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    if _has_the_widened_key():
        return
    with _rebuild_without_cascading(), op.batch_alter_table(
        _COMPANY, copy_from=_company(_OLD_SLUG_UNIQUE, [_SLUG])
    ) as batch_op:
        batch_op.drop_constraint(_OLD_SLUG_UNIQUE, type_="unique")
        batch_op.create_unique_constraint(_NEW_KEY, [_SOURCE, _SLUG])


def downgrade() -> None:
    if not _has_the_widened_key():
        return
    duplicates = _slugs_on_two_rows()
    if duplicates:
        raise RuntimeError(
            "these slugs are watched on two sources, and UNIQUE (slug) cannot hold"
            " both rows: " + ", ".join(duplicates)
        )
    with _rebuild_without_cascading(), op.batch_alter_table(
        _COMPANY, copy_from=_company(_NEW_KEY, [_SOURCE, _SLUG])
    ) as batch_op:
        batch_op.drop_constraint(_NEW_KEY, type_="unique")
        batch_op.create_unique_constraint(_OLD_SLUG_UNIQUE, [_SLUG])
