"""`GET /resume/{id}/file`: the stored bytes, and every way they are not there.

Every test drives the real app through `TestClient` -- no server, no port, no
network -- with the uploads directory pointed at the test's own temporary
directory, and every case that reads a row takes the shared `engine` fixture
through `engine_client`, so it runs on both engines. The uploads are synthetic
and their content is a sentinel: the repository is public and holds no resume,
real or otherwise.

What the route must not do is as much the point as what it says. It answers the
uploaded file byte for byte, under the row's own media type and the row's own
filename, and that filename header is well formed whatever the stored name
holds: a space and a non-ASCII character come back percent-encoded in the RFC
5987 form, and a quote with a CR/LF is carried in it rather than interpolated
into a header of its own. An id with no row, a path value that is not an id, a
file that has gone and a stored path that is not inside the uploads directory
are all refusals that write nothing, and the last of them is refused before
anything is opened. The bytes reach no refusal body and no log record -- the one
log line the route writes names the resume's id and the file's size -- and
`TestClient` re-raises what the app does not handle, so a `404` here is also
proof that a missing file did not become a `500`.
"""

import builtins
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import db
from app.main import app
from app.models import Resume
from app.services.resume import UPLOADS_DIR_VARIABLE

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
# The synthetic PDF the upload suite stores, read here to be downloaded again.
SENTENCE_BEARING_PDF = Path(__file__).resolve().parent / "fixtures" / "synthetic-resume.pdf"

TEXT_PLAIN = "text/plain"
APPLICATION_PDF = "application/pdf"

# The file's own bytes, and the same text: a refusal body or a log record
# holding either fails, and a download that served nothing would be no evidence
# at all.
SENTINEL = b"SENTINEL-RESUME-FILE: these bytes must never reach a log.\n"
SENTINEL_TEXT = SENTINEL.decode()


@pytest.fixture(autouse=True)
def uploads_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The directory every case uploads into: the test's own, never the default."""
    directory = tmp_path / "uploads"
    monkeypatch.setenv(UPLOADS_DIR_VARIABLE, str(directory))
    return directory


@pytest.fixture
def engine_client(engine: Engine) -> Iterator[TestClient]:
    """The app over the engine the shared `engine` fixture chose, so a case runs on both.

    The fixture has already created the tables on that engine and pointed
    `DATABASE_URL` at it -- the variable the app's own `session_scope()` reads --
    and this only drives the real app over them. Its teardown comes first, so the
    `engine` fixture's own pool check sees nothing this client still holds.
    """
    with TestClient(app) as test_client:
        yield test_client


class OpenSpy:
    """Every path the builtin `open` was called with while this was installed.

    `FileResponse` reads the file it serves through `anyio.open_file`, which
    calls the builtin `open`, so a case that installs this and finds the path
    absent has proved the path was not opened. The case below opens a file of
    its own through the spy afterwards, so an absence here cannot be an artifact
    of a spy that never records anything.
    """

    def __init__(self, original: Any) -> None:
        self.original = original
        self.paths: list[str] = []

    def __call__(self, file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | Path):
            self.paths.append(os.fspath(file))
        return self.original(file, *args, **kwargs)


@pytest.fixture
def open_spy(monkeypatch: pytest.MonkeyPatch) -> OpenSpy:
    """The builtin `open`, recorded, for the case that proves a path is never opened."""
    spy = OpenSpy(builtins.open)
    monkeypatch.setattr(builtins, "open", spy)
    return spy


def upload(
    client: TestClient,
    filename: str = "resume.txt",
    data: bytes = SENTINEL,
    content_type: str = TEXT_PLAIN,
) -> dict[str, Any]:
    """One upload through the real route, and the body its `201` answered with."""
    response = client.post("/resume", files={"file": (filename, data, content_type)})
    assert response.status_code == 201, response.text
    return response.json()


def downloaded(client: TestClient, resume_id: int) -> Response:
    """The answer `GET /resume/{id}/file` gives for one id, whatever it is."""
    return client.get(f"/resume/{resume_id}/file")


def row_of(resume_id: int) -> dict[str, Any]:
    """That row's filename, media type and stored path, read through the app's own session."""
    with db.session_scope() as session:
        row = session.get(Resume, resume_id)
        return {
            "filename": row.filename,
            "content_type": row.content_type,
            "stored_path": Path(row.stored_path),
        }


def set_filename(resume_id: int, filename: str) -> None:
    """Store one filename on the row directly, as the upload would have stored it.

    These are the names a header has to survive, and a client cannot send every
    one of them through `POST /resume`: the multipart part escapes or refuses a
    quote and a line break on its way in. What the route has to survive is the
    value the column holds, so it is written here.
    """
    with db.session_scope() as session:
        session.get(Resume, resume_id).filename = filename


def seed_row(filename: str, stored_path: Path) -> int:
    """One row naming a path of this test's choosing, and its id.

    No writer in this app stores a path outside the uploads directory, so the
    case that proves such a path is never opened has to seed the row itself.
    """
    with db.session_scope() as session:
        row = Resume(
            filename=filename,
            content_type=TEXT_PLAIN,
            text="seeded synthetic text",
            stored_path=str(stored_path),
            is_active=False,
        )
        session.add(row)
        session.flush()
        return row.id


def table_state() -> list[tuple[int, str, str, str]]:
    """Every row's id, filename, media type and stored path, in id order."""
    with db.session_scope() as session:
        return [
            (row.id, row.filename, row.content_type, row.stored_path)
            for row in session.scalars(select(Resume).order_by(Resume.id))
        ]


def stored_files(uploads_dir: Path) -> list[Path]:
    """What is in the uploads directory: a refusal has to leave it as it was."""
    return sorted(uploads_dir.iterdir()) if uploads_dir.exists() else []


def refusal(response: Response, status: int) -> str:
    """The message a refusal answered with, having checked it is a JSON body with one."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    detail = response.json()["detail"]
    assert detail, "a refusal must say what it refused"
    return detail if isinstance(detail, str) else str(detail)


def carried_name(header: str) -> str:
    """The filename a `content-disposition` carries, decoded from whichever form it uses."""
    assert header.startswith("attachment; "), header
    quoted = header.partition("filename*=")[2]
    if quoted:
        assert quoted.startswith("utf-8''"), header
        return unquote(quoted.removeprefix("utf-8''"))
    return header.partition('filename="')[2].removesuffix('"')


def log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


def git(*arguments: str) -> str:
    """Run git in the repository the suite lives in."""
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


# The round trip: the file that was uploaded is the file that is served.


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("resume.txt", SENTINEL, TEXT_PLAIN),
        ("resume.pdf", SENTENCE_BEARING_PDF.read_bytes(), APPLICATION_PDF),
    ],
    ids=["txt", "pdf"],
)
def test_the_answer_is_the_uploaded_file_byte_for_byte(
    engine_client: TestClient,
    uploads_dir: Path,
    filename: str,
    data: bytes,
    content_type: str,
) -> None:
    uploaded = upload(engine_client, filename, data, content_type)
    row = row_of(uploaded["id"])

    response = downloaded(engine_client, uploaded["id"])

    assert response.status_code == 200, response.text
    assert response.content == data  # the uploaded file, byte for byte
    assert response.headers["content-type"] == row["content_type"]
    assert carried_name(response.headers["content-disposition"]) == row["filename"]
    assert row["stored_path"].parent == uploads_dir
    assert row["stored_path"].read_bytes() == data  # served from the stored file, unchanged


# The header: the stored name is carried, whatever it holds.


def test_a_name_with_a_space_and_a_non_ascii_character_is_carried_in_the_header(
    engine_client: TestClient,
) -> None:
    uploaded = upload(engine_client)
    set_filename(uploaded["id"], "rèsumè vita.txt")

    response = downloaded(engine_client, uploaded["id"])

    assert response.status_code == 200, response.text
    header = response.headers["content-disposition"]
    # The name cannot go in a plain quoted parameter unchanged, so the framework
    # answers the RFC 5987 form -- `filename*`, percent-encoded UTF-8 -- and the
    # name is read back out of it.
    assert "filename*=utf-8''" in header
    assert carried_name(header) == "rèsumè vita.txt"
    assert response.content == SENTINEL


def test_a_name_with_a_quote_and_a_line_break_answers_a_well_formed_header(
    engine_client: TestClient,
) -> None:
    """A crafted name must not add a header, split the one it is in, or break the answer."""
    name = 'resume"; x-injected: yes\r\n.txt'
    uploaded = upload(engine_client)
    set_filename(uploaded["id"], name)

    response = downloaded(engine_client, uploaded["id"])

    assert response.status_code == 200, response.text
    header = response.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header  # no injection, no split header
    assert len(response.headers.get_list("content-disposition")) == 1
    assert "x-injected" not in response.headers
    assert carried_name(header) == name  # carried, percent-encoded, and not dropped
    assert response.content == SENTINEL


# The refusals: no row, no id, no file, no path of ours. Every one writes nothing.


def test_an_id_with_no_row_is_404_naming_the_id_and_writes_nothing(
    engine_client: TestClient, uploads_dir: Path
) -> None:
    uploaded = upload(engine_client)
    missing = uploaded["id"] + 1
    before = table_state()
    files = stored_files(uploads_dir)

    detail = refusal(downloaded(engine_client, missing), 404)

    assert str(missing) in detail  # the refusal names the id it could not find
    assert table_state() == before
    assert stored_files(uploads_dir) == files


@pytest.mark.parametrize("value", ["abc", "1.5", "one"])
def test_a_path_value_that_is_not_a_resume_id_is_422(
    engine_client: TestClient, uploads_dir: Path, value: str
) -> None:
    upload(engine_client)
    before = table_state()
    files = stored_files(uploads_dir)

    response = engine_client.get(f"/resume/{value}/file")

    assert response.status_code == 422, response.text
    assert table_state() == before
    assert stored_files(uploads_dir) == files


def test_a_row_whose_stored_file_is_gone_is_404_and_not_500(
    engine_client: TestClient, uploads_dir: Path
) -> None:
    uploaded = upload(engine_client)
    row_of(uploaded["id"])["stored_path"].unlink()

    missing_file = refusal(downloaded(engine_client, uploaded["id"]), 404)
    unknown_id = refusal(downloaded(engine_client, uploaded["id"] + 1), 404)

    assert "stored file" in missing_file  # it says which file is missing, not just which id
    assert str(uploaded["id"]) in missing_file
    assert missing_file != unknown_id  # told apart from an id that has no row
    assert stored_files(uploads_dir) == []  # nothing is created in its place


def test_a_stored_path_outside_the_uploads_directory_is_refused_before_it_is_opened(
    engine_client: TestClient,
    tmp_path: Path,
    open_spy: OpenSpy,
) -> None:
    """A path this app never wrote is never read: the guard runs before the response opens it."""
    outside = tmp_path / "outside.txt"  # beside the uploads directory, never inside it
    outside.write_bytes(SENTINEL)
    seeded = seed_row("seeded.txt", outside)

    response = downloaded(engine_client, seeded)

    assert response.status_code == 404, response.text
    assert SENTINEL not in response.content
    assert SENTINEL_TEXT not in response.text
    assert outside.read_bytes() == SENTINEL  # left exactly as it was

    # The spy is shown to see an open of its own, so the absence above is not an
    # artifact of a spy that records nothing.
    probe = tmp_path / "probe.txt"
    with builtins.open(probe, "wb") as written:
        written.write(b"")
    assert open_spy.paths == [str(probe)]


# The bytes leave in no log record and in no refusal body.


def test_the_file_reaches_no_log_record_and_the_log_names_the_id_and_the_size(
    engine_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    uploaded = upload(engine_client)
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    response = downloaded(engine_client, uploaded["id"])

    assert response.status_code == 200
    assert response.content == SENTINEL
    # An empty capture would make the absence below no evidence at all.
    assert caplog.records, "the route's own line is what makes this capture non-vacuous"
    lines = log_lines(caplog.records)
    assert [line for line in lines if SENTINEL_TEXT in line] == []
    assert [
        line
        for line in lines
        if str(uploaded["id"]) in line and str(len(SENTINEL)) in line
    ], "the route names the resume's id and the file's size and no contents"


def test_neither_refusal_answers_with_the_file(
    engine_client: TestClient, uploads_dir: Path
) -> None:
    uploaded = upload(engine_client)
    row_of(uploaded["id"])["stored_path"].unlink()

    missing_file = downloaded(engine_client, uploaded["id"])
    unknown_id = downloaded(engine_client, uploaded["id"] + 1)

    assert (missing_file.status_code, unknown_id.status_code) == (404, 404)
    for response in (missing_file, unknown_id):
        assert SENTINEL not in response.content
        assert SENTINEL_TEXT not in response.text


def test_a_download_writes_nothing_into_the_repository(engine_client: TestClient) -> None:
    before = git("status", "--short", "--untracked-files=all")
    uploaded = upload(engine_client)

    response = downloaded(engine_client, uploaded["id"])

    assert response.status_code == 200
    assert git("status", "--short", "--untracked-files=all") == before
