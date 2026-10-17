"""One model set, one test set, both engines.

Every test here asks for the shared `engine` fixture and therefore runs once on
SQLite and once on PostgreSQL: the SQLite parameter always runs, the PostgreSQL
parameter runs when `TEST_DATABASE_URL` is set and is skipped, visibly, when it
is not. Nothing in this module names an engine, a dialect or a URL, so the same
behaviour is proved on both engines by the same functions.

The last section asks the engines to refuse a row that breaks one of the rules
of #23 -- a duplicate dedupe key, a second active resume, a score outside the
range, a state or status outside its vocabulary, a foreign key to a row that is
not there, a second match for one posting -- through the app's own
`session_scope()`. Nothing filters those rows in Python, so what the assertions
read back is the engine's own refusal, named in its own words.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db
from app.models import Base, Company, Match, Posting, Resume, Run, TelemetrySpan

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

# Every timestamp column in the seven tables: each one has to round-trip.
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
    (TelemetrySpan, "started_at"),
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


def _add_company_with_posting(
    session: Session, slug: str = "acme", external_id: str = "greenhouse-1"
) -> tuple[Company, Posting]:
    """One company with one posting under it, flushed so both carry an id."""
    company = Company(name=slug.capitalize(), slug=slug)
    session.add(company)
    session.flush()
    posting = Posting(
        company_id=company.id,
        external_id=external_id,
        title="Backend Engineer",
        url="https://example.test/1",
    )
    session.add(posting)
    session.flush()
    return company, posting


def test_create_read_update_delete_round_trip_over_all_seven_tables(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables)

    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=87, reason="Python and SQL fit"))
        session.add(Run(status="running"))
        session.add(
            TelemetrySpan(
                trace_id="2f1c9a5b7d3e4f60",
                name="http",
                route="/matches/{id}",
                method="GET",
                status_code=200,
                duration_ms=12,
                started_at=KNOWN_UTC,
            )
        )
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
        span = session.scalars(select(TelemetrySpan)).one()

        assert (company.name, company.slug) == ("Acme", "acme")
        assert posting.company_id == company.id  # the link through company_id holds
        assert (posting.external_id, posting.title) == ("greenhouse-1", "Backend Engineer")
        assert posting.url == "https://example.test/1"
        assert match.posting_id == posting.id  # the link through posting_id holds
        assert (match.score, match.reason) == (87, "Python and SQL fit")
        assert run.status == "running"
        assert resume.text == SYNTHETIC_RESUME_TEXT
        assert (span.name, span.route, span.method) == ("http", "/matches/{id}", "GET")
        assert (span.status_code, span.duration_ms) == (200, 12)

        # Columns where absence is legitimate stay unset, and come back as None
        # on both engines rather than as an empty string.
        assert posting.location is None
        assert posting.description is None
        assert posting.closed_at is None
        assert match.gaps is None
        assert match.notified_at is None
        assert run.finished_at is None
        assert run.error is None
        assert session.scalars(select(TelemetrySpan)).one().started_at is not None

    with db.session_scope() as session:
        session.scalars(select(Company)).one().name = "Acme Renamed"
        session.scalars(select(Posting)).one().title = "Staff Engineer"
        session.scalars(select(Match)).one().state = "saved"
        session.scalars(select(Run)).one().status = "succeeded"
        session.scalars(select(Resume)).one().is_active = False
        session.scalars(select(TelemetrySpan)).one().status_code = 404

    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().name == "Acme Renamed"
        assert session.scalars(select(Posting)).one().title == "Staff Engineer"
        assert session.scalars(select(Match)).one().state == "saved"
        assert session.scalars(select(Run)).one().status == "succeeded"
        assert session.scalars(select(Resume)).one().is_active is False
        assert session.scalars(select(TelemetrySpan)).one().status_code == 404

    with db.session_scope() as session:
        for model in (Match, Posting, Company, Resume, Run, TelemetrySpan):
            session.delete(session.scalars(select(model)).one())

    with db.session_scope() as session:
        for model in (Match, Posting, Company, Resume, Run, TelemetrySpan):
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
        session.add(Run(status="succeeded", started_at=KNOWN_UTC, finished_at=KNOWN_UTC))
        session.add(
            TelemetrySpan(
                trace_id="2f1c9a5b7d3e4f60",
                name="run",
                duration_ms=41,
                started_at=KNOWN_UTC,
            )
        )
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
            for model in (Resume, Company, Posting, Match, Run, TelemetrySpan)
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
        session.add(Run(status="succeeded"))
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


# What the database itself refuses, asked through the app's own session helper.


def _synthetic_resume(**overrides: object) -> Resume:
    """A resume row whose text is synthetic: the repository holds no real one."""
    fields: dict[str, object] = {
        "filename": "synthetic-resume.txt",
        "content_type": "text/plain",
        "text": SYNTHETIC_RESUME_TEXT,
        "stored_path": "/tmp/synthetic-resume.txt",
    }
    fields.update(overrides)
    return Resume(**fields)


def _assert_the_engine_named_what_it_refused(
    exception: IntegrityError, *spellings: str
) -> None:
    """Both engines name the rule they refused, each in its own spelling.

    SQLite names a unique constraint by its columns and a named check by its
    name; PostgreSQL names the constraint itself, which for the constraints left
    to SQLAlchemy is `<table>_<columns>_key`. Any of the spellings may match --
    the engine's whole message is reported when none of them does.
    """
    message = str(exception)
    assert any(spelling in message for spelling in spellings), message


def _deletes_sent_by(engine: Engine, action: Callable[[], None]) -> list[str]:
    """Every DELETE the engine sent while `action` ran, as one flat line each."""
    statements: list[str] = []

    def record(connection: object, cursor: object, statement: str, *rest: object) -> None:
        if statement.lstrip().upper().startswith("DELETE"):
            statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


def test_a_duplicate_company_slug_is_refused_by_the_database(engine: Engine) -> None:
    """The key is the pair `(source, slug)`, and a second row of one pair is refused."""
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Company(name="Acme Again", slug="acme"))

    _assert_the_engine_named_what_it_refused(
        refusal.value, "company.source, company.slug", "uq_company_source_slug"
    )
    with db.session_scope() as session:
        assert [company.slug for company in session.scalars(select(Company))] == ["acme"]


def test_the_same_slug_on_another_source_is_a_different_company(engine: Engine) -> None:
    """The widened key's other half: one board slug is watchable on two boards at once."""
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme", source="greenhouse"))
        session.add(Company(name="Acme on Lever", slug="acme", source="lever"))

    with db.session_scope() as session:
        assert [
            (company.slug, company.source)
            for company in session.scalars(select(Company).order_by(Company.id))
        ] == [("acme", "greenhouse"), ("acme", "lever")]


def test_a_duplicate_posting_key_is_refused_by_the_database(engine: Engine) -> None:
    with db.session_scope() as session:
        company, _ = _add_company_with_posting(session)
        company_id = company.id

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(
                Posting(
                    company_id=company_id,
                    external_id="greenhouse-1",
                    title="Backend Engineer",
                    url="https://example.test/1",
                )
            )

    _assert_the_engine_named_what_it_refused(
        refusal.value,
        "posting.company_id, posting.external_id",
        "posting_company_id_external_id_key",
    )
    with db.session_scope() as session:
        assert len(session.scalars(select(Posting)).all()) == 1


def test_the_same_external_id_under_another_company_is_a_different_posting(
    engine: Engine,
) -> None:
    """The key is the pair, not the source's id on its own."""
    with db.session_scope() as session:
        first, _ = _add_company_with_posting(session, slug="acme")
        first_id = first.id

    with db.session_scope() as session:
        second, _ = _add_company_with_posting(session, slug="other")
        second_id = second.id

    with db.session_scope() as session:
        postings = session.scalars(select(Posting)).all()

        assert [posting.external_id for posting in postings] == [
            "greenhouse-1",
            "greenhouse-1",
        ]
        assert {posting.company_id for posting in postings} == {first_id, second_id}


def test_a_slug_is_trimmed_and_lower_cased_before_it_is_stored(engine: Engine) -> None:
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="  Acme  "))

    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().slug == "acme"


def test_two_slugs_that_normalise_to_one_spelling_collide_in_the_database(
    engine: Engine,
) -> None:
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="  Acme  "))

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Company(name="Acme Again", slug="  ACME  "))

    _assert_the_engine_named_what_it_refused(
        refusal.value, "company.source, company.slug", "uq_company_source_slug"
    )


def test_uniqueness_alone_cannot_bind_the_two_spellings_of_a_slug(engine: Engine) -> None:
    """Why the model normalises: neither engine's `UNIQUE` merges the spellings.

    The rows go in through the table rather than through `Company`, on purpose: it
    is the column-level rule on its own that is under test, and it stores `Acme`
    and `acme` side by side because both engines compare text case-sensitively.
    The app's writes go through the model, which trims and lower-cases the slug.
    """
    with db.session_scope() as session:
        session.execute(Company.__table__.insert().values(name="Casey", slug="Acme"))
        session.execute(Company.__table__.insert().values(name="Casey Too", slug="acme"))

    with db.session_scope() as session:
        assert {company.slug for company in session.scalars(select(Company))} == {"Acme", "acme"}


def test_a_second_active_resume_is_refused_and_a_second_inactive_one_is_not(
    engine: Engine,
) -> None:
    with db.session_scope() as session:
        session.add(_synthetic_resume())

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(_synthetic_resume(filename="synthetic-resume-2.txt"))

    _assert_the_engine_named_what_it_refused(
        refusal.value, "resume.is_active", "uq_resume_one_active"
    )

    with db.session_scope() as session:
        session.scalars(select(Resume)).one().is_active = False

    with db.session_scope() as session:
        session.add(
            _synthetic_resume(
                filename="synthetic-resume-2.txt",
                stored_path="/tmp/synthetic-resume-2.txt",
                is_active=False,
            )
        )
        session.add(
            _synthetic_resume(
                filename="synthetic-resume-3.txt",
                stored_path="/tmp/synthetic-resume-3.txt",
                is_active=False,
            )
        )

    with db.session_scope() as session:
        resumes = session.scalars(select(Resume).order_by(Resume.id)).all()

        assert [resume.is_active for resume in resumes] == [False, False, False]


def test_a_score_outside_zero_to_one_hundred_is_refused_by_the_database(
    engine: Engine,
) -> None:
    """Each case gets its own posting: `match.posting_id` is unique as well."""
    for score in (101, -1):
        with db.session_scope() as session:
            _, posting = _add_company_with_posting(session, slug=f"company-{abs(score)}")
            posting_id = posting.id

        with pytest.raises(IntegrityError) as refusal:
            with db.session_scope() as session:
                session.add(Match(posting_id=posting_id, score=score, reason="Fits"))

        _assert_the_engine_named_what_it_refused(refusal.value, "ck_match_score_range")


def test_the_ends_of_the_score_range_are_accepted(engine: Engine) -> None:
    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session, slug="low")
        session.add(Match(posting_id=posting.id, score=0, reason="Fits"))
        _, posting = _add_company_with_posting(session, slug="high")
        session.add(Match(posting_id=posting.id, score=100, reason="Fits"))

    with db.session_scope() as session:
        assert sorted(match.score for match in session.scalars(select(Match))) == [0, 100]


def test_the_two_vocabularies_are_what_the_columns_accept(engine: Engine) -> None:
    with db.session_scope() as session:
        for index, state in enumerate(("new", "saved", "dismissed")):
            _, posting = _add_company_with_posting(session, slug=f"state-{index}")
            session.add(Match(posting_id=posting.id, score=50, reason="Fits", state=state))
        for status in ("running", "succeeded", "failed"):
            session.add(Run(status=status))

    with db.session_scope() as session:
        assert sorted(match.state for match in session.scalars(select(Match))) == [
            "dismissed",
            "new",
            "saved",
        ]
        assert sorted(run.status for run in session.scalars(select(Run))) == [
            "failed",
            "running",
            "succeeded",
        ]


def test_a_state_outside_the_vocabulary_is_refused_by_the_database(engine: Engine) -> None:
    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        posting_id = posting.id

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Match(posting_id=posting_id, score=50, reason="Fits", state="archived"))

    _assert_the_engine_named_what_it_refused(refusal.value, "ck_match_state_values")


def test_a_status_outside_the_vocabulary_is_refused_by_the_database(engine: Engine) -> None:
    """`done` is not a status: the column accepts `running`, `succeeded`, `failed`."""
    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Run(status="done"))

    _assert_the_engine_named_what_it_refused(refusal.value, "ck_run_status_values")


def test_a_posting_for_a_company_that_does_not_exist_is_refused(engine: Engine) -> None:
    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(
                Posting(
                    company_id=999_999,
                    external_id="greenhouse-1",
                    title="Backend Engineer",
                    url="https://example.test/1",
                )
            )

    _assert_the_engine_named_what_it_refused(
        refusal.value, "FOREIGN KEY constraint failed", "violates foreign key constraint"
    )


def test_a_second_match_for_one_posting_is_refused_by_the_database(engine: Engine) -> None:
    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=87, reason="Python and SQL fit"))
        posting_id = posting.id

    with pytest.raises(IntegrityError) as refusal:
        with db.session_scope() as session:
            session.add(Match(posting_id=posting_id, score=91, reason="Re-scored"))

    _assert_the_engine_named_what_it_refused(
        refusal.value, "match.posting_id", "match_posting_id_key"
    )


def test_deleting_a_company_deletes_its_postings_and_their_matches(engine: Engine) -> None:
    """One `DELETE` is enough: the cascade runs in the database, for these rows only."""
    with db.session_scope() as session:
        company, posting = _add_company_with_posting(session, slug="acme")
        session.add(Match(posting_id=posting.id, score=87, reason="Fits"))
        other, other_posting = _add_company_with_posting(session, slug="other")
        session.add(Match(posting_id=other_posting.id, score=40, reason="Partly fits"))
        company_id, other_id, other_posting_id = company.id, other.id, other_posting.id

    def delete_the_company() -> None:
        with db.session_scope() as session:
            session.delete(session.get(Company, company_id))

    statements = _deletes_sent_by(engine, delete_the_company)

    assert len(statements) == 1, statements
    assert statements[0].startswith("DELETE FROM company WHERE company.id")
    with db.session_scope() as session:
        assert [company.slug for company in session.scalars(select(Company))] == ["other"]
        assert [posting.company_id for posting in session.scalars(select(Posting))] == [other_id]
        assert [match.posting_id for match in session.scalars(select(Match))] == [
            other_posting_id
        ]


def test_deleting_a_posting_deletes_its_match(engine: Engine) -> None:
    with db.session_scope() as session:
        _, posting = _add_company_with_posting(session)
        session.add(Match(posting_id=posting.id, score=87, reason="Fits"))
        posting_id = posting.id

    def delete_the_posting() -> None:
        with db.session_scope() as session:
            session.delete(session.get(Posting, posting_id))

    statements = _deletes_sent_by(engine, delete_the_posting)

    assert len(statements) == 1, statements
    assert statements[0].startswith("DELETE FROM posting WHERE posting.id")
    with db.session_scope() as session:
        assert session.scalars(select(Match)).all() == []
        assert session.scalars(select(Posting)).all() == []
        assert len(session.scalars(select(Company)).all()) == 1
