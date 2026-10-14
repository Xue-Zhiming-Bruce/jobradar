# Diagnosis: the **Run digest now** button answered its own 401 (#77)

## Symptom

On any instance with `TICK_SECRET` set, pressing the shipped **Run digest now**
button -- the app's own page, same origin, no header -- was answered `401`, so the
one control `README.md` calls the reliable path could not start a run. With
`TICK_SECRET` unset the same guard refused too: it refuses when nothing is
configured rather than opening. The button was therefore unusable either way, and
`mcp/` or a script holding the secret was the only caller that could press.

## Evidence

- `1acc1c8`'s own message: "(This fixes) the shipped Run digest now button, which
  sends no header and was a 401 on any instance with `TICK_SECRET` set."
- The press, in `frontend/src/screens/RunDigestNow.tsx`: `useRun(startTick)`, and
  `frontend/src/api/client.ts`'s `startTick()` is
  `decodeRun(await request<WireRunSummary>('/internal/tick', { method: 'POST' }))`
  -- no `headers` at all, so the request carries neither `X-Tick-Secret` nor
  anything else the guard could read.
- The bundle cannot hold the value: `grep -rn "TICK_SECRET\|X-Tick-Secret"
  frontend/src` returns nothing, and no route serves an environment variable to
  the page (`backend/app/routes/internal.py` reads `TICK_SECRET` through
  `tick_secret()`, server-side).
- The pre-fix guard, `git show 1acc1c8^:backend/app/routes/internal.py`:
  `_start_run` called `_require_tick_secret(request.headers.get(TICK_SECRET_HEADER))`
  as its whole admission check, and that function's refusal branch was
  `if not configured or not hmac.compare_digest(offered.encode("utf-8"),
  configured.encode("utf-8")): raise HTTPException(status_code=401, ...)` -- the
  header, and nothing else, admitted a caller.
- Issue #77's QA verdict (`PASS`, at `fce7bfd`) attacked the new rule out-of-tree
  on both routes and recorded, as a criterion check, that "Before: `main`'s
  `_start_run` (line 362) called `_require_tick_secret` alone, so an in-app press
  without the header was 401; now the same press with its own `Origin`/`Host` is
  202".

## Root cause

The proof of identity the guard demanded was one the app's own page cannot
produce. `TICK_SECRET` is a server-side value: it is read per request by
`app/routes/internal.py`, it is never sent to the browser, and the bundle holds
neither the name nor the value -- by design, and the right design, because a
secret in a JavaScript bundle is a public secret. So the guard admitted exactly
one caller: one that had been given the secret out of band. The app's own page is
the caller that cannot be given it, which left the one caller the product ships
with no admitted path -- a guard written for scripts that was applied to a button.
The refusal itself was correct and constant-time; the mistake was upstream of it,
in choosing a rule with no path for the same-origin press.

The fix is `1acc1c8` (`#77: Admit the instance's own page to the two run routes`):
`_require_admitted_caller` admits the operator's `X-Tick-Secret` header **or** a
request whose `Origin` names its own `Host`, one guard for both run routes, with
the refusal now naming both ways in. It does not weaken the secret's comparison
(`hmac.compare_digest` over both values' bytes, unchanged) and it adds no CORS.

## Pin

- `backend/tests/test_internal_tick.py::test_a_same_origin_press_with_no_secret_is_202_and_starts_the_work`
  -- a press carrying `Origin` and `Host` and **no** `X-Tick-Secret` is `202`,
  writes the `running` row, hands over exactly one unit of work, and the row reads
  `succeeded` once that work runs.
- `backend/tests/test_internal_tick.py::test_with_no_configured_secret_only_the_instance_s_own_page_is_admitted`
  -- with `TICK_SECRET` unset, no `Origin` is `401`, a wrong secret is `401`, and
  the instance's own page is still `202`.

Both are parametrised over `_RUN_PATHS`, so each runs against `/internal/tick`
**and** `/internal/rematch`: the digest press and the re-match press are pinned
alike, because they share the one guard. `_SAME_HOSTS` covers the spellings the
browser produces -- a scheme-default port omitted and one spelled out, http and
https, a non-default port.

**What this does not cover.** No live instance exists, so this was diagnosed in
the code and pinned by the offline suite, never by a press on a deployment: the
deployed check is #16's, which is open, and the `Host` a browser sends in front of
a TLS terminator is the same premise #43's AC9 rests on. The admission rule's own
limit -- `Origin` is a header, not a credential, and one press costs up to
`SCORER_MAX_POSTINGS` model calls of the owner's key -- is #77's accepted
exposure, stated in `README.md`'s **Known limitations** and in
`security/agent-extension-notes.md`; it is not re-decided here.
