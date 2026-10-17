#!/usr/bin/env bash
# The engine behind `make test`: one process runs the suites the Makefile names,
# in the order it names them, and stops at the first failure. The throwaway
# PostgreSQL the backend's two passes need is started here and removed on exit --
# after a pass, after a failure and after an interrupt -- so the reviewer starts
# and cleans up no database by hand.
#
# The suites run against the server started here, never one a shell's
# `TEST_DATABASE_URL` already names: an inherited variable pointing at a real
# database would let the suite write to it, or make the run red for a server it
# never started.
#
# `make test` runs the ordinary `uv run pytest` in each directory, the same
# command CI runs. The macOS `uv` editable-install trap in AGENTS.md is the
# environment's, not this script's.
set -euo pipefail

port="${1:?make test passes the PostgreSQL port the Makefile fixes}"
shift

# The throwaway credentials ci.yml's own ephemeral container publishes.
dsn="postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:${port}/jobradar"
export TEST_DATABASE_URL="$dsn"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is required: make test starts its own PostgreSQL" >&2
  exit 1
fi

# A server already on the port is not this run's server to test against.
if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
  echo "error: port ${port} is taken: make test starts its own PostgreSQL there" >&2
  exit 1
fi

# What a checkout without `node_modules` or the virtual environments is missing,
# installed the way ci.yml's jobs install it.
(cd backend && uv sync)
(cd mcp && uv sync)
(cd frontend && npm ci)

container="jobradar-make-test-$$"
# The suite logs live outside the repository: a passing run leaves the working
# tree as it found it.
logdir="$(mktemp -d)"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$logdir"
}
trap cleanup EXIT
# `exit` on the interrupt so the EXIT trap still removes the container.
trap 'exit 130' INT TERM

docker run --rm -d --name "$container" \
  -p "${port}:5432" \
  -e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway \
  -e POSTGRES_DB=jobradar postgres:17 >/dev/null

for _ in $(seq 1 60); do
  docker exec "$container" pg_isready -U jobradar -d jobradar >/dev/null 2>&1 && break
  sleep 1
done
if ! docker exec "$container" pg_isready -U jobradar -d jobradar >/dev/null 2>&1; then
  echo "error: the throwaway PostgreSQL on port ${port} did not become ready" >&2
  exit 1
fi

while [ "$#" -gt 0 ]; do
  suite="$1"
  suite_command="$2"
  shift 2
  log="${logdir}/${suite}.log"

  echo "== ${suite} =="
  if ! bash -c "$suite_command" 2>&1 | tee "$log"; then
    echo "error: the ${suite} suite failed" >&2
    exit 1
  fi

  # A summary with no passed case is a suite that ran nothing: green by
  # omission, not by passing. A summary with skipped cases is the half-run the
  # backend's engine matrix and the MCP live case go red for, because the
  # variable or the marker did not reach it.
  if ! grep -qE '[1-9][0-9]* passed' "$log"; then
    echo "error: the ${suite} suite reported no passed case" >&2
    exit 1
  fi
  if grep -qE '[0-9]+ skipped' "$log"; then
    echo "error: the ${suite} suite skipped cases" >&2
    exit 1
  fi
done

echo "all suites passed"
