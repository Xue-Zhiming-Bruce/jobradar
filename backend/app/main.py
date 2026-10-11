"""The JobRadar API.

Only the health check exists so far; domain routes arrive in later tasks.
Importing this module builds an app object and nothing else: no configuration,
no database, no files.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.db import init_db

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


@app.get("/healthz", response_model=Health, tags=["health"], summary="Liveness check")
async def healthz() -> Health:
    """Report that the process is up. Reads no configuration and no database."""
    return Health(status="ok")
