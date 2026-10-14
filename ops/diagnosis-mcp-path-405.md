# Diagnosis: `/mcp` answered 405 and 404 in the container, and the suite was green (#43)

## Symptom

The container built by this repository's own command answered the path
`README.md` documents with a refusal:

```
POST http://<the app's host>/mcp   (Authorization: Bearer <the configured token>)
  → 405 {"detail":"Method Not Allowed"}
GET  http://<the app's host>/mcp   → 404
POST http://<the app's host>/mcp/  → 200 text/event-stream
```

The MCP SDK's streamable-HTTP client failed at `initialize` (`MCPError: Server
returned an error response`) against the documented path, and listed the seven
tools against the trailing-slash path only. The observation is QA's `FAIL (code)`
verdict on issue #43, with the command that produced it: `docker compose up
--build --wait`, then the SDK's `streamable_http_client` + `ClientSession`
pointed at the app's own base URL. QA reproduced it without Docker as well, with
a built UI present (`STATIC_DIR` set to a scratch build): `POST /mcp` → 405,
`POST /mcp/` → reaches the transport, `GET /mcp` → 404. The same issue's verdict
records `cd backend && uv run pytest` as **1307 passed, 373 skipped, 2
deselected** at `07e9992` -- the suite was green while the documented path was
broken.

## Evidence

- Issue #43, QA's `FAIL (code)` verdict comment (the observation, the two
  reproductions, and the green suite above).
- The pre-fix source, `git show 0c5a671^:backend/app/main.py`: the endpoint was
  registered as `app.mount(mcp.MCP_PATH, mcp.endpoint, name="mcp")`, after the
  API routers and before `if static_dir().is_dir(): app.mount("/", BuiltUiFiles(
  directory=static_dir(), html=True), name="ui")`.
- Reproduced on this machine from a scratch copy (`/tmp/jr82-diag`) with those
  two modules restored from `0c5a671^`, driving the app with `TestClient` and no
  Docker:

  | probe | pre-fix code | this tree |
  | --- | --- | --- |
  | `POST /mcp`, no static dir (the test environment) | 307 Temporary Redirect to `/mcp/` | 400 from the transport |
  | `POST /mcp`, a static dir mounted | **405** | 400 from the transport |
  | `GET /mcp`, a static dir mounted | **404** | 400 from the transport |
  | `GET /`, a static dir mounted | 200 (the UI really is mounted) | 200 |

  The 400s are the transport's own answer to a header-only request, so 400 rather
  than 405/404 is what reaching it looks like; `GET /` at 200 rules out a probe
  that simply failed to mount the UI.
- The fix, `backend/app/main.py`: the mount was replaced by routes registered for
  both spellings -- `for path in (mcp.MCP_PATH, mcp.MCP_PATH + "/"): app.routes.append(Route(path, endpoint=mcp.endpoint))` -- and `app/mcp.py`'s `_at_transport_root`
  normalises either scope to the transport's single root.
- QA's second verdict on #43 (`PASS`) records the container matrix after the fix:
  `POST /mcp` and `POST /mcp/` both 401 without a token and 200 with it, `GET /mcp`
  reaching the transport, `GET /` 200.

## Root cause

A Starlette `Mount` at a path matches that path's subtree only. `app.mount("/mcp",
...)` compiles to the pattern `/mcp/{path:path}`, which a request for the bare
`/mcp` does not match. Two things then follow, and only the first was visible to
the suite:

- **With no catch-all mount -- the test environment** -- Starlette's
  `redirect_slashes` answers `307` to `/mcp/`, and that redirect is what
  `backend/tests/test_mcp_hosted.py` had been seeing. The endpoint was reachable
  and the suite was honest about what it tested: it just never tested the bare
  path.
- **In the image -- with the UI mounted** -- the `StaticFiles` mount registered at
  `/` (last, so it claims what no other route did) matched the bare `/mcp` first,
  so `redirect_slashes` never ran and the static files app answered the request:
  405 for `POST`, 404 for `GET`.

The suite could not see the second because `static_dir()` returned
`backend/app/static`, which does not exist in a checkout, so `app/main.py`'s
`if static_dir().is_dir()` never mounted the catch-all. The failure lived exactly
in the difference between the test process and the image, which is why a green
suite proved nothing here.

## Pin

`backend/tests/test_mcp_hosted.py::test_the_documented_path_reaches_the_transport_with_the_ui_mounted`
builds a real static directory (`_a_built_ui(tmp_path / "static")`), reloads
`app.main` with `STATIC_DIR` naming it, and drives the SDK's streamable-HTTP client
at `/mcp` and `/mcp/`, asserting the seven tools list at both spellings.

The pin is real, not a test that passes because `app/static` is absent: in the
scratch copy above, with `0c5a671^`'s `backend/app/main.py` and
`backend/app/mcp.py` restored, `uv run pytest tests/test_mcp_hosted.py -q` is
`1 failed, 9 passed` -- the failure being that case (`MCPError: Server returned an
error response`) -- and it passes on this tree.

The fix is `0c5a671` (`#43: serve the hosted endpoint at /mcp with and without the
trailing slash`).

**What this does not cover.** No deployment exists -- #16 is open -- so the 405 was
observed on this machine's container and in a scratch copy, never against a live
instance: the deployed check is #43's AC9, dropped rather than passed. The
endpoint's token matrix (missing, blank and wrong all `401`) and the tools'
in-container behaviour are not re-diagnosed here; they are #43's own verdict.
