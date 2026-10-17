"""On-call responder: wake on an `alert` issue, diagnose, remediate once, verify, report.

This is the responder half of #92: `alert.yml` (#92) opens one GitHub issue
labelled `alert` when the hourly impact check finds user impact, and this module
wakes on that issue and answers it. It is the workflow's step and nothing else --
`ops/oncall/test_responder.py` drives every branch of it offline over a fake app,
a fake model and a fake Render call.

**What it reads (AC2).** The issue body itself, `GET /healthz`,
`GET /telemetry/metrics` (#90) and `GET /runs/{id}` for the run id the alert
body names. The body #92's `Report.body` writes names its newest run on one line
as ``- id `<n>`, status `<status>` ``, which is the id this reads. A read that
does not answer is evidence too -- an unreachable app is an absent status, not an
exception.

**How it decides (AC2).** With `LLM_API_KEY` set it asks the model to choose
among the fixed action set from that evidence and parses one JSON object back;
the model's answer names an action only when that action is in the set. With the
key unset, or the model call failing, the *documented deterministic rule* decides
over the same evidence:

1. `/healthz` did not answer at all -> `escalate` (the app is unreachable, so
   nothing can be diagnosed and a second request cannot be assumed to work).
2. `/healthz` answered a status other than `200` -> `rollback` (the serving
   revision is unhealthy).
3. The named run ended `failed` -> `retrigger`.
4. Anything else -> `escalate`.

An answer that is unparseable or names nothing in the set is *not* an action: it
becomes `escalate` and never a fallback to the deterministic rule, so a model
that answers nonsense cannot silently pick a remediation.

**The action set (AC3).** `retrigger` is `POST /internal/tick` with the `Origin`
header set to `JOBRADAR_URL` -- the same same-origin admission
`backend/app/routes/internal.py` gives the in-app button, so it sends no
`X-Tick-Secret` and needs no new secret. `rollback` runs `ops/rollback.sh` (#89),
which owns the selection of the newest `live` commit that is not the one serving;
this module never re-derives it. Every other conclusion is `escalate`, which
comments and changes nothing. One wake takes at most one action and writes at
most one comment, and no path here edits or pushes code, re-adds the label,
reopens the issue or schedules another run.

**What it verifies (AC4).** After `retrigger` it polls `GET /runs/{id}` with the
id the `202` carried until that run reaches `succeeded` or `failed`, and the
comment names that status -- `succeeded` is the fix, `failed` is not. After
`rollback` it polls `GET /healthz` until it answers `200`, over a wait long
enough to cover a free instance's cold start. It closes the issue only when the
effect is verified, and otherwise leaves it open naming the owner action.

**What it never carries (AC6).** The comment quotes signals, ids and statuses
only. Every free-text value that can reach it -- the model's reason, the rollback
script's own stderr -- passes through `redact`, which replaces the value of every
available credential and any connection string with `[redacted]`. The trace id it
prints is the one the tick request adopted, read from the `X-Request-ID` response
header the app's telemetry middleware sets (#90); it is the run's own trace id
too, because the request hands it to the run.

The standard library is the whole dependency: `urllib` for the app reads, `json`
for the bodies, `subprocess` for the one `gh` call and the rollback script. The
seams are arguments (`app`, `complete`, `rollback`, `comment`, `close`) so the
offline test drives every branch without a socket, a model, a Render service or a
GitHub token.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The three paths this reads and the one it posts. Spelled once each, so a
# failure names the same path the workflow's prose does.
HEALTHZ_PATH = "/healthz"
METRICS_PATH = "/telemetry/metrics"
TICK_PATH = "/internal/tick"
RUN_PATH = "/runs/{id}"

# The header the same-origin admission reads, and the one the app's telemetry
# middleware sets with the request's trace id. Both are names, never values.
ORIGIN_HEADER = "Origin"
REQUEST_ID_HEADER = "X-Request-ID"  # noqa: S105 -- a header name, not a secret

# The fixed action set, and the three words it is spelled with (AC3).
RETRIGGER = "retrigger"
ROLLBACK = "rollback"
ESCALATE = "escalate"
ACTIONS = (RETRIGGER, ROLLBACK, ESCALATE)

# The two terminal run statuses `app.routes.internal` writes.
SUCCEEDED = "succeeded"
FAILED = "failed"

# The marker every comment this writes begins with, so a second delivery of the
# same alert is a no-op: `needs_a_response` refuses an issue that already carries
# one, which is what keeps the `opened` and `labeled` deliveries from remediating
# twice (AC1). The `concurrency` group in `oncall.yml` serialises the two, so the
# second delivery reads the first's comment.
COMMENT_MARKER = "<!-- jobradar-oncall -->"

# The label the `alert` issue carries (#92). Named once here and once in
# `oncall.yml`.
ALERT_LABEL = "alert"

# The run id #92's `Report.body` names, as `- id `<n>`, status `<status>``.
RUN_ID_IN_BODY = re.compile(r"- id `(\d+)`")

# A connection string of any shape the guard's own rules know: redacted by
# pattern, so a value this module was not handed cannot reach a comment either.
CONNECTION_STRING = re.compile(r"\b(?:postgres(?:ql)?(?:\+[a-z0-9]+)?|rediss?)://\S+", re.IGNORECASE)

# The most of the rollback script's output a comment quotes, so a long log cannot
# become the comment.
_QUOTED_OUTPUT_LIMIT = 600

# The app is served over HTTP and answers at once; the model may take longer.
_READ_TIMEOUT = 30
_MODEL_TIMEOUT = 60

# How long `retrigger` waits for its run to reach a terminal status, how long
# `rollback` waits for `/healthz` to answer 200 -- long enough to cover a free
# instance's cold start, about a minute -- and how often either is polled.
RUN_WAIT_SECONDS = 300
HEALTHZ_WAIT_SECONDS = 300
POLL_SECONDS = 5

# The model and its endpoint when the app names neither, the app's own defaults.
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MODEL_BASE_URL = "https://api.openai.com/v1"

# `ops/rollback.sh`, beside this file's package, resolved from `__file__` so the
# path does not depend on the working directory.
ROLLBACK_SCRIPT = Path(__file__).resolve().parent.parent / "rollback.sh"

# The variables whose values are credentials the comment must never carry (AC6).
# Read for redaction only, never printed.
SECRET_VARIABLES = (
    "LLM_API_KEY",
    "RENDER_API_KEY",
    "RENDER_SERVICE_ID",
    "GITHUB_TOKEN",
    "TICK_SECRET",
    "DATABASE_URL",
)


class ResponderError(Exception):
    """The responder cannot run: a required input is unset or the issue cannot be read."""


@dataclass(frozen=True)
class Answer:
    """One app read: the status it answered, its parsed body, and its headers.

    `status` is `None` when the app did not answer at all -- a refused connection
    or a timeout. That is an answer of its own, and the one the deterministic rule
    reads as an unreachable app.
    """

    status: int | None
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """One response header, case-insensitively, or `None` when it is absent."""
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


@dataclass(frozen=True)
class Issue:
    """The alert issue as `gh issue view` renders it: its state, body and comments."""

    number: int
    state: str
    body: str
    comments: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    """Everything the responder reads before it decides (AC2).

    A `None` in any field is a read that did not answer, which is evidence and not
    an exception: `health_status` `None` is an unreachable app, `metrics` `None` is
    a metrics read that failed, and `named_run` `None` with an id is a run that
    could not be read.
    """

    health_status: int | None
    metrics: list[Mapping[str, Any]] | None
    named_run_id: int | None
    named_run: Mapping[str, Any] | None


@dataclass(frozen=True)
class Decision:
    """The chosen action, why it was chosen, and who chose it."""

    action: str
    reason: str
    source: str  # "llm", "deterministic" or "invalid-answer"


@dataclass(frozen=True)
class Started:
    """The outcome of one `retrigger` attempt: the run it started, or why not."""

    ok: bool
    run_id: int | None = None
    trace_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class Outcome:
    """What one wake found, did and verified -- the comment's own fields (AC4)."""

    action: str
    source: str
    reason: str
    found: str
    performed: str | None
    run_id: int | None
    trace_id: str | None
    fixed: bool | None
    owner_action: str | None

    @property
    def fixed_label(self) -> str:
        if self.fixed is True:
            return "yes"
        if self.fixed is False:
            return "no"
        return "not verified"


@dataclass(frozen=True)
class Config:
    """The run's inputs: the app's address, the waits, and the values to redact."""

    base_url: str
    run_wait_seconds: int = RUN_WAIT_SECONDS
    healthz_wait_seconds: int = HEALTHZ_WAIT_SECONDS
    poll_seconds: int = POLL_SECONDS
    secrets: tuple[str, ...] = ()


# The seams the offline test replaces: an app read, the model call, one run of
# `ops/rollback.sh`, and the three `gh` operations.
App = Callable[[str, str, Mapping[str, str]], Answer]
Complete = Callable[[str], str | None]
Rollback = Callable[[], tuple[int, str]]
ReadIssue = Callable[[int], Issue]
Comment = Callable[[int, str], None]
CloseIssue = Callable[[int], None]


# --- Redaction (AC6) ---------------------------------------------------------


def redact(text: str, secrets: Sequence[str]) -> str:
    """`text` with every credential value and connection string replaced.

    Each available variable's value is replaced verbatim first, then a
    connection-string shape of any kind. Nothing else is touched: an id, a status
    and a commit sha are the signals a comment is for.
    """
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return CONNECTION_STRING.sub("[redacted connection string]", text)


def _last_lines(output: str, limit: int = _QUOTED_OUTPUT_LIMIT) -> str:
    """The tail of a command's output, on one line and bounded, for a comment."""
    collapsed = " / ".join(line.strip() for line in output.splitlines() if line.strip())
    if len(collapsed) > limit:
        return "..." + collapsed[-limit:]
    return collapsed


# --- The issue and the one-comment rule (AC1, AC3) ---------------------------


def needs_a_response(issue: Issue) -> bool:
    """Whether this delivery is a fresh wake, and not an issue already answered.

    A closed issue was answered (the responder closes only a verified fix, and a
    human closing it is a decision too), and an issue whose comments already hold
    `COMMENT_MARKER` was answered by an earlier delivery of the same alert. Both
    are no-ops, so the `opened` and `labeled` deliveries of one alert cannot
    remediate twice.
    """
    if issue.state == "closed":
        return False
    return COMMENT_MARKER not in "\n".join(issue.comments)


# --- Evidence (AC2) ----------------------------------------------------------


def named_run_id(body: str) -> int | None:
    """The run id the alert body names, or `None` when it names none.

    #92's `Report.body` writes the newest run on one line as
    ``- id `<n>`, status `<status>` ``; that line is the id this reads.
    """
    match = RUN_ID_IN_BODY.search(body)
    return int(match.group(1)) if match else None


def read_evidence(app: App, body: str) -> Evidence:
    """The three reads and the issue body, gathered into one `Evidence`.

    `GET /healthz`, `GET /telemetry/metrics`, and `GET /runs/{id}` for the id the
    body names -- the last skipped when the body names none. No read raises: a
    refusal is a `None` field.
    """
    health = app("GET", HEALTHZ_PATH, {})
    metrics_answer = app("GET", METRICS_PATH, {})
    run = named_run_id(body)
    run_answer = app("GET", RUN_PATH.format(id=run), {}) if run is not None else None
    return Evidence(
        health_status=health.status,
        metrics=(
            metrics_answer.body
            if metrics_answer.status == 200 and isinstance(metrics_answer.body, list)
            else None
        ),
        named_run_id=run,
        named_run=(
            run_answer.body
            if run_answer is not None
            and run_answer.status == 200
            and isinstance(run_answer.body, Mapping)
            else None
        ),
    )


def evidence_json(evidence: Evidence) -> dict[str, Any]:
    """The evidence as the model reads it, and the only fields it may act on."""
    return {
        "healthz_status": evidence.health_status,
        "metrics": evidence.metrics,
        "named_run_id": evidence.named_run_id,
        "named_run": evidence.named_run,
    }


# --- The decision (AC2) ------------------------------------------------------


def deterministic_decision(evidence: Evidence) -> Decision:
    """The documented rule, over the same evidence the model sees.

    1. an unreachable `/healthz` -> `escalate`, 2. a non-`200` `/healthz` ->
    `rollback`, 3. a named run that ended `failed` -> `retrigger`, 4. otherwise
    `escalate`. The reason names the value the rule read.
    """
    if evidence.health_status is None:
        return Decision(
            ESCALATE,
            "`GET /healthz` did not answer, so the app is unreachable and its state "
            "cannot be diagnosed",
            "deterministic",
        )
    if evidence.health_status != 200:
        return Decision(
            ROLLBACK,
            f"`GET /healthz` answered HTTP {evidence.health_status}, so the serving "
            "revision is unhealthy",
            "deterministic",
        )
    if evidence.named_run is not None and evidence.named_run.get("status") == FAILED:
        return Decision(
            RETRIGGER,
            f"the alert's run `{evidence.named_run_id}` ended `failed`, so re-run the digest",
            "deterministic",
        )
    if evidence.named_run is None:
        return Decision(
            ESCALATE,
            "no run could be read from the alert's evidence, so there is nothing to re-run",
            "deterministic",
        )
    return Decision(
        ESCALATE,
        "the evidence named no remediation in the fixed action set",
        "deterministic",
    )


def decision_prompt(evidence: Evidence, base_url: str) -> str:
    """The model's prompt: the fixed action set and the evidence, nothing else."""
    return (
        "You are JobRadar's on-call responder. Choose exactly one action for the "
        "incident described by the evidence below.\n"
        "The only actions are:\n"
        '- "retrigger": POST /internal/tick to re-run the digest journey.\n'
        '- "rollback": re-deploy the newest known-good commit with ops/rollback.sh.\n'
        '- "escalate": change nothing and hand the issue to a human.\n'
        'Answer with one JSON object and nothing else: {"action": "<one of the three>", '
        '"reason": "<one short sentence>"}.\n'
        f"The app is at {base_url}.\n"
        f"Evidence:\n{json.dumps(evidence_json(evidence), indent=2, sort_keys=True)}"
    )


def parse_decision(raw: str) -> tuple[str | None, str]:
    """The `action` and `reason` of the first JSON object in a model's answer.

    The whole answer is tried first, then the first `{...}` inside it, so a model
    that wraps its JSON in prose still parses. Anything unparseable answers
    `(None, "")`, which `llm_decision` turns into `escalate`.
    """
    candidate = raw.strip()
    payload: Any = None
    try:
        payload = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(candidate[start : end + 1])
            except ValueError:
                payload = None
    if not isinstance(payload, Mapping):
        return None, ""
    action = payload.get("action")
    reason = payload.get("reason")
    return (action if isinstance(action, str) else None, reason if isinstance(reason, str) else "")


def llm_decision(evidence: Evidence, complete: Complete, base_url: str) -> Decision | None:
    """The model's decision, or `None` when the model call itself failed.

    A `None` answer is "the model failing", which falls back to the deterministic
    rule. An answer that arrives but parses to no action in the set is not an
    action at all: it becomes `escalate`, so a nonsense answer cannot pick a
    remediation.
    """
    raw = complete(decision_prompt(evidence, base_url))
    if raw is None:
        return None
    action, reason = parse_decision(raw)
    if action not in ACTIONS:
        return Decision(
            ESCALATE,
            "the model's answer named no action in the fixed set",
            "invalid-answer",
        )
    return Decision(action, reason or "the model chose this action", "llm")


def decide(evidence: Evidence, complete: Complete | None, base_url: str) -> Decision:
    """The model's choice when it can make one, and the documented rule otherwise."""
    if complete is not None:
        decision = llm_decision(evidence, complete, base_url)
        if decision is not None:
            return decision
    return deterministic_decision(evidence)


# --- Actions and verification (AC3, AC4, AC8, AC9) ---------------------------


def start_run(app: App, base_url: str) -> Started:
    """`POST /internal/tick` with `Origin` set to the app's own URL, and read the `202`.

    The same same-origin admission the in-app button passes: no `X-Tick-Secret` is
    sent. A `409` (a run is already in progress), a non-`202`, a `202` with no run
    id, and no answer at all are each a `Started(ok=False)` naming the answer --
    never an exception (AC8).
    """
    answer = app("POST", TICK_PATH, {ORIGIN_HEADER: base_url.rstrip("/")})
    if answer.status is None:
        return Started(False, detail="`POST /internal/tick` did not answer")
    if answer.status == 409:
        return Started(
            False,
            detail="`POST /internal/tick` answered 409: a run is already in progress",
        )
    if answer.status != 202:
        return Started(
            False, detail=f"`POST /internal/tick` answered HTTP {answer.status} instead of 202"
        )
    body = answer.body
    if not isinstance(body, Mapping) or not isinstance(body.get("id"), int):
        return Started(False, detail="`POST /internal/tick` answered 202 with no run id")
    return Started(True, run_id=body["id"], trace_id=answer.header(REQUEST_ID_HEADER))


def _wait_for_run(
    app: App, run_id: int, config: Config, sleep: Callable[[float], None], clock: Callable[[], float]
) -> str | None:
    """The run's terminal status, or `None` when the bounded wait runs out."""
    deadline = clock() + config.run_wait_seconds
    while True:
        answer = app("GET", RUN_PATH.format(id=run_id), {})
        if answer.status == 200 and isinstance(answer.body, Mapping):
            status = answer.body.get("status")
            if status in (SUCCEEDED, FAILED):
                return status
        if clock() >= deadline:
            return None
        sleep(config.poll_seconds)


def _wait_for_healthz(
    app: App, config: Config, sleep: Callable[[float], None], clock: Callable[[], float]
) -> int | None:
    """The first `200` from `/healthz`, or the last answer when the wait runs out."""
    deadline = clock() + config.healthz_wait_seconds
    answer = Answer(None)
    while True:
        answer = app("GET", HEALTHZ_PATH, {})
        if answer.status == 200:
            return 200
        if clock() >= deadline:
            return answer.status
        sleep(config.poll_seconds)


def describe_evidence(evidence: Evidence) -> str:
    """The evidence as one line of signals -- ids, statuses and counts, nothing private."""
    parts = [
        "`GET /healthz` -> "
        + ("no answer" if evidence.health_status is None else f"HTTP {evidence.health_status}")
    ]
    parts.append(
        "`GET /telemetry/metrics` -> "
        + ("no answer" if evidence.metrics is None else f"{len(evidence.metrics)} route(s)")
    )
    if evidence.named_run_id is None:
        parts.append("the alert body names no run id")
    elif evidence.named_run is None:
        parts.append(f"`GET /runs/{evidence.named_run_id}` -> no run")
    else:
        parts.append(
            f"`GET /runs/{evidence.named_run_id}` -> status "
            f"`{evidence.named_run.get('status')}`"
        )
    return "; ".join(parts)


# --- The comment (AC4, AC6) --------------------------------------------------


def comment_body(outcome: Outcome) -> str:
    """The one comment a wake writes: what it found, did, saw and verified.

    Only ids, statuses, the action, the redacted reason and the owner action reach
    it, so no resume, file name, credential or connection string can (AC6).
    """
    lines = [
        COMMENT_MARKER,
        f"## On-call response: {outcome.action}",
        "",
        f"- **Found:** {outcome.found}",
        f"- **Decided by:** {outcome.source}",
        f"- **Reason:** {outcome.reason}",
        f"- **Action taken:** {outcome.performed or 'none'}",
        (
            f"- **Run id:** `{outcome.run_id}`"
            if outcome.run_id is not None
            else "- **Run id:** none observed"
        ),
        (
            f"- **Trace id:** `{outcome.trace_id}`"
            if outcome.trace_id
            else "- **Trace id:** none observed"
        ),
        f"- **Fixed:** {outcome.fixed_label}",
    ]
    if outcome.owner_action:
        lines += ["", f"**Owner action required:** {outcome.owner_action}"]
    return "\n".join(lines)


# --- One wake, end to end (AC3, AC4, AC8, AC9) -------------------------------


def respond(
    issue: Issue,
    *,
    app: App,
    complete: Complete | None,
    rollback: Rollback,
    comment: Comment,
    close: CloseIssue,
    config: Config,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome | None:
    """Diagnose, take at most one action, verify it, comment once, and close only on a fix.

    Answers the `Outcome` it commented, or `None` when `needs_a_response` says the
    issue is already answered or closed -- in which case it touches nothing. Every
    awkward answer (a `409`, an unreachable app, a wait that runs out, a rollback
    that refuses) becomes `escalate` with the issue left open, so no path lets an
    unhandled exception stand in for an answer on the issue (AC8).
    """
    if not needs_a_response(issue):
        return None

    evidence = read_evidence(app, issue.body)
    decision = decide(evidence, complete, config.base_url)
    found = describe_evidence(evidence)
    reason = redact(decision.reason, config.secrets)

    action = decision.action
    performed: str | None = None
    run_id = evidence.named_run_id
    trace_id: str | None = None
    fixed: bool | None = None
    owner_action: str | None = None

    if decision.action == RETRIGGER:
        started = start_run(app, config.base_url)
        if not started.ok:
            action = ESCALATE
            owner_action = (
                f"{started.detail}. A human must confirm the app's state and re-run the "
                "digest by hand."
            )
        else:
            performed = (
                f"retriggered the digest: `POST /internal/tick` answered 202 with run id "
                f"`{started.run_id}`"
            )
            run_id = started.run_id
            trace_id = started.trace_id
            status = _wait_for_run(app, run_id, config, sleep, clock)
            if status == SUCCEEDED:
                fixed = True
            elif status == FAILED:
                fixed = False
                owner_action = (
                    f"run `{run_id}` ended `failed` again. A human must read its error and "
                    "the boards it reports."
                )
            else:
                action = ESCALATE
                owner_action = (
                    f"run `{run_id}` never reached a terminal status within "
                    f"{config.run_wait_seconds}s. A human must check it."
                )

    elif decision.action == ROLLBACK:
        returncode, output = rollback()
        if returncode != 0:
            action = ESCALATE
            performed = "attempted `ops/rollback.sh`, which refused or failed"
            owner_action = (
                "the rollback could not be completed: "
                f"{redact(_last_lines(output), config.secrets)}. A human must roll back by hand."
            )
        else:
            performed = "ran `ops/rollback.sh`, which re-deployed the newest known-good commit"
            status = _wait_for_healthz(app, config, sleep, clock)
            if status == 200:
                fixed = True
            else:
                action = ESCALATE
                fixed = False
                owner_action = (
                    f"`GET /healthz` did not answer 200 within {config.healthz_wait_seconds}s "
                    f"after the rollback (last answer: {status}). A human must check the instance."
                )

    else:  # escalate: comment only, change nothing
        owner_action = "a human must investigate the alert; the responder changed nothing."

    outcome = Outcome(
        action=action,
        source=decision.source,
        reason=reason,
        found=found,
        performed=performed,
        run_id=run_id,
        trace_id=trace_id,
        fixed=fixed,
        owner_action=owner_action,
    )
    comment(issue.number, comment_body(outcome))
    if outcome.fixed is True:
        close(issue.number)
    return outcome


# --- The real seams ----------------------------------------------------------


def _json_body(raw: bytes) -> Any:
    """One response body as JSON, or its first characters as text when it is not JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw[:400].decode("utf-8", "replace")


def urllib_app(base_url: str, *, timeout: float = _READ_TIMEOUT) -> App:
    """The real app read: one `urllib` request, its status, body and headers."""
    root = base_url.rstrip("/")

    def app(method: str, path: str, headers: Mapping[str, str]) -> Answer:
        request = urllib.request.Request(
            f"{root}{path}",
            method=method,
            headers={**headers, "Accept": "application/json"},
        )
        if method == "POST":
            request.data = b""
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return Answer(
                    response.status,
                    _json_body(response.read()),
                    {key: value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as error:
            return Answer(
                error.code,
                _json_body(error.read()),
                {key: value for key, value in error.headers.items()},
            )
        except urllib.error.URLError:
            return Answer(None)

    return app


def llm_complete(api_key: str, model: str, base_url: str, *, timeout: float = _MODEL_TIMEOUT) -> Complete:
    """The real model call: one chat completion, its text back, or `None` on any failure."""
    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    def complete(prompt: str) -> str | None:
        payload = json.dumps(
            {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read())
            return body["choices"][0]["message"]["content"]
        except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError):
            return None

    return complete


def real_rollback(*, timeout: float = 1500) -> tuple[int, str]:
    """Run `ops/rollback.sh`, which owns the selection and the wait for `live`."""
    try:
        completed = subprocess.run(
            ["bash", str(ROLLBACK_SCRIPT)],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 1, f"ops/rollback.sh did not finish within {timeout}s"
    return completed.returncode, completed.stdout + completed.stderr


def gh_issue(number: int) -> Issue:
    """Read the alert issue's state, body and comments through `gh`."""
    completed = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "state,body,comments"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    return Issue(
        number=number,
        state=payload.get("state", "open"),
        body=payload.get("body") or "",
        comments=tuple(comment.get("body", "") for comment in payload.get("comments", [])),
    )


def gh_comment(number: int, body: str) -> None:
    """Write the one comment, through `gh`, reading the body from stdin."""
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--body-file", "-"],
        input=body,
        text=True,
        check=True,
    )


def gh_close(number: int) -> None:
    """Close the issue, through `gh`, only when the effect was verified."""
    subprocess.run(
        ["gh", "issue", "close", str(number), "--reason", "completed"], check=True
    )


def secrets_from_environment() -> tuple[str, ...]:
    """Every available credential value, for `redact` and nothing else."""
    return tuple(
        value.strip()
        for name in SECRET_VARIABLES
        if (value := os.environ.get(name, ""))
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Read the environment, wake once, and print a redacted one-line outcome."""
    del argv  # no arguments: every input arrives through the environment
    base_url = os.environ.get("JOBRADAR_URL", "").strip()
    if not base_url:
        print("responder: JOBRADAR_URL is unset or empty, so no alert can be answered.", file=sys.stderr)
        return 2
    number_text = os.environ.get("ISSUE_NUMBER", "").strip()
    if not number_text.isdigit():
        print("responder: ISSUE_NUMBER is unset or not a number.", file=sys.stderr)
        return 2

    secrets = secrets_from_environment()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    complete: Complete | None = None
    if api_key:
        complete = llm_complete(
            api_key,
            os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
            os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_MODEL_BASE_URL,
        )

    issue = gh_issue(int(number_text))
    outcome = respond(
        issue,
        app=urllib_app(base_url),
        complete=complete,
        rollback=real_rollback,
        comment=gh_comment,
        close=gh_close,
        config=Config(base_url=base_url, secrets=secrets),
    )
    if outcome is None:
        print("responder: nothing to do; this alert is already answered or closed.")
        return 0
    print(redact(f"responder: {outcome.action} -- {outcome.reason}", secrets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
