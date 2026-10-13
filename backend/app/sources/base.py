"""What every board adapter returns, how every one of them fails, and the one request they make.

Two adapters present one shape and one family of failures to the rest of the app,
so both live here rather than in either adapter: `SourcePosting`, the five fields
`ingest` (#6) stores; `BoardAnswer` and `BoardPostings`, the postings an answer
carried and the board's own validator beside them; `NotModified`, the `304` that
is a returned outcome rather than a failure; `SourceError` and its six members,
the family the tick (#9) catches and `POST /watchlist` (#5) maps to `404` and
`502`; the slug gate every adapter calls before it builds a URL; `fetch_body`,
the one conditional `GET` both make, with the timeout, the bounded retry and the
body cap; the html-to-text helper both strip markup with; and `Source`, the
one-call interface `SOURCES` in `app.sources` resolves to.

This module names no board's payload, no board's address and no board's key names:
an adapter is a module, a registry entry and a stored name, so a third source adds
one of each and no change here. Importing it builds nothing -- no client, no
socket, no environment read.

The six failures, and the one parent no case raises:

- `UnknownSlug` -- the value cannot name a board, or no such board exists. The
  watchlist has one thing to report either way: there is no board to poll.
- `BoardUnavailable` -- the board is there and refused to answer: a 5xx, a 403, a
  connection that failed. The message carries the status and never the body.
- `MalformedPayload` -- the board answered `2xx` with something that is not a list
  of postings.
- `SourceTimeout` -- the board did not answer within `REQUEST_TIMEOUT_SECONDS`.
- `OversizedPayload` -- the board's body passed `MAX_BODY_BYTES`. The same board
  answers the same size, so it is never retried.
- `BoardShrunk` -- the board listed far fewer postings than the company has open.
  The walk's (#34) rather than an adapter's: only the walk can see the stored
  postings a payload would otherwise close.
"""

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Protocol

import httpx2

# One constant for the request and for the client an adapter builds when none is
# injected: a board that hangs is a `SourceTimeout` in ten seconds, not a tick
# that outlives an HTTP timeout.
REQUEST_TIMEOUT_SECONDS = 10.0

# One try and two retries, the waits doubling from the base with no jitter, so the
# waits are exactly 0.5s and 1.0s and a test can assert the list. One board's worst
# case is bounded by construction: 3 x REQUEST_TIMEOUT_SECONDS + 1.5s = 31.5s.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5

# The memory bound on one answer, enforced while reading rather than after it: it is
# a bound and not a filter, and a real board is far below it.
MAX_BODY_BYTES = 8 * 1024 * 1024

# The two conditional headers a stored validator asks with, and the two an answer
# carries it in. Spelled once, so nothing retypes a header name.
IF_NONE_MATCH_HEADER = "If-None-Match"
IF_MODIFIED_SINCE_HEADER = "If-Modified-Since"
ETAG_HEADER = "ETag"
LAST_MODIFIED_HEADER = "Last-Modified"

# What a slug can be: the characters a board puts in a board name. Anything else
# -- blank, a space, a `/`, a `?` -- is refused before it can reach a URL, because
# a slug is not allowed to choose the host a request goes to. Case is left alone:
# `Acme` is sent as `Acme`, because `app.models.Company` normalises the column and
# a board's spelling is the source's business, not this module's.
SLUG_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class SourceError(Exception):
    """A board did not yield its postings.

    One family, so the tick (#9) catches the four failures the same way and never
    an `httpx2` exception. `UnknownSlug` is the one #5 answers on.
    """


class UnknownSlug(SourceError):
    """This value cannot name a board, or no board goes by that name.

    Raised before the network for a value that is not slug-shaped, and for the
    source's `404`. Both are one error because the watchlist has one thing to
    report: either way, there is no board to poll.
    """


class BoardUnavailable(SourceError):
    """The board is there and refused to be read: a 5xx, a 403, a connection that failed."""


class MalformedPayload(SourceError):
    """The board answered `2xx` with something that is not a list of postings."""


class SourceTimeout(SourceError):
    """The board did not answer in time."""


class OversizedPayload(SourceError):
    """The board's body passed `MAX_BODY_BYTES`, so it was never fully read or parsed."""


class BoardShrunk(SourceError):
    """The board listed far fewer postings than the company has open postings.

    The walk's refusal rather than an adapter's, and the one family member a fetch
    never raises: a payload listing fewer than half of the company's open postings
    is not trusted to close any of them.
    """


@dataclass(frozen=True)
class SourcePosting:
    """One posting as the board gave it, in the shape `posting` (#2) stores.

    `external_id` is the board's own id rendered as a string, because the column
    is `String(255)` and #6 dedupes on it. `url` is the board's own url verbatim,
    whatever host it names: a URL synthesised from the slug would be wrong.
    `location` and `description` are `None` when the payload does not carry them,
    which is a normal posting and not a malformed one. A source's own extras are
    its own business and stop at its adapter; `fetch` writes no row.
    """

    external_id: str
    title: str
    location: str | None
    url: str
    description: str | None


@dataclass(frozen=True)
class BoardAnswer:
    """One board's body, and the validator that body arrived with.

    `etag` and `last_modified` are the board's own opaque strings, echoed as they
    arrived -- an `ETag` keeps its quotes -- or `None` when the answer carried
    neither (or an empty one). Neither is parsed: the value exists to be sent back
    in `If-None-Match`/`If-Modified-Since` and for nothing else.
    """

    body: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class BoardPostings:
    """An adapter's answer: the postings it parsed, plus the validator they came with."""

    postings: list[SourcePosting]
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class NotModified:
    """The board answered `304` to a request that carried a validator: nothing changed.

    A returned outcome and never an error: it is not a `SourceError`, so nothing
    fails and no route answers `502` on account of it, and it is never retried.
    The walk (#55) skips the company it names, so a board that was not read closes
    nothing.
    """


class Source(Protocol):
    """One board adapter: a slug in, that board's postings out, or a `SourceError`.

    `SOURCES` maps a stored name to one of these, and both the watchlist probe and
    the tick call through it. The walk hands the stored validator back as the two
    keyword-only parameters, and gets either the postings that validator names or
    the `NotModified` that means the board has not changed. Each adapter keeps its
    own `client` and `sleep` keywords -- the way a test stays off the network and
    off the clock.
    """

    def __call__(
        self,
        slug: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> BoardPostings | NotModified: ...


def require_slug(slug: str) -> None:
    """Refuse a value that cannot name a board, before any adapter builds a URL."""
    if not SLUG_PATTERN.fullmatch(slug):
        raise UnknownSlug("A board slug may hold only letters, digits, '.', '_' and '-'")


def fetch_body(
    slug: str,
    url: str,
    *,
    params: Mapping[str, str],
    etag: str | None = None,
    last_modified: str | None = None,
    client: httpx2.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> BoardAnswer | NotModified:
    """One board's answer, or a `SourceError` -- never an `httpx2` exception.

    One `GET`, over `client` when one is injected and over a client built here
    otherwise, carrying `REQUEST_TIMEOUT_SECONDS` on every attempt. The request is
    conditional exactly as far as the stored validator goes: `If-None-Match` when
    `etag` is stored, `If-Modified-Since` when `last_modified` is, each on its own,
    and neither when the company holds no validator.

    A `304` to that conditional request is `NotModified`, returned where it stands:
    it is a cached answer and not a failure, so it is not retried and no wait
    happens. A `304` to a request that carried no validator is a protocol
    violation and not a cached answer -- reading it as "not modified" would skip a
    board this app holds no validator for on every later run -- so it keeps the
    behaviour every other unexpected status has: a `BoardUnavailable` naming HTTP
    304, after one request and no wait.

    A failure a retry can heal -- a connection error, a read/connect/write timeout,
    any `5xx` -- is tried `RETRY_ATTEMPTS` times in all, waiting
    `RETRY_BACKOFF_SECONDS` and then twice that; `sleep` is the seam a test
    replaces, so a real wait is only ever the default. Nothing else is retried: a
    `404` is an `UnknownSlug` at once, every other `4xx` -- `429` included -- is a
    `BoardUnavailable` at once, and an oversized body is refused without asking the
    same board the same thing again. A later answer ends the loop where it stands,
    so a retry that answers `404` is an `UnknownSlug` after two requests rather
    than a third try. A value that cannot be a slug makes no request at all.

    The body is read here rather than by the client, because `MAX_BODY_BYTES` has
    to bite while the answer is still arriving: `_body_within_cap` enforces it. A
    `200` answers with the body and the two validator headers the board sent, each
    verbatim and each `None` when it sent none.
    """
    require_slug(slug)
    if client is None:
        client = httpx2.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    conditional = _conditional_headers(etag, last_modified)
    failure: SourceError
    cause: Exception | None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            # `stream`, not `get`: a client that read the body itself would buffer a
            # payload this helper has not yet measured against the cap.
            with client.stream(
                "GET",
                url,
                params=params,
                headers=conditional or None,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code == 304 and conditional:
                    return NotModified()
                if response.status_code == 404:
                    raise UnknownSlug(f"There is no board named '{slug}'")
                if response.is_success:
                    return BoardAnswer(
                        _body_within_cap(response, slug),
                        response.headers.get(ETAG_HEADER) or None,
                        response.headers.get(LAST_MODIFIED_HEADER) or None,
                    )
                failure = BoardUnavailable(
                    f"The board '{slug}' refused to answer: HTTP {response.status_code}"
                )
                cause = None
                if response.status_code < 500:
                    raise failure
        except httpx2.TimeoutException as error:
            failure = SourceTimeout(f"The board '{slug}' did not answer in time")
            cause = error
        except httpx2.HTTPError as error:  # no answer at all: a refused or broken connection
            failure = BoardUnavailable(f"The board '{slug}' could not be reached")
            cause = error
        if attempt + 1 == RETRY_ATTEMPTS:
            raise failure from cause
        sleep(RETRY_BACKOFF_SECONDS * 2**attempt)
    # Unreachable while `RETRY_ATTEMPTS` is at least one: every attempt raises or returns.
    raise AssertionError(f"RETRY_ATTEMPTS must be at least 1, not {RETRY_ATTEMPTS}")


def _conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    """The conditional headers a stored validator asks with: each only when its value is stored.

    An empty value asks nothing, the way an answer's empty one is stored as `None`,
    so no empty conditional header is ever sent.
    """
    headers: dict[str, str] = {}
    if etag:
        headers[IF_NONE_MATCH_HEADER] = etag
    if last_modified:
        headers[IF_MODIFIED_SINCE_HEADER] = last_modified
    return headers


def _body_within_cap(response: httpx2.Response, slug: str) -> str:
    """The answer's text, refused the moment it passes `MAX_BODY_BYTES`.

    A declared `Content-Length` over the cap is refused before one byte of the body
    is read; a body that arrives in chunks is refused as soon as the bytes
    accumulated pass the cap, so the whole answer is never buffered and never
    parsed. The comparison is `>` and not `>=`, so a body exactly at the cap is
    accepted. The message names the board and the cap, and nothing from the body.
    """
    refusal = f"The board '{slug}' answered with more than {MAX_BODY_BYTES} bytes"
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise OversizedPayload(refusal)
    content = bytearray()
    for chunk in response.iter_bytes():
        content.extend(chunk)
        if len(content) > MAX_BODY_BYTES:
            raise OversizedPayload(refusal)
    return content.decode(response.encoding or "utf-8", errors="replace")


def html_to_text(content: str) -> str:
    """The text a reader sees: the payload's markup decoded, then stripped of its tags.

    Some boards send `content` escaped -- `&lt;h2&gt;` -- and some send real HTML,
    so the value is unescaped into real HTML first and then walked with the
    standard library's parser, which also decodes the entities that HTML itself
    carries (`&amp;nbsp;`). What is left is what the scorer (#8) should read: the
    sentences, and no `<h2>` and no `&lt;h2&gt;`.
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
