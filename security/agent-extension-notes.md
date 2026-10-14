# Agent and extension surfaces, and the blast radius of each (#82)

Every place this repository lets something act on a running instance or on the
repository itself, what it can reach from there, and the file that shows it. A
surface's limits are written beside it rather than left to be assumed, and where
a limit is carried by code or by a test, the path is named.

## The committed-data guard (#19)

What it stops: a secret, a resume or a database file in the git index, which is
what keeps this public repository free of the data `AGENTS.md` and
`_docs/process.md` name. `backend/tests/test_committed_data_guard.py` reads the
tracked paths and their staged content and fails on a `.env`, a `*.db` or
`*.sqlite3` path, anything under `uploads/` or `data/`, a resume file, a
credential shape, or a non-empty value assigned to a secret-shaped name. It runs
in the default suite and as one step of `ci.yml`'s `backend` job.

Its limits are its own docstring's, quoted: it reads **"the git index"** and
never the working tree, so a file that was never staged is invisible to it; **"a
secret hardcoded in a quoted Python string counts only when it has a credential
shape"**; **"A resume committed as a `.txt` outside `uploads/` and `data/` is
indistinguishable from ordinary text and is deliberately not caught"**; and
**"a leak that predates this module is invisible to it, because the index is the
only thing read."** CI runs after a push, so the guard stops a merge and makes a
leak loud rather than preventing the push. One change is audited in
`security/change-audit-committed-data-guard.md`.

## The hosted MCP endpoint (#43)

`backend/app/mcp.py` serves the seven tools over streamable HTTP on the app's own
origin at `/mcp`, with and without the trailing slash. Its credential is
`MCP_AUTH_TOKEN`, an `Authorization: Bearer` value separate from `TICK_SECRET`
and read **per request** rather than at import (`auth_token()`), so an operator
turns the endpoint on or off by setting the variable. A missing, blank or wrong
token is one indistinguishable `401`, answered before the transport runs, so a
refused request performs no tool action; **while the variable is unset or blank
the endpoint refuses every request**, which is the shipped default.

Blast radius of a token holder, from `mcp/jobradar_mcp/tools.py`: read the
watchlist and the matches, add a company and remove one, trigger a run and read
its row. That is the app's own API reached with the endpoint's credential -- the
tools hold no credential of their own, and the one they need for a run
(`JOBRADAR_TICK_SECRET`) is the stdio server's variable, below.

**The endpoint's live behaviour is unverified.** #43's live criterion was dropped
rather than passed and #16 (the deployment) is still open, so this section
describes the code and the offline suite -- the endpoint is exercised in-process
in `backend/tests/test_mcp_hosted.py` -- and not a call that was never made.

## The two run routes' admission rule (#77)

`POST /internal/tick` and `POST /internal/rematch` are the only routes that spend
money and send mail. `backend/app/routes/internal.py`'s `_require_admitted_caller`
admits a caller that sends `X-Tick-Secret` equal to the configured `TICK_SECRET`,
or one whose `Origin` names the request's own `Host` (scheme ignored, a
scheme-default port omitted equal to one spelled out); everything else is `401`.
`TICK_SECRET` unset refuses all but the instance's own page.

**The documented limit is that this is not authentication.** `Origin` is a header
and not a credential, so a non-browser client can forge it, and anyone who can
reach the URL can press either run button: one press spends at most
`SCORER_MAX_POSTINGS` model calls of the owner's key, and a digest press also
sends mail. `README.md`'s **Known limitations** states the same sentence, and
`backend/tests/test_internal_tick.py` covers the admission cases on both routes.

## The stdio MCP server (`mcp/jobradar_mcp/client.py`)

The server the README demonstrates locally holds exactly two variables it reads
from its own environment, `JOBRADAR_URL` and `JOBRADAR_TICK_SECRET`, and no third
one: not the model key, not a database URL, not the mail settings. Its blast
radius is the tick's -- the same five calls a `X-Tick-Secret` holder can make
against the two run routes and the read routes -- and it adds none of its own:
the server holds no credential beyond those two values, and it is the caller's
own environment that holds them.

## This repository's own subagents

The roles an agent takes here are the files in `_docs/team/`: `pm.md` (groom a
task, write no code), `qa-engineer.md` (check finished work, change nothing,
report by comment), `software-engineer.md` (implement one task, commit as it
goes, never close an issue), and `AGENTS.md` above all of them, which carries the
data rules. The blast radius of an agent working in this repository is a commit
on a branch: it can write files the roles allow and push them, and it can reach
anything its own machine can read -- which is what
`security/ai-tool-and-data-policy.md` takes up.
