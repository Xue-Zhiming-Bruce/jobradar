"""On-call re-check: a scheduled run that re-reads the app's signals and re-escalates a
still-open incident, bounded.

This is the fourth response of the on-call agent (#97), and the one that needs nobody
awake. `responder.py` (#93) wakes on the `alert` label `alert.yml` (#92) applies, takes
at most one bounded action and answers on the issue -- once, when the label arrives. A
remediation that did not hold, or an escalation nobody picked up, therefore stays silent
until a person notices. This module is the scheduled run that notices: it re-reads the
deployed app's own signals while an incident is still open and answers again.

**What one scheduled run does (AC1).** `open_alert_issues` reads every open `alert`
issue, and each of them gets one attempt: the app's own signals are read again and one
action is chosen from the fixed set, through #93's own evidence read, action set and
escalation rule (`responder.decide_and_act`) rather than a second copy of either. The
answer the attempt comments is #93's own comment body, quoted verbatim by `comment_body`
below, so what the attempt found, did and verified is the same text a wake writes -- and
the field #96's proposer reads (`- **Fixed:**`) is the same one.

**The bound (AC2).** `MAX_ATTEMPTS` is the stated maximum number of attempts per
incident, and the attempt count is read back from the comments this module itself wrote
(`RECHECK_MARKER`), so the bound holds across runs and not only inside one. The last
attempt's comment carries the one notice that names it stopped and what a person must do,
and carries `STOP_MARKER`; every later run does nothing at all. An incident a run finds
fixed is closed by the same rule a wake uses, so a re-check that verifies the fix ends the
incident rather than re-reporting it.

**What it never does (AC3).** No path here schedules, dispatches or labels anything: the
only `gh` commands it can issue are its own `gh issue list` and #93's issue read, comment
and close, and the scheduled job in `oncall.yml` is granted no `actions: write`, so this
run cannot start another run of its own. A comment it writes fires `issue_comment`, not
the `issues` event `oncall.yml` wakes on, and it re-adds no `alert` label, so a re-check
cannot cascade into a wake either. The bound is the second half of AC3: the incident is
re-attempted at most `MAX_ATTEMPTS` times and then once named as stopped.

shortcut: a scheduled run and a wake on the same issue are serialised only among
themselves, so the two can act once each if they start in the same instant; upgrade if
the schedule ever runs more often than the alert's own hourly check.

**The trigger (AC4).** `oncall.yml` runs it hourly on a cron offset from `alert.yml`'s
hourly check, and that run's URL -- from `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and
`GITHUB_RUN_ID` -- is recorded in the attempt's comment with the signals it read, what it
attempted and the attempt count it reached. Like #93's wake, the scheduled job only fires
once this file is on the default branch.

**What it never carries (AGENTS.md).** The comment is #93's own, which quotes signals,
ids and statuses only, and every value it passes on keeps #93's redaction.

The standard library and `responder` are the whole dependency: the seams `recheck_issue`
takes are #93's own (`App`, `Complete`, `Rollback`, `Comment`, `CloseIssue`), so the
offline test drives every branch without a socket, a model, a Render service or a GitHub
token.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

# `ops/oncall` is not a package: its modules are loaded by path -- the way the offline
# tests load them -- so this file puts its own directory on `sys.path` before importing
# its neighbour, and works both as `python3 ops/oncall/recheck.py` and loaded by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import responder  # noqa: E402 -- the import above must run first

# The one label #92's `alert.yml` applies and #93's wake reads, spelled once.
ALERT_LABEL = "alert"

# The marker every attempt comment carries, and the one the single stop notice carries.
# Both are read back off the issue's own comments, so the attempt count and the stop
# condition survive a run that dies between its action and its comment -- which is the one
# failure this module cannot report on the issue, exactly as #93's own comment cannot.
RECHECK_MARKER = "<!-- jobradar-oncall-recheck -->"
STOP_MARKER = "<!-- jobradar-oncall-recheck-stopped -->"

# The stated maximum: three scheduled attempts per incident, after which one comment names
# it stopped and no later run makes a fourth. At #97's hourly schedule that is three
# hours of unattended re-attempts, which is the bound this task asks for rather than a
# knob nobody asked to tune.
MAX_ATTEMPTS = 3


def open_alert_issues() -> list[responder.Issue]:
    """Every open `alert` issue, each read through #93's own issue read.

    The list is filtered by `--state open`, which is what "while an incident is still
    open" means operationally: a closed issue is not re-checked at all. A run that cannot
    list them raises, and `main` reports it rather than inventing an incident.
    """
    completed = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--label",
            ALERT_LABEL,
            "--state",
            "open",
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [responder.gh_issue(int(row["number"])) for row in json.loads(completed.stdout)]


def next_attempt(issue: responder.Issue) -> int | None:
    """The attempt this run may make on this incident, or `None` when it makes none.

    `None` for an issue that is no longer open, for one whose comments already carry the
    stop notice (`STOP_MARKER`), and for one that has already had `MAX_ATTEMPTS`
    attempts: the bound is read from the attempts themselves, so it holds even for the
    last attempt that was fixed and closed, or whose stop notice was never written.
    """
    if issue.state.lower() != "open":
        return None
    if any(STOP_MARKER in body for body in issue.comments):
        return None
    made = sum(1 for body in issue.comments if RECHECK_MARKER in body)
    return made + 1 if made < MAX_ATTEMPTS else None


def stopped_section(outcome: responder.Outcome) -> str:
    """The one notice that names the re-check as stopped, and what a person must do (AC2)."""
    lines = [
        STOP_MARKER,
        f"**Stopped: this scheduled re-check has made its {MAX_ATTEMPTS} attempts on this "
        "incident, so it will make no further one.** A person must take it from here.",
    ]
    if outcome.owner_action:
        lines.append(outcome.owner_action)
    return "\n".join(lines)


def comment_body(
    attempt: int, outcome: responder.Outcome, run_url: str | None = None
) -> str:
    """The one comment an attempt writes: the bound it is under, and #93's own answer.

    The responder's own comment body follows verbatim, so a reader sees what this attempt
    found, did and verified in the same fields a wake writes them in. The last attempt of
    an incident that is not fixed also carries the one stop notice.
    """
    lines = [
        RECHECK_MARKER,
        f"## Scheduled on-call re-check: attempt {attempt} of {MAX_ATTEMPTS}",
        "",
        f"- **Scheduled run:** {run_url or 'not recorded'}",
        "",
        responder.comment_body(outcome),
    ]
    if attempt == MAX_ATTEMPTS and outcome.fixed is not True:
        lines += ["", stopped_section(outcome)]
    return "\n".join(lines)


def recheck_issue(
    issue: responder.Issue,
    *,
    app: responder.App,
    complete: responder.Complete | None,
    rollback: responder.Rollback,
    comment: responder.Comment,
    close: responder.CloseIssue,
    config: responder.Config,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    run_url: str | None = None,
) -> responder.Outcome | None:
    """Make this incident's next attempt, or nothing at all when it has none left.

    Answers the `Outcome` it commented, and `None` when `next_attempt` allows no further
    attempt -- a closed, stopped or exhausted incident, which is a no-op that touches
    nothing. The decision, the action and its verification are #93's own
    (`responder.decide_and_act`), wrapped the same way, so a seam that raises still gets
    the one escalation comment rather than an unhandled exception. The comment is written
    outside that wrapper and the close runs only after it, exactly as in a wake: one
    attempt writes at most one comment, and closes the issue only on a verified fix.
    """
    attempt = next_attempt(issue)
    if attempt is None:
        return None
    try:
        outcome = responder.decide_and_act(
            issue,
            app=app,
            complete=complete,
            rollback=rollback,
            config=config,
            sleep=sleep,
            clock=clock,
        )
    except Exception as error:  # an exception must not stand in for an answer
        outcome = responder.failure_outcome(error, config)
    comment(issue.number, comment_body(attempt, outcome, run_url))
    if outcome.fixed is True:
        close(issue.number)
    return outcome


def workflow_run_url(environment: Mapping[str, str] | None = None) -> str | None:
    """This run's own URL, from the runner's environment (AC4), or `None` off a runner."""
    names = os.environ if environment is None else environment
    server = names.get("GITHUB_SERVER_URL", "").strip()
    repository = names.get("GITHUB_REPOSITORY", "").strip()
    run_id = names.get("GITHUB_RUN_ID", "").strip()
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return None


def main() -> int:
    """Read the environment, make one attempt per open `alert` issue, and say what it did.

    `JOBRADAR_URL` is the one value a re-check cannot do without: without it there is no
    app to re-read, so the run is red with an `::error::` naming it rather than quietly
    checking nothing. The rest -- the Render credential a rollback needs, the optional
    model key -- is #93's own required and optional set, and a re-check that cannot roll
    back because Render refuses gets #93's escalation instead of an exception.
    """
    base_url = os.environ.get("JOBRADAR_URL", "").strip()
    if not base_url:
        print(
            "::error::JOBRADAR_URL is unset or empty, so no scheduled re-check can read "
            "the app's signals.",
            file=sys.stderr,
        )
        return 2

    secrets = responder.secrets_from_environment()
    config = responder.Config(base_url=base_url, secrets=secrets)
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    complete: responder.Complete | None = None
    if api_key:
        complete = responder.llm_complete(
            api_key,
            os.environ.get("LLM_MODEL", "").strip() or responder.DEFAULT_MODEL,
            os.environ.get("LLM_BASE_URL", "").strip() or responder.DEFAULT_MODEL_BASE_URL,
        )

    try:
        issues = open_alert_issues()
    except Exception as error:  # no incident was read, so there is nothing to re-check
        print(
            responder.redact(
                f"recheck: could not read the open {ALERT_LABEL} issues: {type(error).__name__}",
                secrets,
            ),
            file=sys.stderr,
        )
        return 1

    app = responder.urllib_app(base_url)
    run_url = workflow_run_url()
    for issue in issues:
        outcome = recheck_issue(
            issue,
            app=app,
            complete=complete,
            rollback=responder.real_rollback,
            comment=responder.gh_comment,
            close=responder.gh_close,
            config=config,
            run_url=run_url,
        )
        if outcome is None:
            print(f"recheck: #{issue.number} gets no further attempt.")
            continue
        print(
            responder.redact(
                f"recheck: #{issue.number} -> {outcome.action} -- {outcome.reason}", secrets
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
