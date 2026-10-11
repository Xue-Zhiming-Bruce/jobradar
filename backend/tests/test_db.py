"""The local SQLite path, the one create-tables function, and the session helper.

`DATABASE_URL` is never set for the whole test session: each test that wants a
database points the app at one with `monkeypatch`, so the suite proves the
default path, a scratch file, and -- through the shared `engine` fixture in
`tests/conftest.py` -- a PostgreSQL database when `TEST_DATABASE_URL` is set.
The session-helper, worker-thread and pool tests are parametrised over that
fixture, so the same functions run on both engines.

The run does leave one database in the checkout: `tests/test_health.py` starts
the app through `TestClient`, and the lifespan's `init_db()` creates
`backend/data/jobradar.db`. `.gitignore` covers it, which is why `git status
--short` stays empty -- the last tests here check that instead of claiming the
suite leaves nothing behind.

The SQLite-only behaviour sits here rather than in the parametrised matrix, for
the same reason the foreign-key pragma does: SQLite is the engine the pragma and
the write lock belong to. That is the busy timeout, read back from a connection,
and the contention that shows what it buys, each with a control that builds the
same thing without the app's listener -- an engine with no busy handler, and an
engine with no foreign-key pragma -- so the difference is attributable.

What the URL configuration must do is checked here too: the three accepted
spellings, the refusal of every other one by name, and the credential that must
stay out of log records and error text, success path and failure path alike.
"""

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, make_url, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import db
from app.main import app
from app.models import Base, Company, Posting, Resume

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BACKEND_ROOT.parent
APP_ROOT = BACKEND_ROOT / "app"

# A credential whose characters have to be percent-encoded in a URL, so a
# redaction that only replaces the decoded spelling leaves the encoded one
# behind. Synthetic on purpose: nothing here is a real secret.
KNOWN_PASSWORD = "p@ss:w/rd"
KNOWN_PASSWORD_ENCODED = "p%40ss%3Aw%2Frd"

# The form Neon and Render hand out: a bare scheme, no driver name to add, and
# TLS in the query string.
NEON_STYLE_DATABASE_URL = (
    "postgresql://jobradar:synthetic-password@ep-example.jobradar.test:5432/jobradar"
    "?sslmode=require"
)

# Every spelling the app has to refuse, with the scheme the error must name
# (`None` for a value that names no scheme at all).
REFUSED_DATABASE_URLS = [
    ("postgres://jobradar:synthetic-password@127.0.0.1:5432/jobradar", "postgres"),
    ("mysql://jobradar:synthetic-password@127.0.0.1:3306/jobradar", "mysql"),
    (
        "postgresql+psycopg2://jobradar:synthetic-password@127.0.0.1:5432/jobradar",
        "postgresql+psycopg2",
    ),
    (
        "postgresql+asyncpg://jobradar:synthetic-password@127.0.0.1:5432/jobradar",
        "postgresql+asyncpg",
    ),
    ("jobradar.db", None),
    ("/tmp/jobradar.db", None),
]

# How long the holder keeps its write transaction open while the second writer
# waits: long enough that the wait is observed rather than assumed, short enough
# to keep the suite quick.
_CONTENTION_HOLD_SECONDS = 0.4

# The loggers `echo=True` writes to, turned up so the password has somewhere to
# leak to if anything ever put it there.
_SQLALCHEMY_LOGGERS = ("sqlalchemy.engine", "sqlalchemy.pool", "sqlalchemy.dialects")

# Test fixtures are synthetic on purpose: the repository is public and holds no
# resume, real or otherwise.
SYNTHETIC_RESUME_TEXT = (
    "SYNTHETIC RESUME TEXT. Written for this test; not a real person's resume.\n"
    "Skills: Python, SQL."
)

_PRELUDE = "import sys; sys.path.insert(0, {root!r}); "
_RESOLVE_DEFAULT_URL = _PRELUDE + "from app.db import database_url; print(database_url())"
_IMPORT_DB = _PRELUDE + "import app.db; print(app.db._engine)"

# One whole app run in one process: create the tables, optionally write a row, read back.
_APP_RUN = (
    _PRELUDE
    + """
from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import Company

init_db()
with session_scope() as session:
    if {write}:
        session.add(Company(name="Acme", slug="acme"))
    print([company.slug for company in session.scalars(select(Company))])
"""
)


def _run(
    script: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a script in a fresh interpreter, the way `uv run uvicorn` would start one."""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=cwd,
        env={} if env is None else env,
        capture_output=True,
        text=True,
    )


def _point_at_a_scratch_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jobradar.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    return path


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def test_the_environment_wins_when_database_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    assert db.database_url() == "sqlite:///:memory:"


@pytest.mark.parametrize("empty", ["unset", ""])
def test_the_default_is_the_local_file_beside_the_app(
    empty: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if empty == "unset":
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", "")

    assert db.database_url() == f"sqlite:///{db.DEFAULT_DATABASE_PATH}"


def test_the_default_file_is_the_same_from_the_root_and_from_backend() -> None:
    """A clean checkout works with no setup, whichever directory the app starts in."""
    expected = f"sqlite:///{db.DEFAULT_DATABASE_PATH}\n"

    from_root = _run(_RESOLVE_DEFAULT_URL.format(root=str(BACKEND_ROOT)), REPOSITORY_ROOT)
    from_backend = _run(_RESOLVE_DEFAULT_URL.format(root=str(BACKEND_ROOT)), BACKEND_ROOT)

    assert from_root.returncode == 0, from_root.stderr
    assert from_backend.returncode == 0, from_backend.stderr
    assert from_root.stdout == from_backend.stdout == expected


def test_importing_the_database_module_builds_no_engine_and_no_file(tmp_path: Path) -> None:
    """T1 guards `app.main`; `app.db` carries the same rule for the engine itself."""
    completed = _run(_IMPORT_DB.format(root=str(BACKEND_ROOT)), tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "None"
    assert list(tmp_path.iterdir()) == []


def test_database_url_is_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in (BACKEND_ROOT / "app").rglob("*.py")
        if "DATABASE_URL" in path.read_text(encoding="utf-8")
    )

    assert readers == ["db.py"]


# What the configured URL is allowed to be, and what it must never say out loud.


def test_a_bare_postgresql_url_is_taken_exactly_as_neon_hands_it_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted as given: nobody rewrites the scheme to name a driver, and the query survives.

    `create_engine` opens no connection, so this needs no server. It is also a
    check on the locked stack: SQLAlchemy 2.1 resolves a bare `postgresql://` to
    psycopg 3, the driver this project declares, where 2.0 resolved it to
    psycopg2 -- undeclared, and a `ModuleNotFoundError` at deploy. A lock that
    moves fails here instead.
    """
    monkeypatch.setenv("DATABASE_URL", NEON_STYLE_DATABASE_URL)

    assert db.database_url() == NEON_STYLE_DATABASE_URL

    engine = db.get_engine()

    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"
    assert engine.dialect.dbapi is psycopg  # the driver the project declares
    # The query parameters reach the driver's connect call instead of being
    # dropped on the way to it.
    assert engine.url.query == {"sslmode": "require"}
    _, connect_args = engine.dialect.create_connect_args(engine.url)
    assert connect_args["sslmode"] == "require"
    assert connect_args["host"] == "ep-example.jobradar.test"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "postgresql://jobradar:synthetic-password@127.0.0.1:5432/jobradar",
        "postgresql+psycopg://jobradar:synthetic-password@127.0.0.1:5432/jobradar",
    ],
)
def test_the_three_supported_spellings_build_an_engine(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`postgresql+psycopg://` is the spelling the engine matrix uses: refusing it breaks it."""
    monkeypatch.setenv("DATABASE_URL", url)

    engine = db.get_engine()

    assert engine.dialect.name in {"sqlite", "postgresql"}


@pytest.mark.parametrize(("url", "scheme"), REFUSED_DATABASE_URLS)
def test_any_other_scheme_is_refused_with_one_clear_error(
    url: str, scheme: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused where the engine is built, by name, and not inside SQLAlchemy's dialect loading."""
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(ValueError) as refusal:
        db.get_engine()

    message = str(refusal.value)
    assert "sqlite" in message
    assert "postgresql" in message
    if scheme is None:
        assert "no scheme" in message
    else:
        assert scheme in message
    assert "synthetic-password" not in message  # the error never interpolates the URL
    # Not one of SQLAlchemy's driver-loading errors, and not their text either.
    assert "NoSuchModuleError" not in message
    assert "No module named" not in message


def test_the_app_startup_refuses_the_same_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """`init_db()` is the path the app's lifespan takes, and it builds the engine."""
    monkeypatch.setenv(
        "DATABASE_URL", "mysql://jobradar:synthetic-password@127.0.0.1:3306/jobradar"
    )

    with pytest.raises(ValueError) as refusal:
        db.init_db()

    assert "sqlite" in str(refusal.value)
    assert "postgresql" in str(refusal.value)


def test_the_unsupported_scheme_error_names_the_scheme_and_not_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", f"postgres://jobradar:{KNOWN_PASSWORD_ENCODED}@127.0.0.1:5432/jobradar"
    )

    with pytest.raises(ValueError) as refusal:
        db.get_engine()

    message = str(refusal.value)
    assert "postgres" in message
    assert KNOWN_PASSWORD not in message
    assert KNOWN_PASSWORD_ENCODED not in message


def test_the_redacted_rendering_keeps_the_route_and_replaces_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one rendering the app produces for text: user, host, port and database legible."""
    assert quote(KNOWN_PASSWORD, safe="") == KNOWN_PASSWORD_ENCODED  # the value has to be encoded
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql://jobradar:{KNOWN_PASSWORD_ENCODED}@db.example.test:5432/jobradar"
        "?sslmode=require",
    )

    redacted = db.redacted_database_url()

    assert redacted == (
        "postgresql://jobradar:***@db.example.test:5432/jobradar?sslmode=require"
    )
    assert KNOWN_PASSWORD not in redacted
    assert KNOWN_PASSWORD_ENCODED not in redacted


def _interpolates_the_raw_url(line: str) -> bool:
    """Whether a source line puts `database_url()`'s own value into a string."""
    without_the_redacted_call = line.replace("redacted_database_url()", "")
    return "database_url()" in without_the_redacted_call and ("{" in line or ".format(" in line)


def test_the_app_renders_the_url_redacted_and_never_with_its_password() -> None:
    """Criterion (a), kept honest by the source: text goes through the redacted call only.

    `database_url()`'s raw value goes to `create_engine` and to nothing printed,
    and no app module turns the password back on for `render_as_string`.
    """
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(APP_ROOT.rglob("*.py"))
    }

    renderings = [
        name
        for name, text in sources.items()
        for line in text.splitlines()
        if "render_as_string(" in line
    ]
    assert renderings == ["db.py"]  # one rendering, in one place
    assert "hide_password=True" in sources["db.py"]
    assert [name for name, text in sources.items() if "hide_password=False" in text] == []

    assert [
        f"{name}: {line.strip()}"
        for name, text in sources.items()
        for line in text.splitlines()
        if _interpolates_the_raw_url(line)
    ] == []


def _turn_the_sqlalchemy_loggers_up(caplog: pytest.LogCaptureFixture) -> None:
    """The loggers `echo=True` would write to, at DEBUG, so the stream is watched."""
    for name in _SQLALCHEMY_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)


def _spellings(password: str | None) -> list[str]:
    """A credential as it can appear in text: decoded, and percent-encoded."""
    if password is None:
        return []
    return sorted({password, quote(password, safe="")})


def _log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its formatted message and its arguments."""
    return [f"{record.name}: {record.getMessage()} {record.args!r}" for record in records]


def _lines_holding_the_credential(lines: list[str], password: str | None) -> list[str]:
    return [line for line in lines if any(spelling in line for spelling in _spellings(password))]


def test_the_password_reaches_no_log_record_while_the_app_works(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Criterion (b): a real query through the session helper, the credential in the URL.

    On the SQLite parameter the configured URL carries no credential, so the
    capture there proves the stream is watched; on the PostgreSQL parameter --
    when `TEST_DATABASE_URL` is set -- the password in that URL is the one the
    capture has to be free of. The record count is asserted, because an empty
    log stream would otherwise pass this test as evidence.
    """
    password = make_url(db.database_url()).password
    _turn_the_sqlalchemy_loggers_up(caplog)

    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))
    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().slug == "acme"

    assert caplog.records  # the capture saw the engine work, so absence means something
    assert _lines_holding_the_credential(_log_lines(caplog.records), password) == []


def test_a_refused_connection_names_the_route_and_never_the_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Criterion (c): the failure path, where a driver's own text normally carries the URL.

    No server is needed: 127.0.0.1:1 refuses at once. The error still has to name
    the host and port it could not reach, so the absence of the password is not
    the absence of a failure.
    """
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql://jobradar:{KNOWN_PASSWORD_ENCODED}@127.0.0.1:1/jobradar"
    )
    _turn_the_sqlalchemy_loggers_up(caplog)

    with pytest.raises(db.DatabaseUnavailableError) as raised:
        db.init_db()

    wrapper, cause = raised.value, raised.value.__cause__
    assert isinstance(cause, OperationalError)
    # The attempt really reached the driver: the failure names the route.
    assert "127.0.0.1" in str(cause)
    assert "port 1" in str(cause)

    redacted = "postgresql://jobradar:***@127.0.0.1:1/jobradar"
    assert db.redacted_database_url() == redacted
    assert redacted in str(wrapper)  # the app's own message names the redacted database

    everywhere = [str(wrapper), str(cause), repr(cause.orig), *_log_lines(caplog.records)]
    assert _lines_holding_the_credential(everywhere, KNOWN_PASSWORD) == []


def test_init_db_creates_the_five_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)

    db.init_db()

    assert set(inspect(db.get_engine()).get_table_names()) == set(Base.metadata.tables)


def test_init_db_twice_is_a_no_op_that_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    db.init_db()

    assert set(inspect(db.get_engine()).get_table_names()) == set(Base.metadata.tables)
    with db.session_scope() as session:
        assert [company.slug for company in session.scalars(select(Company))] == ["acme"]


def test_two_consecutive_app_runs_share_the_file_and_keep_their_rows(tmp_path: Path) -> None:
    """Restarting against the same file is safe: the second run finds the first run's row."""
    database_file = tmp_path / "jobradar.db"
    env = {"DATABASE_URL": f"sqlite:///{database_file}"}

    first = _run(_APP_RUN.format(root=str(BACKEND_ROOT), write=True), REPOSITORY_ROOT, env)
    second = _run(_APP_RUN.format(root=str(BACKEND_ROOT), write=False), REPOSITORY_ROOT, env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    # The writing run saw only its own row; the writing-free run read it back.
    assert first.stdout.strip() == "['acme']"
    assert second.stdout.strip() == "['acme']"


def test_starting_the_app_creates_the_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_file = _point_at_a_scratch_file(tmp_path, monkeypatch)

    with TestClient(app):
        pass

    assert database_file.is_file()
    assert set(inspect(db.get_engine()).get_table_names()) == set(Base.metadata.tables)


def test_the_session_helper_commits_on_success(engine: Engine) -> None:
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    assert engine.pool.checkedout() == 0
    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().name == "Acme"


def test_the_session_helper_rolls_back_and_closes_on_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with db.session_scope() as session:
            session.add(Company(name="Acme", slug="acme"))
            session.flush()  # the INSERT has reached the database
            raise RuntimeError("boom")

    assert engine.pool.checkedout() == 0  # the connection went back to the pool
    with db.session_scope() as session:
        assert session.scalars(select(Company)).all() == []


def test_the_pool_check_sees_a_session_that_still_holds_a_connection(engine: Engine) -> None:
    """The control for the `engine` fixture's teardown assertion, so it is not a no-op.

    The fixture asserts the checked-out count is 0 after every test that takes
    it, which is what fails the suite when a reference outlives the test. This
    shows the count is live on an engine that really handed a connection out: 1
    while the session that wrote still holds it, 0 once that session is closed.
    """
    session = Session(engine)
    session.add(Company(name="Acme", slug="acme"))
    session.flush()  # the connection is checked out and stays out

    assert engine.pool.checkedout() == 1

    session.close()

    assert engine.pool.checkedout() == 0


# A test that leaks a connection on purpose, so the fixture's teardown check has
# something to catch in a subprocess: keeping the module-level reference is the
# leak, and nothing here repairs it.
_POOL_LEAK_PROBE = '''\
from sqlalchemy.orm import Session

from app.models import Company


def test_a_session_that_outlives_the_test_is_caught(engine) -> None:
    leaked = Session(engine)
    leaked.add(Company(name="Acme", slug="acme"))
    leaked.flush()
    globals()["leaked"] = leaked
'''


def test_the_pool_check_fails_a_test_that_keeps_its_connection() -> None:
    """What the teardown assertion is for: a leak fails the suite, it does not print.

    The probe is a real test file run by a real pytest, in this directory so it
    gets the shared fixture, and it leaks a session that writes and stays
    referenced -- the case the criterion says the count has to catch.
    """
    probe = Path(__file__).with_name("pool_leak_probe.py")
    probe.write_text(_POOL_LEAK_PROBE, encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe),
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=BACKEND_ROOT,
            env={**os.environ, "TEST_DATABASE_URL": ""},
            capture_output=True,
            text=True,
        )
    finally:
        probe.unlink()

    assert completed.returncode != 0, completed.stdout
    assert "still checked out" in completed.stdout + completed.stderr


def test_a_session_writes_from_a_worker_thread(engine: Engine) -> None:
    """FastAPI runs `def` endpoints in a threadpool, so the session helper has to work there.

    The thread writes and commits through `session_scope()`, the main thread
    reads the row back, and the thread checks that it really is not the main
    one. Nothing here builds an engine or passes `connect_args`: the app's own
    engine is what has to work off the event loop, and SQLAlchemy's SQLite
    dialect is what allows it.
    """
    observed: dict[str, object] = {}
    failures: list[BaseException] = []

    def write_from_a_worker_thread() -> None:
        try:
            observed["off_the_main_thread"] = (
                threading.current_thread() is not threading.main_thread()
            )
            with db.session_scope() as session:
                session.add(Company(name="Acme", slug="acme"))
        except BaseException as error:  # reported on the main thread, not left in the log
            failures.append(error)

    worker = threading.Thread(target=write_from_a_worker_thread)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive()
    assert failures == []
    assert observed == {"off_the_main_thread": True}
    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().slug == "acme"


def _foreign_keys_pragma(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()


def test_every_sqlite_connection_the_app_opens_enforces_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite ignores foreign keys unless each connection asks it to.

    A connection taken from the pool and one opened after the pool was emptied
    both carry the pragma, which is what "per connection" has to mean: the
    listener runs whenever a connection is opened, not once at import.
    """
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    engine = db.get_engine()

    assert _foreign_keys_pragma(engine) == 1
    engine.dispose()  # close the pooled connection, so the next one is new
    assert _foreign_keys_pragma(engine) == 1


def test_without_the_pragma_the_same_schema_takes_a_posting_with_no_company(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the pragma is the whole difference between the two engines.

    Same file, same schema, same row: an engine built without the listener
    accepts a posting whose company does not exist, and the app's engine refuses
    it. This is the SQLite half of #23's foreign-key rule, so it is here rather
    than in the parametrised matrix.
    """
    database_file = _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()

    pragma_free = create_engine(f"sqlite:///{database_file}")
    with Session(pragma_free) as session:
        session.add(
            Posting(
                company_id=999_999,
                external_id="greenhouse-1",
                title="Backend Engineer",
                url="https://example.test/1",
            )
        )
        session.commit()
    with Session(pragma_free) as session:
        assert session.scalars(select(Posting)).one().company_id == 999_999
    pragma_free.dispose()

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        with db.session_scope() as session:
            session.add(
                Posting(
                    company_id=999_998,
                    external_id="greenhouse-2",
                    title="Backend Engineer",
                    url="https://example.test/2",
                )
            )


def _busy_timeout_milliseconds(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()


def test_every_sqlite_connection_waits_for_a_write_lock_instead_of_failing_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PRAGMA busy_timeout`, per connection, exactly like the foreign-key pragma.

    A connection taken from the pool and one opened after the pool was emptied
    both carry the value `db.py` declares. That value is deliberately not
    `sqlite3`'s implicit 5 000 ms: the default is in force with no code at all,
    so a read-back equal to it would be evidence of nothing.
    """
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    engine = db.get_engine()

    with sqlite3.connect(":memory:") as untouched:
        implicit_default = untouched.execute("PRAGMA busy_timeout").fetchone()[0]
    assert implicit_default == 5_000
    assert db.SQLITE_BUSY_TIMEOUT_MILLISECONDS != implicit_default

    assert _busy_timeout_milliseconds(engine) == db.SQLITE_BUSY_TIMEOUT_MILLISECONDS
    engine.dispose()  # close the pooled connection, so the next one is new
    assert _busy_timeout_milliseconds(engine) == db.SQLITE_BUSY_TIMEOUT_MILLISECONDS


def test_a_second_writer_waits_for_the_write_lock_and_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write contention degrades to a wait, not to `database is locked`.

    The holder opens a write transaction on its own connection and does not
    commit. The second writer starts its write on another thread and cannot
    finish while the lock is held -- asserted, so a run that never contended
    cannot pass -- and completes once the holder commits.
    """
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    engine = db.get_engine()

    holder_is_writing = threading.Event()
    second_write_begun = threading.Event()
    second_write_finished = threading.Event()
    failures: list[BaseException] = []

    def write_from_the_second_thread() -> None:
        holder_is_writing.wait(timeout=10)
        try:
            with db.session_scope() as session:
                session.add(Company(name="Second", slug="second"))
                second_write_begun.set()
        except BaseException as error:
            failures.append(error)
        finally:
            second_write_finished.set()

    second = threading.Thread(target=write_from_the_second_thread)
    second.start()

    holder = Session(engine)
    holder.add(Company(name="Acme", slug="acme"))
    holder.flush()  # the INSERT holds the write lock until the commit below
    holder_is_writing.set()

    began = second_write_begun.wait(timeout=10)
    # The moment the lock is released, nothing is waiting any more: if the
    # second write has finished by then, the two writers never contended.
    waited = began and not second_write_finished.wait(timeout=_CONTENTION_HOLD_SECONDS)

    holder.commit()
    holder.close()
    second.join(timeout=10)

    assert began, "the second writer never began its write"
    assert waited, "the second write finished while the holder's transaction was open"
    assert failures == []
    assert not second.is_alive()
    with db.session_scope() as session:
        assert sorted(company.slug for company in session.scalars(select(Company))) == [
            "acme",
            "second",
        ]


def test_without_a_busy_timeout_the_same_two_writers_fail_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the wait above: the busy timeout is what buys it.

    Same file, same schema, same two writers; the second one is on an engine
    whose connections get no busy handler (`?timeout=0`), and it fails the
    moment the holder's lock is in its way -- at once, not after the timeout
    the app declares. The holder never commits, so the failure happened while
    the lock was held, and its row is rolled back rather than stored.
    """
    database_file = _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    engine = db.get_engine()

    # A second engine on the same file: the app's engine is what sets the
    # timeout, and this one deliberately does not attach the listener.
    impatient = create_engine(f"sqlite:///{database_file}?timeout=0")
    assert impatient.dialect.create_connect_args(impatient.url)[1]["timeout"] == 0

    holder = Session(engine)
    try:
        holder.add(Company(name="Acme", slug="acme"))
        holder.flush()  # the write lock is held, not committed

        started = time.monotonic()
        with pytest.raises(OperationalError, match="database is locked"):
            with Session(impatient) as second:
                second.add(Company(name="Second", slug="second"))
                second.flush()

        assert time.monotonic() - started < 1
    finally:
        holder.rollback()
        holder.close()
        impatient.dispose()

    with db.session_scope() as session:
        assert session.scalars(select(Company)).all() == []


def test_resume_text_lives_in_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()

    with db.session_scope() as session:
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text=SYNTHETIC_RESUME_TEXT,
                stored_path=str(tmp_path / "synthetic-resume.txt"),
            )
        )

    with db.session_scope() as session:
        resume = session.scalars(select(Resume)).one()
        assert resume.text == SYNTHETIC_RESUME_TEXT
        assert resume.is_active is True
        assert resume.uploaded_at is not None


def test_the_default_database_file_is_ignored_by_git() -> None:
    relative = db.DEFAULT_DATABASE_PATH.relative_to(REPOSITORY_ROOT).as_posix()

    ignored = _git("check-ignore", relative)

    assert ignored == relative


def test_the_run_creates_the_local_database_and_git_ignores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suite's own app start writes `backend/data/jobradar.db`, and git hides it."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with TestClient(app):
        pass

    relative = db.DEFAULT_DATABASE_PATH.relative_to(REPOSITORY_ROOT).as_posix()
    assert db.DEFAULT_DATABASE_PATH.is_file()
    assert _git("check-ignore", relative) == relative


def test_no_database_file_is_committed_or_left_for_git_to_report() -> None:
    assert _git("ls-files", "*.db", "*.sqlite3", "data/*") == ""

    reported = _git("status", "--short", "--untracked-files=all").splitlines()
    assert [line for line in reported if line.endswith((".db", ".sqlite3"))] == []
