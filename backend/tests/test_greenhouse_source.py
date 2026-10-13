"""The Greenhouse adapter, driven by recorded boards and never by the network.

The endpoint under test is
`GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`,
recorded from the public API without a key on 2026-10-12. Each fixture is a
recorded body: `board.json` is two postings of a live board (stripe) trimmed to
the fields the adapter reads -- id, title, location, absolute_url and content --
with `meta` kept as recorded, `empty-board.json` is a board with no opening, and
`unknown-board.json` is what a board that does not exist answers. The postings
are public board content: no key, no secret and no personal data. The fixture's
`absolute_url` links to `stripe.com` rather than to `boards.greenhouse.io`, so a
URL synthesised from the slug fails here.

Every test drives `fetch` through an injected `httpx2.MockTransport` whose
handler serves the board URL -- and only that URL -- and every test runs with
`socket.socket.connect` patched to raise, so an implementation that fell through
to a client of its own fails here instead of quietly passing. No server is
started and no test opens a socket.
"""

import json
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx2
import pytest

from app.sources.base import (
    REQUEST_TIMEOUT_SECONDS,
    BoardShrunk,
    BoardUnavailable,
    MalformedPayload,
    OversizedPayload,
    SourceError,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
)
from app.sources.greenhouse import fetch

BACKEND_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "greenhouse"
BOARD_BODY = (FIXTURES / "board.json").read_bytes()
EMPTY_BOARD_BODY = (FIXTURES / "empty-board.json").read_bytes()
UNKNOWN_BOARD_BODY = (FIXTURES / "unknown-board.json").read_bytes()

# The slug and the URL every test but the slug gate uses, spelled out rather than
# built from the module's own constant, so a moved endpoint fails here.
SLUG = "acme"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

# The key names the payload is known by, and the only files allowed to name them:
# the adapter and this test module. `app.sources.base` and `app.sources.__init__`
# name neither source's payload.
PAYLOAD_TERMS = ("boards-api.greenhouse.io", "internal_job_id", "absolute_url", "gh_jid")


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _body_of(payload: object) -> bytes:
    return json.dumps(payload).encode()


def _board_payload() -> dict:
    """The committed board, so a case is derived from the fixture rather than from a copy of it."""
    return json.loads(BOARD_BODY)


def _board_without(field: str) -> dict:
    payload = _board_payload()
    del payload["jobs"][0][field]
    return payload


def _no_wait(seconds: float) -> None:
    """The retry waits, injected: the waits themselves are `test_source_hardening.py`'s business."""


def _client_serving(
    body: bytes, requests: list[httpx2.Request], *, slug: str = SLUG, status: int = 200
) -> httpx2.Client:
    """A client whose one handler serves that board URL with `body` and refuses any other request."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == BOARD_URL.format(slug=slug) + "?content=true", (
            f"unexpected request: {request.url}"
        )
        return httpx2.Response(status, content=body)

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def test_one_request_is_made_to_the_board_endpoint_with_content_requested() -> None:
    """The recorded board drives the fetch, and the request is the one the payload needs."""
    requests: list[httpx2.Request] = []

    postings = fetch(SLUG, client=_client_serving(BOARD_BODY, requests))

    assert len(postings) == 2
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == httpx2.URL(BOARD_URL.format(slug=SLUG))
    assert request.url.params["content"] == "true"


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
            "8172510",
            "Abuse Investigator",
            "Seattle, San Francisco, New York City",
            "https://stripe.com/jobs/search?gh_jid=8172510",
        ),
        (
            "8194328",
            "Senior Software Engineer, Product Velocity",
            "Seattle, WA",
            "https://stripe.com/jobs/search?gh_jid=8194328",
        ),
    ]


def test_a_description_is_the_visible_text_of_the_escaped_html() -> None:
    """The scorer (#8) gets sentences, not the payload's escaped markup."""
    first, second = fetch(SLUG, client=_client_serving(BOARD_BODY, []))

    assert "Abuse Operations is the front-line incident response" in first.description
    assert "Design and build internal products used daily by engineering" in second.description
    for description in (first.description, second.description):
        assert "&lt;h2&gt;" not in description
        assert "<h2>" not in description
        assert "<" not in description


def test_a_posting_without_a_location_or_a_description_is_a_posting() -> None:
    """`location: null` and an absent `content` are normal, not a malformed payload."""
    payload = _board_payload()
    payload["jobs"][0]["location"] = None
    payload["jobs"][0]["content"] = None
    del payload["jobs"][1]["location"]
    del payload["jobs"][1]["content"]

    first, second = fetch(SLUG, client=_client_serving(_body_of(payload), []))

    assert first.location is None
    assert first.description is None
    assert second.location is None
    assert second.description is None
    assert first.title == "Abuse Investigator"


def test_an_empty_board_is_an_empty_list_and_still_one_request() -> None:
    requests: list[httpx2.Request] = []

    postings = fetch(SLUG, client=_client_serving(EMPTY_BOARD_BODY, requests))

    assert isinstance(postings, list)
    assert postings == []
    assert len(requests) == 1


def test_meta_is_not_read() -> None:
    """The count is a convenience, not the list: absent or disagreeing, `jobs` decides."""
    disagreeing = _board_payload()
    disagreeing["meta"] = {"total": 0}

    assert fetch(SLUG, client=_client_serving(_body_of({"jobs": []}), [])) == []
    assert len(fetch(SLUG, client=_client_serving(_body_of(disagreeing), []))) == 2


MALFORMED_BODIES = [
    pytest.param(b"<html><body>502 Bad Gateway</body></html>", id="a 200 holding an HTML page"),
    pytest.param(b'["not", "an", "object"]', id="valid JSON that is a list"),
    pytest.param(b'"just a string"', id="valid JSON that is a string"),
    pytest.param(_body_of({"meta": {"total": 0}}), id="no jobs key"),
    pytest.param(_body_of({"jobs": "two"}), id="jobs that is not a list"),
    pytest.param(_body_of(_board_without("id")), id="a job with no id"),
    pytest.param(_body_of(_board_without("title")), id="a job with no title"),
    pytest.param(_body_of(_board_without("absolute_url")), id="a job with no url"),
    pytest.param(_body_of({"jobs": ["not an object"]}), id="a job that is not an object"),
]


@pytest.mark.parametrize("body", MALFORMED_BODIES)
def test_a_malformed_payload_is_a_typed_refusal(body: bytes) -> None:
    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=_client_serving(body, []))

    assert type(raised.value) is MalformedPayload


UNKNOWN_BOARD_BODIES = [
    pytest.param(UNKNOWN_BOARD_BODY, id="the recorded body"),
    pytest.param(b"", id="an empty body"),
    pytest.param(b"<html><body>Job not found</body></html>", id="an HTML body"),
]


@pytest.mark.parametrize("body", UNKNOWN_BOARD_BODIES)
def test_a_404_is_an_unknown_slug_whatever_its_body_says(body: bytes) -> None:
    """The status decides. The body names a job, so nothing here reads it."""
    with pytest.raises(SourceError) as raised:
        fetch(SLUG, client=_client_serving(body, [], status=404))

    assert type(raised.value) is UnknownSlug
    assert "job not found" not in str(raised.value).lower()


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


def test_the_client_it_builds_carries_the_module_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no client injected, one is built -- locally, with the module's own timeout."""
    real_client = httpx2.Client
    built: list[dict[str, object]] = []

    def build(**kwargs: object) -> httpx2.Client:
        built.append(kwargs)
        return real_client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(200, content=BOARD_BODY)
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx2, "Client", build)

    assert len(fetch(SLUG)) == 2
    assert built == [{"timeout": REQUEST_TIMEOUT_SECONDS}]


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


def test_the_error_family_is_one_parent_and_five_distinct_classes() -> None:
    """Five fetch failures, so no case above can be satisfied by one catch-all.

    `BoardShrunk` is the family's sixth member and no fetch raises it -- the walk
    (#34) does -- so it is asserted here rather than listed with the five.
    """
    family = {UnknownSlug, BoardUnavailable, MalformedPayload, SourceTimeout, OversizedPayload}

    assert len(family) == 5
    assert all(issubclass(error, SourceError) for error in family)
    assert BoardShrunk not in family
    assert issubclass(BoardShrunk, SourceError)


def test_only_one_module_knows_the_greenhouse_payload() -> None:
    adapter = BACKEND_ROOT / "app" / "sources" / "greenhouse.py"

    found = {
        f"{path.relative_to(BACKEND_ROOT)}: {term}"
        for path in (BACKEND_ROOT / "app").rglob("*.py")
        if path != adapter
        for term in PAYLOAD_TERMS
        if term in path.read_text(encoding="utf-8")
    }

    assert found == set()


def test_the_sources_package_holds_base_the_registry_and_the_two_adapters() -> None:
    """Two adapters, the shared vocabulary, and the one mapping from a name to one."""
    modules = sorted(path.name for path in (BACKEND_ROOT / "app" / "sources").glob("*.py"))

    assert modules == ["__init__.py", "base.py", "greenhouse.py", "lever.py"]


def test_the_http_client_is_a_runtime_dependency() -> None:
    """The image runs `uv sync --frozen --no-dev`: a dev-only client would import-fail in it."""
    project = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert any(
        requirement.startswith("httpx2") for requirement in project["project"]["dependencies"]
    )
    dev = project["dependency-groups"]["dev"]
    assert not any(requirement.startswith("httpx2") for requirement in dev)


IMPORT_SCRIPT = f"""
import os
import socket
import sys

sys.path.insert(0, {str(BACKEND_ROOT)!r})

reads = []


class RecordingEnvironment(dict):
    def get(self, key, *args):
        reads.append(key)
        return None

    def __getitem__(self, key):
        reads.append(key)
        raise KeyError(key)


def refuse(*args, **kwargs):
    raise AssertionError("importing the adapter opened a socket")


os.environ = RecordingEnvironment()
socket.socket.connect = refuse
import httpx2


class RefusingClient(httpx2.Client):
    def __init__(self, *args, **kwargs):
        raise AssertionError("importing the adapter built a client")


httpx2.Client = RefusingClient
import app.sources.greenhouse

assert reads == [], reads
"""


def test_importing_the_adapter_reads_no_configuration_and_builds_no_client() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", IMPORT_SCRIPT],
        cwd=BACKEND_ROOT,
        env={},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

