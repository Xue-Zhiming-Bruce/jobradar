"""`/resume`: the upload, and the history the upload supersedes.

`POST /resume` takes a file: everything the upload has to satisfy lives in
`app.services.resume`, and this module is the HTTP shape around it -- the
multipart parameter, the status codes and the JSON body each answer carries.
Success is `201` and names the new resume; rejections are `415` for a type this
app cannot read, `413` for an upload over the limit and `422` for a file that is
corrupt, carries no text or was not sent as a named file part.

Three more routes read and change what the uploads left behind: `GET /resume`
lists every stored resume newest first, `PATCH /resume/{id}` makes an older one
the active resume and answers with it, and `DELETE /resume/{id}` removes one --
row and stored file together. The row work behind them is `app.services.resume`'s
the way `record_active_resume` and `active_resume_text` already are; what stays
here is the HTTP shape, the `responses=` dicts and one renderer, `ResumeSummary`
with `resume_summary(row)`, shared by the list and the activate answer so the two
cannot drift apart -- the `RunSummary`/`run_summary` pattern of
`app.routes.runs`. `POST`'s own `201` body (`ResumeUploaded`) is unchanged: a
client that wants the whole row reads `GET /resume`.

The body of every answer here is built from the row's metadata -- id, filename,
media type, upload time, active state -- and never from its contents: the resume
text is stored, and it reaches no response and no log line.

One route is the exception, and it is the point of the parse: `POST /resume/parse`
turns the active resume's stored text into its name and the skills it claims,
stores them as JSON text on the row and answers them. The model call itself is
`app.matching.resume_fields`'s -- this module owns the JSON in both directions,
and reaches the parser through one `Depends` seam so no case here needs a key, a
socket or a model. The fields it answers are user data, like the text they were
read from: they belong in that one body, and in no log line.
"""

import json
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from app.matching.resume_fields import LlmResumeParser, ResumeFieldParser, ResumeParseError
from app.matching.scorer import ScorerConfigurationError
from app.models import Resume
from app.services import resume as resume_service

logger = logging.getLogger(__name__)

router = APIRouter()


class ResumeUploaded(BaseModel):
    """Body of a successful upload: which resume is now the active one."""

    id: int
    filename: str
    content_type: str
    is_active: bool


class ResumeSummary(BaseModel):
    """One `resume` row as the history routes render it: five fields, and no text.

    The same body `GET /resume` lists and `PATCH /resume/{id}` answers with, so a
    client reads one shape for a resume wherever it meets one. `text` and
    `stored_path` are deliberately not here: the text is user data and the path
    is where the server put the file, and neither is the client's business.
    `is_active` is the row's own column, not a property of the order: it is true
    for exactly one row, the one a run would score against.
    """

    id: int
    filename: str
    content_type: str
    uploaded_at: datetime
    is_active: bool


class ResumeActivation(BaseModel):
    """Body of an activation: the one value this endpoint accepts, and no other field.

    `is_active` is a `Literal` of `True` -- the `MatchVerdict` precedent -- so
    only "make this one active" is expressible and `{"is_active": false}` is
    refused by this model's own validation as `422`. The model forbids extra
    fields as well: deactivation is not in `_docs/plan.md`, and a body carrying
    anything else is a request this route does not understand rather than a
    write it performs while ignoring what it was sent.
    """

    model_config = ConfigDict(extra="forbid")

    is_active: Literal[True]


class ResumeParsed(BaseModel):
    """Body of a parse: which resume, the name it states, and the skills it claims.

    The two fields are #26's own -- `name` is `None` and `skills` is `[]` for what
    the resume does not state -- and they are answered here because a parse is the
    one place they belong. `ResumeSummary` is deliberately not widened to carry
    them: the history routes answer five public fields and no user data.
    """

    id: int
    name: str | None
    skills: list[str]


def _too_large(size: int, limit: int) -> HTTPException:
    """The `413` for an upload over the limit, naming both sizes so it is fixable."""
    return HTTPException(
        status_code=413,
        detail=f"Resume is {size} bytes: the upload limit is {limit} bytes",
    )


def _rejection(rejected: resume_service.ResumeRejected) -> HTTPException:
    """The answer for a refused upload. Its message names the reason, never the content."""
    return HTTPException(status_code=rejected.status_code, detail=str(rejected))


# The refusals this route answers with, named in the contract: a client reads the
# status codes it has to handle without having to try them out.
REFUSAL_RESPONSES = {
    413: {"description": "The upload is larger than the configured limit"},
    415: {"description": "The declared type, or the bytes, are not a readable resume"},
    422: {"description": "The upload is corrupt, carries no text, or is not a named file part"},
}


@router.post(
    "/resume",
    response_model=ResumeUploaded,
    status_code=201,
    tags=["resume"],
    summary="Upload the active resume",
    responses=REFUSAL_RESPONSES,
)
async def upload_resume(
    file: Annotated[UploadFile, File(description="A PDF, a DOCX or a UTF-8 text file")],
) -> ResumeUploaded:
    """Accept one file, extract its text, and store it as the single active resume."""
    # A part the parser did not read as a file -- missing, or sent without a
    # filename -- is refused by the parameter's own validation, as `422`.
    filename = file.filename or ""
    if not filename.strip():
        raise HTTPException(
            status_code=422,
            detail="The uploaded part has no filename: send the resume as a named file part",
        )

    # shortcut: the limit counts the file's own bytes, so a request carrying a
    # large multipart envelope is read and spooled before this refusal; upgrade
    # if the endpoint ever has to refuse an unbounded body while reading it.
    data = await file.read()
    limit = resume_service.max_upload_bytes()
    if len(data) > limit:
        raise _too_large(len(data), limit)

    try:
        content_type = resume_service.content_type_of(file.content_type, data)
        text = resume_service.extract_text(content_type, data)
    except resume_service.ResumeRejected as rejected:
        raise _rejection(rejected) from rejected

    stored_path = resume_service.save_upload(data, content_type)
    resume_id = resume_service.record_active_resume(filename, content_type, text, stored_path)
    logger.info("Stored resume %s: %s, %s bytes", resume_id, content_type, len(data))
    return ResumeUploaded(
        id=resume_id, filename=filename, content_type=content_type, is_active=True
    )


# The refusals the history routes answer with, named in the contract the way
# `app.routes.watchlist` names its own: a client reads the status codes it has to
# handle without having to try them out.
NOT_FOUND_RESPONSE = {404: {"description": "No resume has that id"}}
UNUSABLE_REQUEST_RESPONSE = {
    422: {"description": "The body, or the path, is not something this route can use"}
}
UPDATE_RESPONSES = NOT_FOUND_RESPONSE | UNUSABLE_REQUEST_RESPONSE
DELETE_RESPONSES = NOT_FOUND_RESPONSE | UNUSABLE_REQUEST_RESPONSE


def resume_summary(row: Resume) -> ResumeSummary:
    """One resume as the history routes render it, read while its values are still there.

    Five fields and no sixth: the row's text, its stored path and its media
    type's parameters stay server-side. Both routes that answer with a resume
    call this, so the list item and the single-row answer are the same shape by
    construction rather than by two models that happen to agree today.
    """
    return ResumeSummary(
        id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        uploaded_at=row.uploaded_at,
        is_active=row.is_active,
    )


@router.get(
    "/resume",
    response_model=list[ResumeSummary],
    tags=["resume"],
    summary="List every stored resume",
)
def list_resumes() -> list[ResumeSummary]:
    """Every `resume` row, newest first, and `[]` when none was ever uploaded.

    No filter and no paging: one person's resumes are few, and every upload is
    history worth showing -- the active one first among them, since it is the
    newest until an older one is activated again. The order and the reading of
    the rows are `app.services.resume.resume_rows`'s.
    """
    return [resume_summary(row) for row in resume_service.resume_rows()]


@router.patch(
    "/resume/{id}",
    response_model=ResumeSummary,
    tags=["resume"],
    summary="Make one stored resume the active one",
    responses=UPDATE_RESPONSES,
)
def activate_resume(id: int, body: ResumeActivation) -> ResumeSummary:
    """Make that resume the active one, and answer with it as the list renders it.

    The id is a path parameter and an `int`: a value that is not one never
    reaches this function and is refused by the path's own validation as `422`.
    A body that says anything but "active" is refused by `ResumeActivation` the
    same way, before any write. An id that finds no row is a `404` and writes
    nothing. The row that was active goes inactive in the same transaction, so
    one row is active afterwards and it is the one the path names; sending the
    request again for the row that is already active is the same answer twice.
    """
    row = resume_service.activate_resume(id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"There is no resume with id {id}")
    return resume_summary(row)


@router.delete(
    "/resume/{id}",
    status_code=204,
    tags=["resume"],
    summary="Delete one stored resume and its file",
    responses=DELETE_RESPONSES,
)
def delete_resume(id: int) -> None:
    """Delete exactly that resume -- its row and the file it names -- and answer `204`.

    The id is a path parameter and an `int`, refused by the path's own validation
    as `422` when it is not one, and an id that finds no row is a `404` that
    writes nothing. Deleting the active resume is allowed: it leaves no active
    row, which is a state the app already handles. Nothing about the resume is
    answered: `204` carries no body, so no part of the row -- the text above all
    -- can ride along on it.
    """
    if not resume_service.delete_resume(id):
        raise HTTPException(status_code=404, detail=f"There is no resume with id {id}")


# The refusals the parse answers with, named in the contract the way the rest of
# this module names its own: a client reads the status codes it has to handle
# without having to try them out. The 503 is the one refusal here that names no
# response body of its own -- its detail is the error's message, which names the
# missing variable and never its value.
PARSE_RESPONSES = {
    404: {"description": "No resume is active, so there is nothing to parse"},
    502: {"description": "The model call timed out, failed, or answered something unusable"},
    503: {"description": "The model key is not configured, so the model cannot be asked"},
}


def get_resume_parser() -> ResumeFieldParser:
    """The one seam between this route and the model: the provider a test replaces.

    Zero arguments, so it carries no configuration, and resolved per request by
    `Depends`, so the suite installs `app.dependency_overrides[get_resume_parser]`
    and then no case needs a key, a socket or a model -- the way
    `app.routes.internal.get_run_starter` is declared and replaced. It returns the
    real parser, with no client: which endpoint, model and timeout a real call
    uses is `app.matching.scorer`'s, read per parse.
    """
    return LlmResumeParser()


@router.post(
    "/resume/parse",
    response_model=ResumeParsed,
    tags=["resume"],
    summary="Parse the active resume into its name and skills",
    responses=PARSE_RESPONSES,
)
def parse_resume(
    parser: Annotated[ResumeFieldParser, Depends(get_resume_parser)],
) -> ResumeParsed:
    """Parse the active resume once, store its fields on its row, and answer them.

    No path parameter and no request body: the resume this parses is the active
    one, which is the row every other route in this family already names. A row
    that already holds its fields is answered from the stored JSON without a
    request, because a parse is a slow, paid call and the stored text never
    changes after the upload; a NULL column is the one thing that makes a call,
    and it is the row's own column, so a resume activated again later parses on
    its own first call.

    The two JSON directions are this module's: `json.dumps` of the object the
    parser answered, and the same object read back. The failures are the
    parser's own types, answered by name: the scorer's configuration error is a
    `503` -- it means no key is set, which is a deployment's problem and not the
    request's -- and a parse that produced no fields is a `502`, the model
    upstream having failed. The log line names the resume's id and the shape of
    what was stored, never a value: the fields are user data, like the text.
    """
    row = resume_service.active_resume()
    if row is None:
        raise HTTPException(
            status_code=404, detail="No active resume: upload one before parsing it"
        )

    if row.parsed_fields is not None:
        stored = json.loads(row.parsed_fields)
        return ResumeParsed(id=row.id, name=stored["name"], skills=stored["skills"])

    try:
        fields = parser.parse(row.text)
    except ScorerConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ResumeParseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    resume_service.record_parsed_fields(
        row.id, json.dumps({"name": fields.name, "skills": fields.skills})
    )
    logger.info(
        "Parsed resume %s: name %s, %s skills",
        row.id,
        "present" if fields.name is not None else "absent",
        len(fields.skills),
    )
    return ResumeParsed(id=row.id, name=fields.name, skills=fields.skills)
