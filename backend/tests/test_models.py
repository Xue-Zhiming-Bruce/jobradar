"""The five tables, declared once, under the column names later tasks read.

`__tablename__` may appear in no module other than `app/models.py`: one place
declares table structure, so one place has to be migrated (#21) when it changes.

Everything here reads the metadata and compiles DDL; nothing connects. The two
rendered dialects are the evidence that one generic declaration covers SQLite
and PostgreSQL, and the column tests pin the lengths, the timestamp type and the
Python-side defaults that #2 declared.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import DateTime, Dialect, String
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app import models
from app.models import Base, UTCDateTime

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# Every column a later task reads. Tables may gain columns; these may not go.
_EXPECTED_COLUMNS = {
    "resume": {
        "id",
        "filename",
        "content_type",
        "text",
        "stored_path",
        "uploaded_at",
        "is_active",
    },
    "company": {"id", "name", "slug", "is_active", "created_at"},
    "posting": {
        "id",
        "company_id",
        "external_id",
        "title",
        "location",
        "url",
        "description",
        "first_seen_at",
        "updated_at",
        "closed_at",
    },
    "match": {
        "id",
        "posting_id",
        "score",
        "reason",
        "gaps",
        "state",
        "created_at",
        "notified_at",
    },
    "run": {
        "id",
        "status",
        "started_at",
        "finished_at",
        "error",
        "postings_seen",
        "matches_created",
    },
}

# The length of every string column: the budget #2 declared, so a column is not
# resized by accident. `None` means unbounded, which `String(255)` would not be
# on PostgreSQL -- the seven columns whose source text has no known budget.
_STRING_LENGTHS = {
    ("resume", "filename"): 255,
    ("resume", "content_type"): 100,
    ("resume", "text"): None,
    ("resume", "stored_path"): 500,
    ("company", "name"): 255,
    ("company", "slug"): 255,
    ("posting", "external_id"): 255,
    ("posting", "title"): None,
    ("posting", "location"): 255,
    ("posting", "url"): None,
    ("posting", "description"): None,
    ("match", "reason"): None,
    ("match", "gaps"): None,
    ("match", "state"): 20,
    ("run", "status"): 20,
    ("run", "error"): None,
}

_UNBOUNDED_STRING_COLUMNS = {
    ("resume", "text"),
    ("posting", "title"),
    ("posting", "url"),
    ("posting", "description"),
    ("match", "reason"),
    ("match", "gaps"),
    ("run", "error"),
}

# Every timestamp column, in every table: a timestamp, not a string or an epoch.
_TIMESTAMP_COLUMNS = {
    ("resume", "uploaded_at"),
    ("company", "created_at"),
    ("posting", "first_seen_at"),
    ("posting", "updated_at"),
    ("posting", "closed_at"),
    ("match", "created_at"),
    ("match", "notified_at"),
    ("run", "started_at"),
    ("run", "finished_at"),
}

# The columns whose default is a Python callable or literal, with no server_default.
_DEFAULTED_COLUMNS = {
    ("resume", "uploaded_at"),
    ("resume", "is_active"),
    ("company", "is_active"),
    ("company", "created_at"),
    ("posting", "first_seen_at"),
    ("posting", "updated_at"),
    ("match", "state"),
    ("match", "created_at"),
    ("run", "started_at"),
    ("run", "postings_seen"),
    ("run", "matches_created"),
}

# `match` is a keyword in SQLite, whose DDL quotes it, and is not reserved in
# PostgreSQL, whose DDL leaves it bare: the pair allows both spellings.
_MATCH_TABLE_DDL = {
    "sqlite": 'CREATE TABLE "match" (',
    "postgres": "CREATE TABLE match (",
}


def _ddl(dialect: Dialect) -> str:
    """The same metadata rendered for one dialect, every table in order."""
    return "\n".join(
        str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables
    )


def test_the_five_tables_are_declared_and_no_others() -> None:
    assert set(Base.metadata.tables) == set(_EXPECTED_COLUMNS)


@pytest.mark.parametrize("table_name", sorted(_EXPECTED_COLUMNS))
def test_every_column_a_later_task_reads_is_present(table_name: str) -> None:
    declared = set(Base.metadata.tables[table_name].columns.keys())
    assert _EXPECTED_COLUMNS[table_name] <= declared


def test_no_other_module_declares_table_structure() -> None:
    declaring = sorted(
        path.relative_to(APP_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if "__tablename__" in path.read_text(encoding="utf-8")
    )
    assert declaring == ["models.py"]


def test_no_app_module_names_a_dialect() -> None:
    """One generic declaration per column: no engine gets its own type or DDL."""
    naming_a_dialect = sorted(
        path.relative_to(APP_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if "sqlalchemy.dialects" in path.read_text(encoding="utf-8")
    )
    assert naming_a_dialect == []


def test_every_string_column_carries_the_length_declared_for_it() -> None:
    lengths = {
        (table.name, column.name): column.type.length
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, String)
    }

    assert lengths == _STRING_LENGTHS


def test_only_the_seven_columns_with_no_length_budget_are_unbounded() -> None:
    unbounded = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, String) and column.type.length is None
    }

    assert unbounded == _UNBOUNDED_STRING_COLUMNS


def test_every_timestamp_column_uses_the_one_shared_type() -> None:
    declared = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, UTCDateTime)
    }

    assert declared == _TIMESTAMP_COLUMNS
    # The storage stays a timestamp: `UTCDateTime` wraps `DateTime(timezone=True)`,
    # which PostgreSQL renders as TIMESTAMP WITH TIME ZONE.
    assert all(
        isinstance(Base.metadata.tables[table].columns[column].type.impl, DateTime)
        and Base.metadata.tables[table].columns[column].type.impl.timezone
        for table, column in _TIMESTAMP_COLUMNS
    )


def test_a_timestamp_without_a_utc_offset_is_rejected() -> None:
    """A naive value means two different instants on two engines, so it is refused."""
    with pytest.raises(ValueError, match="UTC offset"):
        UTCDateTime().process_bind_param(datetime(2024, 5, 6, 7, 8, 9), sqlite.dialect())

    aware = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
    assert UTCDateTime().process_bind_param(aware, postgresql.dialect()) == aware


def test_defaults_live_on_the_python_side_and_nowhere_else() -> None:
    """No server_default, so no dialect's DDL holds a literal DEFAULT to diverge on."""
    python_defaulted = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.default is not None or column.onupdate is not None
    }
    server_defaulted = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.server_default is not None
    }

    assert python_defaulted == _DEFAULTED_COLUMNS
    assert server_defaulted == set()


@pytest.mark.parametrize("dialect_name", sorted(_MATCH_TABLE_DDL))
def test_the_rendered_ddl_creates_the_five_tables(dialect_name: str) -> None:
    dialect = sqlite.dialect() if dialect_name == "sqlite" else postgresql.dialect()

    ddl = _ddl(dialect)

    assert _MATCH_TABLE_DDL[dialect_name] in ddl
    assert all(
        f"CREATE TABLE {table.name} (" in ddl
        for table in Base.metadata.sorted_tables
        if table.name != "match"
    )


def test_the_two_rendered_dialects_derive_their_types_from_one_metadata() -> None:
    """The rendered pair is the evidence: no column type was written per engine."""
    sqlite_ddl = _ddl(sqlite.dialect())
    postgres_ddl = _ddl(postgresql.dialect())

    # What each dialect derives for the same declaration: an integer primary key,
    # a timestamp, a bounded string -- and no DEFAULT clause from the metadata.
    assert "id INTEGER NOT NULL" in sqlite_ddl
    assert "id SERIAL NOT NULL" in postgres_ddl
    assert "uploaded_at DATETIME NOT NULL" in sqlite_ddl
    assert "uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL" in postgres_ddl
    assert "title TEXT NOT NULL" in sqlite_ddl
    assert "title TEXT NOT NULL" in postgres_ddl
    assert "filename VARCHAR(255) NOT NULL" in sqlite_ddl
    assert "filename VARCHAR(255) NOT NULL" in postgres_ddl
    assert "DEFAULT" not in sqlite_ddl
    assert "DEFAULT" not in postgres_ddl


def test_the_docstring_says_tables_only_are_created_and_names_21() -> None:
    docstring = models.__doc__ or ""
    assert "#21" in docstring
    assert "never alters" in docstring
