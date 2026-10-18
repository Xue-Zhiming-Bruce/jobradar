"""On-call pager channel: one bounded page after the alert issue is written (#100).

`alert.yml` (#92) is the channel a person reads: one hourly read of the deployed
journey, one open `alert` issue, and the evidence in its body. A repository where
GitHub notifications are not read can also wake someone on a third-party pager
(PagerDuty, Opsgenie or Telegram), and this module is that call. The two secrets
and the no-op are documented in `README.md`'s alerting section.

**Opt-in, and a no-op when off (AC1).** The endpoint is `PAGER_URL` and its
credential `PAGER_TOKEN`; both arrive in the environment the workflow fills from
repository secrets. `PAGER_URL` unset or empty is the documented no-op: no call is
made and `main` exits `0`, so an unconfigured repository is green and its
behaviour unchanged.

**The same evidence, and no more (AC2).** The payload is the report's own `title`
and `body` -- the two strings `impact_check.py` (#92) wrote and `alert.yml` put on
the issue -- so the page carries exactly the issue's evidence: run ids, statuses,
counts, instants and route names. `page_from_report` answers `None` for a report
that is not `healthy: false`, so a healthy run pages nobody. Nothing private can
ride along: both strings pass through `responder.redact`, which replaces every
available credential value and any connection string with `[redacted]`, so no
resume text, file name, API key, connection string or request body reaches the
pager.

**Bounded, and its failure is reported (AC3).** One call carries the one timeout
and is retried at most once; a non-`2xx` answer, and no answer at all, are the
failure. `main` prints an `::error::` and exits non-zero on that failure, naming
no credential, and the `alert` issue the earlier step opened is untouched and
still open -- a page that did not arrive cannot lose the GitHub issue.

**Offline (AC4).** The call is the `post` seam, so `test_notifier.py` drives the
delivered case, the unset-configuration case and the redaction case over a fake
pager endpoint: no socket, no real webhook, no credential.

The standard library and `responder`'s redaction are the whole dependency.
"""

import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `ops/oncall` is not a package: its modules are loaded by path -- the way the
# offline tests load them -- so this file puts its own directory on `sys.path`
# before importing its neighbour, and works both as `python3 ops/oncall/notifier.py`
# and loaded by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import responder  # noqa: E402 -- the import above must run first

# The pager endpoint and its credential, both read from repository secrets (AC1).
PAGER_URL_VARIABLE = "PAGER_URL"
PAGER_TOKEN_VARIABLE = "PAGER_TOKEN"

# One call's timeout, and how many attempts it may take: the first, and one retry.
PAGER_TIMEOUT_SECONDS = 10
PAGER_ATTEMPTS = 2

# Every way one `urllib` call can fail without answering: a refused or reset
# connection (`OSError`) and a peer that accepts it and then stalls
# (`http.client.HTTPException`). `HTTPError` is an `OSError` too and is caught
# first, where its status is an answer of its own.
_NETWORK_FAILURES = (OSError, http.client.HTTPException)


@dataclass(frozen=True)
class Page:
    """The one page: the issue's own title and body, and nothing else (AC2)."""

    title: str
    body: str

    def payload(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}


# The seam the offline test replaces: one POST, its status, or `None` when it did
# not answer. `timeout` is the one timeout the call is bounded by (AC3).
Post = Callable[[Mapping[str, str], float], int | None]


def page_from_report(report: Mapping[str, Any], secrets: Sequence[str]) -> Page | None:
    """The page a report makes, or `None` when there is nothing to page (AC2).

    Only a report that says `healthy: false` makes a page: a healthy journey is
    nobody's incident, and a report that carries no `healthy` field is not
    evidence of impact either. The title and the body are the report's own,
    redacted, so the page is the issue's evidence and no more.
    """
    if report.get("healthy") is not False:
        return None
    return Page(
        title=responder.redact(str(report.get("title", "")), secrets),
        body=responder.redact(str(report.get("body", "")), secrets),
    )


def secrets_to_redact(token: str = "") -> tuple[str, ...]:
    """Every credential value a page must not carry (AC2)."""
    return responder.secrets_from_environment() + ((token,) if token else ())


def notify(
    page: Page,
    post: Post,
    *,
    attempts: int = PAGER_ATTEMPTS,
    timeout: float = PAGER_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Deliver `page` at most `attempts` times, and answer whether it arrived (AC3).

    Each attempt carries the one `timeout`, and the loop makes at most one retry
    after the first. A `2xx` answer is delivery; a non-`2xx` status, or `None` for
    a call that did not answer, is a failure whose last answer the detail names.
    """
    detail = "the pager was not called"
    for attempt in range(1, attempts + 1):
        status = post(page.payload(), timeout)
        if status is not None and 200 <= status < 300:
            return True, f"the pager answered HTTP {status} on attempt {attempt}"
        detail = (
            "the pager did not answer"
            if status is None
            else f"the pager answered HTTP {status}"
        )
        detail += f" on attempt {attempt} of {attempts}"
    return False, detail


def urllib_post(url: str, token: str) -> Post:
    """The real pager call: one JSON POST, its status, or `None` when it did not answer.

    The credential is the `Authorization: Bearer` header and nothing else: it is
    never put in the payload and never printed. A peer that refuses the connection
    or accepts it and then stalls is `None`, never an exception (AC3).
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def post(payload: Mapping[str, str], request_timeout: float) -> int | None:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:  # noqa: S310
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except _NETWORK_FAILURES:
            return None

    return post


def main(report_path: str = "report.json") -> int:
    """Read the report and the pager's configuration, make at most one page, and report.

    The documented no-op cases -- a report that cannot be read, a healthy report,
    and an unset `PAGER_URL` -- exit `0` and make no call, so an unconfigured
    repository and a quiet journey are not failures (AC1). A pager call that fails
    after its one retry exits non-zero with an `::error::` naming the failure and
    no credential, while the `alert` issue the earlier step opened stays open
    (AC3).
    """
    try:
        report: Any = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(
            f"notifier: no readable report at {report_path} ({type(error).__name__}), "
            "so no page was made.",
            file=sys.stderr,
        )
        return 0
    if not isinstance(report, Mapping):
        print("notifier: the report is not a JSON object, so no page was made.", file=sys.stderr)
        return 0

    url = os.environ.get(PAGER_URL_VARIABLE, "").strip()
    if not url:
        print(
            f"notifier: {PAGER_URL_VARIABLE} is unset or empty, so no pager call was "
            "made; this is the documented no-op.",
        )
        return 0

    token = os.environ.get(PAGER_TOKEN_VARIABLE, "").strip()
    page = page_from_report(report, secrets_to_redact(token))
    if page is None:
        print("notifier: the journey is healthy, so no page was made.")
        return 0

    delivered, detail = notify(page, urllib_post(url, token))
    if delivered:
        print(f"notifier: {detail}.")
        return 0
    print(
        f"::error::notifier: {detail}, within one timeout and one retry. The alert "
        "issue is still open on GitHub; a person must be reached another way.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "report.json"))
