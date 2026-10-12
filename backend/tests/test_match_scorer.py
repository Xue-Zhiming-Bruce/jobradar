"""The scorer seam: the result shape, the interface, and the fake the suite scores with.

Nothing here reaches a model, a key or a network. The autouse fixture patches
`socket.socket.connect` to raise, the way `test_greenhouse_source.py` does, so an
implementation that called out fails inside the test, and the package's own
source is read to prove it imports no client and names no key. The real scorer --
the prompt, the structured output and the retries -- is #8's, and #8 writes its
test too; this module proves only the seam #8 and #9 build on.
"""

import ast
import inspect
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.matching.scorer import FakeScorer, MatchResult, MatchScorer
from app.sources.greenhouse import SourcePosting

BACKEND_ROOT = Path(__file__).resolve().parent.parent
MATCHING_ROOT = BACKEND_ROOT / "app" / "matching"

# The modules a network client would come from, and the terms that would name a
# model or its credential: the package imports neither and reads no key.
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}
MODEL_TERMS = ("api_key", "api-key", "openai", "anthropic", "Authorization")


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoring here is a fake: a socket opened in this module fails the test."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _posting(external_id: str = "1") -> SourcePosting:
    """A posting as the adapter would hand it over: synthetic, and this module's own."""
    return SourcePosting(
        external_id=external_id,
        title=f"Backend Engineer {external_id}",
        location="Berlin",
        url=f"https://example.test/{external_id}",
        description="We use Python and PostgreSQL daily.",
    )


def _result() -> MatchResult:
    return MatchResult(score=87, reason="Synthetic fit.", gaps=["No Kubernetes."])


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


def test_the_package_imports_no_client_and_names_no_model_or_key() -> None:
    """No module here implements the seam against a model: that is #8, and not in this package."""
    for module in sorted(MATCHING_ROOT.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert imported & NETWORK_MODULES == set(), module.name
        assert [term for term in MODEL_TERMS if term in source] == [], module.name


def test_the_package_holds_only_the_prefilter_and_the_scorer() -> None:
    modules = sorted(path.name for path in MATCHING_ROOT.glob("*.py"))

    assert modules == ["__init__.py", "prefilter.py", "scorer.py"]
