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

`#40` gives the mount one behavior of its own: a file under `assets/` is cached
forever and the page is not. Vite names each built file with a hash of its own
contents -- `index-B6OjL1iC.js` -- so a new build never reuses an old name and
the browser may keep the old file without ever asking again. `index.html` names
those hashes and has none itself, so caching it would serve a page pointing at a
build that is no longer there; it is revalidated instead. Without this, the
`cache-control` was absent on both and the browser made a conditional request for
every hashed asset on every load.

`#43` serves the hosted MCP endpoint at `/mcp` (with or without its trailing
slash), from `app.mcp`: the same seven `mcp/` tools over streamable HTTP, on this
origin and behind the bearer guard. Its routes are registered after the API
routers and before the UI mount, and its transport is started in the lifespan
below.

`#90` makes the journey visible: `app.telemetry` is installed here and nowhere
else. The JSON log format goes on at import, before anything can log; the
telemetry middleware goes on after the bearer guard, so it is the outermost of
this app's middleware and every response -- the guard's `401`, a `404`, the UI's
-- carries the trace id back; the two telemetry reads are registered with the
other routers, above the UI mount, so the mount at `/` cannot swallow them; and
the prune of spans past their retention runs once per process start in the
lifespan beside `reap_stale_runs()`.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.routing import Route
from starlette.types import Scope

from app import mcp, telemetry
from app.db import init_db
from app.routes.internal import router as internal_router
from app.routes.matches import router as matches_router
from app.routes.resume import router as resume_router
from app.routes.runs import reap_stale_runs, router as runs_router
from app.routes.watchlist import router as watchlist_router

API_TITLE = "JobRadar API"
API_VERSION = "0.1.0"

# Where the frontend's build lands: the Dockerfile copies `frontend/dist` here,
# one directory below this module, so the running app finds it however it was
# started. `STATIC_DIR` overrides it, read at import -- which is when a mount
# either exists or does not -- and unset or empty means this path.
STATIC_DIR_VARIABLE = "STATIC_DIR"
DEFAULT_STATIC_DIR = Path(__file__).parent / "static"

# A year, which is as long as the name's content hash stays valid.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Revalidate every load: the page is what names the current build's hashes.
PAGE_CACHE_CONTROL = "no-cache"


def static_dir() -> Path:
    """`STATIC_DIR`, or the directory the image copies the build into when unset."""
    return Path(os.environ.get(STATIC_DIR_VARIABLE, "").strip() or DEFAULT_STATIC_DIR)


class BuiltUiFiles(StaticFiles):
    """`StaticFiles`, with the `cache-control` the requested path deserves.

    The path arrives relative to the mount, so `assets/`-prefixed is a built
    asset (`index-B6OjL1iC.js`) and anything else -- `index.html`, serving `/` --
    is the page. Only the assets carry a content hash, so only they may be kept.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = (
            ASSET_CACHE_CONTROL if path.startswith("assets/") else PAGE_CACHE_CONTROL
        )
        return response


class Health(BaseModel):
    """Body of `GET /healthz`."""

    status: str


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create the missing tables, finish stranded runs, prune old spans, start MCP, and serve.

    The reap comes after `init_db()`, because the table it writes has to exist,
    and before the yield, because it is what makes "no `run` row reads `running`"
    true of every request this process will answer: a tick's guard is the
    process's own, so a `running` row found here is a row no process is running.
    The prune of spans past `telemetry.SPAN_RETENTION` is beside it for the same
    two reasons and is the only deleter of spans (`#90`), so the window the two
    telemetry reads answer over is bounded without a scheduled job. The hosted
    MCP endpoint (#43) starts its transport here too, because a
    mounted ASGI app's own lifespan is not entered by Starlette; one reap and one
    prune per process start, and no other startup work.
    """
    init_db()
    reap_stale_runs()
    telemetry.prune_spans()
    async with mcp.endpoint.lifespan():
        yield


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)

# The app's own log format, installed once at process start (#90), before any
# request is served and before anything logs: one JSON object per line, with the
# line's trace id in a field of its own. `uvicorn` configures its loggers before
# it imports this module, so its handlers are re-pointed at the same format here.
telemetry.configure_logging()

# The one guard the hosted MCP endpoint has (#43): the bearer token is read per
# request and an unset one refuses every request, so the endpoint is off unless an
# operator configures it.
app.add_middleware(mcp.BearerAuthMiddleware)

# Added after that guard, so it is the outermost of this app's middleware and is
# handed every response the app produces, the guard's `401` included (#90).
app.add_middleware(telemetry.TelemetryMiddleware)


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
app.include_router(telemetry.router)

# The hosted MCP endpoint (#43): the `mcp/` tools over streamable HTTP, on this
# origin and behind the bearer guard, so one container serves the API, the UI and
# MCP. Registered as routes for the path with and without its trailing slash: a
# `Mount` matches only the slashed form, and the UI's mount at `/` -- registered
# last -- would otherwise swallow the bare path instead of letting Starlette
# redirect it. Both spellings reach the transport at the same path.
for path in (mcp.MCP_PATH, mcp.MCP_PATH + "/"):
    app.routes.append(Route(path, endpoint=mcp.endpoint))

# Last, after every `include_router` above: the API's own paths are matched
# first, and only a path none of them claims reaches the built UI.
if static_dir().is_dir():
    app.mount("/", BuiltUiFiles(directory=static_dir(), html=True), name="ui")
