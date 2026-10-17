"""`ops/oncall/responder.py`'s branches, driven offline over a fake app, model and Render call.

#93's AC5: the responder is exercised with a fake model, a fake app and a fake
Render API -- no model, no Render service and no `gh` -- so every branch is a fact
rather than a hope. The cases cover a remediation that is applied and verified,
one that is applied and not effective, the unhealthy-app rollback, a redaction
case, the no-key deterministic rule choosing the same action as the fake model on
the same evidence, and an answer from outside the fixed action set becoming
`escalate`. AC8's awkward answers -- a `409`, a wait that runs out, an unreachable
app, a failing model -- and AC9's fail-closed rollback are covered too. The other
AC8 case is a peer that accepts the connection and then stalls: those cases drive
the real `urllib` seams against a local socket so the `TimeoutError` that is not a
`URLError` is a fact rather than a hope.

The module lives under `ops/`, outside the backend package, so it is loaded by
path the way `backend/tests/test_impact_check.py` loads `ops/oncall/impact_check.py`:
it is the real file, not a copy that could drift.
"""

import importlib.util
import json
import socketserver
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULE_PATH = REPO_ROOT / "ops" / "oncall" / "responder.py"

_spec = importlib.util.spec_from_file_location("responder", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
responder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(responder)

Answer = responder.Answer
ISSUE_NUMBER = 93
BASE_URL = "https://jobradar.example.test"

# The alert body #92's `Report.body` writes, with the newest run on its own line.
ALERT_BODY = "\n".join(
    [
        "## JobRadar digest journey",
        "",
        "User impact detected: `newest_run_failed`.",
        "",
        "### The newest run",
        "",
        "- id `7`, status `failed`",
        "- started_at `2026-10-17T17:55:00Z`",
    ]
)

# A planted credential and a planted connection string, assembled from parts so
# this module's own source carries neither a secret-shaped literal nor a URL with
# a password (the rule `backend/tests/test_committed_data_guard.py` enforces).
SECRET = "oncall-" + "sentinel-" + "not-a-real-key"
CONNECTION_STRING = "postgresql://jobradar:" + "planted" + "@db.example.test:5432/jobradar"


class FakeApp:
    """An app read that answers from a route table and records every call.

    A route's value is one `Answer`, or a list of them consumed newest-first with
    the last one repeated -- which is how a poll that changes answer is driven. A
    call to a route that is not in the table is a test written wrong, so it fails
    by name instead of inventing an answer.
    """

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
        return any(method == call_method and path == call_path for call_method, call_path, _ in self.calls)


class FakeClock:
    """A monotonic clock whose `sleep` advances it, so a bounded wait is instant and exact."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def run_body(run_id: int, status: str) -> dict[str, Any]:
    """One `GET /runs/{id}` body, the fields this responder reads among them."""
    return {"id": run_id, "status": status, "error": None}


def evidence_routes(
    *,
    health: Any = None,
    metrics: Any = None,
    named_run: Any = None,
) -> dict[tuple[str, str], Any]:
    """The three reads `read_evidence` always makes, with per-case answers."""
    return {
        ("GET", "/healthz"): Answer(200) if health is None else health,
        ("GET", "/telemetry/metrics"): Answer(200, []) if metrics is None else metrics,
        ("GET", "/runs/7"): Answer(200, run_body(7, "failed")) if named_run is None else named_run,
    }


def make_responder(
    routes: dict[tuple[str, str], Any] | None,
    *,
    app: Any = None,
    base_url: str = BASE_URL,
    complete: Callable[[str], str | None] | None = None,
    rollback: Callable[[], tuple[int, str]] | None = None,
    secrets: tuple[str, ...] = (),
    run_wait_seconds: int = responder.RUN_WAIT_SECONDS,
    healthz_wait_seconds: int = responder.HEALTHZ_WAIT_SECONDS,
) -> tuple[Any, Any, list[tuple[int, str]], list[int], FakeClock]:
    """A responder harness: the app, the outcome, and what it commented and closed.

    `app` replaces the route-table `FakeApp` when a case drives the real `urllib`
    seam instead, and `base_url` moves the address the same-origin tick would post
    to. Both default to the offline fake.
    """
    if app is None:
        app = FakeApp(routes or {})
    comments: list[tuple[int, str]] = []
    closed: list[int] = []
    clock = FakeClock()
    outcome = responder.respond(
        responder.Issue(ISSUE_NUMBER, "open", ALERT_BODY, ()),
        app=app,
        complete=complete,
        rollback=rollback or (lambda: (0, "rolled back")),
        comment=lambda number, body: comments.append((number, body)),
        close=closed.append,
        config=responder.Config(
            base_url=base_url,
            run_wait_seconds=run_wait_seconds,
            healthz_wait_seconds=healthz_wait_seconds,
            secrets=secrets,
        ),
        sleep=clock.sleep,
        clock=clock,
    )
    return app, outcome, comments, closed, clock


@pytest.fixture
def stalled_peer(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A TCP peer that accepts the connection and never answers, and its base URL.

    This is the socket a `urllib` timeout fires on: a peer that is up and silent
    makes `getresponse` raise `TimeoutError`, which is not a `URLError`, so a read
    that raised it would leave the issue unanswered (AC8). The handler holds the
    connection until the client closes it, so no thread outlives the case.

    The opener is installed without proxies, so the silent peer is reached
    directly rather than through a proxy the machine's own settings carry, which
    would answer for it and hide the timeout.
    """

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(1)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if not self.request.recv(4096):
                        break  # the client closed the connection
                except OSError:
                    continue

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open,
    )
    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# AC1, AC3: one wake per alert.


def test_needs_a_response_refuses_a_closed_issue_and_one_already_answered() -> None:
    assert responder.needs_a_response(responder.Issue(1, "open", "", ()))
    assert not responder.needs_a_response(responder.Issue(1, "closed", "", ()))
    assert not responder.needs_a_response(
        responder.Issue(1, "open", "", (f"earlier response\n{responder.COMMENT_MARKER}",))
    )


def test_a_second_delivery_of_the_same_alert_touches_nothing() -> None:
    """The `opened` and `labeled` deliveries cannot remediate twice."""
    app = FakeApp(evidence_routes())
    comments: list[tuple[int, str]] = []
    outcome = responder.respond(
        responder.Issue(ISSUE_NUMBER, "open", ALERT_BODY, (responder.COMMENT_MARKER,)),
        app=app,
        complete=None,
        rollback=lambda: (0, ""),
        comment=lambda number, body: comments.append((number, body)),
        close=lambda number: None,
        config=responder.Config(base_url=BASE_URL),
    )

    assert outcome is None
    assert comments == []
    assert app.calls == []


# AC3, AC4: retrigger, verified.


def test_a_retrigger_that_succeeds_is_verified_and_closes_the_issue() -> None:
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(
        202, run_body(12, "running"), {responder.REQUEST_ID_HEADER: "trace-abc"}
    )
    routes[("GET", "/runs/12")] = [Answer(200, run_body(12, "running")), Answer(200, run_body(12, "succeeded"))]

    app, outcome, comments, closed, _ = make_responder(routes)

    assert outcome is not None
    assert outcome.action == responder.RETRIGGER
    assert outcome.fixed is True
    assert outcome.run_id == 12
    assert outcome.trace_id == "trace-abc"
    assert closed == [ISSUE_NUMBER]
    assert len(comments) == 1
    body = comments[0][1]
    assert "**Fixed:** yes" in body
    assert "`12`" in body and "`trace-abc`" in body


def test_the_retrigger_posts_the_same_origin_header_and_no_tick_secret() -> None:
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
    routes[("GET", "/runs/12")] = Answer(200, run_body(12, "succeeded"))

    app, _, _, _, _ = make_responder(routes)

    post = [call for call in app.calls if call[0] == "POST"]
    assert len(post) == 1
    assert post[0][1] == "/internal/tick"
    assert post[0][2].get(responder.ORIGIN_HEADER) == BASE_URL
    assert "X-Tick-Secret" not in post[0][2]


def test_a_retrigger_that_fails_again_leaves_the_issue_open() -> None:
    """Applied and not effective: the run ends `failed`, so the issue stays open."""
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
    routes[("GET", "/runs/12")] = [Answer(200, run_body(12, "running")), Answer(200, run_body(12, "failed"))]

    _, outcome, comments, closed, _ = make_responder(routes)

    assert outcome is not None
    assert outcome.action == responder.RETRIGGER
    assert outcome.fixed is False
    assert closed == []
    assert len(comments) == 1
    assert "**Fixed:** no" in comments[0][1]


# AC9: rollback, verified, and fail-closed.


def test_an_unhealthy_app_rolls_back_and_closes_on_a_healthy_read() -> None:
    routes = evidence_routes(health=[Answer(503), Answer(200)])
    _, outcome, comments, closed, _ = make_responder(routes)

    assert outcome is not None
    assert outcome.action == responder.ROLLBACK
    assert outcome.fixed is True
    assert closed == [ISSUE_NUMBER]
    assert len(comments) == 1


def test_a_rollback_that_refuses_escalates_and_leaves_the_issue_open() -> None:
    """`ops/rollback.sh` exited non-zero -- there is no earlier live commit, or Render refused."""
    routes = evidence_routes(health=[Answer(503)])
    _, outcome, comments, closed, _ = make_responder(
        routes, rollback=lambda: (1, "error: nothing is being served")
    )

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.fixed is None
    assert closed == []
    assert "nothing is being served" in comments[0][1]


def test_a_rollback_whose_healthz_never_answers_200_escalates() -> None:
    """The re-deployed commit never reached a healthy read inside the wait."""
    routes = evidence_routes(health=[Answer(503), Answer(503)])
    _, outcome, _, closed, _ = make_responder(
        routes, healthz_wait_seconds=0, rollback=lambda: (0, "deploy is live")
    )

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.fixed is False
    assert closed == []


# AC8: the awkward answers end in escalate.


def test_a_409_from_the_tick_escalates() -> None:
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(409, {"detail": "A run is already in progress"})

    _, outcome, comments, closed, _ = make_responder(routes)

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert closed == []
    assert "409" in comments[0][1]


def test_a_run_that_never_reaches_a_terminal_status_escalates() -> None:
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
    routes[("GET", "/runs/12")] = Answer(200, run_body(12, "running"))

    _, outcome, comments, closed, _ = make_responder(routes, run_wait_seconds=0)

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert closed == []
    assert "never reached a terminal status" in comments[0][1]


def test_an_unreachable_app_escalates() -> None:
    routes = evidence_routes(health=Answer(None))

    app, outcome, _, closed, _ = make_responder(routes)

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "deterministic"
    assert not app.called("POST", "/internal/tick")
    assert closed == []


# AC2: the model, the deterministic rule, and the fixed action set.


def test_a_failing_model_falls_back_to_the_deterministic_rule() -> None:
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
    routes[("GET", "/runs/12")] = Answer(200, run_body(12, "succeeded"))

    _, outcome, _, _, _ = make_responder(routes, complete=lambda prompt: None)

    assert outcome is not None
    assert outcome.source == "deterministic"
    assert outcome.action == responder.RETRIGGER


def test_the_no_key_rule_chooses_the_same_action_as_the_fake_model() -> None:
    """The documented rule and the model agree on the same evidence."""
    def routes() -> dict[tuple[str, str], Any]:
        table = evidence_routes()
        table[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
        table[("GET", "/runs/12")] = Answer(200, run_body(12, "succeeded"))
        return table

    fake_model = json.dumps({"action": responder.RETRIGGER, "reason": "the run failed"})
    _, with_model, _, _, _ = make_responder(routes(), complete=lambda prompt: fake_model)
    _, without_key, _, _, _ = make_responder(routes(), complete=None)

    assert with_model is not None and without_key is not None
    assert with_model.source == "llm"
    assert without_key.source == "deterministic"
    assert with_model.action == without_key.action == responder.RETRIGGER


def test_an_answer_outside_the_action_set_escalates() -> None:
    routes = evidence_routes()

    app, outcome, comments, closed, _ = make_responder(
        routes, complete=lambda prompt: json.dumps({"action": "redeploy", "reason": "try anything"})
    )

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "invalid-answer"
    assert not app.called("POST", "/internal/tick")
    assert closed == []
    assert len(comments) == 1


def test_an_unparseable_answer_escalates() -> None:
    routes = evidence_routes()
    _, outcome, _, _, _ = make_responder(routes, complete=lambda prompt: "I would retrigger it")

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "invalid-answer"


# AC6: nothing private reaches the comment.


def test_a_secret_in_the_model_reason_is_redacted_from_the_comment() -> None:
    routes = evidence_routes()
    routes[("POST", "/internal/tick")] = Answer(202, run_body(12, "running"), {})
    routes[("GET", "/runs/12")] = Answer(200, run_body(12, "succeeded"))
    fake_model = json.dumps(
        {"action": responder.RETRIGGER, "reason": f"key {SECRET} at {CONNECTION_STRING}"}
    )

    _, outcome, comments, _, _ = make_responder(
        routes, complete=lambda prompt: fake_model, secrets=(SECRET,)
    )

    body = comments[0][1]
    assert outcome is not None
    assert SECRET not in body
    assert CONNECTION_STRING not in body
    assert "[redacted]" in body


def test_a_secret_in_the_rollback_output_is_redacted_from_the_comment() -> None:
    routes = evidence_routes(health=[Answer(503)])
    _, _, comments, _, _ = make_responder(
        routes,
        rollback=lambda: (1, f"error: Render refused for {SECRET} {CONNECTION_STRING}"),
        secrets=(SECRET,),
    )

    body = comments[0][1]
    assert SECRET not in body
    assert CONNECTION_STRING not in body


def test_the_redaction_replaces_values_and_connection_strings() -> None:
    text = f"the key is {SECRET} and the database is {CONNECTION_STRING}"

    redacted = responder.redact(text, (SECRET,))

    assert SECRET not in redacted
    assert CONNECTION_STRING not in redacted
    assert "[redacted]" in redacted
    assert "[redacted connection string]" in redacted


# The alert body's run id, and the one-line evidence summary.


def test_named_run_id_reads_the_line_the_alert_body_writes() -> None:
    assert responder.named_run_id(ALERT_BODY) == 7
    assert responder.named_run_id("no run id here") is None


# AC8: a peer that accepts the connection and then stalls must not raise out of
# the responder -- its `TimeoutError` is not a `URLError`.


def test_a_stalled_app_read_is_no_answer_not_an_exception(stalled_peer: str) -> None:
    app = responder.urllib_app(stalled_peer, timeout=1)

    assert app("GET", responder.HEALTHZ_PATH, {}) == Answer(None)


def test_a_stalled_model_call_is_a_failure_not_an_exception(stalled_peer: str) -> None:
    complete = responder.llm_complete("key", "model", stalled_peer, timeout=1)

    assert complete("prompt") is None


def test_a_stalled_app_escalates_and_comments(stalled_peer: str) -> None:
    """Every app read stalls, so the app is unreachable: one escalation comment."""
    app = responder.urllib_app(stalled_peer, timeout=1)

    _, outcome, comments, closed, _ = make_responder(None, app=app, base_url=stalled_peer)

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "deterministic"
    assert outcome.performed is None
    assert closed == []
    assert len(comments) == 1
    assert "no answer" in comments[0][1]


def test_a_stalled_model_falls_back_to_the_rule_and_escalates(stalled_peer: str) -> None:
    """A model that stalls is the model failing: the rule decides, and comments once."""
    routes = evidence_routes(named_run=Answer(200, run_body(7, "succeeded")))
    complete = responder.llm_complete("key", "model", stalled_peer, timeout=1)

    _, outcome, comments, closed, _ = make_responder(routes, complete=complete)

    assert outcome is not None
    assert outcome.source == "deterministic"
    assert outcome.action == responder.ESCALATE
    assert closed == []
    assert len(comments) == 1


def test_a_seam_that_raises_still_gets_one_escalation_comment() -> None:
    """No failure inside the decision lets an exception stand in for an answer."""

    class Boom(RuntimeError):
        pass

    def raising_app(method: str, path: str, headers: dict[str, str]) -> Any:
        raise Boom("the seam raised instead of answering")

    _, outcome, comments, closed, _ = make_responder(None, app=raising_app)

    assert outcome is not None
    assert outcome.action == responder.ESCALATE
    assert outcome.source == "error"
    assert closed == []
    assert len(comments) == 1
    assert "Boom" in comments[0][1]
