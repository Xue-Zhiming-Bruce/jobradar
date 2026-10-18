"""`ops/oncall/impact_check.py`'s five predicates, pinned offline over synthetic responses.

#92's AC9: this drives the classifier with the bodies the app's reads would answer
-- no app, no socket and no credential -- and pins which inputs alert and which do
not. Each of AC3--AC6's four predicates is exercised in its firing form and in the
awkward cases: an empty run list, a newest run still `running`, a matches window
truncated by the page's limit, a window mixing `scored` and `not_scored`, a
telemetry window with zero requests, and a read that does not answer `200`.

#99's `app_unreachable` is pinned in the same style: the app answering, one probe
that did not answer -- a cold free-tier instance -- and both probes still silent,
which is the one case where a read that never answered is user impact and does
alert, through the same one-open-issue path as the journey predicates.

`fetch` itself is pinned against a fake `urlopen` too (#108): a socket timeout on
connect and one on `read()` -- the escape that killed the hourly run -- a refused
connection, a DNS failure, a read that times out and then answers, and a read
that times out on every attempt, each one `ReadFailed` and never a traceback.

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


# The classifier as a whole: one healthy input, one read that fails, and #99's
# unreachable app.


def _healthy_reads() -> dict[str, Any]:
    """A healthy journey's three reads, plus the two probes #99 reads first."""
    return {
        impact_check.HEALTHZ_READ: {"status": "ok"},
        impact_check.UNREACHABLE_RUNS_READ: [
            run(
                7,
                "succeeded",
                started_at=NOW - timedelta(hours=1),
                finished_at=NOW - timedelta(minutes=59),
            )
        ],
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


def _raise_read_failures(
    reads: dict[str, Any], unreachable: dict[str, Any] | None = None
) -> Any:
    """`reads` as a `read` callable, with `unreachable`'s paths raising instead."""
    failures = unreachable or {}

    def read(path: str) -> Any:
        if path in failures:
            raise failures[path]
        return reads[path]

    return read


def test_check_fires_none_on_a_healthy_journey() -> None:
    report = impact_check.check(_healthy_reads().__getitem__, NOW)

    assert report.healthy
    assert report.fired == []


def test_check_does_not_alert_on_one_probe_that_did_not_answer() -> None:
    """AC1 (#99): a cold start loses the first read; the second answering is enough."""
    healthz_down = {
        impact_check.HEALTHZ_READ: impact_check.ReadFailed(
            impact_check.HEALTHZ_READ,
            None,
            TimeoutError("The read operation timed out"),
        )
    }

    report = impact_check.check(
        _raise_read_failures(_healthy_reads(), healthz_down), NOW
    )
    unreachable = next(
        verdict for verdict in report.verdicts if verdict.name == "app_unreachable"
    )

    assert report.healthy, report.body
    assert report.fired == []
    assert unreachable.values["probes_failed"] == 1


def test_check_fires_app_unreachable_when_both_probes_still_fail() -> None:
    """AC1/AC2 (#99): `/healthz` and then `/runs?limit=1` both never answered."""
    calls: list[str] = []

    def read(path: str) -> Any:
        calls.append(path)
        if path in impact_check.APP_PROBES:
            raise impact_check.ReadFailed(
                path, None, urllib.error.URLError("connection refused")
            )
        raise AssertionError(f"no journey read may follow an unreachable app: {path}")

    report = impact_check.check(read, NOW)

    assert calls == list(impact_check.APP_PROBES)
    assert report.fired == ["app_unreachable"]
    assert report.runs == []
    assert "app_unreachable" in report.title
    assert impact_check.HEALTHZ_READ in report.body
    assert impact_check.UNREACHABLE_RUNS_READ in report.body
    assert "none was read: the app did not answer" in report.body


def test_app_unreachable_evidence_names_each_read_and_what_it_answered() -> None:
    """AC2 (#99): the status or the error of the failed read, and no body."""
    report = impact_check.check(
        _raise_read_failures(
            _healthy_reads(),
            {
                impact_check.HEALTHZ_READ: impact_check.ReadFailed(
                    impact_check.HEALTHZ_READ, 503
                ),
                impact_check.UNREACHABLE_RUNS_READ: impact_check.ReadFailed(
                    impact_check.UNREACHABLE_RUNS_READ,
                    None,
                    TimeoutError("The read operation timed out"),
                ),
            },
        ),
        NOW,
    )
    verdict = report.verdicts[0]

    assert verdict.name == "app_unreachable"
    assert verdict.values["probes_failed"] == 2
    assert verdict.values["failed_reads"] == [
        {"path": impact_check.HEALTHZ_READ, "status": 503, "error": None},
        {
            "path": impact_check.UNREACHABLE_RUNS_READ,
            "status": None,
            "error": "The read operation timed out",
        },
    ]
    assert "503" in report.body
    assert "The read operation timed out" in report.body


def test_app_unreachable_is_quiet_on_a_single_probe_and_on_a_reachable_app() -> None:
    """One failed read is not an outage, and neither is a reading app."""
    healthz_down = impact_check.ReadFailed(impact_check.HEALTHZ_READ, 503)

    assert not impact_check.app_unreachable([]).fired
    assert not impact_check.app_unreachable([healthz_down]).fired
    assert impact_check.app_unreachable(
        [healthz_down, impact_check.ReadFailed(impact_check.UNREACHABLE_RUNS_READ, 503)]
    ).fired


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
        impact_check.HEALTHZ_READ: {"status": "ok"},
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


class _Response:
    """One `urlopen` answer: a `200` whose `read()` answers `payload`."""

    status = 200

    def __init__(self, payload: bytes = b'[{"id": 1}]') -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exception: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.payload


def _no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the retries without waiting out the real `RETRY_DELAY_SECONDS`."""
    monkeypatch.setattr(impact_check, "RETRY_DELAY_SECONDS", 0)


def test_the_read_is_retried_at_least_once_after_a_short_named_delay() -> None:
    """AC2's bound, read off the module: at least one retry, and a short wait."""
    assert impact_check.READ_ATTEMPTS >= 2
    assert 0 < impact_check.RETRY_DELAY_SECONDS <= 5


def test_fetch_names_a_read_that_never_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection is `ReadFailed` too, it names the read, and it retries."""
    calls: list[int] = []

    def unreachable(request: Any, timeout: float | None = None) -> Any:
        calls.append(1)
        raise urllib.error.URLError("connection refused")

    _no_delay(monkeypatch)
    monkeypatch.setattr(impact_check.urllib.request, "urlopen", unreachable)

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.fetch("https://example.test", impact_check.METRICS_READ)

    assert len(calls) == impact_check.READ_ATTEMPTS
    assert impact_check.METRICS_READ in str(failure.value)
    assert "no answer" in str(failure.value)
    assert "connection refused" in str(failure.value)


def test_fetch_maps_a_connect_timeout_to_read_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`urlopen` itself timing out -- the connect half of AC1's socket timeout."""

    def slow(request: Any, timeout: float | None = None) -> Any:
        raise TimeoutError("timed out")

    _no_delay(monkeypatch)
    monkeypatch.setattr(impact_check.urllib.request, "urlopen", slow)

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.fetch("https://example.test", impact_check.METRICS_READ)

    assert impact_check.METRICS_READ in str(failure.value)
    assert "timed out" in str(failure.value)


def test_fetch_maps_a_dns_failure_to_read_failed(monkeypatch: pytest.MonkeyPatch) -> None:

    def unresolved(request: Any, timeout: float | None = None) -> Any:
        raise urllib.error.URLError("[Errno -2] Name or service not known")

    _no_delay(monkeypatch)
    monkeypatch.setattr(impact_check.urllib.request, "urlopen", unresolved)

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.fetch("https://example.test", impact_check.RUNS_READ)

    assert "Name or service not known" in str(failure.value)


def test_fetch_gives_up_as_read_failed_when_a_read_times_out_on_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed bug: a cold instance answers, then `read()` times out."""
    reads: list[int] = []

    class ColdResponse:
        status = 200

        def __enter__(self) -> "ColdResponse":
            return self

        def __exit__(self, *exception: object) -> bool:
            return False

        def read(self) -> bytes:
            reads.append(1)
            raise TimeoutError("The read operation timed out")

    _no_delay(monkeypatch)
    monkeypatch.setattr(
        impact_check.urllib.request,
        "urlopen",
        lambda request, timeout=None: ColdResponse(),
    )

    with pytest.raises(impact_check.ReadFailed) as failure:
        impact_check.fetch("https://example.test", impact_check.RUNS_READ)

    assert len(reads) == impact_check.READ_ATTEMPTS
    assert impact_check.RUNS_READ in str(failure.value)
    assert "timed out" in str(failure.value)


def test_fetch_retries_and_returns_the_body_when_the_read_then_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2's retry path: one read that timed out, the next one `200`."""
    calls: list[int] = []

    def once_cold(request: Any, timeout: float | None = None) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        return _Response()

    _no_delay(monkeypatch)
    monkeypatch.setattr(impact_check.urllib.request, "urlopen", once_cold)

    assert impact_check.fetch("https://example.test", impact_check.RUNS_READ) == [{"id": 1}]
    assert len(calls) == 2


def test_fetch_answers_the_parsed_json_of_a_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        impact_check.urllib.request,
        "urlopen",
        lambda request, timeout=None: _Response(),
    )

    assert impact_check.fetch("https://example.test", impact_check.RUNS_READ) == [{"id": 1}]


def test_main_prints_one_line_and_exits_non_zero_when_a_journey_read_never_answers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC3: no traceback, one line naming the read, a non-zero exit, no report.

    The app answered its liveness probe and then lost a journey read, so this is a
    broken monitor and not `app_unreachable` (#99): red, and no issue opened.
    """

    def unreachable(request: Any, timeout: float | None = None) -> Any:
        if request.full_url.endswith(impact_check.HEALTHZ_READ):
            return _Response(payload=b'{"status": "ok"}')
        raise urllib.error.URLError("connection refused")

    _no_delay(monkeypatch)
    monkeypatch.setenv("JOBRADAR_URL", "https://example.test")
    monkeypatch.setattr(impact_check.urllib.request, "urlopen", unreachable)

    exit_code = impact_check.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert impact_check.RUNS_READ in captured.err
    assert "connection refused" in captured.err


def test_main_prints_the_report_and_exits_zero_when_the_app_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1/AC2 (#99): an unreachable app is user impact, so the workflow gets the
    `healthy: false` report it opens or comments its one `alert` issue with.
    """

    def refused(request: Any, timeout: float | None = None) -> Any:
        raise urllib.error.URLError("connection refused")

    _no_delay(monkeypatch)
    monkeypatch.setenv("JOBRADAR_URL", "https://example.test")
    monkeypatch.setattr(impact_check.urllib.request, "urlopen", refused)

    exit_code = impact_check.main()
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert report["healthy"] is False
    assert report["fired"] == ["app_unreachable"]
    assert report["title"]
    assert impact_check.HEALTHZ_READ in report["body"]
    assert impact_check.UNREACHABLE_RUNS_READ in report["body"]
    assert "connection refused" in report["body"]


def test_the_report_json_is_the_machine_readable_half_the_workflow_reads() -> None:
    reads = {
        impact_check.RUNS_READ: [run(7, "failed", finished_at=NOW)],
        impact_check.MATCHES_READ: [],
        impact_check.METRICS_READ: [],
        impact_check.HEALTHZ_READ: {"status": "ok"},
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
        "app_unreachable",
    }
