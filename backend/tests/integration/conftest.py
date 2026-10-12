"""What every case in `backend/tests/integration/` shares.

This is the one suite that drives the real application end to end: the real
routes, the real run row and the daemon thread that carries it, the real
pipeline, prefilter and digest -- over real PostgreSQL. What it shares here is
therefore only plumbing: the server `TEST_DATABASE_URL` names, the app over it,
and the guards that keep a case honest.

Four rules, and this file is the single place each of them lives:

- the suite is separate from the default run by its marker, not by its path.
  `pytest_collection_modifyitems` below marks every item this directory collects
  with `integration`, and `backend/pyproject.toml`'s
  `addopts = "-m 'not integration'"` is what deselects them, so
  `cd backend && uv run pytest` collects this suite and runs none of it. A test
  module here that forgot `pytestmark` still cannot leak into the default run.
- `TEST_DATABASE_URL` is the only variable that chooses the server, and it is
  read here and in `tests/conftest.py` only -- `app/` never mentions it. Unset
  or empty is a failure, never a skip: there is nothing in this suite to skip,
  and a green run with no server would be a lie. The failure names the variable
  and the `docker run` command that provides a throwaway server.
- the engine has to be PostgreSQL. `db.get_engine().dialect.name` is asserted
  before a single table is touched, so a run that quietly fell back to SQLite
  fails instead of passing -- and fails before it could drop anything.
- nothing reaches the network and no key or credential is needed. Every variable
  the run reads -- the model's key, name and cap, the six mail variables, the
  cutoff and the four prefilter terms -- is deleted from the environment, so a
  developer's shell cannot decide a case, and `socket.socket.connect` is patched
  to raise in every case the way `tests/test_pipeline.py` does. The board, the
  model and the mail are faked at their own seams and nowhere else.
"""

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app import db
from app.matching.prefilter import (
    PREFILTER_LOCATION_TERMS,
    PREFILTER_MUST_HAVE_KEYWORDS,
    PREFILTER_SENIORITY_TERMS,
    PREFILTER_TITLE_TERMS,
)
from app.matching.scorer import LLM_API_KEY, LLM_MODEL, SCORER_MAX_POSTINGS
from app.main import app
from app.models import Base
from app.routes.internal import TICK_SECRET_HEADER, TICK_SECRET_VARIABLE
from app.services.digest import (
    DIGEST_FROM,
    DIGEST_MIN_SCORE,
    DIGEST_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
)
from app.services.resume import UPLOADS_DIR_VARIABLE

TEST_DATABASE_URL = "TEST_DATABASE_URL"

POSTGRES = "postgresql"

# The directory this conftest governs: what `pytest_collection_modifyitems` below marks.
INTEGRATION_DIR = Path(__file__).resolve().parent

# The secret this suite's ticks carry: a value that could not appear in a row, a
# response or a log line by accident. It is not a credential and nothing reads it
# outside these tests.
TICK_SECRET = "tick-secret-sentinel-7f4c1d9ab38e"

# The throwaway server, and the one command that starts it. Named here and in
# `README.md`, on port 5433 because 5432 is taken on this machine; the compose
# stack is not a test harness and is not pointed at.
THROWAWAY_POSTGRES = (
    "docker run --rm -d --name jobradar-test-db -p 5433:5432 "
    "-e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway "
    "-e POSTGRES_DB=jobradar postgres:17"
)
THROWAWAY_TEST_DATABASE_URL = (
    "postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar"
)

# Every variable a run reads for itself, deleted in every case: the model's key,
# name and cap, the six mail variables and the cutoff, and the four prefilter
# terms. A case that wants one of them sets it, so what a case drives is what the
# case wrote and never what the developer's shell happened to hold.
UNSET_VARIABLES = (
    LLM_API_KEY,
    LLM_MODEL,
    SCORER_MAX_POSTINGS,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    DIGEST_FROM,
    DIGEST_TO,
    DIGEST_MIN_SCORE,
    PREFILTER_TITLE_TERMS,
    PREFILTER_SENIORITY_TERMS,
    PREFILTER_LOCATION_TERMS,
    PREFILTER_MUST_HAVE_KEYWORDS,
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every item collected from this directory, so the default run deselects it.

    The marker is applied here rather than in each module so it cannot be
    forgotten: `-m 'not integration'` is what keeps this suite out of the default
    run, and an item without the marker would be executed by it. A hook in a
    nested conftest is handed the whole session's items and not only its own
    directory's, hence the filter: an item is marked here if and only if it was
    collected from `backend/tests/integration/`. `README.md` says where the
    marker is declared and where it is applied.
    """
    for item in items:
        if item.path.is_relative_to(INTEGRATION_DIR):
            item.add_marker("integration")


@pytest.fixture(autouse=True)
def no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key, no credential, no cutoff and no prefilter term set."""
    for name in UNSET_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here reaches the network: an unpatched client fails rather than connects.

    The database is not affected: psycopg talks to the server through libpq, not
    through Python's socket module, so this bites on exactly the request this
    suite must never make -- a board or a model call.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def test_database_url() -> str:
    """`TEST_DATABASE_URL`, or a failure naming the variable and how to get a server.

    A failure rather than a skip, in a fixture, so `pytest -m integration` with
    nothing configured reports errors and `0 skipped`: this suite cannot run
    anywhere but against a real PostgreSQL server, and it says so instead of
    looking green.
    """
    url = os.environ.get(TEST_DATABASE_URL, "").strip()
    if not url:
        pytest.fail(
            f"{TEST_DATABASE_URL} is unset or empty: this suite drives the real app "
            f"end to end against a real PostgreSQL server and fails rather than skipping "
            f"without one. Start a throwaway server:\n\n"
            f"    {THROWAWAY_POSTGRES}\n\n"
            f"and run the marked command against it:\n\n"
            f"    cd backend && TEST_DATABASE_URL={THROWAWAY_TEST_DATABASE_URL} "
            f"uv run pytest -m integration"
        )
    return url


@pytest.fixture
def database(test_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """The app's own engine on `TEST_DATABASE_URL`, with empty tables, left as it was found.

    `DATABASE_URL` is pointed at the test server for the duration of the case, so
    the app's own `get_engine()` and `session_scope()` are what every case
    exercises and the app's default SQLite file is untouched. The dialect is
    asserted before anything is written, so a URL that is not PostgreSQL -- and a
    run that quietly fell back to SQLite -- fails here instead of passing.

    The tables are dropped before the case starts, so it begins on empty tables
    rather than inheriting whatever a previous run left, and dropped again
    afterwards: the suite owns these five table names in this database and leaves
    no trace.
    """
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    engine = db.get_engine()  # builds the engine and opens nothing
    assert db.get_engine().dialect.name == POSTGRES, (
        f"{TEST_DATABASE_URL} does not name PostgreSQL: this suite runs against a real "
        f"server, never against SQLite ({db.redacted_database_url()})"
    )

    db.init_db()  # the app's own table creation; a server that does not answer fails here
    Base.metadata.drop_all(engine)  # a previous run's rows must not decide this case
    db.init_db()  # ... and the tables exist again, empty

    yield engine

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def uploads_dir(tmp_path: Path) -> Path:
    """Where this case's uploads land: a temporary directory, never the repository."""
    return tmp_path / "uploads"


@pytest.fixture
def client(
    database: Engine, uploads_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The real app object over `TEST_DATABASE_URL`, driven in process by `TestClient`.

    No `uvicorn`, no bound port, no browser: `TestClient` calls the app the way
    the unit suite does. The uploads directory and the tick's secret are set
    before the app's lifespan runs, because that is where the tables are created.
    """
    monkeypatch.setenv(UPLOADS_DIR_VARIABLE, str(uploads_dir))
    monkeypatch.setenv(TICK_SECRET_VARIABLE, TICK_SECRET)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def tick_headers() -> dict[str, str]:
    """The header a valid tick carries, so no case spells the secret or the header name again."""
    return {TICK_SECRET_HEADER: TICK_SECRET}
