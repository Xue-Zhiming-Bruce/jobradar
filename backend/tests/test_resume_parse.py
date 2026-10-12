"""`POST /resume/parse`: the active resume parsed once, its fields stored, then answered.

Every case drives the real app through `TestClient` -- no server, no port, no
network -- with `DATABASE_URL` and `UPLOADS_DIR` pointed at a temporary
directory, so the suite uploads to a scratch directory and leaves
`git status --short` empty. Every case takes the shared `engine` fixture, so each
one runs on SQLite and on PostgreSQL.

The parser arrives through the route's own seam,
`app.routes.resume.get_resume_parser`, and no case needs a model: the cases about
the fields install `FakeResumeParser`, and the cases about the request install a
real `LlmResumeParser` over `httpx2.MockTransport`, the way the suite drives every
HTTP client in this project. An autouse fixture patches `socket.socket.connect` to
raise and deletes `LLM_API_KEY`, so an implementation that ignored the seam, or
that read a real key from the shell, fails here instead of quietly passing.

The uploads are synthetic and their text carries a sentinel, and the last case
here is the reason: the resume's text and the fields parsed from it are user data
and the key is a credential, so none of them may reach a log record, an error or a
body that is not the parse's own answer.
"""

import inspect
import json
import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import db
from app.main import app
from app.matching.resume_fields import (
    FakeResumeParser,
    LlmResumeParser,
    ResumeFieldParser,
    ResumeFields,
    ResumeParseError,
)
from app.matching.scorer import LLM_API_KEY, SCORER_MAX_ATTEMPTS
from app.models import Resume
from app.routes.resume import get_resume_parser, parse_resume

TEXT_PLAIN = "text/plain"

# The endpoint, spelled out rather than imported, so a moved one fails here.
THE_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# A distinctive line for the leak checks: a log record, an error or a body
# holding it fails, and an empty stream or body would be no evidence at all.
SENTINEL = "SENTINEL-RESUME-LINE: this text must never leave the database."

# The provider's body and the key, both sentinels: neither is a real credential
# or a real answer, and neither may appear in a message.
UPSTREAM_SENTINEL = "UPSTREAM-SENTINEL-TEXT-THAT-MUST-NOT-BE-ECHOED"
THE_KEY = "unit-test-key-not-a-real-credential"

# The fields every parse case uses, and the one answer the prompt asks for when
# the resume states neither a name nor a skill.
FIELDS = ResumeFields(name="Ada Lovelace", skills=["Python", "SQL"])
ABSENT_FIELDS = ResumeFields(name=None, skills=[])

# The five public fields `GET /resume` answers a resume with, and therefore the
# whole body the history routes may carry: no text and no parsed fields.
RESUME_FIELDS = {"id", "filename", "content_type", "uploaded_at", "is_active"}


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No case here reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key, whatever its shell holds: a real one cannot decide a case."""
    monkeypatch.delenv(LLM_API_KEY, raising=False)


@pytest.fixture(autouse=True)
def clean_parser_override() -> Iterator[None]:
    """Every case leaves the app's dependency overrides as it found them."""
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_resume_parser, None)


@pytest.fixture
def client(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The app over this case's engine, with its uploads in a temporary directory."""
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    with TestClient(app) as test_client:
        yield test_client


def install(parser: ResumeFieldParser) -> None:
    """Put `parser` behind the route's own seam, in place of the real one."""
    app.dependency_overrides[get_resume_parser] = lambda: parser


def upload(
    client: TestClient, filename: str = "resume.txt", text: str = SENTINEL
) -> dict[str, Any]:
    """One upload of synthetic text, and the body its `201` answered with."""
    response = client.post("/resume", files={"file": (filename, text.encode(), TEXT_PLAIN)})
    assert response.status_code == 201, response.text
    return response.json()


def parse(client: TestClient) -> Response:
    """One parse of the active resume, the way a client asks for it."""
    return client.post("/resume/parse")


def _response(body: str, *, status: int = 200) -> httpx2.Response:
    """One HTTP answer whose body is exactly `body`: the response's own shape is the case."""
    return httpx2.Response(status, content=body)


def _completion(fields: ResumeFields) -> str:
    """A well-formed Chat Completions body carrying those fields, as a model would write them."""
    content = json.dumps({"name": fields.name, "skills": fields.skills})
    return json.dumps({"choices": [{"message": {"content": content}}]})


def serving(responses: list[httpx2.Response], requests: list[httpx2.Request]) -> LlmResumeParser:
    """The real parser over a stub provider that answers with each response in turn.

    It records every request and serves the one endpoint: a second URL, a second
    request shape or a second provider fails here. The cases that read a request
    count read it through this, because the count is the evidence that a stored
    parse was not paid for twice.
    """

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == THE_ENDPOINT, f"unexpected request: {request.url}"
        return responses[min(len(requests) - 1, len(responses) - 1)]

    return LlmResumeParser(client=httpx2.Client(transport=httpx2.MockTransport(handle)))


def answering_by_marker(requests: list[httpx2.Request]) -> LlmResumeParser:
    """The real parser over a provider that answers per resume, so two rows differ.

    The answer is built from the resume text the request carried, which is how a
    case tells a row's own stored fields from the fields of the row that is active
    now.
    """

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        content = json.loads(request.content)["messages"][0]["content"]
        adams = "MARKER-ADAMS" in content
        return _response(
            _completion(
                FIELDS if adams else ResumeFields(name="Grace Hopper", skills=["COBOL"])
            )
        )

    return LlmResumeParser(client=httpx2.Client(transport=httpx2.MockTransport(handle)))


def stored_parsed_fields(resume_id: int) -> str | None:
    """One row's `parsed_fields` column, read through the app's own session."""
    with db.session_scope() as session:
        return session.get(Resume, resume_id).parsed_fields


def stored_resume_values() -> list[str]:
    """Every stored resume's filename, text, path and parsed fields: a stray write is visible."""
    with db.session_scope() as session:
        return [
            value or ""
            for row in session.scalars(select(Resume).order_by(Resume.id))
            for value in (row.filename, row.text, row.stored_path, row.parsed_fields)
        ]


def log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


# The active resume, or the refusal that says there is none.


def test_no_active_resume_is_404_and_asks_no_model(client: TestClient) -> None:
    parser = FakeResumeParser(FIELDS)
    install(parser)

    response = parse(client)

    assert response.status_code == 404
    assert response.json() == {"detail": "No active resume: upload one before parsing it"}
    assert parser.calls == []


# The parse: the row's own text in, the fields stored on that row, the fields out.


def test_a_parse_answers_the_fields_and_stores_them_on_that_row(client: TestClient) -> None:
    uploaded = upload(client)
    parser = FakeResumeParser(FIELDS)
    install(parser)

    response = parse(client)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": uploaded["id"],
        "name": "Ada Lovelace",
        "skills": ["Python", "SQL"],
    }
    # The parser saw the active row's text, exactly as it was stored.
    assert parser.calls == [SENTINEL]
    assert (
        stored_parsed_fields(uploaded["id"])
        == '{"name": "Ada Lovelace", "skills": ["Python", "SQL"]}'
    )


def test_a_freshly_uploaded_row_reads_null_and_that_is_what_causes_the_call(
    client: TestClient,
) -> None:
    """NULL is "never parsed" and not "no name", and it is the one thing that spends a call."""
    uploaded = upload(client)
    assert stored_parsed_fields(uploaded["id"]) is None
    parser = FakeResumeParser(FIELDS)
    install(parser)

    assert parse(client).status_code == 200

    assert len(parser.calls) == 1


def test_the_absent_answer_is_an_answer_and_stores_the_literal_json_text(
    client: TestClient,
) -> None:
    """A null name and no skills is `ResumeFields(None, [])`, stored as that JSON object."""
    uploaded = upload(client)
    install(FakeResumeParser(ABSENT_FIELDS))

    response = parse(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"id": uploaded["id"], "name": None, "skills": []}
    assert stored_parsed_fields(uploaded["id"]) == '{"name": null, "skills": []}'


def test_a_second_parse_of_the_same_resume_makes_no_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse is slow and paid, and the stored text never changes after the upload."""
    uploaded = upload(client)
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    requests: list[httpx2.Request] = []
    install(serving([_response(_completion(FIELDS))], requests))

    first = parse(client)
    second = parse(client)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {
        "id": uploaded["id"],
        "name": "Ada Lovelace",
        "skills": ["Python", "SQL"],
    }
    assert len(requests) == 1, "the stored fields must answer the second call"
    assert (
        stored_parsed_fields(uploaded["id"])
        == '{"name": "Ada Lovelace", "skills": ["Python", "SQL"]}'
    )


def test_an_older_resume_activated_again_is_parsed_on_its_own_first_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row keeps its own fields: activating an older one does not re-parse it either."""
    adams = upload(client, "adams.txt", "MARKER-ADAMS synthetic resume")
    hopper = upload(client, "hopper.txt", "MARKER-HOPPER synthetic resume")
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    requests: list[httpx2.Request] = []
    install(answering_by_marker(requests))

    hopper_parse = parse(client)  # hopper is the active row
    activated = client.patch(f"/resume/{adams['id']}", json={"is_active": True})
    adams_parse = parse(client)  # adams is the active row now: its own first call
    adams_again = parse(client)  # stored by then, so no request

    assert activated.status_code == 200, activated.text
    assert hopper_parse.json() == {
        "id": hopper["id"],
        "name": "Grace Hopper",
        "skills": ["COBOL"],
    }
    assert adams_parse.json() == {
        "id": adams["id"],
        "name": "Ada Lovelace",
        "skills": ["Python", "SQL"],
    }
    assert adams_again.json() == adams_parse.json()
    assert len(requests) == 2
    assert [stored_parsed_fields(row["id"]) for row in (adams, hopper)] == [
        '{"name": "Ada Lovelace", "skills": ["Python", "SQL"]}',
        '{"name": "Grace Hopper", "skills": ["COBOL"]}',
    ]


# The three named failures, and what each one leaves behind.


def test_a_missing_key_is_503_naming_the_variable_and_not_its_value(client: TestClient) -> None:
    upload(client)
    # No override: this case reaches the real provider, which refuses first.
    response = parse(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert LLM_API_KEY in detail
    assert "not set" in detail
    assert THE_KEY not in response.text
    assert SENTINEL not in response.text


def test_a_failed_call_is_502_and_stores_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploaded = upload(client)
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    requests: list[httpx2.Request] = []
    install(serving([_response(f"<html>{UPSTREAM_SENTINEL}</html>", status=500)], requests))

    response = parse(client)

    assert response.status_code == 502
    assert response.json() == {"detail": "the model call failed (HTTP 500)"}
    assert len(requests) == 1
    assert UPSTREAM_SENTINEL not in response.text
    assert stored_parsed_fields(uploaded["id"]) is None


def test_an_unusable_answer_is_502_after_every_attempt_and_stores_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploaded = upload(client)
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    requests: list[httpx2.Request] = []
    install(serving([_response("junk")], requests))

    response = parse(client)

    assert response.status_code == 502
    assert response.json() == {"detail": "the model's answer was unusable after 3 attempts"}
    assert len(requests) == SCORER_MAX_ATTEMPTS == 3
    assert stored_parsed_fields(uploaded["id"]) is None


def test_a_parser_that_raises_its_own_error_is_502_too(client: TestClient) -> None:
    """The route answers the parser's two types by name, whoever implements the protocol."""
    upload(client)

    class Refusing:
        """A `ResumeFieldParser` whose one call failed: no model, no transport, one raised type."""

        def parse(self, resume_text: str) -> ResumeFields:
            raise ResumeParseError("the model call did not answer in time")

    install(Refusing())

    response = parse(client)

    assert response.status_code == 502
    assert response.json() == {"detail": "the model call did not answer in time"}


# The seam, and the routes that are unchanged around it.


def test_the_parser_reaches_the_route_through_one_dependency() -> None:
    """One provider, declared the way `app.routes.internal.get_run_starter` is."""
    provider = get_resume_parser()

    assert isinstance(provider, ResumeFieldParser)
    assert isinstance(provider, LlmResumeParser)
    assert list(inspect.signature(parse_resume).parameters) == ["parser"]


def test_the_other_resume_routes_answer_as_they_did(client: TestClient) -> None:
    """The upload and the history are #3's and #25's: neither gains a field here."""
    uploaded = upload(client)
    assert set(uploaded) == {"id", "filename", "content_type", "is_active"}
    install(FakeResumeParser(FIELDS))
    assert parse(client).status_code == 200

    listed = client.get("/resume")

    assert listed.status_code == 200
    (item,) = listed.json()
    assert set(item) == RESUME_FIELDS
    assert SENTINEL not in listed.text
    assert "parsed_fields" not in listed.text
    assert not {"Ada Lovelace", "Python", "SQL"} & set(item.values())


# The text, the fields and the key: in no log record, no error and no stray body.


def test_neither_the_text_nor_the_key_reaches_a_log_an_error_or_a_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    uploaded = upload(client)
    caplog.set_level(logging.DEBUG)
    requests: list[httpx2.Request] = []

    install(serving([_response(f"<html>{UPSTREAM_SENTINEL}</html>", status=500)], requests))
    failed = parse(client)
    install(serving([_response(_completion(FIELDS))], requests))
    parsed = parse(client)
    listed = client.get("/resume")
    # A record of the test's own, so an empty stream cannot pass as evidence.
    logging.getLogger("tests.resume_parse").debug("the capture is live")

    assert (failed.status_code, parsed.status_code, listed.status_code) == (502, 200, 200)
    # The parsed values are in the parse's own answer, which is the one body they belong in.
    assert parsed.json() == {
        "id": uploaded["id"],
        "name": "Ada Lovelace",
        "skills": ["Python", "SQL"],
    }
    assert failed.json() == {"detail": "the model call failed (HTTP 500)"}
    assert set(listed.json()[0]) == RESUME_FIELDS
    for response in (failed, listed):
        assert SENTINEL not in response.text
        assert UPSTREAM_SENTINEL not in response.text

    lines = log_lines(caplog.records)
    assert lines, "the capture must be non-vacuous"
    for leaked in (
        SENTINEL,
        THE_KEY,
        UPSTREAM_SENTINEL,
        '{"name": "Ada Lovelace", "skills": ["Python", "SQL"]}',
        "Ada Lovelace",
        "Python",
    ):
        assert [line for line in lines if leaked in line] == []
    # The route's own line names the row and the shape of what was stored, no value.
    route_lines = [line for line in lines if "Parsed resume" in line]
    assert len(route_lines) == 1
    assert f"Parsed resume {uploaded['id']}" in route_lines[0]
    assert "name present, 2 skills" in route_lines[0]
    # The key reached no column either: not the text, not the path, not the parsed fields.
    assert [value for value in stored_resume_values() if THE_KEY in value] == []
