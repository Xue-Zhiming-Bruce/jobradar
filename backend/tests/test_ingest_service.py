"""What one call to the ingest service does to the `posting` table, on both engines.

Every case here asks for the shared `engine` fixture of `tests/conftest.py`, so it
runs once on SQLite and once on PostgreSQL, and that fixture is also what points
the app at this test's own database: nothing here names an engine, a dialect or a
URL, and no case leaves a database behind in the checkout.

The postings are built here, as `SourcePosting` values: no board is fetched, no
fixture file is read, and an autouse fixture patches `socket.socket.connect` to
raise the way `test_greenhouse_source.py` does, so a first call is the only way a
posting reaches the table and an implementation that reached for the network
fails inside the test.

The subject is one call: what it inserts, what it reports, and what it leaves
alone. "Leaves alone" is checked column for column rather than by row count --
`_stored` returns every column of a company's postings as plain values -- because
the table's own `updated_at` carries `onupdate`, so a run that rewrites an
unchanged row is visible here and a run that does nothing is too. A `match` row is
inserted in one case, through `session_scope()` like the app's own writes, only to
prove the alert state survives a close and a reopen; #7 and #8 own every real one.
"""

import ast
import inspect
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Company, Match, Posting
from app.services.ingest import IngestResult, ingest_postings
from app.sources.greenhouse import SourcePosting

BACKEND_ROOT = Path(__file__).resolve().parent.parent
INGEST_MODULE = BACKEND_ROOT / "app" / "services" / "ingest.py"
INGEST_SOURCE = INGEST_MODULE.read_text(encoding="utf-8")

# Every column of `posting`, so a case can compare a whole row without naming
# them one by one, and a later change to the table shows up as a failure here.
STORED_COLUMNS = (
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

# The modules an HTTP client would come from, and the payload keys only the
# adapter may know: `test_greenhouse_source.py` scans `app/` for the keys, and
# this module scans the one file it added for both.
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}
PAYLOAD_TERMS = ("boards-api.greenhouse.io", "internal_job_id", "absolute_url", "gh_jid", "slug")


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service stores what it is given: a socket opened here fails the test."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the ingest service tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _company(slug: str = "acme") -> int:
    """One watchlist company, flushed so its id can name the postings under it."""
    with db.session_scope() as session:
        company = Company(name=slug.capitalize(), slug=slug)
        session.add(company)
        session.flush()
        return company.id


def _posting(external_id: str = "1", **overrides: object) -> SourcePosting:
    """A posting as the adapter would hand it over: synthetic, and this module's own."""
    fields: dict[str, object] = {
        "external_id": external_id,
        "title": f"Backend Engineer {external_id}",
        "location": "Berlin",
        "url": f"https://example.test/{external_id}",
        "description": f"Synthetic description for {external_id}.",
    }
    fields.update(overrides)
    return SourcePosting(**fields)


def _stored(company_id: int) -> dict[str, dict[str, object]]:
    """Every stored posting of one company, by external id, as plain column values.

    Read inside a session and returned as values rather than rows: a row handed
    out of `session_scope()` is expired by its commit, and touching it afterwards
    raises instead of proving anything.
    """
    with db.session_scope() as session:
        return {
            row.external_id: {column: getattr(row, column) for column in STORED_COLUMNS}
            for row in session.scalars(
                select(Posting).where(Posting.company_id == company_id).order_by(Posting.id)
            )
        }


def _posting_count() -> int:
    """How many `posting` rows exist at all, so a stray insert cannot hide."""
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(Posting))


def _assert_aware_utc(instant: datetime, label: str) -> None:
    """The instant came back aware and in UTC, with nothing here repairing it."""
    assert instant.tzinfo is not None, label
    assert instant.utcoffset() == timedelta(0), label


def test_one_call_stores_one_row_per_posting_the_payload_carries(engine: Engine) -> None:
    company_id = _company()

    result = ingest_postings(company_id, [_posting("1"), _posting("2")])

    stored = _stored(company_id)
    assert set(stored) == {"1", "2"}
    assert _posting_count() == 2
    assert result.created_ids == [stored["1"]["id"], stored["2"]["id"]]
    assert result.reopened_ids == []
    assert stored["1"]["company_id"] == company_id
    assert (
        stored["1"]["title"],
        stored["1"]["location"],
        stored["1"]["url"],
        stored["1"]["description"],
    ) == ("Backend Engineer 1", "Berlin", "https://example.test/1", "Synthetic description for 1.")
    assert stored["1"]["closed_at"] is None
    assert stored["1"]["first_seen_at"] <= stored["1"]["updated_at"]
    _assert_aware_utc(stored["1"]["first_seen_at"], "first_seen_at")
    _assert_aware_utc(stored["1"]["updated_at"], "updated_at")
    assert datetime.now(UTC) - stored["1"]["first_seen_at"] < timedelta(minutes=1)


def test_a_posting_without_a_location_or_a_description_stores_null(engine: Engine) -> None:
    """Absence is stored as SQL `NULL`, not as an empty string."""
    company_id = _company()

    ingest_postings(company_id, [_posting("1", location=None, description=None)])

    stored = _stored(company_id)["1"]
    assert stored["location"] is None
    assert stored["description"] is None


def test_a_company_that_does_not_exist_is_refused_and_leaves_no_posting_behind(
    engine: Engine,
) -> None:
    """The foreign key refuses the call, the caller sees it, and the write is rolled back."""
    with pytest.raises(IntegrityError):
        ingest_postings(999_999, [_posting("1")])

    assert _posting_count() == 0


def test_a_repeat_call_with_the_same_payload_changes_no_column_and_reports_nothing(
    engine: Engine,
) -> None:
    """The identity is the pair, and a repeat is a no-op down to `updated_at`."""
    company_id = _company()
    first = ingest_postings(company_id, [_posting("1"), _posting("2")])
    before = _stored(company_id)

    again = ingest_postings(company_id, [_posting("1"), _posting("2")])

    assert again == IngestResult(created_ids=[], reopened_ids=[])
    assert _stored(company_id) == before
    assert _posting_count() == 2
    assert first.created_ids == [before["1"]["id"], before["2"]["id"]]


def test_the_same_external_id_under_another_company_is_a_new_row(engine: Engine) -> None:
    """Keyed on the pair: the source's id alone would collapse two companies' postings."""
    acme = _company("acme")
    other = _company("other")
    acme_result = ingest_postings(acme, [_posting("1")])
    before = _stored(acme)["1"]

    other_result = ingest_postings(other, [_posting("1", title="A different job")])

    assert other_result.created_ids != acme_result.created_ids
    assert other_result.created_ids == [_stored(other)["1"]["id"]]
    after = _stored(acme)["1"]
    assert after == before
    assert after["title"] == "Backend Engineer 1"
    assert _stored(other)["1"]["title"] == "A different job"
    assert _posting_count() == 2


def test_a_posting_that_is_already_closed_is_not_re_stamped(engine: Engine) -> None:
    """A close is written once: a later run that still omits the posting leaves it alone."""
    company_id = _company()
    ingest_postings(company_id, [_posting("X"), _posting("Y")])
    closing = ingest_postings(company_id, [_posting("X")])
    after_close = _stored(company_id)
    assert after_close["Y"]["closed_at"] is not None

    third = ingest_postings(company_id, [_posting("X")])

    assert third == IngestResult(created_ids=[], reopened_ids=[])
    assert _stored(company_id) == after_close
    assert closing == IngestResult(created_ids=[], reopened_ids=[])
    assert _stored(company_id)["Y"]["closed_at"] == after_close["Y"]["closed_at"]


def test_a_changed_title_is_written_in_place_and_leaves_the_rest_alone(engine: Engine) -> None:
    company_id = _company()
    ingest_postings(company_id, [_posting("1")])
    before = _stored(company_id)["1"]

    result = ingest_postings(company_id, [_posting("1", title="Staff Engineer")])

    after = _stored(company_id)["1"]
    assert result == IngestResult(created_ids=[], reopened_ids=[])
    assert _posting_count() == 1
    assert after["id"] == before["id"]
    assert after["company_id"] == before["company_id"]
    assert after["external_id"] == before["external_id"]
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["title"] == "Staff Engineer"
    assert after["location"] == before["location"]
    assert after["url"] == before["url"]
    assert after["description"] == before["description"]
    assert after["closed_at"] is None
    assert after["updated_at"] > after["first_seen_at"]


def test_a_changed_description_is_written_in_place_and_leaves_the_title_alone(
    engine: Engine,
) -> None:
    """The other half of the rule: the four source-owned columns move one by one."""
    company_id = _company()
    ingest_postings(company_id, [_posting("1")])
    before = _stored(company_id)["1"]

    ingest_postings(company_id, [_posting("1", description="Rewritten for a new team.")])

    after = _stored(company_id)["1"]
    assert after["description"] == "Rewritten for a new team."
    assert after["title"] == before["title"]
    assert after["location"] == before["location"]
    assert after["url"] == before["url"]
    assert after["id"] == before["id"]
    assert after["updated_at"] > before["updated_at"]


def test_a_value_the_payload_no_longer_carries_is_written_as_null(engine: Engine) -> None:
    """A dropped location is `NULL` afterwards: a writer of truthy values only fails here."""
    company_id = _company()
    ingest_postings(company_id, [_posting("1", location="Berlin")])
    assert _stored(company_id)["1"]["location"] == "Berlin"

    ingest_postings(company_id, [_posting("1", location=None)])

    assert _stored(company_id)["1"]["location"] is None


def test_a_posting_the_board_no_longer_lists_is_closed_in_place(engine: Engine) -> None:
    """Closed, never deleted: the row, its id and its first-seen time stay."""
    company_id = _company()
    ingest_postings(company_id, [_posting("X"), _posting("Y")])
    before = _stored(company_id)

    result = ingest_postings(company_id, [_posting("X")])

    after = _stored(company_id)
    assert result == IngestResult(created_ids=[], reopened_ids=[])
    assert set(after) == {"X", "Y"}
    assert after["X"] == before["X"]
    assert after["Y"]["id"] == before["Y"]["id"]
    assert after["Y"]["title"] == before["Y"]["title"]
    assert after["Y"]["url"] == before["Y"]["url"]
    assert after["Y"]["description"] == before["Y"]["description"]
    assert after["Y"]["first_seen_at"] == before["Y"]["first_seen_at"]
    assert after["Y"]["closed_at"] is not None
    _assert_aware_utc(after["Y"]["closed_at"], "closed_at")
    assert after["Y"]["closed_at"] > after["Y"]["first_seen_at"]


def test_a_close_is_scoped_to_the_company_fetched(engine: Engine) -> None:
    """Another company's posting is not this company's business, whatever the payload says."""
    acme = _company("acme")
    other = _company("other")
    ingest_postings(acme, [_posting("X"), _posting("Y")])
    ingest_postings(other, [_posting("X")])

    ingest_postings(acme, [])

    assert _stored(acme)["Y"]["closed_at"] is not None
    assert set(_stored(other)) == {"X"}
    assert _stored(other)["X"]["closed_at"] is None
    assert _posting_count() == 3


def test_an_empty_payload_closes_every_open_posting_of_that_company(engine: Engine) -> None:
    """The payload for a company is the truth for that company, and nothing raises."""
    company_id = _company()
    ingest_postings(company_id, [_posting("1"), _posting("2")])

    result = ingest_postings(company_id, [])

    assert result == IngestResult(created_ids=[], reopened_ids=[])
    stored = _stored(company_id)
    assert len(stored) == 2
    assert all(row["closed_at"] is not None for row in stored.values())


def test_a_closed_posting_that_reappears_is_reopened_on_the_same_row(engine: Engine) -> None:
    """Reopening clears `closed_at` on the row that is there: it is not re-inserted."""
    company_id = _company()
    created = ingest_postings(company_id, [_posting("Y")])
    original = _stored(company_id)["Y"]
    ingest_postings(company_id, [])
    assert _stored(company_id)["Y"]["closed_at"] is not None

    result = ingest_postings(
        company_id,
        [_posting("Y", title="Reopened Engineer", description="Listed again this week.")],
    )

    reopened = _stored(company_id)["Y"]
    assert result.created_ids == []
    assert result.reopened_ids == [created.created_ids[0]]
    assert _posting_count() == 1
    assert reopened["id"] == original["id"]
    assert reopened["first_seen_at"] == original["first_seen_at"]
    assert reopened["closed_at"] is None
    assert reopened["title"] == "Reopened Engineer"
    assert reopened["description"] == "Listed again this week."


def test_the_match_and_its_notified_at_survive_a_close_and_a_reopen(engine: Engine) -> None:
    """The alert state is `match.notified_at`: a delete anywhere in the cycle destroys it."""
    company_id = _company()
    posting_id = ingest_postings(company_id, [_posting("Y")]).created_ids[0]
    notified_at = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    with db.session_scope() as session:
        match = Match(
            posting_id=posting_id,
            score=87,
            reason="Synthetic fit.",
            state="saved",
            notified_at=notified_at,
        )
        session.add(match)
        session.flush()
        match_id = match.id

    ingest_postings(company_id, [])  # Y closes
    ingest_postings(company_id, [_posting("Y")])  # Y is listed again

    with db.session_scope() as session:
        stored_match = session.scalars(select(Match)).one()
        assert session.get(Posting, posting_id) is not None
        assert (stored_match.id, stored_match.posting_id) == (match_id, posting_id)
        assert stored_match.notified_at == notified_at
        assert (stored_match.score, stored_match.state) == (87, "saved")
        assert session.scalar(select(func.count()).select_from(Posting)) == 1


def test_the_created_ids_are_reported_in_the_order_the_payload_gave_them(
    engine: Engine,
) -> None:
    company_id = _company()

    result = ingest_postings(company_id, [_posting("b"), _posting("a")])

    stored = _stored(company_id)
    assert result.created_ids == [stored["b"]["id"], stored["a"]["id"]]
    assert result.reopened_ids == []
    with db.session_scope() as session:
        assert [
            session.get(Posting, posting_id).external_id for posting_id in result.created_ids
        ] == ["b", "a"]


def test_a_reopened_posting_is_reported_as_reopened_and_not_as_created(engine: Engine) -> None:
    """The hand-off #9 alerts on: a reopened posting is not new."""
    company_id = _company()
    ingest_postings(company_id, [_posting("Y"), _posting("Z")])
    ingest_postings(company_id, [])
    before = _stored(company_id)

    result = ingest_postings(company_id, [_posting("Z"), _posting("Y")])

    assert result.created_ids == []
    assert result.reopened_ids == [before["Z"]["id"], before["Y"]["id"]]
    with db.session_scope() as session:
        assert [
            session.get(Posting, posting_id).external_id for posting_id in result.reopened_ids
        ] == ["Z", "Y"]


def test_the_seam_is_a_company_and_the_postings_fetched_for_it() -> None:
    """The shape #9 and #10 read, named here so a change to it fails loudly."""
    assert list(inspect.signature(ingest_postings).parameters) == ["company_id", "postings"]
    assert IngestResult.__dataclass_fields__.keys() == {"created_ids", "reopened_ids"}
    assert IngestResult.__dataclass_params__.frozen


def test_the_service_imports_no_network_module() -> None:
    """It stores what it is given: an HTTP client or a `fetch` call here would move the seam."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(INGEST_SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported & NETWORK_MODULES == set()
    assert "fetch(" not in INGEST_SOURCE


def test_the_service_knows_no_payload_key_a_board_address_or_a_slug() -> None:
    assert {term for term in PAYLOAD_TERMS if term in INGEST_SOURCE} == set()


def test_the_service_reads_no_configuration() -> None:
    assert "environ" not in INGEST_SOURCE
    assert "getenv" not in INGEST_SOURCE
