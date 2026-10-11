# Process

- Tasks are GitHub issues, one at a time, lowest number first
- A task is groomed before implementation (PM, see _docs/team/pm.md)
- Read the acceptance criteria before starting and before closing
- Commit regularly
- The engineer does not close issues; QA outputs PASS or FAIL; the
  orchestrator closes only after PASS

## Definition of done for an issue

- Every acceptance criterion is implemented and verified by QA
- Tests cover the new behaviour and the whole suite passes
- The work is committed

## Local commands

- Backend tests: `cd backend && uv run pytest`
- Frontend tests: `cd frontend && npm test`
- Whole system: `docker compose up --build --wait`

## Data rules

- A resume is personal data. It is uploaded at runtime, stored outside the
  repository, and never logged or committed.
- Secrets live in `.env`, which is gitignored. `.env.example` lists the keys.
