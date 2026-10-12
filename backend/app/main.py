"""The JobRadar API.

So far the health check, the resume upload, the watchlist, the tick that starts a
digest run, the read of that run and the matches it stored; the rest of the
domain routes arrive in later tasks. Importing this module builds an app object
and nothing else: no database, no files, and the one variable below, which only
decides whether a built UI is mounted.

`#13` mounts the frontend's build at `/`, from `DEFAULT_STATIC_DIR` -- the
`static` directory beside this module, where the Dockerfile copies it, resolved
from this file's location rather than from the process's working directory, the
way `score_prompt.txt` is resolved. The mount is registered after every
`include_router` call, because a mount at `/` matches every path: registered
first it would answer `/healthz` and the API with 404s. When nothing is built
(import, the suite, `uv run uvicorn` on a fresh clone) no mount is registered
and the app is exactly the API it was.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.db import init_db
from app.routes.internal import router as internal_router
from app.routes.matches import router as matches_router
from app.routes.resume import router as resume_router
from app.routes.runs import router as runs_router
from app.routes.watchlist import router as watchlist_router

API_TITLE = "JobRadar API"
API_VERSION = "0.1.0"

# Where the frontend's build lands: the Dockerfile copies `frontend/dist` here,
# one directory below this module, so the running app finds it however it was
# started. `STATIC_DIR` overrides it, read at import -- which is when a mount
# either exists or does not -- and unset or empty means this path.
STATIC_DIR_VARIABLE = "STATIC_DIR"
DEFAULT_STATIC_DIR = Path(__file__).parent / "static"


def static_dir() -> Path:
    """`STATIC_DIR`, or the directory the image copies the build into when unset."""
    return Path(os.environ.get(STATIC_DIR_VARIABLE, "").strip() or DEFAULT_STATIC_DIR)


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
app.include_router(internal_router)
app.include_router(runs_router)
app.include_router(matches_router)

# Last, after every `include_router` above: the API's own paths are matched
# first, and only a path none of them claims reaches the built UI.
if static_dir().is_dir():
    app.mount("/", StaticFiles(directory=static_dir(), html=True), name="ui")
