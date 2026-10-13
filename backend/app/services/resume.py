"""Reading a resume out of an upload, reading back the active one, and writing the file
where the client cannot reach, plus the history the upload supersedes: list it,
make an older row active again, and delete one -- row and stored file together.
`resume_row` is the reader `GET /resume/{id}/file` asks for the one row it serves,
and `stored_file` is the containment rule both that route and the delete turn a
row's `stored_path` into a file with.

`POST /resume` is the one endpoint that takes a file, and this module is where
that file is judged: the declared media type and the bytes both have to name a
format this app can read, the bytes have to yield text, and the file is written
under `uploads_dir()` with a name this module generates -- the client's
`filename` is recorded as a string and never used as a path. Every refusal
raises a `ResumeRejected` naming what was wrong; the route turns that into the
HTTP answer.

A scan is one of those formats: a PNG, a JPEG or an image-only PDF carries no
text layer, so `extract_text` refuses it as blank like any other file without
text, and `OCR_CONTENT_TYPES` is what tells the route which of those refusals is
a scan worth reading by a model. `scan_images` is the other half -- the page
images of that upload, bounded and normalised -- and it is as model-free and
key-free as the rest of this module.

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
render without ever naming the text column. The fields `POST /resume/parse`
stores are that same kind of value: `active_resume` is a reader for the route
that parses them, and `record_parsed_fields` is the one writer of the column they
land in.
"""

import os
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select, update
from sqlalchemy.orm import load_only

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
IMAGE_PNG = "image/png"
IMAGE_JPEG = "image/jpeg"

# The formats this app reads, spelled the one way the column and the contract
# store them: a client's header is canonicalised to one of these.
SUPPORTED_CONTENT_TYPES = (TEXT_PLAIN, APPLICATION_PDF, DOCX, IMAGE_PNG, IMAGE_JPEG)

# The formats a scan is written in: the two images, and the PDF a scanner fills
# with them. The route asks a model to read a file of one of these when its own
# readers found no text, and only then: a blank text file or DOCX is no scan, so
# its refusal stays a refusal.
OCR_CONTENT_TYPES = (APPLICATION_PDF, IMAGE_PNG, IMAGE_JPEG)

_EXTENSIONS = {
    TEXT_PLAIN: ".txt",
    APPLICATION_PDF: ".pdf",
    DOCX: ".docx",
    IMAGE_PNG: ".png",
    IMAGE_JPEG: ".jpg",
}

# What the bytes of a PDF, an OOXML package or one of the two scan images start
# with, and the part a Word document has and an Excel workbook
# (`xl/workbook.xml`) does not.
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# At most this many page images reach one request, and none of them has a long
# edge over this many pixels: the cap bounds the payload a scan can make the way
# the upload limit bounds its bytes. Reading a longer scan in full is its own
# decision, and both are module constants rather than settings because nothing
# about a deployment changes either number.
OCR_MAX_IMAGES = 5
OCR_MAX_IMAGE_EDGE = 1600
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
            f"Unsupported resume type '{media_type or 'none'}': send a PDF, a DOCX, a "
            f"PNG, a JPEG or a plain text file"
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
    if media_type == IMAGE_PNG and not data.startswith(_PNG_MAGIC):
        raise UnsupportedResume(
            f"Unsupported resume type '{media_type}': the file's bytes are not a PNG image"
        )
    if media_type == IMAGE_JPEG and not data.startswith(_JPEG_MAGIC):
        raise UnsupportedResume(
            f"Unsupported resume type '{media_type}': the file's bytes are not a JPEG image"
        )
    return media_type


def extract_text(content_type: str, data: bytes) -> str:
    """The text of an upload, or the refusal naming why there is none.

    Every reader failure is this app's own 422 rather than the reader's
    exception: a truncated PDF and a text file that is not UTF-8 are corrupt
    uploads, not server errors. Blank is a rejection of its own, so a file that
    parses but says nothing is told apart from one that is broken -- and an image
    is decided here, at once: it has no text layer to read, and the bytes still
    have to be an image, so a truncated PNG or JPEG is the corrupt upload this
    reader refuses rather than something a model is asked about.
    """
    readers = {
        TEXT_PLAIN: _text_file_text,
        APPLICATION_PDF: _pdf_text,
        DOCX: _word_document_text,
        IMAGE_PNG: _image_text,
        IMAGE_JPEG: _image_text,
    }
    try:
        text = readers[content_type](data)
    except ResumeRejected:
        raise
    except Exception as error:  # the readers' own error family, at a trust boundary
        raise UnreadableResume(
            f"The {content_type} file is corrupt or truncated: it could not be read"
        ) from error
    return require_text(content_type, text)


def require_text(content_type: str, text: str) -> str:
    """The text that was read, or the refusal naming why there is none.

    The blank rule, in one place: `extract_text`'s tail is this call, so a file
    that parses but says nothing is refused with the same `422` and the same
    message whether its own reader answered it or a model read it from a scan.
    """
    if not text.strip():
        raise ResumeWithoutText(
            f"No text found in the {content_type} file: it is blank, or carries no text layer"
        )
    return text


def scan_images(content_type: str, data: bytes) -> list[bytes]:
    """The page images of a scan, each re-encoded as a PNG with a long edge of at most 1600 px.

    A PNG or a JPEG upload is its own one page image; a PDF contributes the images
    its pages carry, in page order, which is what a scan is. Only the first
    `OCR_MAX_IMAGES` are read, so no upload can make an unbounded request, and the
    format is normalised here so nothing downstream depends on how the scanner
    stored the stream. A PDF whose pages hold no readable image is `[]` -- there
    is no image to send and no request to make.

    The bytes are user data and this function neither logs them nor returns them
    to a client: they are request payloads and nothing else. What will not load as
    an image at all is `UnreadableResume`, the `422` a truncated PDF already gets:
    a corrupt upload and not a model failure.
    """
    try:
        if content_type in (IMAGE_PNG, IMAGE_JPEG):
            return [_png_page(_decoded_image(data))]
        if content_type == APPLICATION_PDF:
            return _pdf_page_images(data)
        return []
    except ResumeRejected:
        raise
    except Exception as error:  # Pillow's and pypdf's own error family, at a trust boundary
        raise UnreadableResume(
            f"The {content_type} file is corrupt or truncated: it could not be read"
        ) from error


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


def active_resume() -> Resume | None:
    """The one active resume row, text and parsed fields included, or `None` when none is active.

    One `session_scope()` per call -- one unit of work, committed and closed before
    this returns -- and one row: the partial index `uq_resume_one_active` allows one
    active row, and no other row and no other column is read. The row is detached
    before the commit expires it, the way `resume_rows` detaches its own, because
    `POST /resume/parse` reads its text outside this session.

    The row holds user data, so nothing here logs it, puts it in an error or
    returns it to a client: this function is a reader, and the row goes to its
    caller.
    """
    with db.session_scope() as session:
        row = session.scalars(select(Resume).where(Resume.is_active.is_(True))).one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def record_parsed_fields(resume_id: int, parsed: str) -> None:
    """Store the parsed fields' JSON text on the row that id names.

    One `session_scope()` per call, one unit of work, committed before this
    returns. Only `parsed_fields` is written: the row's text, its file and its
    active flag are as they were. A row that is no longer there -- deleted while
    the model was answering -- is nothing to write and not a failure, the way #9's
    end-of-run write treats a row that is gone.

    The value is user data, like the text beside it: nothing here logs it or
    returns it to a client.
    """
    with db.session_scope() as session:
        row = session.get(Resume, resume_id)
        if row is None:
            return
        row.parsed_fields = parsed


def resume_row(resume_id: int) -> Resume | None:
    """The row that id names, detached before the commit expires it, or `None` when none has it.

    One `session_scope()` per call -- one unit of work, committed and closed
    before this returns -- and one row: `GET /resume/{id}/file` reads the three
    values its answer is built from, the filename and media type its headers
    carry and the path whose file is served. Only those columns are loaded: a
    resume's text is user data, and a read that does not need it must not select
    it (SQLAlchemy logs a statement's rows and parameters at DEBUG). The row is
    detached before the commit expires it, the way `resume_rows` detaches its own
    and for the same reason: the answer is built after this session is closed.
    """
    with db.session_scope() as session:
        row = session.get(
            Resume,
            resume_id,
            options=[
                load_only(Resume.id, Resume.filename, Resume.content_type, Resume.stored_path)
            ],
        )
        if row is not None:
            session.expunge(row)
        return row


def resume_rows() -> list[Resume]:
    """Every stored resume row, newest first, and `[]` when there are none.

    One `session_scope()` per call, and the order is `uploaded_at` descending
    with the id descending under it -- the shape `app.routes.matches` lists with:
    two uploads committed in the same instant still have one order, newest row
    first. Only the five columns the list renders are loaded -- a resume's text
    is user data and this read does not need it -- and the rows are detached
    before the commit, which would otherwise expire every attribute: the caller
    renders them, and a renderer that reads an expired row outside its session
    gets an error instead of a value.
    """
    with db.session_scope() as session:
        rows = list(
            session.scalars(
                select(Resume)
                .options(
                    load_only(
                        Resume.id,
                        Resume.filename,
                        Resume.content_type,
                        Resume.uploaded_at,
                        Resume.is_active,
                    )
                )
                .order_by(Resume.uploaded_at.desc(), Resume.id.desc())
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
        target = session.get(
            Resume,
            resume_id,
            options=[
                load_only(
                    Resume.id,
                    Resume.filename,
                    Resume.content_type,
                    Resume.uploaded_at,
                    Resume.is_active,
                )
            ],
        )
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
        resume = session.get(
            Resume, resume_id, options=[load_only(Resume.id, Resume.stored_path)]
        )
        if resume is None:
            return False
        _remove_stored_file(resume.stored_path)
        session.delete(resume)
        return True


def stored_file(stored_path: str) -> Path | None:
    """The file that path names, resolved, when it is inside `uploads_dir()`; `None` when it is not.

    The one place a row's `stored_path` becomes a file, for the two routes that
    do it: `DELETE /resume/{id}` unlinks it and `GET /resume/{id}/file` serves
    it, and neither may touch a path `save_upload` did not write. A path that
    resolves outside the uploads directory is therefore `None` here, and the
    caller neither opens nor removes it -- so a row whose path was tampered with
    is a refusal and never a read of, or a delete of, whatever it points at.
    Resolution comes first, so a symbolic link inside the directory that leads
    out of it is refused the same way the delete has always refused one.
    """
    path = Path(stored_path).resolve()
    return path if path.is_relative_to(uploads_dir().resolve()) else None


def _remove_stored_file(stored_path: str) -> None:
    """Unlink the file that path names, when `stored_file` says it is one of ours and it is there.

    A file already gone does not block the delete: the row is the record and the
    file is derived from it. A path `stored_file` refuses is never unlinked at
    all. The row still goes.
    """
    path = stored_file(stored_path)
    if path is None:
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


def _image_text(data: bytes) -> str:
    """A scan reads as blank: an image carries no text layer, so the reader answers nothing.

    It is still decoded here, so the bytes have to be an image before any model
    is reached: `extract_text` turns a truncated PNG or JPEG into the same 422 a
    truncated PDF gets. The scan is decoded once more where its request is built,
    which costs milliseconds against the seconds the model call takes.
    """
    _decoded_image(data)
    return ""


def _decoded_image(data: bytes) -> Image.Image:
    """One image, fully decoded: a truncated PNG or JPEG fails here rather than upstream."""
    image = Image.open(BytesIO(data))
    image.load()
    return image


def _pdf_page_images(data: bytes) -> list[bytes]:
    """A PDF's page images, in page order, at most `OCR_MAX_IMAGES` of them.

    The pages past the cap are not decoded at all: nothing reads them, and a
    scan's later pages would otherwise be work nobody asked for.
    """
    images: list[bytes] = []
    for page in PdfReader(BytesIO(data)).pages:
        for found in page.images:
            images.append(_png_page(found.image))
            if len(images) == OCR_MAX_IMAGES:
                return images
    return images


def _png_page(image: Image.Image) -> bytes:
    """One page image as a PNG, its long edge at most `OCR_MAX_IMAGE_EDGE` px.

    One format reaches the model whatever the scanner stored: a CMYK JPEG, a
    palette PNG and a one-bit fax are all RGB, greyscale or converted to it, and
    a large page is scaled down rather than sent whole.
    """
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    image.thumbnail((OCR_MAX_IMAGE_EDGE, OCR_MAX_IMAGE_EDGE))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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
