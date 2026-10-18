# JobRadar

**JobRadar is a one-user job-search assistant that turns a list of companies you
want to work for into one daily digest of the new postings there, matched against
your uploaded resume, each with a plain-English reason and the gaps.**

**The live instance: <https://jobradar-c75b.onrender.com>** (Render's free plan
with Neon Postgres — the first request after a quiet period takes about a minute,
see [Deployment](#deployment)).

## Problem

**Who has it.** One person: the owner. No accounts, no login, no multi-tenancy.

**Why the obvious options fall short.** Finding the openings worth applying to
means either checking many company career pages by hand or drowning in a job-board
firehose where most postings are irrelevant. Neither tells you *why* a role fits,
so the same postings get re-read.

**What the system does.** A company is added by name, board slug and source; once
a day — or whenever the owner presses the in-app **"Run digest now"** button — the
app fetches each company's public job board, normalises the postings, and scores
the new ones against the resume: a score 0–100, why it fits, and what is missing.
New matches above the cutoff are grouped into one email; with nothing above it, no
email is sent and the run says so. The UI lists the watchlist and the matches with
their score, reasoning and gaps, and a match can be marked saved or dismissed.

The behaviour spec, the non-goals and the constraints are `_docs/plan.md`.

## Demo

The live instance is the demo: open <https://jobradar-c75b.onrender.com>, and the
first request may take about a minute while Render's free instance wakes. One page
holds everything: the resume upload, the watchlist, the matches, **"Run digest
now"**, **"Re-match stored postings"**, and the Telemetry screen. A match row
carries its score, reason, gaps and state; the two run controls are the reliable
trigger — the daily workflow is a convenience, and cron drifts (see
[CI/CD](#cicd)).

**A deployed run, end to end (checked 2026-10-18).** `GET /` answered `200` with
`text/html; charset=utf-8` — the frontend build, not a Render error page and not
bare JSON. `POST /resume` accepted a synthetic resume and answered `201`; the
active resume row is id 2. `GET /matches` on the same instance then answered rows
scored against it: read from the deployed database, 80 `match` rows hold
`outcome = 'scored'`, 75 of them a score of 1 or more, on the 0–100 scale the
scorer's prompt and the `match` table's own constraint define — the deployed rows'
own maximum is 30 — and the rows name the model `deepseek/deepseek-v4.1-flash`
with prompt version 1 as their provenance. The press that wrote them is run id 6 —
`succeeded`, `2026-10-18T12:44:56Z` to `12:49:35Z`, 50 matches decided. Those 50
keep the `created_at` of the run that first scored them, because a re-match
updates the `match` row its posting already keys rather than inserting one. No
resume text, file name, candidate detail or email is quoted here or anywhere else
in this file.

The deployed instance's storage and its cold start are under
[Deployment](#deployment).

**The MCP server, one call, no UI.** A local app with a throwaway secret and a
throwaway database on port 8123 (8000 belongs to compose) — `TICK_SECRET` is a
literal throwaway, like the local Postgres password in `compose.yaml`: its only
power is over the app on this machine's port 8123:

    cd backend && TICK_SECRET=throwaway-local-secret \
      DATABASE_URL=sqlite:////tmp/jobradar-mcp-demo.db \
      uv run uvicorn app.main:app --port 8123

and one command that opens a stdio MCP session to it, calls one tool and prints
the answer — no Node, no Inspector, no interactive click:

    cd mcp && JOBRADAR_URL=http://127.0.0.1:8123 \
      JOBRADAR_TICK_SECRET=throwaway-local-secret uv run python demo_client.py

`mcp.client.stdio` starts the server on the other end of a pipe,
`ClientSession.call_tool("trigger_run", {})` makes the one call, and its result is
the app's own `202` body (the ids and timestamps are the app's own, so they differ
per run):

```
trigger_run -> {
  "id": 1,
  "status": "running",
  "started_at": "2026-10-18T16:49:43.006783Z",
  "finished_at": null,
  "error": null,
  "postings_seen": 0,
  "matches_created": 0,
  "matches_emailed": null,
  "companies_not_fetched": null,
  "postings_not_rematched": null,
  "trace_id": "f19940a52ee14c3b82d9d303d9a7a6a5",
  "company_outcomes": []
}
```

The app is up at that moment, and the same row is readable from the app itself:

    curl -s http://127.0.0.1:8123/healthz      # {"status":"ok"}
    curl -s http://127.0.0.1:8123/runs/1

The run's later outcome is **not** asserted: with an empty watchlist and no model
key the pipeline scores nothing and ends the row `failed`, so the `curl` above may
already read `"error": "No active resume: upload one before a run can score
postings"`. That is the app's own business, not this call's.

## Evaluation

**What is measured: the cost of one whole tick.** `backend/benchmarks/tick_benchmark.py`
drives the real pipeline — watchlist, fetch, ingest, prefilter, score, record,
digest — against a real PostgreSQL over a synthetic watchlist, with only the board
and the model faked at the seams the app already has. It needs the throwaway
Postgres the Testing section documents and **fails rather than skips** without it:
timings on SQLite say nothing about the engine the app deploys on. It is a script,
not a case, so `uv run pytest` never collects it, and it empties the tables it
writes before and after every run:

    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar uv run python benchmarks/tick_benchmark.py --companies 10 --postings 50

**That command does not run at this commit, and this is not smoothed over:**
`pipeline._fetched()` passes `etag` and `last_modified` to the board source and
`SyntheticBoard.__call__()` accepts neither, so the script fails with a
`TypeError` before it measures anything — it predates that change to the fetch
seam. The numbers below are the ones `_docs/performance.md` recorded with it, on
one Apple M3: the postings and match rows read back from the database, the wall
time the whole run.

| scale (companies × postings) | postings | wall time | rows written |
| --- | --- | --- | --- |
| 5 × 20 | 100 | 0.2 s | 100 postings, 100 matches |
| 10 × 50 | 500 | 0.9 s | 500 postings, 500 matches |
| 10 × 50, repeated | 500 | 1.2 s | 500 postings, 500 matches |
| 20 × 50 | 1000 | 1.7 s | 1000 postings, 1000 matches |
| 40 × 50 | 2000 | 3.3 s | 2000 postings, 2000 matches |

Between 1.6 ms and 2.4 ms of app-side work per posting — the row's insert, the
prefilter, the `match` write, the two `run` counter updates and the digest's own
select, write and markup. The cost is linear, which is what makes
`SCORER_MAX_POSTINGS` (50) a bound worth having and `POST /internal/tick`'s `202`
plus background run row the right shape: 2,000 postings took 3.3 s of app time,
and the model calls that would precede them are the real cost. The machine, the
command and what the numbers mean for the three constants are
`_docs/performance.md`.

**What is not measured: match quality.** There is no labelled set of postings and
no precision/recall figure, because the project has no ground truth and one user.
The scorer's judgement is therefore not evaluated anywhere; what the suite proves
is that the pipeline calls it, stores its structured answer, and behaves correctly
when the call fails — with a `FakeScorer` substituted at the seam, so the suite
needs no API key and no network. That gap is a named limitation below, not a
target that was met.

## Testing

One command runs the whole test surface — backend unit, backend integration,
frontend, MCP, and the on-call responder's offline suite, in that order:

    make test

The root `Makefile` names each suite and the command that runs it, one line each,
and `scripts/test.sh` runs them in one process. It installs what a fresh checkout
is missing (`uv sync` for the backend and MCP environments, `npm ci` for the
frontend), starts a throwaway `postgres:17` in Docker for the two backend passes,
and removes that container when the run ends — after a pass, after a failure and
after `Ctrl-C`. `make test` needs `uv`, `npm` and `docker` on `PATH`.

It stops at the first suite that fails, heading each suite `== <suite> ==`; and a
suite whose summary reports no passed case, or any skipped case, fails the run too
— a `[postgres]` parameter or the MCP `live` case that skipped is a half-run, not
a green. The suites run against the PostgreSQL `scripts/test.sh` starts, never one
an inherited `TEST_DATABASE_URL` already names. The port is 5433, fixed in the
`Makefile` as `TEST_DB_PORT` because 5432 is taken on a development machine; a
server already there is not reused — the run prints the port it uses and moves to
the next free one.

**What each suite reports** (measured 2026-10-19, with `TEST_DATABASE_URL`
pointing at a throwaway `postgres:17` on 5433, the way CI runs it):

| Suite | Command | Result |
| --- | --- | --- |
| backend unit | `cd backend && uv run pytest` | 1956 passed, 2 deselected |
| backend integration | `cd backend && uv run pytest -m integration` | 2 passed |
| frontend | `cd frontend && npm test` | 10 files, 167 tests passed |
| MCP | `cd mcp && uv run pytest` | 47 passed, 1 deselected |
| on-call | `uv run --project backend pytest ops/oncall` | 88 passed |

Without a PostgreSQL server the backend suite reports `1546 passed, 410 skipped,
2 deselected` instead: the engine matrix's `[postgres]` parameter skips by name.
CI refuses a skipped case for exactly that reason, so the number that matters is
the one with the server.

Each suite also runs on its own, which is what the rest of this section documents.
The two deselected backend cases are the marked integration suite (see below); the
MCP suite's one deselected case is its `live` case.

### The backend suite, and the two engines

    cd backend && uv sync
    cd backend && uv run pytest

No container, no Postgres and no API key: SQLite is the default engine, and fakes
stand in for the board and the model. The engine matrix's PostgreSQL cases run
only when `TEST_DATABASE_URL` names a server, and skip with that variable's name
when it does not. A throwaway server is how to run them, on a free port — 5433
here, because 5432 is taken on this machine:

    docker run --rm -d --name jobradar-test-db -p 5433:5432 \
      -e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway \
      -e POSTGRES_DB=jobradar postgres:17
    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar uv run pytest
    docker rm -f jobradar-test-db

`compose.yaml` is not a test harness: the container above is the database those
tests run against. The engine matrix asserts that server holds exactly the eight
tables the models declare, so pointing `alembic` at it by hand —
`DATABASE_URL=<that URL> uv run alembic upgrade head` — leaves alembic's own
`alembic_version` table behind and the next `uv run pytest` fails on that
assertion until it is dropped:

    docker exec jobradar-test-db psql -U jobradar -d jobradar -c "drop table alembic_version"

The suite's own two engines are also pinned by `backend/tests/test_engine_parity.py`
(one behaviour, both engines) and `backend/tests/test_migrations.py` (the
migrations reach the models' schema on both).

### The integration suite: the whole workflow against PostgreSQL

`backend/tests/integration/` holds one suite that drives the real application end
to end: the real `POST /resume`, the real `POST /watchlist`, the real
`POST /internal/tick` with its own daemon thread and its run row, the real
pipeline, prefilter and digest, and the real `GET /matches` — all of it against a
real PostgreSQL server. Exactly three things are faked, each at the seam the
running code reads it from: the Greenhouse fetch, the LLM scorer and the mailer.
So **no API key and no SMTP credential are needed**, no request leaves the
process, and the resume it uploads is synthetic — written for the tests, in the
request.

Every test there carries the `integration` marker, declared in
`backend/pyproject.toml`'s `[tool.pytest.ini_options]` (`markers`), which is also
where the default run's `addopts = "-m 'not integration'"` lives;
`backend/tests/integration/conftest.py` marks every item it collects. The default
run therefore collects this suite and **deselects** it, and the command that runs
only it, against the same throwaway server as above, is:

    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar uv run pytest -m integration

It needs a real PostgreSQL server and it **fails rather than skips** when
`TEST_DATABASE_URL` is unset, empty or unreachable: there is nothing in it to
skip, and SQLite is not the engine under test. The suite drops the tables it
created, so the database is left as it was found. A unit test
(`backend/tests/test_integration_separation.py`) fails if the directory holds no
test module, if the marker is not declared, or if an item collected there does not
carry the marker.

### The frontend suite

    cd frontend && npm ci
    cd frontend && npm test

`npm ci` installs exactly what the committed `frontend/package-lock.json` pins,
which is the install `ci.yml` runs. `npm test` is `vitest run` under jsdom against
the mock backend in `frontend/src/mocks/`, so the suite needs no API and no key,
and reaches nothing: `frontend/src/api/noStrayFetch.test.ts` fails if any file
under `src/` other than the API module reaches the network.

### The MCP suite

    cd mcp && uv sync
    cd mcp && uv run pytest
    cd mcp && uv run pytest -m live

The default run needs no app, no network and no `.env`: every case injects
`httpx2.Client(transport=httpx2.MockTransport(...))` the way
`backend/tests/test_greenhouse_source.py` does, and drives the tools by name
through `MCPServer.call_tool` and `MCPServer.list_tools()`, so the SDK's own
argument validation, output conversion and error wrapping are what is exercised.
The one case that drives the real app is excluded by `addopts = "-m 'not live'"`
in `mcp/pyproject.toml`, the way the backend excludes `integration`: it starts the
app as a subprocess on a free port with a temporary SQLite file under the system
temp directory — never `backend/data/jobradar.db` — and asserts that the row
`trigger_run()` returned is the row `get_run(run_id)` reads back. An autouse guard
in `mcp/tests/conftest.py` refuses a real socket, and the two variables are set
in-process to throwaways.

### The on-call suite, and the guards around the surface

The on-call responder's offline test is the one suite outside a project directory.
It runs at the repository root, over the responder's fake app, model and Render
API, needs no `.env`, no socket, no key and no database:

    uv run --project backend pytest ops/oncall

Five guards keep the test surface itself honest, each named for what it pins:
`backend/tests/test_single_test_command.py` (the `Makefile`'s `test` target names
every suite, and `scripts/test.sh` is the one process that runs them),
`backend/tests/test_ci_make_parity.py` (CI reaches each suite through the
`Makefile` target rather than repeating its command), `backend/tests/test_committed_data_guard.py`
(nothing private is committed — see [Data and configuration](#data-and-configuration)),
`backend/tests/test_repo_layout.py` (every path the [Repository layout](#repository-layout)
section names is on disk), and `backend/tests/test_dockerignore.py` (the image's
build context stays source and the two lock files, see [Quickstart](#quickstart)).

## Monitoring

### Telemetry: what is captured, and how to view it

One trace id holds the whole digest journey: the `POST /internal/tick` request,
the background run it starts, and every log line and span either of them writes.
It is the caller's own `X-Request-ID` when that value may be adopted — at most 64
characters of `[A-Za-z0-9._-]`, so a bad header cannot put arbitrary text in a
stored column — and a generated id otherwise; the middleware puts it back on the
response whatever its status, and the run row carries it (`trace_id`, served by
`GET /runs/{id}`), which is how the run screen links to its trace.

Spans go to the `telemetry_span` table through one writer, and hold seven facts
and no eighth: no request body, query string, `Authorization` value,
`X-Tick-Secret`, database URL, resume text or file name has anywhere to go.
`route` is the matched route's *template*, never a path with an id, and a path no
route matched is `<unmatched>`. The run's own span is named `run` and has no
route, method or status code, because it is not an HTTP request. Telemetry never
fails the request it observes: a database that is down is one warning line and a
response unchanged. Logs are one JSON object per line with the trace id in its own
field.

The window is bounded without a scheduled job: `GET /telemetry/metrics` answers
over the last hour, `GET /telemetry/metrics/buckets` cuts the same window into
minute rows, `GET /telemetry/traces` answers the newest traces grouped by trace
id, and the spans that fall out of the 7-day retention are pruned once per process
start. The bucket rows are written in the same transaction as their span and
recounted from the minute's stored spans, so a bucket is the metrics read's own
numbers for that route and minute.

The three reads, on the live instance or on compose's port 8000:

    curl -s https://jobradar-c75b.onrender.com/telemetry/metrics
    curl -s https://jobradar-c75b.onrender.com/telemetry/metrics/buckets
    curl -s https://jobradar-c75b.onrender.com/telemetry/traces

`/telemetry/metrics` answers one object per route — `route`, `count`, `errors`,
`p95_ms`; `/telemetry/metrics/buckets` the same per route and clock minute, with
`bucket_start` in UTC; `/telemetry/traces` the newest traces, each with its id and
its spans (`name`, `route`, `method`, `status_code`, `duration_ms`, `started_at`).
Those key names are a contract: the UI renders this body, and the alert and the
on-call agent quote these trace ids, so a rename breaks them.

**The Telemetry screen** is the last section of the one page. It shows the route
metrics as a table (route, requests, errors, p95 in ms) plus a chart of the same
window from the buckets, and the recent traces below with one row per span; a span
that is not an HTTP request shows `—` for its route and status rather than going
blank. Each read settles on its own, so a failed one shows its own error and still
leaves the other's table on screen. There is no dashboard screenshot in the
repository — the screen is behind the live URL, and the two reads above are the
same data without a browser.

If `OTEL_EXPORTER_OTLP_ENDPOINT` names an OTLP/HTTP collector, the same span is
also exported there, beside the database write and behind the response the request
already got; unset means no exporter is built at all, which is what the deployed
instance runs.

### Alerting on user impact

`.github/workflows/alert.yml` runs hourly (and by hand) and asks one question: is
the digest journey showing user impact right now? `ops/oncall/impact_check.py`
reads the deployed app over HTTP and only over HTTP — `GET /healthz`,
`GET /runs?limit=50`, `GET /matches?limit=200` and `GET /telemetry/metrics`, all
from `JOBRADAR_URL` — and classifies the journey through five named predicates:

| Predicate | Fires when |
| --- | --- |
| `newest_run_failed` | the newest run ended `failed` (a run still in flight is not a failure) |
| `model_scored_none` | the newest run finished, stored matches inside its `[started_at, finished_at]` window, and not one of them was scored: the model call failed on every row it reached |
| `digest_stalled` | no run succeeded within 26 hours, or nothing ran at all in that time |
| `http_5xx_rate` | the journey's two routes (`POST /internal/tick`, `GET /runs/{id}`) answered 5xx at or above 5% over the metrics window |
| `app_unreachable` | `GET /healthz` and then `GET /runs?limit=1` both still fail to answer `200` (a connection error, a timeout or a 5xx) after the reads' own retries: one failed read is a cold free-tier start, not an outage |

When any predicate fires, the workflow opens a GitHub issue labelled `alert` whose
body names the impact and carries the evidence — the newest run's id, status,
counts and timestamps, the values each fired predicate read, and the check's own
output. It keeps at most one open: a detection while one is open comments its new
evidence on it, and a run that finds no impact comments the healthy verdict on the
open issue and closes it, so the one-open rule cannot silently swallow the next
incident. A `concurrency` group serialises the runs, and the only credential is
the workflow's own `GITHUB_TOKEN` with `issues: write`. The alert body names ids,
statuses, counts, instants and route names, and no resume text, file name,
credential, request body, CPU or memory number; it carries no trace id, because
what it quotes is the values each predicate read. An app that never answered
`200` to the two probes is `app_unreachable`, and that alert takes the same
one-open-issue path. A read that did not answer while the app did — one probe of
the two, or a journey read after the probes answered — is neither a healthy
journey nor user impact: the check exits non-zero naming the read, the workflow
goes red and opens no issue.

The local equivalent, with the app on port 8123, and the check's own offline test:

    JOBRADAR_URL=http://127.0.0.1:8123 python3 ops/oncall/impact_check.py
    cd backend && uv run pytest tests/test_impact_check.py

The GitHub issue is the channel a person reads; it can also wake someone on a
third-party pager. `alert.yml`'s last step runs `ops/oncall/notifier.py` after the
issue step, and sends that report's own title and body — the same evidence the
issue carries, and no more. The pager is opt-in through two repository secrets:
`PAGER_URL` (the pager's HTTPS endpoint: one JSON `POST` of
`{"title": ..., "body": ...}`) and `PAGER_TOKEN` (the credential, sent as
`Authorization: Bearer <token>`; optional). `PAGER_URL` unset or empty is a
**documented no-op**: no call is made and the step succeeds. The call is bounded —
one timeout and at most one retry — and everything it sends passes through the
responder's redaction, so no resume text, file name, API key, connection string or
request body reaches the pager. A page that fails after its retry is an `::error::`
and a red step, and the alert issue the previous step opened stays open. Its
offline test is in the responder's suite, over a fake pager endpoint with no
socket and no credential.

### On-call response

The `alert` issue is also a wake-up call. `.github/workflows/oncall.yml` starts
`ops/oncall/responder.py` for the `issues` event when an issue carries the `alert`
label — whether the label was added later or the issue was opened already
labelled — and for no other event: no schedule, no manual dispatch and no pull
request. It needs no new secret: `RENDER_API_KEY`, `RENDER_SERVICE_ID` and
`vars.JOBRADAR_URL` are the values `deploy.yml` already uses, `GITHUB_TOKEN` comes
from the run with `issues: write`, and `LLM_API_KEY` is optional — unset means the
deterministic path and is not a failure. A required value that is unset fails the
job with an `::error::` naming it rather than skipping.

A gateway key needs two more **optional repository variables**, `LLM_MODEL` and
`LLM_BASE_URL`, which the wake's decide step, both proposal steps and the scheduled
re-check are handed: a
key whose gateway is not OpenAI's would otherwise be sent to OpenAI's own endpoint
and answer `401` even though the key is valid. Both are unset in a default
repository, and unset or empty changes nothing — the responder and the proposer
fall back to their own `gpt-4o-mini` at `https://api.openai.com/v1`, so an OpenAI
key works as before. Neither is a credential, so both may appear in the log: the
proposal's configuration check prints them in a `::notice::`, which is what shows a
configured run the model and endpoint it will really call, before any model call.

One `alert` issue is one wake. A `concurrency` group keyed on the issue's number
serialises the deliveries, and the responder refuses an issue that already carries
its own comment marker, so the `opened` and `labeled` deliveries cannot remediate
twice. The responder reads the deployed app's own signals — `GET /healthz`,
`GET /telemetry/metrics` and `GET /runs/{id}` for the run the alert body names —
and the issue body, and then chooses one action from a fixed set. With
`LLM_API_KEY` set the model chooses among those actions and an answer that names
nothing in the set is not an action; with the key unset, or the model call
failing, a documented deterministic rule chooses over the same evidence:

| Action | What it does | The deterministic rule chooses it when |
| --- | --- | --- |
| `retrigger` | `POST /internal/tick` with the app's own same-origin admission: the `Origin` header set to `JOBRADAR_URL`, no `X-Tick-Secret` and no new secret | the alert's run ended `failed` |
| `rollback` | runs `ops/rollback.sh`, which owns the selection of the newest known-good `live` commit that is not the one serving | `GET /healthz` answered a status other than `200` |
| `escalate` | comments and changes nothing | `/healthz` did not answer at all, no rule fits, or an action could not be verified |

After `retrigger` the responder waits for the run the `202` named to reach
`succeeded` or `failed`, and after `rollback` it waits for `GET /healthz` to
answer `200` (long enough for a free instance's cold start). It comments once with
what it found, what it did, the run id and trace id it saw and whether the issue
is fixed, and closes the issue only when the effect is verified — otherwise it
leaves the issue open naming the owner action required. One wake takes at most one
action and writes at most one comment, and no path edits or pushes code, re-adds
the label, reopens the issue or schedules another run, so a wake cannot cascade. An
awkward answer — a `409`, an unreachable app, a rollback that refuses, a wait that
runs out — ends in `escalate`, never an unhandled exception. Nothing it emits
carries a resume, a file name or a credential: the comment quotes signals, ids and
statuses only, and the model's reason and the rollback output pass through a
redaction step.

### Proposing a code fix, behind a review gate

`retrigger` and `rollback` cannot fix a code fault, so the same `alert` issue wakes
a second path: `oncall.yml`'s `proposal` job runs after the wake, on the same
issue, and declines anything the responder's own answer says is fixed
(`- **Fixed:** yes`), anything the responder has not answered, a closed issue, and
an incident this path has already answered once — its comment carries its own
marker, so the two deliveries of one alert cannot push two branches.

For an incident it takes, `ops/oncall/proposer.py` asks the model which files under
`backend/app/` the incident implicates, hands it those files' own contents at
`HEAD`, and applies the one git unified diff it answers on the new branch
`oncall/proposal-<issue number>`. It pushes that branch and nothing else, opens it
as a **draft pull request against `main`**, requests the repository owner's review,
and comments the link on the alert issue. A patch is applied only when it names at
least one file, names nothing outside the files the model was handed, renames
nothing and is bounded, so a proposal cannot reach `.github/workflows/` — the
review gate's own workflow included. No path pushes `main`, merges, approves,
force-pushes or deploys, and `ops/oncall/test_proposer.py` reads every command the
path can issue back to pin that.

What makes the review *required* is a repository setting this workflow cannot make:
**Settings > Branches > require a pull request before merging, with at least one
approving review on `main`** — recorded here because `deploy.yml` records Render's
autodeploy setting the same way. Nothing lands on `main` without a human merge, and
`deploy.yml` deploys only a push to `main`, so no path deploys a commit no human
approved. The proposal job's token is scoped to it (`contents: write` and
`pull-requests: write`, where the wake keeps `contents: read`), and `LLM_API_KEY`
and `PROPOSAL_REVIEWER` are optional there: unset or empty means an `::notice::`
naming it and a green skip, not a red job and not a proposal attempted with no
model or nobody asked to review it. `ISSUE_NUMBER` comes from the event rather than
from configuration, so a malformed one still fails the job. The local equivalent,
with a token that may push a branch:

    ISSUE_NUMBER=<n> PROPOSAL_REVIEWER=<login> \
      LLM_API_KEY=${LLM_API_KEY} GH_TOKEN=${GH_TOKEN} python3 ops/oncall/proposer.py

### The unattended re-check

A wake fires once, when the `alert` label arrives. A remediation that did not hold,
or an escalation nobody picked up, would therefore stay silent until a person
looked — so `oncall.yml` also runs **every hour, at half past**, half an hour
after `alert.yml`'s own hourly check, and starts `ops/oncall/recheck.py`. It reads
the same optional `LLM_MODEL` and `LLM_BASE_URL` as the wake, so a gateway key works
on the unattended path too. The scheduled job and the two `issues` jobs are the same
workflow file; each is guarded to its own event, so the schedule starts the re-check
and never a wake or a proposal.

One scheduled run reads every open `alert` issue and makes **one attempt** per
incident: it reads the deployed app's own signals again — the same `GET /healthz`,
`GET /telemetry/metrics` and `GET /runs/{id}` — and takes at most one action again,
through the responder's own action set and escalation rule
(`responder.decide_and_act`) rather than a second copy of either. Its comment
carries the attempt number, the run's own URL and the responder's own answer
verbatim, and a verified fix closes the issue exactly as a wake closes it.

It is bounded, and the bound is read off the issue itself: at most 3 attempts per
incident, counted from the re-check's own comments, so it holds across runs. The
last attempt's comment carries the one notice that says the re-check has stopped
and what a person must do, and every later run touches nothing. It cannot trigger
itself either: no path here schedules, dispatches or labels anything — the job is
granted no `actions: write`, and its only writes are one comment and, on a verified
fix, a close. A comment fires `issue_comment`, not the `issues` event the wake
reads, and the re-check re-adds no `alert` label, so a re-check cannot cascade into
a wake.

    JOBRADAR_URL=http://127.0.0.1:8123 GH_TOKEN=${GH_TOKEN} python3 ops/oncall/recheck.py
    uv run --project backend pytest ops/oncall

Both on-call paths carry the same named limit: an `issues`-event workflow and a
`schedule` only start once the file is on the default branch, so neither the wake
nor the re-check can be recorded from the branch that adds this workflow; they
start working when `oncall.yml` reaches `main`, and their first real runs are what
[Future work](#future-work) names.

## Quickstart

From the repository root, on a fresh clone, with no `.env` and no other command:

    docker compose up --build --wait

It exits 0 when both services are healthy (verified 2026-10-19); the app is then
on <http://localhost:8000> — the UI at `/` and the API under it. One container
serves both, so there is one origin, no CORS and no second service.

**Prerequisites and toolchain.** Docker with Compose v2 is all the quickstart
needs. The toolchain the image and CI pin: Python 3.12 (`backend/pyproject.toml`,
`python:3.12-slim`), Node 22.12 (the build stage), `uv` for the Python
environments, `npm` for the frontend, PostgreSQL 17 in the container. GitHub
Actions are pinned to released versions, never to a branch.

**What comes up.**

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
it applied (verified against this tree):

    docker compose exec api alembic current   # 0015_metric_bucket (head)

There is no migration step to run by hand. The image ships `alembic.ini` and
`migrations/`, and the container applies them at startup against the same
`DATABASE_URL` the app uses — no second connection setting and no environment
variable of its own. A database that does not exist yet is built by the baseline
revision, so nothing has to happen before the API serves; a database that already
holds rows is adopted where it stands — the baseline creates a table only when it
is missing, later revisions add what the models gained, and every row stays as it
was — so a second `docker compose up` sends no `CREATE` or `DROP` and changes no
row.

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
-v` once (or `docker volume rm jobradar-uploads`) recreates it as uid 10001's, and
nothing else about the volume changes.

The Postgres password in `compose.yaml` is a **local throwaway**, safe to commit
only because `db` publishes no host port — its only power is over a database on
this machine. No resume, key or database file is ever committed; the data rules
are [below](#data-and-configuration) and in `_docs/process.md`.

### The page on `/` is the image's own build

The UI enters the image in an earlier stage of `backend/Dockerfile`, which runs
the frontend's own `npm ci && npm run build` and copies the result into the
runtime stage at `app/static`; `compose.yaml` builds that with the repository root
as the build context (`.dockerignore` at the root is what keeps that context to
source and the two lock files). Node exists in that stage and in no other: the
runtime stage is `python:3.12-slim` plus `uv` and the app, and
`docker compose exec api sh -c 'command -v node'` prints nothing (verified). There
is no committed `frontend/dist`, no mount and no second container, so the served
build comes from the image and nowhere else.

The image must hold source and the built page and nothing else. `ls -a` is not
enough to prove that — it lists the top level only — so this check is recursive
and prints nothing when the image is clean. `--entrypoint sh` is needed because
the image's own entrypoint runs the migration first:

    docker compose build api
    docker run --rm --entrypoint sh jobradar-api -c 'find /app \
      \( -name "*.pyc" -o -name "__pycache__" -o -name ".venv" \
         -o -name ".pytest_cache" -o -name "*.db" -o -name "*.sqlite3" \
         -o -name "uploads" -o -name "data" \) -print'

Anything it prints leaked through `.dockerignore`, which is not `.gitignore`: its
patterns are anchored at the context root, so a bare `__pycache__/` excludes the
root directory only and lets `app/__pycache__/` into the image. Every pattern that
has to match at any depth carries the `**/` prefix, and `backend/tests/test_dockerignore.py`
asserts exactly that.

### Caching the assets

The mount sets `cache-control` from the path, because Vite's build is what makes
one answer correct for each. Every file under `assets/` carries a hash of its own
contents in its name (`index-B6OjL1iC.js`), so a new build never reuses an old
name: those are served `public, max-age=31536000, immutable` and the browser keeps
them for a year without asking. `index.html` names the current build's hashes and
has none of its own, so it is served `no-cache` — stored, but revalidated on every
load — because a cached page pointing at hashes the new build does not have is a
broken page (both verified against the running container):

    curl -si http://localhost:8000/ | grep -i cache-control
    curl -si http://localhost:8000/assets/<hashed file> | grep -i cache-control

No other path is affected: a path the API answers carries no `cache-control`,
mount or not.

### Two details of serving a UI at `/`

There is no client-side router in the UI, so a deep link has no `index.html` to
fall back to: the mount answers `index.html` only for paths that name a file it
holds (`/` among them), and a path in neither the contract nor the build is the
mount's own `404` — `{"detail":"Not Found"}`, the same body shape an unknown API
path gets (`/no/such/path`, verified). `backend/tests/test_static_ui.py` makes one
request for every path in the committed contract and asserts each answer is its
own route's, so adding a router later is a red test rather than a surprise in a
browser.

### Local development, without Docker

    cd backend && uv run uvicorn app.main:app --reload

This path uses SQLite (`backend/data/jobradar.db`) and serves no UI: with no build
at `backend/app/static` no mount is registered, so `GET /` is `404` while
`/healthz` and the API answer as usual. The frontend has its own dev server and
its build:

    cd frontend && npm run dev      # the UI with the mock backend answering in the browser
    cd frontend && npm run build    # tsc -b && vite build, into frontend/dist

If something does not work: the macOS trap in `AGENTS.md` (`uv`'s editable install
of `mcp/` loses its `.pth` files, so `import jobradar_mcp` fails in the backend
suite) has the documented workaround there; port 8000 is compose's, the README's
local examples use 8123; and a stale `backend/openapi.json` is a red `uv run
pytest` with the command to regenerate it in the next section.

### Regenerating the OpenAPI contract

`backend/openapi.json` is generated from the FastAPI app object and committed. Do
not edit it by hand: `uv run pytest` compares it with the document the app
generates now and fails when the two differ, and `ci.yml`'s `contract` job
regenerates it and fails on a diff.

    cd backend && uv run python -m app.export_openapi

The output is deterministic — no timestamps, absolute paths, hostnames or versions
read at runtime — so running the command on a fresh clone rewrites the file byte
for byte identically, and running it twice leaves `git status` clean.

### Changing the schema

`backend/app/models.py` is the only place the schema is declared. `init_db()`
creates the eight tables in a database that does not exist yet; the revision
history in `backend/migrations/versions/` brings a database that already holds
rows to the same eight tables, and `backend/tests/test_migrations.py` holds the
two to each other, on SQLite and on PostgreSQL.

The sequence for a change to the models, from `backend/`:

1. Edit `backend/app/models.py`.
2. `cd backend && uv run alembic revision --autogenerate -m "<message>"`
3. **Read the generated revision** and hand-write what autogenerate does not
   detect: a changed CHECK constraint — the score range, or either vocabulary —
   and a re-typed column.
4. `cd backend && uv run alembic upgrade head`
5. Commit the revision **in the same commit as the model change**.

Step 3 is the easy one to skip and expensive to have skipped: autogenerate renders
tables, columns, indexes and foreign keys from the models, and it does not compare
CHECK constraints or a column's type. A change it cannot see at all — a score
range moved from 100 to 90, nothing else — writes no revision, because an empty
one is discarded rather than committed; write that one by hand with
`uv run alembic revision -m "<message>"` and fill in the `op.*` calls yourself.

**A database that already exists.** A container brings the database it is given to
the revision history its image carries: `docker compose up` runs `alembic upgrade
head` at startup, before uvicorn, through the app's own `database_url()`, and an
upgrade that fails exits the container non-zero instead of serving. The one
database no container sees is the local `backend/data/jobradar.db`, so it keeps
the command run from a checkout:

    cd backend && DATABASE_URL=<the Neon URL> uv run alembic upgrade head

Run it from `backend/` because that is where `alembic.ini` and `migrations/` are,
and with `DATABASE_URL` set because that is the one connection input the revision
environment reads — unset, the command would run against the local SQLite file.
`uv run alembic current` says which revision the database is at; inside a
container, `docker compose exec api alembic current` says the same for the compose
volume. On a database that already holds rows it creates only what is missing and
leaves every row as it was, so it is safe to re-run. `uv run alembic downgrade
base` drops all eight tables: it is destructive, and it is meant for the
disposable databases of local development.

## Data and configuration

`.env.example` at the repository root is the list of every variable the code
reads, one line each with what an unset value does; `.env` — gitignored — is where
the real values go. `backend/tests/test_env_example.py` resolves every variable the
three trees read and fails if the file and the code disagree in either direction.
`compose.yaml` passes the app's variables through, so an unset one arrives empty
and the app's own default is what an empty one means.

| Variable | Required? | Unset or empty means |
| --- | --- | --- |
| `DATABASE_URL` | no | the local SQLite file `backend/data/jobradar.db`; compose sets its own Postgres URL, production points it at Neon |
| `UPLOADS_DIR` | no | `~/.jobradar/uploads`, outside the repository; compose sets `/data/uploads` |
| `MAX_UPLOAD_BYTES` | no | 10 MiB per upload |
| `STATIC_DIR` | no | `backend/app/static`, beside `app/main.py`, where the image copies the frontend's build; with no build there, `GET /` is `404` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | no exporter: spans stay in `telemetry_span` and the Telemetry screen, and no collector is called |
| `TICK_SECRET` | **yes, to start a run** | every `POST /internal/tick` is `401`: there is no default secret |
| `MCP_AUTH_TOKEN` | no | every request to the hosted MCP endpoint (`/mcp`) is `401`: the endpoint is off until an operator sets it |
| `PREFILTER_TITLE_TERMS`, `PREFILTER_SENIORITY_TERMS`, `PREFILTER_LOCATION_TERMS`, `PREFILTER_MUST_HAVE_KEYWORDS` | no | that rule rejects nothing, so a posting goes to the scorer |
| `PREFILTER_MUST_HAVE_FROM_RESUME` | no | off: the resume's parsed skills gate no posting, so the must-have rule reads `PREFILTER_MUST_HAVE_KEYWORDS` alone |
| `LLM_API_KEY` | **yes, to score** | scoring stops at the first posting; the run's row reads `failed` and names this variable, and a scanned upload answers `503` naming it |
| `LLM_MODEL` | no | `gpt-4o-mini` |
| `LLM_BASE_URL` | no | `https://api.openai.com/v1`; every model call posts to `<base>/chat/completions` |
| `LLM_FALLBACK_MODEL` | no | no fallback: only `LLM_MODEL` is tried |
| `SCORER_MAX_POSTINGS` | no | 50 postings scored per run; postings past the cap are stored not scored |
| `RUN_REQUEST_BUDGET` | no | 200 board calls per run; `0` asks no board at all and the run row says how many companies were left |
| `RUN_REQUEST_INTERVAL_SECONDS` | no | 1 second between two board calls; `0` waits nothing |
| `DIGEST_MIN_SCORE` | no | 70 |
| `SMTP_HOST`, `DIGEST_FROM`, `DIGEST_TO` | **yes, to send mail** | the digest sends nothing; a digest with a match to send fails, naming the first required variable that is unset |
| `SMTP_PORT` | no | 587, read only by a real send |
| `SMTP_USERNAME` | no | no login, which is what a local relay without authentication needs |
| `SMTP_PASSWORD` | **only when `SMTP_USERNAME` is set** | nothing is read |
| `TEST_DATABASE_URL` | no (the suite's) | the engine matrix's PostgreSQL cases **skip**; a URL that is set but unreachable **fails** the suite |
| `JOBRADAR_URL` | **yes, for the MCP tools** | every tool fails with a tool error naming it, while the server still starts and lists its tools |
| `JOBRADAR_TICK_SECRET` | **yes, for `trigger_run`** | `trigger_run` fails naming it; the value never reaches a result, an error message or a log line |

With none of them set, `docker compose up --build --wait` is still green:
`/healthz` answers `{"status":"ok"}`, a tick without `TICK_SECRET` is `401` rather
than `500`, and a tick with a secret but no model key answers `202` while its run
row reads `failed` with the missing variable named.

**Data rules.** A resume is uploaded to a running instance at runtime — never as
part of the repository — and is stored outside it, in `UPLOADS_DIR` (a volume
under compose, `~/.jobradar/uploads` otherwise). Its text is user data: it is
never logged, never returned to a client except through the app's own
`GET /resume/{id}/file`, and never committed, and neither is the database file.
The app's validation handler answers a rejected upload with `422` and drops each
error's offending input, so the file itself cannot reach an error body either.
Secrets live in `.env`, which is gitignored, and in a deployment they arrive as the
platform's environment variables (Render) and GitHub repository secrets. The names
are the table above; no value ever appears in this file.

The rule is enforced, not remembered. `backend/tests/test_committed_data_guard.py`
runs in the default suite — so `uv run pytest` fails locally on it — and as one
step of `ci.yml`'s `backend` job on every push and pull request, before the suite
starts:

    cd backend && uv run pytest tests/test_committed_data_guard.py

It fails on a tracked `.env` file, a `*.db` or `*.sqlite3` file, any path under
`uploads/` or `data/`, a resume file (`.pdf`, `.docx`, `.doc`, `.rtf`, `.odt`), and
a credential shape: a PEM private key, an AWS access key id, an `sk-`-prefixed API
key, a GitHub token, and a PostgreSQL URL carrying a password — plus a non-empty
value assigned to a `*_API_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN` or
`*_ACCESS_KEY_ID` name. It reads tracked content only, from the git index rather
than the working tree, so a gitignored local `.env`, the local database and
`~/.jobradar/uploads` are not violations; the seven throwaway values this file and
`compose.yaml` publish are allowed as exact literals; and it names the file and the
kind without ever printing the matched text. `agent-hooks/pre-commit` runs the same
guard before a commit instead of after a push — install it once per clone with
`git config core.hooksPath agent-hooks`. Which data an AI tool or an agent may see,
and where each of those rules is enforced rather than promised, is
`security/ai-tool-and-data-policy.md`.

## Deployment

The target is decided, and its reason is recorded in
`_docs/decisions/2026-10-11-deploy-and-repo.md`: a **Render free web service**
running this repository's Docker image, with **Neon Postgres** as the database.
Render's own free database is deleted 30 days after creation, which would break a
review that opens weeks after submission; Neon's free Postgres does not expire, so
the database lives outside Render, on Neon's `production` branch. That record also
holds the repository's visibility — public on GitHub, so peer reviewers can read
this file — which is why nothing private is ever committed and why no value
appears below. The deployed instance's environment variables are set in Render's
dashboard, and no value of any of them appears in this file.

The deployed image is the one `backend/Dockerfile` builds, with the **repository
root** as the build context, exactly as `compose.yaml` builds it — one container,
the UI and the API on one port ([Architecture](#architecture) is the shape of it).

A production instance sets these variables to its own real values. Their names are
all this file carries, and the [table above](#data-and-configuration) says what
each one means and what an unset one means:

    DATABASE_URL   UPLOADS_DIR   STATIC_DIR   MAX_UPLOAD_BYTES
    TICK_SECRET    LLM_API_KEY   LLM_MODEL    SCORER_MAX_POSTINGS
    LLM_BASE_URL   LLM_FALLBACK_MODEL
    RUN_REQUEST_BUDGET   RUN_REQUEST_INTERVAL_SECONDS
    PREFILTER_TITLE_TERMS    PREFILTER_SENIORITY_TERMS
    PREFILTER_LOCATION_TERMS PREFILTER_MUST_HAVE_KEYWORDS
    PREFILTER_MUST_HAVE_FROM_RESUME
    DIGEST_MIN_SCORE
    SMTP_HOST  SMTP_PORT  SMTP_USERNAME  SMTP_PASSWORD  DIGEST_FROM  DIGEST_TO

`UPLOADS_DIR` points outside the repository rather than into it: a resume is
uploaded at runtime, so the directory it lands in is not one git tracks.

**The instance is live at <https://jobradar-c75b.onrender.com>** — the only new
address this file states. What a deployed run produced — the uploaded resume, the
scored `GET /matches` rows, and the run that wrote them — is
[the demo above](#demo).

**The free tier's spin-down and cold start, as observed.** On 2026-10-17 23:36
+08, after a quiet period with no traffic, the first request failed at 60 s — the
connection dropped, which is what a spun-down instance does — and the next request
answered `200` at +62 s: a cold start of about a minute, consistent with the
15-minute idle spin-down `_docs/plan.md` records. A restart that changed an
environment variable was measured the same way on 2026-10-18 20:44 +08:
`/healthz` answered `200` in 31 s. The alert check treats exactly this as normal:
one failed read is a cold start, not an outage, and the on-call rollback waits
long enough for one.

**A resume uploaded there is stored on the instance's ephemeral filesystem.** No
persistent disk is attached to the service, so the file is written outside the
repository, is never committed and never logged, does not survive a redeploy — a
redeploy starts a fresh filesystem — and is reachable only through the app's own
`GET /resume/{id}/file` route. What that costs is named under
[Limitations](#limitations).

The remaining steps of the deployment — creating the service, attaching the
database, setting the variables — need the owner's own Render and Neon accounts.

### Rollback

A bad green `main` deploys itself: `deploy.yml` deploys the revision CI passed, so
undoing one is two moves — get production off the bad revision, then make that
permanent in git. Both need `RENDER_API_KEY` and `RENDER_SERVICE_ID` exported in
your own shell; no value of either and no service id is written in this file.

1. **Put the last known-good commit back.** `ops/rollback.sh` reads Render's
   newest 20 deploys, takes the `commit.id` of the newest `live` deploy as the
   commit being served, and picks as its target the newest deploy that is both
   another commit and one whose build succeeded — status `live`, or `deactivated`
   for a superseded deploy. It prints those two shas, deploys the target with the
   same call `deploy.yml` makes, and exits 0 only once that deploy is `live`
   (5 minutes to be listed, 15 to `live`, the bounds `deploy.yml` documents); a
   deploy that ends `canceled`, `deactivated`, `build_failed`, `update_failed` or
   `pre_deploy_failed`, or one that never gets there, exits non-zero naming the
   status. It deploys no git ref, no branch tip and no sha you pass in: the target
   is a commit Render itself lists as a successful deploy of this service, because
   an untested revision is what this path exists to avoid.

   `ops/rollback.sh --dry-run` makes the same selection, prints the two shas and
   sends no deploy request — that is how you see which commit the rollback would
   deploy without touching the service. By hand, with `<target>` the sha the dry
   run printed, the read first — `GET /services/{id}/deploys?limit=20`, newest
   first, each item wrapped as `{"deploy": {"id", "status", "commit": {"id"}}}`:

       curl -sS --header "Authorization: Bearer ${RENDER_API_KEY}" \
         "https://api.render.com/v1/services/${RENDER_SERVICE_ID}/deploys?limit=20"

   and then the deploy — `POST /services/{id}/deploys` with that commit as the
   body:

       curl -sS --request POST \
         --header "Authorization: Bearer ${RENDER_API_KEY}" \
         --header "Content-Type: application/json" \
         --data '{"commitId": "<target>"}' \
         "https://api.render.com/v1/services/${RENDER_SERVICE_ID}/deploys"

   That POST answers an empty body and no `Location` header, so the deploy it
   starts names no id to poll: it is found in the next read of the list, which is
   what the script waits on. A sha that is not in that list gets a refusal rather
   than a deploy — the script reads the newest 20 and refuses instead of paging.

2. **Require the instance to serve.** A `live` deploy is not a working app, so the
   rollback is done only when `GET {JOBRADAR_URL}/healthz` answers 200:

       curl -sSf "${JOBRADAR_URL%/}/healthz"

   Retry it for a few minutes before calling it a failure: a free instance spins
   down after 15 minutes idle and takes about a minute to wake, so the first
   request after a deploy can be slow rather than wrong.

3. **Make it permanent in git.** `git revert <bad sha>`, push, and the revert
   reaches production the way every other commit does: it is a new commit, CI runs
   it, and `deploy.yml` deploys it. Without this step the next green `main`
   deploys the bad revision again, because the rollback changed what Render serves
   and not what `main` holds. A bad deploy that was a merge commit needs
   `git revert -m 1 <sha>`, which keeps the first parent — the branch that was
   merged into — and drops the merge's other side.

## Architecture

Six pieces. The rest of this file is the detail behind them.

```mermaid
flowchart LR
  UI["React UI<br/>served by the API at /"] -->|same origin| APP
  MCP["MCP client<br/>stdio in mcp/ · HTTP at /mcp"] -->|seven bounded tools| APP
  CRON["GitHub Actions<br/>tick.yml: one POST a day"] -->|X-Tick-Secret| APP
  APP["One FastAPI container<br/>the API, the frontend's build, and the run row"] --> DB[("SQLite locally<br/>Postgres in compose<br/>Neon in production")]
  APP -->|"one call per posting"| LLM["LLM gateway<br/>LLM_BASE_URL"]
  APP -->|"board JSON by slug"| BOARDS["Greenhouse / Lever"]
  APP -->|"digest above DIGEST_MIN_SCORE"| SMTP["SMTP"]
```

1. **One container and no second service.** `backend/Dockerfile` builds a single
   image holding the API and the frontend's build; `compose.yaml` runs that image
   and the Postgres it writes to, and nothing else. There is no worker process to
   deploy, start or supervise, because the poller is an endpoint (3).
2. **The frontend's build is served by the backend, as static files.** The
   Dockerfile's first stage runs the frontend's own `npm ci && npm run build` and
   copies the result into the runtime image at `app/static`; `app/main.py` mounts
   that directory at `/`, after every API router, so `/` is the UI and the API
   keeps its own paths. One origin, one port, one URL: no CORS and no proxy. When
   no build is present — a clone, the suite, `uv run uvicorn` without the image —
   no mount is registered and the app is the API alone.
3. **The poller is `POST /internal/tick`, an endpoint rather than a separate
   worker.** It carries the shared secret in `X-Tick-Secret`, compared against
   `TICK_SECRET`; with that variable unset every tick is `401`, and a run already
   in progress is `409`. The UI's own two run controls are admitted by
   same-origin instead (see [Limitations](#limitations)). A process-local lock is
   the whole concurrency rule, because the poller lives in the one process the one
   container runs.
4. **SQLAlchemy over SQLite locally and Postgres in production.** The models in
   `backend/app/models.py` are the only schema and `backend/app/db.py` builds the
   engine from `DATABASE_URL`, where unset means the local SQLite file
   `backend/data/jobradar.db`. The same models, constraints and tests run on both
   engines, so the two places they genuinely differ — the SQLite pragmas and the
   partial unique index over active resumes — carry a clause per dialect rather
   than a branch per caller. `compose.yaml` points the container at its own
   Postgres; production points the same variable at Neon.
5. **The `MatchScorer` seam.** `backend/app/matching/prefilter.py` decides first,
   with no network and no model: title, seniority, location and must-have terms
   read from the `PREFILTER_*` variables, and a posting it rejects is stored and
   never asked about. Survivors go through `MatchScorer.score(posting,
   resume_text)` — one method, answering `MatchResult(score, reason, gaps)` —
   which `LlmScorer` implements as one model call per posting, bounded by
   `SCORER_MAX_POSTINGS`. A call the model never answered comes back as a
   not-scored result instead of an exception, so it reaches the run row rather
   than a traceback. The tests substitute a `FakeScorer` through
   `app.dependency_overrides`, so the suite needs no API key and no network.
6. **The run row.** `POST /internal/tick` writes a `run` row in `running` and
   answers `202` at once; the work continues off the request thread, writing
   `postings_seen` and `matches_created` as it earns them, and its end writes
   `succeeded` or `failed` with the failure's own message. The UI reads that row
   through `GET /runs/{id}` for progress, and the row's `trace_id` links it to its
   trace. The reason for the shape is a constraint, not a preference: a tick that
   scores many postings must not outlive an HTTP timeout
   (`_docs/decisions/2026-10-11-deploy-and-repo.md`), and the measured cost of one
   is [above](#evaluation).

**Technologies**, and the seam each talks through — the six pieces above are the
architecture; this is what each is built from:

- **FastAPI** — the API, and the server of the frontend's build: one app object in
  `backend/app/main.py`, one router module per resource in `backend/app/routes/`,
  `uvicorn` as the server.
- **React + TypeScript + Vite** — the UI, in `frontend/`, with every backend call
  funnelled through `frontend/src/api/client.ts`.
- **SQLAlchemy over SQLite and Postgres** — storage, one set of models on both
  engines.
- **Docker + `compose.yaml`** — packaging and the local whole-system path.
- **GitHub Actions** — CI, deployment, the daily tick, the hourly alert and the
  on-call agent, in `.github/workflows/` (see [CI/CD](#cicd) and
  [Monitoring](#monitoring)).
- **The committed contract, `backend/openapi.json`** — 16 paths as of this commit,
  generated from the FastAPI app rather than written by hand; a route change that
  was not exported is caught by the suite and by CI's `contract` job.
- **The MCP server in `mcp/`** — a uv project, `jobradar-mcp`, that drives this app
  over HTTP through seven bounded tools, in a process of its own over **stdio**,
  and — since the hosted endpoint was added — over **MCP streamable HTTP**,
  mounted into the app's own process at `/mcp`. Keeping the tools in a separate
  project was originally so the `mcp` dependency never entered the backend's tree
  or the deployed image; serving the endpoint from the one container reversed
  that, and the tools stayed one implementation rather than a copy
  (`_docs/decisions/2026-10-14-hosted-mcp.md`).

### The MCP server

Seven tools, each one call to an endpoint this app serves today:

| Tool | Arguments | Returns |
| --- | --- | --- |
| `add_company` | `name: string`, `slug: string` (both required), `source: string` (`"greenhouse"` or `"lever"`, optional — Greenhouse when absent) | the app's `201` body: `id`, `name`, `slug`, `is_active`, `source` |
| `list_watchlist` | none | every company, ascending id, active or not: the same five fields |
| `list_matches` | `min_score: integer`, `state: string` (`"new"`, `"saved"` or `"dismissed"`), `company_id: integer`, `limit: integer` (all four optional, the route's own query parameters) | the rows the filters leave, newest first: `id`, `company_name`, `title`, `url`, `score`, `reason`, `gaps`, `state`, `outcome`, `created_at` |
| `trigger_run` | none | the app's `202` body — the run row as `GET /runs/{id}` renders it (12 fields, including `trace_id` and `company_outcomes`; `backend/openapi.json` is the field list) |
| `get_run` | `run_id: integer` (required) | that run's row, the same 12 fields |
| `remove_company` | `company_id: integer` (required) | a confirmation this tool builds: `id`, `removed` — the route answers `204` with no body |
| `set_match_state` | `match_id: integer`, `state: string` (`"saved"` or `"dismissed"`, both required) | the app's `200` body: the written row, the same fields `list_matches` renders |

Each tool returns the app's own response body, field for field and name for name:
nothing is filtered, renamed or derived here. `list_matches` applies no filter of
its own: with no argument it answers every row, so a prefiltered one (`score` 0)
and an unscored one come back exactly as stored — keeping either out of a digest
or a screen is the app's business. A list is rendered as one JSON block per row,
because that is how the SDK renders a returned list; anything else is one JSON
block. Every failure a client can see is a tool error (`isError`) carrying the
app's own `detail` text, never a hang, never a silent success and never a
traceback.

**Over stdio** the server reads exactly two variables and nothing else — no model
key, no database URL, no SMTP variable, no upload directory and no digest rule:
`JOBRADAR_URL` (the app's base URL) and `JOBRADAR_TICK_SECRET` (the value sent as
`X-Tick-Secret`: the same value the app reads from `TICK_SECRET`). Either one unset
or blank is a tool error naming that variable — the server still starts and lists
its tools, and the tools that do not need the missing one still answer. The secret's
value never reaches a result, an error message or a log line. A proxy taken from
the machine's own settings does not stand between the client and the app, which is
what keeps the loopback demo above working on a machine that runs one.

**Over the network**, the same seven tools are served over MCP **streamable HTTP**
from the app's own container and origin — one process, one port, no second service
and no CORS — so a hosted MCP client reaches them at the app's base URL. `app/mcp.py`
serves them at `/mcp` (with or without the trailing slash), and they are the same
tool functions `mcp/` serves over stdio, not a copy. The endpoint adds no route to
the API contract, so `backend/openapi.json` is unchanged by it.

The endpoint is guarded by a bearer token and is **off by default**. The operator
sets one variable on the instance — `MCP_AUTH_TOKEN` ([the table](#data-and-configuration);
on Render it is one more environment variable) — and gives its value to whoever may
use the endpoint. From then on every request must carry
`Authorization: Bearer <the value of MCP_AUTH_TOKEN>`:

    https://<the app's host>/mcp

The token is read from the environment **per request**, so rotating it needs no
restart (verified: with `MCP_AUTH_TOKEN` unset every request to `/mcp` is `401`).
The value never appears in the browser, the repository, a served asset, a log line
or a response body; the UI's API module does not call this path.

The endpoint's own tools drive this app over HTTP, so the process also needs the
two variables the stdio server reads: `JOBRADAR_URL` — the app's own base URL,
which inside the container is `http://127.0.0.1:8000` — and
`JOBRADAR_TICK_SECRET`, the value the app reads as its tick secret. `compose.yaml`
sets both, so a hosted client's `trigger_run` reaches the app's own tick guard; a
deployment sets the same two where it starts the app. The transport streams its
answers (SSE), so it needs a streamable-HTTP MCP client — the SDK's own, or any
other — rather than a plain `curl`.

**Run it, and register it.** An MCP client starts this command for you:

    cd mcp && uv sync
    cd mcp && uv run python -m jobradar_mcp

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

## Project structure

```
JobRadar/
├── backend/                     FastAPI app, its suite, the benchmark, the migrations
│   ├── app/
│   │   ├── main.py              app object: routers, UI mount, MCP endpoint, telemetry, lifespan
│   │   ├── models.py            the eight tables — the only schema declaration
│   │   ├── db.py                DATABASE_URL → engine/session; init_db()
│   │   ├── telemetry.py         trace id, spans, minute buckets, the three reads
│   │   ├── mcp.py               the hosted /mcp endpoint and its bearer guard
│   │   ├── routes/              one module per resource: resume, watchlist, internal, runs, matches
│   │   ├── services/            pipeline, ingest, digest, resume
│   │   ├── sources/             greenhouse.py, lever.py, SOURCES
│   │   └── matching/            prefilter.py, scorer.py, the three prompt files
│   ├── benchmarks/tick_benchmark.py      one measured tick, never collected by pytest
│   ├── migrations/versions/     0001…0015; head is 0015_metric_bucket
│   ├── tests/                   the unit suite; tests/integration/ is the marked one
│   ├── Dockerfile               build stage: the frontend; runtime: python:3.12-slim + uv
│   ├── docker-entrypoint.sh     alembic upgrade head, then exec the server
│   ├── openapi.json             the committed contract
│   └── alembic.ini, pyproject.toml, uv.lock
├── frontend/                    React + TypeScript UI, built by Vite, tested by vitest
│   └── src/{api,screens,mocks,test}
├── mcp/                         the MCP server as a uv project, its suite, demo_client.py
│   └── jobradar_mcp/{tools,client,hosted}.py
├── ops/
│   ├── rollback.sh              deploy the last known-good commit
│   └── oncall/                  impact_check, notifier, responder, proposer, recheck + their tests
├── security/                    the scan, the change audit, the extension notes, the data policy
├── docs/                        the agent-extension pack and the permission notes
├── agent-hooks/pre-commit       runs the committed-data guard before a commit
├── _docs/                       plan, process, tasks, performance, task-template, decisions/, team/
├── .github/workflows/           ci.yml, deploy.yml, tick.yml, alert.yml, oncall.yml
├── scripts/test.sh              starts the throwaway PostgreSQL and runs every suite
└── compose.yaml, Makefile, .env.example
```

## Repository layout

The rubric's expected-contents list names a few things this repository keeps
under other names. One row each, with the path it lives at here beside it; every
path below exists, and `backend/tests/test_repo_layout.py` fails naming any that
does not.

| The rubric lists | Here it is | Why |
| --- | --- | --- |
| docs/ | `_docs/` | `_docs/` is the plan, the process and the team prompts, read by people only; the root `docs/` is the agent-extension pack. |
| product-spec.md | `_docs/plan.md` | The same document: the behaviour spec, the non-goals and the constraints. |
| openapi.yaml | `backend/openapi.json` | The OpenAPI specification, exported from the app rather than written by hand. |
| docker-compose.yml | `compose.yaml` | The same file, which Compose v2 reads by that name; no command changes. |
| mcp-server/ | `mcp/` | The MCP server, a uv project of its own, named for MCP already. |
| security/ | `security/` | The security scan, the change audit, the extension notes and the data policy. |
| ops/ | `ops/` | The two written-up incidents, each with the test that pins it. |

## Decisions and trade-offs

The choices that shaped the project, each with the cost that came with it. The
full records are `_docs/decisions/`.

- **One container with the poller as an endpoint, over a separate worker.** A
  worker is a second thing to deploy, start and supervise; an endpoint in the one
  process needs no scheduling infrastructure. The downside is that a run cannot
  outlive its process and that the concurrency rule is process-local rather than
  shared — accepted because one user and one container are the whole deployment
  (`_docs/decisions/2026-10-11-deploy-and-repo.md`).
- **Render's free service plus Neon Postgres, over Render's free database.**
  Render deletes its free database 30 days after creation, which would break a
  review that opens weeks later. The cost is a second vendor and a connection over
  the public internet; accepted because the database then does not expire
  (`_docs/decisions/2026-10-11-deploy-and-repo.md`).
- **The in-app "Run digest now" button as the reliable trigger, with GitHub
  Actions cron as a convenience.** Actions cron drifts by minutes to hours, can
  skip a run and is disabled after 60 days of inactivity, so the schedule cannot be
  the thing a demo relies on. The cost is that a real daily digest needs the owner
  to open the app; accepted because the owner is the only user
  (`_docs/decisions/2026-10-11-deploy-and-repo.md`).
- **SQLite locally with Postgres in production, one set of models.** Anything else
  is two schemas to keep in step. The cost is a dialect clause in the two places
  the engines genuinely differ — the pragmas and the partial unique index — and a
  test matrix that runs both engines rather than one; accepted because the local
  path then needs no database to be installed.
- **LLM scoring per posting, no embeddings or vector store.** A vector store is
  infrastructure and an embedding model is a second dependency, to rank postings
  that number in the tens per company. The cost is one model call per surviving
  posting, bounded by `SCORER_MAX_POSTINGS`, and no semantic matching; accepted
  because the prefilter removes most postings for free, and the measured app-side
  cost of the rest is milliseconds ([Evaluation](#evaluation)).
- **A separate MCP project, then the endpoint hosted in the same app.** The tools
  stayed one implementation while the `mcp` dependency moved into the backend's
  tree and the deployed image. The cost is that the dependency isolation is gone;
  accepted because one container serving API, UI and MCP is the whole point of the
  architecture (`_docs/decisions/2026-10-14-hosted-mcp.md`).
- **The rubric's CI/CD third tier declined: no dev environment, no manual
  production deploy.** A second service and a second database exist to be reviewed
  rather than used when there is one user and one environment, and a manual
  production deploy adds a path where a failing test does not block a deploy. The
  cost is that the top CI/CD line is not claimed; the condition for revisiting is
  in `_docs/decisions/2026-10-18-ci-cd-third-tier.md`.
- **Greenhouse and Lever only.** `backend/app/sources/greenhouse.py` and
  `backend/app/sources/lever.py` each read a company's public board JSON by slug,
  which needs no key and no account, and `app/sources/__init__.py`'s `SOURCES`
  resolves a stored source to its adapter. Three others were considered and left
  out, each deliberately: **LinkedIn** and **Indeed** are partner-gated and
  scraping them breaches their terms; **Workday and the other enterprise ATS
  providers** publish XML feeds that are messier in shape, so one is a third
  adapter rather than a flag on the two that exist. The consequence of the choice
  is in [Limitations](#limitations).

## CI/CD

Five workflow files in `.github/workflows/`, each file's leading comment naming
the local command that stands in for every job it runs, so the pipeline can be
reproduced without GitHub Actions.

**`ci.yml`** runs on every push and every pull request, with
`permissions: contents: read`, in five jobs:

| Job | What it runs |
| --- | --- |
| `backend` | `uv sync`, then the committed-data guard as its own step, the security scan as its own step, the on-call offline suite, and `make test-backend` against its own `postgres:17` service container with `TEST_DATABASE_URL` naming it — one pass over both engines, failing on a summary that skipped anything |
| `frontend` | `npm ci`, `npm run build`, `make test-frontend` |
| `integration` | `make test-integration` against a `postgres:17` service container |
| `contract` | `uv run python -m app.export_openapi`, then `git diff --exit-code -- backend/openapi.json` — a stale or hand-edited contract fails with the diff printed |
| `mcp` | `uv sync`, `make test-mcp`, failing on a skipped case |

The `backend` job's two extra steps are the repository's own checks, each on its
own so a verdict reads without the suite's output:

    cd backend && uv run pytest tests/test_committed_data_guard.py
    uv run --project backend ruff check --select S --output-format concise backend/app mcp/jobradar_mcp

`ruff`'s `S` rules are flake8-bandit's checks; they need no network and no key,
and `ruff` is a backend dev dependency, so the image never carries it. The seven
findings this tree had are dispositioned at their own sites, so the step is green
today (verified) and a *new* finding — a hardcoded password, an `xml` parse of
untrusted data — turns it red. The scope and the dispositions are
`security/scan.md`.

There is no deploy step in `ci.yml`: a green `main` is what deploys, and the four
suite jobs reach their suite through the `Makefile`'s per-suite targets
(`make test-backend`, `make test-integration`, `make test-frontend`,
`make test-mcp`) so the commands are spelled in one file rather than two
(`backend/tests/test_ci_make_parity.py` fails if they drift).

**`deploy.yml`** deploys the revision CI passed to the Render service. It starts
from `ci.yml`'s completion (`workflow_run`, branches `main`) — not from `push`,
which would race the tests — and its job admits only a `conclusion == 'success'`
run of a `push` to `main`, so a red or cancelled CI run leaves the job skipped and
no request reaches Render. The credential is `RENDER_API_KEY` plus
`RENDER_SERVICE_ID` and the URL is `vars.JOBRADAR_URL`; all three are checked
first, and an unset one is a red job naming it rather than a silent skip. Under
`workflow_run`, `github.sha` is the tip of `main` when the workflow starts, which
is not necessarily the sha CI tested, so the job logs both and deploys
`github.event.workflow_run.head_sha`, failing if no deploy of that commit appears.
`POST /services/{id}/deploys` answers an empty body and no `Location` header, so
the deploy is found by filtering `GET .../deploys?limit=20` on `commit.id` (5
minutes allowed), then polled by its own id to `live` (15 minutes allowed; a
deploy that ends `canceled`, `deactivated`, `build_failed`, `update_failed` or
`pre_deploy_failed` fails the job at once), and only then is
`GET {JOBRADAR_URL}/healthz` required to answer `200` within 5 minutes. The job is
capped at 30 minutes. "Exactly one deploy per green `main`" also needs autodeploys
turned off on the Render service — a setting this file cannot see, recorded here
because it cannot be enforced from the repository.

**`ops/rollback.sh`** is the way back: [Rollback](#rollback) above.

**`tick.yml`** calls `POST /internal/tick` once a day (`0 13 * * *`, plus a manual
dispatch), taking the secret from the repository secret `TICK_SECRET` and the base
URL from the repository variable `JOBRADAR_URL`. No secret value is in the file.
While either input is unset, empty or whitespace-only the job emits a `::notice::`
naming the missing one and skips the call, so the daily schedule stays green
instead of failing; a configured repository runs the same call unchanged and still
fails loudly on a non-2xx answer. The local equivalent, with the app on port 8123
and a throwaway secret:

    cd backend && TICK_SECRET=throwaway-local-secret uv run uvicorn app.main:app --port 8123
    curl --fail-with-body --silent --show-error --max-time 60 \
      --request POST --header "X-Tick-Secret: throwaway-local-secret" \
      http://127.0.0.1:8123/internal/tick
    curl --silent --show-error --max-time 60 http://127.0.0.1:8123/runs/1

The endpoint fails a wrong or absent secret with `401`, a run already in progress
with `409` and an unrecorded run with `500`; it answers `202` while the run
continues in the background, so the second `curl` is where that run's outcome is
read — and the run's outcome is not this job's business. GitHub Actions cron drifts
by minutes to hours and is disabled after 60 days of repository inactivity, so a
late or missed run is expected rather than a fault; that is why the in-app button
is the reliable path.

`alert.yml` and `oncall.yml` are the monitoring and response half, and are
described under [Monitoring](#monitoring). The YAML itself is validated by

    python3 -c 'import yaml, sys; [yaml.safe_load(open(path)) for path in sys.argv[1:]]' .github/workflows/*.yml

and, with Actions' own schema, by
`docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest
.github/workflows/*.yml`. Actions are pinned to released versions and never to a
branch; setup-uv carries its full version because it publishes no moving major tag
above `v7`. `deploy.yml` names `ci.yml`'s workflow `CI` as a literal, so renaming
that `name:` would stop it from ever running.

## Security, audit and DevOps artifacts

Five artifacts under `security/` and `ops/`, each the output of a command or of an
incident rather than a statement of good practice.

- `security/scan.md` — the security scan: `ruff`'s `S` rules, which are
  flake8-bandit's checks, over the shipped Python. It is one step of `ci.yml`'s
  `backend` job, it needs no network and no key, and its findings and their
  dispositions are the file. Rerun it from the repository root:

      uv run --project backend ruff check --select S --output-format concise backend/app mcp/jobradar_mcp

- `security/change-audit-committed-data-guard.md` — a change audit of the
  committed-data guard, re-run for the artifact rather than quoted. Its trail is
  issue comments, because there is no pull request to audit:
  `gh pr list --state all --json number` returns `[]`, so this repository's review
  path is the issue's groom comment, its implementation comment, its QA verdict,
  the commits and the CI run that carried them.
- `security/agent-extension-notes.md` — the blast radius of every agent surface
  here: the committed-data guard, the hosted MCP endpoint, the two run routes, the
  stdio server and this repository's own subagents, each with the file that shows
  it and with its limit written down.
- `security/ai-tool-and-data-policy.md` — which data an agent here may see and
  which it may not, and where each of those rules is enforced.
- `ops/diagnosis-mcp-path-405.md` and `ops/diagnosis-run-press-401.md` — two
  failures that really happened here, each with its symptom, evidence, root cause
  and the test that now pins it. Run those two pins:

      cd backend && uv run pytest "tests/test_mcp_hosted.py::test_the_documented_path_reaches_the_transport_with_the_ui_mounted" "tests/test_internal_tick.py::test_a_same_origin_press_with_no_secret_is_202_and_starts_the_work" "tests/test_internal_tick.py::test_with_no_configured_secret_only_the_instance_s_own_page_is_admitted"

## How this repository was built, with AI

An AI agent writes this repository in bounded steps, and the one decision it could
not settle itself it handed to another model. The files that make both true are
committed, and the six pieces a reviewer looks for are indexed in
[`docs/agent-extension-pack.md`](docs/agent-extension-pack.md).

### The workflow the repository defines

`AGENTS.md` is what an agent reads first and `_docs/process.md` is the rule book;
`_docs/team/` holds one prompt per role, written to the template in
`_docs/task-template.md`. Together they say:

1. **Grooming first.** A task is a GitHub issue, and it is groomed before anyone
   implements it: the PM (`_docs/team/pm.md`) rewrites it against
   `_docs/task-template.md` — goal, acceptance criteria that can be checked by
   looking at the result, out of scope with a link per moved item, constraints.
2. **One issue at a time, lowest number first.**
3. **The engineer implements it.** `_docs/team/software-engineer.md`: implement
   what the issue describes, do not change the acceptance criteria, write tests for
   the new behaviour, commit regularly, do not close the issue, and leave a comment
   saying what was done.
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
  at the build: seven bounded tools over the app, so an AI client can add a
  company, list the watchlist, list matches, start a run, read that run, remove a
  company and set a match's state.

### Two answers were escalated, not accepted

Jev answers each question with a confidence score, and the verdict file marks the
answers that came back below the bar as escalations rather than decisions. Two
were: the digest trigger, and whether the single-container poller is sound. The
decision record presents both as **priors, not decisions the model made**, and
records how each was resolved: the in-app "Run digest now" button is the reliable
trigger with the scheduled workflow as a best-effort convenience, and the
single-container poller stands, with the run row as the constraint it implies.

## Limitations

Each with what it costs, because a limitation without a consequence is a slogan.

- **Greenhouse and Lever are the two sources.** LinkedIn and Indeed are excluded on
  their terms and Workday is not attempted. A company whose board is on neither
  cannot be watched at all: not watched badly, not watched. If the watchlist ever
  needed one of them, the change is a third adapter behind `SOURCES`.
- **One user, no accounts and no sharing.** There is no login and nothing scoped to
  a user: the watchlist, the resume and the matches belong to the instance, so two
  people cannot use one instance without sharing all three.
- **The run buttons are admitted by same-origin, not by a login.** With no login,
  whoever can reach the URL can press both **"Run digest now"** and **"Re-match
  stored postings"**: one press spends at most `SCORER_MAX_POSTINGS` model calls of
  the owner's key, and a digest press also sends mail. Both routes admit a press
  whose `Origin` names the request's own `Host` — an admission rule shaped like a
  CSRF guard, not a credential, because a non-browser client can send any `Origin`
  it likes. Scripts, the scheduled Actions workflow and `mcp/` keep using
  `X-Tick-Secret`.
- **No embeddings and no vector store, so a match is one LLM call per posting**,
  capped for a single run by `SCORER_MAX_POSTINGS` (default 50). Cost and latency
  grow with the size of the watchlist, and postings past the cap in one run are
  stored with a not-scored reason rather than scored later. The condition for
  revisiting it is stated in `_docs/plan.md`: only if match quality proves
  insufficient.
- **Match quality itself is not evaluated.** There is no labelled set and no
  accuracy figure, so nothing in this repository says how good a score is — the
  suite proves the pipeline, the storage and the failure paths, and
  [Evaluation](#evaluation) says exactly what is and is not measured. Revisiting it
  needs ground-truth labels from a second reader, which one user cannot supply.
- **A landed digest is grouped, not per posting** — every match above the cutoff
  travels in one message, so a run that finds twenty matches sends one. Instant or
  per-posting alerts are a non-goal in `_docs/plan.md`.
- **The deployed instance's uploads do not survive a redeploy.** No persistent disk
  is attached, so the resume file is gone after a redeploy while the row that names
  it stays in Neon: `GET /resume/{id}/file` then answers `404` for a resume the app
  still lists, until a new upload. A deployment that must keep the file needs a
  Render disk, which the free plan does not offer.
- **There is no client-side router.** Deep links to a screen do not exist: a path
  the build does not hold is the mount's `404`. Adding one is a red test rather
  than a silent change (`backend/tests/test_static_ui.py`).
- **The scheduled tick is best-effort.** GitHub Actions cron drifts by minutes to
  hours, can skip a run entirely, and is disabled after 60 days of repository
  inactivity, so a late or missing digest is expected rather than a fault. That is
  why the in-app **"Run digest now"** button is the reliable path, and why the demo
  uses it (`_docs/decisions/2026-10-11-deploy-and-repo.md`).
- **The free tier sleeps.** A Render free instance spins down after 15 minutes idle
  and takes about a minute to wake, so the first request after a quiet period is
  slow. That is the free tier's documented behaviour, the deployed instance was
  observed doing it, and the numbers are under [Deployment](#deployment).
- **`downgrade base` is destructive.** It drops the eight tables and every row in
  them, which is acceptable only while the databases in existence are disposable:
  the local SQLite file and the compose volume this file calls a throwaway.

## Future work

Work that is genuinely open is tracked in GitHub issues:

- **Record the first real on-call runs.** The responder, the proposer and the
  scheduled re-check are implemented and their offline suites pass, but an
  `issues`-event workflow and a `schedule` only fire once `oncall.yml` is on the
  default branch. The first real wake and the first real proposal are recorded on
  [#96](https://github.com/Xue-Zhiming-Bruce/jobradar/issues/96), and the first
  scheduled re-check on
  [#97](https://github.com/Xue-Zhiming-Bruce/jobradar/issues/97), including each
  run's URL and whether the effect it claims is visible in the app.
- **The frontend has had no visual design pass.** The screens are centred,
  unstyled sections with default widgets — functional and tested, but designed by
  nobody. Tracked by
  [#114](https://github.com/Xue-Zhiming-Bruce/jobradar/issues/114).
- **A re-check can only be started by the schedule.** GitHub throttles and skips
  cron runs, so there is no way to ask for a re-check on demand; a
  `workflow_dispatch` trigger on `oncall.yml` is tracked by
  [#115](https://github.com/Xue-Zhiming-Bruce/jobradar/issues/115).

The non-goals above are deliberate boundaries, recorded in `_docs/plan.md` and in
[Limitations](#limitations), and no issue tracks them because they are not planned;
a new task starts as a groomed issue, and the process for that is
[above](#the-workflow-the-repository-defines).
