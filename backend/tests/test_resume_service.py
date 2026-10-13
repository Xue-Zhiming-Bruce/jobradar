"""Where the uploads directory comes from, how a file lands in it, and how its text is read back.

The two settings this task adds are read in one place each, the way
`DATABASE_URL` is read in `app/db.py` only, and the default has to keep what
arrives at runtime -- a resume -- out of a public repository. The suite therefore
redirects `UPLOADS_DIR` at a temporary directory; nothing here writes to the
default one, it only checks where the default points.

The reader is the other half: `active_resume_text()` is the one place the stored
text comes back out, and its cases take the shared `engine` fixture so they run
on SQLite and on PostgreSQL.

The history is the reader's other half: the rows behind the routes are read,
activated and deleted through this module, so the cases that assert the state
they leave -- exactly one active row, which one it is, and what a delete does to
the row and to the file -- take the same `engine` fixture and run on both
engines. No case here goes through a route.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import db
from app.models import Resume
from app.services import resume

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BACKEND_ROOT.parent
APP_ROOT = BACKEND_ROOT / "app"

_EXPECTED_EXTENSIONS = {
    resume.TEXT_PLAIN: ".txt",
    resume.APPLICATION_PDF: ".pdf",
    resume.DOCX: ".docx",
    # The two scan images #27 reads: the stored file keeps the format's own
    # extension the way every other upload does.
    resume.IMAGE_PNG: ".png",
    resume.IMAGE_JPEG: ".jpg",
}


def test_uploads_dir_is_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in APP_ROOT.rglob("*.py")
        if resume.UPLOADS_DIR_VARIABLE in path.read_text(encoding="utf-8")
    )

    assert readers == ["resume.py"]


def test_max_upload_bytes_is_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in APP_ROOT.rglob("*.py")
        if resume.MAX_UPLOAD_BYTES_VARIABLE in path.read_text(encoding="utf-8")
    )

    assert readers == ["resume.py"]


def test_the_configured_uploads_directory_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOADS_DIR", "/tmp/jobradar-synthetic-uploads")

    assert resume.uploads_dir() == Path("/tmp/jobradar-synthetic-uploads")


@pytest.mark.parametrize("empty", ["unset", ""])
def test_the_default_uploads_directory_is_outside_the_repository(
    empty: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset means a directory no `git add` in this checkout can reach."""
    if empty == "unset":
        monkeypatch.delenv("UPLOADS_DIR", raising=False)
    else:
        monkeypatch.setenv("UPLOADS_DIR", "")

    directory = resume.uploads_dir()

    assert directory == resume.DEFAULT_UPLOADS_DIR
    assert not directory.is_relative_to(REPOSITORY_ROOT)
    assert not directory.is_relative_to(BACKEND_ROOT)
    assert directory.is_relative_to(Path.home())


@pytest.mark.parametrize("empty", ["unset", ""])
def test_the_default_upload_limit_applies(empty: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if empty == "unset":
        monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    else:
        monkeypatch.setenv("MAX_UPLOAD_BYTES", "")

    assert resume.max_upload_bytes() == resume.DEFAULT_MAX_UPLOAD_BYTES


def test_the_configured_upload_limit_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2048")

    assert resume.max_upload_bytes() == 2048


def test_reading_the_configuration_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    assert resume.uploads_dir() == tmp_path / "uploads"
    assert resume.max_upload_bytes() > 0
    assert not (tmp_path / "uploads").exists()


def test_saving_an_upload_creates_the_directory_and_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory appears on the first upload, and the name is the server's own."""
    directory = tmp_path / "uploads"
    monkeypatch.setenv("UPLOADS_DIR", str(directory))
    assert not directory.exists()

    path = resume.save_upload(b"synthetic resume bytes", resume.TEXT_PLAIN)

    assert directory.is_dir()
    assert path.parent == directory
    assert path.suffix == ".txt"
    assert path.read_bytes() == b"synthetic resume bytes"


@pytest.mark.parametrize("content_type", sorted(_EXPECTED_EXTENSIONS))
def test_each_format_is_stored_with_its_own_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    assert resume.save_upload(b"x", content_type).suffix == _EXPECTED_EXTENSIONS[content_type]


# The reader: the one place the stored text comes back out of the database.

# A value that could not appear in a log line by accident: what the log case
# asserts is that it reaches none of them from where it lives. It carries a
# newline and non-ASCII characters, so "verbatim" means something.
RESUME_TEXT_SENTINEL = "QA-RESUME-SENTINEL-TEXT  keep this verbatim: \u00e9\u00e8 \n line2"


def test_no_active_resume_reads_as_none(engine: Engine) -> None:
    """Before the first upload there is nothing to score against, and that is `None`, not `""`."""
    assert resume.active_resume_text() is None


def test_the_active_rows_text_comes_back_verbatim(engine: Engine) -> None:
    """The active row's text, exactly as stored -- not the inactive row's, not truncated."""
    with db.session_scope() as session:
        session.add(
            Resume(
                filename="older.txt",
                content_type=resume.TEXT_PLAIN,
                text="an older, inactive resume",
                stored_path="/tmp/older.txt",
                is_active=False,
            )
        )
    resume.record_active_resume(
        "synthetic-resume.txt",
        resume.TEXT_PLAIN,
        RESUME_TEXT_SENTINEL,
        Path("/tmp/synthetic-resume.txt"),
    )

    assert resume.active_resume_text() == RESUME_TEXT_SENTINEL


def test_a_second_upload_supersedes_the_text_the_reader_returns(engine: Engine) -> None:
    """Re-uploading makes the new text the one the reader answers with."""
    resume.record_active_resume(
        "first.txt", resume.TEXT_PLAIN, "the first resume", Path("/tmp/first.txt")
    )
    resume.record_active_resume(
        "second.txt", resume.TEXT_PLAIN, "the second resume", Path("/tmp/second.txt")
    )

    assert resume.active_resume_text() == "the second resume"


def test_the_reader_logs_none_of_the_text(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """A resume's text is user data, and the reader is a reader: nothing about it is logged."""
    caplog.set_level(logging.DEBUG)
    resume.record_active_resume(
        "synthetic-resume.txt",
        resume.TEXT_PLAIN,
        RESUME_TEXT_SENTINEL,
        Path("/tmp/synthetic-resume.txt"),
    )

    with caplog.at_level(logging.DEBUG):
        # A record of the test's own, so an empty stream cannot pass as evidence.
        logging.getLogger("tests.resume").debug("the capture is live")
        text = resume.active_resume_text()

    assert text == RESUME_TEXT_SENTINEL  # the reader returned it
    assert caplog.records, "the capture must be non-vacuous"
    assert [record.getMessage() for record in caplog.records if RESUME_TEXT_SENTINEL in record.getMessage()] == []
    assert [record for record in caplog.records if RESUME_TEXT_SENTINEL in str(record.args)] == []


def test_the_reader_opens_one_session_per_call(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unit of work, committed and closed before the call returns."""
    calls: list[int] = []
    real_session_scope = db.session_scope

    def counting_session_scope(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real_session_scope(*args, **kwargs)

    monkeypatch.setattr(resume.db, "session_scope", counting_session_scope)

    assert resume.active_resume_text() is None
    assert len(calls) == 1


# The history: the rows the three routes read, activate and delete.

# Two instants a day apart, so "newest first" has something to sort, and the
# tie-break has two rows written in the same one.
EARLY = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
LATE = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)


def _stored(
    filename: str = "older.txt",
    *,
    uploaded_at: datetime | None = None,
    stored_path: Path | None = None,
    text: str = "an older, inactive resume",
) -> int:
    """One inactive `resume` row written through the app's own session, and its id."""
    with db.session_scope() as session:
        row = Resume(
            filename=filename,
            content_type=resume.TEXT_PLAIN,
            text=text,
            stored_path=str(stored_path or Path("/tmp") / filename),
            is_active=False,
        )
        if uploaded_at is not None:
            row.uploaded_at = uploaded_at
        session.add(row)
        session.flush()
        return row.id


def _active_ids() -> list[int]:
    """Every id of an active row, read through the app's own session -- never more than one."""
    with db.session_scope() as session:
        return list(session.scalars(select(Resume.id).where(Resume.is_active.is_(True))))


def test_the_history_is_empty_before_the_first_upload(engine: Engine) -> None:
    assert resume.resume_rows() == []


def test_the_history_reads_newest_first_with_the_tie_broken_by_id(engine: Engine) -> None:
    """The order, and rows a caller can still read: the session they came from is closed."""
    older = _stored("older.txt", uploaded_at=EARLY)
    first_of_the_instant = _stored("first.txt", uploaded_at=LATE)
    second_of_the_instant = _stored("second.txt", uploaded_at=LATE)

    rows = resume.resume_rows()

    assert [row.id for row in rows] == [second_of_the_instant, first_of_the_instant, older]
    assert [row.filename for row in rows] == ["second.txt", "first.txt", "older.txt"]
    assert [row.is_active for row in rows] == [False, False, False]


def test_activating_an_older_row_deactivates_the_one_that_was_active(engine: Engine) -> None:
    """One transaction, so exactly one row is active afterwards and it is the one named."""
    older = _stored("older.txt", uploaded_at=EARLY)
    newest = resume.record_active_resume(
        "newest.txt", resume.TEXT_PLAIN, RESUME_TEXT_SENTINEL, Path("/tmp/newest.txt")
    )
    assert _active_ids() == [newest]

    activated = resume.activate_resume(older)

    assert activated is not None
    assert (activated.id, activated.is_active) == (older, True)
    assert _active_ids() == [older]
    assert len(resume.resume_rows()) == 2  # the superseded row is history, not a casualty


def test_activating_the_row_that_is_already_active_changes_nothing(engine: Engine) -> None:
    """Idempotent: the same row comes back, and still exactly one row is active."""
    active = resume.record_active_resume(
        "resume.txt", resume.TEXT_PLAIN, RESUME_TEXT_SENTINEL, Path("/tmp/resume.txt")
    )

    first = resume.activate_resume(active)
    second = resume.activate_resume(active)

    assert first is not None and second is not None
    assert (first.id, second.id) == (active, active)
    assert second.is_active is True
    assert _active_ids() == [active]


def test_activating_an_id_with_no_row_writes_nothing(engine: Engine) -> None:
    active = resume.record_active_resume(
        "resume.txt", resume.TEXT_PLAIN, RESUME_TEXT_SENTINEL, Path("/tmp/resume.txt")
    )

    assert resume.activate_resume(active + 100) is None

    assert _active_ids() == [active]
    assert [row.id for row in resume.resume_rows()] == [active]


def test_a_delete_removes_the_row_and_the_file_it_names(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    path = resume.save_upload(b"synthetic resume bytes", resume.TEXT_PLAIN)
    stored = resume.record_active_resume(
        "resume.txt", resume.TEXT_PLAIN, RESUME_TEXT_SENTINEL, path
    )
    assert path.is_file()

    assert resume.delete_resume(stored) is True

    assert resume.resume_rows() == []
    assert _active_ids() == []
    assert not path.exists()


def test_a_delete_whose_file_is_already_gone_still_removes_the_row(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is the record and the file is derived from it: a missing file does not block it."""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    path = resume.save_upload(b"synthetic resume bytes", resume.TEXT_PLAIN)
    stored = resume.record_active_resume(
        "resume.txt", resume.TEXT_PLAIN, RESUME_TEXT_SENTINEL, path
    )
    path.unlink()

    assert resume.delete_resume(stored) is True

    assert resume.resume_rows() == []


@pytest.mark.parametrize("spelling", ["sibling", "traversal"], ids=["sibling", "traversal"])
def test_a_delete_never_unlinks_a_path_outside_the_uploads_directory(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """No writer in this app stores such a path, so the delete must not follow one."""
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("UPLOADS_DIR", str(uploads))
    outside = tmp_path / "outside.txt"
    outside.write_text("a file no upload wrote")
    stored = _stored(
        "seeded.txt",
        stored_path=outside if spelling == "sibling" else uploads / ".." / "outside.txt",
    )

    assert resume.delete_resume(stored) is True

    assert resume.resume_rows() == []
    assert outside.read_text() == "a file no upload wrote"


def test_a_delete_whose_unlink_fails_leaves_the_row_and_its_text(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlink runs first, so a failure of its own never leaves a row half-deleted."""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    path = resume.save_upload(b"synthetic resume bytes", resume.TEXT_PLAIN)
    stored = resume.record_active_resume(
        "resume.txt", resume.TEXT_PLAIN, RESUME_TEXT_SENTINEL, path
    )

    with monkeypatch.context() as failing_unlink:

        def refuse(self: Path, missing_ok: bool = False) -> None:
            raise OSError("the stored file could not be unlinked")

        failing_unlink.setattr(Path, "unlink", refuse)
        with pytest.raises(OSError):
            resume.delete_resume(stored)

    # The list no longer carries a resume's text (it is user data, and the list
    # does not need it), so the stored text is read through the one reader that
    # does: the assertion is unchanged, only the handle is.
    assert [(row.id, row.filename) for row in resume.resume_rows()] == [
        (stored, "resume.txt")
    ]
    assert resume.active_resume_text() == RESUME_TEXT_SENTINEL
    assert path.is_file()  # and nothing was removed from disk either

    path.unlink()  # the file is gone by the retry, which the missing-file case above covers
    assert resume.delete_resume(stored) is True
    assert resume.resume_rows() == []
