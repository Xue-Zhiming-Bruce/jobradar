"""The six tables JobRadar stores, declared once and nowhere else.

`app.db.init_db()` builds every table from these models with SQLAlchemy's
`create_all`, which creates only what is missing. It never alters a table that
already exists: add or rename a column here and a database that already holds
rows -- the local `data/jobradar.db` or the deployed Neon database -- keeps the
old shape, so the app runs against a schema that no longer matches this file.
That gap is real and is owned by #21 (schema migrations); nothing here migrates.

The rules that keep the rows honest ride on these declarations and are enforced
by the database on both engines rather than by a check in application code: the
dedupe keys, at most one active resume, the score range, the vocabularies the
source, state, status and outcome columns accept, and the foreign keys, which
cascade. A rule that is not declared here is not enforced, so a change to one of
them is the migration gap #21 owns as well.

The column names below are the contract later tasks read. They may gain columns,
but none of them may be renamed or dropped without the migration path #21 owns.

Nothing here names a dialect: the types are generic, so `create_all` renders the
right DDL for whichever engine it is pointed at, SQLite or PostgreSQL.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """One metadata object holding all six tables, for `create_all`."""


def _utcnow() -> datetime:
    """Default timestamp, in UTC, as aware values so both engines mean the same."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """One timestamp type for every table: an aware UTC instant, stored as a timestamp.

    `DateTime(timezone=True)` on its own is not enough for parity: PostgreSQL
    stores an instant and hands back an aware value, while SQLite drops the UTC
    offset and hands back a naive one, so subtracting a stored timestamp from an
    aware `datetime.now(UTC)` raised `TypeError` on SQLite only. The conversion
    therefore lives here, on the way in and on the way out, instead of in every
    caller: what comes back is the same aware UTC instant from either engine.

    A naive value is rejected rather than guessed at, because the two engines
    would otherwise read it in two different timezones.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"{value!r} has no UTC offset: store an aware instant")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Resume(Base):
    """The uploaded resume: its extracted text, and where the file itself lives."""

    __tablename__ = "resume"
    # At most one resume is active, so the index covers only the active rows and
    # its predicate is given for both engines: one dialect's predicate alone
    # would leave the other with a plain unique index, which refuses a second
    # *inactive* row -- and several inactive resumes are legitimate history.
    __table_args__ = (
        Index(
            "uq_resume_one_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active"),
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    # The `{"name": ..., "skills": [...]}` object #26's parse stored, as JSON text,
    # and NULL on a row that was never parsed -- which is what makes a parse happen.
    # #50 reads it; nothing writes it but `POST /resume/parse`.
    parsed_fields: Mapped[str | None] = mapped_column(Text)
    stored_path: Mapped[str] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Company(Base):
    """One watchlist entry: a company, the board slug to poll, and that board's source."""

    __tablename__ = "company"
    # The vocabulary `source` accepts, as a named check the database enforces --
    # the same statement on both engines, no dialect enum. A value outside it,
    # `workday` say, is an `IntegrityError` and not a row. And the key, named so
    # both engines can drop and create it by that one spelling: a slug is one
    # company *per source*, so the same board can be watched on Greenhouse and
    # on Lever at once while a second row of the same pair is refused.
    __table_args__ = (
        CheckConstraint(
            "source IN ('greenhouse', 'lever')", name="ck_company_source_values"
        ),
        UniqueConstraint("source", "slug", name="uq_company_source_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    # Which adapter fetches this board -- one of `app.sources.SOURCES`. Defaulted
    # for the ORM so a row added without a source is Greenhouse, which is what
    # every row written before the column existed was.
    source: Mapped[str] = mapped_column(String(20), default="greenhouse")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    # The validator the board's last stored answer carried, sent back as
    # `If-None-Match`/`If-Modified-Since` by the next run and stored verbatim -- an
    # `ETag` keeps its quotes, a `Last-Modified` is the date string it arrived as --
    # so `Text` rather than a bound the server did not choose. NULL means this
    # company has no validator yet, which is every row written before the columns.
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)

    @validates("slug")
    def _normalise_slug(self, key: str, value: str) -> str:
        """Store one spelling of a slug, because a collation cannot bind two.

        Both engines compare text by their default collation and both treat
        `Acme` and `acme` as two different values, so `UNIQUE` over the raw
        column would happily store them side by side. No `COLLATE` clause and
        no expression index is used here either, which leaves normalisation as
        the only thing that can make the one-row-per-source-and-slug rule real:
        the value is trimmed and lower-cased on the way in, so `"  Acme  "` is
        stored as `"acme"` and the second company of that source collides with
        the first.
        """
        return value.strip().lower()


class Posting(Base):
    """One job posting seen on a company's board."""

    __tablename__ = "posting"
    # A posting is identified by what the source calls it, within one company:
    # the dedupe key a re-run of the tick matches on.
    __table_args__ = (UniqueConstraint("company_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow, onupdate=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Match(Base):
    """The score a posting received, why it fits, what is missing and how it got there."""

    __tablename__ = "match"
    # The vocabularies `state` and `outcome` accept, and the score range, as named
    # checks the database enforces -- the same statement on both engines, no
    # dialect enum.
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="ck_match_score_range"),
        CheckConstraint(
            "state IN ('new', 'saved', 'dismissed')", name="ck_match_state_values"
        ),
        CheckConstraint(
            "outcome IN ('scored', 'rejected', 'not_scored')",
            name="ck_match_outcome_values",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # One match per posting: re-scoring has to update the row instead of adding
    # a second one.
    posting_id: Mapped[int] = mapped_column(
        ForeignKey("posting.id", ondelete="CASCADE"), unique=True
    )
    score: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    gaps: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="new")
    # How this row got its score, in one vocabulary rather than in the free text
    # `reason` carries: `'scored'` is a score the scorer answered (a genuine 0
    # included), `'rejected'` is a posting the prefilter refused and
    # `'not_scored'` is a model failure stored instead of a score. `NOT NULL`, so
    # no row is in none of the three, and server-defaulted so a row inserted by
    # neither writer -- a test's, say -- is the ordinary case rather than a NULL.
    # One writer per state: `prefilter.record_rejection` and
    # `scorer.record_score`, the two modules that also decide the reason's prefix.
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, server_default="scored")
    # Which model produced this row's score and which version of the scoring
    # prompt it was read with, written by `scorer.record_score` for a scored row
    # and a not-scored one alike. Both nullable and neither defaulted: NULL is
    # "unknown", which is what a rejection row (no model ran) and every row written
    # before this marker existed keep -- it does not mean "none".
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Run(Base):
    """One digest run, including how far it got and what it produced."""

    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_run_status_values"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    postings_seen: Mapped[int] = mapped_column(Integer, default=0)
    matches_created: Mapped[int] = mapped_column(Integer, default=0)
    # How many matches this run's own digest emailed. Three states, and none of
    # them is a missing value: `0` is this run having emailed nothing at all, a
    # positive value is that many, and NULL is "unknown" -- a row written before
    # this column existed, or a run whose send failed, where #10 writes nothing.
    # Nullable with no default of either kind, because a `NOT NULL` default would
    # make every pre-marker row claim it emailed nothing, which is a lie for a run
    # that did. `matches_created` (above) is not this: it counts `record_score`
    # writes, and only the digest writes this.
    matches_emailed: Mapped[int | None] = mapped_column(Integer)
    # How many active companies this run's budget stopped it from asking. Three
    # readings, and a `0` is not one of them: NULL is "the budget did not stop this
    # walk" -- the run reached the last active company, failed on a board before
    # the bound, or is a row written before this column existed -- and a positive
    # value is that many active companies the run did not fetch. Nullable with no
    # default of either kind, so a row that predates the column reads NULL rather
    # than claiming a walk nobody bounded. Written by `services.pipeline`, the one
    # place that knows the bound stopped the walk; #9 writes neither side.
    companies_not_fetched: Mapped[int | None] = mapped_column(Integer)
    # How many passers a re-match's own model cap left for a later
    # run. Three readings, and a `0` is not one of them: NULL is "the cap left no
    # passer" -- the ordinary case, and every row written by a tick or before this
    # column existed -- and a positive value is that many passers, in candidate
    # order, that this re-match declined to offer the model. Nullable with no
    # default of either kind, so no tick claims a cap it does not have. Written by
    # `services.pipeline._record_not_rematched`, the one place that knows the cap
    # stopped the offers; #9 writes neither side.
    postings_not_rematched: Mapped[int | None] = mapped_column(Integer)


class RunCompany(Base):
    """One company's outcome in one run: what its board answered, or why it did not.

    One row per company a run's walk attempted, written by `services.pipeline` as
    the walk reaches that company. A company the run's budget never reached has no
    row -- it was not attempted -- and #56's `run.companies_not_fetched` counts
    those instead, which is why a budget-stopped run is not a failed one. The two
    foreign keys cascade, and the unique key is what makes "one outcome per company
    per run" a rule the database enforces rather than a check the walk keeps.
    """

    __tablename__ = "run_company"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('fetched', 'not_modified', 'failed')",
            name="ck_run_company_outcome_values",
        ),
        UniqueConstraint("run_id", "company_id", name="uq_run_company_outcome"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"))
    # What the walk did with that board: `'fetched'` for a `200` whose payload was
    # stored, `'not_modified'` for the `304` #55 added, `'failed'` for a fetch or a
    # shrink refusal that raised. The one writer is `services.pipeline`.
    outcome: Mapped[str] = mapped_column(String(20))
    # A failure's own message, on a `'failed'` row and NULL everywhere else: the
    # company's slug and, where a status decided it, the status code -- never a
    # response body, a credential or a resume (#4/#9).
    error: Mapped[str | None] = mapped_column(Text)
