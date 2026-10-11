"""The five tables JobRadar stores, declared once and nowhere else.

`app.db.init_db()` builds every table from these models with SQLAlchemy's
`create_all`, which creates only what is missing. It never alters a table that
already exists: add or rename a column here and a database that already holds
rows -- the local `data/jobradar.db` or the deployed Neon database -- keeps the
old shape, so the app runs against a schema that no longer matches this file.
That gap is real and is owned by #21 (schema migrations); nothing here migrates.

The column names below are the contract later tasks read. They may gain columns,
but none of them may be renamed or dropped without the migration path #21 owns.

Nothing here names a dialect: the types are generic, so `create_all` renders the
right DDL for whichever engine it is pointed at, SQLite or PostgreSQL.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
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

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    stored_path: Mapped[str] = mapped_column(String(500))
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Company(Base):
    """One watchlist entry: a company and the Greenhouse board slug to poll."""

    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)


class Posting(Base):
    """One job posting seen on a company's board."""

    __tablename__ = "posting"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
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

    id: Mapped[int] = mapped_column(primary_key=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("posting.id"))
    score: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    gaps: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="new")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Run(Base):
    """One digest run, including how far it got and what it produced."""

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    postings_seen: Mapped[int] = mapped_column(Integer, default=0)
    matches_created: Mapped[int] = mapped_column(Integer, default=0)
