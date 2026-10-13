"""What every board adapter returns and how every one of them fails.

Two adapters present one shape and one family of failures to the rest of the app,
so both live here rather than in either adapter: `SourcePosting`, the five fields
`ingest` (#6) stores; `SourceError` and its four members, the family the tick (#9)
catches and `POST /watchlist` (#5) maps to `404` and `502`; the slug gate every
adapter calls before it builds a URL; the request timeout both read; the
html-to-text helper both strip markup with; and `Source`, the one-call interface
`SOURCES` in `app.sources` resolves to.

This module names no board's payload, no board's address and no board's key names:
an adapter is a module, a registry entry and a stored name, so a third source adds
one of each and no change here. Importing it builds nothing -- no client, no
socket, no environment read.

The four failures, and the one parent no case raises:

- `UnknownSlug` -- the value cannot name a board, or no such board exists. The
  watchlist has one thing to report either way: there is no board to poll.
- `BoardUnavailable` -- the board is there and refused to answer: a 5xx, a 403, a
  connection that failed. The message carries the status and never the body.
- `MalformedPayload` -- the board answered `2xx` with something that is not a list
  of postings.
- `SourceTimeout` -- the board did not answer within `REQUEST_TIMEOUT_SECONDS`.
"""

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Protocol

# One constant for the request and for the client an adapter builds when none is
# injected: a board that hangs is a `SourceTimeout` in ten seconds, not a tick
# that outlives an HTTP timeout.
REQUEST_TIMEOUT_SECONDS = 10.0

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


class Source(Protocol):
    """One board adapter: a slug in, that board's postings out, or a `SourceError`.

    `SOURCES` maps a stored name to one of these, and both the watchlist probe and
    the tick call through it. Each adapter keeps its own `client` keyword -- the
    way a test stays off the network -- but a caller passes only the slug.
    """

    def __call__(self, slug: str) -> list[SourcePosting]: ...


def require_slug(slug: str) -> None:
    """Refuse a value that cannot name a board, before any adapter builds a URL."""
    if not SLUG_PATTERN.fullmatch(slug):
        raise UnknownSlug("A board slug may hold only letters, digits, '.', '_' and '-'")


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
