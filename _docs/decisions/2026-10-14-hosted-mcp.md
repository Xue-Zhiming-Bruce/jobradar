# Decision record — hosted MCP endpoint: one container, a bearer token, off by default

Date: 2026-10-14
Decided by: the owner, as option A of issue #43's open question ("bearer token
from an environment secret, per request, off by default"); recorded by the
software engineer implementing #43.

## Why this file exists

`#17` built the MCP server as a project of its own and recorded one reason for the
separation: "the `mcp` dependency never enters `backend/`'s tree or the deployed
image." `#43` serves the same tools over the network from the app's one container,
which reverses that consequence. A reversal that is not written down is a silent
one, so the four choices below are recorded together, as issue #43 requires.

## What was decided

**One container, one origin.** The MCP endpoint is mounted into the existing
FastAPI app at `/mcp` (`backend/app/main.py` mounts `backend/app/mcp.py`, which
wraps `jobradar_mcp.hosted.build_http_app`), so the one process and one port that
serve the API and the UI also serve MCP. No second service, no second exposed
port, no reverse proxy and no CORS is added; `docker compose config --services`
still lists only `api` and `db`. The transport is `MCPServer.streamable_http_app()`
from the pinned `mcp>=2.3,<3.0` — the same SDK `mcp/` uses, not a second MCP
implementation.

**A bearer token, read per request.** Every request must carry
`Authorization: Bearer <MCP_AUTH_TOKEN>`. The value is read from the environment
when the request is handled, not at import, so a deployment rotates it by setting
the variable — the shape `POST /internal/tick` already uses for `TICK_SECRET`.
It is compared with `hmac.compare_digest`, never with `==`, and it is never
logged, echoed in a detail, written to a response body, bundled into the frontend
or committed.

**A variable of its own: `MCP_AUTH_TOKEN`.** Not `TICK_SECRET`, because the
endpoint grants more than a tick: it can add and remove companies, set a match
verdict, read the watchlist and the matches and start a run. A separate token
keeps the two powers independently revocable. The name is documented in
`.env.example` (the name only) and in `README.md`'s Configuration table.

**Off by default.** With `MCP_AUTH_TOKEN` unset or blank the endpoint refuses
every request with `401`, and a missing, blank or wrong token is `401` too. A
deployment that does not configure the token exposes no working MCP surface, so
making the endpoint reachable is a deliberate act. (The refusal is one status for
all three so a probe cannot tell them apart.)

## The dependency reversal, in full

Mounting the MCP ASGI app in the app's process means the process imports
`mcp` and `jobradar_mcp`. Consequences, all intended:

- `backend/pyproject.toml` declares `mcp>=2.3,<3.0` and, through
  `[tool.uv.sources]`, `jobradar-mcp = { path = "../mcp", editable = true }`. The
  tools live under `mcp/` and are installed into the backend's environment rather
  than copied, so `mcp/` stays the single implementation (`AC2`/`AC6`).
- `backend/Dockerfile` copies `mcp/pyproject.toml` and `mcp/jobradar_mcp` into the
  runtime image (at `/mcp`, where the path dependency resolves), so the deployed
  image carries the tool source and the SDK.
- Editing `backend/pyproject.toml`'s lock for this generates
  `mcp/jobradar_mcp.egg-info/`; `.gitignore` covers `*.egg-info/` so that build
  metadata never enters the public repository.
- `mcp/`'s own project, its stdio entry point, its two variables
  (`JOBRADAR_URL`, `JOBRADAR_TICK_SECRET`) and its tests are unchanged: stdio is
  still a transport it owns, and `cd mcp && uv run pytest` stays green with
  nothing configured.

## What this does not decide

Deploying, the live URL and the production token are `#16`'s. This record ships
the transport, the mount, the authorisation and the offline tests, and names the
one live check (`AC9`) that only a running deployment can verify.
