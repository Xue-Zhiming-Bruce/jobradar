"""A company's public Lever board, turned into postings this app can store.

`fetch(slug)` makes one synchronous `GET` to
`https://api.lever.co/v0/postings/{slug}?mode=json`. Lever's public board API is
one unauthenticated request that answers a top-level JSON array -- no envelope,
no `meta` -- and a board that does not exist answers `404`. Nothing is written
and no row is touched: #9's tick calls `fetch`, #6 stores what it returns, and
only the watchlist (#5) has a caller to answer an `UnknownSlug` to.

This is the one module that knows the Lever payload's shape -- its key names, its
UUID id, its HTML in `lists` -- and every value it returns is read from the
payload rather than rebuilt from the slug: a posting's `hostedUrl` links to the
company's own site, so a URL synthesised from the slug would be wrong.
`app.sources.base` holds what this adapter and every other one share:
`SourcePosting`, the error family, the slug gate, the timeout and the
html-to-text helper. No other module may know these keys.

The description is the posting's plain text and never its HTML: `descriptionPlain`
when the payload carries it, then each `lists` entry's `text` heading and its
`content` with the tags stripped, in the order Lever sent them. `description` --
the payload's HTML field -- is deliberately not read.

Every way a board can fail comes back as one of four `SourceError`s rather than a
crash or an `httpx2` exception, and no case raises the family's own parent:

- `UnknownSlug` -- the value cannot name a board, or no such board exists. A
  `404` decides that, and its body is never read.
- `BoardUnavailable` -- the board is there and refused to answer: a `5xx`, a
  `403`, a connection that failed. The message carries the status code and never
  the response body, because that message can end up on a run row (#9).
- `MalformedPayload` -- the board answered `2xx` with something that is not a
  list of postings.
- `SourceTimeout` -- the board did not answer within `REQUEST_TIMEOUT_SECONDS`,
  so a slow board cannot hold a tick of #9 open.

A board with no opening is an empty board: `[]` returns `[]` and raises nothing.
Importing this module builds nothing -- no client, no socket, no environment read
-- so `uvicorn` and the tests pay nothing for it.
"""

import json

import httpx2

from app.sources.base import (
    REQUEST_TIMEOUT_SECONDS,
    BoardUnavailable,
    MalformedPayload,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
    html_to_text,
    require_slug,
)

BOARD_URL = "https://api.lever.co/v0/postings/{slug}"

# The board answers JSON only when it is asked for; without it Lever sends HTML.
MODE_PARAMETER = {"mode": "json"}

# What joins the posting's own plain text and each `lists` entry, so the scorer
# reads the sections as separate paragraphs rather than one run-on line.
DESCRIPTION_SEPARATOR = "\n\n"


def fetch(slug: str, *, client: httpx2.Client | None = None) -> list[SourcePosting]:
    """Every posting on the board `slug` names, or a `SourceError` saying why not.

    One synchronous request, over `client` when one is injected and over a client
    built here otherwise. A value that cannot be a slug makes no request at all.
    """
    require_slug(slug)
    if client is None:
        client = httpx2.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        response = client.get(
            BOARD_URL.format(slug=slug),
            params=MODE_PARAMETER,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx2.TimeoutException as error:
        raise SourceTimeout(f"The board '{slug}' did not answer in time") from error
    except httpx2.HTTPError as error:  # no answer at all: a refused or broken connection
        raise BoardUnavailable(f"The board '{slug}' could not be reached") from error
    if response.status_code == 404:
        raise UnknownSlug(f"There is no Lever board named '{slug}'")
    if not response.is_success:
        raise BoardUnavailable(
            f"The board '{slug}' refused to answer: HTTP {response.status_code}"
        )
    return _postings(response.text)


def _postings(body: str) -> list[SourcePosting]:
    """The postings in a board's answer, or `MalformedPayload` naming what was wrong.

    Lever answers a bare array, so anything that is not a list -- an object, a
    string, an HTML page -- is the wrong shape rather than an empty board.
    """
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise MalformedPayload("The board's answer is not JSON") from error
    if not isinstance(payload, list):
        raise MalformedPayload("The board's answer is not a list of postings")
    return [_posting(posting) for posting in payload]


def _posting(posting: object) -> SourcePosting:
    """One posting as a `SourcePosting`. `id`, `text` and `hostedUrl` are required.

    `categories` and the two description fields are optional because Lever omits
    them, so a posting without a location is normal here rather than a broken one.
    """
    if not isinstance(posting, dict):
        raise MalformedPayload("A posting on the board is not an object")
    if (
        not isinstance(posting.get("id"), str)
        or not isinstance(posting.get("text"), str)
        or not isinstance(posting.get("hostedUrl"), str)
    ):
        raise MalformedPayload("A posting on the board carries no id, text or url")
    return SourcePosting(
        external_id=posting["id"],
        title=posting["text"],
        location=_location_name(posting.get("categories")),
        url=posting["hostedUrl"],
        description=_plain_text(posting),
    )


def _location_name(categories: object) -> str | None:
    """The `location` a posting's categories carry, or `None` when there is none to name."""
    location = categories.get("location") if isinstance(categories, dict) else None
    return location if isinstance(location, str) else None


def _plain_text(posting: dict) -> str | None:
    """The posting's plain text, or `None` when Lever sends none.

    `descriptionPlain` first, then each `lists` entry's `text` heading and its
    `content` with the tags stripped, in the order Lever sent them. The payload's
    `description` -- HTML -- is not the one used.
    """
    parts: list[str] = []
    plain = posting.get("descriptionPlain")
    if isinstance(plain, str):
        parts.append(plain)
    entries = posting.get("lists")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            heading = entry.get("text")
            if isinstance(heading, str):
                parts.append(heading)
            content = entry.get("content")
            if isinstance(content, str):
                parts.append(html_to_text(content))
    return DESCRIPTION_SEPARATOR.join(parts) if parts else None
