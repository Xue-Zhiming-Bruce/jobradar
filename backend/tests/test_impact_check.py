"""`ops/oncall/impact_check.py`'s four predicates, pinned offline over synthetic responses.

#92's AC9: this drives the classifier with the bodies the app's reads would answer
-- no app, no socket and no credential -- and pins which inputs alert and which do
not. Each of AC3--AC6's four predicates is exercised in its firing form and in the
awkward cases: an empty run list, a newest run still `running`, a matches window
truncated by the page's limit, a window mixing `scored` and `not_scored`, a
telemetry window with zero requests, and a read that does not answer `200`.

The module lives under `ops/`, outside the backend package, so it is loaded by
path the way `backend/tests/test_rollback_script.py` drives `ops/rollback.sh` by
path: it is the real file, not a copy that could drift.
"""

import importlib.util
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULE_PATH = REPO_ROOT / "ops" / "oncall" / "impact_check.py"

_spec = importlib.util.spec_from_file_location("impact_check", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
impact_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(impact_check)

# The check's own clock, fixed so "26 hours before" is a fact and not a race.
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


def run(
    run_id: int,
    status: str,
    *,
    started_at: datetime = NOW,
    finished_at: datetime | None = None,
    error: str | None = None,
    postings_seen: int = 0,
    matches_created: int = 0,
    matches_emailed: int | None = None,
) -> dict[str, Any]:
    """One `RunSummary` body, the eleven fields `GET /runs` answers with."""
    return {
        "id": run_id,
        "status": status,
        "started_at": started_at.isoformat(),
        "finished_at": None if finished_at is None else finished_at.isoformat(),
        "error": error,
        "postings_seen": postings_seen,
        "matches_created": matches_created,
        "matches_emailed": matches_emailed,
        "companies_not_fetched": None,
        "postings_not_rematched": None,
        "company_outcomes": [],
    }


def match(match_id: int, created_at: datetime, outcome: str, reason: str = "a fit") -> dict[str, Any]:
    """One `MatchSummary` body, the ten fields `GET /matches` answers with."""
    return {
        "id": match_id,
        "company_name": "Acme",
        "title": "Backend Engineer",
        "url": "https://example.test/1",
        "score": 0 if outcome == "not_scored" else 80,
        "reason": reason,
        "gaps": [],
        "state": "new",
        "outcome": outcome,
        "created_at": created_at.isoformat(),
    }


def metric(route: str, count: int, errors: int, p95_ms: int = 10) -> dict[str, Any]:
    return {"route": route, "count": count, "errors": errors, "p95_ms": p95_ms}


FAILING_RUN = run(
    7,
    "succeeded",
    started_at=NOW - timedelta(minutes=5),
    finished_at=NOW,
)
NOT_SCORED = match(
    1, NOW - timedelta(minutes=2), "not_scored", "Not scored: the model call failed"
)


# AC3: `newest_run_failed`.


def test_newest_run_failed_fires_on_a_failed_newest_run() -> None:
    verdict = impact_check.newest_run_failed([run(7, "failed", error="a board was down")])

    assert verdict.fired
    assert verdict.values["run_id"] == 7
    assert verdict.values["status"] == "failed"


def test_newest_run_failed_is_quiet_on_a_success_or_no_run() -> None:
    assert not impact_check.newest_run_failed([run(7, "succeeded")]).fired
    assert not impact_check.newest_run_failed([]).fired


def test_newest_run_failed_is_quiet_on_a_running_newest_run() -> None:
    """A run in flight is not a failure."""
    assert not impact_check.newest_run_failed([run(7, "running")]).fired


# AC4: `model_scored_none`.


def test_model_scored_none_fires_when_a_finished_run_scored_none_of_its_rows() -> None:
    verdict = impact_check.model_scored_none([FAILING_RUN], [NOT_SCORED])

    assert verdict.fired
    assert verdict.values["not_scored_rows"] == 1
    assert verdict.values["scored_rows"] == 0


def test_model_scored_none_is_quiet_when_any_windowed_row_was_scored() -> None:
    """A mixed window -- one scored, one not -- is a working scorer, not impact."""
    mixed = [match(1, NOW - timedelta(minutes=2), "not_scored", "Not scored: x"), match(2, NOW - timedelta(minutes=1), "scored")]

    assert not impact_check.model_scored_none([FAILING_RUN], mixed).fired


def test_model_scored_none_is_quiet_when_the_not_scored_row_is_outside_the_window() -> None:
    """The window is the run's own `[started_at, finished_at]`, not the whole page."""
    outside = match(1, NOW + timedelta(minutes=1), "not_scored", "Not scored: the model call failed")

    assert not impact_check.model_scored_none([FAILING_RUN], [outside]).fired


def test_model_scored_none_is_quiet_on_a_running_run_and_on_no_run() -> None:
    assert not impact_check.model_scored_none([run(7, "running")], [NOT_SCORED]).fired
    assert not impact_check.model_scored_none([], []).fired


def test_model_scored_none_records_a_truncated_window_and_alerts_nothing() -> None:
    """A full page whose oldest row is still inside the window may hold more."""
    rows = [
        match(index, NOW - timedelta(seconds=index), "not_scored", "Not scored: the model call failed")
        for index in range(impact_check.MATCHES_MAX_LIMIT)
    ]
    verdict = impact_check.model_scored_none([FAILING_RUN], rows)

    assert verdict.values["window_truncated"] is True
    assert not verdict.fired


# AC5: `digest_stalled`.


def test_digest_stalled_fires_when_the_newest_success_is_older_than_the_stall_window() -> None:
    stale = run(
        7,
        "succeeded",
        started_at=NOW - timedelta(hours=30),
        finished_at=NOW - timedelta(hours=29),
    )

    assert impact_check.digest_stalled([stale], NOW).fired


def test_digest_stalled_is_quiet_when_a_success_finished_within_the_window() -> None:
    recent = run(
        7,
        "succeeded",
        started_at=NOW - timedelta(hours=2),
        finished_at=NOW - timedelta(hours=1),
    )

    assert not impact_check.digest_stalled([recent], NOW).fired


def test_digest_stalled_is_quiet_when_the_newest_run_is_running_and_recent() -> None:
    running = run(7, "running", started_at=NOW - timedelta(hours=1))

    assert not impact_check.digest_stalled([running], NOW).fired


def test_digest_stalled_fires_when_no_run_succeeded_and_the_newest_is_old() -> None:
    stale_failure = run(
        7,
        "failed",
        started_at=NOW - timedelta(hours=30),
        finished_at=NOW - timedelta(hours=29),
    )

    assert impact_check.digest_stalled([stale_failure], NOW).fired


def test_digest_stalled_is_quiet_with_no_run() -> None:
    assert not impact_check.digest_stalled([], NOW).fired


# AC6: `http_5xx_rate`.


def test_http_5xx_rate_fires_at_or_above_the_threshold() -> None:
    metrics = [metric("/internal/tick", count=10, errors=1)]

    assert impact_check.http_5xx_rate(metrics).fired


def test_http_5xx_rate_is_quiet_below_the_threshold() -> None:
    metrics = [metric("/runs/{id}", count=100, errors=1)]

    assert not impact_check.http_5xx_rate(metrics).fired


def test_http_5xx_rate_is_quiet_on_a_window_with_no_journey_requests() -> None:
    """Zero requests means the ratio is undefined, and undefined is not impact."""
    verdict = impact_check.http_5xx_rate([metric("/healthz", count=50, errors=0)])

    assert verdict.values["journey_requests"] == 0
    assert not verdict.fired


# The classifier as a whole: one healthy input, and one read that fails.


def test_check_fires_none_on_a_healthy_journey() -> None:
    reads = {
        impact_check.RUNS_READ: [
            run(
                7,
                "succeeded",
                started_at=NOW - timedelta(hours=1),
                finished_at=NOW - timedelta(minutes=59),
            )
        ],
        impact_check.MATCHES_READ: [
            match(1, NOW - timedelta(minutes=59, seconds=30), "scored")
        ],
        impact_check.METRICS_READ: [metric("/internal/tick", count=10, errors=0)],
    }

    report = impact_check.check(reads.__getitem__, NOW)

    assert report.healthy
    assert report.fired == []


def test_check_raises_read_failed_naming_the_read_that_did_not_answer_200() -> None:
    def read(path: str) -> Any:
        if path == impact_check.MATCHES_READ:
            raise impact_check.ReadFailed(path, 503)
        return []

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.check(read, NOW)

    assert impact_check.MATCHES_READ in str(failure.value)
    assert "503" in str(failure.value)


def test_the_alert_body_names_the_impact_and_carries_no_private_value() -> None:
    reads = {
        impact_check.RUNS_READ: [
            run(
                7,
                "failed",
                started_at=NOW - timedelta(hours=1),
                finished_at=NOW,
                error="the board refused: acme",
                postings_seen=12,
                matches_created=3,
            )
        ],
        impact_check.MATCHES_READ: [],
        impact_check.METRICS_READ: [],
    }

    report = impact_check.check(reads.__getitem__, NOW)
    body = report.body

    assert report.fired == ["newest_run_failed"]
    assert "newest_run_failed" in report.title
    assert "id `7`" in body and "status `failed`" in body
    for forbidden in (
        "resume-sentinel",
        "api_key",
        "apikey",
        "password",
        "postgresql://",
        "database_url",
        "cpu",
        "memory",
    ):
        assert forbidden not in body.lower()


# The read that touches the network: `200` parses, anything else is `ReadFailed`.


def test_fetch_maps_a_non_200_answer_to_read_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: Any, timeout: float | None = None) -> Any:
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(impact_check.urllib.request, "urlopen", refuse)

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.fetch("https://example.test", impact_check.RUNS_READ)

    assert impact_check.RUNS_READ in str(failure.value)
    assert "503" in str(failure.value)


def test_fetch_names_a_read_that_never_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that never answered is `ReadFailed` too, and it names the read."""

    def unreachable(request: Any, timeout: float | None = None) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(impact_check.urllib.request, "urlopen", unreachable)

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.fetch("https://example.test", impact_check.METRICS_READ)

    assert impact_check.METRICS_READ in str(failure.value)
    assert "no answer" in str(failure.value)


def test_fetch_answers_the_parsed_json_of_a_200(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exception: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'[{"id": 1}]'

    monkeypatch.setattr(
        impact_check.urllib.request, "urlopen", lambda request, timeout=None: Response()
    )

    assert impact_check.fetch("https://example.test", impact_check.RUNS_READ) == [{"id": 1}]


def test_the_report_json_is_the_machine_readable_half_the_workflow_reads() -> None:
    reads = {
        impact_check.RUNS_READ: [run(7, "failed", finished_at=NOW)],
        impact_check.MATCHES_READ: [],
        impact_check.METRICS_READ: [],
    }

    payload = json.loads(json.dumps(impact_check.check(reads.__getitem__, NOW).as_json()))

    assert payload["healthy"] is False
    assert payload["fired"] == ["newest_run_failed"]
    assert payload["title"] and payload["body"]
    assert {verdict["name"] for verdict in payload["verdicts"]} == {
        "newest_run_failed",
        "model_scored_none",
        "digest_stalled",
        "http_5xx_rate",
    }
