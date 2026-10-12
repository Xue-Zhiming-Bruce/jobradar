"""`POST /resume` with a scan: the two new types, the reader's refusal, and what is stored.

Every case drives the real app through `TestClient` -- no server, no port, no
network -- with `DATABASE_URL` and `UPLOADS_DIR` pointed at a temporary
directory, and every case but the missing-key one installs `FakeResumeOcr`
behind the route's own seam, `app.routes.resume.get_resume_ocr`, so no case here
needs a key, a socket or a model. An autouse fixture patches
`socket.socket.connect` to raise and deletes `LLM_API_KEY`, so an implementation
that ignored the seam, or that read a real key from the shell, fails here instead
of quietly passing. The cases that assert stored rows take the shared `engine`
fixture, so they run on SQLite and on PostgreSQL.

The scans are synthetic and built by this module -- a small PNG, a small JPEG and
a PDF whose pages carry those images -- because the repository is public and holds
no resume, real or otherwise. The text a scan yields is user data and the key is a
credential, so the last case proves neither reaches a body or a log record.
"""

import ast
import inspect
import io
import json
import logging
import socket
import zipfile
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import db
from app.main import app
from app.matching.resume_ocr import FakeResumeOcr, LlmResumeOcr, ResumeOcr, ResumeOcrError
from app.matching.scorer import LLM_API_KEY
from app.models import Resume
from app.routes.resume import get_resume_ocr, upload_resume
from app.services import resume as resume_service

BACKEND_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESUME_ROUTE_MODULE = BACKEND_ROOT / "app" / "routes" / "resume.py"
RESUME_ROUTE_SOURCE = RESUME_ROUTE_MODULE.read_text(encoding="utf-8")

# A small synthetic PDF carrying a text layer: the control for "no reader is asked".
SENTENCE_BEARING_PDF = FIXTURES / "synthetic-resume.pdf"
SENTENCE = "SYNTHETIC RESUME for JobRadar tests. Not a real person's resume."

TEXT_PLAIN = "text/plain"
APPLICATION_PDF = "application/pdf"
IMAGE_PNG = "image/png"
IMAGE_JPEG = "image/jpeg"
# A scan format this task does not accept and #53 does: still a 415.
IMAGE_TIFF = "image/tiff"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A limit small enough that an oversized scan is cheap to send.
SMALL_LIMIT = 64

# The text the fake reader answers with, as a sentinel: it is user data, so it may
# reach the row and nothing else. A distinctive line makes that check able to fail.
SENTINEL = "SENTINEL-SCAN-LINE: this text must never leave the database."

# A recognisable key: not a real credential, and not allowed to appear anywhere.
THE_KEY = "unit-test-key-not-a-real-credential"


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No case here reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key, whatever its shell holds: a real one cannot decide a case."""
    monkeypatch.delenv(LLM_API_KEY, raising=False)


@pytest.fixture(autouse=True)
def clean_ocr_override() -> Iterator[None]:
    """Every case leaves the app's dependency overrides as it found them."""
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_resume_ocr, None)


@pytest.fixture
def client(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The app over this case's engine, with its uploads in a temporary directory."""
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir_of(tmp_path)))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def uploads_dir(tmp_path: Path) -> Path:
    """The directory this case's uploads land in: the suite's own, never the default."""
    return uploads_dir_of(tmp_path)


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    """A configured limit small enough to exceed with one scan."""
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(SMALL_LIMIT))
    return SMALL_LIMIT


def uploads_dir_of(tmp_path: Path) -> Path:
    """Where the `client` fixture points `UPLOADS_DIR`: inside the test's temporary directory."""
    return tmp_path / "uploads"


def install(ocr: ResumeOcr) -> None:
    """Put `ocr` behind the route's own seam, in place of the real reader."""
    app.dependency_overrides[get_resume_ocr] = lambda: ocr


def png_bytes(size: tuple[int, int] = (40, 30)) -> bytes:
    """One synthetic greyscale PNG: a page of a scan, made here, holding no real text."""
    buffer = BytesIO()
    Image.new("L", size, color=200).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(size: tuple[int, int] = (60, 20)) -> bytes:
    """The same page as a JPEG: the other scan format this app accepts."""
    buffer = BytesIO()
    Image.new("L", size, color=200).save(buffer, format="JPEG")
    return buffer.getvalue()


def scanned_pdf_bytes(pages: list[tuple[int, int]]) -> bytes:
    """A PDF whose pages carry one image each, built by Pillow: a scan, with no text layer."""
    images = [Image.new("L", size, color=200) for size in pages]
    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def docx_bytes(paragraphs: list[str]) -> bytes:
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
        archive.writestr("word/document.xml", document)
    return package.getvalue()


def upload(client: TestClient, filename: str, data: bytes, content_type: str) -> Response:
    """One multipart upload, the way a client sends it."""
    return client.post("/resume", files={"file": (filename, data, content_type)})


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
                "parsed_fields": row.parsed_fields,
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


def log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


class RefusingOcr:
    """A `ResumeOcr` whose one call failed: no model, no transport, one raised type."""

    def __init__(self, message: str) -> None:
        self.message = message

    def read(self, content_type: str, data: bytes) -> str:
        raise ResumeOcrError(self.message)


# A scan is accepted, read once, and its answer stored as the active resume's text.


@pytest.mark.parametrize(
    ("filename", "data", "content_type", "suffix"),
    [
        pytest.param("scan.png", png_bytes(), IMAGE_PNG, ".png", id="png"),
        pytest.param("scan.jpg", jpeg_bytes(), IMAGE_JPEG, ".jpg", id="jpeg"),
    ],
)
def test_a_scan_image_is_accepted_and_the_reader_s_text_is_stored(
    client: TestClient,
    uploads_dir: Path,
    filename: str,
    data: bytes,
    content_type: str,
    suffix: str,
) -> None:
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    response = upload(client, filename, data, content_type)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "id": body["id"],
        "filename": filename,
        "content_type": content_type,
        "is_active": True,
    }
    # The reader was asked once, for this upload's own bytes.
    assert reader.calls == [(content_type, data)]
    (row,) = rows()
    assert row["id"] == body["id"]
    assert row["text"] == SENTINEL  # the OCR'd text, and nothing else, is the row's text
    assert row["parsed_fields"] is None  # #26 parses it on its own first call
    assert row["is_active"] is True
    assert sum(row["is_active"] for row in rows()) == 1
    # The file is the upload, byte for byte, under the format's own extension.
    assert row["stored_path"] == stored_files(uploads_dir)[0]
    assert row["stored_path"].suffix == suffix
    assert row["stored_path"].read_bytes() == data


def test_a_scanned_pdf_is_read_once_and_the_upload_lands_as_one_file(
    client: TestClient, uploads_dir: Path
) -> None:
    """The page images are request payloads: only the upload itself is written anywhere."""
    pdf = scanned_pdf_bytes([(40, 30), (60, 20)])
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    response = upload(client, "scan.pdf", pdf, APPLICATION_PDF)

    assert response.status_code == 201, response.text
    assert reader.calls == [(APPLICATION_PDF, pdf)]
    (row,) = rows()
    assert row["text"] == SENTINEL
    assert row["stored_path"].read_bytes() == pdf
    assert stored_files(uploads_dir) == [row["stored_path"]]


def test_a_second_scan_supersedes_the_first_and_leaves_one_active_resume(
    client: TestClient,
) -> None:
    install(FakeResumeOcr("first scan text"))
    assert upload(client, "first.png", png_bytes(), IMAGE_PNG).status_code == 201
    install(FakeResumeOcr("second scan text"))
    assert upload(client, "second.png", png_bytes(), IMAGE_PNG).status_code == 201

    stored = rows()

    assert [row["text"] for row in stored] == ["first scan text", "second scan text"]
    assert [row["is_active"] for row in stored] == [False, True]
    assert sum(row["is_active"] for row in stored) == 1


# Only a text-less upload of an OCR-able type is read, and only once.


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        pytest.param(
            "resume.txt",
            f"{SENTENCE}\nSkills: Python, SQL.\n".encode(),
            TEXT_PLAIN,
            id="txt",
        ),
        pytest.param("resume.docx", docx_bytes([SENTENCE]), DOCX, id="docx"),
        pytest.param(
            "resume.pdf", SENTENCE_BEARING_PDF.read_bytes(), APPLICATION_PDF, id="a PDF with text"
        ),
    ],
)
def test_an_upload_whose_own_reader_found_text_never_asks_the_reader(
    client: TestClient, filename: str, data: bytes, content_type: str
) -> None:
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    response = upload(client, filename, data, content_type)

    assert response.status_code == 201, response.text
    assert reader.calls == []
    (row,) = rows()
    assert row["text"].strip()  # its own reader's text, not the fake's
    assert SENTINEL not in row["text"]


@pytest.mark.parametrize(
    ("filename", "data", "content_type"),
    [
        pytest.param("blank.txt", b"   \n\t  ", TEXT_PLAIN, id="a blank text file"),
        pytest.param("blank.docx", docx_bytes([]), DOCX, id="a blank DOCX"),
    ],
)
def test_a_blank_text_file_or_docx_is_422_and_the_reader_is_never_consulted(
    client: TestClient,
    uploads_dir: Path,
    filename: str,
    data: bytes,
    content_type: str,
) -> None:
    """Neither format is a scan, so a file with no text is the refusal #3 already made."""
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    message = refusal(upload(client, filename, data, content_type), 422)

    assert "no text" in message.lower()
    assert reader.calls == []
    assert rows() == []
    assert stored_files(uploads_dir) == []


# The type gate, on the two types this task adds.


@pytest.mark.parametrize(
    ("filename", "data", "content_type", "message"),
    [
        pytest.param(
            "scan.png",
            SENTINEL.encode(),
            IMAGE_PNG,
            "Unsupported resume type 'image/png': the file's bytes are not a PNG image",
            id="a text body declared a PNG",
        ),
        pytest.param(
            "scan.png",
            png_bytes(),
            IMAGE_JPEG,
            "Unsupported resume type 'image/jpeg': the file's bytes are not a JPEG image",
            id="PNG bytes declared a JPEG",
        ),
        pytest.param(
            "scan.tiff",
            b"II*\x00",
            IMAGE_TIFF,
            "Unsupported resume type 'image/tiff': send a PDF, a DOCX, a PNG, a JPEG or a "
            "plain text file",
            id="a scan format this task does not accept",
        ),
    ],
)
def test_a_scan_that_is_not_the_type_its_bytes_are_is_415(
    client: TestClient,
    uploads_dir: Path,
    filename: str,
    data: bytes,
    content_type: str,
    message: str,
) -> None:
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    detail = refusal(upload(client, filename, data, content_type), 415)

    assert detail == message
    assert SENTINEL not in detail  # the refusal names the type, never the content
    assert reader.calls == []
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_a_truncated_scan_is_422_naming_the_damage_and_no_model_is_asked(
    client: TestClient, uploads_dir: Path
) -> None:
    """Corrupt bytes are a bad upload, not a model failure: the reader is never reached."""
    truncated = png_bytes()[:8] + b"not an image after the signature"
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    message = refusal(upload(client, "scan.png", truncated, IMAGE_PNG), 422)

    assert message == "The image/png file is corrupt or truncated: it could not be read"
    assert reader.calls == []
    assert rows() == []
    assert stored_files(uploads_dir) == []


# A scan that yielded no text: #3's own refusal, reached by the reader's answer.


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        pytest.param(png_bytes(), IMAGE_PNG, id="an image the reader read as nothing"),
        pytest.param(
            scanned_pdf_bytes([(40, 30)]), APPLICATION_PDF, id="a PDF whose pages hold no image"
        ),
    ],
)
@pytest.mark.parametrize("answer", ["", "   \n\t "], ids=["empty", "whitespace"])
def test_a_scan_the_reader_yielded_no_text_for_is_the_422_that_names_no_text(
    client: TestClient,
    uploads_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    content_type: str,
    answer: str,
) -> None:
    """A blank text file is not a scan; a scan the model could not read is still no text."""
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = FakeResumeOcr(answer)
    install(reader)

    message = refusal(upload(client, "scan", data, content_type), 422)

    assert message == (
        f"No text found in the {content_type} file: it is blank, or carries no text layer"
    )
    assert reader.calls == [(content_type, data)]  # asked once, and the answer was nothing
    assert rows() == []
    assert stored_files(uploads_dir) == []


# The two failures a scan adds, and the limit it still meets.


def test_a_missing_key_is_503_naming_the_variable_and_not_its_value(
    client: TestClient, uploads_dir: Path
) -> None:
    # No override: this case reaches the real reader, which refuses before any request.
    response = upload(client, "scan.png", png_bytes(), IMAGE_PNG)

    assert response.status_code == 503
    assert response.json() == {
        "detail": f"{LLM_API_KEY} is not set: reading a scan needs a model key"
    }
    assert THE_KEY not in response.text
    assert rows() == []
    assert stored_files(uploads_dir) == []


@pytest.mark.parametrize(
    "message",
    [
        "the model call did not answer in time",
        "the model call failed (HTTP 500)",
        "the model's answer was unusable after 3 attempts",
    ],
    ids=["a timeout", "a non-2xx", "an unusable answer"],
)
def test_a_reader_that_failed_is_502_with_the_scorer_s_own_wording(
    client: TestClient, uploads_dir: Path, message: str
) -> None:
    """The route answers the reader's type by name: one status, and nothing stored."""
    install(RefusingOcr(message))

    detail = refusal(upload(client, "scan.png", png_bytes(), IMAGE_PNG), 502)

    assert detail == message
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_an_upload_over_the_limit_is_413_and_the_reader_is_never_consulted(
    client: TestClient, uploads_dir: Path, small_limit: int
) -> None:
    data = png_bytes((80, 80))
    assert len(data) > small_limit
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    message = refusal(upload(client, "big.png", data, IMAGE_PNG), 413)

    assert message == f"Resume is {len(data)} bytes: the upload limit is {small_limit} bytes"
    assert reader.calls == []
    assert rows() == []
    assert stored_files(uploads_dir) == []


def test_a_body_that_is_not_one_named_file_part_is_still_422_and_asks_nothing(
    client: TestClient, uploads_dir: Path
) -> None:
    reader = FakeResumeOcr(SENTINEL)
    install(reader)

    response = client.post("/resume", files={"file": (None, png_bytes(), IMAGE_PNG)})

    assert "file" in refusal(response, 422)
    assert reader.calls == []
    assert rows() == []
    assert stored_files(uploads_dir) == []


# The route's second seam, and the event loop it must not block.


def test_the_reader_reaches_the_route_through_one_dependency() -> None:
    """One provider, declared the way `app.routes.resume.get_resume_parser` is."""
    provider = get_resume_ocr()

    assert isinstance(provider, ResumeOcr)
    assert isinstance(provider, LlmResumeOcr)
    assert list(inspect.signature(get_resume_ocr).parameters) == []
    assert list(inspect.signature(upload_resume).parameters) == ["file", "ocr"]


def test_the_upload_stays_async_and_reads_a_scan_in_a_thread() -> None:
    """A model takes seconds, and the loop that serves every other request does not wait."""
    assert inspect.iscoroutinefunction(upload_resume)

    tree = ast.parse(RESUME_ROUTE_SOURCE)
    (route,) = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "upload_resume"
    ]
    off_loaded = [
        node
        for node in ast.walk(route)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_in_threadpool"
    ]
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }

    assert len(off_loaded) == 1
    assert "run_in_threadpool" in imported
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "fastapi.concurrency"
        for node in tree.body
    )


# The page images: the upload's own image, or the PDF's pages, each a PNG.


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        pytest.param(png_bytes((40, 30)), IMAGE_PNG, id="png"),
        pytest.param(jpeg_bytes((60, 20)), IMAGE_JPEG, id="jpeg"),
    ],
)
def test_a_scan_image_is_its_own_one_page_image(data: bytes, content_type: str) -> None:
    (page,) = resume_service.scan_images(content_type, data)

    assert page.startswith(b"\x89PNG\r\n\x1a\n")
    assert Image.open(BytesIO(page)).size == Image.open(BytesIO(data)).size


def test_a_scanned_pdf_yields_one_png_per_page_image() -> None:
    pdf = scanned_pdf_bytes([(40, 30), (60, 20)])

    pages = resume_service.scan_images(APPLICATION_PDF, pdf)

    assert [Image.open(BytesIO(page)).size for page in pages] == [(40, 30), (60, 20)]
    for page in pages:
        assert page.startswith(b"\x89PNG\r\n\x1a\n")


def test_a_pdf_whose_pages_hold_no_image_yields_no_page_images() -> None:
    """Nothing to send means no request: the route turns the empty text into the 422."""
    assert resume_service.scan_images(APPLICATION_PDF, SENTENCE_BEARING_PDF.read_bytes()) == []


def test_an_image_that_will_not_load_is_the_corrupt_upload_error() -> None:
    truncated = jpeg_bytes()[:3] + b"no image data follows"

    with pytest.raises(resume_service.UnreadableResume):
        resume_service.scan_images(IMAGE_JPEG, truncated)


# The OCR'd text and the key: in no log record and no body that is not the scan's own.


def test_neither_the_ocr_text_nor_the_key_reaches_a_log_or_a_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    caplog.set_level(logging.DEBUG)
    data = png_bytes()
    install(FakeResumeOcr(SENTINEL))
    stored = upload(client, "scan.png", data, IMAGE_PNG)
    install(RefusingOcr("the model call failed (HTTP 500)"))
    failed = upload(client, "scan-again.png", data, IMAGE_PNG)
    # A record of the test's own, so an empty stream cannot pass as evidence.
    logging.getLogger("tests.resume_scan").debug("the capture is live")

    assert (stored.status_code, failed.status_code) == (201, 502)
    (row,) = rows()  # the second scan was refused, so the first row is still the only one
    for response in (stored, failed):
        assert SENTINEL not in response.text
        assert THE_KEY not in response.text

    lines = log_lines(caplog.records)
    assert lines, "the capture must be non-vacuous"
    assert [line for line in lines if SENTINEL in line] == []
    assert [line for line in lines if THE_KEY in line] == []
    # The route's own line names the row, the media type, the byte count and OCR, no text.
    route_lines = [line for line in lines if "Stored resume" in line]
    assert len(route_lines) == 1
    assert f"Stored resume {row['id']}: {IMAGE_PNG}, {len(data)} bytes" in route_lines[0]
    assert "(text read by OCR)" in route_lines[0]
    # The key reached no column either: not the text, not the filename, not the path.
    assert [value for value in row.values() if isinstance(value, str) and THE_KEY in value] == []
