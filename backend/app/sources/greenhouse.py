"""A company's public Greenhouse board, turned into postings this app can store.

`fetch(slug)` makes one synchronous `GET` to
`https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` and returns
one `SourcePosting` per job on the board. `content=true` is not optional: without
it the payload carries no `content` at all, and the description is what the
prefilter (#7) and the scorer (#8) read. Nothing is written and no row is
touched: #9's tick calls `fetch`, #6 stores what it returns, and only the
watchlist (#5) has a caller to answer an `UnknownSlug` to.

This is the one module that knows the Greenhouse payload's shape -- its key
names, its escaped HTML, its numeric id -- and every value it returns is read
from the payload rather than rebuilt from the slug: a board's `absolute_url`
links to the company's own site as often as to `boards.greenhouse.io`, so a URL
synthesised from the slug would be wrong. `app.sources.base` holds what this
adapter and every other one share: `SourcePosting`, the error family, the slug
gate, the timeout and the html-to-text helper. No other module may know these
keys.

Every way a board can fail comes back as one of four `SourceError`s rather than
a crash or an `httpx2` exception, and no case raises the family's own parent:

- `UnknownSlug` -- the value cannot name a board, or no such board exists. A
  `404` decides that, and its body is never read: the body the source sends for
  a board that does not exist names a *job*, so it is no signal at all.
- `BoardUnavailable` -- the board is there and refused to answer: a `5xx`, a
  `403`, a connection that failed. The message carries the status code and never
  the response body, because that message can end up on a run row (#9) and the
  body can be arbitrary HTML.
- `MalformedPayload` -- the board answered `2xx` with something that is not a
  list of jobs.
- `SourceTimeout` -- the board did not answer within `REQUEST_TIMEOUT_SECONDS`,
  so a slow board cannot hold a tick of #9 open.

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

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

# A job carries `content` only when it is asked for; the description is what the
# prefilter and the scorer read, so every request asks for it.
CONTENT_PARAMETER = {"content": "true"}


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
            params=CONTENT_PARAMETER,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx2.TimeoutException as error:
        raise SourceTimeout(f"The board '{slug}' did not answer in time") from error
    except httpx2.HTTPError as error:  # no answer at all: a refused or broken connection
        raise BoardUnavailable(f"The board '{slug}' could not be reached") from error
    if response.status_code == 404:
        raise UnknownSlug(f"There is no Greenhouse board named '{slug}'")
    if not response.is_success:
        raise BoardUnavailable(
            f"The board '{slug}' refused to answer: HTTP {response.status_code}"
        )
    return _postings(response.text)


def _postings(body: str) -> list[SourcePosting]:
    """The postings in a board's answer, or `MalformedPayload` naming what was wrong.

    `meta` is deliberately not read. A body with no `meta`, or one whose `total`
    disagrees with the jobs beside it, is still whatever `jobs` says: the count is
    a convenience the source sends, not the list itself.
    """
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise MalformedPayload("The board's answer is not JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise MalformedPayload("The board's answer has no list of jobs")
    return [_posting(job) for job in payload["jobs"]]


def _posting(job: object) -> SourcePosting:
    """One job as a posting. `id`, `title` and `absolute_url` are the required fields.

    `location` and `content` are optional because Greenhouse omits them, so a
    posting without a location is normal here rather than a broken payload.
    """
    if not isinstance(job, dict):
        raise MalformedPayload("A job on the board is not an object")
    if (
        not isinstance(job.get("id"), int)
        or not isinstance(job.get("title"), str)
        or not isinstance(job.get("absolute_url"), str)
    ):
        raise MalformedPayload("A job on the board carries no id, title or url")
    content = job.get("content")
    return SourcePosting(
        external_id=str(job["id"]),
        title=job["title"],
        location=_location_name(job.get("location")),
        url=job["absolute_url"],
        description=html_to_text(content) if isinstance(content, str) else None,
    )


def _location_name(location: object) -> str | None:
    """The name a location carries, or `None` when there is no location to name."""
    name = location.get("name") if isinstance(location, dict) else None
    return name if isinstance(name, str) else None
