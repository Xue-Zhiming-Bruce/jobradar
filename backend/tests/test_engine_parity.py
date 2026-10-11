"""One model set, one test set, both engines.

Every test here asks for the shared `engine` fixture and therefore runs once on
SQLite and once on PostgreSQL: the SQLite parameter always runs, the PostgreSQL
parameter runs when `TEST_DATABASE_URL` is set and is skipped, visibly, when it
is not. Nothing in this module names an engine, a dialect or a URL, so the same
behaviour is proved on both engines by the same functions.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import db
from app.models import Base, Company, Match, Posting, Resume, Run

# A known instant in the past, so `datetime.now(UTC) - stored` is a positive
# delta on both engines instead of the TypeError SQLite used to raise.
KNOWN_UTC = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)

# Text with no known length budget, longer than the 255 characters a bounded
# column would reject on PostgreSQL while SQLite accepted them.
_LONGER_THAN_255_CHARACTERS = "x" * 300

# The unbounded columns: the seven that must take text of any length.
_LONG_TEXT_COLUMNS = [
    (Resume, "text"),
    (Posting, "title"),
    (Posting, "url"),
    (Posting, "description"),
    (Match, "reason"),
    (Match, "gaps"),
    (Run, "error"),
]

# Every timestamp column in the five tables: each one has to round-trip.
_TIMESTAMP_COLUMNS = [
    (Resume, "uploaded_at"),
    (Company, "created_at"),
    (Posting, "first_seen_at"),
    (Posting, "updated_at"),
    (Posting, "closed_at"),
    (Match, "created_at"),
    (Match, "notified_at"),
    (Run, "started_at"),
    (Run, "finished_at"),
]

# Test data is synthetic on purpose: the repository is public and holds no
# resume, real or otherwise.
SYNTHETIC_RESUME_TEXT = (
    "SYNTHETIC RESUME TEXT. Written for this test; not a real person's resume.\n"
    "Skills: Python, SQL."
)


def _assert_aware_utc(instant: datetime, label: str) -> None:
    """The engine handed back the same instant, still aware and still UTC.

    Nothing here repairs the value: no `.replace(tzinfo=...)`, no `.astimezone()`
    and no `assume_utc()`, because the conversion belongs to the models.
    """
    assert instant == KNOWN_UTC, label
    assert instant.tzinfo is not None, label
    assert instant.utcoffset() == timedelta(0), label


def _add_company_with_posting(session: Session) -> tuple[Company, Posting]:
    """One company with one posting under it, flushed so both carry an id."""
    company = Company(name="Acme", slug="acme")
    session.add(company)
    session.flush()
    posting = Posting(
        company_id=company.id,
        external_id="greenhouse-1",
        title="Backend Engineer",
        url="https://example.test/1",
    )
    session.add(posting)
    session.flush()
    return company, posting


def test_create_read_update_delete_round_trip_over_all_five_tables(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables)

    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=87, reason="Python and SQL fit"))
        session.add(Run(status="running"))
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text=SYNTHETIC_RESUME_TEXT,
                stored_path="/tmp/synthetic-resume.txt",
            )
        )

    with db.session_scope() as session:
        company = session.scalars(select(Company)).one()
        posting = session.scalars(select(Posting)).one()
        match = session.scalars(select(Match)).one()
        run = session.scalars(select(Run)).one()
        resume = session.scalars(select(Resume)).one()

        assert (company.name, company.slug) == ("Acme", "acme")
        assert posting.company_id == company.id  # the link through company_id holds
        assert (posting.external_id, posting.title) == ("greenhouse-1", "Backend Engineer")
        assert posting.url == "https://example.test/1"
        assert match.posting_id == posting.id  # the link through posting_id holds
        assert (match.score, match.reason) == (87, "Python and SQL fit")
        assert run.status == "running"
        assert resume.text == SYNTHETIC_RESUME_TEXT

        # Columns where absence is legitimate stay unset, and come back as None
        # on both engines rather than as an empty string.
        assert posting.location is None
        assert posting.description is None
        assert posting.closed_at is None
        assert match.gaps is None
        assert match.notified_at is None
        assert run.finished_at is None
        assert run.error is None

    with db.session_scope() as session:
        session.scalars(select(Company)).one().name = "Acme Renamed"
        session.scalars(select(Posting)).one().title = "Staff Engineer"
        session.scalars(select(Match)).one().state = "saved"
        session.scalars(select(Run)).one().status = "done"
        session.scalars(select(Resume)).one().is_active = False

    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().name == "Acme Renamed"
        assert session.scalars(select(Posting)).one().title == "Staff Engineer"
        assert session.scalars(select(Match)).one().state == "saved"
        assert session.scalars(select(Run)).one().status == "done"
        assert session.scalars(select(Resume)).one().is_active is False

    with db.session_scope() as session:
        for model in (Match, Posting, Company, Resume, Run):
            session.delete(session.scalars(select(model)).one())

    with db.session_scope() as session:
        for model in (Match, Posting, Company, Resume, Run):
            assert session.scalars(select(model)).all() == []


def test_source_text_of_any_length_survives_on_either_engine(engine: Engine) -> None:
    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        posting.title = _LONGER_THAN_255_CHARACTERS
        posting.url = _LONGER_THAN_255_CHARACTERS
        posting.description = _LONGER_THAN_255_CHARACTERS
        session.add(
            Match(
                posting_id=posting.id,
                score=1,
                reason=_LONGER_THAN_255_CHARACTERS,
                gaps=_LONGER_THAN_255_CHARACTERS,
            )
        )
        session.add(Run(status="failed", error=_LONGER_THAN_255_CHARACTERS))
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text=_LONGER_THAN_255_CHARACTERS,
                stored_path="/tmp/synthetic-resume.txt",
            )
        )

    with db.session_scope() as session:
        rows = {
            model.__tablename__: session.scalars(select(model)).one()
            for model in (Resume, Posting, Match, Run)
        }

        for model, column in _LONG_TEXT_COLUMNS:
            assert getattr(rows[model.__tablename__], column) == _LONGER_THAN_255_CHARACTERS, (
                f"{model.__tablename__}.{column}"
            )


def test_timestamps_round_trip_as_the_same_aware_instant(engine: Engine) -> None:
    with db.session_scope() as session:
        company, posting = _add_company_with_posting(session)
        company.created_at = KNOWN_UTC
        posting.first_seen_at = KNOWN_UTC
        posting.updated_at = KNOWN_UTC
        posting.closed_at = KNOWN_UTC
        session.add(
            Match(
                posting_id=posting.id,
                score=1,
                reason="Fits",
                created_at=KNOWN_UTC,
                notified_at=KNOWN_UTC,
            )
        )
        session.add(Run(status="done", started_at=KNOWN_UTC, finished_at=KNOWN_UTC))
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text=SYNTHETIC_RESUME_TEXT,
                stored_path="/tmp/synthetic-resume.txt",
                uploaded_at=KNOWN_UTC,
            )
        )

    with db.session_scope() as session:
        rows = {
            model.__tablename__: session.scalars(select(model)).one()
            for model in (Resume, Company, Posting, Match, Run)
        }

        for model, column in _TIMESTAMP_COLUMNS:
            _assert_aware_utc(
                getattr(rows[model.__tablename__], column), f"{model.__tablename__}.{column}"
            )

        # What the criterion buys: arithmetic on a stored timestamp works on both
        # engines instead of raising TypeError on the one that returned a naive value.
        assert datetime.now(UTC) - rows["run"].started_at > timedelta(0)
        assert datetime.now(UTC) - rows["posting"].first_seen_at > timedelta(0)


def test_defaults_come_from_the_models_not_from_the_database(engine: Engine) -> None:
    """A writer outside the models gets the defaults without naming a dialect."""
    before = datetime.now(UTC)

    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=1, reason="Fits"))
        session.add(Run(status="done"))
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text=SYNTHETIC_RESUME_TEXT,
                stored_path="/tmp/synthetic-resume.txt",
            )
        )

    with db.session_scope() as session:
        resume = session.scalars(select(Resume)).one()
        company = session.scalars(select(Company)).one()
        posting = session.scalars(select(Posting)).one()
        match = session.scalars(select(Match)).one()
        run = session.scalars(select(Run)).one()

        assert resume.is_active is True
        assert company.is_active is True
        assert match.state == "new"
        assert run.postings_seen == 0
        assert run.matches_created == 0

        for label, defaulted in [
            ("resume.uploaded_at", resume.uploaded_at),
            ("company.created_at", company.created_at),
            ("posting.first_seen_at", posting.first_seen_at),
            ("posting.updated_at", posting.updated_at),
            ("match.created_at", match.created_at),
            ("run.started_at", run.started_at),
        ]:
            assert defaulted.tzinfo is not None, label
            assert defaulted.utcoffset() == timedelta(0), label
            assert defaulted >= before, label


def test_a_table_named_match_is_created_read_updated_and_deleted(engine: Engine) -> None:
    """`match` is a keyword in SQLite and only a word in PostgreSQL; both accept it."""
    assert inspect(engine).has_table("match")

    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=40, reason="Partly fits"))

    with db.session_scope() as session:
        assert session.scalars(select(Match)).one().score == 40

    with db.session_scope() as session:
        session.scalars(select(Match)).one().score = 91

    with db.session_scope() as session:
        match = session.scalars(select(Match)).one()
        assert match.score == 91
        session.delete(match)

    with db.session_scope() as session:
        assert session.scalars(select(Match)).all() == []
