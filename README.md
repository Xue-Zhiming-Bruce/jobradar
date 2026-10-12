# JobRadar

A daily digest of new job postings at companies you actually want to work for,
matched against your resume, with a plain-English reason for each match. See
`_docs/plan.md` for the full plan.

## Run the whole system

From the repository root, on a fresh clone, with no `.env` and no other command:

    docker compose up --build --wait

It exits 0 when both services are healthy; the app is then on
<http://localhost:8000>, the UI at `/` and the API under it. One container
serves both, so there is one origin, no CORS and no second service.

- `api` — the API and the frontend's build in one image, published `8000:8000`.
  Healthcheck: `GET /healthz`, read with the image's own Python.
- `db` — PostgreSQL 17. It publishes **no** host port, so nothing outside the
  compose network reaches it. Healthcheck: `pg_isready`.

`docker compose ps` shows both as `healthy`. The tables are created by the app's
own `init_db()` at startup: no migration step, no seed step and no
`docker compose exec` before the API serves.

### Where the data lives

Two named volumes, never a bind mount, so nothing in the repository and nothing
under `~/.jobradar` is written to:

- `jobradar-postgres` — the database, at `/var/lib/postgresql/data`.
- `jobradar-uploads` — uploaded resumes, at `/data/uploads`.

Both survive `docker compose restart api`, and both still hold after
`docker compose down` followed by `docker compose up -d --wait`: the resume file
and the row naming it are there afterwards, which is why the upload directory is
a volume of its own.

The Postgres password in `compose.yaml` is a **local throwaway**, safe to commit
only because `db` publishes no host port — its only power is over a database on
this machine. Production credentials come from Render and Neon (#16) and never
appear in this file. No resume, key or database file is ever committed; the data
rules are in `_docs/process.md`.

### Configuration

`compose.yaml` passes every variable the app reads. Unset or empty means:

| Variable | Unset means |
| --- | --- |
| `DATABASE_URL` | the local SQLite file `backend/data/jobradar.db`; compose sets its own Postgres URL |
| `UPLOADS_DIR` | `~/.jobradar/uploads`, outside the repository; compose sets `/data/uploads` |
| `MAX_UPLOAD_BYTES` | 10 MiB per upload |
| `TICK_SECRET` | every `POST /internal/tick` is `401`: there is no default secret |
| `LLM_API_KEY` | scoring stops at the first posting; the run's row reads `failed` and names this variable |
| `LLM_MODEL` | `gpt-4o-mini` |
| `SCORER_MAX_POSTINGS` | 50 postings scored per run |
| `PREFILTER_TITLE_TERMS`, `PREFILTER_SENIORITY_TERMS`, `PREFILTER_LOCATION_TERMS`, `PREFILTER_MUST_HAVE_KEYWORDS` | that rule rejects nothing, so a posting goes to the scorer |
| `DIGEST_MIN_SCORE` | 70 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | the digest sends nothing; a digest with a match to send fails, naming the first required variable that is unset |
| `STATIC_DIR` | `backend/app/static`, beside `app/main.py`, where the image copies the frontend's build |

With none of them set, `docker compose up --build --wait` is still green:
`/healthz` answers `{"status":"ok"}`, a tick without `TICK_SECRET` is `401`
rather than `500`, and a tick with a secret but no model key answers `202` while
its run row reads `failed` with the missing variable named.

### The page on `/` is the image's own build

The UI enters the image in an earlier stage of `backend/Dockerfile`, which runs
the frontend's own `npm ci && npm run build` and copies the result into the
runtime stage; `compose.yaml` builds that with the repository root as the build
context. There is no committed `frontend/dist`, no mount and no second container,
so deleting the host build changes nothing:

    rm -rf frontend/dist
    docker compose up --build --wait

The page still appears on <http://localhost:8000/>. `frontend/dist` on the host
belongs to `npm run dev`; the image is where the served build comes from.

One detail comes with serving a UI at `/`: a request that names an API path with
a method that path does not serve (`GET /resume`, say) is answered by the mount
with `404` instead of the API's `405`, because a mount at `/` matches every path
before a partial match can be reported. Every documented answer is unchanged:
the paths in `backend/openapi.json` answer their own methods, and no path returns
HTML.

The image must hold source and the built page and nothing else. `ls -a` is not
enough to prove that — it lists the top level only — so this check is recursive
and prints nothing when the image is clean:

    docker compose build api
    docker run --rm jobradar-api sh -c 'find /app \
      \( -name "*.pyc" -o -name "__pycache__" -o -name ".venv" \
         -o -name ".pytest_cache" -o -name "*.db" -o -name "*.sqlite3" \
         -o -name "uploads" -o -name "data" \) -print'

Anything it prints leaked through `.dockerignore` at the repository root, which
governs this build because the context is the repository. That file is not
`.gitignore`: its patterns are anchored at the context root, so a bare
`__pycache__/` excludes the root directory only and lets `app/__pycache__/` into
the image. Every pattern that has to match at any depth carries the `**/` prefix,
and `uv run pytest` asserts exactly that.

## Backend

### Tests, and the two engines

    cd backend && uv sync
    cd backend && uv run pytest

No container, no Postgres and no API key: SQLite is the default engine and fakes
stand in for the board and the model. The engine matrix's PostgreSQL cases run
only when `TEST_DATABASE_URL` names a server, and skip with that variable's name
when it does not. A throwaway server is how to run them, on a free port -- 5433
here, because 5432 is taken on this machine:

    docker run --rm -d --name jobradar-test-db -p 5433:5432 \
      -e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway \
      -e POSTGRES_DB=jobradar postgres:17
    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar uv run pytest

`compose.yaml` is not a test harness: the container above is the database those
tests run against, and `docker rm -f jobradar-test-db` throws it away.

### Local development, without Docker

    cd backend && uv run uvicorn app.main:app --reload

This path uses SQLite (`backend/data/jobradar.db`) and serves no UI: with no
build at `backend/app/static` no mount is registered, so `GET /` is `404` while
`/healthz` and the API answer as usual.

### Regenerating the OpenAPI contract

`backend/openapi.json` is generated from the FastAPI app object and committed.
Do not edit it by hand: `uv run pytest` compares it with the document the app
generates now and fails when the two differ.

Regenerate it with this one command:

    cd backend && uv run python -m app.export_openapi

The output is deterministic — no timestamps, absolute paths, hostnames or
versions read at runtime — so running the command on a fresh clone rewrites the
file byte for byte identically, and running it twice leaves `git status` clean.

## Frontend

The app in `frontend/` is React and TypeScript, and its tests run against a
mock backend, so they need no API, no key and no network.

    cd frontend && npm install
    cd frontend && npm test
    cd frontend && npm run dev

`npm test` is `vitest run`: it runs the whole suite under `frontend/src/` once
and exits on its own. `npm run dev` serves the app with the mock backend
answering in the browser.
