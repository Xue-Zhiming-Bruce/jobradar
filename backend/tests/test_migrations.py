"""The migration path, measured against the models on both engines.

`app.db.init_db()` creates the five tables in an empty database; the revisions in
`migrations/versions/` bring a database that already exists to the same five
tables -- the baseline, then the column #26 added, then the source column and
check #28 added, then the outcome column, its check and its backfill #30 added,
then the model and prompt-version columns #32 added, then the run's emailed
count #36 added, then the two board-validator columns #55 added. Two paths to one schema
can drift, so nothing here assumes a revision is right: every test runs `alembic
upgrade head` -- in-process, through `alembic.command.upgrade` -- against the
database the shared `engine` fixture in `tests/conftest.py` supplies, and then
compares the *result* with `app.models`.
That fixture is what makes this both engines: SQLite always, PostgreSQL whenever
`TEST_DATABASE_URL` is set, skipped visibly when it is not. Every database a test
touches is the fixture's -- a temporary SQLite file, or the server that variable
names -- and never `backend/data/jobradar.db`.

What is measured, in the order the guarantees build:

- the migrated schema against `Base.metadata`: `alembic.autogenerate.compare_metadata`,
  then the tables, columns, checks, partial index and foreign keys the engine
  reflects back, with the DDL compiled for both dialects the way
  `tests/test_models.py` compiles it (`CreateTable(...)` and `CreateIndex(...)`
  against `sqlalchemy.dialects.sqlite` and `sqlalchemy.dialects.postgresql`);
- the rules of #23 *on* that schema -- a duplicate slug, a second active resume, a
  score of 101, a state outside the vocabulary, a foreign key to a missing row --
  asked through the app's own `session_scope()`, the way
  `tests/test_engine_parity.py` asks them of `init_db()`'s schema;
- a database `init_db()` already built and filled: `upgrade head` adopts it and a
  second run sends no DDL at all;
- a database at the baseline, with rows in it: the revision that adds a column
  adds it, and the rows keep their text and their active flag;
- a database at #28, with one rejection, one not-scored result and one score in
  it: the revision that adds `outcome` backfills all three from the reasons the
  writers wrote, and rewrites no text;
- the four commands of the layout, each against the fixture's database;
- drift: the current models produce no diff at `head`, and one appended column in
  a deep copy of `Base.metadata` produces a diff naming that column.

One reading of a criterion is recorded here because the code depends on it: an
`upgrade head` that adopts an existing database still creates alembic's own
`alembic_version` table, which is how `alembic current` can report `head` at all.
"Creates nothing" therefore means none of the five tables, and the assertions
below name the five rather than counting statements.
"""

import copy
import io
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Column, Dialect, MetaData, Table, Text, event, inspect, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from app import db
from app.matching.prefilter import REJECTION_REASON_PREFIX
from app.matching.scorer import NOT_SCORED_PREFIX
from app.models import Base, Company, Match, Posting, Resume

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
REVISIONS_DIR = BACKEND_ROOT / "migrations" / "versions"

# The readable revision ids, in the order they apply, and the files that hold
# them. The head is the last of the seven.
BASELINE_REVISION = "0001_baseline"
BASELINE_REVISION_FILE = "0001_baseline_five_tables.py"
PARSED_FIELDS_REVISION = "0002_parsed_fields"
PARSED_FIELDS_REVISION_FILE = "0002_add_resume_parsed_fields.py"
SOURCE_REVISION = "0003_company_source"
SOURCE_REVISION_FILE = "0003_company_source.py"
OUTCOME_REVISION = "0004_match_outcome"
OUTCOME_REVISION_FILE = "0004_match_outcome.py"
PROVENANCE_REVISION = "0005_match_provenance"
PROVENANCE_REVISION_FILE = "0005_match_provenance.py"
RUN_MATCHES_EMAILED_REVISION = "0006_run_matches_emailed"
RUN_MATCHES_EMAILED_REVISION_FILE = "0006_run_matches_emailed.py"
BOARD_VALIDATOR_REVISION = "0007_company_board_validator"
BOARD_VALIDATOR_REVISION_FILE = "0007_company_board_validator.py"
THE_REVISION_FILES = (
    BASELINE_REVISION_FILE,
    PARSED_FIELDS_REVISION_FILE,
    SOURCE_REVISION_FILE,
    OUTCOME_REVISION_FILE,
    PROVENANCE_REVISION_FILE,
    RUN_MATCHES_EMAILED_REVISION_FILE,
    BOARD_VALIDATOR_REVISION_FILE,
)
HEAD_REVISION = BOARD_VALIDATOR_REVISION

# Alembic's own bookkeeping table: one row per applied revision. It is not one of
# the five the models declare, and `alembic current` reads it.
ALEMBIC_VERSION_TABLE = "alembic_version"

# The five tables `Base.metadata` holds, sorted so a failure reads in one order.
THE_FIVE_TABLES = tuple(sorted(Base.metadata.tables))

# The one live index this schema declares, and the DDL both dialects render it as.
# The predicate is what keeps a second *inactive* resume legal.
PARTIAL_INDEX_DDL = (
    "CREATE UNIQUE INDEX uq_resume_one_active ON resume (is_active) WHERE is_active"
)

# Test data is synthetic on purpose: the repository is public and holds no
# resume, real or otherwise.
SYNTHETIC_RESUME_TEXT = (
    "SYNTHETIC RESUME TEXT. Written for this test; not a real person's resume.\n"
    "Skills: Python, SQL."
)


def _config() -> Config:
    """The committed alembic config, read the way `alembic -c alembic.ini` reads it.

    `stdout` is where `command.current` writes, so a test reads what
    `alembic current` prints instead of parsing a terminal.
    """
    return Config(str(ALEMBIC_INI), stdout=io.StringIO())


def _drop_alembic_version(engine: Engine) -> None:
    """Remove alembic's bookkeeping table, if the database has one."""
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")


@pytest.fixture
def database(engine: Engine) -> Iterator[Engine]:
    """The fixture's database, with alembic's own table removed before and after the test.

    What it holds is the schema `init_db()` builds and no revision applied: the
    state a developer's own `backend/data/jobradar.db` is in the first time
    `alembic upgrade head` meets it. `_migrate()` empties it and lets the baseline
    build it; the adoption tests below use it as it is.

    `alembic_version` has to go *before* a test as well as after it, because a
    database that still says it is at `head` -- the PostgreSQL test database after
    an earlier test, or after an earlier run of the suite -- would make
    `upgrade head` a no-op and the comparison below vacuous. It has to go after one
    too: `tests/conftest.py` drops the five tables and promises to leave the server
    as it was found.
    """
    _drop_alembic_version(engine)
    yield engine
    _drop_alembic_version(engine)


def _migrate(engine: Engine) -> None:
    """Empty the fixture's database and let the baseline revision build it instead.

    `init_db()` has already created the same five tables in it: dropping them is
    what makes what follows evidence about the *revision* rather than about the
    models' own `create_all`.
    """
    Base.metadata.drop_all(engine)
    command.upgrade(_config(), "head")


def _dialect(dialect_name: str) -> Dialect:
    """One of the two dialects the suite compiles DDL for, by the name the matrix uses."""
    return sqlite.dialect() if dialect_name == "sqlite" else postgresql.dialect()


def _dialect_of(engine: Engine) -> Dialect:
    """The dialect the engine runs, as the compiler object the DDL checks use."""
    return _dialect("sqlite" if engine.dialect.name == "sqlite" else "postgres")


def _reflected_tables(engine: Engine) -> MetaData:
    """The five tables as the engine itself reports them, not as the models declare them."""
    metadata = MetaData()
    metadata.reflect(bind=engine, only=list(THE_FIVE_TABLES))
    return metadata


def _diff(engine: Engine, metadata: MetaData) -> list[object]:
    """What autogenerate would write: the difference between `metadata` and the database."""
    with engine.connect() as connection:
        return compare_metadata(MigrationContext.configure(connection), metadata)


def _model_columns(table: Table, dialect: Dialect) -> dict[str, tuple[str, bool]]:
    """Every column of a declared table: its type as this dialect renders it, and nullability."""
    return {
        column.name: (column.type.compile(dialect), column.nullable) for column in table.columns
    }


def _reflected_columns(engine: Engine, table_name: str) -> dict[str, tuple[str, bool]]:
    """The same two facts, for a table the database reports."""
    dialect = _dialect_of(engine)
    return {
        column["name"]: (column["type"].compile(dialect), column["nullable"])
        for column in inspect(engine).get_columns(table_name)
    }


def _check_names(table: Table, dialect: Dialect) -> list[str]:
    """The named CHECK constraints in the DDL this table renders for a dialect."""
    return re.findall(r"CONSTRAINT (\w+) CHECK", str(CreateTable(table).compile(dialect=dialect)))


def _ddl_statements_sent_by(engine: Engine, action: Callable[[], None]) -> list[str]:
    """Every CREATE or DROP the engine sent while `action` ran, as one flat line each."""
    statements: list[str] = []

    def record(connection: object, cursor: object, statement: str, *rest: object) -> None:
        if re.match(r"\s*(CREATE|DROP)\b", statement, re.IGNORECASE):
            statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


def _applied_revisions(engine: Engine) -> list[str]:
    """The rows in `alembic_version`: which revision the database says it is at."""
    with engine.connect() as connection:
        return [
            row[0] for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        ]


def _written_rows(engine: Engine) -> tuple[object, ...]:
    """The row the adoption test writes, read back through the models."""
    with db.session_scope() as session:
        company = session.scalars(select(Company)).one()
        posting = session.scalars(select(Posting)).one()
        match = session.scalars(select(Match)).one()
        return (
            company.name,
            company.slug,
            company.created_at,
            posting.external_id,
            posting.title,
            posting.url,
            match.score,
            match.reason,
            match.state,
        )


def _assert_the_engine_named_what_it_refused(exception: IntegrityError, *spellings: str) -> None:
    """Both engines name the rule they refused, each in its own spelling.

    SQLite names a unique constraint by its columns and a named check by its name;
    PostgreSQL names the constraint itself, which for the constraints left to
    SQLAlchemy is `<table>_<columns>_key`. Any of the spellings may match -- the
    engine's whole message is reported when none of them does.
    """
    message = str(exception)
    assert any(spelling in message for spelling in spellings), message


def _resume_rows_before_the_column(engine: Engine) -> list[tuple[object, ...]]:
    """The `resume` values a database at the baseline holds, read with SQL.

    Read with SQL rather than through `Resume`, because the model declares the
    column this test is about to add and a SELECT naming it would fail on the
    schema that predates it -- which is the schema this reading is evidence
    about.
    """
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT id, filename, content_type, text, stored_path, is_active"
                " FROM resume ORDER BY id"
            )
        ]


def _store_a_resume_row_at_the_baseline(engine: Engine, filename: str, *, active: bool) -> None:
    """Insert one resume row with SQL, the way the app stored them before this revision.

    SQL rather than the model, because the model declares the column this case is
    about to add and an INSERT naming it fails on the schema that predates it --
    which is the schema this row is written into.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO resume"
                " (filename, content_type, text, stored_path, uploaded_at, is_active)"
                " VALUES (:filename, 'text/plain', :text, :stored_path,"
                " '2026-01-01 00:00:00', :is_active)"
            ),
            {
                "filename": filename,
                "text": SYNTHETIC_RESUME_TEXT,
                "stored_path": f"/tmp/{filename}",
                "is_active": active,
            },
        )


def _parsed_fields_values(engine: Engine) -> list[object]:
    """The new column's values, read with SQL for the reason above."""
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.exec_driver_sql("SELECT parsed_fields FROM resume ORDER BY id")
        ]


def _store_a_match_row_before_this_revision(
    engine: Engine, *, external_id: str, score: int, reason: str
) -> None:
    """Insert one match row with only the columns this schema has always had.

    All three rows go in with SQL: the model declares columns the schema under test
    does not have yet -- `company` gains the two validator columns #55 added -- so
    an INSERT naming them would fail on the schema that predates them, which is the
    schema this row is written into. `reason` is the caller's, written from the
    writers' own prefix constants, so a prefix that moved fails a case instead of
    silently backfilling nothing.
    """
    slug = f"acme-{external_id}"
    with engine.begin() as connection:
        company_id = connection.execute(
            text(
                "INSERT INTO company (name, slug, source, is_active, created_at)"
                " VALUES (:name, :slug, 'greenhouse', TRUE, '2026-01-01 00:00:00')"
                " RETURNING id"
            ),
            {"name": f"Acme {external_id}", "slug": slug},
        ).scalar_one()
        posting_id = connection.execute(
            text(
                "INSERT INTO posting (company_id, external_id, title, url, first_seen_at,"
                " updated_at) VALUES (:company_id, :external_id, 'Backend Engineer', :url,"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00') RETURNING id"
            ),
            {
                "company_id": company_id,
                "external_id": external_id,
                "url": f"https://example.test/{external_id}",
            },
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO match (posting_id, score, reason, state, created_at)"
                " VALUES (:posting_id, :score, :reason, 'new', '2026-01-01 00:00:00')"
            ),
            {"posting_id": posting_id, "score": score, "reason": reason},
        )


def _stored_match_texts(engine: Engine) -> list[tuple[str, str, int]]:
    """Every match row's reason, state and score, read with SQL for the reason above."""
    with engine.connect() as connection:
        return [
            (row[0], row[1], row[2])
            for row in connection.exec_driver_sql(
                "SELECT reason, state, score FROM match ORDER BY id"
            )
        ]


def _outcome_column_values(engine: Engine) -> list[object]:
    """The `outcome` values, in id order, read with SQL for the reason above."""
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.exec_driver_sql("SELECT outcome FROM match ORDER BY id")
        ]


def _provenance_values(engine: Engine) -> list[tuple[object, object]]:
    """The `model` and `prompt_version` values, in id order, read with SQL for the reason above."""
    with engine.connect() as connection:
        return [
            (row[0], row[1])
            for row in connection.exec_driver_sql(
                "SELECT model, prompt_version FROM match ORDER BY id"
            )
        ]


def _store_a_run_row_before_this_revision(
    engine: Engine,
    *,
    status: str,
    error: str | None,
    postings_seen: int,
    matches_created: int,
) -> None:
    """Insert one run row with only the columns this schema has always had.

    SQL rather than the model, for the reason `_store_a_match_row_before_this_revision`
    gives: the model declares the column this case is about to add, so an INSERT
    naming it would fail on the schema that predates the revision -- which is the
    schema this row is written into.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO run"
                " (status, started_at, finished_at, error, postings_seen, matches_created)"
                " VALUES (:status, '2026-01-01 00:00:00', :finished_at, :error,"
                " :postings_seen, :matches_created)"
            ),
            {
                "status": status,
                "finished_at": None if status == "running" else "2026-01-01 00:00:04",
                "error": error,
                "postings_seen": postings_seen,
                "matches_created": matches_created,
            },
        )


def _run_rows_before_the_column(engine: Engine) -> list[tuple[object, ...]]:
    """The `run` values a database before this revision holds, read with SQL.

    Read with SQL for the reason the resume helper above gives: the model declares
    the column this case adds, and a SELECT naming it would fail on the schema
    that predates it.
    """
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT id, status, started_at, finished_at, error, postings_seen,"
                " matches_created FROM run ORDER BY id"
            )
        ]


def _matches_emailed_values(engine: Engine) -> list[object]:
    """The new column's values, in id order, read with SQL for the reason above."""
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.exec_driver_sql("SELECT matches_emailed FROM run ORDER BY id")
        ]


def _store_company_rows_before_this_revision(engine: Engine, *slugs: str) -> None:
    """Insert company rows with only the columns this schema has always had.

    SQL rather than the model, for the reason `_store_a_match_row_before_this_revision`
    gives: the model declares the two columns this case is about to add, so an
    INSERT naming them would fail on the schema that predates the revision -- which
    is the schema these rows are written into.
    """
    with engine.begin() as connection:
        for slug in slugs:
            connection.execute(
                text(
                    "INSERT INTO company (name, slug, source, is_active, created_at)"
                    " VALUES (:name, :slug, 'greenhouse', TRUE, '2026-01-01 00:00:00')"
                ),
                {"name": slug.capitalize(), "slug": slug},
            )


def _company_rows_before_the_columns(engine: Engine) -> list[tuple[object, ...]]:
    """The `company` values a database before this revision holds, read with SQL.

    Read with SQL for the reason the resume helper above gives: the model declares
    the columns this case adds, and a SELECT naming them would fail on the schema
    that predates them.
    """
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT id, name, slug, source, is_active, created_at"
                " FROM company ORDER BY id"
            )
        ]


def _validator_values(engine: Engine) -> list[tuple[object, object]]:
    """The two new columns' values, in id order, read with SQL for the reason above."""
    with engine.connect() as connection:
        return [
            (row[0], row[1])
            for row in connection.exec_driver_sql(
                "SELECT etag, last_modified FROM company ORDER BY id"
            )
        ]


def _synthetic_resume(**overrides: object) -> Resume:
    """A resume row whose text is synthetic: the repository holds no real one."""
    fields: dict[str, object] = {
        "filename": "synthetic-resume.txt",
        "content_type": "text/plain",
        "text": SYNTHETIC_RESUME_TEXT,
        "stored_path": "/tmp/synthetic-resume.txt",
    }
    fields.update(overrides)
    return Resume(**fields)


def _company_with_posting(session: Session, slug: str = "acme") -> tuple[Company, Posting]:
    """One company with one posting under it, flushed so both carry an id."""
    company = Company(name=slug.capitalize(), slug=slug)
    session.add(company)
    session.flush()
    posting = Posting(
        company_id=company.id,
        external_id="greenhouse-1",
        title="Backend Engineer",
        url="https://example.test/1",
    )
    session.add(posting)
    session.flush()
    return company, posting


# The migrated schema against the models: the check that makes "a migration
# exists" insufficient.


def test_the_migrated_schema_holds_the_five_tables_and_alembics_own(database: Engine) -> None:
    _migrate(database)

    assert set(inspect(database).get_table_names()) == set(THE_FIVE_TABLES) | {
        ALEMBIC_VERSION_TABLE
    }
    assert _applied_revisions(database) == [HEAD_REVISION]


def test_the_migrated_schema_produces_an_empty_diff_against_the_models(database: Engine) -> None:
    _migrate(database)

    assert _diff(database, Base.metadata) == []


def test_every_migrated_column_matches_the_model_by_name_type_and_nullability(
    database: Engine,
) -> None:
    _migrate(database)
    dialect = _dialect_of(database)

    assert {name: _reflected_columns(database, name) for name in THE_FIVE_TABLES} == {
        name: _model_columns(Base.metadata.tables[name], dialect) for name in THE_FIVE_TABLES
    }


def test_the_four_named_checks_survive_the_migration(database: Engine) -> None:
    _migrate(database)

    assert {
        table_name: {check["name"] for check in inspect(database).get_check_constraints(table_name)}
        for table_name in ("match", "run")
    } == {
        "match": {"ck_match_score_range", "ck_match_state_values", "ck_match_outcome_values"},
        "run": {"ck_run_status_values"},
    }

    # And they ride on the DDL the migrated schema renders, not only on the
    # inspector's list: the same names the models render, for both dialects. The
    # names are compared sorted, because a batch ALTER rebuilds the table on
    # SQLite and the DDL builder places the added check where it likes: which
    # checks are there is the fact, their order is not one either engine keeps.
    reflected = _reflected_tables(database)
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        assert {
            table_name: sorted(_check_names(reflected.tables[table_name], dialect))
            for table_name in ("match", "run")
        } == {
            table_name: sorted(_check_names(Base.metadata.tables[table_name], dialect))
            for table_name in ("match", "run")
        }


def test_the_partial_unique_index_rides_the_migrated_schema_with_its_predicate(
    database: Engine,
) -> None:
    _migrate(database)
    (reflected,) = _reflected_tables(database).tables["resume"].indexes

    assert reflected.name == "uq_resume_one_active"
    # SQLite reflects this flag as `1` and PostgreSQL as `True`: truthiness is the
    # fact they share.
    assert reflected.unique
    assert [column.name for column in reflected.columns] == ["is_active"]
    # The predicate the engine itself reports, compiled the way test_models.py
    # compiles the declared index: SQLite reflects it as `sqlite_where` and
    # PostgreSQL as `postgresql_where`, so this is the predicate this engine
    # enforces. The other engine's is what the models declare, rendered here.
    assert (
        str(CreateIndex(reflected).compile(dialect=_dialect_of(database))).strip()
        == PARTIAL_INDEX_DDL
    )
    (declared,) = Resume.__table__.indexes
    assert {
        dialect_name: str(CreateIndex(declared).compile(dialect=_dialect(dialect_name))).strip()
        for dialect_name in ("sqlite", "postgres")
    } == {"sqlite": PARTIAL_INDEX_DDL, "postgres": PARTIAL_INDEX_DDL}


def test_both_foreign_keys_cascade_on_the_migrated_schema(database: Engine) -> None:
    _migrate(database)

    assert {
        (table_name, key["constrained_columns"][0]): key["options"].get("ondelete")
        for table_name in THE_FIVE_TABLES
        for key in inspect(database).get_foreign_keys(table_name)
    } == {
        ("posting", "company_id"): "CASCADE",
        ("match", "posting_id"): "CASCADE",
    }


def test_the_migrated_schema_renders_the_five_tables_for_both_dialects(database: Engine) -> None:
    """The DDL-compile pattern of `test_models.py`, pointed at the migrated schema."""
    _migrate(database)
    reflected = _reflected_tables(database)

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        ddl = "\n".join(
            str(CreateTable(table).compile(dialect=dialect)) for table in reflected.sorted_tables
        )

        assert all(re.search(rf'CREATE TABLE "?{name}"? \(', ddl) for name in THE_FIVE_TABLES)
        assert ddl.count("ON DELETE CASCADE") == 2


# The rules of #23 on the migrated schema, asked the way test_engine_parity.py
# asks them of init_db()'s: through the app's own session helper, so what the
# assertions read back is the engine's own refusal and not a Python check.


def test_the_migrated_schema_refuses_a_duplicate_company_slug(database: Engine) -> None:
    _migrate(database)

    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Company(name="Acme Again", slug="acme"))

    _assert_the_engine_named_what_it_refused(refusal.value, "company.slug", "company_slug_key")


def test_the_migrated_schema_holds_the_source_column_and_its_check(database: Engine) -> None:
    """Autogenerate does not detect a check constraint, so the revision carries it by hand."""
    _migrate(database)

    assert _reflected_columns(database, "company")["source"] == ("VARCHAR(20)", False)
    assert {
        check["name"] for check in inspect(database).get_check_constraints("company")
    } == {"ck_company_source_values"}


def test_the_migrated_schema_refuses_a_source_outside_the_vocabulary(database: Engine) -> None:
    _migrate(database)

    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Company(name="Acme Lever", slug="acme-lever", source="workday"))

    _assert_the_engine_named_what_it_refused(refusal.value, "ck_company_source_values")

    with db.session_scope() as session:
        assert [company.source for company in session.scalars(select(Company))] == ["greenhouse"]


def test_the_migrated_schema_refuses_a_second_active_resume(database: Engine) -> None:
    _migrate(database)

    with db.session_scope() as session:
        session.add(_synthetic_resume())

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(_synthetic_resume(filename="synthetic-resume-2.txt"))

    _assert_the_engine_named_what_it_refused(
        refusal.value, "resume.is_active", "uq_resume_one_active"
    )


def test_the_migrated_schema_refuses_a_score_outside_zero_to_one_hundred(database: Engine) -> None:
    _migrate(database)

    with db.session_scope() as session:
        _, posting = _company_with_posting(session)
        posting_id = posting.id

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Match(posting_id=posting_id, score=101, reason="Fits"))

    _assert_the_engine_named_what_it_refused(refusal.value, "ck_match_score_range")


def test_the_migrated_schema_refuses_a_state_outside_the_vocabulary(database: Engine) -> None:
    _migrate(database)

    with db.session_scope() as session:
        _, posting = _company_with_posting(session)
        posting_id = posting.id

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Match(posting_id=posting_id, score=50, reason="Fits", state="archived"))

    _assert_the_engine_named_what_it_refused(refusal.value, "ck_match_state_values")


def test_the_migrated_schema_refuses_a_posting_for_a_company_that_is_not_there(
    database: Engine,
) -> None:
    _migrate(database)

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(
                Posting(
                    company_id=999_999,
                    external_id="greenhouse-1",
                    title="Backend Engineer",
                    url="https://example.test/1",
                )
            )

    _assert_the_engine_named_what_it_refused(
        refusal.value, "FOREIGN KEY constraint failed", "violates foreign key constraint"
    )


# A database init_db() already built: adopted, never rebuilt.


def test_a_database_init_db_built_and_filled_is_adopted_by_upgrade_head(database: Engine) -> None:
    """The fixture's `init_db()` schema, with rows in it, taken to `head` as it stands."""
    with db.session_scope() as session:
        _, posting = _company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=87, reason="Python and SQL fit"))

    before = _written_rows(database)
    upgrade_config = _config()
    statements = _ddl_statements_sent_by(
        database, lambda: command.upgrade(upgrade_config, "head")
    )

    # None of the five tables is created or dropped: only alembic's own
    # `alembic_version`, which is how `current` below can report `head` at all.
    assert [line for line in statements if any(name in line for name in THE_FIVE_TABLES)] == []
    assert _written_rows(database) == before

    current_config = _config()
    command.current(current_config)
    assert current_config.stdout.getvalue().strip() == f"{HEAD_REVISION} (head)"

    second = _ddl_statements_sent_by(database, lambda: command.upgrade(upgrade_config, "head"))

    assert second == []
    assert _written_rows(database) == before


def test_the_new_column_is_added_to_a_database_that_predates_it_with_no_backfill(
    database: Engine,
) -> None:
    """#26's column: a database at the baseline gains it, and its rows are as they were.

    The fixture's database is rebuilt at the baseline only -- the schema that
    existed before this revision -- and two synthetic resume rows go in, one
    active and one older. `upgrade head` adds the column: the text, the media
    type, the stored path and the active flag of both rows are what they were,
    and the new column is NULL on both, which is the state that makes the next
    parse of a row happen rather than a value somebody wrote.
    """
    Base.metadata.drop_all(database)
    command.upgrade(_config(), BASELINE_REVISION)
    # The rows go in with SQL, the way the app stored them before this revision:
    # the model now declares the column the schema here does not have yet.
    _store_a_resume_row_at_the_baseline(database, "synthetic-resume.txt", active=True)
    _store_a_resume_row_at_the_baseline(database, "synthetic-resume-2.txt", active=False)
    before = _resume_rows_before_the_column(database)
    assert "parsed_fields" not in _reflected_columns(database, "resume")

    command.upgrade(_config(), "head")

    assert _reflected_columns(database, "resume")["parsed_fields"] == ("TEXT", True)
    assert _resume_rows_before_the_column(database) == before
    assert [bool(row[5]) for row in before] == [True, False]
    assert [row[3] for row in before] == [SYNTHETIC_RESUME_TEXT] * 2
    assert _parsed_fields_values(database) == [None, None]


def test_the_outcome_column_is_backfilled_from_the_reasons_a_database_already_holds(
    database: Engine,
) -> None:
    """#30's column: three rows that predate it, each marked by the reason it carries.

    The fixture's database is rebuilt at #28 -- the schema before this revision --
    and one row of each kind goes in: a rejection, a not-scored result and a
    genuine score, all three `score = 0` or not, with the reasons written from
    `prefilter.REJECTION_REASON_PREFIX` and `scorer.NOT_SCORED_PREFIX` themselves.
    `upgrade head` adds the column, its check and the backfill: one value per row,
    and not one character of any reason rewritten.
    """
    Base.metadata.drop_all(database)
    command.upgrade(_config(), SOURCE_REVISION)
    reasons = (
        REJECTION_REASON_PREFIX + "title term 'intern'",
        NOT_SCORED_PREFIX + "the model call failed",
        "Synthetic fit: Python and SQL.",
    )
    for number, reason in enumerate(reasons, start=1):
        _store_a_match_row_before_this_revision(
            database, external_id=str(number), score=0 if number < len(reasons) else 87, reason=reason
        )
    before = _stored_match_texts(database)
    assert "outcome" not in _reflected_columns(database, "match")

    command.upgrade(_config(), "head")

    assert _reflected_columns(database, "match")["outcome"] == ("VARCHAR(20)", False)
    assert _outcome_column_values(database) == ["rejected", "not_scored", "scored"]
    assert _stored_match_texts(database) == before
    assert [reason for reason, _, _ in before] == list(reasons)


def test_the_provenance_columns_are_added_null_with_no_backfill(database: Engine) -> None:
    """#32's columns: a database at #30 with three rows gains them, all three NULL.

    The fixture's database is rebuilt at #30 -- the schema before this revision --
    and one row of each kind goes in: a rejection, a not-scored result and a
    genuine score. `upgrade head` adds the two columns: every row's model and
    prompt version is NULL, which means "unknown" and not "none" -- a row written
    before them had no way to say which model produced it, so inventing one would
    be a lie -- and not one character of any reason nor any score is rewritten.
    """
    Base.metadata.drop_all(database)
    command.upgrade(_config(), OUTCOME_REVISION)
    reasons = (
        REJECTION_REASON_PREFIX + "title term 'intern'",
        NOT_SCORED_PREFIX + "the model call failed",
        "Synthetic fit: Python and SQL.",
    )
    for number, reason in enumerate(reasons, start=1):
        _store_a_match_row_before_this_revision(
            database, external_id=str(number), score=0 if number < len(reasons) else 87, reason=reason
        )
    before = _stored_match_texts(database)
    assert "model" not in _reflected_columns(database, "match")

    command.upgrade(_config(), "head")

    assert _reflected_columns(database, "match")["model"] == ("VARCHAR(100)", True)
    assert _reflected_columns(database, "match")["prompt_version"] == ("VARCHAR(50)", True)
    assert _provenance_values(database) == [(None, None)] * len(reasons)
    assert _stored_match_texts(database) == before


def test_the_run_column_is_added_null_with_no_backfill(database: Engine) -> None:
    """#36's column: a database at #32 with two run rows gains it, both NULL.

    The fixture's database is rebuilt at #32 -- the schema before this revision --
    and two run rows go in: a succeeded run that mailed its matches and a failed
    one. `upgrade head` adds the column: every row reads NULL, which means
    "unknown" and not "none" -- a `NOT NULL` default of `0` would make the run
    that did email claim it emailed nothing -- and no row's status, end, error or
    other count is rewritten. `downgrade()` takes the column away again and
    leaves the rows exactly as they were.
    """
    Base.metadata.drop_all(database)
    command.upgrade(_config(), PROVENANCE_REVISION)
    _store_a_run_row_before_this_revision(
        database, status="succeeded", error=None, postings_seen=12, matches_created=4
    )
    _store_a_run_row_before_this_revision(
        database,
        status="failed",
        error="the board refused: acme",
        postings_seen=1,
        matches_created=0,
    )
    before = _run_rows_before_the_column(database)
    assert "matches_emailed" not in _reflected_columns(database, "run")

    command.upgrade(_config(), "head")

    assert _reflected_columns(database, "run")["matches_emailed"] == ("INTEGER", True)
    assert _matches_emailed_values(database) == [None, None]
    assert _run_rows_before_the_column(database) == before
    assert [row[1] for row in before] == ["succeeded", "failed"]
    assert [row[3] is not None for row in before] == [True, True]  # both ended, read as the engine's own type
    assert [row[4] for row in before] == [None, "the board refused: acme"]
    assert [row[5:] for row in before] == [(12, 4), (1, 0)]

    command.downgrade(_config(), PROVENANCE_REVISION)

    assert "matches_emailed" not in _reflected_columns(database, "run")
    assert _run_rows_before_the_column(database) == before


def test_the_validator_columns_are_added_null_with_no_backfill(database: Engine) -> None:
    """#55's columns: a database at #36 with two company rows gains them, both NULL.

    The fixture's database is rebuilt at #36 -- the schema before this revision --
    and two company rows go in. `upgrade head` adds the two columns: every row
    reads NULL, which means "no validator yet" and not "no ETag" -- a row written
    before them has no validator to send, so its next run asks an unconditional
    question and stores whatever the answer carries -- and no row's name, slug,
    source, active flag or timestamp is rewritten. `downgrade()` takes both columns
    away again and leaves the rows exactly as they were.
    """
    Base.metadata.drop_all(database)
    command.upgrade(_config(), RUN_MATCHES_EMAILED_REVISION)
    _store_company_rows_before_this_revision(database, "acme", "globex")
    before = _company_rows_before_the_columns(database)
    assert len(before) == 2  # the assertions below are about rows that exist
    assert "etag" not in _reflected_columns(database, "company")
    assert "last_modified" not in _reflected_columns(database, "company")

    command.upgrade(_config(), "head")

    assert _reflected_columns(database, "company")["etag"] == ("TEXT", True)
    assert _reflected_columns(database, "company")["last_modified"] == ("TEXT", True)
    assert _validator_values(database) == [(None, None), (None, None)]
    assert _company_rows_before_the_columns(database) == before

    command.downgrade(_config(), RUN_MATCHES_EMAILED_REVISION)

    assert "etag" not in _reflected_columns(database, "company")
    assert "last_modified" not in _reflected_columns(database, "company")
    assert _company_rows_before_the_columns(database) == before


def test_the_four_documented_commands_do_what_they_say(database: Engine, tmp_path: Path) -> None:
    """`upgrade head`, `current`, `revision --autogenerate`, `downgrade base`.

    Every command runs against the fixture's database -- a temporary SQLite file,
    or the server `TEST_DATABASE_URL` names -- and never the local one. The
    autogenerate runs with its revisions pointed at `tmp_path`, so the committed
    directory cannot be written to even if the check that follows failed.
    """
    assert db.database_url() != f"sqlite:///{db.DEFAULT_DATABASE_PATH}"
    Base.metadata.drop_all(database)
    config = _config()

    scratch = tmp_path / "versions"
    scratch.mkdir()
    for name in THE_REVISION_FILES:
        (scratch / name).write_text(
            (REVISIONS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    config.set_main_option("version_locations", str(scratch))

    command.upgrade(config, "head")

    assert set(inspect(database).get_table_names()) == set(THE_FIVE_TABLES) | {
        ALEMBIC_VERSION_TABLE
    }
    assert _applied_revisions(database) == [HEAD_REVISION]

    current_config = _config()
    command.current(current_config)
    assert current_config.stdout.getvalue().strip() == f"{HEAD_REVISION} (head)"

    # Against a database already at `head`: exit 0, and no revision written.
    assert command.revision(config, message="no change here", autogenerate=True) == []
    assert sorted(path.name for path in scratch.glob("*.py")) == sorted(THE_REVISION_FILES)
    assert sorted(path.name for path in REVISIONS_DIR.glob("*.py")) == sorted(THE_REVISION_FILES)

    command.downgrade(config, "base")

    assert inspect(database).get_table_names() == [ALEMBIC_VERSION_TABLE]
    assert _applied_revisions(database) == []

    # `init_db()` is still what creates the tables in an empty database.
    db.init_db()

    assert set(inspect(database).get_table_names()) == set(THE_FIVE_TABLES) | {
        ALEMBIC_VERSION_TABLE
    }
    assert _applied_revisions(database) == []


# Drift between the models and the history, reported by a plain test.


def test_a_column_appended_to_a_copy_of_the_models_is_reported_by_name(database: Engine) -> None:
    """A model change with no revision turns the suite red, and names what is missing."""
    _migrate(database)

    assert _diff(database, Base.metadata) == []

    extended = copy.deepcopy(Base.metadata)
    extended.tables["company"].append_column(Column("scratch_added", Text(), nullable=True))
    diff = _diff(database, extended)

    assert diff != []
    assert "scratch_added" in str(diff)
    # The deep copy is the only thing that changed: the models stay as declared.
    assert "scratch_added" not in Base.metadata.tables["company"].c
