"""The engine matrix: one fixture, so the database tests run on both engines.

Nothing in the suite chooses an engine except this file. The `engine` fixture is
parametrised over SQLite and PostgreSQL, so every test that asks for it runs
twice, and the same test functions cover both engines instead of a copy per
engine. PostgreSQL is used only when `TEST_DATABASE_URL` names a server: unset,
the PostgreSQL parameter is reported as skipped and the reason names the
variable. A URL that is set but unreachable is not a skip -- the fixture fails,
because a dead server must not look like a green run.

`TEST_DATABASE_URL` is the only variable read here, and it is read here only:
`app/` never mentions it. The fixture points `DATABASE_URL` at the chosen test
database for the duration of a test, because the app's own `session_scope()` and
`get_engine()` are what the tests exercise; a stray `DATABASE_URL` therefore
cannot choose an engine or be written to -- whatever it names is untouched.

Each test gets empty tables: the fixture creates them and, for PostgreSQL, drops
them again, so the test database is left as it was found.

Every connection the engine hands out has to be back in the pool when the test
ends, so the fixture asserts the pool's checked-out count after every test that
takes it: a connection whose reference outlives the test -- a module-level or
closure reference, a session a background thread still holds -- fails the suite
instead of leaking quietly into the next test. A connection that is merely
dropped, with no reference left, is returned by the pool before this runs, and
that is the one case the count cannot close.

One autouse fixture here is not about the engine: `waits` replaces the walk's own
`sleep` default with a collector, so no case in the suite waits and every case
that asserts the pacing reads the list it collected.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from app import db
from app.models import Base
from app.services.pipeline import run_pipeline

TEST_DATABASE_URL = "TEST_DATABASE_URL"

SQLITE = "sqlite"
POSTGRES = "postgres"


def configured_test_database_url() -> str | None:
    """`TEST_DATABASE_URL`, or `None` when it is unset or empty."""
    return os.environ.get(TEST_DATABASE_URL, "").strip() or None


@pytest.fixture
def test_database_url() -> str | None:
    """The variable that turns the PostgreSQL cases on, read the way the fixture reads it."""
    return configured_test_database_url()


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The walk's pacing seam, replaced by a collector: no case in this suite waits.

    `run_pipeline`'s `sleep` default is `time.sleep`, and four modules drive the
    walk -- `test_pipeline.py` alone has 42 call sites, and the integration suite
    drives the real provider and cannot pass a keyword at all -- so the default
    itself is replaced here, once, rather than at every call site. The list the
    fixture returns is the waits in the order the walk made them, and `monkeypatch`
    puts the real default back when the test ends. `time.sleep` is left alone: the
    integration suite's own polling uses it.
    """
    waits: list[float] = []
    monkeypatch.setitem(run_pipeline.__kwdefaults__, "sleep", waits.append)
    return waits


@pytest.fixture(params=[SQLITE, POSTGRES])
def engine(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str | None,
) -> Iterator[Engine]:
    """The app's engine, pointed at one engine of the matrix, with the tables created."""
    if request.param == SQLITE:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    else:
        if test_database_url is None:
            pytest.skip(f"{TEST_DATABASE_URL} is not set: no PostgreSQL to run against")
        monkeypatch.setenv("DATABASE_URL", test_database_url)

    db.init_db()
    engine = db.get_engine()
    yield engine

    assert engine.pool.checkedout() == 0, (
        "a connection is still checked out when the test ended: something kept it "
        "referenced -- a module-level or closure reference, or a session a "
        "background thread still holds"
    )

    if request.param == POSTGRES:
        # The suite owns these six table names in the test database: drop what
        # it created and release the connections, leaving no trace behind.
        Base.metadata.drop_all(engine)
        engine.dispose()
