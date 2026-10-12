"""What both cases in this package need to read a run: the deadline, the poll, and the rows.

`wait_for_terminal` is the one place a run is waited for, and `RUN_DEADLINE_SECONDS`
is the one deadline: a run that has not ended by then fails the case with the
last row it was seen in, rather than hanging the suite or sleeping for a
coin-flip. The rows are read through the app's own `db.session_scope()`, the way
the unit suite reads them, so what a case asserts is what the app wrote.

`rows_of` is for the case that compares a whole table before and after a tick --
`notified_at`, `state` and `updated_at` included, which is what "unchanged"
means. `GET /matches` deliberately does not expose `notified_at`, so a case that
asserts on it reads the row.
"""

import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.models import Run

# The vocabulary `run.status` accepts in `app.models.Run`, spelled the way the
# check constraint spells it: the one non-terminal status is the one polled past.
RUNNING = "running"

# One deadline for a run to reach a terminal status, and the short sleep between
# two reads of its row. Ten seconds is far more than one run of two synthetic
# postings needs, and short enough that a run that will never end fails a case
# instead of hanging it.
RUN_DEADLINE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05


def wait_for_terminal(client: TestClient, run_id: int) -> dict[str, Any]:
    """Poll `GET /runs/{id}` until the run ends, and answer with its last body.

    A run that is still `running` at the deadline fails the case and names the
    last row read, so the failure says which status and which counts it saw
    rather than only that time ran out. No retry loop: the run's row is the only
    thing looked at, and the row is what the app writes when the run ends.
    """
    deadline = time.monotonic() + RUN_DEADLINE_SECONDS
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        row = client.get(f"/runs/{run_id}").json()
        if row["status"] != RUNNING:
            return row
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"run {run_id} was still '{RUNNING}' after {RUN_DEADLINE_SECONDS:g} s: the last row "
        f"read was {row}"
    )


def rows_of(model: type) -> list[dict[str, Any]]:
    """Every row of one table as plain column values, in ascending id order."""
    with db.session_scope() as session:
        columns = [column.name for column in model.__table__.columns]
        return [
            {name: getattr(row, name) for name in columns}
            for row in session.scalars(select(model).order_by(model.id))
        ]


def run_rows() -> list[dict[str, Any]]:
    """Every `run` row: what "no second row, and this one untouched" means."""
    return rows_of(Run)
