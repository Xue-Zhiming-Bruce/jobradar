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
- A change to the routes or what they send and receive regenerates the committed contract in the same commit: `cd backend && uv run python -m app.export_openapi`. `cd backend && uv run pytest` compares `backend/openapi.json` with the document the app generates now (`backend/tests/test_openapi_contract.py`), and `ci.yml`'s `contract` job fails on a diff, so a stale contract is a red build and not a reviewer's catch.
- A change to `backend/app/models.py` ships a revision in the same commit: `cd backend && uv run alembic revision --autogenerate -m "<message>"`, then read the generated file and hand-write what autogenerate does not detect (a changed CHECK constraint, a re-typed column). `cd backend && uv run pytest` compares the migrated schema with the models (`backend/tests/test_migrations.py`), so a model change with no revision is a red build and not a reviewer's catch.

## Local commands

- Backend tests: `cd backend && uv run pytest`
- Frontend tests: `cd frontend && npm test`
- Whole system: `docker compose up --build --wait`

## Data rules

- A resume is personal data. It is uploaded at runtime, stored outside the
  repository, and never logged or committed.
- Secrets live in `.env`, which is gitignored. `.env.example` lists the keys.
