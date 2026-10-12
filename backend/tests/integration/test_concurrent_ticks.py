"""Two ticks at once: the second is refused, the first finishes, and the guard is freed.

The overlap has to be real, so it is made provable rather than waited for: the
pipeline is replaced at the declared `get_pipeline` seam by one that signals a
`threading.Event` when it starts and then blocks on a second until this case
releases it -- on its first call only, so a later tick completes. Nothing here
sleeps for a verdict and nothing asserts on `internal._run_in_progress`: the
evidence is the HTTP answers and the run rows, which is what a client sees.

This is the one case in the suite that replaces `get_pipeline`; every other case
leaves the provider, the daemon thread and the run-row lifecycle exactly as they
ship.
"""

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from app.main import app
from app.routes.internal import get_pipeline
from tests.integration.support import RUNNING, run_rows, wait_for_terminal

# The ceiling on a wait that should not happen, so a run that never starts fails
# this suite instead of hanging it. Nothing sleeps for it: the two events do the
# signalling, and the release timeout is what frees the thread if a case dies.
EVENT_TIMEOUT_SECONDS = 5.0


class BlockingPipeline:
    """A pipeline that blocks on its first call only, so a later tick completes.

    The two events are the whole synchronisation: `started` is set from inside the
    work, which is what proves the work is not on the request's thread, and
    `release` is the test's to set.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, run_id: int) -> None:
        self.calls += 1
        if self.calls > 1:
            return
        self.started.set()
        self.release.wait(timeout=EVENT_TIMEOUT_SECONDS)


@pytest.fixture
def blocking_pipeline() -> Iterator[BlockingPipeline]:
    """The one seam this case replaces: the run's work, blocked at its first call."""
    fake = BlockingPipeline()
    app.dependency_overrides[get_pipeline] = lambda: fake
    try:
        yield fake
    finally:
        fake.release.set()  # never leave the thread waiting on a case that failed
        app.dependency_overrides.pop(get_pipeline, None)


def tick(client: TestClient, tick_headers: dict[str, str]) -> Response:
    """One tick, and the answer it gave: this case asserts on the status it chose."""
    return client.post("/internal/tick", headers=tick_headers)


def test_two_overlapping_ticks_are_refused_one_at_a_time(
    client: TestClient, tick_headers: dict[str, str], blocking_pipeline: BlockingPipeline
) -> None:
    first = tick(client, tick_headers)
    assert first.status_code == 202, first.text
    run_id = first.json()["id"]

    assert blocking_pipeline.started.wait(timeout=EVENT_TIMEOUT_SECONDS), (
        "the run's work never started: nothing was handed a thread"
    )
    # The work is blocked, and the run's row still says so: the request answered
    # 202 without waiting for it, and the row is only written when the run ends.
    assert client.get(f"/runs/{run_id}").json()["status"] == RUNNING
    running = run_rows()

    second = tick(client, tick_headers)
    assert second.status_code == 409, second.text
    assert "in progress" in second.json()["detail"]
    assert run_rows() == running  # no second row, and the first one is untouched

    blocking_pipeline.release.set()
    finished = wait_for_terminal(client, run_id)
    assert finished["status"] == "succeeded"

    # Accepted again: `_run`'s `finally` gave the guard back when the run ended.
    third = tick(client, tick_headers)
    assert third.status_code == 202, third.text
    assert wait_for_terminal(client, third.json()["id"])["status"] == "succeeded"
    assert [row["status"] for row in run_rows()] == ["succeeded", "succeeded"]
