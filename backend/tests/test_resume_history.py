"""`GET /resume`, `PATCH /resume/{id}`, `DELETE /resume/{id}`: the uploaded history, acted on.

Every test drives the real app through `TestClient` -- no server, no port, no
network -- with `DATABASE_URL` and `UPLOADS_DIR` pointed at a temporary
directory, so the suite uploads to a scratch directory, deletes from it and
leaves `git status --short` empty.

What the routes must not do is as much the point as what they say. No answer
carries the resume text or the stored path: the list items and the activation
answer are `ResumeSummary`, five fields, one renderer. A body that does not say
"make this one active" writes nothing at all. A delete removes the row and the
file it names, and the last case here reads the `resume` table through the app's
own session to show the text is gone from the database and reached no response
body and no log record at `DEBUG`.

The uploads are synthetic and their text is a sentinel: the repository is
public, and a fixture that held a real resume would be the one thing this suite
must never do.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select

from app import db
from app.main import app
from app.models import Resume, Run
from app.routes.internal import TICK_SECRET_HEADER, TICK_SECRET_VARIABLE, get_run_starter
from app.routes.resume import ResumeSummary
from app.services.pipeline import NO_ACTIVE_RESUME

TEXT_PLAIN = "text/plain"

# The five fields of a resume on the wire, and therefore the whole body both
# routes may answer with. A field that is not here is a field a route invented.
RESUME_FIELDS = {"id", "filename", "content_type", "uploaded_at", "is_active"}

# A distinctive line for the leak checks: a log record or a response body holding
# it fails, and an empty stream or body would be no evidence at all.
SENTINEL = "SENTINEL-RESUME-LINE: this text must never leave the database."

TICK_SECRET = "tick-secret-sentinel-7f4c1d9ab38e"

# Bodies that do not say "make this one active": a missing field, the other
# value, a field the model does not declare, and a value of the wrong type.
_REFUSED_BODIES = [
    ({}, "missing"),
    ({"is_active": False}, "false"),
    ({"is_active": True, "extra": "no"}, "extra-field"),
    ({"is_active": "true"}, "string"),
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with a temporary database, a temporary uploads directory and the tick's secret."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv(TICK_SECRET_VARIABLE, TICK_SECRET)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def inline_starter() -> Iterator[None]:
    """The tick's starter replaced by one that runs the work inside the request, on this thread."""
    app.dependency_overrides[get_run_starter] = lambda: _run_inline
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_run_starter, None)


def _run_inline(work: Callable[[], None]) -> None:
    """The starter's body: run the work here and now, so the run row is final when the tick answers."""
    work()


def upload(client: TestClient, filename: str = "resume.txt", text: str = SENTINEL) -> dict[str, Any]:
    """One upload of synthetic text, and the body its `201` answered with."""
    response = client.post("/resume", files={"file": (filename, text.encode(), TEXT_PLAIN)})
    assert response.status_code == 201, response.text
    return response.json()


def listed(client: TestClient) -> list[dict[str, Any]]:
    """The history as `GET /resume` answers it, having checked it answered."""
    response = client.get("/resume")
    assert response.status_code == 200, response.text
    return response.json()


def table_state() -> list[tuple[int, str, bool]]:
    """Every row's id, filename and active state, read through the app's own session."""
    with db.session_scope() as session:
        return [
            (row.id, row.filename, row.is_active)
            for row in session.scalars(select(Resume).order_by(Resume.id))
        ]


def active_ids() -> list[int]:
    """Every id of an active row, read through the app's own session."""
    with db.session_scope() as session:
        return list(session.scalars(select(Resume.id).where(Resume.is_active.is_(True))))


def stored_texts() -> list[str]:
    """Every text the `resume` table holds, read through the app's own session."""
    with db.session_scope() as session:
        return list(session.scalars(select(Resume.text)))


def stored_path_of(resume_id: int) -> Path:
    """The file that row's `stored_path` names, read through the app's own session."""
    with db.session_scope() as session:
        return Path(session.get(Resume, resume_id).stored_path)


def seed_stored_path(filename: str, stored_path: Path) -> int:
    """One row naming a path of this test's choosing, and its id.

    No writer in this app stores a path outside the uploads directory, so the
    case that proves such a path is never unlinked has to seed the row itself.
    """
    with db.session_scope() as session:
        row = Resume(
            filename=filename,
            content_type=TEXT_PLAIN,
            text="seeded synthetic text",
            stored_path=str(stored_path),
            is_active=False,
        )
        session.add(row)
        session.flush()
        return row.id


def run_row(run_id: int) -> dict[str, Any]:
    """One `run` row's status, error and counts, read through the app's own session."""
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        return {
            "status": run.status,
            "error": run.error,
            "postings_seen": run.postings_seen,
            "matches_created": run.matches_created,
        }


def log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


# The list: every row, newest first, in the shape the activation answers with.


def test_the_list_is_empty_before_the_first_upload(client: TestClient) -> None:
    assert listed(client) == []


def test_the_list_answers_every_upload_newest_first(client: TestClient) -> None:
    first = upload(client, "first.txt", "first synthetic resume")
    second = upload(client, "second.txt", "second synthetic resume")
    third = upload(client, "third.txt", "third synthetic resume")

    body = listed(client)

    assert [item["id"] for item in body] == [third["id"], second["id"], first["id"]]
    assert [item["filename"] for item in body] == ["third.txt", "second.txt", "first.txt"]
    assert [item["content_type"] for item in body] == [TEXT_PLAIN] * 3
    assert [item["is_active"] for item in body] == [True, False, False]
    for item in body:
        assert set(item) == RESUME_FIELDS
        assert datetime.fromisoformat(item["uploaded_at"]) is not None


def test_the_list_items_are_the_body_the_activation_answers_with(client: TestClient) -> None:
    """One model and one renderer: the item and the single-row answer cannot drift apart."""
    first = upload(client, "first.txt", "first synthetic resume")
    upload(client, "second.txt", "second synthetic resume")

    activated = client.patch(f"/resume/{first['id']}", json={"is_active": True})

    assert activated.status_code == 200, activated.text
    body = activated.json()
    assert set(body) == RESUME_FIELDS
    assert set(body) == set(ResumeSummary.model_fields)
    assert body["is_active"] is True
    assert body["id"] == first["id"]
    assert SENTINEL not in activated.text  # the text is stored, and not answered with

    items = listed(client)
    assert [item for item in items if item["id"] == first["id"]] == [body]
    assert [item for item in items if item["is_active"]] == [body]


def test_the_list_carries_no_resume_text_and_no_stored_path(client: TestClient) -> None:
    upload(client)

    response = client.get("/resume")

    assert response.status_code == 200
    assert SENTINEL not in response.text
    assert "stored_path" not in response.text
    assert set(response.json()[0]) == RESUME_FIELDS


# The activation: one row active at a time, and only ever by asking for it.


def test_activating_an_older_resume_leaves_exactly_one_active_row(client: TestClient) -> None:
    first = upload(client, "first.txt", "first synthetic resume")
    second = upload(client, "second.txt", "second synthetic resume")
    assert active_ids() == [second["id"]]

    response = client.patch(f"/resume/{first['id']}", json={"is_active": True})

    assert response.status_code == 200, response.text
    assert active_ids() == [first["id"]]
    assert [(item["id"], item["is_active"]) for item in listed(client)] == [
        (second["id"], False),
        (first["id"], True),
    ]


def test_activating_the_active_resume_twice_answers_the_same_body_twice(client: TestClient) -> None:
    uploaded = upload(client)

    first = client.patch(f"/resume/{uploaded['id']}", json={"is_active": True})
    second = client.patch(f"/resume/{uploaded['id']}", json={"is_active": True})

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert active_ids() == [uploaded["id"]]


@pytest.mark.parametrize(
    ("body", "case"), _REFUSED_BODIES, ids=[case for _, case in _REFUSED_BODIES]
)
def test_a_body_that_does_not_say_make_this_active_is_422_and_writes_nothing(
    client: TestClient, body: dict[str, Any], case: str
) -> None:
    first = upload(client, "first.txt", "first synthetic resume")
    upload(client, "second.txt", "second synthetic resume")
    before = table_state()

    response = client.patch(f"/resume/{first['id']}", json=body)

    assert response.status_code == 422, response.text
    assert table_state() == before
    assert SENTINEL not in response.text


# An id no row has, and a path value that is not an id at all.


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_an_id_with_no_row_is_404_and_writes_nothing(client: TestClient, method: str) -> None:
    uploaded = upload(client)
    missing = uploaded["id"] + 1
    before = table_state()

    if method == "patch":
        response = client.patch(f"/resume/{missing}", json={"is_active": True})
    else:
        response = client.delete(f"/resume/{missing}")

    assert response.status_code == 404
    assert response.json()["detail"], "a refusal must say what it refused"
    assert table_state() == before


@pytest.mark.parametrize("method", ["patch", "delete"])
@pytest.mark.parametrize("value", ["abc", "1.5", "one"])
def test_a_path_value_that_is_not_a_resume_id_is_422(
    client: TestClient, method: str, value: str
) -> None:
    upload(client)
    before = table_state()

    if method == "patch":
        response = client.patch(f"/resume/{value}", json={"is_active": True})
    else:
        response = client.delete(f"/resume/{value}")

    assert response.status_code == 422, response.text
    assert table_state() == before


# The delete: the row and the file it names, and nothing else.


def test_deleting_a_resume_answers_204_removes_the_row_and_the_file(client: TestClient) -> None:
    first = upload(client, "first.txt", "first synthetic resume")
    second = upload(client, "second.txt", "second synthetic resume")
    path = stored_path_of(first["id"])
    assert path.is_file()

    response = client.delete(f"/resume/{first['id']}")

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert [item["id"] for item in listed(client)] == [second["id"]]
    assert not path.exists()
    assert stored_path_of(second["id"]).is_file()  # another row's file is left alone


def test_deleting_a_row_whose_file_is_already_gone_still_answers_204(client: TestClient) -> None:
    uploaded = upload(client)
    stored_path_of(uploaded["id"]).unlink()

    response = client.delete(f"/resume/{uploaded['id']}")

    assert response.status_code == 204
    assert listed(client) == []


def test_a_stored_path_outside_the_uploads_directory_is_never_unlinked(
    client: TestClient, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("a file no upload wrote")
    seeded = seed_stored_path("seeded.txt", outside)

    response = client.delete(f"/resume/{seeded}")

    assert response.status_code == 204
    assert listed(client) == []
    assert outside.read_text() == "a file no upload wrote"


def test_deleting_the_active_resume_leaves_the_next_tick_with_nothing_to_score(
    client: TestClient, inline_starter: None
) -> None:
    """The state after the delete is the state of an instance that never uploaded one."""
    uploaded = upload(client)

    assert client.delete(f"/resume/{uploaded['id']}").status_code == 204

    assert active_ids() == []
    ticked = client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})
    assert ticked.status_code == 202
    row = run_row(ticked.json()["id"])
    assert (row["status"], row["error"]) == ("failed", NO_ACTIVE_RESUME)
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert SENTINEL not in ticked.text


# The text: gone from the database, and in no body and no log line over the three requests.


def test_the_text_is_gone_from_the_database_and_from_every_answer_and_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    uploaded = upload(client)
    caplog.set_level(logging.DEBUG)

    listed_response = client.get("/resume")
    activated = client.patch(f"/resume/{uploaded['id']}", json={"is_active": True})
    deleted = client.delete(f"/resume/{uploaded['id']}")
    # A record of the test's own, so an empty stream cannot pass as evidence.
    logging.getLogger("tests.resume_history").debug("the capture is live")

    assert (listed_response.status_code, activated.status_code, deleted.status_code) == (
        200,
        200,
        204,
    )
    # The row holding it is gone, read through the app's own session.
    assert [text for text in stored_texts() if SENTINEL in text] == []
    assert uploaded["id"] not in [item["id"] for item in listed(client)]
    assert listed(client) == []

    for response in (listed_response, activated, deleted):
        assert SENTINEL not in response.text
    lines = log_lines(caplog.records)
    assert lines, "the capture must be non-vacuous"
    assert [line for line in lines if SENTINEL in line] == []
