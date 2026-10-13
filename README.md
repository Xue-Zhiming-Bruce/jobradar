# JobRadar

**The problem.** Finding the openings worth applying to means either checking
many company career pages by hand, or drowning in a job-board firehose where most
postings are irrelevant. Neither tells you *why* a role fits, so the same postings
get re-read.

**The user.** One person: the owner. No accounts, no login, no multi-tenancy.

**What it does.** A daily digest of new postings at companies on a watchlist,
matched against the uploaded resume, with a plain-English reason and the gaps per
match. A company is added by name and Greenhouse slug; once a day -- or whenever
the owner presses the in-app **"Run digest now"** button -- the app fetches each
company's public job board, normalises the postings, and scores the new ones
against the resume: a score 0-100, why it fits and what is missing. New matches
above the cutoff are grouped into one email; with nothing above it, no email is
sent and the run says so. The UI lists the watchlist and the matches with their
score, reasoning and gaps, and a match can be marked saved or dismissed.

The full plan -- the behaviour spec, the non-goals and the constraints -- is
`_docs/plan.md`.

## Architecture

Six pieces. The rest of this file is the detail behind them.

1. **One container and no second service.** `backend/Dockerfile` builds a single
   image holding the API and the frontend's build; `compose.yaml` runs that image
   and the Postgres it writes to, and nothing else. There is no worker process to
   deploy, start or supervise, because the poller is an endpoint (3).
2. **The frontend's build is served by the backend, as static files.** The
   Dockerfile's first stage runs the frontend's own `npm ci && npm run build` and
   copies the result into the runtime image at `app/static`; `app/main.py` mounts
   that directory at `/`, after every API router, so `/` is the UI and the API
   keeps its own paths. One origin, one port, one URL: no CORS and no proxy. When
   no build is present -- a clone, the suite, `uv run uvicorn` without the image
   -- no mount is registered and the app is the API alone.
3. **The poller is `POST /internal/tick`, an endpoint rather than a separate
   worker.** It carries the shared secret in `X-Tick-Secret`, compared against
   `TICK_SECRET`; with that variable unset every tick is `401`, and a run already
   in progress is `409`. A process-local lock is the whole concurrency rule,
   because the poller lives in the one process the one container runs.
4. **SQLAlchemy over SQLite locally and Postgres in production.** The models in
   `backend/app/models.py` are the only schema and `backend/app/db.py` builds the
   engine from `DATABASE_URL`, where unset means the local SQLite file
   `backend/data/jobradar.db`. The same models, constraints and tests run on both
   engines, so the two places they genuinely differ -- the SQLite pragmas and the
   partial unique index over active resumes -- carry a clause per dialect rather
   than a branch per caller. `compose.yaml` points the container at its own
   Postgres; production points the same variable at Neon.
5. **The `MatchScorer` seam.** `backend/app/matching/prefilter.py` decides first,
   with no network and no model: title, seniority, location and must-have terms
   read from the `PREFILTER_*` variables, and a posting it rejects is stored and
   never asked about. Survivors go through `MatchScorer.score(posting,
   resume_text)` -- one method, answering `MatchResult(score, reason, gaps)` --
   which `LlmScorer` implements as one model call per posting, bounded by
   `SCORER_MAX_POSTINGS`. A call the model never answered comes back as a
   not-scored result instead of an exception, so it reaches the run row rather
   than a traceback. The tests substitute a `FakeScorer` through
   `app.dependency_overrides`, so the suite needs no API key and no network.
6. **The run row.** `POST /internal/tick` writes a `run` row in `running` and
   answers `202` at once; the work continues off the request thread, writing
   `postings_seen` and `matches_created` as it earns them, and its end writes
   `succeeded` or `failed` with the failure's own message. The UI reads that row
   through `GET /runs/{id}` for progress. The reason for the shape is a
   constraint, not a preference: a tick that scores many postings must not
   outlive an HTTP timeout (`_docs/decisions/2026-10-11-deploy-and-repo.md`).

## Technologies

What each piece is, where it sits, and the seam it talks through.

- **FastAPI** -- the API, and the server of the frontend's build. One app object
  in `backend/app/main.py`, one router module per resource in
  `backend/app/routes/`, `uvicorn` as the server. The app object carries the
  startup that creates the tables, the validation handler that keeps an upload's
  contents out of an error body, and the UI mount, so the static files are served
  by the same server as the API rather than by a second one.
- **React + TypeScript + Vite** -- the UI, in `frontend/`. Screens in
  `frontend/src/screens/`, and every backend call funnelled through one module,
  `frontend/src/api/client.ts`; a test
  (`frontend/src/api/noStrayFetch.test.ts`) fails if any other file under `src/`
  reaches the network, which is what keeps the one-origin design honest. Vite
  builds it (`npm run build`), and the tests run under `vitest` against the mock
  backend in `frontend/src/mocks/`, so they need no API and no key.
- **SQLAlchemy over SQLite and Postgres** -- storage, the same models on both
  engines. `backend/app/models.py` is the schema, `backend/app/db.py` turns
  `DATABASE_URL` into an engine and hands out the session scope every writer
  uses, and the suite's engine fixture is parametrised over both engines: SQLite
  needs nothing, and the Postgres parameter runs the same tests whenever
  `TEST_DATABASE_URL` names a server -- the throwaway one below is that server.
- **Docker + `compose.yaml`** -- packaging and the local whole-system path. One
  image, built from the repository root because the build needs both halves of
  the repository, and one compose file that brings the image up with its database
  and its named volumes, so `docker compose up --build --wait` from the root is
  the whole of the local system.
- **GitHub Actions** -- CI, in `.github/workflows/`. `ci.yml` runs four jobs on
  every push and pull request -- `backend`, `frontend`, `integration` and
  `contract` -- and each file's leading comment names the local command that
  stands in for every job it runs, so the pipeline is reproducible off GitHub.
  `tick.yml` is the scheduled convenience: one `POST /internal/tick` a day,
  deliberately red until #16 supplies its two inputs.
- **The committed contract, `backend/openapi.json`** -- what the API answers,
  generated from the FastAPI app rather than written by hand. `uv run pytest`
  compares the file with the document the app generates now and fails when the
  two differ, and `ci.yml`'s `contract` job regenerates it and fails on a diff,
  so a route change that was not exported is caught rather than merged.
- **The MCP server in `mcp/`** -- the app reachable by an MCP client. A separate
  uv project, `jobradar-mcp`, that drives this app over HTTP through five bounded
  tools, in a process of its own, over stdio. Separate on purpose: the `mcp`
  dependency never enters `backend/`'s tree and never enters the deployed image.

## Run the whole system

From the repository root, on a fresh clone, with no `.env` and no other command:

    docker compose up --build --wait

It exits 0 when both services are healthy; the app is then on
<http://localhost:8000>, the UI at `/` and the API under it. One container
serves both, so there is one origin, no CORS and no second service.

- `api` — the API and the frontend's build in one image, published `8000:8000`.
  It runs as a non-root user of its own (`USER jobradar`, uid 10001), so a request
  that could write a file is not a request that can rewrite the image. Every start
  goes through `docker-entrypoint.sh`: `alembic upgrade head` first, and uvicorn
  only once that succeeded — a failed upgrade exits the container non-zero rather
  than leave the API serving against a schema it does not match. Healthcheck:
  `GET /healthz`, read with the image's own Python, **and** `alembic current`
  reporting the revision this image carries.
- `db` — PostgreSQL 17. It publishes **no** host port, so nothing outside the
  compose network reaches it. Healthcheck: `pg_isready`.

`docker compose ps` shows both as `healthy`, and the container names the revision
it applied:

    docker compose exec api alembic current   # 0007_company_board_validator (head)

There is no migration step to run by hand. The image ships `alembic.ini` and
`migrations/`, and the container applies them at startup against the same
`DATABASE_URL` the app uses — no second connection setting and no environment
variable of its own. A database that does not exist yet is built by the baseline
revision, so nothing has to happen before the API serves: no seed step and no
`docker compose exec`. A database that already holds rows is adopted where it
stands — the baseline revision creates a table only when it is missing, later
revisions add what the models gained, and every row stays as it was — so a second
`docker compose up` sends no `CREATE` or `DROP` and changes no row. A change to
the models ships as a revision, see **Changing the schema** under Backend.

### Where the data lives

Two named volumes, never a bind mount, so nothing in the repository and nothing
under `~/.jobradar` is written to:

- `jobradar-postgres` — the database, at `/var/lib/postgresql/data`.
- `jobradar-uploads` — uploaded resumes, at `/data/uploads`.

Both survive `docker compose restart api`, and both still hold after
`docker compose down` followed by `docker compose up -d --wait`: the resume file
and the row naming it are there afterwards, which is why the upload directory is
a volume of its own.

The uploads volume has one owner, and it is the container's user: the image owns
`/data/uploads` as uid 10001 and Docker gives a **fresh** named volume the owner
of the image directory it covers. A volume created by an image from before the
non-root user is root-owned, so the app cannot write to it — `docker compose down
-v` once (or `docker volume rm jobradar-uploads`) recreates it as uid 10001's and
nothing else about the volume changes.

The Postgres password in `compose.yaml` is a **local throwaway**, safe to commit
only because `db` publishes no host port — its only power is over a database on
this machine. Production credentials come from Render and Neon (#16) and never
appear in this file. No resume, key or database file is ever committed; the data
rules are in `_docs/process.md`.

**Data rules.** A resume is uploaded to a running instance at runtime — never as
part of the repository — and is stored outside it, in `UPLOADS_DIR` (a volume
under compose, `~/.jobradar/uploads` otherwise), so it survives a redeploy. Its
text is user data: it is never logged, never returned to a client and never
committed, and neither is the database file. Secrets live in `.env`, which is
gitignored, and in a deployment they arrive as the platform's environment
variables (Render) and GitHub repository secrets. The names are the
Configuration table below; no value ever appears in this file.

The rule is enforced, not remembered: `backend/tests/test_committed_data_guard.py`
runs in the default suite — so `uv run pytest` fails locally on it — and as one
step of `ci.yml`'s `backend` job on every push and pull request. It fails on a
tracked `.env` file, a `*.db` or `*.sqlite3` file, any path under `uploads/` or
`data/`, a resume file (`.pdf`, `.docx`, `.doc`, `.rtf`, `.odt`), and a credential
shape: a PEM private key, an AWS access key id, an `sk-`-prefixed API key, a
GitHub token, and a PostgreSQL URL carrying a password — plus a non-empty value
assigned to a `*_API_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN` or
`*_ACCESS_KEY_ID` name. It reads tracked content only, from the git index rather
than the working tree, so a gitignored local `.env`, the local database and
`~/.jobradar/uploads` are not violations; the seven throwaway values this file
and `compose.yaml` publish are allowed as exact literals; and it names the file
and the kind without ever printing the matched text.

### Configuration

`.env.example` at the repository root lists every variable in the table below,
one line each with its default and what an unset value does; `.env` --
gitignored -- is where the real values go.

`compose.yaml` passes every variable the app reads. Unset or empty means:

| Variable | Unset means |
| --- | --- |
| `DATABASE_URL` | the local SQLite file `backend/data/jobradar.db`; compose sets its own Postgres URL |
| `UPLOADS_DIR` | `~/.jobradar/uploads`, outside the repository; compose sets `/data/uploads` |
| `MAX_UPLOAD_BYTES` | 10 MiB per upload |
| `TICK_SECRET` | every `POST /internal/tick` is `401`: there is no default secret |
| `LLM_API_KEY` | scoring stops at the first posting; the run's row reads `failed` and names this variable |
| `LLM_MODEL` | `gpt-4o-mini` |
| `LLM_BASE_URL` | `https://api.openai.com/v1`; every model call posts to `<base>/chat/completions` |
| `LLM_FALLBACK_MODEL` | no fallback: only `LLM_MODEL` is tried |
| `SCORER_MAX_POSTINGS` | 50 postings scored per run |
| `PREFILTER_TITLE_TERMS`, `PREFILTER_SENIORITY_TERMS`, `PREFILTER_LOCATION_TERMS`, `PREFILTER_MUST_HAVE_KEYWORDS` | that rule rejects nothing, so a posting goes to the scorer |
| `PREFILTER_MUST_HAVE_FROM_RESUME` | off: the resume's parsed skills gate no posting, so the must-have rule reads `PREFILTER_MUST_HAVE_KEYWORDS` alone |
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
context. Node exists in that stage and in no other: the runtime stage is
`python:3.12-slim` plus `uv` and the app, and `docker compose exec api
sh -c 'command -v node'` prints nothing. Neither does that stage's size grow —
the image reports the same megabytes on `docker image ls` as the one built before
the non-root user, which costs a few kilobytes and nothing else. There is no
committed `frontend/dist`, no mount and no second container, so deleting the host
build changes nothing:

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

Another detail is a path in neither the contract nor the build: `/no/such/path`
is the mount's own `404` — `{"detail":"Not Found"}`, the same shape an unknown
API path gets — and not an unhandled error or the page. There is no client-side
router in the UI, so a deep link has no `index.html` to fall back to; the mount
answers `index.html` only for paths that name a file it holds (`/` among them),
and `backend/tests/test_static_ui.py` asserts that, so adding a router later is a
red test rather than a surprise in a browser.

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

### Caching the assets

The mount sets `cache-control` from the path, because Vite's build is what makes
one answer correct for each. Every file under `assets/` carries a hash of its own
contents in its name (`index-B6OjL1iC.js`), so a new build never reuses an old
name: those are served `public, max-age=31536000, immutable` and the browser
keeps them for a year without asking. `index.html` names the current build's
hashes and has none of its own, so it is served `no-cache` — stored, but
revalidated on every load — because a cached page pointing at hashes the new
build does not have is a broken page:

    curl -si http://localhost:8000/ | grep -i cache-control
    curl -si http://localhost:8000/assets/<hashed file> | grep -i cache-control

No other path is affected: a path the API answers carries no `cache-control`,
mount or not, and the mount never answers one of those.

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
tests run against, and `docker rm -f jobradar-test-db` throws it away. The engine
matrix asserts that this server holds exactly the five tables the models declare,
so pointing `alembic` at it by hand — `DATABASE_URL=<that URL> uv run alembic
upgrade head` — leaves alembic's own `alembic_version` table behind and the next
`uv run pytest` fails on that assertion until it is dropped:

    docker exec jobradar-test-db psql -U jobradar -d jobradar -c "drop table alembic_version"

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

### Measuring a tick, and what one costs

One command measures a whole tick -- watchlist, fetch, ingest, prefilter, score,
record, digest -- against a real PostgreSQL over a synthetic watchlist, with the
board and the model faked at the seams the app already has. It prints the scale it
used, the wall time of the run, the run's own `postings_seen`, `matches_created`
and `matches_emailed`, and the `posting` and `match` rows it wrote:

    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar uv run python benchmarks/tick_benchmark.py --companies 10 --postings 50

It needs the throwaway Postgres above -- `--help` documents `--companies` and
`--postings` and their defaults, and names the `docker run` line -- and it
**fails rather than skips** without it: timings on SQLite say nothing about the
engine the app deploys on. It is a script and not a test, so `uv run pytest` does
not collect it, and it writes its rows in the database it was given only,
emptying them before and after every run, so the repository stays clean. The
numbers it produced, and what they mean for `SCORER_MAX_POSTINGS`, the tick's
`202` design and the two 30-second timeouts, are in `_docs/performance.md`.

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

### Changing the schema

`backend/app/models.py` is the only place the schema is declared. `init_db()`
creates the five tables in a database that does not exist yet; the revision
history in `backend/migrations/versions/` brings a database that already holds
rows to the same five tables, and `backend/tests/test_migrations.py` holds the
two to each other, on SQLite and on PostgreSQL.

The sequence for a change to the models, from `backend/`:

1. Edit `backend/app/models.py`.
2. `cd backend && uv run alembic revision --autogenerate -m "<message>"`
3. **Read the generated revision** and hand-write what autogenerate does not
   detect: a changed CHECK constraint — the score range, or either vocabulary —
   and a re-typed column.
4. `cd backend && uv run alembic upgrade head`
5. Commit the revision **in the same commit as the model change**.

Step 3 is the one that is easy to skip and expensive to have skipped: autogenerate
renders tables, columns, indexes and foreign keys from the models, and it does not
compare CHECK constraints or a column's type. A change it cannot see at all — a
score range moved from 100 to 90, nothing else — writes no revision, because an
empty one is discarded rather than committed; write that one by hand with
`uv run alembic revision -m "<message>"` and fill in the `op.*` calls yourself.

**A database that already exists.** A container brings the database it is given to
the revision history its image carries: `docker compose up` runs `alembic upgrade
head` at startup, before uvicorn, through the app's own `database_url()`, and an
upgrade that fails exits the container non-zero instead of serving. The one
database no container sees is the local `backend/data/jobradar.db`, so it keeps
the command run from a checkout:

    cd backend && DATABASE_URL=<the Neon URL> uv run alembic upgrade head

Run from `backend/` because that is where `alembic.ini` and `migrations/` are, and
with `DATABASE_URL` set because that is the one connection input the revision
environment reads — unset, the command would run against the local SQLite file.
`uv run alembic current` says which revision the database is at; inside a
container, `docker compose exec api alembic current` says the same for the compose
volume. On a database that already holds rows it creates only what is missing and
leaves every row as it was, so it is safe to re-run. `uv run alembic downgrade
base` drops all five tables: it is destructive, and it is meant for the disposable
databases of local development.

## Frontend

The app in `frontend/` is React and TypeScript, and its tests run against a
mock backend, so they need no API, no key and no network.

    cd frontend && npm ci
    cd frontend && npm test
    cd frontend && npm run dev

`npm ci` installs exactly what the committed `frontend/package-lock.json` pins,
which is the install `ci.yml` runs. `npm test` is `vitest run`: it runs the whole
suite under `frontend/src/` once and exits on its own. `npm run dev` serves the
app with the mock backend answering in the browser.

## CI, and the scheduled tick

Two workflows, both in `.github/workflows/`, and each file's leading comment
names the local command that stands in for every job it runs, so the pipeline can
be reproduced without GitHub Actions.

- `ci.yml` runs on every push and every pull request, in four jobs: `backend`
  (`uv sync && uv run pytest`, with the step `Fail on a committed secret, resume
  or database file` running `uv run pytest tests/test_committed_data_guard.py`
  before the suite), `frontend` (`npm ci && npm run build && npm test`),
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
| `list_matches` | `min_score: integer`, `state: string` (`"new"`, `"saved"` or `"dismissed"`), `company_id: integer`, `limit: integer` (all four optional) | the app's rows the filters leave, newest first: `id`, `company_name`, `title`, `url`, `score`, `reason`, `gaps`, `state`, `outcome`, `created_at` |
| `trigger_run` | none | the app's `202` body: `id`, `status`, `started_at`, `finished_at`, `error`, `postings_seen`, `matches_created` |
| `get_run` | `run_id: integer` (required) | that run's row, the same seven fields |
| `remove_company` | `company_id: integer` (required) | a confirmation this tool builds: `id`, `removed` — the route answers `204` with no body |
| `set_match_state` | `match_id: integer`, `state: string` (`"saved"` or `"dismissed"`, both required) | the app's `200` body: the written row, the same fields `list_matches` renders |

Each tool returns the app's own response body, field for field and name for name:
nothing is filtered, renamed or derived here. `list_matches` applies no filter of
its own: with no argument it answers every row, so a prefiltered one (`score` 0)
and an unscored one come back exactly as stored — keeping either out of a digest
or a screen is the app's business. Its four optional arguments are the route's own
query parameters and only the ones it was given are sent: `min_score` is a floor
on the score, `state` is one value of the column's vocabulary (`"new"`, `"saved"`
or `"dismissed"`), `company_id` narrows to one company's postings, and `limit` is
the newest N (at most 200). A list is rendered as one JSON block per row, because
that is how the SDK renders a returned list; anything else is one JSON block. Every failure a client can see is
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

## Deploy

The target is decided, and its reason is recorded in
`_docs/decisions/2026-10-11-deploy-and-repo.md`: a **Render free web service**
running this repository's Docker image, with **Neon Postgres** as the database.
Render's own free database is deleted 30 days after creation, which would break a
review that opens weeks after submission; Neon's free Postgres does not expire, so
the database lives outside Render. That record also holds the repository's
visibility — public on GitHub, so peer reviewers can read this file — which is why
nothing private is ever committed and why no value appears below.

The deployed image is the one `backend/Dockerfile` builds, with the **repository
root** as the build context, exactly as `compose.yaml` builds it. Its first stage
runs the frontend's own build and its runtime stage copies that build in beside
the API, so one container serves both the UI and the API on one port: no second
service, no CORS, and no committed `frontend/dist`.

A production instance sets these variables to its own real values. Their names
are all this file carries:

    DATABASE_URL   UPLOADS_DIR   STATIC_DIR   MAX_UPLOAD_BYTES
    TICK_SECRET    LLM_API_KEY   LLM_MODEL    SCORER_MAX_POSTINGS
    LLM_BASE_URL   LLM_FALLBACK_MODEL
    PREFILTER_TITLE_TERMS    PREFILTER_SENIORITY_TERMS
    PREFILTER_LOCATION_TERMS PREFILTER_MUST_HAVE_KEYWORDS
    PREFILTER_MUST_HAVE_FROM_RESUME
    DIGEST_MIN_SCORE
    SMTP_HOST  SMTP_PORT  SMTP_USERNAME  SMTP_PASSWORD  DIGEST_FROM  DIGEST_TO

The "Configuration" table above says what each one means and what an unset one
means. A production instance sets them for real, and points `UPLOADS_DIR` at a
volume rather than at the repository, because a resume is stored outside the
repository and has to survive a redeploy.

The remaining steps — creating the service, attaching the database, setting the
variables — need the owner's own Render and Neon accounts and are #16. None of
them was performed to write this file, and the file therefore states no URL: #46
adds the deployed-instance evidence once #16 exists.

## Known limitations

Each with what it costs, because a limitation without a consequence is a slogan.

- **Greenhouse is the only source.** LinkedIn and Indeed are excluded on their
  terms and Workday is not attempted — the reasons are in the next section — and
  a second adapter is #28. A company without a Greenhouse board cannot be watched
  at all: not watched badly, not watched.
- **One user, no accounts and no sharing.** There is no login and nothing scoped
  to a user: the watchlist, the resume and the matches belong to the instance, so
  two people cannot use one instance without sharing all three.
- **No embeddings and no vector store, so a match is one LLM call per posting**,
  capped for a single run by `SCORER_MAX_POSTINGS` (default 50). Cost and latency
  grow with the size of the watchlist, and postings past the cap in one run are
  stored with a not-scored reason rather than scored later.
- **The scheduled tick is best-effort.** GitHub Actions cron drifts by minutes to
  hours, can skip a run entirely, and is disabled after 60 days of repository
  inactivity, so a late or missing digest is expected rather than a fault. That is
  why the in-app **"Run digest now"** button is the reliable path, and why the
  demo uses it (`_docs/decisions/2026-10-11-deploy-and-repo.md`).
- **`downgrade base` is destructive.** It drops the five tables and every row in
  them, which is acceptable only while the databases in existence are disposable:
  the local SQLite file and the compose volume this file calls a throwaway.
- **The free tier sleeps.** `_docs/plan.md`'s "Known constraints" records that a
  Render free instance spins down after 15 minutes idle and takes about a minute
  to wake, so the first request after a quiet period is slow. That is the free
  tier's documented behaviour rather than an observation made here — #46 owns the
  observed numbers.

## Sources deliberately not ingested

**Greenhouse is the one source implemented**: `backend/app/sources/greenhouse.py`
reads a company's public board JSON by slug, which needs no key and no account.
Three others were considered and left out, each with its own reason.

- **LinkedIn** — partner-gated, and scraping it breaches its terms.
- **Indeed** — the same: partner-gated, and scraping it breaches its terms.
- **Workday and the other enterprise ATS providers** — their feeds are XML and
  messier in shape, so a second adapter is a follow-up rather than a flag on this
  one: #28.

## How this repository was built, with AI

An AI agent writes this repository in bounded steps, and the one decision it could
not settle itself it handed to another model. The files that make both true are
committed.

### The workflow the repository defines

`AGENTS.md` is what an agent reads first and `_docs/process.md` is the rule book;
`_docs/team/` holds one prompt per role, written to the template in
`_docs/team/task-template.md`. Together they say:

1. **Grooming first.** A task is a GitHub issue, and it is groomed before anyone
   implements it: the PM (`_docs/team/pm.md`) rewrites it against
   `_docs/team/task-template.md` — goal, acceptance criteria that can be checked
   by looking at the result, out of scope with a link per moved item, constraints.
2. **One issue at a time, lowest number first.**
3. **The engineer implements it.** `_docs/team/software-engineer.md`: implement
   what the issue describes, do not change the acceptance criteria, write tests
   for the new behaviour, commit regularly, do not close the issue, and leave a
   comment saying what was done.
4. **QA checks it against the issue.** `_docs/team/qa-engineer.md`: a verdict that
   starts with PASS or FAIL, one line per acceptance criterion, with the test
   command and its result — and QA changes no code. A single FAIL keeps the issue
   open.
5. **The orchestrator closes the issue only after a PASS.**

### Which AI did what

- **The orchestrator session** owns the plan, the delegation, the review fanout
  and the closing of issues, working from `AGENTS.md` and `_docs/process.md`.
- **The PM agent** grooms each issue before implementation, under
  `_docs/team/pm.md`.
- **The engineer agent** implements one groomed issue at a time and writes its
  tests, under `_docs/team/software-engineer.md` — the role that wrote this
  README.
- **The QA agent** verifies the finished work against the issue's criteria, under
  `_docs/team/qa-engineer.md`.
- **Jev**, a separate model (`typesafe/jev-1.13-20260917`, through OpenRouter),
  took one bounded architecture decision: the deploy target, the repository's
  visibility, the digest trigger, and whether a single-container poller is sound.
  What was asked is preserved verbatim in `_docs/decisions/jev-request.json`, the
  raw verdicts with their confidence scores are in
  `_docs/decisions/jev-verdicts.json`, and the outcome is in
  `_docs/decisions/2026-10-11-deploy-and-repo.md`.
- **The MCP server in `mcp/`** is the same idea pointed at the product instead of
  at the build: five bounded tools over the app, so an AI client can add a
  company, list the watchlist, list matches, start a run and read that run.

### Two answers were escalated, not accepted

Jev answers each question with a confidence score, and the verdict file marks the
answers that came back below the bar as escalations rather than decisions. Two
were: the digest trigger, and whether the single-container poller is sound. The
decision record presents both as **priors, not decisions the model made**, and
records how each was resolved: the in-app "Run digest now" button is the reliable
trigger with the scheduled workflow as a best-effort convenience, and the
single-container poller stands, with the run row as the constraint it implies.
