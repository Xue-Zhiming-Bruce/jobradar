"""`POST /resume`: the one endpoint that accepts a file.

Everything the upload has to satisfy lives in `app.services.resume`; this module
is the HTTP shape around it -- the multipart parameter, the status codes and the
JSON body each answer carries. Success is `201` and names the new resume;
rejections are `415` for a type this app cannot read, `413` for an upload over
the limit and `422` for a file that is corrupt, carries no text or was not sent
as a named file part.

The body of every answer here is built from the upload's metadata -- id, media
type, size -- and never from its contents: the resume text is stored, and it
reaches no response and no log line.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.services import resume as resume_service

logger = logging.getLogger(__name__)

router = APIRouter()


class ResumeUploaded(BaseModel):
    """Body of a successful upload: which resume is now the active one."""

    id: int
    filename: str
    content_type: str
    is_active: bool


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
