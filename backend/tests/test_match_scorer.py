"""The scorer seam, the real scorer, and the write that stores its result.

The fake and the interface are #7's; `LlmScorer` and `record_score` are #8's, and
they are driven here the way the suite drives every HTTP client in this project:
through `httpx2.MockTransport`, with the autouse fixture below patching
`socket.socket.connect` to raise, so an implementation that fell through to a
client of its own fails here instead of quietly passing. `LLM_API_KEY` is deleted
by an autouse fixture, so a case that needs a key has to set one, and a real key
in a developer's shell cannot decide a case.

Every assertion is on a value: the exact request body and count, the exact
`MatchResult` each failure shape returns, the exact exception for a missing key
and for an empty resume, and the exact `match` row after `record_score` -- whose
cases take the shared `engine` fixture, so each runs on SQLite and on PostgreSQL.
No case starts a server, sets a real key or reads a real resume.
"""

import ast
import inspect
import json
import socket
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db
from app import matching as matching_module
from app.matching import scorer as scorer_module
from app.matching.prefilter import REJECTION_REASON_PREFIX, prefilter, record_rejection
from app.matching.scorer import (
    CHAT_COMPLETIONS_URL,
    DEFAULT_MAX_POSTINGS,
    DEFAULT_MODEL,
    LLM_API_KEY,
    LLM_MODEL,
    MODEL_TIMEOUT_SECONDS,
    NOT_SCORED_PREFIX,
    PROMPT_PATH,
    SCORER_MAX_ATTEMPTS,
    SCORER_MAX_POSTINGS,
    FakeScorer,
    LlmScorer,
    MatchResult,
    MatchScorer,
    ScorerConfigurationError,
    api_key,
    max_postings,
    model,
    record_score,
)
from app.models import Company, Match, Posting, Run
from app.sources.greenhouse import SourcePosting

BACKEND_ROOT = Path(__file__).resolve().parent.parent
MATCHING_ROOT = BACKEND_ROOT / "app" / "matching"
SCORER_MODULE = MATCHING_ROOT / "scorer.py"
SCORER_SOURCE = SCORER_MODULE.read_text(encoding="utf-8")

# The endpoint, spelled out rather than imported, so a moved one fails here.
THE_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# The three variables, in the order this module reads them, with the reader beside
# each, so one case can drive the configuration without naming it twice.
THE_THREE = ((LLM_API_KEY, api_key), (LLM_MODEL, model), (SCORER_MAX_POSTINGS, max_postings))

# A recognisable key value and a recognisable resume: neither is a real
# credential, a real resume, or allowed to appear in a message or a result.
THE_KEY = "unit-test-key-not-a-real-credential"
RESUME_TEXT = "Backend engineer: Python, PostgreSQL, and a little Rust."

# Two postings, one of them with the fields a board may omit.
UNKNOWN_FIELDS_POSTING = SourcePosting(
    external_id="2",
    title="Backend Engineer",
    location=None,
    url="https://example.test/2",
    description=None,
)

GOOD_FIELDS = {"score": 87, "reason": "Fits the backend work.", "gaps": ["No Kubernetes."]}

# The modules a network client would come from, and the terms that would name a
# model or its credential: the prefilter imports neither and reads no key.
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}
MODEL_TERMS = ("api_key", "api-key", "openai", "anthropic", "Authorization")

# Every column of both tables, so a case can compare a whole row without naming
# the columns one by one and a change to either table shows up as a failure here.
MATCH_COLUMNS = (
    "id",
    "posting_id",
    "score",
    "reason",
    "gaps",
    "state",
    "created_at",
    "notified_at",
)
POSTING_COLUMNS = (
    "id",
    "company_id",
    "external_id",
    "title",
    "location",
    "url",
    "description",
    "first_seen_at",
    "updated_at",
    "closed_at",
)


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key, no model and no cap, whatever its shell holds."""
    for name, _ in THE_THREE:
        monkeypatch.delenv(name, raising=False)


def _posting(external_id: str = "1") -> SourcePosting:
    """A posting as the adapter would hand it over: synthetic, and this module's own."""
    return SourcePosting(
        external_id=external_id,
        title=f"Backend Engineer {external_id}",
        location="Berlin",
        url=f"https://example.test/{external_id}",
        description="We use Python and PostgreSQL daily.",
    )


def _intern_posting() -> SourcePosting:
    """A posting the title rule rejects, for the row `record_rejection` writes."""
    return SourcePosting(
        external_id="9",
        title="Intern Backend Engineer",
        location="Berlin",
        url="https://example.test/9",
        description="We use Python and PostgreSQL daily.",
    )


def _result() -> MatchResult:
    return MatchResult(score=87, reason="Synthetic fit.", gaps=["No Kubernetes."])


def _response(body: str, *, status: int = 200) -> httpx2.Response:
    """One HTTP answer whose body is exactly `body`: the response's own shape is the case."""
    return httpx2.Response(status, content=body)


def _completion(content: str) -> str:
    """A well-formed Chat Completions body whose one message's content is `content`."""
    return json.dumps({"choices": [{"message": {"content": content}}]})


def _rendered(posting: SourcePosting, resume_text: str) -> str:
    """The template file rendered the way criterion 4 spells it, built here, not by the module."""
    location = posting.location or "Not stated"
    description = posting.description or "Not stated"
    return PROMPT_PATH.read_text(encoding="utf-8").format(
        resume_text=resume_text,
        posting_text=f"Title: {posting.title}\nLocation: {location}\nDescription: {description}",
    )


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


def _stored_postings(count: int) -> list[int]:
    """`count` posting rows for a score to point at, stored here and by no app function."""
    with db.session_scope() as session:
        company = Company(name="Acme", slug="acme")
        session.add(company)
        session.flush()
        postings = [
            Posting(
                company_id=company.id,
                external_id=str(number),
                title="Backend Engineer",
                location="Berlin",
                url=f"https://example.test/{number}",
                description="We use Python and PostgreSQL daily.",
            )
            for number in range(1, count + 1)
        ]
        session.add_all(postings)
        session.flush()
        return [posting.id for posting in postings]


def _one_stored_posting() -> int:
    return _stored_postings(1)[0]


def _matches() -> list[dict[str, object]]:
    """Every `match` row as plain column values, in id order."""
    with db.session_scope() as session:
        return [
            {column: getattr(row, column) for column in MATCH_COLUMNS}
            for row in session.scalars(select(Match).order_by(Match.id))
        ]


def _stored_posting(posting_id: int) -> dict[str, object]:
    """One `posting` row as plain column values, so a stray write to it is visible."""
    with db.session_scope() as session:
        row = session.get(Posting, posting_id)
        return {column: getattr(row, column) for column in POSTING_COLUMNS}


def _save_the_row_owner_style(posting_id: int) -> datetime:
    """Save one match and mark it notified, the way the owner and a digest would."""
    notified_at = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    with db.session_scope() as session:
        row = session.scalar(select(Match).where(Match.posting_id == posting_id))
        row.state = "saved"
        row.notified_at = notified_at
    return notified_at


def _rows(model_class: type) -> int:
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(model_class))


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# The seam #7 built, which #8 implements without moving it.


def test_a_fake_scorer_is_a_match_scorer() -> None:
    assert isinstance(FakeScorer(_result()), MatchScorer)


def test_a_call_returns_the_result_the_fake_was_built_with_unchanged() -> None:
    result = _result()
    scorer = FakeScorer(result)

    assert scorer.score(_posting(), "Synthetic resume text.") is result


def test_the_fake_returns_that_result_for_every_posting() -> None:
    result = _result()
    scorer = FakeScorer(result)

    returned = [scorer.score(_posting(str(number)), f"Resume {number}") for number in (1, 2)]

    assert returned == [result, result]


def test_the_fake_records_every_call_as_the_posting_and_the_resume_text() -> None:
    """What #9's tests read to prove the scorer ran once per survivor, with the resume."""
    posting = _posting("1")
    other = _posting("2")
    scorer = FakeScorer(_result())

    scorer.score(posting, "The active resume's text.")
    scorer.score(other, "Another resume's text.")

    assert scorer.calls == [(posting, "The active resume's text."), (other, "Another resume's text.")]


def test_a_result_carries_the_score_the_reason_and_the_gaps() -> None:
    result = MatchResult(score=42, reason="Fits the backend work.", gaps=["No Go."])

    assert result.score == 42
    assert result.reason == "Fits the backend work."
    assert result.gaps == ["No Go."]


def test_a_result_is_frozen() -> None:
    result = _result()

    with pytest.raises(FrozenInstanceError):
        result.score = 0


def test_the_seam_is_one_method_over_a_posting_and_a_resume_text() -> None:
    """The shape #8 implements and #9 calls, named here so a change to it fails loudly."""
    assert list(inspect.signature(MatchScorer.score).parameters) == [
        "self",
        "posting",
        "resume_text",
    ]
    assert inspect.signature(MatchScorer.score).return_annotation is MatchResult
    assert MatchResult.__dataclass_fields__.keys() == {"score", "reason", "gaps"}
    assert MatchResult.__dataclass_params__.frozen
    assert list(inspect.signature(FakeScorer).parameters) == ["result"]
    assert hasattr(FakeScorer(_result()), "calls")


def test_the_real_scorer_satisfies_the_protocol_without_moving_it() -> None:
    """One method, the protocol's, and one optional argument: the injected client."""
    assert isinstance(LlmScorer(), MatchScorer)
    assert MatchScorer not in LlmScorer.__mro__
    assert list(inspect.signature(LlmScorer.score).parameters) == [
        "self",
        "posting",
        "resume_text",
    ]
    assert inspect.signature(LlmScorer.score).return_annotation is MatchResult
    assert list(inspect.signature(LlmScorer).parameters) == ["client"]
    assert [name for name in vars(LlmScorer) if not name.startswith("_")] == ["score"]


def test_constructing_the_scorer_reads_no_configuration_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `LlmScorer` is inert: reading nothing, opening no socket, building no client."""

    def refuse(*args: object, **kwargs: object) -> httpx2.Client:
        raise AssertionError("constructing the scorer built a client")

    monkeypatch.setattr(httpx2, "Client", refuse)

    LlmScorer()

    assert api_key() == ""
    assert max_postings() == DEFAULT_MAX_POSTINGS


# The configuration: three variables, read per call, in one place.


def test_a_missing_key_is_a_configuration_error_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    scorer = LlmScorer(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError) as raised:
        scorer.score(_posting(), RESUME_TEXT)

    assert LLM_API_KEY in str(raised.value)
    assert "key" in str(raised.value)
    assert requests == []


@pytest.mark.parametrize("configured", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_key_is_a_configuration_error_too(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, configured)
    scorer = LlmScorer(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError):
        scorer.score(_posting(), RESUME_TEXT)

    assert requests == []


@pytest.mark.parametrize("resume_text", ["", "   ", "\n\t "], ids=["empty", "blank", "whitespace"])
def test_an_empty_resume_is_a_configuration_error_before_any_request(
    resume_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose resume was never uploaded must not store a confident-looking 0."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError) as raised:
        scorer.score(_posting(), resume_text)

    assert "resume" in str(raised.value).lower()
    assert requests == []


def test_the_two_configuration_errors_are_distinct_rather_than_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    scorer = LlmScorer(client=_client_serving([], requests))
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    with pytest.raises(ScorerConfigurationError) as empty_resume:
        scorer.score(_posting(), "")
    monkeypatch.delenv(LLM_API_KEY)
    with pytest.raises(ScorerConfigurationError) as no_key:
        scorer.score(_posting(), RESUME_TEXT)

    assert str(empty_resume.value) != str(no_key.value)
    assert LLM_API_KEY in str(no_key.value)
    assert requests == []


@pytest.mark.parametrize(("name", "reader"), THE_THREE, ids=[name for name, _ in THE_THREE])
def test_each_variable_is_a_module_constant_and_its_reader_takes_no_argument(
    name: str, reader: object
) -> None:
    assert getattr(scorer_module, name) == name
    assert list(inspect.signature(reader).parameters) == []


def test_the_key_the_model_and_the_cap_are_read_per_call_not_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert api_key() == ""
    assert model() == DEFAULT_MODEL == "gpt-4o-mini"
    assert max_postings() == DEFAULT_MAX_POSTINGS == 50

    monkeypatch.setenv(LLM_API_KEY, "  unit-test-key  ")
    monkeypatch.setenv(LLM_MODEL, "  gpt-4o  ")
    monkeypatch.setenv(SCORER_MAX_POSTINGS, " 7 ")
    assert api_key() == "unit-test-key"
    assert model() == "gpt-4o"
    assert max_postings() == 7

    monkeypatch.setenv(LLM_API_KEY, "")
    monkeypatch.setenv(LLM_MODEL, "   ")
    monkeypatch.setenv(SCORER_MAX_POSTINGS, "")
    assert api_key() == ""
    assert model() == DEFAULT_MODEL
    assert max_postings() == DEFAULT_MAX_POSTINGS


IMPORT_SCRIPT = f"""
import os
import sys

sys.path.insert(0, {str(BACKEND_ROOT)!r})

reads = []


class RecordingEnvironment(dict):
    def get(self, key, *args):
        reads.append(key)
        return args[0] if args else None

    def __getitem__(self, key):
        reads.append(key)
        raise KeyError(key)


os.environ = RecordingEnvironment()
import httpx2


class RefusingClient(httpx2.Client):
    def __init__(self, *args, **kwargs):
        raise AssertionError("importing the scorer built a client")


httpx2.Client = RefusingClient
from app.matching import scorer

# The import may read what Python itself reads (sysconfig, importlib); it may not
# read any of the three variables this module configures itself from.
assert [name for name in reads if name in ("LLM_API_KEY", "LLM_MODEL", "SCORER_MAX_POSTINGS")] == [], reads
assert scorer.PROMPT_PATH.is_file(), scorer.PROMPT_PATH
"""


def test_importing_the_scorer_reads_no_configuration_builds_no_client_and_finds_its_prompt() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", IMPORT_SCRIPT],
        cwd=BACKEND_ROOT,
        env={},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


# The request: one call per posting, to one endpoint, with four fields and nothing else.


def test_one_request_is_made_to_the_chat_completions_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    posting = _posting()
    scorer = LlmScorer(client=_client_serving([_response(_completion(json.dumps(GOOD_FIELDS)))], requests))

    result = scorer.score(posting, RESUME_TEXT)

    assert result == MatchResult(score=87, reason="Fits the backend work.", gaps=["No Kubernetes."])
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == THE_ENDPOINT == CHAT_COMPLETIONS_URL
    assert request.headers["authorization"] == f"Bearer {THE_KEY}"
    assert _body_of(request) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": _rendered(posting, RESUME_TEXT)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    assert request.extensions["timeout"]["connect"] == MODEL_TIMEOUT_SECONDS
    assert request.extensions["timeout"]["read"] == MODEL_TIMEOUT_SECONDS


def test_the_model_the_body_names_is_the_configured_one(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([], requests))

    monkeypatch.setenv(LLM_MODEL, "gpt-4o")
    scorer.score(_posting(), RESUME_TEXT)

    assert _body_of(requests[0])["model"] == "gpt-4o"


def test_the_client_it_builds_carries_the_module_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no client injected, one is built -- locally, with the module's own timeout."""
    real_client = httpx2.Client
    built: list[dict[str, object]] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    def build(**kwargs: object) -> httpx2.Client:
        built.append(kwargs)
        return real_client(
            transport=httpx2.MockTransport(
                lambda request: _response(_completion(json.dumps(GOOD_FIELDS)))
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx2, "Client", build)

    assert LlmScorer().score(_posting(), RESUME_TEXT).score == 87
    assert built == [{"timeout": MODEL_TIMEOUT_SECONDS}]


def test_scoring_writes_no_row(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """`score` reads and calls: it opens no session and touches no row."""
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([], []))

    scorer.score(_posting(), RESUME_TEXT)

    assert _rows(Posting) == 0
    assert _rows(Match) == 0


# The prompt: one template file, rendered from this module's directory, on every call.


def test_the_template_sits_beside_the_module_and_holds_the_two_placeholders() -> None:
    assert PROMPT_PATH == MATCHING_ROOT / "score_prompt.txt"
    assert PROMPT_PATH.is_absolute()
    template = PROMPT_PATH.read_text(encoding="utf-8")

    assert "{resume_text}" in template
    assert "{posting_text}" in template
    assert "one JSON object" in template
    assert "nothing else" in template
    for key in ("score", "reason", "gaps"):
        assert f'"{key}"' in template


def test_the_message_content_is_the_rendered_template_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    posting = _posting()

    LlmScorer(client=_client_serving([], requests)).score(posting, RESUME_TEXT)

    assert _body_of(requests[0])["messages"] == [
        {"role": "user", "content": _rendered(posting, RESUME_TEXT)}
    ]


def test_a_posting_without_a_location_or_a_description_says_not_stated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    LlmScorer(client=_client_serving([], requests)).score(UNKNOWN_FIELDS_POSTING, RESUME_TEXT)

    content = _body_of(requests[0])["messages"][0]["content"]
    assert _rendered(UNKNOWN_FIELDS_POSTING, RESUME_TEXT) == content
    assert "Location: Not stated" in content
    assert "Description: Not stated" in content
    assert "Title: Backend Engineer\nLocation: Not stated\nDescription: Not stated" in content


def test_the_posting_is_exactly_three_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    posting = _posting()

    LlmScorer(client=_client_serving([], requests)).score(posting, RESUME_TEXT)

    content = _body_of(requests[0])["messages"][0]["content"]
    assert (
        f"Title: {posting.title}\nLocation: Berlin\nDescription: {posting.description}" in content
    )


def test_the_resume_reaches_the_model_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not truncated, not escaped, not summarised: the text the owner uploaded."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    resume_text = 'Line one\n\n  indented {braces} "quotes" — ünïcode\n' + ("padding " * 500)

    LlmScorer(client=_client_serving([], requests)).score(_posting(), resume_text)

    content = _body_of(requests[0])["messages"][0]["content"]
    assert content.count(resume_text) == 1
    assert "\\n" not in content


def test_the_template_is_read_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Editing the file changes the next request: nothing is read at import."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([], requests))
    scorer.score(_posting(), RESUME_TEXT)

    edited = tmp_path / "score_prompt.txt"
    edited.write_text("Edited: {posting_text}\nResume: {resume_text}", encoding="utf-8")
    monkeypatch.setattr(scorer_module, "PROMPT_PATH", edited)
    scorer.score(_posting(), RESUME_TEXT)

    first, second = (_body_of(request)["messages"][0]["content"] for request in requests)
    assert first != second
    assert second == f"Edited: Title: Backend Engineer 1\nLocation: Berlin\nDescription: We use Python and PostgreSQL daily.\nResume: {RESUME_TEXT}"
    assert second.count(RESUME_TEXT) == 1


# The answer: one shape is read, and anything else is malformed.


def test_a_well_formed_answer_is_the_result_field_for_field(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    sent = {
        "score": 87,
        "reason": "Fits the backend work, and the on-call rotation.",
        "gaps": ["No Go.", "No Kubernetes."],
    }

    result = LlmScorer(client=_client_serving([_response(_completion(json.dumps(sent)))], requests)).score(
        _posting(), RESUME_TEXT
    )

    assert result == MatchResult(score=87, reason=sent["reason"], gaps=sent["gaps"])
    assert isinstance(result.score, int)
    assert result.gaps == ["No Go.", "No Kubernetes."]
    assert len(requests) == 1


def test_the_score_bounds_are_read_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(
        client=_client_serving(
            [
                _response(_completion(json.dumps({"score": 0, "reason": "No fit.", "gaps": []}))),
                _response(_completion(json.dumps({"score": 100, "reason": "The fit.", "gaps": []}))),
            ],
            [],
        )
    )

    assert scorer.score(_posting("1"), RESUME_TEXT) == MatchResult(0, "No fit.", [])
    assert scorer.score(_posting("2"), RESUME_TEXT) == MatchResult(100, "The fit.", [])


def test_extra_keys_in_the_answer_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    sent = dict(GOOD_FIELDS, confidence=0.9, notes="ignored")

    result = LlmScorer(client=_client_serving([_response(_completion(json.dumps(sent)))], requests)).score(
        _posting(), RESUME_TEXT
    )

    assert result == MatchResult(score=87, reason="Fits the backend work.", gaps=["No Kubernetes."])
    assert len(requests) == 1


def test_the_reason_is_the_model_s_own_string_and_the_gaps_keep_its_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    sent = {"score": 61, "reason": " Fits.\n", "gaps": ["zeta", "alpha"]}

    result = LlmScorer(
        client=_client_serving([_response(_completion(json.dumps(sent)))], [])
    ).score(_posting(), RESUME_TEXT)

    assert result.reason == " Fits.\n"
    assert result.gaps == ["zeta", "alpha"]


def test_an_empty_gaps_list_is_a_valid_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    sent = {"score": 61, "reason": "Fits.", "gaps": []}

    result = LlmScorer(
        client=_client_serving([_response(_completion(json.dumps(sent)))], [])
    ).score(_posting(), RESUME_TEXT)

    assert result == MatchResult(score=61, reason="Fits.", gaps=[])


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
    pytest.param(_completion(json.dumps({"reason": "Fits.", "gaps": []})), id="no score"),
    pytest.param(_completion(json.dumps({"score": 50, "gaps": []})), id="no reason"),
    pytest.param(_completion(json.dumps({"score": 50, "reason": "Fits."})), id="no gaps"),
    pytest.param(
        _completion(json.dumps({"score": 85.0, "reason": "Fits.", "gaps": []})),
        id="a score that is a float",
    ),
    pytest.param(
        _completion(json.dumps({"score": "85", "reason": "Fits.", "gaps": []})),
        id="a score that is a string",
    ),
    pytest.param(
        _completion(json.dumps({"score": True, "reason": "Fits.", "gaps": []})),
        id="a score that is a boolean",
    ),
    pytest.param(
        _completion(json.dumps({"score": None, "reason": "Fits.", "gaps": []})),
        id="a score that is null",
    ),
    pytest.param(
        _completion(json.dumps({"score": 101, "reason": "Fits.", "gaps": []})),
        id="a score above 100",
    ),
    pytest.param(
        _completion(json.dumps({"score": -1, "reason": "Fits.", "gaps": []})),
        id="a score below 0",
    ),
    pytest.param(
        _completion(json.dumps({"score": 50, "reason": "", "gaps": []})), id="an empty reason"
    ),
    pytest.param(
        _completion(json.dumps({"score": 50, "reason": "   ", "gaps": []})), id="a blank reason"
    ),
    pytest.param(
        _completion(json.dumps({"score": 50, "reason": 5, "gaps": []})),
        id="a reason that is not a string",
    ),
    pytest.param(
        _completion(json.dumps({"score": 50, "reason": "Fits.", "gaps": "none"})),
        id="gaps that are not a list",
    ),
    pytest.param(
        _completion(json.dumps({"score": 50, "reason": "Fits.", "gaps": ["ok", 5]})),
        id="a gap that is not a string",
    ),
]


@pytest.mark.parametrize("answer", MALFORMED_ANSWERS)
def test_a_malformed_answer_costs_every_attempt_and_is_then_reported(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([_response(answer)], requests))

    result = scorer.score(_posting(), RESUME_TEXT)

    assert len(requests) == SCORER_MAX_ATTEMPTS == 3
    assert result == MatchResult(
        score=0,
        reason="Not scored: the model's answer was unusable after 3 attempts",
        gaps=[],
    )


@pytest.mark.parametrize("malformed", [0, 1, 2], ids=["none", "one", "two"])
def test_one_malformed_answer_costs_one_attempt_and_the_good_one_ends_it(
    malformed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    good = _response(_completion(json.dumps({"score": 61, "reason": "Fits.", "gaps": []})))
    answers = [_response("junk") for _ in range(malformed)] + [good]
    scorer = LlmScorer(client=_client_serving(answers, requests))

    result = scorer.score(_posting(), RESUME_TEXT)

    assert len(requests) == malformed + 1
    assert result == MatchResult(score=61, reason="Fits.", gaps=[])


def test_a_retry_is_a_fresh_request_with_the_same_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([_response("junk")], requests))

    scorer.score(_posting(), RESUME_TEXT)

    assert [request.content for request in requests] == [requests[0].content] * 3
    assert {str(request.url) for request in requests} == {THE_ENDPOINT}
    assert requests[0].headers["authorization"] == f"Bearer {THE_KEY}"


# A call that produced no answer: one request, and a not-scored result.


CALL_FAILURES = [
    pytest.param(
        httpx2.ConnectTimeout("slow"),
        "Not scored: the model call did not answer in time",
        id="a connect timeout",
    ),
    pytest.param(
        httpx2.ReadTimeout("slow"),
        "Not scored: the model call did not answer in time",
        id="a read timeout",
    ),
    pytest.param(
        httpx2.ConnectError("no route to host"),
        "Not scored: the model call failed",
        id="an unreachable endpoint",
    ),
    pytest.param(
        _response("<html><body>Nope, and this text is not repeated</body></html>", status=500),
        "Not scored: the model call failed (HTTP 500)",
        id="a 500",
    ),
    pytest.param(
        _response("slow down", status=429),
        "Not scored: the model call failed (HTTP 429)",
        id="a 429",
    ),
    pytest.param(
        _response("no key", status=401),
        "Not scored: the model call failed (HTTP 401)",
        id="a 401",
    ),
]


@pytest.mark.parametrize(("failure", "reason"), CALL_FAILURES)
def test_a_call_that_produced_no_answer_is_reported_after_one_request(
    failure: httpx2.Response | Exception,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `httpx2` exception escapes `score`: a model outage is a row, not a traceback."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([failure], requests))

    result = scorer.score(_posting(), RESUME_TEXT)

    assert len(requests) == 1
    assert result == MatchResult(score=0, reason=reason, gaps=[])
    assert result.reason.startswith(NOT_SCORED_PREFIX)
    for leaked in ("Nope", "no route to host", "slow down", THE_KEY, RESUME_TEXT):
        assert leaked not in result.reason


# A not-scored result is an ordinary result, and it is storable.


def test_every_not_scored_reason_is_the_prefix_and_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    monkeypatch.setenv(SCORER_MAX_POSTINGS, "1")
    scorer = LlmScorer(client=_client_serving([_response("junk")], []))

    unusable = scorer.score(_posting("1"), RESUME_TEXT)
    capped = scorer.score(_posting("2"), RESUME_TEXT)

    assert NOT_SCORED_PREFIX == "Not scored: "
    assert NOT_SCORED_PREFIX != REJECTION_REASON_PREFIX
    assert unusable.reason == (
        "Not scored: the model's answer was unusable after 3 attempts"
    )
    assert capped.reason == "Not scored: the run's cap of 1 postings was reached"
    assert [result.gaps for result in (unusable, capped)] == [[], []]
    assert [result.score for result in (unusable, capped)] == [0, 0]


def test_the_attempt_count_is_a_module_constant_of_three() -> None:
    """The total number of calls one posting's `score` makes, and not configurable."""
    assert SCORER_MAX_ATTEMPTS == 3


# The cap bounds one run, and the postings over it are reported rather than dropped.


def test_the_cap_bounds_one_run_and_the_postings_over_it_make_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    monkeypatch.setenv(SCORER_MAX_POSTINGS, "2")
    scorer = LlmScorer(client=_client_serving([], requests))

    results = [scorer.score(_posting(str(number)), RESUME_TEXT) for number in range(1, 5)]

    assert len(requests) == 2
    assert [result.score for result in results] == [87, 87, 0, 0]
    assert results[2] == results[3] == MatchResult(
        score=0, reason="Not scored: the run's cap of 2 postings was reached", gaps=[]
    )


def test_a_cap_of_zero_scores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    monkeypatch.setenv(SCORER_MAX_POSTINGS, "0")
    scorer = LlmScorer(client=_client_serving([], requests))

    result = scorer.score(_posting(), RESUME_TEXT)

    assert requests == []
    assert result == MatchResult(
        score=0, reason="Not scored: the run's cap of 0 postings was reached", gaps=[]
    )


def test_the_cap_is_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """An instance built before the variable was set still honours it."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    scorer = LlmScorer(client=_client_serving([], requests))

    monkeypatch.setenv(SCORER_MAX_POSTINGS, "1")
    assert scorer.score(_posting("1"), RESUME_TEXT).score == 87
    assert scorer.score(_posting("2"), RESUME_TEXT).score == 0

    assert len(requests) == 1


def test_a_posting_that_took_every_attempt_counts_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    monkeypatch.setenv(SCORER_MAX_POSTINGS, "1")
    scorer = LlmScorer(client=_client_serving([_response("junk")], requests))

    unusable = scorer.score(_posting("1"), RESUME_TEXT)
    capped = scorer.score(_posting("2"), RESUME_TEXT)

    assert len(requests) == 3
    assert unusable.reason == "Not scored: the model's answer was unusable after 3 attempts"
    assert capped.reason == "Not scored: the run's cap of 1 postings was reached"


def test_a_refused_call_does_not_consume_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(SCORER_MAX_POSTINGS, "1")
    scorer = LlmScorer(client=_client_serving([], requests))

    with pytest.raises(ScorerConfigurationError):
        scorer.score(_posting("1"), RESUME_TEXT)
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    assert scorer.score(_posting("1"), RESUME_TEXT).score == 87
    assert scorer.score(_posting("2"), RESUME_TEXT).score == 0

    assert len(requests) == 1


# One writer, on the single `match` row a posting is allowed -- on both engines.


def test_a_first_score_inserts_the_one_row_the_posting_is_allowed(engine: Engine) -> None:
    posting_id = _one_stored_posting()
    posting_before = _stored_posting(posting_id)
    result = MatchResult(score=87, reason="Fits the backend work.", gaps=["No Go.", "No on-call."])

    record_score(posting_id, result)

    rows = _matches()
    assert len(rows) == 1
    stored = rows[0]
    assert stored["posting_id"] == posting_id
    assert stored["score"] == 87
    assert stored["reason"] == "Fits the backend work."
    assert stored["gaps"] == '["No Go.", "No on-call."]'
    assert stored["state"] == "new"
    assert stored["notified_at"] is None
    assert stored["created_at"].tzinfo is not None
    assert stored["created_at"].utcoffset() == timedelta(0)
    assert datetime.now(UTC) - stored["created_at"] < timedelta(minutes=1)
    # The posting row, and every other table, are as they were.
    assert _stored_posting(posting_id) == posting_before
    assert _rows(Posting) == 1
    assert _rows(Run) == 0


def test_no_gaps_is_stored_as_an_empty_json_list(engine: Engine) -> None:
    posting_id = _one_stored_posting()

    record_score(posting_id, MatchResult(score=40, reason="Partly fits.", gaps=[]))

    assert _matches()[0]["gaps"] == "[]"
    assert json.loads(_matches()[0]["gaps"]) == []


def test_a_not_scored_result_is_stored_like_any_other(engine: Engine) -> None:
    """The model outage reaches the table: #9 stores it, and #10 cannot alert on it."""
    posting_id = _one_stored_posting()
    result = MatchResult(
        score=0, reason=f"{NOT_SCORED_PREFIX}the model call failed (HTTP 500)", gaps=[]
    )

    record_score(posting_id, result)

    stored = _matches()[0]
    assert stored["score"] == 0
    assert stored["reason"] == "Not scored: the model call failed (HTTP 500)"
    assert stored["gaps"] == "[]"


def test_a_score_over_a_recorded_rejection_updates_that_row_and_leaves_one(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`match.posting_id` is unique: the rejected row is the scored row, and it is updated."""
    posting_id = _one_stored_posting()
    monkeypatch.setenv("PREFILTER_TITLE_TERMS", "intern")
    record_rejection(posting_id, prefilter(_intern_posting()))
    rejected = _matches()[0]
    assert rejected["reason"].startswith(REJECTION_REASON_PREFIX)

    record_score(posting_id, MatchResult(score=70, reason="Fits the backend work.", gaps=[]))

    rows = _matches()
    assert len(rows) == 1
    assert rows[0]["id"] == rejected["id"]
    assert rows[0]["posting_id"] == posting_id
    assert rows[0]["created_at"] == rejected["created_at"]
    assert rows[0]["score"] == 70
    assert rows[0]["reason"] == "Fits the backend work."
    assert rows[0]["gaps"] == "[]"
    assert rows[0]["state"] == "new"
    assert rows[0]["notified_at"] is None


def test_a_second_score_with_the_same_result_leaves_the_one_row_as_it_was(engine: Engine) -> None:
    posting_id = _one_stored_posting()
    result = MatchResult(score=87, reason="Fits the backend work.", gaps=["No Go."])

    record_score(posting_id, result)
    first = _matches()[0]
    record_score(posting_id, result)

    rows = _matches()
    assert len(rows) == 1
    assert rows[0] == first


def test_a_second_score_updates_the_values_on_the_same_row(engine: Engine) -> None:
    posting_id = _one_stored_posting()
    record_score(posting_id, MatchResult(score=30, reason="Barely fits.", gaps=["No Go."]))
    first = _matches()[0]

    record_score(posting_id, MatchResult(score=91, reason="Fits now.", gaps=[]))

    rows = _matches()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["created_at"] == first["created_at"]
    assert (rows[0]["score"], rows[0]["reason"], rows[0]["gaps"]) == (91, "Fits now.", "[]")


def test_a_row_the_owner_saved_and_a_digest_notified_keeps_both(engine: Engine) -> None:
    """A re-score must not un-save a match or make the digest send it twice."""
    posting_id = _one_stored_posting()
    record_score(posting_id, MatchResult(score=30, reason="Barely fits.", gaps=[]))
    notified_at = _save_the_row_owner_style(posting_id)

    record_score(posting_id, MatchResult(score=91, reason="Fits now.", gaps=[]))

    stored = _matches()[0]
    assert stored["score"] == 91
    assert stored["state"] == "saved"
    assert stored["notified_at"] == notified_at


def test_scores_of_two_postings_are_two_rows(engine: Engine) -> None:
    first_posting, second_posting = _stored_postings(2)

    record_score(first_posting, MatchResult(score=87, reason="Fits.", gaps=[]))
    record_score(second_posting, MatchResult(score=12, reason="Does not fit.", gaps=["No Go."]))

    assert [(row["posting_id"], row["score"]) for row in _matches()] == [
        (first_posting, 87),
        (second_posting, 12),
    ]


# The module's boundaries: one place per variable, one writer, one model client.


def test_the_key_the_model_and_the_cap_are_read_in_one_module_only() -> None:
    """The one-place rule #7 proved for the rules, proved here for the model config.

    `scorer` is the one module that reads the three variables --
    `test_only_the_three_readers_touch_the_environment` is that proof -- and the
    resume parser #26 adds is the one module that names the key besides it, in
    the refusal it raises before any request.
    """
    naming = sorted(
        path.relative_to(BACKEND_ROOT).as_posix()
        for path in (BACKEND_ROOT / "app").rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name, _ in THE_THREE)
    )

    assert naming == ["app/matching/resume_fields.py", "app/matching/scorer.py"]


def test_only_the_three_readers_touch_the_environment() -> None:
    """`os.environ` appears in one reader per value, and nowhere else in the module."""
    reading: list[str] = []
    for node in ast.parse(SCORER_SOURCE).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(
            isinstance(inner, ast.Attribute) and inner.attr == "environ"
            for inner in ast.walk(node)
        ):
            reading.append(node.name)

    assert reading == ["api_key", "model", "max_postings"]


def test_only_the_recorder_opens_a_session() -> None:
    """One writer in this module, and `score` is not it."""
    opening: list[str] = []
    for node in ast.parse(SCORER_SOURCE).body:
        if not isinstance(node, ast.FunctionDef | ast.ClassDef):
            continue
        if any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "session_scope"
            for inner in ast.walk(node)
        ):
            opening.append(node.name)

    assert opening == ["record_score"]


def test_the_prefilter_reaches_no_network_and_the_model_path_is_the_two_model_modules() -> None:
    """#8's boundary: the prefilter still decides everything without a model or a key.

    Two modules touch a network client and only these two: `scorer` owns the one
    request, and the resume parser #26 adds holds the client it is handed and
    sends it through `scorer.ask_model`.
    """
    prefilter_source = (MATCHING_ROOT / "prefilter.py").read_text(encoding="utf-8")
    assert _imported_roots(prefilter_source) & NETWORK_MODULES == set()
    assert [term for term in MODEL_TERMS if term in prefilter_source] == []

    importing_the_network = sorted(
        path.name
        for path in MATCHING_ROOT.glob("*.py")
        if _imported_roots(path.read_text(encoding="utf-8")) & NETWORK_MODULES
    )

    assert importing_the_network == ["resume_fields.py", "scorer.py"]


def test_the_package_holds_the_prefilter_the_scorer_the_parser_and_their_prompts() -> None:
    modules = sorted(path.name for path in MATCHING_ROOT.glob("*.py"))

    assert modules == ["__init__.py", "prefilter.py", "resume_fields.py", "scorer.py"]
    assert sorted(path.name for path in MATCHING_ROOT.glob("*.txt")) == [
        "resume_prompt.txt",
        "score_prompt.txt",
    ]


def test_both_docstrings_describe_the_real_scorer_that_is_here() -> None:
    """The claim that the real scorer is another task's is gone from both docstrings."""
    scorer_docstring = " ".join((scorer_module.__doc__ or "").split())
    package_docstring = " ".join((matching_module.__doc__ or "").split())

    assert "nothing here scores anything" not in scorer_docstring
    assert "imports no HTTP client" not in package_docstring
    for docstring in (scorer_docstring, package_docstring):
        assert "LlmScorer" in docstring
        assert "score_prompt.txt" in docstring
    assert "NOT_SCORED_PREFIX" in scorer_docstring
    assert "Not scored: " in package_docstring
