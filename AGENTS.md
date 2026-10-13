# JobRadar

Commands
- Backend deps: `cd backend && uv sync`
- Backend tests: `cd backend && uv run pytest`
- Backend dev server: `cd backend && uv run uvicorn app.main:app --reload`
- Frontend deps: `cd frontend && npm install`
- Frontend tests: `cd frontend && npm test`
- Frontend dev server: `cd frontend && npm run dev`
- Whole system: `docker compose up --build --wait`

Local trap (macOS)
- `uv` installs `mcp/` into `backend/.venv` as an editable path source (#43), and
  writes its `.pth` files with the macOS hidden flag. Python then skips them, so
  `import jobradar_mcp` fails and the whole backend suite errors at collection —
  after a `uv run`, which re-writes them. Clear the flag and run the venv's own
  python: `chflags nohidden backend/.venv/lib/python3.12/site-packages/__editable__*jobradar_mcp*`
  then `backend/.venv/bin/python -m pytest`. Linux CI is unaffected.

Rules
- Follow _docs/process.md
- Read the acceptance criteria before starting and before closing a task
- Never commit a resume, an API key, a database file, or anything from `.env`
- Resume text is user data: do not log it, do not print it in errors

Documents
- _docs/plan.md - what we're building and why
- _docs/process.md - how work is organized
- _docs/team/ - role definitions for sub-agents
- _docs/decisions/ - architecture decisions, including the ones Jev made
