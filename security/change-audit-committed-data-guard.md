# Change audit: the committed-data guard (#19)

An audit of one merged change, taken through the review path this repository
actually runs. That path is not a pull request: `gh pr list --state all --json
number` returns `[]` -- there has never been one -- so the trail of a change is
its issue, the branches and commits that implement it, the engineer's
implementation comment, and QA's verdict comment per acceptance criterion
(`_docs/process.md`, `_docs/team/qa-engineer.md`). This audit reads that trail
and re-runs the checks it depends on, and it records what the change still does
not cover.

**The change under audit**: `backend/tests/test_committed_data_guard.py`, the
one step it added to `ci.yml`'s `backend` job, and the README paragraphs that
describe it -- issue #19, closed after a QA PASS.

## The trail, and what each part of it is

| the trail | what it says |
| --- | --- |
| Issue #19, groom comment | The decisions the implementation could not guess: the guard is a pytest module in the default suite plus one step of the existing `backend` job rather than a fifth job (the job names, triggers and `permissions: contents: read` are #15's contract); it reads the **git index**, never the working tree, so a developer's own gitignored `.env`, `backend/data/jobradar.db` and `~/.jobradar/uploads` are never read while `git add -f` of an ignored file is caught; the allowlist is seven exact literals rather than a path or shape exemption; the planted-file demonstration happens in a scratch copy, because a planted secret in a pushed branch of a public repository is itself the leak the guard exists to prevent; and the four named limits are stated rather than implied. |
| Issue #19, implementation comment | Files and claims: the module (61 tests), the `ci.yml` step named `Fail on a committed secret, resume or database file`, the README paragraphs; the planted shapes with the one line each produced; and the two ambiguities resolved in unattended mode, both with the reason. |
| Issue #19, QA verdict | `PASS`, 11 of 11 criteria, against `732f062..2c4e866`; the criteria were attacked out-of-tree with an independent probe and a scratch repository under `/tmp/qa19`, not only re-read. |
| Commits | `3772805` (the guard), `6ba0a9f` (the `ci.yml` step and the README), `2c4e866` (the two exemption cases). |
| CI run `38300253741` | push, `name: CI`, head SHA `2c4e86689e2b775f72bfffd6fc0c4bba64b52417` (`gh run view 38300253741 --json conclusion,headSha`), conclusion `success` -- so the step ran on that commit's tree and was green there. |

## Checks re-run for this artifact

| command | observed |
| --- | --- |
| `gh pr list --state all --json number` | `[]` -- no pull request exists to audit, which is why the trail above is the audit's subject. |
| `cd backend && uv run pytest tests/test_committed_data_guard.py` | `61 passed` (exit 0). |
| `cd backend && uv run pytest` | `1386 passed, 408 skipped, 2 deselected` (exit 0), the guard's 61 cases among them. This machine needed the workaround `AGENTS.md`'s "Local trap (macOS)" paragraph records: `uv run` re-writes the editable `jobradar_mcp` `.pth` files with macOS's hidden flag, so after `chflags nohidden backend/.venv/lib/python3.12/site-packages/__editable__*jobradar_mcp*` the venv's own python ran it. |
| `gh run view 38300253741 --json databaseId,conclusion,headSha,displayTitle,event,name` | `{"conclusion":"success","databaseId":38300253741,"event":"push","headSha":"2c4e86689e2…","name":"CI"}`. |
| `git status --short` in this repository, before and after every run above | empty; nothing staged, no tracked file changed. |

**Red then green, in a scratch copy under `/tmp`** (nothing planted in this
repository). `rsync` of the tree to `/tmp/jr82-audit`, excluding every virtualenv
and cache; one `.env` holding a single empty assignment was written there and
`git add -f`'d into that copy's index -- the `git add -f` of an ignored file being
the awkward case the guard exists for -- then the guard was run in it:

    $ cd /tmp/jr82-audit/backend && uv run pytest tests/test_committed_data_guard.py -q
    >       assert violations == [], "\n".join(violations)
    E       AssertionError: .env (environment file)
    tests/test_committed_data_guard.py:248: AssertionError
    2 failed, 59 passed in 10.10s

The two failures are the module's own repository-wide scan and its
working-directory case, both re-run against the copy; the line names the path and
the kind and nothing else. `git rm --cached .env` and deleting the file, then the
same command:

    $ cd /tmp/jr82-audit/backend && uv run pytest tests/test_committed_data_guard.py -q
    61 passed in 12.83s

The scratch copy is deleted, and no file was planted in this repository at any
point.

## The control's residual gaps

Each of these is the guard's own docstring, quoted, and each is a limit of the
control rather than a defect in it:

- **"The root is resolved from `__file__`"** -- and the thing read is the index
  and never the working tree, so a file that was never staged is invisible and an
  edit that was not staged does not change the verdict. CI runs after a push: the
  guard stops a merge and makes a leak loud, rather than preventing the push. The
  local `uv run pytest` is the earlier warning.
- **"A secret hardcoded in a quoted Python string counts only when it has a
  credential shape."** The environment rule reads the unquoted `.env`, shell,
  YAML and CI spellings; a quoted assignment is left to the credential-shape
  rules (PEM block, AWS access key id, `sk-` key, GitHub token, a PostgreSQL URL
  carrying a password).
- **"A resume committed as a `.txt` outside `uploads/` and `data/` is
  indistinguishable from ordinary text and is deliberately not caught."**
- **"A leak that predates this module is invisible to it, because the index is
  the only thing read."**

What this audit did not check: the guard's behaviour against a real credential or
a real resume -- no such value exists in this repository, and none was created
for this artifact. Every planted value above is a path, not a value: the red run
reports `.env (environment file)` and prints no matched text.

The guard is also one of the two checks `security/scan.md` reads together, the
other being the `ruff` `S` scan (#82). Neither looks for a secret the other would
find: the scan reads shipped code for rule violations, the guard reads the index
for data.
