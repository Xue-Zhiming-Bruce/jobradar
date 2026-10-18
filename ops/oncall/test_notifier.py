"""`ops/oncall/notifier.py`'s pager call, driven offline over a fake pager endpoint.

AC1's opt-in and its unset-configuration no-op are pinned by
`test_an_unset_pager_is_a_documented_no_op_that_calls_nothing` and by the workflow
test, which reads `alert.yml` and asserts the step sits after the issue step. AC2's
"the issue's evidence and no more" is the delivered case plus
`test_a_credential_in_the_report_reaches_no_page`, and `test_a_healthy_report_pages_nobody`
pins that no page is made without impact. AC3's bound is pinned by the attempt count
and the timeout each attempt carries, and its "reported without losing the issue" by
the failure case's `::error::`. AC4's offline requirement is the fake `post` seam
here: no socket, no real webhook and no credential is touched.

The module lives under `ops/`, outside the backend package, so it is loaded by path
the way `ops/oncall/test_recheck.py` loads `recheck.py`: it is the real file, not a
copy that could drift.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULE_PATH = REPO_ROOT / "ops" / "oncall" / "notifier.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "alert.yml"
ISSUE_STEP = "Open, update or close the one open alert issue"
PAGER_STEP = "Notify the configured pager"
PAGER_URL = "https://pager.example.test/hook"

_spec = importlib.util.spec_from_file_location("notifier", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
notifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notifier)

# The alert report #92's `impact_check.py` writes, and the two strings whose evidence
# the issue body and the page both carry.
REPORT = {
    "healthy": False,
    "fired": ["newest_run_failed"],
    "title": "JobRadar alert: newest_run_failed",
    "body": "\n".join(
        [
            "## JobRadar digest journey",
            "",
            "User impact detected: `newest_run_failed`.",
            "",
            "- id `7`, status `failed`",
        ]
    ),
}

# A planted credential and a planted connection string, assembled from parts so this
# module's own source carries neither a secret-shaped literal nor a URL with a
# password (the rule `backend/tests/test_committed_data_guard.py` enforces).
SECRET = "pager-" + "sentinel-" + "not-a-real-key"
CONNECTION_STRING = "postgresql://jobradar:" + "planted" + "@db.example.test:5432/jobradar"


class FakePager:
    """A fake pager endpoint: it records each call and answers a programmed status list.

    The last status repeats, so a single-element list answers the same to every
    attempt and the attempt count is the only thing a case reads.
    """

    def __init__(self, statuses: list[int | None]) -> None:
        self._statuses = statuses
        self.payloads: list[dict[str, str]] = []
        self.timeouts: list[float] = []

    def post(self, payload: dict[str, str], timeout: float) -> int | None:
        self.payloads.append(dict(payload))
        self.timeouts.append(timeout)
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]


def write_report(tmp_path: Path, report: dict | None = None) -> str:
    """One `report.json` on disk, the file the workflow's notifier step reads."""
    path = tmp_path / "report.json"
    path.write_text(json.dumps(REPORT if report is None else report), encoding="utf-8")
    return str(path)


def fake_urllib_post(pager: FakePager, seen: list[tuple[str, str]]) -> object:
    """`notifier.urllib_post` replaced by the fake endpoint, recording the URL and token."""

    def urllib_post(url: str, token: str) -> object:
        seen.append((url, token))
        return pager.post

    return urllib_post


# AC2, AC4: the delivered case, and the payload is the issue's evidence and no more.


def test_a_delivered_page_carries_the_issue_title_and_body_and_no_more() -> None:
    pager = FakePager([202])

    delivered, detail = notifier.notify(notifier.page_from_report(REPORT, ()), pager.post)

    assert delivered is True
    assert "202" in detail
    assert pager.payloads == [{"title": REPORT["title"], "body": REPORT["body"]}]
    assert set(pager.payloads[0]) == {"title", "body"}


def test_main_delivers_the_report_to_the_configured_pager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAGER_URL", PAGER_URL)
    monkeypatch.delenv("PAGER_TOKEN", raising=False)
    pager = FakePager([202])
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(notifier, "urllib_post", fake_urllib_post(pager, seen))

    assert notifier.main(write_report(tmp_path)) == 0
    assert seen == [(PAGER_URL, "")]
    assert pager.payloads[0]["body"] == REPORT["body"]


# AC1, AC4: the unset-configuration case is a no-op, and a healthy report pages nobody.


def test_an_unset_pager_is_a_documented_no_op_that_calls_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PAGER_URL", raising=False)
    monkeypatch.delenv("PAGER_TOKEN", raising=False)
    called: list[str] = []

    def never(url: str, token: str) -> object:
        called.append(url)
        raise AssertionError("an unset PAGER_URL must not make a call")

    monkeypatch.setattr(notifier, "urllib_post", never)

    assert notifier.main(write_report(tmp_path)) == 0
    assert called == []


def test_a_healthy_report_pages_nobody(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAGER_URL", PAGER_URL)
    pager = FakePager([202])
    monkeypatch.setattr(notifier, "urllib_post", fake_urllib_post(pager, []))

    assert notifier.main(write_report(tmp_path, {**REPORT, "healthy": True})) == 0
    assert pager.payloads == []


def test_a_report_without_a_healthy_verdict_is_not_evidence_of_impact() -> None:
    assert notifier.page_from_report({"title": "t", "body": "b"}, ()) is None
    assert notifier.page_from_report(REPORT, ()) is not None


# AC2, AC4: the redaction case.


def test_a_credential_in_the_report_reaches_no_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAGER_URL", PAGER_URL)
    monkeypatch.setenv("PAGER_TOKEN", SECRET)
    body = f"{REPORT['body']}\ntoken {SECRET} at {CONNECTION_STRING}"
    pager = FakePager([202])
    monkeypatch.setattr(notifier, "urllib_post", fake_urllib_post(pager, []))

    assert notifier.main(write_report(tmp_path, {**REPORT, "body": body})) == 0

    sent = json.dumps(pager.payloads)
    assert SECRET not in sent
    assert CONNECTION_STRING not in sent
    assert "[redacted]" in sent


# AC3: the call is bounded by one timeout and at most one retry.


def test_one_timeout_and_at_most_one_retry_bound_every_call() -> None:
    page = notifier.page_from_report(REPORT, ())
    pager = FakePager([None])

    delivered, detail = notifier.notify(page, pager.post)

    assert delivered is False
    assert notifier.PAGER_ATTEMPTS == 2
    assert pager.payloads == [page.payload()] * 2
    assert pager.timeouts == [notifier.PAGER_TIMEOUT_SECONDS] * 2
    assert "did not answer" in detail


def test_one_retry_delivers_when_the_first_attempt_fails() -> None:
    pager = FakePager([500, 202])

    delivered, detail = notifier.notify(notifier.page_from_report(REPORT, ()), pager.post)

    assert delivered is True
    assert len(pager.payloads) == 2
    assert "attempt 2" in detail


# AC3: a failed page is reported, and the issue the earlier step opened is not lost.


def test_a_failed_page_is_reported_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PAGER_URL", PAGER_URL)
    monkeypatch.delenv("PAGER_TOKEN", raising=False)
    pager = FakePager([503])
    monkeypatch.setattr(notifier, "urllib_post", fake_urllib_post(pager, []))

    assert notifier.main(write_report(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "::error::" in err
    assert "HTTP 503" in err
    assert "still open on GitHub" in err
    assert len(pager.payloads) == 2


def test_a_report_that_cannot_be_read_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAGER_URL", PAGER_URL)
    pager = FakePager([202])
    monkeypatch.setattr(notifier, "urllib_post", fake_urllib_post(pager, []))

    assert notifier.main(str(tmp_path / "missing.json")) == 0
    assert pager.payloads == []


# AC1: the step sits after the issue step and reads the pager from secrets.


def test_the_workflow_notifies_the_pager_only_after_the_issue_step() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    names = re.findall(r"^      - name: (.+)$", text, re.MULTILINE)
    assert names.index(PAGER_STEP) == names.index(ISSUE_STEP) + 1
    assert "python3 ops/oncall/notifier.py report.json" in text
    assert "PAGER_URL: ${{ secrets.PAGER_URL }}" in text
    assert "PAGER_TOKEN: ${{ secrets.PAGER_TOKEN }}" in text
