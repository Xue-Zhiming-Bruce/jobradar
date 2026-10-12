"""The one seam that turns a posting into a score, and the fake the suite uses.

`MatchResult` is what a scorer answers with: a score on the same 0-100 scale the
`match` table checks, a plain-English reason it fits and the gaps. `MatchScorer`
is the interface #9's tick calls once per survivor, and `FakeScorer` stands in
for the real implementation so a test can assert what was scored and with which
resume text without a model, a key or a network.

Nothing here scores anything: this module imports no LLM client and no HTTP
client, reads no API key and calls nothing. The implementation -- the prompt, the
structured output, the retries and the cap -- is #8's, and it is also #8's write
that stores a result in `match`.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.sources.greenhouse import SourcePosting


@dataclass(frozen=True)
class MatchResult:
    """How well one posting fits: the score, why, and what is missing."""

    score: int
    reason: str
    gaps: list[str]


@runtime_checkable
class MatchScorer(Protocol):
    """Scoring one posting against one resume. The only method #9 knows about."""

    def score(self, posting: SourcePosting, resume_text: str) -> MatchResult:
        """Score `posting` against `resume_text` and say why."""
        ...


class FakeScorer:
    """A `MatchScorer` that decides nothing: it returns the result it was built with.

    Every `(posting, resume_text)` pair it was called with is appended to `calls`,
    so a test of #9's tick can assert the scorer ran once per survivor, with the
    active resume's text, without a model anywhere in reach.
    """

    def __init__(self, result: MatchResult) -> None:
        self.result = result
        self.calls: list[tuple[SourcePosting, str]] = []

    def score(self, posting: SourcePosting, resume_text: str) -> MatchResult:
        """Record the call and return the result this fake was built with, unchanged."""
        self.calls.append((posting, resume_text))
        return self.result
