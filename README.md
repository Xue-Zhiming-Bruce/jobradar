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

### The integration suite: the whole workflow against PostgreSQL

`backend/tests/integration/` holds one suite that drives the real application end
to end: the real `POST /resume`, the real `POST /watchlist`, the real
`POST /internal/tick` with its own daemon thread and its run row, the real
pipeline, prefilter and digest, and the real `GET /matches` -- all of it against a
real PostgreSQL server. Exactly three things are faked, each at the seam the
running code reads it from: the Greenhouse fetch, the LLM scorer, and the
mailer. So **no API key and no SMTP credential are needed**, no request leaves
the process, and the resume it uploads is synthetic -- written for the tests, in
the request.

Every test there carries the `integration` marker. It is declared in
`backend/pyproject.toml`'s `[tool.pytest.ini_options]` (`markers`), and that is
also where the default run's `addopts = "-m 'not integration'"` lives;
`backend/tests/integration/conftest.py` marks every item it collects. The default
`cd backend && uv run pytest` therefore collects this suite and **deselects** it --
its summary reads `N deselected` -- and the command that runs only it is:

    cd backend && uv run pytest -m integration

A unit test in the default suite (`backend/tests/test_integration_separation.py`)
fails if the directory holds no test module, if the marker is not declared, or if
an item collected there does not carry the marker.

That suite needs a real PostgreSQL server, and it **fails rather than skips** when
`TEST_DATABASE_URL` is unset, empty or unreachable: there is nothing in it to
skip. The server is the same throwaway container, on port 5433, because 5432 is
taken on this machine -- and it is *not* the compose stack, which is not a test
harness:

    docker run --rm -d --name jobradar-test-db -p 5433:5432 \
      -e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway \
      -e POSTGRES_DB=jobradar postgres:17
    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar uv run pytest -m integration
    docker rm -f jobradar-test-db

The suite drops the tables it created, so the database is left as it was found,
and every case starts on empty tables. The `TEST_DATABASE_URL` paragraph above is
the unit suite's rule and stays as it is: there the PostgreSQL parameter skips
without a server, because SQLite covers the same tests.

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

## CI, and the scheduled tick

Two workflows, both in `.github/workflows/`, and each file's leading comment
names the local command that stands in for every job it runs, so the pipeline can
be reproduced without GitHub Actions.

- `ci.yml` runs on every push and every pull request, in four jobs: `backend`
  (`uv sync && uv run pytest`), `frontend` (`npm ci && npm run build && npm test`),
  `integration` (`uv run pytest -m integration` against a `postgres:17` service
  container holding the same throwaway credentials as the local server) and
  `contract` (regenerate `backend/openapi.json`, then fail on a diff). It has no
  deploy step; deploying a green `main` is #45.
- `tick.yml` calls `POST /internal/tick` once a day, taking the secret from the
  repository secret `TICK_SECRET` and the base URL from the repository variable
  `JOBRADAR_URL`. No secret value is in either file. Until #16 sets
  `JOBRADAR_URL`, that job is red on purpose rather than silently green.

The schedule is best-effort: GitHub Actions cron drifts by minutes to hours and
can skip a run entirely, and it is disabled after 60 days of repository
inactivity, so a late or missed run is expected rather than a fault. That is the
trigger decision in `_docs/decisions/2026-10-11-deploy-and-repo.md`, and it is why
the in-app **"Run digest now"** button is the reliable path -- the one the demo
relies on.

## MCP server

`mcp/` is a second uv project, `jobradar_mcp`: a small MCP server that drives the
app this repository already serves, over HTTP, through five bounded tools. It is a
project of its own so the `mcp` dependency never enters `backend/`'s tree or the
deployed image, and `cd backend && uv run pytest` is unaffected. It speaks MCP over
**stdio** — no port, no host and no HTTP server of its own — and it holds no
credential of its own beyond two variables:

| Variable | What it is |
| --- | --- |
| `JOBRADAR_URL` | the app's base URL, e.g. `http://127.0.0.1:8123` |
| `JOBRADAR_TICK_SECRET` | the value sent as `X-Tick-Secret`: the same value the app reads from `TICK_SECRET` |

Nothing else is read: no model key, no database URL, no SMTP variable, no upload
directory and no digest rule. Either variable unset or blank is a tool error naming
that variable — the server still starts and lists its tools, and the tools that do
not need the missing one still answer. The secret's value never reaches a result,
an error message or a log line. The app itself is reached directly: a proxy taken
from the machine's own settings does not stand between the two, which is what
keeps the loopback demo below working on a machine that runs one.

### Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `add_company` | `name: string`, `slug: string` (both required) | the app's `201` body: `id`, `name`, `slug`, `is_active` |
| `list_watchlist` | none | every company, ascending id, active or not: `id`, `name`, `slug`, `is_active` |
| `list_matches` | none | every match row, newest first, unfiltered: `id`, `company_name`, `title`, `url`, `score`, `reason`, `gaps`, `state`, `created_at` |
| `trigger_run` | none | the app's `202` body: `id`, `status`, `started_at`, `finished_at`, `error`, `postings_seen`, `matches_created` |
| `get_run` | `run_id: integer` (required) | that run's row, the same seven fields |

Each tool returns the app's own response body, field for field and name for name:
nothing is filtered, renamed or derived here. `list_matches` has no filter of its
own, so a prefiltered row (`score` 0) and an unscored one come back exactly as
stored — keeping either out of a digest or a screen is the app's business. A list
is rendered as one JSON block per row, because that is how the SDK renders a
returned list; anything else is one JSON block. Every failure a client can see is
a tool error (`isError`) carrying the app's own `detail` text, never a hang, never
a silent success and never a traceback.

Each tool calls one endpoint the app serves today: `POST /watchlist`,
`GET /watchlist`, `GET /matches`, `POST /internal/tick` and `GET /runs/{id}`.
`trigger_run` sends one request and answers the row while the run is still
running — it does not wait for it and does not poll it; `get_run` is the read.

### Run it, and register it

    cd mcp && uv sync
    cd mcp && uv run python -m jobradar_mcp

An MCP client starts that command for you; the entry is the command, its
arguments and the two variables:

```json
{
  "mcpServers": {
    "jobradar": {
      "command": "uv",
      "args": ["--directory", "mcp", "run", "python", "-m", "jobradar_mcp"],
      "env": {
        "JOBRADAR_URL": "http://127.0.0.1:8123",
        "JOBRADAR_TICK_SECRET": "throwaway-local-secret"
      }
    }
  }
}
```

### See one tool call yourself

**Terminal 1** — the app, with a throwaway secret and a throwaway database, on
port 8123 (8000 belongs to compose). `TICK_SECRET` is a literal throwaway, like
the local Postgres password in `compose.yaml`: its only power is over the app on
this machine's port 8123.

    cd backend && TICK_SECRET=throwaway-local-secret \
      DATABASE_URL=sqlite:////tmp/jobradar-mcp-demo.db \
      uv run uvicorn app.main:app --port 8123

**Terminal 2** — one command that opens a stdio session to that server, calls one
tool and prints the result. No Node, no Inspector, no interactive click:

    cd mcp && JOBRADAR_URL=http://127.0.0.1:8123 \
      JOBRADAR_TICK_SECRET=throwaway-local-secret uv run python demo_client.py

`demo_client.py` is that short client: `mcp.client.stdio` starts the server on the
other end of a pipe, `ClientSession.call_tool("trigger_run", {})` makes the one
call, and the body it returns is printed. What a reviewer must see is a run row
with an `id` and `"status": "running"` (the id and the timestamps are the app's
own, so they differ per run):

```
trigger_run -> {
  "id": 1,
  "status": "running",
  "started_at": "2026-10-12T18:09:54.022466Z",
  "finished_at": null,
  "error": null,
  "postings_seen": 0,
  "matches_created": 0
}
```

The server logs the one request it made to stderr, above that block; the answer
itself is the whole of stdout's protocol on that pipe.

At that moment the app is up, with its tables created, and the same row is
readable from the app itself:

    curl -s http://127.0.0.1:8123/healthz      # {"status":"ok"}
    curl -s http://127.0.0.1:8123/runs/1

The demo needs no LLM key, no SMTP, no real `TICK_SECRET`, no real `DATABASE_URL`
and no resume: the configuration table above says what each of those unset means.
The run's later outcome is **not** asserted — with an empty watchlist and no model
key the pipeline may score nothing and end the row `succeeded` or `failed`, so the
row the `curl` above prints at `id` 1 may already read
`"error": "No active resume: upload one before a run can score postings"`. That is
the app's own business, and not this call's.

### MCP tests

    cd mcp && uv sync
    cd mcp && uv run pytest
    cd mcp && uv run pytest -m live

The default run needs no app, no network, no `.env` and neither variable: every
case injects `httpx2.Client(transport=httpx2.MockTransport(...))` the way
`backend/tests/test_greenhouse_source.py` does, and drives the tools by name
through `MCPServer.call_tool` and `MCPServer.list_tools()`, so the SDK's own
argument validation, output conversion and error wrapping are what is exercised.
The one case that drives the real app is excluded by
`addopts = "-m 'not live'"` in `mcp/pyproject.toml`, the way the backend excludes
`integration`: it starts the app as a subprocess on a free port with a temporary
SQLite file under the system temp directory — never `backend/data/jobradar.db` —
and asserts that the row `trigger_run()` returned is the row `get_run(run_id)`
reads back.
