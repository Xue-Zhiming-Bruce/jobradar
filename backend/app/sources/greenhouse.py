"""A company's public Greenhouse board, turned into postings this app can store.

`fetch(slug)` makes one synchronous `GET` to
`https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` and returns
one `SourcePosting` per job on the board. `content=true` is not optional: without
it the payload carries no `content` at all, and the description is what the
prefilter (#7) and the scorer (#8) read. Nothing is written and no row is
touched: #9's tick calls `fetch`, #6 stores what it returns, and only the
watchlist (#5) has a caller to answer an `UnknownSlug` to.

This is the one module that knows the payload's shape -- its key names, its
escaped HTML, its numeric id -- and every value it returns is read from the
payload rather than rebuilt from the slug: a board's `absolute_url` links to the
company's own site as often as to `boards.greenhouse.io`, so a URL synthesised
from the slug would be wrong. No other module may know these keys; the second
adapter and whatever the two sources share arrive with #28.

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

The timeout is `REQUEST_TIMEOUT_SECONDS`, read here and in no other module: this
task adds no configuration. Importing this module builds nothing -- no client, no
socket, no environment read -- so `uvicorn` and the tests pay nothing for it.
"""

import json
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

import httpx2

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

# A job carries `content` only when it is asked for; the description is what the
# prefilter and the scorer read, so every request asks for it.
CONTENT_PARAMETER = {"content": "true"}

# One constant for the request and for the client `fetch` builds when none is
# injected: a board that hangs is a `SourceTimeout` in ten seconds, not a tick
# that outlives an HTTP timeout.
REQUEST_TIMEOUT_SECONDS = 10.0

# What a slug can be: the characters Greenhouse puts in a board name. Anything
# else -- blank, a space, a `/`, a `?` -- is refused before it can reach a URL,
# because a slug is not allowed to choose the host a request goes to. Case is
# left alone: `Acme` is sent as `Acme`, because `app.models.Company` normalises
# the column and a board's spelling is the source's business, not this module's.
SLUG_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class SourceError(Exception):
    """A board did not yield its postings.

    One family, so the tick (#9) catches the four failures the same way and never
    an `httpx2` exception. `UnknownSlug` is the one #5 answers on.
    """


class UnknownSlug(SourceError):
    """This value cannot name a Greenhouse board, or no board goes by that name.

    Raised before the network for a value that is not slug-shaped, and for the
    source's `404`. Both are one error because the watchlist has one thing to
    report: either way, there is no board to poll.
    """


class BoardUnavailable(SourceError):
    """The board is there and refused to be read: a 5xx, a 403, a connection that failed."""


class MalformedPayload(SourceError):
    """The board answered `2xx` with something that is not a list of jobs."""


class SourceTimeout(SourceError):
    """The board did not answer in time."""


@dataclass(frozen=True)
class SourcePosting:
    """One posting as the board gave it, in the shape `posting` (#2) stores.

    `external_id` is the payload's numeric `id` rendered as a string, because the
    column is `String(255)` and #6 dedupes on it. `url` is the payload's
    `absolute_url` verbatim. `location` and `description` are `None` when the
    payload does not carry them, which is a normal posting and not a malformed
    one. The rest of the payload -- `updated_at`, `internal_job_id`, the
    compliance block -- is #6's business, and `fetch` returns values without
    writing a row.
    """

    external_id: str
    title: str
    location: str | None
    url: str
    description: str | None


def fetch(slug: str, *, client: httpx2.Client | None = None) -> list[SourcePosting]:
    """Every posting on the board `slug` names, or a `SourceError` saying why not.

    One synchronous request, over `client` when one is injected and over a client
    built here otherwise. A value that cannot be a slug makes no request at all.
    """
    if not SLUG_PATTERN.fullmatch(slug):
        raise UnknownSlug(
            "A board slug may hold only letters, digits, '.', '_' and '-'"
        )
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
        description=_visible_text(content) if isinstance(content, str) else None,
    )


def _location_name(location: object) -> str | None:
    """The name a location carries, or `None` when there is no location to name."""
    name = location.get("name") if isinstance(location, dict) else None
    return name if isinstance(name, str) else None


def _visible_text(content: str) -> str:
    """The text a reader sees: the payload's HTML decoded, then stripped of its tags.

    `content` arrives escaped -- `&lt;h2&gt;` -- so it is unescaped into real HTML
    first and then walked with the standard library's parser, which also decodes
    the entities that HTML itself carries (`&amp;nbsp;`). What is left is what the
    scorer (#8) should read: the sentences, and no `<h2>` and no `&lt;h2&gt;`.
    """
    parser = _VisibleText()
    parser.feed(unescape(content))
    return parser.text


class _VisibleText(HTMLParser):
    """Collects the data between tags, and drops the tags themselves."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)
