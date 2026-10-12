"""`POST /resume`: the formats it reads, the refusals it answers with, and where the file lands.

Every test drives the real app through `TestClient` -- no server, no port, no
network -- with `DATABASE_URL` and `UPLOADS_DIR` pointed at a temporary
directory, so the suite uploads to a scratch directory and is the only thing
that writes there. `TestClient` re-raises an exception the app does not handle,
so a `422` here is also proof that the rejection did not escape as a `500`.

The fixtures are synthetic, written for these tests: the repository is public and
holds no resume, real or otherwise.
"""

import io
import json
import logging
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select

from app import db
from app.main import app
from app.models import Resume

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A small synthetic PDF carrying a text layer, built for this test file.
SENTENCE_BEARING_PDF = FIXTURES / "synthetic-resume.pdf"
# The same shape of PDF with a blank page: it parses, and has nothing to read.
BLANK_PAGE_PDF = FIXTURES / "blank-page.pdf"

# The sentence both PDF fixtures were built with, and the text these tests upload.
SENTENCE = "SYNTHETIC RESUME for JobRadar tests. Not a real person's resume."

TXT_CONTENTS = f"{SENTENCE}\nSkills: Python, SQL.\n".encode()

# A distinctive line for the leak checks: a log record or a response body
# holding it fails, and an empty stream or body would be no evidence at all.
SENTINEL = "SENTINEL-RESUME-LINE: this text must never leave the database."

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A limit small enough that an oversized upload is cheap to send.
SMALL_LIMIT = 1024

_UNSUPPORTED_DECLARED_TYPES = [
    # A scan format this file's own task does not accept: it is still a `415` now
    # that a scan can be read, and #53 is where TIFF, HEIC and WebP arrive.
    ("scan.tiff", b"II*\x00", "image/tiff"),
    ("archive.zip", b"PK\x03\x04", "application/zip"),
    ("rows.csv", b"title,company\n", "text/csv"),
    # Real PDF bytes under a type nobody promised to read: refused as well,
    # which is the cost of deciding the format from the declared type.
    ("resume.pdf", SENTENCE_BEARING_PDF.read_bytes(), "application/octet-stream"),
]

# Client filenames that would escape the uploads directory if they were used as a path.
_TRAVERSING_FILENAMES = [
    "../../evil.txt",
    "/etc/passwd.txt",
    "sub/directory/resume.txt",
    "..\\..\\windows\\evil.txt",
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with a temporary database and a temporary uploads directory."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir_of(tmp_path)))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def uploads_dir(tmp_path: Path) -> Path:
    """The directory `client`'s uploads land in: the suite's own, never the default."""
    return uploads_dir_of(tmp_path)


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    """A configured limit small enough to exceed with a few kilobytes."""
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(SMALL_LIMIT))
    return SMALL_LIMIT


def uploads_dir_of(tmp_path: Path) -> Path:
    """Where the `client` fixture points `UPLOADS_DIR`: inside the test's temporary directory."""
    return tmp_path / "uploads"


def upload(client: TestClient, filename: str, data: bytes, content_type: str) -> Response:
    """One multipart upload, the way a client sends it."""
    return client.post("/resume", files={"file": (filename, data, content_type)})


def multipart_body(
    data: bytes, filename: str, content_type: str = "text/plain"
) -> tuple[bytes, dict[str, str]]:
    """A multipart body built by hand, for a request that streams its parts."""
    boundary = "jobradar-test-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


def rows() -> list[dict[str, Any]]:
    """Every `resume` row as plain values, read while the app's own session is open."""
    with db.session_scope() as session:
        return [
            {
                "id": row.id,
                "filename": row.filename,
                "content_type": row.content_type,
                "text": row.text,
                "stored_path": Path(row.stored_path),
                "is_active": row.is_active,
            }
            for row in session.scalars(select(Resume).order_by(Resume.id))
        ]


def stored_files(uploads_dir: Path) -> list[Path]:
    """What is in the uploads directory: a rejection has to leave this empty."""
    return sorted(uploads_dir.iterdir()) if uploads_dir.exists() else []


def refusal(response: Response, status: int) -> str:
    """The message a rejection answered with, having checked it is a JSON body with one."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    detail = response.json()["detail"]
    assert detail, "a rejection must say what it rejected"
    return detail if isinstance(detail, str) else json.dumps(detail)


def collapsed(text: str) -> str:
    """`text` with every run of whitespace collapsed, since extraction may re-wrap lines."""
    return " ".join(text.split())


def docx_bytes(paragraphs: list[str], part: str = "word/document.xml") -> bytes:
    """A minimal DOCX: an OOXML package holding one `w:p` per paragraph."""
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(part, document)
    return package.getvalue()


def xlsx_bytes() -> bytes:
    """A ZIP that is an Excel workbook rather than a Word document."""
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return package.getvalue()


def git(*arguments: str) -> str:
    """Run git in the repository the suite lives in."""
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


# The three supported formats, each round-tripped through the real extractor.


def test_a_txt_upload_is_stored_as_the_active_resume(
    client: TestClient, uploads_dir: Path
) -> None:
    response = upload(client, "resume.txt", TXT_CONTENTS, "text/plain")

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body["is_active"] is True
    assert body["filename"] == "resume.txt"

    (row,) = rows()
    assert row["id"] == body["id"]
    assert row["filename"] == "resume.txt"
    assert row["content_type"] == "text/plain"
    assert row["text"] == TXT_CONTENTS.decode("utf-8")  # exactly the file's UTF-8 contents
    assert row["is_active"] is True
    assert row["stored_path"].parent == uploads_dir
    assert row["stored_path"].read_bytes() == TXT_CONTENTS


def test_a_pdf_upload_stores_the_sentence_its_fixture_carries(
    client: TestClient, uploads_dir: Path
) -> None:
    pdf = SENTENCE_BEARING_PDF.read_bytes()
    assert pdf.startswith(b"%PDF")  # the fixture really is a PDF, not a renamed text file

    response = upload(client, "resume.pdf", pdf, "application/pdf")

    assert response.status_code == 201
    (row,) = rows()
    assert row["content_type"] == "application/pdf"
    assert SENTENCE in collapsed(row["text"])
    assert row["stored_path"] == stored_files(uploads_dir)[0]
    assert row["stored_path"].read_bytes() == pdf  # the file kept, byte for byte


def test_a_docx_upload_stores_the_sentence_its_document_carries(
    client: TestClient, uploads_dir: Path
) -> None:
    document = docx_bytes([SENTENCE, "Skills: Python, SQL."])

    response = upload(client, "resume.docx", document, DOCX)

    assert response.status_code == 201
    (row,) = rows()
    assert row["content_type"] == DOCX
    assert SENTENCE in collapsed(row["text"])
    assert "Skills: Python, SQL." in collapsed(row["text"])
    assert row["stored_path"].read_bytes() == document


# The type gate: the declared type, and then the bytes.


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    _UNSUPPORTED_DECLARED_TYPES,
    ids=[case[2] for case in _UNSUPPORTED_DECLARED_TYPES],
)
def test_an_unsupported_declared_type_is_rejected_with_415(
    client: TestClient,
    uploads_dir: Path,
    filename: str,
    data: bytes,
    content_type: str,
) -> None:
    message = refusal(upload(client, filename, data, content_type), 415)

    assert content_type in message  # the refusal names the type, so it is fixable
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_bytes_that_contradict_a_supported_type_are_rejected_with_415(
    client: TestClient, uploads_dir: Path
) -> None:
    """Flipping only the client's Content-Type string must not get a file in.

    Neither body is the format it claims: plain words named `application/pdf`, a
    ZIP that is an Excel workbook (`xl/workbook.xml`, no `word/document.xml`)
    named as a Word document, and a third pair of plain words named `image/png` --
    a scan format now, so its bytes are what keeps a text body out.
    """
    not_a_pdf = refusal(
        upload(client, "resume.pdf", b"plain words, no PDF header", "application/pdf"), 415
    )
    not_a_docx = refusal(upload(client, "resume.docx", xlsx_bytes(), DOCX), 415)
    not_a_scan = refusal(upload(client, "diagram.png", SENTINEL.encode(), "image/png"), 415)

    assert "application/pdf" in not_a_pdf
    assert DOCX in not_a_docx
    assert "image/png" in not_a_scan
    assert SENTINEL not in not_a_scan  # the refusal names the type, never the content
    assert rows() == []
    assert stored_files(uploads_dir) == []


# The size gate, against the bytes that arrived rather than against a header.


def test_an_upload_over_the_limit_is_rejected_with_413(
    client: TestClient, uploads_dir: Path, small_limit: int
) -> None:
    message = refusal(upload(client, "big.txt", b"x" * (small_limit + 1), "text/plain"), 413)

    assert str(small_limit) in message  # the refusal names the limit
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_a_file_at_the_limit_is_accepted(client: TestClient, small_limit: int) -> None:
    """The limit is what is refused when it is exceeded, not when it is met."""
    response = upload(client, "at-the-limit.txt", b"y" * small_limit, "text/plain")

    assert response.status_code == 201


def test_an_upload_that_understates_content_length_is_still_rejected(
    client: TestClient, uploads_dir: Path, small_limit: int
) -> None:
    response = client.post(
        "/resume",
        files={"file": ("big.txt", b"x" * (small_limit * 2), "text/plain")},
        headers={"content-length": "10"},
    )

    refusal(response, 413)
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_an_upload_with_no_content_length_is_still_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    small_limit: int,
) -> None:
    """A streamed body announces no length, and the bytes received are what count."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir_of(tmp_path)))
    body, headers = multipart_body(b"z" * (small_limit * 2), "chunked.txt")
    seen: dict[str, Any] = {}

    async def recording_app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            seen["headers"] = [name.lower() for name, _ in scope["headers"]]
        await app(scope, receive, send)

    with TestClient(recording_app) as recorded_client:
        response = recorded_client.post("/resume", content=iter([body]), headers=headers)

    assert b"content-length" not in seen["headers"], "the request did not omit the header"
    refusal(response, 413)
    assert rows() == []
    assert stored_files(uploads_dir_of(tmp_path)) == []


# Corrupt uploads: a 422 the app answers itself, never a 500.


def test_a_truncated_pdf_is_rejected_with_422(client: TestClient, uploads_dir: Path) -> None:
    truncated = SENTENCE_BEARING_PDF.read_bytes()[:300]

    message = refusal(upload(client, "resume.pdf", truncated, "application/pdf"), 422)

    assert "corrupt" in message
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_a_text_file_that_is_not_utf8_is_rejected_with_422(
    client: TestClient, uploads_dir: Path
) -> None:
    message = refusal(upload(client, "resume.txt", b"SYNTHETIC \xff\xfe bytes", "text/plain"), 422)

    assert "UTF-8" in message
    assert rows() == []
    assert stored_files(uploads_dir) == []


# Nothing to read: a page with no text layer, and a text file with no text.


def test_a_pdf_without_a_text_layer_is_rejected_and_one_with_text_is_not(
    client: TestClient, uploads_dir: Path
) -> None:
    blank = refusal(
        upload(client, "blank.pdf", BLANK_PAGE_PDF.read_bytes(), "application/pdf"), 422
    )

    assert "no text" in blank.lower()
    assert rows() == []

    # The control: the same app accepts the text-bearing fixture, so the refusal
    # above is about the text layer and not about every PDF.
    accepted = upload(client, "resume.pdf", SENTENCE_BEARING_PDF.read_bytes(), "application/pdf")
    assert accepted.status_code == 201
    assert len(rows()) == 1

@pytest.mark.parametrize("data", [b"", b"   \n\t  ", b"\n\n"], ids=["empty", "spaces", "newlines"])
def test_a_text_file_with_no_text_is_rejected_with_422(
    client: TestClient, uploads_dir: Path, data: bytes
) -> None:
    message = refusal(upload(client, "blank.txt", data, "text/plain"), 422)

    assert "no text" in message.lower()
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_a_docx_with_no_text_is_rejected_with_422(client: TestClient, uploads_dir: Path) -> None:
    message = refusal(upload(client, "blank.docx", docx_bytes([]), DOCX), 422)

    assert "no text" in message.lower()
    assert rows() == []
    assert stored_files(uploads_dir) == []


# A request that is not one named file part: still a clear 422, never a 500.


def test_a_request_with_no_file_part_is_rejected_with_422(
    client: TestClient, uploads_dir: Path
) -> None:
    message = refusal(client.post("/resume"), 422)

    assert "file" in message
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_a_part_without_a_filename_is_rejected_with_422(
    client: TestClient, uploads_dir: Path
) -> None:
    response = client.post("/resume", files={"file": (None, SENTINEL.encode(), "text/plain")})

    message = refusal(response, 422)

    assert "file" in message
    # The framework's own validation error is the one body that would echo the
    # part back, so the upload's text is checked for here as well.
    assert SENTINEL not in response.text
    assert rows() == []
    assert stored_files(uploads_dir) == []


# One active resume, however many uploads arrived.


def test_a_second_upload_supersedes_the_first_and_leaves_one_active_resume(
    client: TestClient,
) -> None:
    first = upload(client, "resume.txt", b"first synthetic resume", "text/plain")
    second = upload(client, "resume.txt", b"second synthetic resume", "text/plain")

    assert (first.status_code, second.status_code) == (201, 201)
    stored = rows()
    assert [row["is_active"] for row in stored] == [False, True]
    assert stored[1]["id"] == second.json()["id"]
    assert stored[1]["text"] == "second synthetic resume"
    assert stored[0]["stored_path"].is_file()  # the superseded file is left where it was


@pytest.mark.parametrize("uploads", [2, 3, 4])
def test_a_successful_upload_always_leaves_exactly_one_active_resume(
    client: TestClient, uploads: int
) -> None:
    responses = [
        upload(client, f"resume-{number}.txt", f"synthetic resume {number}".encode(), "text/plain")
        for number in range(1, uploads + 1)
    ]

    assert [response.status_code for response in responses] == [201] * uploads
    stored = rows()
    assert sum(row["is_active"] for row in stored) == 1
    assert stored[-1]["is_active"] is True
    assert stored[-1]["id"] == responses[-1].json()["id"]


# Where the file is written: the uploads directory, with a name we generate.


@pytest.mark.parametrize("client_filename", _TRAVERSING_FILENAMES)
def test_the_client_filename_never_chooses_where_the_file_is_written(
    client: TestClient,
    uploads_dir: Path,
    tmp_path: Path,
    client_filename: str,
) -> None:
    response = upload(client, client_filename, b"traversal probe", "text/plain")

    assert response.status_code == 201
    (row,) = rows()
    stored = row["stored_path"]
    assert stored.parent == uploads_dir
    assert stored.resolve().is_relative_to(uploads_dir.resolve())
    assert stored.read_bytes() == b"traversal probe"  # the row names the file on disk
    assert row["filename"] == client_filename  # recorded as sent, used as a name and no more
    assert stored.name != Path(client_filename).name
    assert stored_files(uploads_dir) == [stored]

    # Nothing appeared at a traversal target, and no directory below the uploads one.
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "windows").exists()
    assert not (uploads_dir / "sub").exists()


def test_an_upload_writes_nothing_into_the_repository(client: TestClient) -> None:
    before = git("status", "--short", "--untracked-files=all")

    response = upload(client, "resume.txt", TXT_CONTENTS, "text/plain")

    assert response.status_code == 201
    assert not (BACKEND_ROOT / "uploads").exists()
    assert git("status", "--short", "--untracked-files=all") == before


# The resume text leaves in no log line and no response body.


def _log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


def test_the_resume_text_reaches_no_log_record(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The stream is captured at DEBUG, and the capture is shown to have seen something."""
    caplog.set_level(logging.DEBUG)

    response = upload(client, "sentinel.txt", SENTINEL.encode(), "text/plain")

    assert response.status_code == 201
    assert caplog.records  # an empty stream would make the absence below meaningless
    assert [line for line in _log_lines(caplog.records) if SENTINEL in line] == []
    assert SENTINEL not in response.text


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        ("sentinel.csv", SENTINEL.encode(), "text/csv"),
        ("sentinel.pdf", SENTINEL.encode(), "application/pdf"),
        ("sentinel.txt", b"   ", "text/plain"),
        ("sentinel.txt", SENTINEL.encode() + b"\xff", "text/plain"),
        ("sentinel.txt", (SENTINEL * 40).encode(), "text/plain"),
    ],
    ids=["unsupported-type", "pdf-bytes", "no-text", "not-utf8", "oversized"],
)
def test_a_rejected_upload_never_answers_with_the_resume_text(
    client: TestClient,
    small_limit: int,
    filename: str,
    data: bytes,
    content_type: str,
) -> None:
    response = upload(client, filename, data, content_type)

    assert response.status_code != 201
    assert SENTINEL not in response.text
