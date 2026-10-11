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

### Checking the image carries no build junk

The image must hold source only. `ls -a` is not enough to prove that: it lists
the top level only, so a cache inside `app/` stays invisible. This check is
recursive and prints nothing when the image is clean:

    docker build backend/ -t jobradar-backend
    docker run --rm jobradar-backend sh -c 'find /app \
      \( -name "*.pyc" -o -name "__pycache__" -o -name ".venv" \
         -o -name ".pytest_cache" -o -name "*.db" -o -name "*.sqlite3" \
         -o -name "uploads" -o -name "data" \) -print'

Anything it prints leaked through `backend/.dockerignore`. That file is not
`.gitignore`: its patterns are anchored at the build-context root, so a bare
`__pycache__/` excludes the root directory only and lets `app/__pycache__/`
into the image. Every pattern that has to match at any depth needs the `**/`
prefix — `uv run pytest` asserts exactly that.
