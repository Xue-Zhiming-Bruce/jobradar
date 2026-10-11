"""Write the committed OpenAPI contract from the app object.

    cd backend && uv run python -m app.export_openapi

`backend/openapi.json` is generated here and committed. It is never hand-edited
and never fetched from a running server, and the output is deterministic — no
timestamps, no absolute paths, no hostname, no version read at runtime — so a
fresh clone regenerates the same bytes and `git status` stays clean.
"""

import json
from pathlib import Path

from fastapi import FastAPI

from app.main import app

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def render_contract(fastapi_app: FastAPI) -> str:
    """Return the contract text exactly as it is committed."""
    return json.dumps(fastapi_app.openapi(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    CONTRACT_PATH.write_text(render_contract(app), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
