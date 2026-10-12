"""The JobRadar API.

So far the health check, the resume upload and the watchlist; the rest of the
domain routes arrive in later tasks. Importing this module builds an app object
and nothing else: no configuration, no database, no files.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import init_db
from app.routes.resume import router as resume_router
from app.routes.watchlist import router as watchlist_router

API_TITLE = "JobRadar API"
API_VERSION = "0.1.0"


class Health(BaseModel):
    """Body of `GET /healthz`."""

    status: str


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the missing tables when the app starts, and do nothing else."""
    init_db()
    yield


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    """Answer `422` without echoing the request back into the response body.

    FastAPI's own body carries each error's offending input, which on an upload
    is the resume itself: user data is never printed in an error, here or in a
    log line. The location, the type and the reason stay, so a client can still
    see which field it got wrong and why.
    """
    details = [
        {key: value for key, value in detail.items() if key not in {"input", "ctx"}}
        for detail in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


@app.get("/healthz", response_model=Health, tags=["health"], summary="Liveness check")
async def healthz() -> Health:
    """Report that the process is up. Reads no configuration and no database."""
    return Health(status="ok")


app.include_router(resume_router)
app.include_router(watchlist_router)
