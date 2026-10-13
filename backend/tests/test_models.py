"""The five tables, declared once, under the column names later tasks read.

`__tablename__` may appear in no module other than `app/models.py`: one place
declares table structure, so one place has to be migrated (#21) when it changes.

Everything here reads the metadata and compiles DDL; nothing connects. The two
rendered dialects are the evidence that one generic declaration covers SQLite
and PostgreSQL, and the column tests pin the lengths, the timestamp type and the
Python-side defaults that #2 declared. The rules of #23 -- the dedupe keys, the
single active resume, the score range, the two vocabularies and the cascading
foreign keys -- are pinned here as declared, and refused by the engines in
`tests/test_engine_parity.py`.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, DateTime, Dialect, Integer, String, UniqueConstraint
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app import models
from app.models import Base, Company, Match, Posting, Resume, Run, UTCDateTime

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
        "outcome",
        "model",
        "prompt_version",
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
        "matches_emailed",
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
    ("resume", "parsed_fields"): None,
    ("company", "name"): 255,
    ("company", "slug"): 255,
    ("company", "source"): 20,
    ("posting", "external_id"): 255,
    ("posting", "title"): None,
    ("posting", "location"): 255,
    ("posting", "url"): None,
    ("posting", "description"): None,
    ("match", "reason"): None,
    ("match", "gaps"): None,
    ("match", "state"): 20,
    ("match", "outcome"): 20,
    ("match", "model"): 100,
    ("match", "prompt_version"): 50,
    ("run", "status"): 20,
    ("run", "error"): None,
}

_UNBOUNDED_STRING_COLUMNS = {
    ("resume", "text"),
    ("resume", "parsed_fields"),
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

# The columns whose default is a Python callable or literal. `outcome` is not
# one of them: its default is the database's own.
_DEFAULTED_COLUMNS = {
    ("resume", "uploaded_at"),
    ("resume", "is_active"),
    ("company", "is_active"),
    ("company", "created_at"),
    ("company", "source"),
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


def _dialect(dialect_name: str) -> Dialect:
    """One of the two dialects the suite runs on, by the name the matrix uses."""
    return sqlite.dialect() if dialect_name == "sqlite" else postgresql.dialect()


def _both_ddl() -> dict[str, str]:
    """The whole schema as SQLite and PostgreSQL each render it."""
    return {name: _ddl(_dialect(name)) for name in ("sqlite", "postgres")}


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


def _assigns_a_match_column(source: str, column: str) -> bool:
    """Whether this module writes a `match` row's `column`: `row.column = ...` or `Match(column=...)`.

    Those two spellings and no others, because the fact is who *writes* the
    column: rendering it into a response (`MatchSummary(column=...)`), comparing
    it (`Match.column != ...`) and declaring it in the model are none of them
    writes.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Match":
            if any(keyword.arg == column for keyword in node.keywords):
                return True
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            targets = []
        if any(getattr(target, "attr", None) == column for target in targets):
            return True
    return False


def test_the_two_writers_are_the_only_modules_that_assign_an_outcome() -> None:
    """One writer per state: only the prefilter and the scorer decide an outcome."""
    assigning = sorted(
        path.relative_to(APP_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if _assigns_a_match_column(path.read_text(encoding="utf-8"), "outcome")
    )

    assert assigning == ["matching/prefilter.py", "matching/scorer.py"]


def test_only_the_scorer_writes_the_provenance_columns() -> None:
    """One writer for the model and the prompt version: the scorer's `record_score`.

    `record_rejection` writes neither -- a rejection ran no model -- so the
    prefilter is not one of them, and no other module assigns either column.
    """
    for column in ("model", "prompt_version"):
        assigning = sorted(
            path.relative_to(APP_ROOT).as_posix()
            for path in APP_ROOT.rglob("*.py")
            if _assigns_a_match_column(path.read_text(encoding="utf-8"), column)
        )

        assert assigning == ["matching/scorer.py"], column


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


def test_the_count_the_digest_writes_is_nullable_and_defaulted_by_neither_side() -> None:
    """Three states, and `0` is one of them, so nothing fills the column in.

    A `NOT NULL` column with a default of `0` would make every row written before
    the digest recorded its count claim it emailed nothing (#36), so this one is
    left to its only writer. Nullability is not one of the facts
    `_DEFAULTED_COLUMNS` covers, hence the two assertions here.
    """
    column = Run.__table__.c.matches_emailed

    assert isinstance(column.type, Integer)
    assert column.nullable is True
    assert column.default is None and column.server_default is None


def test_defaults_are_python_side_except_the_outcome_the_database_fills() -> None:
    """Every default but one is the Python side's; `outcome` is the engine's.

    `outcome` is `NOT NULL` with a `server_default` and no Python default, so a
    row inserted without it -- a test's own seed, or one written before the
    column existed -- reads `scored` rather than NULL. Every other default is
    Python-side, so no other column's DDL holds a literal DEFAULT.
    """
    python_defaulted = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.default is not None or column.onupdate is not None
    }
    server_defaulted = {
        (table.name, column.name): str(column.server_default.arg)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.server_default is not None
    }

    assert python_defaulted == _DEFAULTED_COLUMNS
    assert server_defaulted == {("match", "outcome"): "scored"}


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
    # a timestamp, a bounded string -- and no DEFAULT clause from the metadata
    # except the one `outcome` declares as its server default.
    assert "id INTEGER NOT NULL" in sqlite_ddl
    assert "id SERIAL NOT NULL" in postgres_ddl
    assert "uploaded_at DATETIME NOT NULL" in sqlite_ddl
    assert "uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL" in postgres_ddl
    assert "title TEXT NOT NULL" in sqlite_ddl
    assert "title TEXT NOT NULL" in postgres_ddl
    assert "filename VARCHAR(255) NOT NULL" in sqlite_ddl
    assert "filename VARCHAR(255) NOT NULL" in postgres_ddl
    # One DEFAULT in the whole schema, and it is `outcome`'s server default.
    for ddl in (sqlite_ddl, postgres_ddl):
        assert ddl.count("DEFAULT") == 1
        assert "outcome VARCHAR(20) DEFAULT 'scored' NOT NULL" in ddl


def test_the_docstring_says_tables_only_are_created_and_names_21() -> None:
    docstring = models.__doc__ or ""
    assert "#21" in docstring
    assert "never alters" in docstring


# The rules the database enforces on both engines (#23), pinned as declared:
# what each engine is asked to refuse, and the portable spelling it is asked in.


def test_the_dedupe_keys_are_declared_as_unique_constraints() -> None:
    """The three keys a re-run, a double click or a re-score must not get past."""
    unique_constraints = {
        table.name: [
            [column.name for column in constraint.columns]
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        for table in Base.metadata.sorted_tables
    }

    assert Company.__table__.c.slug.unique is True
    assert Match.__table__.c.posting_id.unique is True
    assert unique_constraints["company"] == [["slug"]]
    assert unique_constraints["posting"] == [["company_id", "external_id"]]
    assert unique_constraints["match"] == [["posting_id"]]


def test_uniqueness_never_leans_on_a_collation() -> None:
    """No `COLLATE` and no expression index: the models normalise the slug instead."""
    for dialect_name, ddl in _both_ddl().items():
        assert "COLLATE" not in ddl, dialect_name
        assert "lower(" not in ddl, dialect_name


def test_the_one_active_resume_index_is_partial_for_both_engines() -> None:
    (index,) = Resume.__table__.indexes

    assert index.unique is True
    assert index.dialect_options["sqlite"]["where"] is not None
    assert index.dialect_options["postgresql"]["where"] is not None
    rendered = {
        dialect_name: str(CreateIndex(index).compile(dialect=_dialect(dialect_name))).strip()
        for dialect_name in ("sqlite", "postgres")
    }

    assert set(rendered.values()) == {
        "CREATE UNIQUE INDEX uq_resume_one_active ON resume (is_active) WHERE is_active"
    }


def test_the_checks_are_named_and_carry_plain_string_columns() -> None:
    checks = {
        (table.name, constraint.name): str(constraint.sqltext)
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert checks == {
        ("company", "ck_company_source_values"): "source IN ('greenhouse', 'lever')",
        ("match", "ck_match_score_range"): "score >= 0 AND score <= 100",
        ("match", "ck_match_state_values"): "state IN ('new', 'saved', 'dismissed')",
        ("match", "ck_match_outcome_values"): (
            "outcome IN ('scored', 'rejected', 'not_scored')"
        ),
        ("run", "ck_run_status_values"): "status IN ('running', 'succeeded', 'failed')",
    }
    for dialect_name, ddl in _both_ddl().items():
        assert all(
            f"CONSTRAINT {name} CHECK ({rule})" in ddl for (_, name), rule in checks.items()
        ), dialect_name
        # The vocabulary rides on a bounded string on both engines: no ENUM type
        # and no dialect import, which `test_no_app_module_names_a_dialect` holds.
        assert "source VARCHAR(20) NOT NULL" in ddl, dialect_name
        assert "state VARCHAR(20) NOT NULL" in ddl, dialect_name
        # The one vocabulary that is also server-defaulted renders its DEFAULT
        # between the type and NOT NULL, on both engines.
        assert "outcome VARCHAR(20) DEFAULT 'scored' NOT NULL" in ddl, dialect_name
        assert "status VARCHAR(20) NOT NULL" in ddl, dialect_name


def test_every_foreign_key_cascades_on_both_engines() -> None:
    declared = {
        (table.name, foreign_key.parent.name): foreign_key.ondelete
        for table in Base.metadata.sorted_tables
        for foreign_key in table.foreign_keys
    }

    assert declared == {
        ("posting", "company_id"): "CASCADE",
        ("match", "posting_id"): "CASCADE",
    }
    for dialect_name, ddl in _both_ddl().items():
        assert ddl.count("ON DELETE CASCADE") == 2, dialect_name


def test_the_slug_is_a_bounded_string_column_like_the_lengths_test_expects() -> None:
    """Normalisation is the model's job, not a type's: the column is a plain string."""
    assert isinstance(Company.__table__.c.slug.type, String)
    assert Company.__table__.c.slug.type.length == 255


def test_every_column_a_constraint_rides_on_is_not_nullable() -> None:
    """A `NULL` collides with nothing and a `CHECK` over it passes, on both engines."""
    riding_on_a_rule = [
        Company.__table__.c.slug,
        Resume.__table__.c.is_active,
        Posting.__table__.c.company_id,
        Posting.__table__.c.external_id,
        Match.__table__.c.posting_id,
        Match.__table__.c.score,
        Match.__table__.c.state,
        Match.__table__.c.outcome,
        Run.__table__.c.status,
    ]

    assert [column.name for column in riding_on_a_rule if column.nullable] == []
