"""`/watchlist`: the companies this app watches, and the board each one names.

`POST /watchlist` adds one company by name, board slug and, optionally, which
source the slug names -- Greenhouse when the body does not say -- and only after
that source has answered for that slug: a slug no board goes by is `404`, a board
that is down, slow or malformed is `502`, and a slug that is already watched is
`409`. `GET /watchlist` lists every company with its slug, its source and its
active state, and `DELETE /watchlist/{id}` removes one. Nobody renames a company
or writes `is_active` here; that is #29.

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

from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
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
