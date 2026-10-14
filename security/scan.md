# The security scan: `ruff`'s `S` rules, offline and keyless (#82)

The scan is `ruff`'s `S` rule set -- flake8-bandit's checks -- over the Python
this repository ships. It is one step of `.github/workflows/ci.yml`'s `backend`
job, named exactly `Fail on a new security scan finding`, placed after the
committed-data guard and before the suite. It reads the working tree, needs no
network and no key, and leaves no file behind: `.ruff_cache/` is in `.gitignore`
and `.dockerignore`, so `git status --short` is empty after it.

## The command, once, spelled the same in CI and here

    uv run --project backend ruff check --select S --output-format concise backend/app mcp/jobradar_mcp

From the repository root. The CI step carries no `working-directory`, so it runs
at that root too and every path the tool prints is repository-relative;
`uv run --project backend` uses the environment the job's `uv sync` already
built. `--output-format concise` prints one line per finding with no source
frame, which is what makes the block below a thing a reviewer diffs rather than
reads. Rerun it from the repository root and compare.

`ruff` is `backend/pyproject.toml`'s `[dependency-groups] dev` entry
`ruff>=0.14,<0.15`, resolved by `backend/uv.lock` to **0.14.14**
(`cd backend && uv run ruff --version`), so a finding is reproducible instead of
a version's opinion. It is a dev dependency and nothing else: the image syncs
`--frozen --no-dev` (`backend/Dockerfile`), so no part of the scanner is in it.

## The findings, and the disposition of each

The command reported seven findings on this branch's first commit (`d4a7b2a`,
the one that pins the tool). Reproduce that output in a scratch copy --
`git archive d4a7b2a | tar -x -C <an empty directory>`, then the command above in
it -- where the tool is present and no disposition is yet:

```text
backend/app/mcp.py:43:23: S105 Possible hardcoded password assigned to: "AUTH_TOKEN_VARIABLE"
backend/app/routes/internal.py:105:22: S105 Possible hardcoded password assigned to: "TICK_SECRET_HEADER"
backend/app/routes/internal.py:106:24: S105 Possible hardcoded password assigned to: "TICK_SECRET_VARIABLE"
backend/app/services/digest.py:69:17: S105 Possible hardcoded password assigned to: "SMTP_PASSWORD"
backend/app/services/resume.py:739:12: S314 Using `xml` to parse untrusted data is known to be vulnerable to XML attacks; use `defusedxml` equivalents
mcp/jobradar_mcp/client.py:27:24: S105 Possible hardcoded password assigned to: "TICK_SECRET_VARIABLE"
mcp/jobradar_mcp/tools.py:64:22: S105 Possible hardcoded password assigned to: "TICK_SECRET_HEADER"
Found 7 errors.
```

Each is dispositioned where the code is, so the command is green on this commit:
it prints `All checks passed!` and exits 0. The dispositions are the gate, and
there is no `--ignore`, no `--per-file-ignores`, no `[tool.ruff.lint]` list and
no `# ruff: noqa` anywhere -- a *new* finding of any `S` rule turns the step red,
which is what the step's name says.

| finding | disposition, at its site |
| --- | --- |
| `backend/app/mcp.py:43` `S105` | `AUTH_TOKEN_VARIABLE` holds the *name* of the variable the guard reads, not a token: `auth_token()` reads the value by that name at call time. `# noqa: S105`. |
| `backend/app/routes/internal.py:105` `S105` | `TICK_SECRET_HEADER` is the header's name; the value a caller sends is compared in `_require_tick_secret` at call time. `# noqa: S105`. |
| `backend/app/routes/internal.py:106` `S105` | `TICK_SECRET_VARIABLE` is the variable's name; `tick_secret()` reads what the operator set. `# noqa: S105`. |
| `backend/app/services/digest.py:69` `S105` | `SMTP_PASSWORD` is the variable's name; the relay's password is read by that name when a digest sends. `# noqa: S105`. |
| `backend/app/services/resume.py:739` `S314` | The parsed string is the uploaded archive's own `word/document.xml` part, the upload is bounded by `MAX_UPLOAD_BYTES` (10 MiB default, refused with `413` in `app/routes/resume.py` before any reader runs) and `ElementTree` resolves no external entity. `# noqa: S314`; assessing a hardened parser is #85. |
| `mcp/jobradar_mcp/client.py:27` `S105` | `TICK_SECRET_VARIABLE` is the variable's name; the stdio client reads it per call. `# noqa: S105`. |
| `mcp/jobradar_mcp/tools.py:64` `S105` | `TICK_SECRET_HEADER` is the header's name; the app reads the value itself and no secret is held here. `# noqa: S105`. |

Nothing else in those six files changed: `git diff -U0` of each is the comment
line above and the `# noqa` suffix on the flagged line, and no code.

## Why this tool, and not a dependency or a secret scanner

`ruff` runs offline with no key, its output carries no source snippet, and the
`S` rules are flake8-bandit's checks. `bandit` itself was the first choice and
was tested, and it cannot be tracked here: its report carries a `code` field with
the flagged source line, so the report for `app/services/digest.py:69` holds the
assignment `SMTP_PASSWORD = "SMTP_PASSWORD"` with the escaped quotes the guard's
unquoted-assignment rule reads as an environment value -- the report itself would
have to pass `backend/tests/test_committed_data_guard.py` (#19) -- and stripping
the field breaks its own `-b` baseline. `ruff`'s frame keeps the quotes, so this
scan's output is a tracked file the guard accepts.

No dependency- or advisory-database scan is part of this step, and none is
missing by accident: `pip-audit`, `npm audit`, `trivy` and `gitleaks` each need
the network, or a downloaded advisory database or binary, at scan time. Secret
scanning is the committed-data guard's job and it is already shipped.

## The other offline check, in the same place

The scan does not look for secrets; the guard does, offline and keyless too:

    cd backend && uv run pytest tests/test_committed_data_guard.py

`backend/tests/test_committed_data_guard.py` (#19) reads the git index -- the
tracked paths and their staged content -- and fails on a staged `.env`, a
`*.db`/`*.sqlite3` path, anything under `uploads/` or `data/`, a resume file, a
credential shape, or a non-empty value assigned to a secret-shaped name. It runs
in the default suite as well, and as its own step of the same `backend` job,
named `Fail on a committed secret, resume or database file`. The two checks are
read together here because they are the repository's only two offline checks:
this one looks for a rule violation in shipped code, that one for data in the
index.

## What the scan covers, and what it does not

- **Shipped Python only**: `backend/app` and `mcp/jobradar_mcp`. `backend/tests`
  is excluded by decision rather than by oversight -- the suite plants
  credential-shaped sentinels on purpose (the guard's own fixtures) and uses
  `assert` throughout, so `uv run --project backend ruff check --select S
  --statistics backend/tests` reports 3198 findings, 3128 of them `S101`, which
  is noise rather than a signal.
- **The frontend is not scanned by this step**: the scan is Python only, and
  `npm audit` would need the network.
- **It cannot see** a real credential whose shape no `S` rule models, or
  anything outside the `S` rules -- a flaw that is not a bandit pattern is not
  reported here. The parser of the DOCX reader is exactly that: `S314` names a
  rule, and whether `ElementTree` should be replaced by a hardened parser is a
  judgement this scan cannot make, which is why #85 owns it.
