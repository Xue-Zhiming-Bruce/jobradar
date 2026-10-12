"""`/matches`: the read of every stored match, and the owner's verdict on one.

Every test drives the real app through `TestClient` -- no server, no port, no
network -- with `DATABASE_URL` and `UPLOADS_DIR` pointed at a temporary
directory, so the suite leaves no `backend/data/jobradar.db` and no uploaded file
behind and `git status --short` stays empty.

What the read must not do is as much the point as what it says: it applies no
filter, so a prefiltered and a not-scored row are in it exactly as they were
stored (`_is_a_non_score` is #10's rule, and whether #12's screen hides them is
#12's decision), it decodes the `gaps` JSON text rather than serving the column,
and it answers a row no writer in this app produces with `[]` instead of a `500`.
What the write must not do is move anything but `state`: the snapshot cases below
compare `id`, `score`, `reason`, `gaps`, `created_at` and `notified_at` across a
verdict, each of them carrying a distinctive non-default value first, so a write
that touched any of them fails here rather than quietly re-opening or re-alerting
a match. The matches are stored through the app's own writer, `record_score`,
where the row's shape matters; the rest are seeded straight through the app's own
session, and every stored value is synthetic.
"""

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from app import db
from app.main import app
from app.matching.prefilter import REJECTION_REASON_PREFIX
from app.matching.scorer import NOT_SCORED_PREFIX, MatchResult, record_score
from app.models import Company, Match, Posting, Resume
from app.routes.internal import TICK_SECRET_VARIABLE
from app.routes.matches import MatchSummary

# The nine fields of a match, and therefore the whole body both routes may answer
# with. A field that is not here is a field the route invented.
MATCH_FIELDS = {
    "id",
    "company_name",
    "title",
    "url",
    "score",
    "reason",
    "gaps",
    "state",
    "created_at",
}

# The columns a verdict must leave exactly as they were. `state` is deliberately
# not one of them: it is the one column the endpoint writes.
UNAFFECTED_COLUMNS = ("id", "score", "reason", "gaps", "created_at", "notified_at")

# Values that must not reach the wire: the tick's shared secret, a resume's text
# and the database URL. The resume text is really stored by that test, so its
# absence from the body is evidence rather than an empty comparison.
SECRET = "tick-secret-sentinel-7f4c1d9ab38e"
RESUME_MARKER = "QA-RESUME-SENTINEL-TEXT: synthetic resume, Python and PostgreSQL."
UPLOADS_DIR_VARIABLE = "UPLOADS_DIR"

# The distinctive values the read is asserted against, so a default, an empty
# string or a value taken from another row cannot pass.
COMPANY_NAME = "Nimbus Robotics Ltd"
POSTING_TITLE = "Staff Backend Engineer, Platform"
POSTING_URL = "https://boards.test/nimbus/jobs/8842"
REASON = "Synthetic fit: the resume names Python and PostgreSQL."

# Two instants a day apart, so "newest first" has something to sort and the
# tie-break has two rows written in the same one.
EARLY = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
LATE = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with its database and its uploads pointed at this test's temporary directory."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv(UPLOADS_DIR_VARIABLE, str(tmp_path / "uploads"))
    monkeypatch.setenv(TICK_SECRET_VARIABLE, SECRET)
    with TestClient(app) as test_client:
        yield test_client


def seed_company(name: str = COMPANY_NAME, slug: str = "nimbus") -> int:
    """One watchlist company, and its id."""
    with db.session_scope() as session:
        company = Company(name=name, slug=slug)
        session.add(company)
        session.flush()
        return company.id


def seed_posting(
    company_id: int,
    *,
    external_id: str = "8842",
    title: str = POSTING_TITLE,
    url: str = POSTING_URL,
) -> int:
    """One posting on that company's board, and its id."""
    with db.session_scope() as session:
        posting = Posting(company_id=company_id, external_id=external_id, title=title, url=url)
        session.add(posting)
        session.flush()
        return posting.id


def seed_match(
    posting_id: int,
    *,
    score: int = 90,
    reason: str = REASON,
    stored_gaps: str | None = "[]",
    state: str = "new",
    created_at: datetime | None = None,
    notified_at: datetime | None = None,
) -> int:
    """One `match` row written straight through the app's own session, and its id.

    `stored_gaps` is the raw column, so a case can put text there that no writer
    in this app produces and prove the read survives it.
    """
    with db.session_scope() as session:
        match = Match(
            posting_id=posting_id,
            score=score,
            reason=reason,
            gaps=stored_gaps,
            state=state,
            created_at=created_at,
            notified_at=notified_at,
        )
        session.add(match)
        session.flush()
        return match.id


def scored_match(
    *,
    score: int = 87,
    gaps: list[str] | None = None,
    state: str = "new",
    notified_at: datetime | None = None,
) -> tuple[int, int]:
    """A company, a posting and the match `record_score` writes for it, as their ids.

    The score, reason and `gaps` JSON text are therefore #8's own rows rather
    than a shape this test invented, which is what the verdict cases need before
    they can assert that a write moves `state` and nothing else.
    """
    company_id = seed_company()
    posting_id = seed_posting(company_id)
    record_score(posting_id, MatchResult(score=score, reason=REASON, gaps=gaps or ["Rust"]))
    match_id = match_id_of(posting_id)
    amend(match_id, state=state, notified_at=notified_at)
    return posting_id, match_id


def match_id_of(posting_id: int) -> int:
    """The id of the one match #8 wrote for that posting."""
    with db.session_scope() as session:
        return session.scalar(select(Match.id).where(Match.posting_id == posting_id))


def amend(match_id: int, **values: Any) -> None:
    """Write columns on one match straight through the app's own session."""
    with db.session_scope() as session:
        row = session.get(Match, match_id)
        for column, value in values.items():
            setattr(row, column, value)


def row_values(match_id: int) -> dict[str, Any]:
    """That match row's own columns, as values, with the stored JSON text decoded."""
    with db.session_scope() as session:
        match = session.get(Match, match_id)
        return {
            "id": match.id,
            "score": match.score,
            "reason": match.reason,
            "gaps": json.loads(match.gaps) if match.gaps else [],
            "state": match.state,
            "created_at": match.created_at,
            "notified_at": match.notified_at,
        }


def snapshot(match_id: int) -> dict[str, Any]:
    """The six columns a verdict must leave alone, read from the row itself."""
    values = row_values(match_id)
    return {column: values[column] for column in UNAFFECTED_COLUMNS}


def match_count() -> int:
    """How many `match` rows the database holds right now."""
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(Match))


def read(client: TestClient) -> Response:
    """One `GET /matches`, with no header at all: the route asks for no secret."""
    return client.get("/matches")


def matches(client: TestClient) -> list[dict[str, Any]]:
    """That read's body, with its `200` asserted first."""
    response = read(client)
    assert response.status_code == 200
    return response.json()


def verdict(client: TestClient, match_id: object, state: object) -> Response:
    """One `PATCH /matches/{id}` carrying that verdict."""
    return client.patch(f"/matches/{match_id}", json={"state": state})


def upload_resume(client: TestClient, text: str) -> Response:
    """One synthetic resume sent as a UTF-8 text file, the way a client sends one."""
    return client.post("/resume", files={"file": ("resume.txt", text.encode(), "text/plain")})


def test_an_empty_match_table_reads_as_an_empty_list(client: TestClient) -> None:
    response = read(client)

    assert response.status_code == 200
    assert response.json() == []
    assert response.text != "null"


def test_every_field_of_the_body_is_a_stored_value(client: TestClient) -> None:
    """The nine fields come from the three rows, and from nothing else."""
    company_id = seed_company()
    posting_id = seed_posting(company_id)
    record_score(posting_id, MatchResult(score=87, reason=REASON, gaps=["Rust", "Kubernetes"]))
    match_id = match_id_of(posting_id)
    amend(match_id, state="saved", created_at=EARLY)

    body = matches(client)[0]
    stored = row_values(match_id)

    assert set(body) == MATCH_FIELDS
    assert body["id"] == match_id
    assert body["company_name"] == COMPANY_NAME
    assert body["title"] == POSTING_TITLE
    assert body["url"] == POSTING_URL
    assert body["score"] == 87 == stored["score"]
    assert body["reason"] == REASON == stored["reason"]
    assert body["gaps"] == ["Rust", "Kubernetes"] == stored["gaps"]
    assert body["state"] == "saved" == stored["state"]
    assert datetime.fromisoformat(body["created_at"]) == EARLY == stored["created_at"]

    # The id in that body is the one the verdict route takes.
    assert verdict(client, body["id"], "dismissed").status_code == 200
    assert matches(client)[0]["state"] == "dismissed"


@pytest.mark.parametrize(
    ("stored_gaps", "expected"),
    [
        pytest.param(None, [], id="null-column"),
        pytest.param("[]", [], id="empty-list"),
        pytest.param('["Rust", "Kubernetes"]', ["Rust", "Kubernetes"], id="two-strings"),
        pytest.param("not json at all", [], id="not-json"),
        pytest.param('{"Rust": true}', [], id="json-object"),
        pytest.param('["Rust", 3]', [], id="a-list-that-is-not-strings"),
        pytest.param('"Rust"', [], id="json-string"),
    ],
)
def test_gaps_are_decoded_and_an_unrecognised_column_renders_empty(
    client: TestClient, stored_gaps: str | None, expected: list[str]
) -> None:
    posting_id = seed_posting(seed_company())
    seed_match(posting_id, stored_gaps=stored_gaps)

    response = read(client)
    body = response.json()[0]

    assert response.status_code == 200
    assert isinstance(body["gaps"], list)
    assert body["gaps"] == expected
    if stored_gaps is not None and stored_gaps != "[]":
        assert stored_gaps not in response.text


def test_the_list_is_newest_first_and_the_id_breaks_a_tie(client: TestClient) -> None:
    company_id = seed_company()
    older = seed_match(seed_posting(company_id, external_id="1"), created_at=EARLY)
    same_instant = seed_match(seed_posting(company_id, external_id="2"), created_at=EARLY)
    newest = seed_match(seed_posting(company_id, external_id="3"), created_at=LATE)

    assert [body["id"] for body in matches(client)] == [newest, same_instant, older]


def test_a_prefiltered_and_a_not_scored_row_are_listed_exactly_as_stored(
    client: TestClient,
) -> None:
    company_id = seed_company()
    rejection = REJECTION_REASON_PREFIX + "title term 'intern'"
    not_scored = NOT_SCORED_PREFIX + "the model call failed"
    seed_match(
        seed_posting(company_id, external_id="1"),
        score=0,
        reason=rejection,
        created_at=EARLY,
    )
    seed_match(
        seed_posting(company_id, external_id="2"),
        score=88,
        reason=REASON,
        created_at=datetime(2024, 1, 1, 13, 0, tzinfo=UTC),
    )
    seed_match(
        seed_posting(company_id, external_id="3"),
        score=0,
        reason=not_scored,
        created_at=LATE,
    )

    assert [(body["score"], body["reason"]) for body in matches(client)] == [
        (0, not_scored),
        (88, REASON),
        (0, rejection),
    ]


def test_a_verdict_answers_with_the_body_the_read_answers_with(client: TestClient) -> None:
    match_id = seed_match(seed_posting(seed_company()))

    response = verdict(client, match_id, "saved")

    assert response.status_code == 200
    assert response.json()["state"] == "saved"
    assert response.json() == matches(client)[0]


def test_both_routes_render_the_one_match_summary_the_module_declares(client: TestClient) -> None:
    """One model behind both bodies, so the two routes cannot drift apart."""
    match_id = seed_match(seed_posting(seed_company()))

    listed = matches(client)[0]
    updated = verdict(client, match_id, "saved").json()

    assert set(listed) == set(MatchSummary.model_fields) == MATCH_FIELDS
    assert set(updated) == set(listed)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"state": "new"}, id="new-is-not-a-verdict"),
        pytest.param({"state": ""}, id="empty-string"),
        pytest.param({"state": "SAVED"}, id="another-spelling"),
        pytest.param({"state": 3}, id="not-a-string"),
        pytest.param({"state": None}, id="null"),
        pytest.param({}, id="missing-field"),
    ],
)
def test_a_value_outside_the_vocabulary_is_refused_and_writes_nothing(
    client: TestClient, body: dict[str, Any]
) -> None:
    match_id = seed_match(seed_posting(seed_company()))
    before = row_values(match_id)

    response = client.patch(f"/matches/{match_id}", json=body)

    assert response.status_code == 422
    assert row_values(match_id) == before
    assert row_values(match_id)["state"] == "new"


def test_a_verdict_with_no_body_at_all_is_refused(client: TestClient) -> None:
    match_id = seed_match(seed_posting(seed_company()))

    response = client.patch(f"/matches/{match_id}")

    assert response.status_code == 422
    assert row_values(match_id)["state"] == "new"


@pytest.mark.parametrize(
    ("start", "sent"),
    [
        pytest.param("new", "saved", id="new-to-saved"),
        pytest.param("new", "dismissed", id="new-to-dismissed"),
        pytest.param("saved", "dismissed", id="saved-to-dismissed"),
        pytest.param("dismissed", "saved", id="dismissed-to-saved"),
    ],
)
def test_a_verdict_moves_state_and_nothing_else(
    client: TestClient, start: str, sent: str
) -> None:
    _, match_id = scored_match(
        score=87, gaps=["Rust"], state=start, notified_at=datetime(2024, 1, 3, 9, 0, tzinfo=UTC)
    )
    before = snapshot(match_id)
    # Non-default everywhere, so an unchanged snapshot is evidence and not an
    # empty comparison.
    assert before["score"] == 87
    assert before["gaps"] == ["Rust"]
    assert before["notified_at"] == datetime(2024, 1, 3, 9, 0, tzinfo=UTC)
    assert before["reason"] == REASON
    assert before["created_at"] is not None

    response = verdict(client, match_id, sent)

    assert response.status_code == 200
    assert response.json()["state"] == sent
    assert snapshot(match_id) == before


def test_the_same_verdict_twice_is_one_write_s_worth_of_change(client: TestClient) -> None:
    posting_id, match_id = scored_match(gaps=["Rust"])
    before = snapshot(match_id)

    first = verdict(client, match_id, "dismissed")
    second = verdict(client, match_id, "dismissed")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["state"] == "dismissed"
    assert snapshot(match_id) == before
    assert match_count() == 1, "a second verdict must not add a match row"
    assert match_id_of(posting_id) == match_id


def test_a_verdict_on_an_id_with_no_match_is_404_and_writes_nothing(client: TestClient) -> None:
    match_id = seed_match(seed_posting(seed_company()))
    assert match_count() == 1

    response = verdict(client, match_id + 1, "saved")

    assert response.status_code == 404
    assert response.json()["detail"], "a refusal must say what it refused"
    assert match_count() == 1


@pytest.mark.parametrize("value", ["abc", "1.5", "one"])
def test_a_path_that_is_not_a_match_id_is_422(client: TestClient, value: str) -> None:
    seed_match(seed_posting(seed_company()))

    response = verdict(client, value, "saved")

    assert response.status_code == 422
    assert match_count() == 1, "the path never reached the function"


def test_no_secret_no_resume_text_and_no_database_url_reach_a_body_or_a_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The marker is in the database, so its absence from every answer means something."""
    caplog.set_level(logging.DEBUG)
    assert upload_resume(client, RESUME_MARKER).status_code == 201
    match_id = seed_match(seed_posting(seed_company()))
    with db.session_scope() as session:
        stored = session.scalar(select(Resume.text).where(Resume.is_active))
    assert RESUME_MARKER in stored, "the marker must really be stored for this to prove anything"
    assert caplog.records, "a log record was made while handling this test's requests"

    read_body = read(client)
    verdict_body = verdict(client, match_id, "saved")

    assert read_body.status_code == 200
    assert verdict_body.status_code == 200
    for body in (read_body.text, verdict_body.text):
        for forbidden in (SECRET, RESUME_MARKER, os.environ["DATABASE_URL"]):
            assert forbidden not in body
    assert RESUME_MARKER not in caplog.text
    assert set(read_body.json()[0]) == MATCH_FIELDS
