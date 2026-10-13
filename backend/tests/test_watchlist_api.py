"""`/watchlist`: the routes, the probe behind the seam, and the database guard under it.

Every test drives the real app through `TestClient` -- no server, no port, no
network -- with `DATABASE_URL` pointed at a temporary SQLite file, so the suite
leaves no `backend/data/jobradar.db` behind and `git status --short` stays empty.

The source is never reached for real here. The `POST` route takes its source from
`app.routes.watchlist.get_source`, and every test replaces that provider with the
small fake below through `app.dependency_overrides`; an autouse fixture patches
`socket.socket.connect` to raise, so an implementation that ignored the seam and
called `greenhouse.fetch` itself fails instead of quietly passing. The
PostgreSQL parameter of the `engine` fixture still reaches a server, because its
driver talks to the database through libpq rather than through Python's socket
module.

Nothing here is a copy of `test_greenhouse_source.py`: the fake answers with
`SourcePosting`s of this module's own making, and the adapter's payload keys and
URL stay unknown to it.
"""

import inspect
import socket
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app import db
from app.main import app
from app.matching.scorer import FakeScorer, MatchResult
from app.models import Company, Match, Posting, Run
from app.routes.watchlist import get_source, update_company
from app.services import resume
from app.services.pipeline import run_pipeline
from app.sources import SOURCES
from app.sources.base import (
    BoardUnavailable,
    MalformedPayload,
    SourceError,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent

# The postings the fake answers a probe with: real `SourcePosting` values, so a
# probe that went on to store them would have something to store.
POSTINGS = [
    SourcePosting(
        external_id="8172510",
        title="Backend Engineer",
        location=None,
        url="https://example.test/board/1",
        description="Synthetic text written for this test.",
    )
]

# Text that must not reach a response: it stands in for whatever body a source
# sends with a refusal, which can be arbitrary HTML.
UPSTREAM_TEXT = "UPSTREAM-SENTINEL-TEXT-THAT-MUST-NOT-BE-ECHOED"

# A synthetic resume, so the walk case reaches no real one, and the alert time and
# closure a stored row has to keep exactly as they are.
RESUME_TEXT = "Synthetic resume text written for this test: Python and PostgreSQL."
CLOSED_AT = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)

# Every column of the three tables an edit must leave alone, so a write nobody
# asked for -- a `created_at` that moved, a `closed_at` that was set -- is visible.
COMPANY_COLUMNS = ("id", "name", "slug", "source", "is_active", "created_at")
POSTING_COLUMNS = (
    "id",
    "company_id",
    "external_id",
    "title",
    "location",
    "url",
    "description",
    "first_seen_at",
    "updated_at",
    "closed_at",
)
MATCH_COLUMNS = (
    "id",
    "posting_id",
    "score",
    "reason",
    "gaps",
    "state",
    "created_at",
    "notified_at",
)


class FakeSource:
    """Stands in for a board adapter: records every slug it is asked for, then answers or raises."""

    def __init__(self, *, error: SourceError | None = None) -> None:
        self.slugs: list[str] = []
        self.error = error
        self.postings = POSTINGS

    def __call__(self, slug: str) -> list[SourcePosting]:
        self.slugs.append(slug)
        if self.error is not None:
            raise self.error
        return self.postings


class BoardSource:
    """The walk's own small fake: it records every slug and answers from a script.

    A slug the case did not script is a `KeyError`, so a run that asked for a
    company it should have skipped fails here instead of passing as a board
    nobody scripted.
    """

    def __init__(self, boards: dict[str, list[SourcePosting]]) -> None:
        self.boards = boards
        self.calls: list[str] = []

    def __call__(self, slug: str) -> list[SourcePosting]:
        self.calls.append(slug)
        return self.boards[slug]


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this module reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def sources() -> Iterator[dict[str, FakeSource]]:
    """One fake per known source, installed over the route's own provider for the length of one test."""
    fakes = {"greenhouse": FakeSource(), "lever": FakeSource()}
    app.dependency_overrides[get_source] = lambda: fakes
    try:
        yield fakes
    finally:
        app.dependency_overrides.pop(get_source, None)


@pytest.fixture
def source(sources: dict[str, FakeSource]) -> FakeSource:
    """The Greenhouse fake: the source a body that does not name one is probed against."""
    return sources["greenhouse"]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app, with its database pointed at this test's temporary SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def engine_client(engine: Engine) -> Iterator[TestClient]:
    """The app over the shared `engine` fixture's database, so a row-level case runs on both engines.

    The `client` fixture above is one temporary SQLite file and is enough for a
    case about a status code or an answer body; a case whose point is that every
    column of every row is unchanged asks for this one instead, because the
    engine matrix is what proves the write means the same thing on PostgreSQL.
    """
    with TestClient(app) as test_client:
        yield test_client


def add(client: TestClient, name: str = "Acme Inc", slug: str = "acme") -> Response:
    """One addition, the way a client sends it."""
    return client.post("/watchlist", json={"name": name, "slug": slug})


def refusal(response: Response, status: int) -> str:
    """The message a refusal answered with, having checked it is a JSON body carrying one."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json()["detail"], "a refusal must say what it refused"
    return response.json()["detail"]


def companies() -> list[dict[str, Any]]:
    """Every `company` row as plain values, read through the app's own session."""
    with db.session_scope() as session:
        return [
            {
                "id": company.id,
                "name": company.name,
                "slug": company.slug,
                "is_active": company.is_active,
                "source": company.source,
            }
            for company in session.scalars(select(Company).order_by(Company.id))
        ]


def postings() -> list[Posting]:
    """Every `posting` row: the probe decides a slug and stores none of them."""
    with db.session_scope() as session:
        return list(session.scalars(select(Posting)))


def rows(model: type, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every row of one table as plain column values, in id order.

    The whole row and not the fields this API renders: a case that compares these
    before and after a request sees a write to a column the answer never carries.
    """
    with db.session_scope() as session:
        return [
            {column: getattr(row, column) for column in columns}
            for row in session.scalars(select(model).order_by(model.id))
        ]


def seed_posting(company_id: int, *, closed_at: datetime | None = None) -> None:
    """One stored posting of that company -- open or closed -- and the one match row it earned.

    The match is written the way #10 leaves a row the owner acted on, so a route
    that re-scored or reopened anything would show up in the comparison.
    """
    with db.session_scope() as session:
        posting = Posting(
            company_id=company_id,
            external_id=POSTINGS[0].external_id,
            title="Backend Engineer",
            location=None,
            url="https://example.test/board/1",
            description="Synthetic text written for this test.",
            closed_at=closed_at,
        )
        session.add(posting)
        session.flush()
        session.add(
            Match(
                posting_id=posting.id,
                score=87,
                reason="Synthetic fit.",
                gaps='["No Kubernetes."]',
                state="saved",
                notified_at=CLOSED_AT,
            )
        )


def new_run() -> int:
    """One `run` row in `running`, written the way #9's route writes it, and its id."""
    with db.session_scope() as session:
        run = Run(status="running")
        session.add(run)
        session.flush()
        return run.id


def a_result() -> MatchResult:
    """The result the fake scorer hands back, so no case needs a model or a key."""
    return MatchResult(score=87, reason="Synthetic fit.", gaps=[])


def watch(name: str, slug: str, *, is_active: bool = True, source: str = "greenhouse") -> int:
    """A company put on the watchlist without going through the route, and its id."""
    with db.session_scope() as session:
        company = Company(name=name, slug=slug, is_active=is_active, source=source)
        session.add(company)
        session.flush()
        return company.id


def git(*arguments: str) -> str:
    """Run git in the repository the suite lives in."""
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


# The seam: one named provider, so no test has to guess how to stand in for the source.


def test_the_source_seam_is_the_source_mapping_behind_a_zero_argument_provider() -> None:
    """One provider, resolving a stored name to that board's adapter and nothing else."""
    assert get_source() is SOURCES
    assert sorted(get_source()) == ["greenhouse", "lever"]


def test_listing_and_deleting_never_ask_the_source(client: TestClient, source: FakeSource) -> None:
    company_id = watch("Acme Inc", "acme")

    assert client.get("/watchlist").status_code == 200
    assert client.delete(f"/watchlist/{company_id}").status_code == 204

    assert source.slugs == []


# GET: every company, its slug and its active state.


def test_an_empty_watchlist_is_an_empty_list(client: TestClient) -> None:
    response = client.get("/watchlist")

    assert response.status_code == 200
    assert response.json() == []


def test_the_watchlist_lists_every_company_with_its_slug_and_active_state(
    client: TestClient,
) -> None:
    """The inactive row is listed as inactive: an implementation hard-coding `true` fails."""
    first = watch("Acme Inc", "acme")
    second = watch("  Globex  ", "  Globex  ", is_active=False)

    response = client.get("/watchlist")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": first,
            "name": "Acme Inc",
            "slug": "acme",
            "is_active": True,
            "source": "greenhouse",
        },
        {
            "id": second,
            "name": "  Globex  ",
            "slug": "globex",
            "is_active": False,
            "source": "greenhouse",
        },
    ]


# POST: one company, once the board answered and only if the slug is new.


def test_posted_company_is_created_and_returned_as_the_row_it_added(
    client: TestClient, source: FakeSource
) -> None:
    response = add(client, "Acme Inc", "acme")

    assert response.status_code == 201
    assert len(source.postings) == 1  # the probe really was answered with a posting
    (row,) = companies()
    assert response.json() == row
    assert row["name"] == "Acme Inc"
    assert row["slug"] == "acme"
    assert row["is_active"] is True
    assert row["source"] == "greenhouse"  # a body with no source stores Greenhouse
    assert source.slugs == ["acme"]  # asked exactly once, for the slug that was stored
    assert postings() == []  # the probe decides the slug; #6 is what stores postings


def test_an_addition_writes_one_row_and_changes_no_other(
    client: TestClient, source: FakeSource
) -> None:
    existing = watch("Already Watched Ltd", "other")
    before = companies()

    assert add(client, "Acme Inc", "acme").status_code == 201

    stored = companies()
    assert len(stored) == 2
    assert stored[0] == before[0]  # the row that was there is untouched
    assert stored[1]["id"] != existing


def test_a_slug_is_normalised_before_it_is_probed_and_stored(
    client: TestClient, source: FakeSource
) -> None:
    """`"  ACME  "` is one slug with `"acme"`; the same spelling in `name` is left alone."""
    response = add(client, "  Acme  ", "  ACME  ")

    assert response.status_code == 201
    assert response.json()["slug"] == "acme"
    assert source.slugs == ["acme"]
    (row,) = companies()
    assert row == response.json()


def test_a_second_post_of_a_watched_slug_is_refused_with_409(
    client: TestClient, source: FakeSource
) -> None:
    assert add(client, "Acme Inc", "acme").status_code == 201

    message = refusal(add(client, "Acme Again", "acme"), 409)

    assert "acme" in message  # the refusal names the slug, so it is fixable
    assert companies() == [
        {
            "id": 1,
            "name": "Acme Inc",
            "slug": "acme",
            "is_active": True,
            "source": "greenhouse",
        }
    ]


def test_a_second_post_spelled_differently_normalises_onto_the_watched_slug(
    client: TestClient, source: FakeSource
) -> None:
    """A route comparing raw strings would add a second row here."""
    add(client, "Acme Inc", "acme")

    refusal(add(client, "Acme Again", "  ACME  "), 409)

    assert len(companies()) == 1


# The source a body names: probed through that adapter, stored, and reported back.


def test_a_body_naming_lever_is_probed_stored_and_reported_through_that_adapter(
    client: TestClient, sources: dict[str, FakeSource]
) -> None:
    response = client.post(
        "/watchlist", json={"name": "Acme Inc", "slug": "acme", "source": "lever"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["source"] == "lever"
    assert sources["lever"].slugs == ["acme"]
    assert sources["greenhouse"].slugs == []  # the other board is never asked
    assert companies()[0]["source"] == "lever"


def test_the_listed_source_is_what_each_row_stores(
    client: TestClient, sources: dict[str, FakeSource]
) -> None:
    watch("Globex", "globex", source="lever")
    watch("Acme Inc", "acme")

    assert [row["source"] for row in client.get("/watchlist").json()] == [
        "lever",
        "greenhouse",
    ]


@pytest.mark.parametrize("named", ["workday", "ashby", "Lever", "LEVER", "greenhouse "])
def test_a_body_naming_an_unknown_source_is_422_and_makes_no_request(
    client: TestClient, sources: dict[str, FakeSource], named: str
) -> None:
    response = client.post(
        "/watchlist", json={"name": "Acme Inc", "slug": "acme", "source": named}
    )

    assert response.status_code == 422, response.text
    assert sources["greenhouse"].slugs == []
    assert sources["lever"].slugs == []
    assert companies() == []


# The database guard under that 409, on both engines the suite runs on.


def test_the_route_write_path_is_guarded_by_the_unique_slug_on_every_engine(
    engine: Engine,
) -> None:
    """The 409 has the unique index under it, and not only a handler check.

    The rows go in the way the route writes them, through `Company(...)`, so the
    model's normaliser runs and the two spellings of one slug collide here as
    they do on the route. Nothing is filtered in Python: the refusal read back is
    the engine's own `IntegrityError`.
    """
    with db.session_scope() as session:
        session.add(Company(name="Acme Inc", slug="acme"))

    with pytest.raises(IntegrityError):
        with db.session_scope() as session:
            session.add(Company(name="Acme Again", slug="acme"))

    with pytest.raises(IntegrityError):
        with db.session_scope() as session:
            session.add(Company(name="Acme Third", slug="  ACME  "))

    with db.session_scope() as session:
        assert [company.slug for company in session.scalars(select(Company))] == ["acme"]


def test_the_database_refuses_a_source_outside_the_vocabulary(engine: Engine) -> None:
    """`workday` is not a value the check accepts, so it is an `IntegrityError` and not a row."""
    with pytest.raises(IntegrityError):
        with db.session_scope() as session:
            session.add(Company(name="Acme", slug="acme", source="workday"))

    with db.session_scope() as session:
        assert session.scalars(select(Company)).all() == []


# The probe: a board that is not there, and a board that cannot be read.


def test_a_slug_no_board_answers_to_is_refused_with_404(
    client: TestClient, source: FakeSource
) -> None:
    source.error = UnknownSlug(f"There is no board named 'nope': {UPSTREAM_TEXT}")
    assert type(source.error) is UnknownSlug  # the class under test, not the family's parent

    message = refusal(add(client, "Nope Inc", "nope"), 404)

    assert "nope" in message
    assert "does not exist" in message
    assert UPSTREAM_TEXT not in message
    assert source.slugs == ["nope"]
    assert companies() == []


@pytest.mark.parametrize(
    "error",
    [
        BoardUnavailable(f"HTTP 503: {UPSTREAM_TEXT}"),
        SourceTimeout(f"the board did not answer: {UPSTREAM_TEXT}"),
        MalformedPayload(f"the answer was not a board: {UPSTREAM_TEXT}"),
    ],
    ids=["a board that refuses", "a board that is slow", "a malformed payload"],
)
def test_a_board_that_cannot_be_read_is_502_and_creates_no_row(
    client: TestClient, source: FakeSource, error: SourceError
) -> None:
    """A board that is down is never a board that does not exist."""
    source.error = error
    assert type(source.error) is type(error)  # each case is its own class, not the parent

    response = add(client, "Acme Inc", "acme")

    message = refusal(response, 502)
    assert "acme" in message
    assert "could not be read" in message
    assert UPSTREAM_TEXT not in response.text  # none of the upstream body comes back
    assert source.slugs == ["acme"]
    assert companies() == []


def test_a_lever_slug_no_board_answers_to_is_refused_with_404(
    client: TestClient, sources: dict[str, FakeSource]
) -> None:
    sources["lever"].error = UnknownSlug("There is no board named 'nope'")

    response = client.post(
        "/watchlist", json={"name": "Nope Inc", "slug": "nope", "source": "lever"}
    )

    message = refusal(response, 404)
    assert "nope" in message
    assert sources["lever"].slugs == ["nope"]
    assert sources["greenhouse"].slugs == []
    assert companies() == []


@pytest.mark.parametrize(
    "error",
    [
        BoardUnavailable("The board refused to answer: HTTP 503"),
        SourceTimeout("the board did not answer"),
        MalformedPayload("the answer was not a board"),
    ],
    ids=["a board that refuses", "a board that is slow", "a malformed payload"],
)
def test_a_lever_board_that_cannot_be_read_is_502_and_creates_no_row(
    client: TestClient, sources: dict[str, FakeSource], error: SourceError
) -> None:
    sources["lever"].error = error

    response = client.post(
        "/watchlist", json={"name": "Acme Inc", "slug": "acme", "source": "lever"}
    )

    message = refusal(response, 502)
    assert "acme" in message
    assert sources["lever"].slugs == ["acme"]
    assert sources["greenhouse"].slugs == []
    assert companies() == []


# A body the route cannot use: refused before the source is asked, and nothing written.


_UNUSABLE_BODIES = [
    pytest.param({}, id="no name and no slug"),
    pytest.param({"slug": "acme"}, id="no name"),
    pytest.param({"name": "Acme Inc"}, id="no slug"),
    pytest.param({"name": "", "slug": "acme"}, id="an empty name"),
    pytest.param({"name": "   ", "slug": "acme"}, id="a whitespace name"),
    pytest.param({"name": "Acme Inc", "slug": ""}, id="an empty slug"),
    pytest.param({"name": "Acme Inc", "slug": "   "}, id="a whitespace slug"),
    pytest.param({"name": "Acme Inc", "slug": 5}, id="a slug that is not a string"),
    pytest.param({"name": 5, "slug": "acme"}, id="a name that is not a string"),
    pytest.param([{"name": "Acme Inc", "slug": "acme"}], id="a body that is a list"),
    pytest.param("Acme Inc", id="a body that is a string"),
    pytest.param(None, id="a body that is null"),
]


@pytest.mark.parametrize("body", _UNUSABLE_BODIES)
def test_a_body_the_route_cannot_use_is_422_and_creates_no_row(
    client: TestClient, source: FakeSource, body: object
) -> None:
    response = client.post("/watchlist", json=body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]
    assert source.slugs == []  # a body that cannot be used never reaches the source
    assert companies() == []


def test_a_request_carrying_no_body_at_all_is_422(client: TestClient, source: FakeSource) -> None:
    response = client.post("/watchlist")

    assert response.status_code == 422, response.text
    assert source.slugs == []
    assert companies() == []


# DELETE: exactly that company, and a clear 404 when there is none.


def test_deleting_a_company_removes_exactly_that_one(
    client: TestClient, source: FakeSource
) -> None:
    first = watch("Acme Inc", "acme")
    second = watch("Globex", "globex")

    response = client.delete(f"/watchlist/{first}")

    assert response.status_code == 204
    assert response.content == b""
    assert [row["id"] for row in companies()] == [second]
    assert [row["id"] for row in client.get("/watchlist").json()] == [second]


def test_deleting_an_id_that_never_existed_is_404_and_deletes_nothing(
    client: TestClient,
) -> None:
    kept = watch("Acme Inc", "acme")

    message = refusal(client.delete("/watchlist/999999"), 404)

    assert "999999" in message
    assert [row["id"] for row in companies()] == [kept]


def test_deleting_the_same_id_twice_answers_204_then_404(client: TestClient) -> None:
    company_id = watch("Acme Inc", "acme")

    first = client.delete(f"/watchlist/{company_id}")
    second = client.delete(f"/watchlist/{company_id}")

    assert (first.status_code, first.content) == (204, b"")
    refusal(second, 404)
    assert companies() == []


def test_an_id_that_is_not_an_integer_is_refused_by_the_path_as_422(
    client: TestClient,
) -> None:
    """`/watchlist/abc` is the path's own validation, not a server error."""
    kept = watch("Acme Inc", "acme")

    response = client.delete("/watchlist/abc")

    assert response.status_code == 422, response.text
    assert [row["id"] for row in companies()] == [kept]


# PATCH: a rename, the poll switch, and nothing else. No board is asked on this path.


def test_an_edit_answers_the_company_the_list_carries(client: TestClient) -> None:
    """Five fields and no sixth: the row read back, and the object `GET /watchlist` lists."""
    company_id = watch("Acme Inc", "acme")

    response = client.patch(f"/watchlist/{company_id}", json={"name": "Acme Corporation"})

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"id", "name", "slug", "is_active", "source"}
    assert response.json()["name"] == "Acme Corporation"
    assert response.json() == companies()[0] == client.get("/watchlist").json()[0]


def test_a_body_naming_both_fields_writes_both_in_one_answer(client: TestClient) -> None:
    company_id = watch("Acme Inc", "acme")

    response = client.patch(
        f"/watchlist/{company_id}", json={"name": "Acme Corporation", "is_active": False}
    )

    assert response.status_code == 200, response.text
    assert (response.json()["name"], response.json()["is_active"]) == ("Acme Corporation", False)
    assert response.json() == companies()[0]
    assert len(companies()) == 1  # one write, and no second row


def test_the_route_takes_the_id_and_the_body_and_asks_no_board() -> None:
    """No `Depends`, so no adapter and no probe: the `get_source()` seam stays #5's own."""
    assert list(inspect.signature(update_company).parameters) == ["id", "body"]


# A rename: the name, and every other column of every row left exactly as it was.


def test_a_rename_changes_the_name_and_nothing_else(engine_client: TestClient) -> None:
    company_id = watch("Acme Inc", "acme")
    watch("Globex", "globex", source="lever")
    seed_posting(company_id)
    before = (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    )

    response = engine_client.patch(f"/watchlist/{company_id}", json={"name": "Acme Corporation"})

    assert response.status_code == 200, response.text
    renamed = rows(Company, COMPANY_COLUMNS)
    assert renamed == [
        {**row, "name": "Acme Corporation"} if row["id"] == company_id else row
        for row in before[0]
    ]
    assert len(renamed) == len(before[0])  # nothing was created or deleted
    row = next(row for row in renamed if row["id"] == company_id)
    assert (row["slug"], row["source"], row["is_active"]) == ("acme", "greenhouse", True)
    assert rows(Posting, POSTING_COLUMNS) == before[1]
    assert rows(Match, MATCH_COLUMNS) == before[2]


def test_a_name_is_stored_and_answered_exactly_as_it_was_sent(client: TestClient) -> None:
    """No normaliser runs on a label: the slug has one, and this is not a slug."""
    company_id = watch("Acme Inc", "acme")

    response = client.patch(f"/watchlist/{company_id}", json={"name": "  Acme  "})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "  Acme  "
    assert companies()[0]["name"] == "  Acme  "


def test_renaming_a_company_that_is_already_inactive_leaves_it_inactive(
    client: TestClient,
) -> None:
    company_id = watch("Acme Inc", "acme", is_active=False)

    response = client.patch(f"/watchlist/{company_id}", json={"name": "Acme Corporation"})

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert response.json() == companies()[0]


def test_the_same_rename_twice_answers_the_same_body_and_leaves_one_row(
    client: TestClient,
) -> None:
    company_id = watch("Acme Inc", "acme")

    first = client.patch(f"/watchlist/{company_id}", json={"name": "Acme Corporation"})
    second = client.patch(f"/watchlist/{company_id}", json={"name": "Acme Corporation"})

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert len(companies()) == 1


# The board is not editable here, and this route asks no board.

_UNEDITABLE_BODIES = [
    pytest.param({"slug": "other"}, id="a slug on its own"),
    pytest.param({"source": "lever"}, id="a source on its own"),
    pytest.param({"name": "Acme Corporation", "slug": "other"}, id="a rename with a slug"),
]


@pytest.mark.parametrize("body", _UNEDITABLE_BODIES)
def test_the_board_is_not_editable_here_and_no_board_is_asked(
    engine_client: TestClient, source: FakeSource, body: object
) -> None:
    company_id = watch("Acme Inc", "acme")
    before = rows(Company, COMPANY_COLUMNS)

    response = engine_client.patch(f"/watchlist/{company_id}", json=body)

    refusal(response, 422)
    assert rows(Company, COMPANY_COLUMNS) == before
    assert source.slugs == []  # no re-probe, so no 409 and no 502 either


# The poll switch: the same route for both values, and the list keeps showing the row.


def test_deactivating_answers_the_company_and_the_list_still_lists_it(
    client: TestClient,
) -> None:
    first = watch("Acme Inc", "acme")
    second = watch("Globex", "globex")

    response = client.patch(f"/watchlist/{first}", json={"is_active": False})

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    listed = client.get("/watchlist").json()
    assert listed == [
        {
            "id": first,
            "name": "Acme Inc",
            "slug": "acme",
            "is_active": False,
            "source": "greenhouse",
        },
        {
            "id": second,
            "name": "Globex",
            "slug": "globex",
            "is_active": True,
            "source": "greenhouse",
        },
    ]
    assert response.json() == listed[0]


def test_reactivating_is_the_same_route_and_answers_an_active_company(
    client: TestClient,
) -> None:
    company_id = watch("Acme Inc", "acme", is_active=False)

    response = client.patch(f"/watchlist/{company_id}", json={"is_active": True})

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True
    assert client.get("/watchlist").json()[0]["is_active"] is True
    assert len(companies()) == 1


def test_the_same_poll_state_twice_answers_the_same_body_and_creates_nothing(
    client: TestClient,
) -> None:
    company_id = watch("Acme Inc", "acme")

    first = client.patch(f"/watchlist/{company_id}", json={"is_active": False})
    second = client.patch(f"/watchlist/{company_id}", json={"is_active": False})

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert len(companies()) == 1


# Deactivation is not a deletion: the row, its postings and their matches stay.


@pytest.mark.parametrize(
    "closed_at",
    [None, CLOSED_AT],
    ids=["an open posting", "a posting closed by the source"],
)
def test_deactivating_and_reactivating_disturb_nothing_else(
    engine_client: TestClient, closed_at: datetime | None
) -> None:
    """Every column of `company`, `posting` and `match`, before and after both requests."""
    company_id = watch("Acme Inc", "acme")
    seed_posting(company_id, closed_at=closed_at)
    before = (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    )

    stopped = engine_client.patch(f"/watchlist/{company_id}", json={"is_active": False})
    started = engine_client.patch(f"/watchlist/{company_id}", json={"is_active": True})

    assert (stopped.status_code, started.status_code) == (200, 200)
    assert len(before[1]) == len(before[2]) == 1  # the case is not vacuous
    assert rows(Posting, POSTING_COLUMNS) == before[1]
    assert rows(Match, MATCH_COLUMNS) == before[2]
    assert rows(Company, COMPANY_COLUMNS) == before[0]


# The walk: `is_active` is what takes a company out of the poll and nothing else does.


def test_a_run_asks_the_active_company_only_and_leaves_the_other_s_postings_alone(
    engine_client: TestClient,
) -> None:
    watch("Acme Inc", "acme")
    stopped = watch("Globex", "globex")
    seed_posting(stopped)  # an open posting the run that skips the board must not touch
    assert engine_client.patch(f"/watchlist/{stopped}", json={"is_active": False}).status_code == 200
    resume.record_active_resume(
        "synthetic-resume.txt", resume.TEXT_PLAIN, RESUME_TEXT, Path("/tmp/synthetic-resume.txt")
    )
    source = BoardSource({"acme": POSTINGS})

    run_pipeline(new_run(), sources={"greenhouse": source}, scorer=FakeScorer(a_result()))

    assert source.calls == ["acme"]
    (stored,) = [row for row in rows(Posting, POSTING_COLUMNS) if row["company_id"] == stopped]
    assert stored["closed_at"] is None  # the board it was not fetched from left it open


def test_reactivating_a_company_puts_its_board_back_in_the_walk_in_id_order(
    engine_client: TestClient,
) -> None:
    watch("Acme Inc", "acme")
    company_id = watch("Globex", "globex")
    assert engine_client.patch(f"/watchlist/{company_id}", json={"is_active": False}).status_code == 200
    resume.record_active_resume(
        "synthetic-resume.txt", resume.TEXT_PLAIN, RESUME_TEXT, Path("/tmp/synthetic-resume.txt")
    )
    skipped = BoardSource({"acme": POSTINGS})
    run_pipeline(new_run(), sources={"greenhouse": skipped}, scorer=FakeScorer(a_result()))
    assert skipped.calls == ["acme"]
    assert engine_client.patch(f"/watchlist/{company_id}", json={"is_active": True}).status_code == 200
    source = BoardSource({"acme": POSTINGS, "globex": POSTINGS})

    run_pipeline(new_run(), sources={"greenhouse": source}, scorer=FakeScorer(a_result()))

    assert source.calls == ["acme", "globex"]  # both, in ascending id order


# A body this route cannot use: refused before any write, and nothing written.

_UNUSABLE_EDITS = [
    pytest.param({}, id="no field at all"),
    pytest.param({"name": None}, id="a null name"),
    pytest.param({"is_active": None}, id="a null is_active"),
    pytest.param({"name": ""}, id="an empty name"),
    pytest.param({"name": "   "}, id="a whitespace name"),
    pytest.param({"name": 5}, id="a name that is not a string"),
    # The length the `company.name` column holds is 255: one over it is refused
    # here, because PostgreSQL would refuse the write with a driver error -- a
    # `500` -- and the route owes `422`.
    pytest.param({"name": "A" * 256}, id="a name one over the column"),
    pytest.param({"name": "A" * 300}, id="a name far over the column"),
    # A NUL is the one character a PostgreSQL text column cannot store, and a
    # control character is no part of a label either.
    pytest.param({"name": "Ac\x00me"}, id="a name carrying a NUL"),
    pytest.param({"name": "Ac\x01me"}, id="a name carrying a control character"),
    pytest.param({"is_active": "false"}, id="the string 'false'"),
    pytest.param({"is_active": "yes"}, id="the string 'yes'"),
    pytest.param([{"name": "Acme Corporation"}], id="a body that is a list"),
    pytest.param("Acme Corporation", id="a body that is a string"),
    pytest.param(None, id="a body that is null"),
]


@pytest.mark.parametrize("body", _UNUSABLE_EDITS)
def test_an_edit_body_the_route_cannot_use_is_422_and_writes_nothing(
    engine_client: TestClient, source: FakeSource, body: object
) -> None:
    company_id = watch("Acme Inc", "acme")
    seed_posting(company_id)
    before = (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    )

    response = engine_client.patch(f"/watchlist/{company_id}", json=body)

    refusal(response, 422)
    assert (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    ) == before
    assert source.slugs == []


def test_a_name_at_the_column_s_length_is_accepted_and_stored_as_sent(
    engine_client: TestClient,
) -> None:
    """255 characters is what `company.name` holds: PostgreSQL stores it, 256 does not."""
    company_id = watch("Acme Inc", "acme")
    seed_posting(company_id)
    before = (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    )
    name = "A" * 255

    response = engine_client.patch(f"/watchlist/{company_id}", json={"name": name})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == name
    renamed = rows(Company, COMPANY_COLUMNS)
    assert renamed == [
        {**row, "name": name} if row["id"] == company_id else row for row in before[0]
    ]
    assert (rows(Posting, POSTING_COLUMNS), rows(Match, MATCH_COLUMNS)) == before[1:]


def test_an_edit_request_carrying_no_body_at_all_is_422(
    engine_client: TestClient, source: FakeSource
) -> None:
    company_id = watch("Acme Inc", "acme")
    before = rows(Company, COMPANY_COLUMNS)

    response = engine_client.patch(f"/watchlist/{company_id}")

    refusal(response, 422)
    assert rows(Company, COMPANY_COLUMNS) == before
    assert source.slugs == []


# The path: an id no row has, and a value that is not an id at all.


def test_editing_an_id_that_never_existed_is_404_and_writes_nothing(
    engine_client: TestClient, source: FakeSource
) -> None:
    """`TestClient` re-raises a server error, so a `500` here fails the case rather than passing it."""
    kept = watch("Acme Inc", "acme")
    seed_posting(kept)
    before = (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    )

    message = refusal(
        engine_client.patch("/watchlist/999999", json={"name": "Acme Corporation"}), 404
    )

    assert "999999" in message  # the refusal names the id, so it is fixable
    assert (
        rows(Company, COMPANY_COLUMNS),
        rows(Posting, POSTING_COLUMNS),
        rows(Match, MATCH_COLUMNS),
    ) == before
    assert source.slugs == []


def test_an_edit_path_that_is_not_an_integer_is_refused_by_the_path_as_422(
    engine_client: TestClient, source: FakeSource
) -> None:
    kept = watch("Acme Inc", "acme")

    refusal(engine_client.patch("/watchlist/abc", json={"name": "Acme Corporation"}), 422)

    assert [row["id"] for row in companies()] == [kept]
    assert source.slugs == []


# The suite writes nothing into the repository.


def test_a_watchlist_write_leaves_the_working_tree_as_it_was(
    client: TestClient, source: FakeSource, tmp_path: Path
) -> None:
    before = git("status", "--short", "--untracked-files=all")

    assert add(client, "Acme Inc", "acme").status_code == 201
    assert client.delete(f"/watchlist/{companies()[0]['id']}").status_code == 204

    assert db.database_url() == f"sqlite:///{tmp_path / 'jobradar.db'}"
    assert git("status", "--short", "--untracked-files=all") == before
