"""Where the database lives, and the one function that creates its tables.

`DATABASE_URL` is read here and in no other module. Unset or empty means the
local SQLite file `backend/data/jobradar.db`, resolved from this file's
location, so the app uses one file whether it was started from the repository
root or from `backend/`, with no setup and no environment.

`init_db()` only creates tables that do not exist yet. `create_all` never alters
an existing table, so a change to `app.models` is invisible to a database that
already holds rows -- the local file or the deployed Neon database. That gap is
`#21`'s (schema migrations); nothing here migrates anything.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import Base

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = BACKEND_ROOT / "data" / "jobradar.db"

_engine: Engine | None = None
_engine_url: str | None = None


def database_url() -> str:
    """`DATABASE_URL`, or the local SQLite file when it is unset or empty."""
    return os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{DEFAULT_DATABASE_PATH}"


def get_engine() -> Engine:
    """Build the engine on first use, and rebuild it if `DATABASE_URL` changes.

    Importing this module reads no configuration, creates no file and opens no
    database: the engine appears the first time something asks for one.
    """
    global _engine, _engine_url

    url = database_url()
    if _engine is None or url != _engine_url:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url)
        _engine_url = url
    return _engine


def init_db() -> None:
    """Create the tables that are missing. Existing tables and rows are left alone."""
    Base.metadata.create_all(get_engine())


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
