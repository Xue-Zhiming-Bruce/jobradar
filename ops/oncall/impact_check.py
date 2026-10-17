"""Does the digest journey show user impact right now? One read-only check.

This is the input the on-call agent wakes on (#93) and the thing the hourly
`alert.yml` runs (#92). It touches the deployed app over HTTP and only over HTTP
-- `GET /runs?limit=50`, `GET /matches?limit=200` and `GET /telemetry/metrics`,
all from `JOBRADAR_URL` -- and classifies the journey through four named
predicates:

- `newest_run_failed` -- the newest run ended `failed`.
- `model_scored_none` -- the newest run finished, stored matches inside its
  `[started_at, finished_at]` window, and not one of them was scored: the model
  call failed on every row it reached.
- `digest_stalled` -- no run succeeded within `STALL_AFTER`, or nothing at all
  ran within it. The daily schedule means a quiet journey is overdue.
- `http_5xx_rate` -- the journey's two route templates (`POST /internal/tick`
  and `GET /runs/{id}`) answered 5xx at or above `FIVE_XX_THRESHOLD` over the
  window `GET /telemetry/metrics` reports.

Each predicate returns a `Verdict`: whether it fired and the raw values it read,
so an alert quotes evidence a person can check and not a summary nobody can.
`check()` takes the reader as an argument, so the classifier is driven offline
over synthetic responses -- no app, no socket, no credential -- by
`backend/tests/test_impact_check.py`.

A read that does not answer `200` raises `ReadFailed`, which `main` prints and
turns into a non-zero exit. A broken monitor must not read as a healthy journey,
and must not read as user impact either, so the workflow opens no `alert` issue
and goes red instead.

No secret is read and nothing private is rendered: the body names run ids,
statuses, counts, instants and the metrics' route names and numbers, and nothing
else, so no resume text, file name, credential or request body can reach an
issue. The standard library is the whole dependency -- `urllib` for the three
reads, `json` for the bodies -- and the workflow's own `gh` steps are what open
the issue.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# The runs page this check reads. It mirrors `RUNS_DEFAULT_LIMIT` in
# `backend/app/routes/runs.py` rather than importing it -- this script is run
# from the repository root and does not import the app -- and `RUNS_MAX_LIMIT`
# is that route's own ceiling, the largest page it will answer.
RUNS_LIMIT = 50
RUNS_MAX_LIMIT = 200

# The matches page this check reads, the ceiling `MATCHES_MAX_LIMIT` in
# `backend/app/routes/matches.py` puts on that read.
MATCHES_MAX_LIMIT = 200

# How long a journey may go without a successful run before it is stalled. The
# digest is daily, so 26 hours is one missed schedule plus slack.
STALL_AFTER = timedelta(hours=26)

# The 5xx ratio of the journey's routes that counts as impact. A ratio and not a
# count, so one 5xx in a handful of journey requests trips it: these are the two
# routes a user's journey is made of, and a user-visible error is the thing this
# alerts on.
FIVE_XX_THRESHOLD = 0.05

# The two route templates the journey is made of, as `GET /telemetry/metrics`
# names them: `POST /internal/tick` starts it and `GET /runs/{id}` is how a
# client follows it. The metric is keyed by template, not by method, so these
# are the templates alone.
JOURNEY_ROUTES = ("/internal/tick", "/runs/{id}")

# The reason prefix `app.matching.scorer` writes on a row it could not score.
NOT_SCORED_PREFIX = "Not scored: "

# The three reads, spelled once so a failure names the same path the workflow's
# prose does.
RUNS_READ = f"/runs?limit={RUNS_LIMIT}"
MATCHES_READ = f"/matches?limit={MATCHES_MAX_LIMIT}"
METRICS_READ = "/telemetry/metrics"


class ReadFailed(Exception):
    """One read did not answer `200`, so the journey cannot be judged from outside."""

    def __init__(self, path: str, status: int | None) -> None:
        self.path = path
        self.status = status
        detail = f"HTTP {status}" if status is not None else "no answer"
        super().__init__(f"{path} did not answer 200 ({detail})")


def fetch(base_url: str, path: str, *, timeout: float = 30) -> Any:
    """One JSON read of `base_url + path`, or `ReadFailed` if it did not answer `200`.

    A `4xx` or `5xx` is an `HTTPError`, a connection that never answered is a
    `URLError`, and anything that is not `200` is the same refusal: the caller
    gets the path and the status it saw, and no body is read into the alert.
    """
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        raise ReadFailed(path, error.code) from error
    except urllib.error.URLError as error:
        raise ReadFailed(path, None) from error
    if status != 200:
        raise ReadFailed(path, status)
    return json.loads(body)


@dataclass(frozen=True)
class Verdict:
    """One predicate's answer: whether it fired, and the raw values it read."""

    name: str
    fired: bool
    values: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "fired": self.fired, "values": self.values}


def _instant(text: str) -> datetime:
    """One ISO instant as FastAPI writes it, which `fromisoformat` reads in 3.12."""
    return datetime.fromisoformat(text)


def _newest(runs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The newest run, which `GET /runs` answers first, or `None` for no runs."""
    return runs[0] if runs else None


def newest_run_failed(runs: Sequence[Mapping[str, Any]]) -> Verdict:
    """Fires when the newest run's `status` is `failed`, and on nothing else.

    A run still `running` is in flight and not a failure, a `succeeded` run is the
    journey working, and no run at all is `digest_stalled`'s case and not this one.
    """
    newest = _newest(runs)
    if newest is None:
        return Verdict("newest_run_failed", False, {"newest_run": None})
    return Verdict(
        "newest_run_failed",
        newest["status"] == "failed",
        {
            "run_id": newest["id"],
            "status": newest["status"],
            "error": newest.get("error"),
            "started_at": newest["started_at"],
        },
    )


def model_scored_none(
    runs: Sequence[Mapping[str, Any]], matches: Sequence[Mapping[str, Any]]
) -> Verdict:
    """Fires when the newest finished run stored rows and scored none of them.

    The window is the newest run's `[started_at, finished_at]`, read from a page
    of the newest `MATCHES_MAX_LIMIT` matches: at least one row in it is
    `not_scored` with a `Not scored: ` reason and no row in it is `scored`. This
    is a bounded predicate and not an exact one -- a row re-scored inside the run
    keeps its original `created_at`, so it can fall outside the window -- and the
    page is truncated when it is full and its oldest row is still inside the
    window, in which case nothing fires: a window this check cannot see whole is
    not evidence.
    """
    newest = _newest(runs)
    if newest is None:
        return Verdict("model_scored_none", False, {"newest_run": None})
    finished = newest["finished_at"]
    if newest["status"] == "running" or finished is None:
        return Verdict(
            "model_scored_none",
            False,
            {
                "run_id": newest["id"],
                "status": newest["status"],
                "finished_at": finished,
                "window_rows": 0,
                "window_truncated": False,
            },
        )

    started_at = _instant(newest["started_at"])
    finished_at = _instant(finished)
    window = [
        row for row in matches if started_at <= _instant(row["created_at"]) <= finished_at
    ]
    # The page is newest-first, so it covers the whole window unless it is full
    # and its oldest row is still inside it: an older row would prove the page
    # reached past the window's start.
    truncated = (
        len(matches) >= MATCHES_MAX_LIMIT
        and bool(matches)
        and _instant(matches[-1]["created_at"]) >= started_at
    )
    not_scored = [
        row
        for row in window
        if row["outcome"] == "not_scored"
        and str(row.get("reason", "")).startswith(NOT_SCORED_PREFIX)
    ]
    scored = [row for row in window if row["outcome"] == "scored"]
    return Verdict(
        "model_scored_none",
        bool(not_scored) and not scored and not truncated,
        {
            "run_id": newest["id"],
            "status": newest["status"],
            "started_at": newest["started_at"],
            "finished_at": finished,
            "window_rows": len(window),
            "not_scored_rows": len(not_scored),
            "scored_rows": len(scored),
            "window_truncated": truncated,
        },
    )


def digest_stalled(runs: Sequence[Mapping[str, Any]], now: datetime) -> Verdict:
    """Fires when no successful run is recent enough, against the check's own clock.

    The reference is the newest `succeeded` run's `finished_at`, and -- when the
    fetched page holds no successful run at all -- the newest run's `started_at`.
    Either reference more than `STALL_AFTER` old is a stalled journey; a
    successful run inside the window, a run still `running` and recently started,
    and no run at all are not.
    """
    newest = _newest(runs)
    if newest is None:
        return Verdict("digest_stalled", False, {"runs_read": 0, "reference": None})

    succeeded = next((row for row in runs if row["status"] == "succeeded"), None)
    reference = (
        succeeded["finished_at"] if succeeded is not None else newest["started_at"]
    )
    age = None if reference is None else now - _instant(reference)
    return Verdict(
        "digest_stalled",
        age is not None and age > STALL_AFTER,
        {
            "newest_run_id": newest["id"],
            "newest_status": newest["status"],
            "newest_started_at": newest["started_at"],
            "newest_succeeded_finished_at": (
                succeeded["finished_at"] if succeeded is not None else None
            ),
            "reference": reference,
            "hours_since_reference": (
                None if age is None else round(age.total_seconds() / 3600, 2)
            ),
            "stall_after_hours": STALL_AFTER.total_seconds() / 3600,
        },
    )


def http_5xx_rate(metrics: Sequence[Mapping[str, Any]]) -> Verdict:
    """Fires when the journey's routes answered 5xx at or above `FIVE_XX_THRESHOLD`.

    The ratio is the journey templates' combined errors over their combined
    requests. A window with zero journey requests has no ratio -- undefined, and
    undefined is not user impact -- so it does not fire.
    """
    journey = [row for row in metrics if row["route"] in JOURNEY_ROUTES]
    requests = sum(row["count"] for row in journey)
    errors = sum(row["errors"] for row in journey)
    ratio = None if requests == 0 else errors / requests
    return Verdict(
        "http_5xx_rate",
        ratio is not None and ratio >= FIVE_XX_THRESHOLD,
        {
            "journey_routes": list(JOURNEY_ROUTES),
            "journey_requests": requests,
            "journey_5xx": errors,
            "ratio": ratio,
            "threshold": FIVE_XX_THRESHOLD,
        },
    )


@dataclass(frozen=True)
class Report:
    """The four verdicts, the newest runs page, and the alert text they make."""

    verdicts: list[Verdict]
    runs: list[Mapping[str, Any]]

    @property
    def fired(self) -> list[str]:
        return [verdict.name for verdict in self.verdicts if verdict.fired]

    @property
    def healthy(self) -> bool:
        return not self.fired

    @property
    def title(self) -> str:
        if self.healthy:
            return "JobRadar: the digest journey is healthy"
        return f"JobRadar alert: {', '.join(self.fired)}"

    @property
    def body(self) -> str:
        """The alert text: the impact, its evidence, and the check's own output.

        Only the fields named here reach it -- ids, statuses, counts, instants and
        the metrics' route names and numbers -- so no resume text, file name,
        credential or request body is in it, and no CPU or memory number is either.
        """
        newest = self.runs[0] if self.runs else None
        lines = ["## JobRadar digest journey", ""]
        if self.healthy:
            lines += ["No user impact: every impact predicate stayed quiet.", ""]
        else:
            named = ", ".join(f"`{name}`" for name in self.fired)
            lines += [f"User impact detected: {named}.", "", "### The newest run", ""]
            if newest is None:
                lines += ["- no run has been recorded", ""]
            else:
                lines += [
                    f"- id `{newest['id']}`, status `{newest['status']}`",
                    f"- started_at `{newest['started_at']}`",
                    f"- finished_at `{newest['finished_at']}`",
                    f"- error `{newest['error']}`",
                    f"- postings_seen `{newest['postings_seen']}`, "
                    f"matches_created `{newest['matches_created']}`, "
                    f"matches_emailed `{newest['matches_emailed']}`",
                    "",
                ]
        lines += ["### What each predicate read", ""]
        for verdict in self.verdicts:
            state = "FIRED" if verdict.fired else "quiet"
            lines.append(
                f"- `{verdict.name}`: {state} -- "
                f"{json.dumps(verdict.values, sort_keys=True)}"
            )
        lines += [
            "",
            "### The check's own output",
            "",
            "```json",
            json.dumps(self.output, indent=2, sort_keys=True),
            "```",
        ]
        return "\n".join(lines)

    @property
    def output(self) -> dict[str, Any]:
        """The check's verdicts alone, the machine-readable half of its output."""
        return {
            "healthy": self.healthy,
            "fired": self.fired,
            "verdicts": [verdict.as_json() for verdict in self.verdicts],
        }

    def as_json(self) -> dict[str, Any]:
        return {**self.output, "title": self.title, "body": self.body}


def check(
    read: Callable[[str], Any], now: datetime
) -> Report:
    """Read the three paths through `read` and classify the journey.

    `read(path)` answers the parsed JSON of one read or raises `ReadFailed`; it is
    the only thing that touches the network, which is what lets the tests drive
    this over synthetic responses. A `ReadFailed` propagates: the caller names the
    read and exits non-zero, and no verdict is invented from a read that failed.
    """
    runs = read(RUNS_READ)
    matches = read(MATCHES_READ)
    metrics = read(METRICS_READ)
    return Report(
        verdicts=[
            newest_run_failed(runs),
            model_scored_none(runs, matches),
            digest_stalled(runs, now),
            http_5xx_rate(metrics),
        ],
        runs=runs,
    )


def main() -> int:
    """Read `JOBRADAR_URL`, classify, and print the report as JSON on stdout."""
    base_url = os.environ.get("JOBRADAR_URL", "").strip()
    if not base_url:
        print(
            "impact_check: JOBRADAR_URL is unset or empty; no read was made",
            file=sys.stderr,
        )
        return 2
    try:
        report = check(lambda path: fetch(base_url, path), now=datetime.now(UTC))
    except ReadFailed as failure:
        print(
            f"impact_check: {failure}; a broken monitor is neither a healthy "
            "journey nor user impact, so no alert was opened",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report.as_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
