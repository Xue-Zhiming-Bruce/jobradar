"""The Lever adapter, driven by a recorded board and never by the network.

The endpoint under test is
`GET https://api.lever.co/v0/postings/{slug}?mode=json`, recorded from the public
API without a key on 2026-10-13, from the demo board `leverdemo`. `board.json` is
two postings of that live board -- `Staff Accountant`, which carries a `lists`
block, and `Stephanie Test Posting A`, which carries none -- trimmed to the
fields the adapter reads: id, text, hostedUrl, categories, descriptionPlain and
lists. Every value in it is copied unedited from the recorded response. The
postings are public board content: no key, no secret, no e-mail address and no
phone number (the one digit run a naive scan finds is a slice of a posting's own
UUID id). The fixture's `hostedUrl` links to `jobs.lever.co` rather than to the
API host, so a URL synthesised from the slug fails here.

Every test drives `fetch` through an injected `httpx2.MockTransport` whose
handler serves the board URL -- and only that URL -- and every test runs with
`socket.socket.connect` patched to raise, so an implementation that fell through
to a client of its own fails here instead of quietly passing. No server is
started and no test opens a socket.
"""

import json
import socket
from pathlib import Path

import httpx2
import pytest

from app.sources import SOURCES, greenhouse, lever, source_named
from app.sources.base import (
    REQUEST_TIMEOUT_SECONDS,
    BoardUnavailable,
    MalformedPayload,
    SourceError,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
)
from app.sources.lever import fetch

BACKEND_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lever"
BOARD_BODY = (FIXTURES / "board.json").read_bytes()

# The board the fixture was recorded from, and the URL every test but the slug
# gate uses: spelled out rather than built from the module's own constant, so a
# moved endpoint fails here.
SLUG = "leverdemo"
BOARD_URL = "https://api.lever.co/v0/postings/{slug}"

# The key names the payload is known by, and the only files allowed to name them:
# the adapter and this test module. `app.sources.base` and `app.sources.__init__`
# name neither source's payload.
PAYLOAD_TERMS = ("api.lever.co", "hostedUrl", "descriptionPlain", "categories")

# The description of each posting as the adapter owes it, whole: the posting's own
# plain text, then each `lists` entry's heading and its `content` with the tags
# stripped, joined in the order Lever sent them. Written out so a "read the wrong
# field" answer fails.
FIRST_DESCRIPTION = (
    'Welcome to the Demo Job Listing for Lever! This is a fictional job created solely for'
    ' demonstration purposes and is not an actual open position. We’ve crafted this listing to'
    ' showcase the functionality of our ATS platform, including job descriptions, application'
    ' processes, and more.\n'
    '\n'
    'While you can explore the application process and features here, please note that'
    ' applications submitted to this job will not be reviewed or responded to as it’s for'
    ' demonstration only. \n'
    '\n'
    '\n'
    'The Staff Accountant position requires candidates capable of fulfilling a broad range of'
    ' duties and responsibilities, such as helping small businesses\xa0maintain accounting best'
    ' practices.\xa0\n'
    '\n'
    '\n'
    'Duties and Responsibilities include:\n'
    '\n'
    'Planning, implementing and supervising the company’s financial strategyManaging the'
    ' company’s financial accounts, payroll, budget, cash receipts and financial assetsHandling'
    ' the company’s transactions and debts and doing cash flow forecastingPerforming the'
    ' company’s\xa0financial auditsFollowing proper accounting procedures to reach financial'
    ' objectivesMonitoring and analyzing financial accounting dataCreating financial reports'
    ' based on data analysisMaking recommendations to business stakeholdersThis is an accounting'
    ' role... \n'
    '\n'
    'Skills\n'
    '\n'
    'Great interpersonal and verbal communication skillsSound knowledge of accounting principles,'
    ' standards and regulationsExperience with accounting software and databasesSuperior data'
    ' management and data analysis skillsStrong\xa0time management\xa0and organizational'
    ' skillsDetail-oriented with a passion for accuracyWork experience as an Accounting Manager'
    ' or Finance Manager,\xa0preferably 2 or more years'
)
SECOND_DESCRIPTION = (
    'Welcome to the Demo Job Listing for Lever! This is a fictional job created solely for'
    ' demonstration purposes and is not an actual open position. We’ve crafted this listing to'
    ' showcase the functionality of our ATS platform, including job descriptions, application'
    ' processes, and more.\n'
    '\n'
    'While you can explore the application process and features here, please note that'
    ' applications submitted to this job will not be reviewed or responded to as it’s for'
    ' demonstration only. \n'
)


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _body_of(payload: object) -> bytes:
    return json.dumps(payload).encode()


def _board_payload() -> list:
    """The committed board, so a case is derived from the fixture rather than from a copy of it."""
    return json.loads(BOARD_BODY)


def _board_without(field: str) -> list:
    payload = _board_payload()
    del payload[0][field]
    return payload


def _no_wait(seconds: float) -> None:
    """The retry waits, injected: the waits themselves are `test_source_hardening.py`'s business."""


def _client_serving(
    body: bytes, requests: list[httpx2.Request], *, slug: str = SLUG, status: int = 200
) -> httpx2.Client:
    """A client whose one handler serves that board URL with `body` and refuses any other request."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == BOARD_URL.format(slug=slug) + "?mode=json", (
            f"unexpected request: {request.url}"
        )
        return httpx2.Response(status, content=body)

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def test_one_request_is_made_to_the_board_endpoint_with_mode_json() -> None:
    """The recorded board drives the fetch, and the request is the one the payload needs."""
    requests: list[httpx2.Request] = []

    postings = fetch(SLUG, client=_client_serving(BOARD_BODY, requests))

    assert len(postings) == 2
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == httpx2.URL(BOARD_URL.format(slug=SLUG))
    assert request.url.params["mode"] == "json"


def test_every_field_of_each_posting_comes_from_the_payload() -> None:
    """Fields compared whole, one posting per job: a hard-coded answer cannot pass."""
    postings = fetch(SLUG, client=_client_serving(BOARD_BODY, []))

    assert isinstance(postings, list)
    assert all(isinstance(posting, SourcePosting) for posting in postings)
    assert [
        (posting.external_id, posting.title, posting.location, posting.url)
        for posting in postings
    ] == [
        (
            "5243ea1b-f2c0-4f43-8f02-a5dbb97e24cb",
            "Staff Accountant",
            "New York, New York",
            "https://jobs.lever.co/leverdemo/5243ea1b-f2c0-4f43-8f02-a5dbb97e24cb",
        ),
        (
            "93db84be-2f79-4264-b521-510409fd0225",
            "Stephanie Test Posting A",
            "Baltimore, MD",
            "https://jobs.lever.co/leverdemo/93db84be-2f79-4264-b521-510409fd0225",
        ),
    ]


def test_the_description_is_the_plain_text_with_the_list_html_stripped() -> None:
    """The scorer (#8) gets sentences, not the payload's markup, and never its `description`."""
    first, second = fetch(SLUG, client=_client_serving(BOARD_BODY, []))

    assert first.description == FIRST_DESCRIPTION
    assert second.description == SECOND_DESCRIPTION
    for description in (first.description, second.description):
        assert "<div>" not in description
        assert "<p>" not in description
        assert "<" not in description


def test_the_payload_html_description_field_is_not_the_one_used() -> None:
    """A body that carries both fields is answered from the plain one."""
    payload = _board_payload()
    payload[0]["description"] = "<div>HTML that must not reach the posting.</div>"

    first, _ = fetch(SLUG, client=_client_serving(_body_of(payload), []))

    assert "HTML that must not reach the posting" not in first.description
    assert first.description == FIRST_DESCRIPTION


def test_a_posting_without_a_location_or_a_description_is_a_posting() -> None:
    """`location: null` and an absent description are normal, not a malformed payload."""
    payload = _board_payload()
    payload[0]["categories"]["location"] = None
    del payload[1]["descriptionPlain"]

    first, second = fetch(SLUG, client=_client_serving(_body_of(payload), []))

    assert first.location is None
    assert first.title == "Staff Accountant"
    assert second.description is None
    assert second.title == "Stephanie Test Posting A"


def test_an_empty_board_is_an_empty_list_and_still_one_request() -> None:
    requests: list[httpx2.Request] = []

    postings = fetch(SLUG, client=_client_serving(b"[]", requests))

    assert isinstance(postings, list)
    assert postings == []
    assert len(requests) == 1


MALFORMED_BODIES = [
    pytest.param(b"<html><body>502 Bad Gateway</body></html>", id="not JSON at all"),
    pytest.param(_body_of({"ok": False, "error": "Document not found"}), id="valid JSON that is an object"),
    pytest.param(_body_of("just a string"), id="valid JSON that is a string"),
    pytest.param(_body_of(["not an object"]), id="a list holding a non-object"),
    pytest.param(_body_of(_board_without("id")), id="a posting with no id"),
    pytest.param(_body_of(_board_without("text")), id="a posting with no title"),
    pytest.param(_body_of(_board_without("hostedUrl")), id="a posting with no url"),
]


@pytest.mark.parametrize("body", MALFORMED_BODIES)
def test_a_malformed_payload_is_a_typed_refusal(body: bytes) -> None:
    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=_client_serving(body, []))

    assert type(raised.value) is MalformedPayload


UNKNOWN_BOARD_BODIES = [
    pytest.param(b'{"ok":false,"error":"Document not found"}', id="the recorded body"),
    pytest.param(b"", id="an empty body"),
    pytest.param(b"<html><body>Document not found</body></html>", id="an HTML body"),
]


@pytest.mark.parametrize("body", UNKNOWN_BOARD_BODIES)
def test_a_404_is_an_unknown_slug_whatever_its_body_says(body: bytes) -> None:
    """The status decides. Nothing here reads the body."""
    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=_client_serving(body, [], status=404))

    assert type(raised.value) is UnknownSlug
    assert "document not found" not in str(raised.value).lower()


@pytest.mark.parametrize("slug", ["", "   ", "Acme Inc", "acme/../other", "acme?x=1"])
def test_a_value_that_cannot_name_a_board_never_reaches_the_network(slug: str) -> None:
    requests: list[httpx2.Request] = []

    with pytest.raises(SourceError) as raised:
        fetch(slug, client=_client_serving(BOARD_BODY, requests))

    assert type(raised.value) is UnknownSlug
    assert requests == []


@pytest.mark.parametrize("slug", ["acme", "acme-2", "Acme", "a.b_c"])
def test_a_slug_shaped_value_is_sent_as_it_is(slug: str) -> None:
    """No real board is refused by the gate, and no slug is lower-cased on the way out."""
    requests: list[httpx2.Request] = []

    postings = fetch(slug, client=_client_serving(BOARD_BODY, requests, slug=slug))

    assert len(postings) == 2
    assert len(requests) == 1


@pytest.mark.parametrize(
    "error",
    [httpx2.ConnectTimeout("slow"), httpx2.ReadTimeout("slow")],
    ids=["a connect timeout", "a read timeout"],
)
def test_a_slow_board_is_a_typed_timeout_and_the_request_carries_the_timeout(
    error: httpx2.TimeoutException,
) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise error

    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=httpx2.Client(transport=httpx2.MockTransport(handle)), sleep=_no_wait)

    assert type(raised.value) is SourceTimeout
    assert requests[0].extensions["timeout"]["connect"] == REQUEST_TIMEOUT_SECONDS
    assert requests[0].extensions["timeout"]["read"] == REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("status", [500, 503, 403])
def test_a_refusal_from_the_source_is_board_unavailable(status: int) -> None:
    """A board that is down is never reported as a board that does not exist."""
    body = b"<html><body>Nope, and this text is not repeated anywhere</body></html>"

    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=_client_serving(body, [], status=status), sleep=_no_wait)

    assert type(raised.value) is BoardUnavailable
    assert str(status) in str(raised.value)
    assert "Nope" not in str(raised.value)


def test_a_board_that_cannot_be_reached_is_board_unavailable_too() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=httpx2.Client(transport=httpx2.MockTransport(handle)), sleep=_no_wait)

    assert type(raised.value) is BoardUnavailable
    assert "no route to host" not in str(raised.value)


def test_only_one_module_knows_the_lever_payload() -> None:
    adapter = BACKEND_ROOT / "app" / "sources" / "lever.py"

    found = {
        f"{path.relative_to(BACKEND_ROOT)}: {term}"
        for path in (BACKEND_ROOT / "app").rglob("*.py")
        if path != adapter
        for term in PAYLOAD_TERMS
        if term in path.read_text(encoding="utf-8")
    }

    assert found == set()


def test_the_registry_names_both_sources_and_resolves_each_to_its_own_fetch() -> None:
    """A third adapter adds a module, an entry here and a vocabulary value, and nothing else."""
    assert sorted(SOURCES) == ["greenhouse", "lever"]
    assert source_named("greenhouse") is greenhouse.fetch
    assert source_named("lever") is lever.fetch
    assert set(SOURCES.values()) == {greenhouse.fetch, lever.fetch}
