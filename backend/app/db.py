"""Where the database lives, and the one function that creates its tables.

`DATABASE_URL` is read here and in no other module. Unset or empty means the
local SQLite file `backend/data/jobradar.db`, resolved from this file's
location, so the app uses one file whether it was started from the repository
root or from `backend/`, with no setup and no environment.

The URL is accepted in the three spellings this project supports -- `sqlite://`,
the bare `postgresql://` Neon and Render hand out, and the driver-qualified
`postgresql+psycopg://` the suite's engine matrix uses -- and anything else is
refused here, by name, before SQLAlchemy loads a dialect. Nothing rewrites the
URL: SQLAlchemy 2.x resolves a bare `postgresql://` to the psycopg 3 this project
declares, so a Neon connection string is used exactly as Neon gives it.

`redacted_database_url()` is the only rendering of that URL the app produces.
The credential in `DATABASE_URL` must not reach a log line or an error message,
and this repository is public, so every message that names the database names it
in that form.

`init_db()` only creates tables that do not exist yet. `create_all` never alters
an existing table, so a change to `app.models` is invisible to a database that
already holds rows -- the local file or the deployed Neon database. That gap is
`#21`'s (schema migrations); nothing here migrates anything.

Every SQLite connection this module hands out is told to enforce foreign keys
and to wait for a write lock instead of failing at once. Both are per connection
rather than per database, so one engine-level listener sets them whenever a
connection is opened; a handler that ran once, for one connection, would leave
the pool's later connections unguarded. PostgreSQL enforces foreign keys always
and queues writers on its own, so it needs neither pragma.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, make_url
from sqlalchemy.engine import Engine
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from app.models import Base

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = BACKEND_ROOT / "data" / "jobradar.db"

# A resume's text is user data (`AGENTS.md`): never logged. SQLAlchemy logs a
# statement's parameters and a result's rows at DEBUG, and an INSERT into a `text`
# column cannot avoid carrying the value -- so the ORM's own loggers are held at
# WARNING here, where the engine is built. Raising them again is a deliberate
# choice to print user data, and it is the deployment's to make.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# The URL spellings the app accepts: the local default, the bare PostgreSQL form
# Neon and Render hand out, and the driver-qualified form the engine matrix
# uses. The last is not optional -- refusing it would break every PostgreSQL
# test -- and it is spelled out rather than derived, so a lock that moves fails
# in one place instead of at deploy.
ACCEPTED_URL_SCHEMES = ("sqlite", "postgresql", "postgresql+psycopg")

# The schemes the refusal names, and the only place they are spelled out.
SUPPORTED_SCHEMES = "sqlite and postgresql (including postgresql+psycopg)"

# How long a SQLite connection waits, in milliseconds, for a write lock another
# connection holds before giving up with "database is locked". `sqlite3`'s own
# implicit default is 5 000 ms, and this value is deliberately not that: a
# default nobody declared is not a setting, and the test that reads the pragma
# back would prove nothing against it.
SQLITE_BUSY_TIMEOUT_MILLISECONDS = 10_000

_engine: Engine | None = None
_engine_url: str | None = None


class DatabaseUnavailableError(RuntimeError):
    """The configured database could not be reached.

    `init_db()` raises it when the first connection fails, so the app reports
    which database it could not reach -- through `redacted_database_url()`, never
    with the credential. The driver's own error is the `__cause__`, so the detail
    is still there for a debugger without being the text the app prints.
    """


def database_url() -> str:
    """`DATABASE_URL`, or the local SQLite file when it is unset or empty."""
    return os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{DEFAULT_DATABASE_PATH}"


def redacted_database_url() -> str:
    """The configured URL with the credential replaced, for a log line or a message.

    User, host, port, database and query parameters stay legible; the password is
    `***` in whichever spelling it was written, percent-encoded included. This is
    the form any message naming the database uses: `database_url()`'s raw value
    goes to `create_engine` and to nothing that is printed.
    """
    return make_url(database_url()).render_as_string(hide_password=True)


def _url_scheme(url: str) -> str:
    """The scheme a URL string names, or an empty string when it names none.

    A string operation rather than `make_url()`: the gate below runs before
    SQLAlchemy loads a dialect, and a value with no scheme has to be refused here
    instead of in SQLAlchemy's parser.
    """
    scheme, separator, _ = url.partition("://")
    return scheme.strip() if separator else ""


def _reject_unsupported_scheme(url: str) -> None:
    """Refuse a URL this project cannot drive, before a dialect is loaded.

    The message names the scheme and the schemes that are supported, and never
    interpolates the URL itself: a URL carries a password, and this error can
    reach a terminal.
    """
    scheme = _url_scheme(url)
    if scheme in ACCEPTED_URL_SCHEMES:
        return
    named = f"'{scheme}'" if scheme else "no scheme"
    raise ValueError(f"DATABASE_URL uses {named}: this app supports {SUPPORTED_SCHEMES}")


def _configure_sqlite_connection(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    """Set the pragmas one SQLite connection needs, as it is handed out."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
    cursor.close()


def get_engine() -> Engine:
    """Build the engine on first use, and rebuild it if `DATABASE_URL` changes.

    Importing this module reads no configuration, creates no file and opens no
    database: the engine appears the first time something asks for one. The
    scheme is checked before the engine is built, so an unsupported URL fails
    with one clear error rather than inside SQLAlchemy's dialect loading.
    """
    global _engine, _engine_url

    url = database_url()
    if _engine is None or url != _engine_url:
        _reject_unsupported_scheme(url)
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url)
        if engine.dialect.name == "sqlite":
            event.listen(engine, "connect", _configure_sqlite_connection)
        _engine = engine
        _engine_url = url
    return _engine


def init_db() -> None:
    """Create the tables that are missing. Existing tables and rows are left alone.

    A database that cannot be reached becomes a `DatabaseUnavailableError` naming
    the redacted URL; the driver's error stays attached as the cause.
    """
    try:
        Base.metadata.create_all(get_engine())
    except OperationalError as error:
        raise DatabaseUnavailableError(
            f"Could not reach the database at {redacted_database_url()}"
        ) from error


@contextmanager
def session_scope() -> Iterator[Session]:
    """One session per unit of work: commit on success, roll back on error, close always."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
