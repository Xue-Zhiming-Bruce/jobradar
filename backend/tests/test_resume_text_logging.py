"""A resume's text must not reach a log record, on any path, by default or at DEBUG.

SQLAlchemy logs a statement's parameters and a result's rows at `DEBUG`, and an
`INSERT` into a `text` column necessarily carries the value among its parameters
-- so the app cannot stop a *write* from holding the text, only stop it from
being printed. Two things do that here, and this module tests each where it can
be observed:

1. `app/db.py` holds the ORM's own loggers at `WARNING` while the engine is
   built (the floor), so the default configuration never prints a row or a
   parameter. The first case proves the upload -- the path that cannot avoid
   carrying the text -- emits nothing at the default level.
2. The reads that do not need the text do not select it (`resume_rows`,
   `resume_row`, the activation lookup, the delete lookup all use `load_only`),
   so even a deployment that puts the loggers back to DEBUG sees no `text`.
   The second case raises them itself, after the upload, and drives every read.

The uploads are synthetic and their text is a sentinel; the repository is public,
and no case writes a real resume anywhere.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import resume

TEXT_PLAIN = "text/plain"

# The text every case uploads: a distinctive line whose appearance in a log
# record is the failure, and which is never the reason a case passes.
SENTINEL = "SENTINEL-RESUME-LINE: this text must stay in the database."

# The ORM's own loggers, the ones that emit a statement's parameters and a
# result's rows at DEBUG. `sqlalchemy.engine` is the parent, and the result
# loggers hang under it.
ENGINE_LOGGERS = (
    "sqlalchemy.engine",
    "sqlalchemy.engine.Engine",
    "sqlalchemy.engine.result",
)

# A line the test logs itself, so a case can show its capture was live rather
# than silent: an empty stream is no evidence that nothing was logged.
CONTROL = "SENTINEL-CONTROL-LINE: the capture is live."


@contextmanager
def captured(names: tuple[str, ...]) -> Iterator[list[str]]:
    """Every record the named loggers (and their children) emit, as one line each.

    The line carries the record's message, its arguments and its exception, so a
    case looks in all three for the text it must not find.
    """
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(f"{record.getMessage()} {record.args!r} {record.exc_info!r}")

    handler = Collect()
    for name in names:
        logging.getLogger(name).addHandler(handler)
    try:
        yield lines
    finally:
        for name in names:
            logging.getLogger(name).removeHandler(handler)


@contextmanager
def orm_at_debug() -> Iterator[list[str]]:
    """The ORM's own loggers put back to DEBUG, with everything they emit collected."""
    loggers = [logging.getLogger(name) for name in ENGINE_LOGGERS]
    previous = [logger.level for logger in loggers]
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(f"{record.getMessage()} {record.args!r} {record.exc_info!r}")

    handler = Collect()
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    try:
        yield lines
    finally:
        for logger, level in zip(loggers, previous):
            logger.removeHandler(handler)
            logger.setLevel(level)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with a temporary database and a temporary uploads directory."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    with TestClient(app) as test_client:
        yield test_client


def upload(client: TestClient, filename: str = "resume.txt", text: str = SENTINEL) -> int:
    """One upload of synthetic text, and the id its `201` answered with."""
    response = client.post("/resume", files={"file": (filename, text.encode(), TEXT_PLAIN)})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def leaked(lines: list[str]) -> list[str]:
    """The captured lines that hold the sentinel: the failure this module exists for."""
    return [line for line in lines if SENTINEL in line]


def test_the_app_holds_the_orm_loggers_at_warning() -> None:
    """The floor `app/db.py` sets while the engine is built: the default prints no rows."""
    assert logging.getLogger("sqlalchemy.engine").level >= logging.WARNING
    assert logging.getLogger("sqlalchemy.engine.Engine").getEffectiveLevel() >= logging.WARNING


def test_the_upload_prints_no_text_at_the_default_level(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The write path cannot avoid holding the text, so the floor is what keeps it out of a log.

    The capture is proven live by a line this test logs itself: without that, an
    engine that simply never logs would pass this case for the wrong reason.
    """
    caplog.set_level(logging.DEBUG)

    with captured(("",)) as lines:
        logging.getLogger("tests.resume_text_logging").debug(CONTROL)
        upload(client)

    assert any(CONTROL in line for line in lines), "the capture must be live"
    assert leaked(lines) == []


def test_the_read_paths_select_no_text_even_at_debug(client: TestClient) -> None:
    """Every read the app offers, with the ORM's loggers put back to DEBUG.

    The upload happens first, at the default level, so the only statements this
    case sees are the ones the reads issue -- and those must not name the text,
    because `load_only` leaves it out of the SELECT.
    """
    resume_id = upload(client)

    with orm_at_debug() as lines:
        assert client.get("/resume").status_code == 200
        assert client.patch(f"/resume/{resume_id}", json={"is_active": True}).status_code == 200
        assert client.get(f"/resume/{resume_id}/file").content == SENTINEL.encode()

    assert lines, "the ORM must have logged something, or the case proves nothing"
    assert leaked(lines) == []
    # The projection, not just the level: the list's own SELECT does not name the text.
    assert not any("resume.text" in line for line in lines if "SELECT" in line)


def test_a_delete_at_debug_leaves_no_text_behind(client: TestClient) -> None:
    """The delete path reads a row too, and its statement must not carry the text either."""
    resume_id = upload(client)

    with orm_at_debug() as lines:
        assert client.delete(f"/resume/{resume_id}").status_code == 204

    assert lines, "the ORM must have logged something, or the case proves nothing"
    assert leaked(lines) == []


def test_the_reader_that_needs_the_text_still_returns_it(client: TestClient) -> None:
    """The one read that must load the text is unchanged: the parse path depends on it."""
    upload(client)

    assert resume.active_resume_text() == SENTINEL
