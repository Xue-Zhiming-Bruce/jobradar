# JobRadar

A daily digest of new job postings at companies you actually want to work for,
matched against your resume, with a plain-English reason for each match. See
`_docs/plan.md` for the full plan.

## Backend

### Regenerating the OpenAPI contract

`backend/openapi.json` is generated from the FastAPI app object and committed.
Do not edit it by hand: `uv run pytest` compares it with the document the app
generates now and fails when the two differ.

Regenerate it with this one command:

    cd backend && uv run python -m app.export_openapi

The output is deterministic — no timestamps, absolute paths, hostnames or
versions read at runtime — so running the command on a fresh clone rewrites the
file byte for byte identically, and running it twice leaves `git status` clean.
