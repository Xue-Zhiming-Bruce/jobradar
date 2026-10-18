# The whole test surface in one command: `make test` runs the backend's unit and
# integration suites, the frontend suite, the MCP suite and the on-call
# responder's offline test, in that order, and stops at the first failure. One
# line per suite, so a suite that stops being run is a change to this file rather
# than a command quietly doing less than it says --
# `backend/tests/test_single_test_command.py` fails, by name, when one goes.
#
# The fifth suite is `ops/oncall`'s own, the one suite outside a project
# directory: it is handed the repository root and `uv run --project backend`,
# the spelling `.github/workflows/ci.yml` uses, so the one command line is run
# by both surfaces. It is offline and keyless, and uses no database.
#
# The throwaway PostgreSQL the two backend passes need is started and removed by
# `scripts/test.sh`, the one process that runs every suite, so a failure or an
# interrupt removes it too. Nothing is started by hand.
#
# The port is fixed here, and named in README.md's "Running the tests": 5432 is
# taken on a development machine. A server already on this one is not the server
# the suites run against -- `scripts/test.sh` moves to the next free port and
# prints the one it used.
TEST_DB_PORT = 5433

.PHONY: test test-backend test-integration test-frontend test-mcp test-oncall

test:
	./scripts/test.sh $(TEST_DB_PORT) \
	  backend "cd backend && uv run pytest" \
	  integration "cd backend && uv run pytest -m integration" \
	  frontend "cd frontend && npm test" \
	  mcp "cd mcp && uv run pytest" \
	  oncall "uv run --project backend pytest ops/oncall"

# One suite on its own, for a caller that wants that suite's verdict and its own
# database: `.github/workflows/ci.yml` reaches each suite through the target here
# -- a suite job against the service container it starts and the
# `TEST_DATABASE_URL` it passes, the on-call responder's step from the
# repository root -- so the commands are spelled in this file and not a second
# time in the workflow. Each target runs the command the `test` target's own line
# for that suite names; `backend/tests/test_ci_make_parity.py` fails when a
# target and the recipe stop naming the same one.
test-backend:
	cd backend && uv run pytest

test-integration:
	cd backend && uv run pytest -m integration

test-frontend:
	cd frontend && npm test

test-mcp:
	cd mcp && uv run pytest

test-oncall:
	uv run --project backend pytest ops/oncall
