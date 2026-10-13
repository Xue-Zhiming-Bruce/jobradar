"""The retry rule, the named waits, the body cap, and what none of them may say out loud.

Every case here drives the real adapters -- `greenhouse.fetch` and `lever.fetch`
-- through an injected `httpx2.MockTransport`, so what is under test is the one
`app.sources.base.fetch_body` both of them call: an adapter that grew a request
path of its own fails here rather than quietly passing, and the request counts are
read from the transport's own record rather than from a flag.

No server is started and no test opens a socket (`socket.socket.connect` is
patched to raise). No test waits either: the autouse `no_real_sleep` fixture below
turns a real `time.sleep` into a failure, and every case that retries passes
`sleep` a recorder list and then asserts exactly the waits it collected.

The boards are the two committed fixtures, which hold public board content only --
no key, no secret and no personal data -- and both hold two postings, so one
assertion covers which of them answered.
"""

import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes.watchlist import get_source
from app.sources import greenhouse, lever
from app.sources.base import (
    ETAG_HEADER,
    IF_MODIFIED_SINCE_HEADER,
    IF_NONE_MATCH_HEADER,
    LAST_MODIFIED_HEADER,
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    BoardAnswer,
    BoardPostings,
    BoardShrunk,
    BoardUnavailable,
    MalformedPayload,
    NotModified,
    OversizedPayload,
    SourceError,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
    fetch_body,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The slug each fixture was recorded from, and the two adapter/board pairs every
# matrix below is driven over: the behaviour belongs to the helper, so a case that
# passes for one adapter and not the other fails here.
GREENHOUSE_SLUG = "acme"
LEVER_SLUG = "leverdemo"
ADAPTERS = [
    pytest.param(
        greenhouse,
        GREENHOUSE_SLUG,
        (FIXTURES / "greenhouse" / "board.json").read_bytes(),
        id="greenhouse",
    ),
    pytest.param(
        lever, LEVER_SLUG, (FIXTURES / "lever" / "board.json").read_bytes(), id="lever"
    ),
]

# A string a board's bodies carry that must reach no message, no row and no
# response: it stands in for whatever text a board sends, which can be arbitrary
# HTML.
SENTINEL = "PAYLOAD-SENTINEL-TEXT-THAT-MUST-NOT-BE-ECHOED"

# An over-cap body arrives in chunks of this size, so the cap bites on the fourth
# one: a response holding more chunks than that proves fewer were pulled than it had.
CHUNK_BYTES = MAX_BODY_BYTES // 4 + 1
CHUNK_COUNT = 6
BODY_URL = "https://boards.example.test/jobs"


class Board:
    """An `httpx2` transport that answers a script and records every request it was asked.

    The last answer is served for every later request, so "a 500 on every attempt"
    is written once, and an entry may be an exception to raise instead of a
    response to serve, which is how a connection that never opens is served.
    """

    def __init__(self, *answers: httpx2.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self) -> httpx2.Client:
        """A client that reaches this script and nothing else: no socket is ever opened."""
        return httpx2.Client(transport=httpx2.MockTransport(self))


class RefusingStream(httpx2.SyncByteStream):
    """A body nothing may iterate: a declared length already refused the answer."""

    def __iter__(self) -> Iterator[bytes]:
        raise AssertionError("the body was read although its Content-Length was over the cap")
        yield b""  # pragma: no cover -- the line above always raises


class ChunkedStream(httpx2.SyncByteStream):
    """A body that arrives in chunks and records every one pulled out of it."""

    def __init__(self, first: bytes = b"") -> None:
        self.chunks = [first + b"x" * (CHUNK_BYTES - len(first))] * CHUNK_COUNT
        self.pulled: list[bytes] = []

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.pulled.append(chunk)
            yield chunk


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A case that reaches a real wait fails instead of waiting.

    Replacing `time.sleep` alone would not bite: a default argument is bound when
    its module is imported, so the `sleep=time.sleep` defaults are replaced too.
    Every retrying case below therefore injects a recorder, and one that forgot
    would fail here rather than sleep for a second and a half.
    """

    def refuse(seconds: float) -> None:
        raise AssertionError(f"this test tried to wait {seconds}s")

    monkeypatch.setattr(time, "sleep", refuse)
    for function in (fetch_body, greenhouse.fetch, lever.fetch):
        monkeypatch.setitem(function.__kwdefaults__, "sleep", refuse)


# What a retry can heal: the waits are named, bounded and exactly the injected list.


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_two_refusals_then_an_answer_returns_the_postings_after_exactly_three_requests(
    adapter: object, slug: str, body: bytes
) -> None:
    waits: list[float] = []
    board = Board(
        httpx2.Response(503), httpx2.Response(503), httpx2.Response(200, content=body)
    )

    postings = adapter.fetch(slug, client=board.client(), sleep=waits.append).postings

    assert isinstance(postings, list)
    assert len(postings) == 2
    assert all(isinstance(posting, SourcePosting) for posting in postings)
    assert len(board.requests) == 3
    assert waits == [0.5, 1.0]
    assert waits == [RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2]
    assert [request.extensions["timeout"]["connect"] for request in board.requests] == [
        REQUEST_TIMEOUT_SECONDS
    ] * 3
    assert [request.extensions["timeout"]["read"] for request in board.requests] == [
        REQUEST_TIMEOUT_SECONDS
    ] * 3


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_5xx_on_every_attempt_is_a_board_unavailable_after_three_requests(
    adapter: object, slug: str, body: bytes
) -> None:
    waits: list[float] = []
    board = Board(httpx2.Response(500, text=f"<html>{SENTINEL}</html>"))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is BoardUnavailable
    assert "500" in str(raised.value)
    assert len(board.requests) == RETRY_ATTEMPTS == 3
    assert waits == [0.5, 1.0]


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_connection_that_never_opens_is_a_board_unavailable_after_three_requests(
    adapter: object, slug: str, body: bytes
) -> None:
    waits: list[float] = []
    board = Board(httpx2.ConnectError("no route to host"))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is BoardUnavailable
    assert "no route to host" not in str(raised.value)
    assert len(board.requests) == RETRY_ATTEMPTS
    assert waits == [0.5, 1.0]


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ConnectTimeout("slow"),
        httpx2.ReadTimeout("slow"),
        httpx2.WriteTimeout("slow"),
    ],
    ids=["a connect timeout", "a read timeout", "a write timeout"],
)
@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_timeout_on_every_attempt_is_a_source_timeout_after_three_requests(
    adapter: object, slug: str, body: bytes, error: httpx2.TimeoutException
) -> None:
    waits: list[float] = []
    board = Board(error)

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is SourceTimeout
    assert slug in str(raised.value)
    assert len(board.requests) == RETRY_ATTEMPTS
    assert waits == [0.5, 1.0]


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_retry_that_answers_404_ends_the_loop_at_two_requests(
    adapter: object, slug: str, body: bytes
) -> None:
    """A later answer ends the loop where it stands: no third try for this board."""
    waits: list[float] = []
    board = Board(httpx2.Response(503), httpx2.Response(404))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is UnknownSlug
    assert len(board.requests) == 2
    assert waits == [0.5]


# What a retry cannot heal: one request, one typed refusal.


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_404_is_an_unknown_slug_after_exactly_one_request(
    adapter: object, slug: str, body: bytes
) -> None:
    waits: list[float] = []
    board = Board(httpx2.Response(404, text=f"<html>{SENTINEL}</html>"))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is UnknownSlug
    assert len(board.requests) == 1
    assert waits == []


@pytest.mark.parametrize("status", [403, 422, 429])
@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_any_other_4xx_is_a_board_unavailable_after_exactly_one_request(
    adapter: object, slug: str, body: bytes, status: int
) -> None:
    """`429` included: a retry loop aimed at a rate limiter is worse than one refusal."""
    waits: list[float] = []
    board = Board(httpx2.Response(status, text=f"<html>{SENTINEL}</html>"))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is BoardUnavailable
    assert str(status) in str(raised.value)
    assert len(board.requests) == 1
    assert waits == []


MALFORMED_ANSWERS = [
    pytest.param(
        greenhouse,
        GREENHOUSE_SLUG,
        b"<html><body>502 Bad Gateway</body></html>",
        id="an HTML error page for greenhouse",
    ),
    pytest.param(
        lever, LEVER_SLUG, b'{"ok": false, "error": "not a board"}', id="a JSON object for lever"
    ),
]


@pytest.mark.parametrize("adapter, slug, body", MALFORMED_ANSWERS)
def test_a_2xx_of_the_wrong_shape_is_that_adapter_s_malformed_payload_after_one_request(
    adapter: object, slug: str, body: bytes
) -> None:
    waits: list[float] = []
    board = Board(httpx2.Response(200, content=body))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is MalformedPayload
    assert len(board.requests) == 1
    assert waits == []


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_body_over_the_cap_is_refused_after_exactly_one_request(
    adapter: object, slug: str, body: bytes
) -> None:
    """A bigger body is not tried again: the same board answers the same size."""
    waits: list[float] = []
    board = Board(
        httpx2.Response(
            200,
            headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
            stream=RefusingStream(),
        )
    )

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is OversizedPayload
    assert len(board.requests) == 1
    assert waits == []


# The cap bites while the body is arriving, and exactly at the cap is not over it.


def test_a_declared_length_over_the_cap_is_refused_without_reading_the_body() -> None:
    """The body exists only as a stream that fails this test if anything iterates it."""
    board = Board(
        httpx2.Response(
            200,
            headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
            stream=RefusingStream(),
        )
    )

    with pytest.raises(SourceError) as raised:
        fetch_body("acme", BODY_URL, params={}, client=board.client())

    assert type(raised.value) is OversizedPayload
    assert len(board.requests) == 1


def test_a_chunked_body_over_the_cap_is_refused_after_pulling_fewer_chunks_than_it_holds() -> None:
    stream = ChunkedStream()
    board = Board(httpx2.Response(200, stream=stream))

    with pytest.raises(SourceError) as raised:
        fetch_body("acme", BODY_URL, params={}, client=board.client())

    assert type(raised.value) is OversizedPayload
    assert len(stream.pulled) < len(stream.chunks)
    assert len(board.requests) == 1


def test_a_body_exactly_at_the_cap_is_accepted() -> None:
    """`>` and not `>=`: the cap bounds one more byte than a body its own size."""
    body = b"y" * MAX_BODY_BYTES
    board = Board(httpx2.Response(200, content=body))

    answer = fetch_body("acme", BODY_URL, params={}, client=board.client())

    assert answer.body == body.decode()


# No payload text leaves the helper: neither on a raised message nor on the wire.


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_refusing_board_s_own_text_never_reaches_the_raised_message(
    adapter: object, slug: str, body: bytes
) -> None:
    board = Board(httpx2.Response(503, text=f"<html><body>{SENTINEL}</body></html>"))
    waits: list[float] = []

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is BoardUnavailable
    assert "503" in str(raised.value)
    assert SENTINEL not in str(raised.value)
    assert len(board.requests) == RETRY_ATTEMPTS


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_an_oversized_body_s_own_text_never_reaches_the_raised_message(
    adapter: object, slug: str, body: bytes
) -> None:
    stream = ChunkedStream(SENTINEL.encode())
    board = Board(httpx2.Response(200, stream=stream))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=lambda seconds: None)

    assert type(raised.value) is OversizedPayload
    assert stream.pulled, "the body has to have been read for the assertion to mean anything"
    assert SENTINEL not in str(raised.value)


@pytest.fixture
def probe_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app over a temporary SQLite file, the way the watchlist tests drive it."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    with TestClient(app) as test_client:
        yield test_client


PROBE_ANSWERS: list[Callable[[], Board]] = [
    lambda: Board(httpx2.Response(503, text=f"<html>{SENTINEL}</html>")),
    lambda: Board(httpx2.Response(200, stream=ChunkedStream(SENTINEL.encode()))),
]


@pytest.mark.parametrize("board_factory", PROBE_ANSWERS, ids=["exhausted", "oversized"])
def test_the_watchlist_probe_answers_a_board_it_could_not_read_as_502_and_never_404(
    probe_client: TestClient, board_factory: Callable[[], Board]
) -> None:
    """`OversizedPayload` is a board that could not be read: `502`, and no body in the detail."""
    board = board_factory()
    client = board.client()

    def probe(slug: str) -> list[SourcePosting]:
        return greenhouse.fetch(slug, client=client, sleep=lambda seconds: None)

    app.dependency_overrides[get_source] = lambda: {"greenhouse": probe}
    try:
        response = probe_client.post("/watchlist", json={"name": "Acme Inc", "slug": "acme"})
    finally:
        app.dependency_overrides.pop(get_source, None)

    assert response.status_code == 502
    assert "could not be read" in response.json()["detail"]
    assert SENTINEL not in response.text


# The conditional request: the validator a company stores, sent back, and the 304 it earns.


# A validator as a board would send it: an `ETag` keeps its quotes, and a
# `Last-Modified` is the date string the board chose.
STORED_ETAG = 'W/"v1"'
STORED_LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"

_VALIDATOR_CASES = [
    pytest.param(STORED_ETAG, None, {IF_NONE_MATCH_HEADER: STORED_ETAG}, id="an etag only"),
    pytest.param(
        None,
        STORED_LAST_MODIFIED,
        {IF_MODIFIED_SINCE_HEADER: STORED_LAST_MODIFIED},
        id="a date only",
    ),
    pytest.param(
        STORED_ETAG,
        STORED_LAST_MODIFIED,
        {IF_NONE_MATCH_HEADER: STORED_ETAG, IF_MODIFIED_SINCE_HEADER: STORED_LAST_MODIFIED},
        id="both",
    ),
]


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_first_run_asks_an_unconditional_question(
    adapter: object, slug: str, body: bytes
) -> None:
    """No validator stored, so neither conditional header is sent -- read from the request."""
    board = Board(httpx2.Response(200, content=body))

    answer = adapter.fetch(slug, client=board.client(), sleep=lambda seconds: None)

    assert len(board.requests) == 1
    request = board.requests[0]
    assert IF_NONE_MATCH_HEADER not in request.headers
    assert IF_MODIFIED_SINCE_HEADER not in request.headers
    assert (answer.etag, answer.last_modified) == (None, None)


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
@pytest.mark.parametrize("etag, last_modified, sent", _VALIDATOR_CASES)
def test_a_stored_validator_is_sent_back_and_each_header_only_when_its_value_is(
    adapter: object,
    slug: str,
    body: bytes,
    etag: str | None,
    last_modified: str | None,
    sent: dict[str, str],
) -> None:
    """The value as the board sent it, quotes and all, and each header on its own."""
    board = Board(httpx2.Response(304))

    answer = adapter.fetch(
        slug,
        etag=etag,
        last_modified=last_modified,
        client=board.client(),
        sleep=lambda seconds: None,
    )

    assert type(answer) is NotModified
    assert len(board.requests) == 1
    request = board.requests[0]
    conditional = (IF_NONE_MATCH_HEADER, IF_MODIFIED_SINCE_HEADER)
    assert {name: request.headers[name] for name in conditional if name in request.headers} == sent


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_an_empty_validator_asks_no_conditional_question(
    adapter: object, slug: str, body: bytes
) -> None:
    """`value or None` on the way in as well: an empty conditional header is never sent."""
    board = Board(httpx2.Response(200, content=body))

    adapter.fetch(slug, etag="", last_modified="", client=board.client(), sleep=lambda _: None)

    request = board.requests[0]
    assert IF_NONE_MATCH_HEADER not in request.headers
    assert IF_MODIFIED_SINCE_HEADER not in request.headers


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_304_to_a_conditional_request_is_not_modified_after_one_request_and_no_wait(
    adapter: object, slug: str, body: bytes
) -> None:
    """A returned outcome: not one of the six error classes, and the loop does not try again."""
    waits: list[float] = []
    board = Board(httpx2.Response(304))

    answer = adapter.fetch(slug, etag=STORED_ETAG, client=board.client(), sleep=waits.append)

    assert type(answer) is NotModified
    assert not isinstance(answer, SourceError)
    family = (
        UnknownSlug,
        BoardUnavailable,
        MalformedPayload,
        SourceTimeout,
        OversizedPayload,
        BoardShrunk,
    )
    assert not any(isinstance(answer, error) for error in family)
    assert len(board.requests) == 1
    assert waits == []


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_304_to_a_request_that_carried_no_validator_is_a_protocol_violation(
    adapter: object, slug: str, body: bytes
) -> None:
    """Never read as "not modified": a board this app holds no validator for could not be read."""
    waits: list[float] = []
    board = Board(httpx2.Response(304))

    with pytest.raises(SourceError) as raised:
        adapter.fetch(slug, client=board.client(), sleep=waits.append)

    assert type(raised.value) is BoardUnavailable
    assert "304" in str(raised.value)
    assert len(board.requests) == 1
    assert waits == []


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_retry_can_end_in_a_304_and_the_304_is_where_the_loop_ends(
    adapter: object, slug: str, body: bytes
) -> None:
    waits: list[float] = []
    board = Board(httpx2.Response(503), httpx2.Response(304))

    answer = adapter.fetch(slug, etag=STORED_ETAG, client=board.client(), sleep=waits.append)

    assert type(answer) is NotModified
    assert len(board.requests) == 2
    assert waits == [RETRY_BACKOFF_SECONDS] == [0.5]


def test_the_helper_answers_with_the_body_and_the_validator_it_arrived_with() -> None:
    board = Board(
        httpx2.Response(
            200,
            text="a body",
            headers={ETAG_HEADER: STORED_ETAG, LAST_MODIFIED_HEADER: STORED_LAST_MODIFIED},
        )
    )

    answer = fetch_body("acme", BODY_URL, params={}, client=board.client())

    assert type(answer) is BoardAnswer
    assert (answer.body, answer.etag, answer.last_modified) == (
        "a body",
        STORED_ETAG,
        STORED_LAST_MODIFIED,
    )


@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_200_hands_the_boards_own_validator_out_verbatim(
    adapter: object, slug: str, body: bytes
) -> None:
    """Nothing parsed and no quote stripped: the value goes back out as it arrived."""
    board = Board(
        httpx2.Response(
            200,
            content=body,
            headers={ETAG_HEADER: STORED_ETAG, LAST_MODIFIED_HEADER: STORED_LAST_MODIFIED},
        )
    )

    answer = adapter.fetch(slug, client=board.client(), sleep=lambda seconds: None)

    assert type(answer) is BoardPostings
    assert (answer.etag, answer.last_modified) == (STORED_ETAG, STORED_LAST_MODIFIED)
    assert len(answer.postings) == 2


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no validator headers at all"),
        pytest.param(
            {ETAG_HEADER: "", LAST_MODIFIED_HEADER: ""}, id="an empty one on each"
        ),
    ],
)
@pytest.mark.parametrize("adapter, slug, body", ADAPTERS)
def test_a_200_that_sent_no_usable_validator_hands_out_none_for_both(
    adapter: object, slug: str, body: bytes, headers: dict[str, str]
) -> None:
    board = Board(httpx2.Response(200, content=body, headers=headers))

    answer = adapter.fetch(slug, client=board.client(), sleep=lambda seconds: None)

    assert (answer.etag, answer.last_modified) == (None, None)
