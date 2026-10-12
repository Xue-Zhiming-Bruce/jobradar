"""The parser seam, the real parser, and the prompt it renders.

The interface and the fake are #26's; the endpoint, the request body, the retry
rule and the failure wording are #8's, reused through `app.matching.scorer`, and
they are driven here the way the suite drives every HTTP client in this project:
through `httpx2.MockTransport`, with the autouse fixtures below patching
`socket.socket.connect` to raise and deleting `LLM_API_KEY`, so an implementation
that fell through to a client of its own, or that read a real key from the shell,
fails here instead of quietly passing.

Every assertion is on a value: the exact request and count, the exact
`ResumeFields` or the exact exception each answer shape produces. The key and the
resume text are sentinels, and the cases at the end prove neither reaches a
message. No case starts a server, sets a real key or reads a real resume.
"""

import ast
import inspect
import json
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx2
import pytest

from app.matching import resume_fields as resume_fields_module
from app.matching import scorer as scorer_module
from app.matching.resume_fields import (
    PARSE_PROMPT_PATH,
    FakeResumeParser,
    LlmResumeParser,
    ResumeFieldParser,
    ResumeFields,
    ResumeParseError,
)
from app.matching.scorer import (
    CHAT_COMPLETIONS_URL,
    DEFAULT_MODEL,
    LLM_API_KEY,
    MODEL_TIMEOUT_SECONDS,
    SCORER_MAX_ATTEMPTS,
    ScorerConfigurationError,
    api_key,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
MATCHING_ROOT = BACKEND_ROOT / "app" / "matching"
RESUME_FIELDS_MODULE = MATCHING_ROOT / "resume_fields.py"
RESUME_FIELDS_SOURCE = RESUME_FIELDS_MODULE.read_text(encoding="utf-8")

# The endpoint, spelled out rather than imported, so a moved one fails here.
THE_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# A recognisable key value and a recognisable resume: neither is a real
# credential, a real resume, or allowed to appear in a raised message.
THE_KEY = "unit-test-key-not-a-real-credential"
RESUME_TEXT = "Backend engineer: Python, PostgreSQL, and a little Rust."

# The two absent values the prompt asks for, as the model is asked for them.
NO_NAME_AND_NO_SKILLS = {"name": None, "skills": []}

GOOD_FIELDS = {"name": "Ada Lovelace", "skills": ["Python", "SQL"]}


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No case here reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key, whatever its shell holds."""
    monkeypatch.delenv(LLM_API_KEY, raising=False)


def _response(body: str, *, status: int = 200) -> httpx2.Response:
    """One HTTP answer whose body is exactly `body`: the response's own shape is the case."""
    return httpx2.Response(status, content=body)


def _completion(content: str) -> str:
    """A well-formed Chat Completions body whose one message's content is `content`."""
    return json.dumps({"choices": [{"message": {"content": content}}]})


def _rendered(resume_text: str) -> str:
    """The template file rendered the way criterion 4 spells it, built here, not by the module."""
    return PARSE_PROMPT_PATH.read_text(encoding="utf-8").format(resume_text=resume_text)


def _client_serving(
    responses: list[httpx2.Response | Exception], requests: list[httpx2.Request]
) -> httpx2.Client:
    """A stub provider that answers with each response in turn, repeating the last.

    It records every request and serves this module's endpoint and no other: a
    second URL, a second request shape or a second provider fails here. A response
    that is an exception is raised instead, so a timeout or a refused connection
    is one case of the same stub.
    """
    served: list[httpx2.Response | Exception] = responses or [
        _response(_completion(json.dumps(GOOD_FIELDS)))
    ]

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == THE_ENDPOINT, f"unexpected request: {request.url}"
        answer = served[min(len(requests) - 1, len(served) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def _body_of(request: httpx2.Request) -> dict:
    return json.loads(request.content)


def _fields() -> ResumeFields:
    return ResumeFields(name="Ada Lovelace", skills=["Python", "SQL"])


def _answer(stated: dict) -> str:
    """One well-formed answer whose inner object is `stated`."""
    return _completion(json.dumps(stated))


def _parse(answer: str, monkeypatch: pytest.MonkeyPatch) -> ResumeFields:
    """One parse that the stub answers with `answer`, and the fields it returned."""
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    return LlmResumeParser(client=_client_serving([_response(answer)], [])).parse(RESUME_TEXT)


# The seam: one method over a resume text, and one fake a route test can read.


def test_a_fake_parser_is_a_resume_field_parser() -> None:
    assert isinstance(FakeResumeParser(_fields()), ResumeFieldParser)


def test_a_call_returns_the_fields_the_fake_was_built_with_unchanged() -> None:
    fields = _fields()
    parser = FakeResumeParser(fields)

    assert parser.parse(RESUME_TEXT) is fields


def test_the_fake_records_every_call_as_the_resume_text() -> None:
    """What the route's tests read to prove the model ran once, with the row's text."""
    parser = FakeResumeParser(_fields())

    parser.parse("The active resume's text.")
    parser.parse("Another resume's text.")

    assert parser.calls == ["The active resume's text.", "Another resume's text."]


def test_the_fields_are_a_name_and_a_list_of_skills() -> None:
    fields = ResumeFields(name="Ada Lovelace", skills=["Python"])

    assert fields.name == "Ada Lovelace"
    assert fields.skills == ["Python"]


def test_the_fields_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        _fields().name = None


def test_the_seam_is_one_method_over_a_resume_text() -> None:
    """The shape the route calls and #50 reads, named here so a change to it fails loudly."""
    assert list(inspect.signature(ResumeFieldParser.parse).parameters) == ["self", "resume_text"]
    assert inspect.signature(ResumeFieldParser.parse).return_annotation is ResumeFields
    assert ResumeFields.__dataclass_fields__.keys() == {"name", "skills"}
    assert ResumeFields.__dataclass_params__.frozen
    assert list(inspect.signature(FakeResumeParser).parameters) == ["fields"]
    assert hasattr(FakeResumeParser(_fields()), "calls")


def test_the_real_parser_satisfies_the_protocol_without_moving_it() -> None:
    """One method, the protocol's, and one optional argument: the injected client."""
    assert isinstance(LlmResumeParser(), ResumeFieldParser)
    assert ResumeFieldParser not in LlmResumeParser.__mro__
    assert list(inspect.signature(LlmResumeParser.parse).parameters) == ["self", "resume_text"]
    assert inspect.signature(LlmResumeParser.parse).return_annotation is ResumeFields
    assert list(inspect.signature(LlmResumeParser).parameters) == ["client"]
    assert [name for name in vars(LlmResumeParser) if not name.startswith("_")] == ["parse"]


def test_constructing_the_parser_reads_no_configuration_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `LlmResumeParser` is inert: reading nothing, opening no socket, building no client."""

    def refuse(*args: object, **kwargs: object) -> httpx2.Client:
        raise AssertionError("constructing the parser built a client")

    monkeypatch.setattr(httpx2, "Client", refuse)

    LlmResumeParser()

    assert api_key() == ""


def test_the_attempt_bound_is_the_scorer_s_own_constant() -> None:
    """One retry rule, one spelling: a second constant would be the same 3 twice."""
    assert resume_fields_module.SCORER_MAX_ATTEMPTS is scorer_module.SCORER_MAX_ATTEMPTS == 3


def test_the_module_reads_the_environment_nowhere() -> None:
    """The key comes from `scorer.api_key()`, which stays the one reader of it in `app/`."""
    reads = [
        node.attr
        for node in ast.walk(ast.parse(RESUME_FIELDS_SOURCE))
        if isinstance(node, ast.Attribute) and node.attr == "environ"
    ]

    assert reads == []
    assert "import os" not in RESUME_FIELDS_SOURCE


# The configuration: the scorer's two refusals, before any request.


def test_a_missing_key_is_a_configuration_error_before_any_request() -> None:
    requests: list[httpx2.Request] = []
    parser = LlmResumeParser(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError) as raised:
        parser.parse(RESUME_TEXT)

    assert LLM_API_KEY in str(raised.value)
    assert "key" in str(raised.value)
    assert requests == []


@pytest.mark.parametrize("configured", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_key_is_a_configuration_error_too(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, configured)
    parser = LlmResumeParser(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError):
        parser.parse(RESUME_TEXT)

    assert requests == []


@pytest.mark.parametrize("resume_text", ["", "   ", "\n\t "], ids=["empty", "blank", "whitespace"])
def test_a_blank_resume_is_a_configuration_error_before_any_request(
    resume_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    parser = LlmResumeParser(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError) as raised:
        parser.parse(resume_text)

    assert "resume" in str(raised.value).lower()
    assert requests == []


def test_the_two_configuration_errors_are_distinct_rather_than_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    parser = LlmResumeParser(client=_client_serving([], requests))
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    with pytest.raises(ScorerConfigurationError) as blank_resume:
        parser.parse("")

    monkeypatch.delenv(LLM_API_KEY)
    with pytest.raises(ScorerConfigurationError) as no_key:
        parser.parse(RESUME_TEXT)

    assert str(blank_resume.value) != str(no_key.value)
    assert LLM_API_KEY in str(no_key.value)
    assert requests == []


# The request: one call per parse, to #8's endpoint, with #8's own body.


def test_one_request_is_made_to_the_chat_completions_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    parser = LlmResumeParser(client=_client_serving([_response(_answer(GOOD_FIELDS))], requests))

    fields = parser.parse(RESUME_TEXT)

    assert fields == ResumeFields(name="Ada Lovelace", skills=["Python", "SQL"])
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == THE_ENDPOINT == CHAT_COMPLETIONS_URL
    assert request.headers["authorization"] == f"Bearer {THE_KEY}"
    assert _body_of(request) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": _rendered(RESUME_TEXT)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    assert request.extensions["timeout"]["connect"] == MODEL_TIMEOUT_SECONDS
    assert request.extensions["timeout"]["read"] == MODEL_TIMEOUT_SECONDS


def test_the_client_it_builds_carries_the_module_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no client injected, one is built -- locally, with the scorer's own timeout."""
    real_client = httpx2.Client
    built: list[dict[str, object]] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    def build(**kwargs: object) -> httpx2.Client:
        built.append(kwargs)
        return real_client(
            transport=httpx2.MockTransport(
                lambda request: _response(_answer(GOOD_FIELDS))
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx2, "Client", build)

    assert LlmResumeParser().parse(RESUME_TEXT) == ResumeFields(
        name="Ada Lovelace", skills=["Python", "SQL"]
    )
    assert built == [{"timeout": MODEL_TIMEOUT_SECONDS}]


# The prompt: one template file, beside this module, read on every parse.


def test_the_template_sits_beside_the_module_and_asks_for_the_two_keys() -> None:
    assert PARSE_PROMPT_PATH == MATCHING_ROOT / "resume_prompt.txt"
    assert PARSE_PROMPT_PATH.is_absolute()
    template = PARSE_PROMPT_PATH.read_text(encoding="utf-8")

    assert "{resume_text}" in template
    for key in ("name", "skills"):
        assert f'"{key}"' in template
    assert "null" in template
    assert "[]" in template


def test_the_message_content_is_the_rendered_template_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    LlmResumeParser(client=_client_serving([], requests)).parse(RESUME_TEXT)

    assert _body_of(requests[0])["messages"] == [
        {"role": "user", "content": _rendered(RESUME_TEXT)}
    ]


def test_the_resume_reaches_the_model_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not truncated, not escaped, not summarised: the text the owner uploaded."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    resume_text = 'Line one\n\n  indented {braces} "quotes" — ünïcode\n' + ("padding " * 500)

    LlmResumeParser(client=_client_serving([], requests)).parse(resume_text)

    content = _body_of(requests[0])["messages"][0]["content"]
    assert content.count(resume_text) == 1
    assert "\\n" not in content


def test_the_template_is_read_on_every_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Editing the file changes the next request: nothing is read at import."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    parser = LlmResumeParser(client=_client_serving([], requests))
    parser.parse(RESUME_TEXT)

    edited = tmp_path / "resume_prompt.txt"
    edited.write_text("Edited: {resume_text}", encoding="utf-8")
    monkeypatch.setattr(resume_fields_module, "PARSE_PROMPT_PATH", edited)
    parser.parse(RESUME_TEXT)

    first, second = (_body_of(request)["messages"][0]["content"] for request in requests)
    assert first != second
    assert second == f"Edited: {RESUME_TEXT}"
    assert second.count(RESUME_TEXT) == 1


# The answer: one shape is read, and anything else is malformed.


def test_a_well_formed_answer_is_the_fields_field_for_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    stated = {"name": "Ada Lovelace", "skills": ["Python", "PostgreSQL", "Rust"]}

    fields = LlmResumeParser(
        client=_client_serving([_response(_answer(stated))], requests)
    ).parse(RESUME_TEXT)

    assert fields == ResumeFields(name=stated["name"], skills=stated["skills"])
    assert len(requests) == 1


def test_the_absent_answer_is_the_two_absent_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A null name and no skills is an answer, not a failure: the parse succeeded and said so."""
    assert _parse(_answer(NO_NAME_AND_NO_SKILLS), monkeypatch) == ResumeFields(
        name=None, skills=[]
    )


def test_an_object_with_neither_key_is_the_absent_values_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _parse(_answer({}), monkeypatch) == ResumeFields(name=None, skills=[])


def test_null_skills_are_the_absent_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """`null` and `[]` are the two spellings of "claims no skills", and both are `[]`."""
    assert _parse(_answer({"name": "Ada Lovelace", "skills": None}), monkeypatch) == ResumeFields(
        name="Ada Lovelace", skills=[]
    )


def test_extra_keys_in_the_answer_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    stated = dict(GOOD_FIELDS, years=12, summary="ignored")

    fields = LlmResumeParser(
        client=_client_serving([_response(_answer(stated))], requests)
    ).parse(RESUME_TEXT)

    assert fields == ResumeFields(name="Ada Lovelace", skills=["Python", "SQL"])
    assert len(requests) == 1


def test_the_values_are_the_model_s_own_strings_in_its_own_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stated = {"name": " Ada Lovelace\n", "skills": ["zeta", "alpha "]}

    assert _parse(_answer(stated), monkeypatch) == ResumeFields(
        name=" Ada Lovelace\n", skills=["zeta", "alpha "]
    )


MALFORMED_ANSWERS = [
    pytest.param("", id="an empty body"),
    pytest.param("not JSON at all", id="a body that is not JSON"),
    pytest.param('"just a string"', id="a JSON string for a body"),
    pytest.param("[1, 2]", id="a JSON list for a body"),
    pytest.param(json.dumps({"choices": []}), id="no choices"),
    pytest.param(json.dumps({"choices": ["no"]}), id="a choice that is not an object"),
    pytest.param(
        json.dumps({"choices": [{"message": "nope"}]}), id="a message that is not an object"
    ),
    pytest.param(
        json.dumps({"choices": [{"message": {"content": 5}}]}),
        id="a content that is not a string",
    ),
    pytest.param(json.dumps({"choices": [{"message": {}}]}), id="no content"),
    pytest.param(_completion("not JSON"), id="content that is not JSON"),
    pytest.param(_completion('"a string"'), id="content that is a JSON string"),
    pytest.param(_completion("[]"), id="content that is a JSON list"),
    pytest.param(_answer({"name": "", "skills": []}), id="an empty name"),
    pytest.param(_answer({"name": "   ", "skills": []}), id="a blank name"),
    pytest.param(_answer({"name": 5, "skills": []}), id="a name that is not a string"),
    pytest.param(_answer({"name": True, "skills": []}), id="a name that is a boolean"),
    pytest.param(_answer({"name": "Ada", "skills": "Python, SQL"}), id="skills as a string"),
    pytest.param(_answer({"name": "Ada", "skills": 3}), id="skills as a number"),
    pytest.param(_answer({"name": "Ada", "skills": ["Python", 5]})),
    pytest.param(_answer({"name": "Ada", "skills": ["Python", "  "]})),
    pytest.param(_answer(NO_NAME_AND_NO_SKILLS | {"skills": [""]})),
]


@pytest.mark.parametrize("answer", MALFORMED_ANSWERS)
def test_a_malformed_answer_costs_every_attempt_and_is_then_reported(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    parser = LlmResumeParser(client=_client_serving([_response(answer)], requests))

    with pytest.raises(ResumeParseError) as raised:
        parser.parse(RESUME_TEXT)

    assert len(requests) == SCORER_MAX_ATTEMPTS == 3
    assert str(raised.value) == "the model's answer was unusable after 3 attempts"


@pytest.mark.parametrize("malformed", [0, 1, 2], ids=["none", "one", "two"])
def test_one_malformed_answer_costs_one_attempt_and_the_good_one_ends_it(
    malformed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    good = _response(_answer(GOOD_FIELDS))
    answers = [_response("junk") for _ in range(malformed)] + [good]
    parser = LlmResumeParser(client=_client_serving(answers, requests))

    fields = parser.parse(RESUME_TEXT)

    assert len(requests) == malformed + 1
    assert fields == ResumeFields(name="Ada Lovelace", skills=["Python", "SQL"])


def test_a_retry_is_a_fresh_request_with_the_same_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    parser = LlmResumeParser(client=_client_serving([_response("junk")], requests))

    with pytest.raises(ResumeParseError):
        parser.parse(RESUME_TEXT)

    assert [request.content for request in requests] == [requests[0].content] * 3
    assert {str(request.url) for request in requests} == {THE_ENDPOINT}
    assert requests[0].headers["authorization"] == f"Bearer {THE_KEY}"


# A call that produced no answer: one request, and a typed error at once.


CALL_FAILURES = [
    pytest.param(
        httpx2.ConnectTimeout("slow"),
        "the model call did not answer in time",
        id="a connect timeout",
    ),
    pytest.param(
        httpx2.ReadTimeout("slow"),
        "the model call did not answer in time",
        id="a read timeout",
    ),
    pytest.param(
        httpx2.ConnectError("no route to host"),
        "the model call failed",
        id="an unreachable endpoint",
    ),
    pytest.param(
        _response("<html><body>Nope, and this text is not repeated</body></html>", status=500),
        "the model call failed (HTTP 500)",
        id="a 500",
    ),
    pytest.param(
        _response("slow down", status=429), "the model call failed (HTTP 429)", id="a 429"
    ),
    pytest.param(
        _response("no key", status=401), "the model call failed (HTTP 401)", id="a 401"
    ),
]


@pytest.mark.parametrize(("failure", "message"), CALL_FAILURES)
def test_a_call_that_produced_no_answer_is_reported_after_one_request(
    failure: httpx2.Response | Exception,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `httpx2` exception escapes `parse`: a model outage is a typed error, not a traceback."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    parser = LlmResumeParser(client=_client_serving([failure], requests))

    with pytest.raises(ResumeParseError) as raised:
        parser.parse(RESUME_TEXT)

    assert len(requests) == 1
    assert str(raised.value) == message


# No message carries user data or the key.


def test_no_failure_message_carries_the_key_the_resume_or_the_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every failure shape, with sentinels in everything the provider sends back."""
    key = "key-sentinel-7f4c1d9ab38e"
    resume_text = "RESUME-SENTINEL: this line must never reach a message."
    body_sentinel = "BODY-SENTINEL: neither must this one."
    monkeypatch.setenv(LLM_API_KEY, key)
    answers: list[httpx2.Response | Exception] = [
        httpx2.ConnectTimeout("slow"),
        httpx2.ConnectError("no route to host"),
        _response(f"<html>{body_sentinel}</html>", status=503),
        _response(_completion(body_sentinel)),
        _response(body_sentinel),
    ]

    messages: list[str] = []
    for answer in answers:
        parser = LlmResumeParser(client=_client_serving([answer], []))
        with pytest.raises(ResumeParseError) as raised:
            parser.parse(resume_text)
        messages.append(str(raised.value))

    assert len(messages) == len(answers)
    for leaked in (key, resume_text, body_sentinel):
        assert [message for message in messages if leaked in message] == []
    assert messages == [
        "the model call did not answer in time",
        "the model call failed",
        "the model call failed (HTTP 503)",
        "the model's answer was unusable after 3 attempts",
        "the model's answer was unusable after 3 attempts",
    ]
