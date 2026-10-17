# The whole test surface in one command: `make test` runs the backend's unit and
# integration suites, the frontend suite and the MCP suite, in that order, and
# stops at the first failure. One line per suite, so a suite that stops being run
# is a change to this file rather than a command quietly doing less than it says
# -- `backend/tests/test_single_test_command.py` fails, by name, when one goes.
#
# The throwaway PostgreSQL the two backend passes need is started and removed by
# `scripts/test.sh`, the one process that runs every suite, so a failure or an
# interrupt removes it too. Nothing is started by hand.
#
# The port is fixed here, and named in README.md's "Running the tests": 5432 is
# taken on a development machine, and a server already on this one is a failure
# rather than the server the suites run against.
TEST_DB_PORT = 5433

.PHONY: test

test:
	./scripts/test.sh $(TEST_DB_PORT) \
	  backend "cd backend && uv run pytest" \
	  integration "cd backend && uv run pytest -m integration" \
	  frontend "cd frontend && npm test" \
	  mcp "cd mcp && uv run pytest"
