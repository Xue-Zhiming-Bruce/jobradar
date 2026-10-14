# Decision record — Neon: project link and connection topology

Date: 2026-10-14
Decided by: the orchestrator, following the Neon agent skill
(`neon`, `neon-postgres`) and its pooling guidance.

## What was set up

Neon CLI 8.4.4, installed globally and signed in over OAuth. Linked project
`polished-star-02020119` in org `org-little-dream-31969961`, branch
**`production`**, service **Postgres** only (`neon init --services postgres
--no-agent-setup`). `neon.ts` is committed as the branch config and
infrastructure-as-code; `.neon` holds the org/project/branch IDs and is
git-ignored; `neon env pull` wrote the branch's variables to `.env.local`,
which `.gitignore` already excludes.

The Neon agent skill is installed for pi at
`~/.pi/agent/skills/neon/SKILL.md` and `~/.pi/agent/skills/neon-postgres/SKILL.md`,
and the Neon MCP server is registered in `~/.pi/agent/mcp.json`
(`https://mcp.neon.tech/mcp`, OAuth, 114 tools). Neither is committed here:
they are the development machine's configuration, not the repository's.

## The decision

Neon issues two connection strings for one database, and they are not
interchangeable:

| String | Host | Used for |
| --- | --- | --- |
| `DATABASE_URL` | `…-pooler…` | application query traffic |
| `DATABASE_URL_UNPOOLED` | direct | schema migrations and other session-level work |

The Neon skill is explicit that Alembic must use the **direct** URL: the
pooled endpoint is PgBouncer in transaction mode, which does not carry
session state, and a migration over it can fail in a way that never names
pooling.

**This repository's image runs `alembic upgrade head` at startup**
(`backend/docker-entrypoint.sh`, #47) and it reads `DATABASE_URL` through the
app's own `app.db.database_url()`. So the deployment sets a single variable,
and whichever string it names is used for both the boot migration and query
traffic.

**Decision: the deployment sets `DATABASE_URL` to the direct (unpooled)
string.** One container on Render's free plan, one SQLAlchemy pool, one
connection string, and no pooler in the migration path — the correctness the
skill asks for, with no code change and no second variable. The cost is that
application traffic is unpooled; with a single long-running instance and a
pool of a few connections that is well inside Neon's limits.

The upgrade path, if connection count ever matters: keep `DATABASE_URL`
pooled for traffic and give the boot migration its own direct string. That is
a code change — a second variable read by `app/db.py`, its line in
`.env.example`, and the test that compares `.env.example` with what the app
reads — and it buys nothing at one instance.

## Evidence

- Migrations applied to the `production` branch with the **direct** string:
  all twelve revisions, `alembic_version` = `0012_run_rematch_cursor`.
- The app run locally against the **pooled** string: `GET /healthz` 200,
  `GET /` 200 (SPA served), `POST /watchlist` 201, `GET /watchlist` read the
  row back, `POST /internal/tick` 202 and a `run` row written; an independent
  psycopg connection then read those rows out of Neon (Postgres 18.6) and the
  probe rows were deleted, leaving the schema and no data.
- Prepared statements: the engine is built with no `connect_args`, so
  psycopg3's default `prepare_threshold` is active. Against the pooler, 480
  queries over 60 fresh connections produced no error, so the default is safe
  with Neon's current pooler. If a `prepared statement … already exists` error
  ever appears, the one-line fix is `connect_args={"prepare_threshold": None}`.

## Not decided here

The deployment itself (#16), the live evidence in the README (#46), and the
deploy-on-pass workflow (#45). This record covers only how the database is
reached.
