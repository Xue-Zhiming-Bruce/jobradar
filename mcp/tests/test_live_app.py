"""The one case that drives the real app: the row a tick wrote is the row its own id names.

It is excluded from the default run by its marker, the way the backend excludes
`integration`, because it starts a real app on a real port. The start is the one
the README documents -- `uv run uvicorn app.main:app --port <free port>` from
`backend/`, so `app` is importable -- with a throwaway secret given to the app and
to this server, and the app's `DATABASE_URL` pointed at a temporary SQLite file
under the system temp directory, never `backend/data/jobradar.db`, so no existing
data is touched and nothing is written inside the repository.

The run's final status is deliberately not asserted: an empty watchlist and no
model key mean the pipeline may score nothing and end the row `succeeded` or
`failed`, and that is the app's own business.
"""

import socket
import subprocess
import time
from pathlib import Path

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer

from jobradar_mcp.__main__ import build_server
from jobradar_mcp.client import TICK_SECRET_VARIABLE, URL_VARIABLE, new_client
from tests.support import answered, call

pytestmark = pytest.mark.live

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
LIVE_SECRET = "throwaway-live-secret"
STARTUP_TIMEOUT_SECONDS = 60.0


def free_port() -> int:
    """A port nothing is listening on, asked of the system and then let go."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_up(base_url: str, process: subprocess.Popen) -> None:
    """Poll `/healthz` with this server's own client, or fail with what the app printed."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    with new_client() as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"the app exited with {process.returncode}:\n{process.stdout.read()}")
            try:
                answer = client.get(f"{base_url}/healthz", timeout=1.0)
            except httpx2.HTTPError:
                time.sleep(0.2)
                continue
            if answer.status_code == 200:
                return
            time.sleep(0.2)
    raise AssertionError(f"the app did not answer at {base_url} in {STARTUP_TIMEOUT_SECONDS} seconds")


@pytest.fixture
def running_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The app on a free port, and the two variables this server reads pointed at it."""
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    database = tmp_path / "jobradar-live.db"

    # The app's own two settings, set for the app's process and inherited by it:
    # the MCP server reads neither, and the database is a file under the system
    # temp directory rather than one inside the repository.
    monkeypatch.setenv("TICK_SECRET", LIVE_SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")

    process = subprocess.Popen(
        ["uv", "run", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_until_up(base_url, process)
        assert database.exists()  # the throwaway the app was told to use, outside the repository
        monkeypatch.setenv(URL_VARIABLE, base_url)
        monkeypatch.setenv(TICK_SECRET_VARIABLE, LIVE_SECRET)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def test_the_row_trigger_run_returned_is_the_row_the_app_holds(running_app: str) -> None:
    """One tick over a real socket, then the same row read back by the id it answered with."""
    server: MCPServer = build_server()

    with new_client() as client:
        assert client.get(f"{running_app}/healthz", timeout=5.0).json() == {"status": "ok"}

    started = answered(call(server, "trigger_run", {}))
    read_back = answered(call(server, "get_run", {"run_id": started["id"]}))

    assert started["status"] == "running"
    assert started["finished_at"] is None and started["error"] is None
    assert started["postings_seen"] == 0 and started["matches_created"] == 0
    assert read_back["id"] == started["id"]
    assert read_back["started_at"] == started["started_at"]
