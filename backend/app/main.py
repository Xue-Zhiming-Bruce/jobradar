"""The JobRadar API.

Only the health check exists so far; domain routes arrive in later tasks.
Importing this module builds an app object and nothing else: no configuration,
no database, no files.
"""

from fastapi import FastAPI
from pydantic import BaseModel

API_TITLE = "JobRadar API"
API_VERSION = "0.1.0"


class Health(BaseModel):
    """Body of `GET /healthz`."""

    status: str


app = FastAPI(title=API_TITLE, version=API_VERSION)


@app.get("/healthz", response_model=Health, tags=["health"], summary="Liveness check")
async def healthz() -> Health:
    """Report that the process is up. Reads no configuration and no database."""
    return Health(status="ok")
