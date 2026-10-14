# Agent extension pack

The six pieces a reviewer looks for when asking how this repository is built by an
agent, each with the real file behind it. The build's own narrative is
[`README.md`](../README.md)'s "How this repository was built, with AI"; this page
is the index to the files that narrative names, and does not repeat it.

| Piece | Path that implements it |
| --- | --- |
| Project instructions | `AGENTS.md` |
| Reusable workflow | `_docs/process.md`, using the template `_docs/task-template.md` |
| Subagent / specialist | `_docs/team/pm.md`, `_docs/team/software-engineer.md`, `_docs/team/qa-engineer.md` |
| MCP tool / server | `mcp/` |
| Hook or guardrail | `agent-hooks/pre-commit` |
| Permission notes | `docs/permissions.md` |

## The pieces

**Project instructions** — `AGENTS.md` is what an agent reads first: the commands,
the local trap, and the rules it must not break.

**Reusable workflow** — `_docs/process.md` is the rule book every task follows
(groom, one issue at a time, commit regularly, QA verdicts, the definition of
done), and it is written to the template `_docs/task-template.md`. A new task
starts from that template, which is why the workflow and its template are one
piece and not two.

**Subagent / specialist** — the three role prompts in `_docs/team/`:
`_docs/team/pm.md` grooms, `_docs/team/software-engineer.md` implements,
`_docs/team/qa-engineer.md` verifies. The orchestrating session is the fourth
actor and has no role file of its own: it is described in `README.md`, under
"How this repository was built, with AI".

**MCP tool / server** — `mcp/` is a server that drives the running app over HTTP.
It offers **seven** tools, one per app endpoint, all defined in
`mcp/jobradar_mcp/tools.py`: `add_company`, `list_watchlist`, `list_matches`,
`trigger_run`, `get_run`, `remove_company`, `set_match_state`.

**Hook or guardrail** — `agent-hooks/pre-commit`, the enforcement point that runs
before a commit instead of after a push. See below.

**Permission notes** — `docs/permissions.md`, what an agent may do here, one row
per rule this repository states.

## The guardrail: before the commit, not after the push

The rule — no secret, API key, resume or database file is ever committed — lives
once, in `backend/tests/test_committed_data_guard.py`. `agent-hooks/pre-commit`
calls that guard and nothing else, so no byte of the rule has a second home.

Install it once per clone (the hook is committed; git's own hooks directory is
not part of the tree):

    git config core.hooksPath agent-hooks

It has two halves. `.github/workflows/ci.yml`'s step **"Fail on a committed
secret, resume or database file"** runs the same guard **after the push** and
stops a merge. `agent-hooks/pre-commit` runs it **before the commit** and stops
the commit locally — the earlier of the two, and the one that keeps a leak out of
history rather than making it loud after the fact.

## Where the rubric's content lists land

The rubric's Module 5 names directories. Criterion 12 scores the *content*, not
the directory names, and the physical layout that would match those names is
#83 — not here. For that list, this is the real path behind each name:

| Rubric's Module 5 name (a draft) | Real path here |
| --- | --- |
| `agent-capabilities/` | `_docs/process.md` and `_docs/team/` |
| `mcp-server/` | `mcp/` |
| `plugins/` or `custom-agent/` | `_docs/team/` |
| `agent-hooks/` | `agent-hooks/` — the one new directory this task adds |
| the two `docs/` files | `docs/agent-extension-pack.md` and `docs/permissions.md` |

No other directory is created for the rubric's list: `agent-capabilities/`,
`mcp-server/`, `plugins/` and `custom-agent/` each already have a home, and a
directory whose only content is a pointer adds nothing this table does not.

The rubric and its contents lists are a draft and may change before the cohort
starts, so this page maps to real files rather than betting on its naming.
