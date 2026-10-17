"""`ops/rollback.sh`'s selection, identification, refusals and failures, pinned offline.

#89 asks for a rollback path that is documented *and* runnable, and the runnable
half is only worth having if its decisions are what the issue says. Every case
here drives the real script through `/bin/bash` -- macOS's 3.2, the version
`AGENTS.md` warns about, and the runner's 5 -- with no network, no Render service
and no real credential: `curl` is a shim first on `PATH`, the pattern
`backend/tests/test_agent_hook.py` uses for `uv`.

The shim answers each GET from `get-<n>.json`, newest first, and reuses the last
page when the script polls again, so a fixture is the deploy list Render would
return for `?limit=20`. A POST answers `post-status` (201 by default) with the
empty body and no `Location` header the real endpoint carries. Every call is
logged by request and URL only, never by header, and every run's output is
asserted to hold no part of the key: a credential in a log is the one thing this
script may never do.

The two values are set on the child's own environment as a plain dict --
`dict(os.environ)`, then a subscript assignment -- and never read here. That
spelling is deliberate: `backend/tests/test_env_example.py` reads an assignment to
`RENDER_API_KEY` as an environment read the app would have to list in
`.env.example`, and `backend/tests/test_committed_data_guard.py` reads it as a
committed environment value. The app reads neither variable; only this script
does.
"""

import itertools
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "ops" / "rollback.sh"

# The child's values. Neither is a real Render value, and the key is a string no
# shape rule reads as a credential.
SENTINEL_KEY = "rollback-test-key-not-a-credential"
SENTINEL_SERVICE_ID = "srv-rollback-test"

# The fake `curl`, written next to the fixture files it reads. It answers from
# `FAKE_CURL_STATE` so no case needs a socket, and it appends one line per call to
# `calls.log` -- the request and the URL, never a header, because the header
# carries the key. It is `sh` on purpose: a `#!` line holding a path with a space
# in it (this checkout is under one) is not one interpreter.
_SHIM = r'''#!/bin/sh
# A `curl` that answers from files: the offline stand-in for Render's API.

state="${FAKE_CURL_STATE}"
output=""
write_out=""
request="GET"
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    --write-out)
      write_out="$2"
      shift 2
      ;;
    --request)
      request="$2"
      shift 2
      ;;
    --header | --data | --max-time)
      shift 2
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done

printf '%s %s\n' "${request}" "${url}" >>"${state}/calls.log"

if [ "${request}" = "POST" ]; then
  body=""
  if [ -f "${state}/post-status" ]; then
    code=$(cat "${state}/post-status")
  else
    code=201
  fi
else
  if [ -f "${state}/get-exit" ]; then
    echo "curl: (28) Operation timed out after 60001 milliseconds" >&2
    exit "$(cat "${state}/get-exit")"
  fi
  if [ -f "${state}/get-raw" ]; then
    body=$(cat "${state}/get-raw")
  else
    count=1
    if [ -f "${state}/get-count" ]; then
      count=$(( $(cat "${state}/get-count") + 1 ))
    fi
    echo "${count}" >"${state}/get-count"
    while [ "${count}" -gt 1 ] && [ ! -f "${state}/get-${count}.json" ]; do
      count=$(( count - 1 ))
    done
    body=$(cat "${state}/get-${count}.json")
  fi
  code=200
fi

if [ -n "${output}" ]; then
  printf '%s' "${body}" >"${output}"
else
  printf '%s' "${body}"
fi

if [ -n "${write_out}" ]; then
  # The one format the rollback script asks for, spelled back exactly: an
  # unexpected one is a shim that would lie rather than answer.
  case "${write_out}" in
    '\n%{http_code}')
      printf '\n%s' "${code}"
      ;;
    '%{http_code}')
      printf '%s' "${code}"
      ;;
    *)
      echo "shim: unexpected --write-out '${write_out}'" >&2
      exit 3
      ;;
  esac
fi
'''

_RUNS = itertools.count()


def commit(name: str) -> str:
    """A 40-hex-character commit id that reads as its name in a failure message."""
    return (name * 40)[:40]


CURRENT = commit("c0ffee")  # what the service is serving when the script runs
PREVIOUS = commit("dead")  # the deploy the rollback targets
OLDER = commit("older")  # a third commit, older than PREVIOUS
OTHER = commit("0bad")  # a commit whose deploys all failed

LIST_ENDPOINT = f"https://api.render.com/v1/services/{SENTINEL_SERVICE_ID}/deploys?limit=20"
DEPLOY_ENDPOINT = f"https://api.render.com/v1/services/{SENTINEL_SERVICE_ID}/deploys"


def deploy(identifier: str, status: str, sha: str) -> dict[str, dict[str, object]]:
    """One item of Render's deploy list, wrapped the way the endpoint wraps it."""
    return {"deploy": {"id": identifier, "status": status, "commit": {"id": sha}}}


def run_rollback(
    tmp_path: Path,
    deploys: Sequence[list[dict[str, dict[str, object]]]],
    *,
    arguments: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
    post_status: str | None = None,
    get_exit: int | None = None,
    raw_body: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The real script, run through `/bin/bash`, answering from `deploys`.

    `deploys` is one page per GET, in order; the last page is reused for every
    further GET. The timeout is a test guard: no fixture here waits, so a run that
    reaches one of the script's own waits is a fault, not slowness.

    Every run's output is checked for the sentinel key here rather than in each
    test, because the rule is about the script and not about one case.
    """
    run = tmp_path / f"run-{next(_RUNS)}"
    state = run / "state"
    state.mkdir(parents=True)
    binaries = run / "bin"
    binaries.mkdir()
    shim = binaries / "curl"
    shim.write_text(_SHIM, encoding="utf-8")
    shim.chmod(0o755)

    for index, page in enumerate(deploys, start=1):
        (state / f"get-{index}.json").write_text(json.dumps(page), encoding="utf-8")
    if post_status is not None:
        (state / "post-status").write_text(post_status, encoding="utf-8")
    if get_exit is not None:
        (state / "get-exit").write_text(str(get_exit), encoding="utf-8")
    if raw_body is not None:
        (state / "get-raw").write_text(raw_body, encoding="utf-8")

    child = dict(os.environ)
    child["RENDER_API_KEY"] = SENTINEL_KEY
    child["RENDER_SERVICE_ID"] = SENTINEL_SERVICE_ID
    child["FAKE_CURL_STATE"] = str(state)
    child["PATH"] = os.pathsep.join((str(binaries), child.get("PATH", "")))
    for name in unset:
        child.pop(name, None)
    child.update(environment or {})

    completed = subprocess.run(
        ["/bin/bash", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env=child,
        timeout=60,
    )

    assert SENTINEL_KEY not in completed.stdout + completed.stderr, (
        "a run of the rollback script printed the credential"
    )
    return completed, state


def calls(state: Path) -> str:
    """Every curl the shim answered, one `REQUEST url` line each, or `""` for none."""
    log = state / "calls.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def output_of(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


# The page a healthy service answers: CURRENT is live, the deploy before it is
# deactivated, and other commits' deploys are interleaved through the list.
_HEALTHY_PAGE = [
    deploy("dep-current", "live", CURRENT),
    deploy("dep-failed", "build_failed", OTHER),
    deploy("dep-previous", "deactivated", PREVIOUS),
    deploy("dep-older", "deactivated", OLDER),
]

# The same page after the rollback was triggered: the deploy of PREVIOUS this run
# created is newest and live, and the deploy that was live is now deactivated.
_PAGE_AFTER_ROLLBACK = [
    deploy("dep-rollback", "live", PREVIOUS),
    deploy("dep-current", "deactivated", CURRENT),
    deploy("dep-failed", "build_failed", OTHER),
    deploy("dep-previous", "deactivated", PREVIOUS),
    deploy("dep-older", "deactivated", OLDER),
]


def test_it_rolls_back_to_the_previous_deploy_when_render_reports_it_deactivated(
    tmp_path: Path,
) -> None:
    """`deactivated` is what a superseded deploy carries, and it is a success."""
    completed, state = run_rollback(tmp_path, [_HEALTHY_PAGE, _PAGE_AFTER_ROLLBACK])
    output = output_of(completed)

    assert completed.returncode == 0, output
    assert f"being served: {CURRENT}" in output
    assert f"rollback target: {PREVIOUS}" in output
    assert "POST" in calls(state)


def test_it_rolls_back_to_the_previous_deploy_when_render_still_reports_it_live(
    tmp_path: Path,
) -> None:
    """The other spelling: Render may leave the superseded deploy `live`.

    Which one the previous deploy carries is Render's choice, so the rule accepts
    both and the rollback does not depend on that answer.
    """
    page = [
        deploy("dep-current", "live", CURRENT),
        deploy("dep-previous", "live", PREVIOUS),
    ]
    after = [deploy("dep-rollback", "live", PREVIOUS), *page]

    completed, _ = run_rollback(tmp_path, [page, after])
    output = output_of(completed)

    assert completed.returncode == 0, output
    assert f"rollback target: {PREVIOUS}" in output


def test_it_names_the_deploy_it_triggered_and_not_the_target_commit_s_old_deploy(
    tmp_path: Path,
) -> None:
    """The target commit is already listed before the POST, so only a new id is it.

    A match on `commit.id` alone names the target's previous deploy -- which is in
    the pre-POST list and was not triggered by this run -- so this case fails if
    the identification is by commit instead of by deploy id.
    """
    before = [
        deploy("dep-current", "live", CURRENT),
        deploy("dep-old", "deactivated", PREVIOUS),
    ]
    after = [deploy("dep-new", "live", PREVIOUS), *before]

    completed, _ = run_rollback(tmp_path, [before, after])
    output = output_of(completed)

    assert completed.returncode == 0, output
    assert "dep-new" in output
    assert "dep-old" not in output


def test_it_refuses_when_nothing_is_live(tmp_path: Path) -> None:
    """No `live` deploy means nothing is being served to roll back from."""
    page = [deploy("dep-previous", "deactivated", PREVIOUS)]

    completed, state = run_rollback(tmp_path, [page])
    output = output_of(completed)

    assert completed.returncode != 0
    assert "nothing is being served" in output
    assert "POST" not in calls(state)
    assert calls(state).startswith("GET")


def test_it_refuses_when_the_only_succeeded_deploy_is_the_one_being_served(
    tmp_path: Path,
) -> None:
    """The serving commit's own older deploys are not a rollback target."""
    page = [
        deploy("dep-current", "live", CURRENT),
        deploy("dep-failed", "build_failed", OTHER),
        deploy("dep-current-old", "deactivated", CURRENT),
    ]

    completed, state = run_rollback(tmp_path, [page])
    output = output_of(completed)

    assert completed.returncode != 0
    assert "limit=20" in output
    assert "POST" not in calls(state)


def test_it_refuses_when_the_target_is_older_than_the_newest_20(tmp_path: Path) -> None:
    """The page is the newest 20, so the refusal says the page was read, not that history is empty.

    The fixture is what Render answers for `limit=20` when the last known-good
    deploy sits beyond the page: twenty entries, none of them a succeeded deploy of
    another commit.
    """
    page = [deploy("dep-current", "live", CURRENT)]
    page += [deploy(f"dep-current-{index}", "deactivated", CURRENT) for index in range(1, 10)]
    page += [deploy(f"dep-failed-{index}", "build_failed", OTHER) for index in range(1, 11)]
    assert len(page) == 20

    completed, state = run_rollback(tmp_path, [page])
    output = output_of(completed)

    assert completed.returncode != 0
    assert "limit=20" in output
    assert "POST" not in calls(state)


@pytest.mark.parametrize("missing", [("RENDER_API_KEY",), ("RENDER_SERVICE_ID",)])
def test_it_refuses_when_a_variable_is_unset(tmp_path: Path, missing: tuple[str, ...]) -> None:
    completed, state = run_rollback(tmp_path, [_HEALTHY_PAGE], unset=missing)
    output = output_of(completed)

    assert completed.returncode != 0
    assert missing[0] in output
    assert calls(state) == ""


def test_it_names_every_missing_variable_and_sends_no_request(tmp_path: Path) -> None:
    """One message, both names: stopping at the first variable is the failure this pins."""
    completed, state = run_rollback(
        tmp_path, [_HEALTHY_PAGE], unset=("RENDER_API_KEY", "RENDER_SERVICE_ID")
    )
    output = output_of(completed)

    assert completed.returncode != 0
    assert "RENDER_API_KEY" in output
    assert "RENDER_SERVICE_ID" in output
    assert calls(state) == ""


def test_it_refuses_an_empty_variable(tmp_path: Path) -> None:
    """Empty is unset: an exported-but-empty value is the same fault."""
    completed, state = run_rollback(
        tmp_path, [_HEALTHY_PAGE], environment={"RENDER_API_KEY": ""}
    )
    output = output_of(completed)

    assert completed.returncode != 0
    assert "RENDER_API_KEY" in output
    assert calls(state) == ""


def test_dry_run_prints_the_two_shas_and_sends_no_deploy_request(tmp_path: Path) -> None:
    completed, state = run_rollback(tmp_path, [_HEALTHY_PAGE], arguments=("--dry-run",))
    output = output_of(completed)

    assert completed.returncode == 0, output
    assert f"being served: {CURRENT}" in output
    assert f"rollback target: {PREVIOUS}" in output
    assert "POST" not in calls(state)
    assert calls(state).startswith("GET")


def test_an_unknown_argument_is_refused_rather_than_read_as_a_commit(tmp_path: Path) -> None:
    """No sha is ever passed in, so an argument that is not `--dry-run` is refused."""
    completed, state = run_rollback(tmp_path, [_HEALTHY_PAGE], arguments=(CURRENT,))
    output = output_of(completed)

    assert completed.returncode != 0
    assert CURRENT not in output
    assert calls(state) == ""


def test_a_non_2xx_post_fails_naming_the_status_and_the_endpoint(tmp_path: Path) -> None:
    completed, _ = run_rollback(tmp_path, [_HEALTHY_PAGE], post_status="500")
    output = output_of(completed)

    assert completed.returncode != 0
    assert "500" in output
    assert DEPLOY_ENDPOINT in output
    assert f"rollback target: {PREVIOUS}" in output


def test_a_deploy_that_ends_build_failed_fails_naming_the_status(tmp_path: Path) -> None:
    """The script exits 0 only once the deploy it triggered is `live`."""
    after = [
        deploy("dep-rollback", "build_failed", PREVIOUS),
        *_HEALTHY_PAGE,
    ]

    completed, _ = run_rollback(tmp_path, [_HEALTHY_PAGE, after])
    output = output_of(completed)

    assert completed.returncode != 0
    assert "build_failed" in output


@pytest.mark.parametrize("body", ["-not-a-list-", '{"error": "what?"}'])
def test_a_body_that_is_not_a_deploy_list_fails_without_echoing_it(
    tmp_path: Path, body: str
) -> None:
    """A body that is not the list is named as such, and never printed: it could echo the key."""
    completed, _ = run_rollback(tmp_path, [], raw_body=f"{body} {SENTINEL_KEY}")
    output = output_of(completed)

    assert completed.returncode != 0
    assert "not a JSON array" in output
    assert body not in output
    assert SENTINEL_KEY not in output


def test_a_read_that_does_not_answer_fails_naming_the_endpoint(tmp_path: Path) -> None:
    completed, _ = run_rollback(tmp_path, [_HEALTHY_PAGE], get_exit=28)
    output = output_of(completed)

    assert completed.returncode != 0
    assert "curl exit 28" in output
    assert LIST_ENDPOINT in output
