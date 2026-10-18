"""`ops/oncall/recheck.py`'s scheduled re-check, driven offline over fakes.

#97's AC1 is `test_a_scheduled_run_re_reads_the_apps_signals_and_acts_again` and
`test_the_action_set_and_the_rule_are_not_copied_here`: the scheduled run reads the
app's own signals again and takes its action through #93's own rule and action set. AC2
and AC3's bound and stop condition are pinned by the `..._bound` and
`..._stop...` cases: `MAX_ATTEMPTS` attempts per incident, the last of them carrying the
one stop notice, and every later run touching nothing. AC3's other half -- that the run
cannot start another run of its own -- is pinned over the module's whole command
vocabulary and the workflow's own conditions and permissions. AC4's record is pinned by
the attempt comment carrying the workflow run URL, the signals it read and its attempt
count. The awkward paths #93 answers -- an unreachable app, an answer outside the action
set, a seam that raises -- are driven here too, because an attempt must not be the one
wake that leaves an exception on the issue.

The fakes are #93's own seams (`App`, `Complete`, `Rollback`, `Comment`, `CloseIssue`),
so no socket, no model, no Render service and no `gh` is touched, and the previous
attempts a bound case reads are built by this module's own comment writer rather than
typed out, so the markers the count is read from cannot drift from the ones it writes.

The module lives under `ops/`, outside the backend package, so it is loaded by path the
way `ops/oncall/test_responder.py` loads `responder.py`: it is the real file, not a copy
that could drift.
"""

import importlib.util
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULE_PATH = REPO_ROOT / "ops" / "oncall" / "recheck.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "oncall.yml"

_spec = importlib.util.spec_from_file_location("recheck", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
recheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recheck)

responder = recheck.responder
Answer = responder.Answer

ISSUE_NUMBER = 97
BASE_URL = "https://jobradar.example.test"
RUN_URL = "https://github.com/example/jobradar/actions/runs/1234"

# The alert body #92's `Report.body` writes, with the newest run on its own line.
ALERT_BODY = "\n".join(
    [
        "## JobRadar digest journey",
        "",
        "User impact detected: `newest_run_failed`.",
        "",
        "- id `7`, status `failed`",
    ]
)

# A planted credential and a planted connection string, assembled from parts so this
# module's own source carries neither a secret-shaped literal nor a URL with a password
# (the rule `backend/tests/test_committed_data_guard.py` enforces).
SECRET = "oncall-" + "sentinel-" + "not-a-real-key"
CONNECTION_STRING = "postgresql://jobradar:" + "planted" + "@db.example.test:5432/jobradar"


class FakeApp:
    """An app read that answers from a route table and records every call (as in #93's test)."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self._routes = dict(routes)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, path: str, headers: dict[str, str]) -> Answer:
        self.calls.append((method, path, dict(headers)))
        assert (method, path) in self._routes, f"unexpected {method} {path}"
        route = self._routes[(method, path)]
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route

    def called(self, method: str, path: str) -> bool:
        return any(
            call_method == method and call_path == path
            for call_method, call_path, _ in self.calls
        )


class FakeClock:
    """A monotonic clock whose `sleep` advances it, so a bounded wait is instant and exact."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def run_body(run_id: int, status: str) -> dict[str, Any]:
    """One `GET /runs/{id}` body, the fields #93's responder reads among them."""
    return {"id": run_id, "status": status, "error": None}


def evidence_routes(
    *, health: Any = None, metrics: Any = None, named_run: Any = None
) -> dict[tuple[str, str], Any]:
    """The three reads one attempt makes, with per-case answers."""
    return {
        ("GET", "/healthz"): Answer(200) if health is None else health,
        ("GET", "/telemetry/metrics"): Answer(200, []) if metrics is None else metrics,
        ("GET", "/runs/7"): (
            Answer(200, run_body(7, "failed")) if named_run is None else named_run
        ),
    }


def a_retrigger() -> dict[tuple[str, str], Any]:
    """The evidence reads plus a tick that starts a run which succeeds."""
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
    routes[("GET", "/runs/12")] = Answer(200, run_body(12, "succeeded"))
    return routes


def attempt_comment(attempt: int, *, fixed: bool = False) -> str:
    """A previous attempt's comment, written by this module's own comment writer."""
    outcome = responder.Outcome(
        action=responder.ESCALATE,
        source="deterministic",
        reason="the evidence named no remediation in the fixed action set",
        found="`GET /healthz` -> HTTP 200",
        performed=None,
        run_id=7,
        trace_id=None,
        fixed=fixed,
        owner_action=None if fixed else "a human must investigate the alert.",
    )
    return recheck.comment_body(attempt, outcome)


def recheck_offline(
    comments: Sequence[str] = (),
    *,
    state: str = "open",
    routes: dict[tuple[str, str], Any] | None = None,
    complete: Callable[[str], str | None] | None = None,
    rollback: Callable[[], tuple[int, str]] | None = None,
    secrets: tuple[str, ...] = (),
    run_url: str | None = None,
) -> tuple[Any, list[tuple[int, str]], list[int], FakeApp]:
    """One attempt of the re-check: its outcome, what it commented and closed, and the app."""
    app = FakeApp(routes if routes is not None else evidence_routes())
    written: list[tuple[int, str]] = []
    closed: list[int] = []
    clock = FakeClock()
    outcome = recheck.recheck_issue(
        responder.Issue(ISSUE_NUMBER, state, ALERT_BODY, tuple(comments)),
        app=app,
        complete=complete,
        rollback=rollback or (lambda: (0, "rolled back")),
        comment=lambda number, body: written.append((number, body)),
        close=closed.append,
        config=responder.Config(base_url=BASE_URL, secrets=secrets),
        sleep=clock.sleep,
        clock=clock,
        run_url=run_url,
    )
    return outcome, written, closed, app


# AC1: one scheduled run, re-reading the app and answering through #93's own path.


def test_a_scheduled_run_re_reads_the_apps_signals_and_acts_again() -> None:
    outcome, written, closed, app = recheck_offline(routes=a_retrigger(), run_url=RUN_URL)

    assert outcome is not None
    assert outcome.action == responder.RETRIGGER
    assert outcome.fixed is True
    # The signals, read again from the deployed app, and the one action it took.
    assert app.calls == [
        ("GET", "/healthz", {}),
        ("GET", "/telemetry/metrics", {}),
        ("GET", "/runs/7", {}),
        ("POST", "/internal/tick", {responder.ORIGIN_HEADER: BASE_URL}),
        ("GET", "/runs/12", {}),
    ]
    assert closed == [ISSUE_NUMBER]
    assert len(written) == 1
    body = written[0][1]
    # The attempt's own bound and its run URL, then #93's own comment body verbatim.
    assert recheck.RECHECK_MARKER in body
    assert f"attempt 1 of {recheck.MAX_ATTEMPTS}" in body
    assert RUN_URL in body
    assert responder.COMMENT_MARKER in body
    assert "**Fixed:** yes" in body


def test_the_action_set_and_the_rule_are_not_copied_here() -> None:
    """The attempt decides through `responder`, not through a second copy of #93's rule."""
    for name in ("ACTIONS", "RETRIGGER", "ROLLBACK", "ESCALATE", "deterministic_decision"):
        assert not hasattr(recheck, name)
    assert responder.ACTIONS == (responder.RETRIGGER, responder.ROLLBACK, responder.ESCALATE)


@pytest.mark.parametrize(
    ("routes", "action", "fixed"),
    [
        (a_retrigger(), responder.RETRIGGER, True),
        (evidence_routes(named_run=Answer(200, run_body(7, "succeeded"))), responder.ESCALATE, None),
        (evidence_routes(health=Answer(None)), responder.ESCALATE, None),
    ],
)
def test_the_attempt_chooses_what_the_responders_own_rule_chooses(
    routes: dict[tuple[str, str], Any], action: str, fixed: bool | None
) -> None:
    """Each signal set ends in the action #93's documented rule decides for it."""
    outcome, _, _, _ = recheck_offline(routes=routes)

    assert outcome is not None
    assert outcome.source == "deterministic"
    assert outcome.action == action
    assert outcome.fixed is fixed
    expected = responder.deterministic_decision(
        responder.read_evidence(FakeApp(routes), ALERT_BODY)
    )
    assert expected.action == action


def test_a_rollback_that_the_rule_chooses_runs_the_same_script_a_wake_runs() -> None:
    routes = evidence_routes(health=[Answer(503), Answer(200)])
    calls: list[str] = []

    outcome, _, closed, _ = recheck_offline(
        routes=routes, rollback=lambda: (calls.append("rollback.sh"), (0, "deployed"))[1]
    )

    assert outcome is not None
    assert outcome.action == responder.ROLLBACK
    assert calls == ["rollback.sh"]
    assert closed == [ISSUE_NUMBER]


def test_a_model_answer_outside_the_action_set_escalates_and_changes_nothing() -> None:
    outcome, written, closed, app = recheck_offline(
        routes=a_retrigger(),
        complete=lambda prompt: json.dumps({"action": "redeploy", "reason": "try anything"}),
    )

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "invalid-answer"
    assert not app.called("POST", "/internal/tick")
    assert closed == []
    assert len(written) == 1


# AC2, AC3: the bound, and the one stop notice.


def test_the_bound_is_the_stated_number_of_attempts() -> None:
    assert recheck.MAX_ATTEMPTS == 3
    for made in range(recheck.MAX_ATTEMPTS):
        pending = responder.Issue(
            ISSUE_NUMBER,
            "open",
            ALERT_BODY,
            tuple(attempt_comment(number) for number in range(1, made + 1)),
        )
        assert recheck.next_attempt(pending) == made + 1


def test_the_last_attempt_carries_the_one_stop_notice_and_names_the_person_action() -> None:
    comments = tuple(attempt_comment(number) for number in range(1, recheck.MAX_ATTEMPTS))

    outcome, written, closed, _ = recheck_offline(
        comments, routes=evidence_routes(named_run=Answer(200, run_body(7, "succeeded")))
    )

    assert outcome is not None
    assert outcome.fixed is None
    assert closed == []
    body = written[0][1]
    assert recheck.STOP_MARKER in body
    assert f"made its {recheck.MAX_ATTEMPTS} attempts" in body
    assert "no further one" in body
    assert "A person must take it from here." in body
    assert outcome.owner_action in body


def test_no_further_attempt_is_made_once_the_bound_is_reached() -> None:
    """The bound holds even for an incident whose last attempt verified nothing to stop."""
    made = tuple(
        attempt_comment(number, fixed=number == recheck.MAX_ATTEMPTS)
        for number in range(1, recheck.MAX_ATTEMPTS + 1)
    )
    comments = made

    outcome, written, closed, app = recheck_offline(comments)

    assert recheck.STOP_MARKER not in made[-1]
    assert outcome is None
    assert written == [] and closed == [] and app.calls == []


def test_an_issue_that_already_carries_the_stop_notice_is_left_alone() -> None:
    outcome, written, closed, app = recheck_offline((attempt_comment(1), recheck.STOP_MARKER))

    assert outcome is None
    assert written == [] and closed == [] and app.calls == []


def test_a_closed_issue_is_never_re_checked() -> None:
    outcome, written, closed, app = recheck_offline(state="closed", routes=a_retrigger())

    assert outcome is None
    assert written == [] and closed == [] and app.calls == []


def test_one_attempt_writes_one_comment_at_most_and_closes_only_a_verified_fix() -> None:
    cases = [
        (a_retrigger(), [], None),
        (evidence_routes(), None, None),
        (evidence_routes(health=Answer(503)), None, (0, "deployed")),
        (evidence_routes(health=Answer(503)), None, (1, "Render refused")),
    ]
    for routes, complete, rollback in cases:
        outcome, written, closed, _ = recheck_offline(
            routes=routes, complete=complete, rollback=rollback
        )
        assert outcome is not None
        assert len(written) == 1
        assert closed == ([ISSUE_NUMBER] if outcome.fixed is True else [])


# AC3: the run cannot start another run of its own.


def test_the_module_issues_one_gh_command_and_none_that_could_start_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued: list[list[str]] = []

    def run(argv: Sequence[str], **kwargs: Any) -> Any:
        issued.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps([{"number": ISSUE_NUMBER}]))

    monkeypatch.setattr(recheck.subprocess, "run", run)
    monkeypatch.setattr(
        recheck.responder,
        "gh_issue",
        lambda number: responder.Issue(number, "open", ALERT_BODY, ()),
    )

    issues = recheck.open_alert_issues()

    assert [issue.number for issue in issues] == [ISSUE_NUMBER]
    assert len(issued) == 1
    assert issued[0][:3] == ["gh", "issue", "list"]
    assert f"--label {recheck.ALERT_LABEL}" in " ".join(issued[0])
    for token in ("workflow", "api", "dispatch", "edit", "reopen"):
        assert token not in " ".join(issued[0])


def test_the_schedule_runs_the_re_check_alone_and_grants_no_way_to_start_a_run() -> None:
    """`oncall.yml`'s own conditions and permissions: the schedule starts nothing else."""
    text = WORKFLOW.read_text(encoding="utf-8")

    conditions = re.findall(r"^    if: (.+)$", text, re.MULTILINE)
    assert len(conditions) == 3
    issues_only = [
        condition
        for condition in conditions
        if condition.startswith("github.event_name == 'issues'")
    ]
    assert len(issues_only) == 2 and all("'alert'" in condition for condition in issues_only)
    assert [condition for condition in conditions if condition == "github.event_name == 'schedule'"]
    assert "\n  schedule:" in text
    # No job is granted the scope a workflow dispatch needs, so none can start a run.
    grants = re.findall(
        r"^\s{2,6}(actions|contents|issues|pull-requests): (\S+)$", text, re.MULTILINE
    )
    assert ("actions", "write") not in grants
    assert ("contents", "read") in grants and ("issues", "write") in grants


# AC4: the record the comment carries.


def test_the_attempt_records_the_run_url_the_signals_and_the_attempt_count() -> None:
    outcome, written, _, _ = recheck_offline(routes=a_retrigger(), run_url=RUN_URL)

    body = written[0][1]

    assert outcome is not None
    assert f"- **Scheduled run:** {RUN_URL}" in body
    assert f"attempt 1 of {recheck.MAX_ATTEMPTS}" in body
    assert "`GET /healthz` -> HTTP 200" in body
    assert "`GET /runs/7` -> status `failed`" in body


def test_the_run_url_comes_from_the_runners_environment_and_is_absent_off_it() -> None:
    environment = {
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "example/jobradar",
        "GITHUB_RUN_ID": "1234",
    }

    assert (
        recheck.workflow_run_url(environment)
        == "https://github.com/example/jobradar/actions/runs/1234"
    )
    assert recheck.workflow_run_url({}) is None
    assert recheck.workflow_run_url({"GITHUB_SERVER_URL": "https://github.com"}) is None


def test_an_offline_run_records_no_run_url() -> None:
    _, written, _, _ = recheck_offline(routes=a_retrigger())

    assert "- **Scheduled run:** not recorded" in written[0][1]


# AGENTS.md: nothing private reaches an attempt's comment.


def test_a_secret_in_the_model_reason_reaches_no_comment() -> None:
    fake_model = json.dumps(
        {"action": responder.RETRIGGER, "reason": f"key {SECRET} at {CONNECTION_STRING}"}
    )

    _, written, _, _ = recheck_offline(
        routes=a_retrigger(), complete=lambda prompt: fake_model, secrets=(SECRET,)
    )

    body = written[0][1]
    assert SECRET not in body
    assert CONNECTION_STRING not in body
    assert "[redacted]" in body


# The wake's own environment, and its loop over the open incidents.


def test_main_refuses_an_environment_without_the_app_to_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOBRADAR_URL", raising=False)

    assert recheck.main() == 2


def test_main_makes_one_attempt_per_open_alert_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBRADAR_URL", BASE_URL)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    seen: list[tuple[int, str | None]] = []
    listed = [
        responder.Issue(1, "open", ALERT_BODY, ()),
        responder.Issue(2, "open", ALERT_BODY, ()),
    ]

    monkeypatch.setattr(recheck, "open_alert_issues", lambda: listed)
    monkeypatch.setattr(
        recheck,
        "recheck_issue",
        lambda issue, **kwargs: seen.append((issue.number, kwargs["run_url"])),
    )

    assert recheck.main() == 0
    assert [number for number, _ in seen] == [1, 2]


def test_main_reports_an_issue_list_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JOBRADAR_URL", BASE_URL)

    def boom() -> Any:
        raise subprocess.CalledProcessError(1, ["gh", "issue", "list"])

    monkeypatch.setattr(recheck, "open_alert_issues", boom)

    assert recheck.main() == 1
    assert "could not read the open alert issues" in capsys.readouterr().err


def test_a_seam_that_raises_still_gets_one_escalation_comment() -> None:
    """No failure inside an attempt lets an exception stand in for an answer."""

    class Boom(RuntimeError):
        pass

    def raising_app(method: str, path: str, headers: dict[str, str]) -> Any:
        raise Boom("the seam raised instead of answering")

    written: list[tuple[int, str]] = []
    outcome = recheck.recheck_issue(
        responder.Issue(ISSUE_NUMBER, "open", ALERT_BODY, ()),
        app=raising_app,
        complete=None,
        rollback=lambda: (0, ""),
        comment=lambda number, body: written.append((number, body)),
        close=lambda number: None,
        config=responder.Config(base_url=BASE_URL),
    )

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "error"
    assert len(written) == 1
    assert "Boom" in written[0][1]
