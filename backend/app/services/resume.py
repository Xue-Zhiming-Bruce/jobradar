"""Reading a resume out of an upload, reading back the active one, and writing the file
where the client cannot reach, plus the history the upload supersedes: list it,
make an older row active again, and delete one -- row and stored file together.

`POST /resume` is the one endpoint that takes a file, and this module is where
that file is judged: the declared media type and the bytes both have to name a
format this app can read, the bytes have to yield text, and the file is written
under `uploads_dir()` with a name this module generates -- the client's
`filename` is recorded as a string and never used as a path. Every refusal
raises a `ResumeRejected` naming what was wrong; the route turns that into the
HTTP answer.

`UPLOADS_DIR` and `MAX_UPLOAD_BYTES` are read here and in no other module, the
way the database URL is read in `app/db.py` only. Unset means the defaults below,
and the default uploads directory is deliberately outside the repository --
`~/.jobradar/uploads` -- because a resume is personal data and this repository is
public. Nothing here creates it at import: the directory appears on the first
upload, and its absence is what `tests/test_app_main.py` checks.

A resume's text is user data: it is stored, and nothing in this module logs it,
puts it in an error message or returns it to a client. The readers below name the
file's media type, never its contents -- `active_resume_text` hands the text to
its caller and to nobody else, and `resume_rows` hands out rows for the routes to
render without ever naming the text column.
"""

import os
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader
from sqlalchemy import select, update

from app import db
from app.models import Resume

UPLOADS_DIR_VARIABLE = "UPLOADS_DIR"
MAX_UPLOAD_BYTES_VARIABLE = "MAX_UPLOAD_BYTES"

# Outside the repository and outside `backend/`, whichever directory the app is
# started from: a resume must never be one `git add` away from a public repo.
DEFAULT_UPLOADS_DIR = Path.home() / ".jobradar" / "uploads"

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

TEXT_PLAIN = "text/plain"
APPLICATION_PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# The three formats this app reads, spelled the one way the column and the
# contract store them: a client's header is canonicalised to one of these.
SUPPORTED_CONTENT_TYPES = (TEXT_PLAIN, APPLICATION_PDF, DOCX)

_EXTENSIONS = {TEXT_PLAIN: ".txt", APPLICATION_PDF: ".pdf", DOCX: ".docx"}

# What the bytes of a PDF and of an OOXML package start with, and the part a
# Word document has and an Excel workbook (`xl/workbook.xml`) does not.
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_WORD_DOCUMENT_PART = "word/document.xml"
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class ResumeRejected(Exception):
    """An upload this app refuses. `status_code` is the answer the endpoint sends."""

    status_code = 422


class UnsupportedResume(ResumeRejected):
    """The declared media type is not readable here, or the bytes contradict it.

    Both halves have to agree: the type decides which reader runs, and the bytes
    have to be that format. A real PDF declared `application/octet-stream` is
    therefore refused too -- the error names the declared type, so the fix is on
    the client.
    """

    status_code = 415


class ResumeTooLarge(ResumeRejected):
    """The upload is bigger than `MAX_UPLOAD_BYTES` allows."""

    status_code = 413


class UnreadableResume(ResumeRejected):
    """The bytes do not parse as the format they were declared to be: corrupt or truncated."""


class ResumeWithoutText(ResumeRejected):
    """The file parses and carries no text -- a blank text file, or a page with no text layer."""


def uploads_dir() -> Path:
    """`UPLOADS_DIR`, or the default outside the repository when it is unset or empty."""
    return Path(os.environ.get(UPLOADS_DIR_VARIABLE, "").strip() or DEFAULT_UPLOADS_DIR)


def max_upload_bytes() -> int:
    """`MAX_UPLOAD_BYTES`, or the default when it is unset or empty.

    Read per request rather than at import, so a deployment changes the limit by
    setting the variable and a test sets a small one instead of sending megabytes.
    """
    configured = os.environ.get(MAX_UPLOAD_BYTES_VARIABLE, "").strip()
    return int(configured) if configured else DEFAULT_MAX_UPLOAD_BYTES


def declared_content_type(declared: str | None) -> str:
    """The media type alone: lower-cased, without the parameters a client may add."""
    return declared.split(";", 1)[0].strip().lower() if declared else ""


def content_type_of(declared: str | None, data: bytes) -> str:
    """The format an upload is, decided by its declared type *and* by its bytes.

    Returns the canonical media type the row stores. A declared type outside the
    three supported ones, and bytes that are not the format they were declared as,
    are both `UnsupportedResume`.
    """
    media_type = declared_content_type(declared)
    if media_type not in SUPPORTED_CONTENT_TYPES:
        raise UnsupportedResume(
            f"Unsupported resume type '{media_type or 'none'}': send a PDF, a DOCX "
            f"or a plain text file"
        )
    if media_type == APPLICATION_PDF and not data.startswith(_PDF_MAGIC):
        raise UnsupportedResume(
            f"Unsupported resume type '{media_type}': the file's bytes are not a PDF"
        )
    if media_type == DOCX and not _is_word_document(data):
        raise UnsupportedResume(
            f"Unsupported resume type '{media_type}': the file's bytes are not a "
            f"Word document"
        )
    return media_type


def extract_text(content_type: str, data: bytes) -> str:
    """The text of an upload, or the refusal naming why there is none.

    Every reader failure is this app's own 422 rather than the reader's
    exception: a truncated PDF and a text file that is not UTF-8 are corrupt
    uploads, not server errors. Blank is a rejection of its own, so a file that
    parses but says nothing is told apart from one that is broken.
    """
    readers = {
        TEXT_PLAIN: _text_file_text,
        APPLICATION_PDF: _pdf_text,
        DOCX: _word_document_text,
    }
    try:
        text = readers[content_type](data)
    except ResumeRejected:
        raise
    except Exception as error:  # the readers' own error family, at a trust boundary
        raise UnreadableResume(
            f"The {content_type} file is corrupt or truncated: it could not be read"
        ) from error
    if not text.strip():
        raise ResumeWithoutText(
            f"No text found in the {content_type} file: it is blank, or carries no text layer"
        )
    return text


def save_upload(data: bytes, content_type: str) -> Path:
    """Write the upload under `uploads_dir()` and return the path it landed on.

    The directory is created here, when the first upload arrives -- never at
    import and never in the app's lifespan. The name is a fresh UUID with the
    format's extension: the client's `filename` is not an argument of this
    function, so no `../`, absolute path, subdirectory or Windows separator in it
    can choose where the file goes.
    """
    directory = uploads_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}{_EXTENSIONS[content_type]}"
    path.write_bytes(data)
    return path


def record_active_resume(filename: str, content_type: str, text: str, stored_path: Path) -> int:
    """Store the uploaded resume as the active one and return the id of its row.

    The previous active row is deactivated in the same transaction as the insert,
    which is what the partial index `uq_resume_one_active` requires: on its own
    the insert is an `IntegrityError`, and a second upload has to supersede the
    first rather than fail. The older rows stay as inactive history; managing
    that history is #25.
    """
    with db.session_scope() as session:
        session.execute(update(Resume).where(Resume.is_active.is_(True)).values(is_active=False))
        resume = Resume(
            filename=filename,
            content_type=content_type,
            text=text,
            stored_path=str(stored_path),
        )
        session.add(resume)
        session.flush()  # the row's id, and the write, exist before the commit
        return resume.id


def active_resume_text() -> str | None:
    """The text of the one active resume, or `None` when no resume is active yet.

    One `session_scope()` per call -- one unit of work, committed and closed before
    this returns -- and one column of the one row the partial index
    `uq_resume_one_active` allows to be active: no other row and no other column is
    read, because a caller that wants the text is not asking for the filename or
    the file's path. The text comes back exactly as it was stored: not truncated,
    not summarised, not escaped. The caller is #34, which reads it once per run,
    before the first board is called.

    A resume's text is user data, so nothing here logs it, puts it in an error or
    returns it to a client: this function is a reader, and the value goes to its
    caller.
    """
    with db.session_scope() as session:
        return session.scalars(
            select(Resume.text).where(Resume.is_active.is_(True))
        ).one_or_none()


def resume_rows() -> list[Resume]:
    """Every stored resume row, newest first, and `[]` when there are none.

    One `session_scope()` per call, and the order is `uploaded_at` descending
    with the id descending under it -- the shape `app.routes.matches` lists with:
    two uploads committed in the same instant still have one order, newest row
    first. The rows are detached before the commit, which would otherwise expire
    every attribute: the caller renders them, and a renderer that reads an
    expired row outside its session gets an error instead of a value.
    """
    with db.session_scope() as session:
        rows = list(
            session.scalars(
                select(Resume).order_by(Resume.uploaded_at.desc(), Resume.id.desc())
            )
        )
        session.expunge_all()
        return rows


def activate_resume(resume_id: int) -> Resume | None:
    """Make that row the active resume and return it, or `None` when no row has that id.

    The row that is active now is deactivated in the same transaction as this
    update, which is what the partial index `uq_resume_one_active` requires: two
    active rows are never visible, not even between the two statements. Exactly
    one row is active afterwards and it is the one the id names. Activating the
    row that is already active rewrites the same state and is not an error, so a
    client may send the same request twice. Nothing else about the row -- its
    text, its file, its `uploaded_at` -- is touched.

    The returned row is detached before the commit expires it, for the reason
    `resume_rows` gives: its renderer reads it outside this session.
    """
    with db.session_scope() as session:
        target = session.get(Resume, resume_id)
        if target is None:
            return None
        session.execute(update(Resume).where(Resume.is_active.is_(True)).values(is_active=False))
        target.is_active = True
        session.flush()  # both writes are in the transaction, and the index's verdict is in
        session.expunge(target)
        return target


def delete_resume(resume_id: int) -> bool:
    """Delete one resume row and the file its `stored_path` names; `False` when no row has it.

    The file goes first and the row second, so nothing is ever half-deleted: an
    `OSError` other than `FileNotFoundError` escapes this function, the session
    rolls back, and the row with its text is left exactly as it was. A retry
    after that file is gone takes `_remove_stored_file`'s missing-file path and
    succeeds. Deleting the active resume is allowed and leaves no active row at
    all, which is the state `pipeline.NO_ACTIVE_RESUME` already covers.
    """
    with db.session_scope() as session:
        resume = session.get(Resume, resume_id)
        if resume is None:
            return False
        _remove_stored_file(resume.stored_path)
        session.delete(resume)
        return True


def _remove_stored_file(stored_path: str) -> None:
    """Unlink the file that path names, when it is inside `uploads_dir()` and still there.

    A file already gone does not block the delete: the row is the record and the
    file is derived from it. A path that does not resolve inside the uploads
    directory is never unlinked -- `save_upload` writes nothing else, so such a
    value is not a file this app put there, and removing it would turn a delete
    into deleting whatever path a row happens to hold. The row still goes.
    """
    path = Path(stored_path).resolve()
    if not path.is_relative_to(uploads_dir().resolve()):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _is_word_document(data: bytes) -> bool:
    """Whether the bytes are a ZIP package holding the document part Word writes."""
    if not data.startswith(_ZIP_MAGIC):
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as package:
            return _WORD_DOCUMENT_PART in package.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _text_file_text(data: bytes) -> str:
    """A text file is UTF-8 or it is corrupt; no other encoding is guessed at."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnreadableResume("The text/plain file is not valid UTF-8") from error


def _pdf_text(data: bytes) -> str:
    """The text layer of a PDF, page by page.

    `extract_text()` returns `None` for a page with no text operators, which the
    blank check in `extract_text` turns into the same rejection either way.
    """
    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _word_document_text(data: bytes) -> str:
    """The paragraphs of a DOCX, read from `word/document.xml` with the standard library.

    A DOCX is an OOXML ZIP and the document part holds every run of text a
    resume has, so no DOCX library is needed for what this app reads. A run and a
    paragraph are joined the way Word lays them out: runs inline, paragraphs on
    their own line.
    """
    with zipfile.ZipFile(BytesIO(data)) as package:
        document = package.read(_WORD_DOCUMENT_PART)
    root = ElementTree.fromstring(document)
    paragraphs = [
        "".join(node.text or "" for node in paragraph.iter(f"{{{_WORD_NAMESPACE}}}t"))
        for paragraph in root.iter(f"{{{_WORD_NAMESPACE}}}p")
    ]
    return "\n".join(paragraphs)
