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

import socket
import subprocess
from collections.abc import Iterator
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
from app.models import Company, Posting
from app.routes.watchlist import get_source
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


# The suite writes nothing into the repository.


def test_a_watchlist_write_leaves_the_working_tree_as_it_was(
    client: TestClient, source: FakeSource, tmp_path: Path
) -> None:
    before = git("status", "--short", "--untracked-files=all")

    assert add(client, "Acme Inc", "acme").status_code == 201
    assert client.delete(f"/watchlist/{companies()[0]['id']}").status_code == 204

    assert db.database_url() == f"sqlite:///{tmp_path / 'jobradar.db'}"
    assert git("status", "--short", "--untracked-files=all") == before
