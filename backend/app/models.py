"""The five tables JobRadar stores, declared once and nowhere else.

`app.db.init_db()` builds every table from these models with SQLAlchemy's
`create_all`, which creates only what is missing. It never alters a table that
already exists: add or rename a column here and a database that already holds
rows -- the local `data/jobradar.db` or the deployed Neon database -- keeps the
old shape, so the app runs against a schema that no longer matches this file.
That gap is real and is owned by #21 (schema migrations); nothing here migrates.

The rules that keep the rows honest ride on these declarations and are enforced
by the database on both engines rather than by a check in application code: the
dedupe keys, at most one active resume, the score range, the two vocabularies
the state and status columns accept, and the foreign keys, which cascade. A rule
that is not declared here is not enforced, so a change to one of them is the
migration gap #21 owns as well.

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
    """One metadata object holding all five tables, for `create_all`."""


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
    """One watchlist entry: a company and the Greenhouse board slug to poll."""

    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)

    @validates("slug")
    def _normalise_slug(self, key: str, value: str) -> str:
        """Store one spelling of a slug, because a collation cannot bind two.

        Both engines compare text by their default collation and both treat
        `Acme` and `acme` as two different values, so `UNIQUE` over the raw
        column would happily store them side by side. No `COLLATE` clause and
        no expression index is used here either, which leaves normalisation as
        the only thing that can make the one-row-per-slug rule real: the value
        is trimmed and lower-cased on the way in, so `"  Acme  "` is stored as
        `"acme"` and the second company collides with the first.
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
    """The score a posting received, why it fits and what is missing."""

    __tablename__ = "match"
    # The vocabulary `state` accepts, and the score range, as named checks the
    # database enforces -- the same statement on both engines, no dialect enum.
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="ck_match_score_range"),
        CheckConstraint(
            "state IN ('new', 'saved', 'dismissed')", name="ck_match_state_values"
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
