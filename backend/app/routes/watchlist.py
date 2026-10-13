"""`/watchlist`: the companies this app watches, and the board each one names.

`POST /watchlist` adds one company by name, board slug and, optionally, which
source the slug names -- Greenhouse when the body does not say -- and only after
that source has answered for that slug: a slug no board goes by is `404`, a board
that is down, slow or malformed is `502`, and a slug that is already watched is
`409`. `GET /watchlist` lists every company with its slug, its source and its
active state, `PATCH /watchlist/{id}` renames one and switches whether its board
is polled, and `DELETE /watchlist/{id}` removes one.

`PATCH` is the one route here that asks no board, and it writes nothing but a
label and a boolean: the board a company points at -- its `slug` and its
`source` -- is not editable on this path, because a posting is identified by
`(company_id, external_id)` (#6) and a new slug would strand the rows the old
board left behind; that rule is #60's to state. `is_active: false` is the poll's
switch and not a deletion: it takes the company out of `app.services.pipeline`'s
walk and leaves the row, its postings and their matches exactly as they were.

The probe is a validity check and nothing more. #6 is where a board's postings
are stored and deduped, and #9 is where the tick calls the source for real; this
route drops what the probe returns and writes no `posting` row.

Two things decide the shape of this module. The source is reached through
`get_source()`, one zero-argument provider the `POST` route takes with `Depends`,
returning the mapping from a stored source name to its adapter: the suite replaces
it with `app.dependency_overrides[get_source]` and no test in this module opens a
socket. And every write goes through the `Company(...)` constructor rather than
the table, because that is what runs the model's slug normaliser -- `"  ACME  "`
is probed and stored as `"acme"`, so it collides with the row already holding it,
and the unique index refuses the duplicate under the write. The `IntegrityError`
that comes back is the `409`; no check in Python is what keeps one row per slug.

This module knows nothing about a source's payload: no key name and no URL of one
appears here, and the family of failures it distinguishes is the shared one.
"""

import unicodedata
from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Company
from app.sources import SOURCES
from app.sources.base import Source, SourceError, UnknownSlug

router = APIRouter()


class CompanyCreate(BaseModel):
    """Body of an addition: the name to show, the board slug to poll, and its source.

    `name` and `slug` are required strings, and a string that carries nothing but
    whitespace is refused as well: the name labels a row a person reads, and the
    slug is what the source is asked about. `source` is optional and means
    Greenhouse when it is absent, so a client that sends `{name, slug}` keeps
    working; a value outside the two known names -- a case variant included -- is
    a `422` from this model, before any request is made. The values are left
    exactly as they were sent; normalising the slug is `app.models.Company`'s.
    """

    name: str
    slug: str
    source: Literal["greenhouse", "lever"] = "greenhouse"

    @field_validator("name", "slug")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Refuse a blank value as a body this route cannot use, before the source is asked."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CompanyUpdate(BaseModel):
    """Body of an edit: the label to show, the poll state, or both, and nothing else.

    Both fields are optional and at least one of them has to be supplied: `{}`
    is a request that says nothing, and answering it `200` would report a write
    that did not happen. `name` is a label stored exactly as it arrived, refused
    when it is blank (`CompanyCreate._not_blank`'s rule), carries a control
    character, or is longer than the 255 characters `company.name` holds -- the
    last two are the column's own refusals, and answering them here makes them
    the `422` this route owes instead of the `500` PostgreSQL's driver answers.
    No normaliser runs on it, because the one in `app.models` touches `slug`.
    `is_active` is a `Literal` of the two booleans, so `"false"` is refused
    rather than read as one: whether a board is polled is a state and not a
    truthy string.

    The board is not here, and refusing it is the point: `extra="forbid"` -- the
    `ResumeActivation`/`MatchVerdict` precedent -- makes `slug` and `source` a
    `422` rather than a write this route ignores, because the postings stored
    under the old slug would be stranded by a new one (#60). Nothing clears a
    field either: `name` is a `NOT NULL` column and `is_active` is the whole of
    "is it polled".
    """

    model_config = ConfigDict(extra="forbid")

    # 255 is `company.name`'s own `String(255)`, and the bound belongs here: the
    # column's refusal is a driver error, which on PostgreSQL is a `500` and not
    # the `422` this route owes. `test_openapi_contract.py` holds the two together.
    name: str | None = Field(default=None, max_length=255)
    is_active: Literal[True, False] | None = None

    @field_validator("name")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        """Refuse a blank label as a body this route cannot use: a name is what a person reads."""
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("name")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        """Refuse a label carrying a control character, a NUL included.

        PostgreSQL stores no NUL in a text column: it answers that write with a
        driver error, which is a `500` and not the `422` this route owes. No
        other control character belongs in a label a person reads either, and
        refusing them here keeps both engines answering a body the same way.
        """
        if value is not None and any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("must not carry a control character")
        return value

    @model_validator(mode="after")
    def _names_a_field(self) -> "CompanyUpdate":
        """Refuse a body naming neither field, so no `200` reports a write that did not happen."""
        if self.name is None and self.is_active is None:
            raise ValueError("must carry a name, an is_active, or both")
        return self


class WatchedCompany(BaseModel):
    """One watchlist entry as this API renders it: five fields, and no timestamp.

    The same body `POST` answers with, so a client reads one shape for a company
    wherever it sees one. `created_at` is stored and not exposed. `source` is the
    board the row names, defaulted here so it is not a required property of the
    contract: a client that already reads `{id, name, slug, is_active}` is not
    broken by the extra field.
    """

    id: int
    name: str
    slug: str
    is_active: bool
    source: Literal["greenhouse", "lever"] = "greenhouse"


# The refusals this router answers with, named in the contract the way
# `app.routes.resume` names its own: a client reads the status codes it has to
# handle without having to try them out. Each dict is what the route that
# answers it declares, so the contract stays a list of real answers.
NOT_FOUND_RESPONSE = {404: {"description": "No board goes by that slug, or no company has that id"}}
DUPLICATE_RESPONSE = {409: {"description": "The slug is already on the watchlist"}}
UNUSABLE_REQUEST_RESPONSE = {
    422: {"description": "The body, or the path, is not something this route can use"}
}
UNREADABLE_BOARD_RESPONSE = {
    502: {"description": "The board could not be read: it is down, slow or malformed"}
}

CREATE_RESPONSES = (
    NOT_FOUND_RESPONSE
    | DUPLICATE_RESPONSE
    | UNUSABLE_REQUEST_RESPONSE
    | UNREADABLE_BOARD_RESPONSE
)
UPDATE_RESPONSES = NOT_FOUND_RESPONSE | UNUSABLE_REQUEST_RESPONSE
DELETE_RESPONSES = NOT_FOUND_RESPONSE | UNUSABLE_REQUEST_RESPONSE


def get_source() -> Mapping[str, Source]:
    """The one seam between this route and a job board: the provider the tests replace.

    Zero arguments, so it carries no configuration, and resolved per request by
    `Depends`, so the suite installs `app.dependency_overrides[get_source] =
    lambda: fake_sources` without a socket ever being opened. It returns
    `app.sources.SOURCES`, the mapping from a stored source name to that board's
    adapter: the request's own `source` is the key, so the probe goes through the
    board the row will name.
    """
    return SOURCES


def _watched(company: Company) -> WatchedCompany:
    """One company as this API renders it, read while its session is still open."""
    return WatchedCompany(
        id=company.id,
        name=company.name,
        slug=company.slug,
        is_active=company.is_active,
        source=company.source,
    )


def _probe(source: Source, slug: str) -> None:
    """Ask that board whether the slug names it, and drop the answer.

    `UnknownSlug` is the one failure that means there is no board to poll, and it
    is the only one that answers `404`. The rest of the family --
    `BoardUnavailable`, `SourceTimeout`, `MalformedPayload` -- is a board that
    could not be read: `502`, never `404`, because a board that is down must not
    be reported as a board that does not exist. Neither message carries the
    source's own text, which can be any HTML.
    """
    try:
        source(slug)
    except UnknownSlug as unknown:
        raise HTTPException(
            status_code=404, detail=f"The board '{slug}' does not exist"
        ) from unknown
    except SourceError as unreadable:
        raise HTTPException(
            status_code=502, detail=f"The board '{slug}' could not be read"
        ) from unreadable


@router.get(
    "/watchlist",
    response_model=list[WatchedCompany],
    tags=["watchlist"],
    summary="List the watched companies",
)
def list_watchlist() -> list[WatchedCompany]:
    """Every company on the watchlist, in ascending id order, active or not.

    No filter and no paging: one user's watchlist is short, and an inactive
    company is still a company to show. Each row carries the source it names.
    """
    with db.session_scope() as session:
        return [
            _watched(company)
            for company in session.scalars(select(Company).order_by(Company.id))
        ]


@router.post(
    "/watchlist",
    response_model=WatchedCompany,
    status_code=201,
    tags=["watchlist"],
    summary="Watch a company's board",
    responses=CREATE_RESPONSES,
)
def add_company(
    body: CompanyCreate,
    sources: Annotated[Mapping[str, Source], Depends(get_source)],
) -> WatchedCompany:
    """Add one company, once the board its `source` names has answered for its slug.

    The board is asked before anything is written, and every `POST` asks it once:
    a body that cannot be used never reaches this function, and a slug that is
    already watched is refused only after the source has spoken for it.
    """
    # The constructor, not the table: this is where the model keeps one spelling
    # of a slug, and what the probe is then asked about.
    company = Company(name=body.name, slug=body.slug, source=body.source)
    _probe(sources[body.source], company.slug)
    try:
        with db.session_scope() as session:
            session.add(company)
            session.flush()  # the row, and the unique index's verdict, exist here
            return _watched(company)  # read before the commit expires it
    except IntegrityError as duplicate:
        raise HTTPException(
            status_code=409, detail=f"The slug '{company.slug}' is already watched"
        ) from duplicate


@router.patch(
    "/watchlist/{id}",
    response_model=WatchedCompany,
    tags=["watchlist"],
    summary="Rename a watched company, or take it out of the poll",
    responses=UPDATE_RESPONSES,
)
def update_company(id: int, body: CompanyUpdate) -> WatchedCompany:
    """Write the fields the body names on that company, and answer as `GET /watchlist` renders it.

    The id is a path parameter and an `int`: a value that is not one never reaches
    this function and is refused by the path's own validation as `422`, and an id
    that finds no row is a `404` that writes nothing. No board is asked on this
    path -- neither `slug` nor `source` is editable here -- so this route answers
    no `409` and no `502` either.

    `is_active: false` takes the company out of the tick's walk and is not a
    deletion: the row, its postings and their matches stay untouched, and
    `GET /watchlist` keeps listing the company as inactive. Sending the same body
    again is the same answer twice, and no request here creates a row.
    """
    with db.session_scope() as session:
        company = session.get(Company, id)
        if company is None:
            raise HTTPException(
                status_code=404, detail=f"There is no watched company with id {id}"
            )
        if body.name is not None:
            company.name = body.name
        if body.is_active is not None:
            company.is_active = body.is_active
        session.flush()
        return _watched(company)  # read before the commit expires it


@router.delete(
    "/watchlist/{id}",
    status_code=204,
    tags=["watchlist"],
    summary="Stop watching a company",
    responses=DELETE_RESPONSES,
)
def remove_company(id: int) -> None:
    """Delete exactly that company, and answer `204` with no body when it went.

    The id is a path parameter and an `int`: a value that is not one never
    reaches this function and is refused by the path's own validation as `422`.
    An id that finds no row is a `404` and deletes nothing.
    """
    with db.session_scope() as session:
        company = session.get(Company, id)
        if company is None:
            raise HTTPException(
                status_code=404, detail=f"There is no watched company with id {id}"
            )
        session.delete(company)
