"""The real scorer: one prompt, one bounded model call per posting, one writer.

`MatchResult` is what a scorer answers with: a score on the same 0-100 scale the
`match` table checks, a plain-English reason it fits and the gaps. `MatchScorer`
is the interface #9's tick calls once per survivor, `FakeScorer` stands in for
the real one so a test can assert what was scored and with which resume text
without a model, a key or a network, and `LlmScorer` is the real one.

`LlmScorer` makes one `POST` to `CHAT_COMPLETIONS_URL` -- OpenAI's Chat
Completions endpoint -- per posting, carrying the prompt rendered from
`PROMPT_PATH` (the file `score_prompt.txt` beside this module, read on every
call) and reading the answer at `choices[0].message.content`. The answer is
parsed strictly and nothing is coerced: a score that is not a JSON integer in
0-100, a reason that is not a non-blank string, or a `gaps` list holding anything
but strings is a malformed answer, and a malformed answer costs one of
`SCORER_MAX_ATTEMPTS` attempts. There is no LLM SDK in this project: the call is
`httpx2`, which ships with it.

The prompt carries the resume's text verbatim, because that is the point of the
task -- and the resume is user data, so it reaches the provider and appears in no
log line, no error message and no stored reason. `LLM_API_KEY` is that kind of
value too: `api_key()` is the only reader, it reads the environment on every call
and never at import, and the value goes into one request header and nowhere else.

Every way a call can fail -- a timeout, an unreachable endpoint, a non-2xx, an
answer still unusable after the last attempt, the run's cap -- is a normal
`MatchResult` with `score = 0`, `gaps = []` and a reason beginning
`NOT_SCORED_PREFIX`, *returned* rather than raised, because a model outage has to
reach #9's run row instead of its traceback. A score of 0 is below any cutoff, so
#10's digest cannot alert on one. The only exceptions `score` raises are
`ScorerConfigurationError`'s two -- no key, no resume text -- and both happen
before any request.

`record_score(posting_id, result)` is this module's only writer, the mirror of
`prefilter.record_rejection`: it stores the result on the single `match` row
`posting_id` is allowed, updating the row a rejection already wrote instead of
inserting a second one.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx2
from sqlalchemy import select

from app import db
from app.models import Match
from app.sources.greenhouse import SourcePosting

# The three variables, named here and read in no other module, the way the resume
# service and the prefilter read their own settings in one place.
LLM_API_KEY = "LLM_API_KEY"
LLM_MODEL = "LLM_MODEL"
SCORER_MAX_POSTINGS = "SCORER_MAX_POSTINGS"

# The one endpoint this module knows, and the one request per posting the plan
# asks for. A second provider or a configurable base URL is #32.
CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

# What an unset or empty variable means, declared rather than left to a library.
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_POSTINGS = 50

# The total number of calls one posting's `score` makes -- not three retries on
# top of an original -- and deliberately not configurable: an answer that cannot
# be read three times will not become readable on the fourth.
SCORER_MAX_ATTEMPTS = 3

# One constant for the request and for the client built when none is injected: a
# model that hangs is a not-scored result after half a minute, not a tick of #9
# held open past an HTTP timeout. Longer than the board adapter's ten seconds,
# because an answer takes longer to write than a payload takes to send.
MODEL_TIMEOUT_SECONDS = 30.0

# The marker #10 reads to keep a not-scored row out of the digest, distinct from
# `prefilter.REJECTION_REASON_PREFIX`: a 0 the scorer produced is not a rejection
# the prefilter decided.
NOT_SCORED_PREFIX = "Not scored: "

# What a posting without a location or a description reads as in the prompt.
NOT_STATED = "Not stated"

# The prompt, resolved from this file's location and never from the process's
# working directory, so `Dockerfile`'s `COPY app ./app` ships it and a run from
# `backend/` or from the repository root reads the same file.
PROMPT_PATH = Path(__file__).parent / "score_prompt.txt"

# One wording per cause, after `NOT_SCORED_PREFIX`.
_OUT_OF_TIME = "the model call did not answer in time"
_CALL_FAILED = "the model call failed"


class ScorerConfigurationError(RuntimeError):
    """Scoring was asked for without something it cannot work without.

    A missing key and an empty resume are both this error, raised before any
    request: an unconfigured JobRadar must fail loudly once instead of storing a
    confident-looking 0 for every posting. Neither message carries the key's
    value or any part of the resume's text, and neither is a `MatchResult` --
    that would be indistinguishable from a model that answered 0.
    """


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


def api_key() -> str:
    """`LLM_API_KEY`, or the empty string when it is unset or empty.

    Read per call and never at import, so a deployment sets the variable and a
    test works without one. The value is a credential: it goes into one request
    header and into nothing that is printed, stored or raised.
    """
    return os.environ.get(LLM_API_KEY, "").strip()


def model() -> str:
    """`LLM_MODEL`, or `DEFAULT_MODEL` when it is unset or empty."""
    return os.environ.get(LLM_MODEL, "").strip() or DEFAULT_MODEL


def max_postings() -> int:
    """`SCORER_MAX_POSTINGS`, or `DEFAULT_MAX_POSTINGS` when it is unset or empty.

    Read per call, so a deployment bounds a run by setting the variable and a test
    sets a small one instead of scoring fifty postings. `0` is a legitimate value:
    it scores nothing.
    """
    configured = os.environ.get(SCORER_MAX_POSTINGS, "").strip()
    return int(configured) if configured else DEFAULT_MAX_POSTINGS


def posting_text(posting: SourcePosting) -> str:
    """The posting as the prompt's three lines: title, location, description.

    A posting the source carried no location or no description for reads "Not
    stated" rather than "None", which is what the field's absence means.
    """
    location = posting.location or NOT_STATED
    description = posting.description or NOT_STATED
    return f"Title: {posting.title}\nLocation: {location}\nDescription: {description}"


def render_prompt(posting: SourcePosting, resume_text: str) -> str:
    """The whole prompt for one posting: the template, with the resume verbatim.

    The template is read here, on every call, from this module's own directory --
    editing `score_prompt.txt` changes the next request without a restart. The
    resume's text is substituted into the template, so it needs no escaping and is
    neither truncated nor summarised: the model is asked about the resume the
    owner uploaded, not about a paraphrase of it.
    """
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(resume_text=resume_text, posting_text=posting_text(posting))


class LlmScorer:
    """The real `MatchScorer`: the model's score for one posting, or a not-scored result.

    It satisfies the protocol by that one method and adds no method to it; the
    client is the only thing a caller may pass in, the way `greenhouse.fetch`
    takes one, so no key, model or cap travels through a call. A posting offered
    to `score` is counted on the instance and the count is the run's: #9 builds
    one `LlmScorer` per run, so `SCORER_MAX_POSTINGS` bounds that run and every
    posting over it is answered without a request.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = client
        self._offered = 0

    def score(self, posting: SourcePosting, resume_text: str) -> MatchResult:
        """The model's result for `posting`, or a `NOT_SCORED_PREFIX` result saying why not.

        Both configuration checks happen before the posting is counted and before
        any request; the count is incremented once per posting offered, whatever
        the calls that follow do.
        """
        key = api_key()
        if not key:
            raise ScorerConfigurationError(
                f"{LLM_API_KEY} is not set: scoring a posting needs a model key"
            )
        if not resume_text.strip():
            raise ScorerConfigurationError(
                "No active resume text was supplied: upload a resume before scoring"
            )
        limit = max_postings()
        if self._offered >= limit:
            return _not_scored(f"the run's cap of {limit} postings was reached")
        self._offered += 1

        body = request_body(render_prompt(posting, resume_text))
        client = self._client
        if client is None:
            client = httpx2.Client(timeout=MODEL_TIMEOUT_SECONDS)
        for _ in range(SCORER_MAX_ATTEMPTS):
            answer, failure = ask_model(client, body, key)
            if failure:
                return _not_scored(failure)
            result = _parsed(answer)
            if result is not None:
                return result
        return _not_scored(
            f"the model's answer was unusable after {SCORER_MAX_ATTEMPTS} attempts"
        )


def record_score(posting_id: int, result: MatchResult) -> None:
    """Record one scoring result on the `match` row keyed by `posting_id`.

    One `session_scope()` per call, the mirror of `prefilter.record_rejection`: a
    posting with no row gets one, written through the ORM so the model's own
    defaults fill `created_at` and the `state` of a fresh row. A posting that
    already has a row -- a previous score, or the `Prefiltered out: ...` row a
    rejection wrote -- is that same row: `match.posting_id` is unique, so the row
    is looked up and its `score`, `reason` and `gaps` are written, where inserting
    blind would be an `IntegrityError`. A posting scored after it was prefiltered
    out therefore no longer reads as a rejection to #10.

    `state` and `notified_at` are left alone on an update, so a row the owner
    saved stays saved and a digest that already went out is not re-sent. `gaps` is
    stored as JSON text -- "[]" for no gaps -- which is the format #10 and #12 read.
    """
    gaps = json.dumps(result.gaps)
    with db.session_scope() as session:
        row = session.scalar(select(Match).where(Match.posting_id == posting_id))
        if row is None:
            session.add(
                Match(
                    posting_id=posting_id,
                    score=result.score,
                    reason=result.reason,
                    gaps=gaps,
                )
            )
        else:
            row.score = result.score
            row.reason = result.reason
            row.gaps = gaps


def request_body(prompt: str) -> dict[str, object]:
    """The request body: four fields, and nothing that would change the answer.

    Public because it is one half of the seam #26's resume parser reuses: the
    body, and `ask_model` below, so a second model path cannot drift from this
    one's URL, model, temperature or answer format.


    No `tools`, no `stream`, no second request: one user message holding the
    rendered prompt, `temperature: 0` because a posting's score should not depend
    on the day it was scored, and a JSON-object answer rather than prose.
    """
    return {
        "model": model(),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def ask_model(client: httpx2.Client, body: dict[str, object], key: str) -> tuple[str, str]:
    """One call, as `(answer body, "")` or `("", reason there is none)`.

    Public for the reason `request_body` gives: #26's resume parser sends the
    same request with this same function, so the timeout, the endpoint and the
    failure wording have one spelling.

    Every `httpx2` failure is turned into a reason here, so none of them escapes
    `score`. The reason names a status or nothing at all: the response body is
    arbitrary, and a reason reaches a `match` row and a run row (#9).
    """
    try:
        response = client.post(
            CHAT_COMPLETIONS_URL,
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=MODEL_TIMEOUT_SECONDS,
        )
    except httpx2.TimeoutException:
        return "", _OUT_OF_TIME
    except httpx2.HTTPError:
        return "", _CALL_FAILED
    if not response.is_success:
        return "", f"{_CALL_FAILED} (HTTP {response.status_code})"
    return response.text, ""


def _parsed(answer: str) -> MatchResult | None:
    """The result in one answer, or `None` when the answer is unusable.

    The whole response contract in one place: the body is JSON, `choices` is a
    non-empty list, `choices[0].message.content` is a string, and that string is a
    JSON object whose `score` is a JSON integer in 0-100, whose `reason` is a
    non-blank string and whose `gaps` is a list of strings. Nothing is coerced
    (`85.0`, `"85"` and `true` are all malformed), extra keys are ignored, and the
    values that do pass are returned as the model sent them, in its order.
    """
    try:
        payload = json.loads(answer)
    except ValueError:
        return None
    content = _content_of(payload)
    if content is None:
        return None
    try:
        fields = json.loads(content)
    except ValueError:
        return None
    if not isinstance(fields, dict):
        return None
    score = fields.get("score")
    reason = fields.get("reason")
    gaps = fields.get("gaps")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    if not isinstance(gaps, list) or not all(isinstance(gap, str) for gap in gaps):
        return None
    return MatchResult(score=score, reason=reason, gaps=gaps)


def _content_of(payload: object) -> str | None:
    """`choices[0].message.content` when the answer carries one, and `None` otherwise."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _not_scored(reason: str) -> MatchResult:
    """One failure as an ordinary result: no score, no gaps, and the prefix #10 reads.

    Returned rather than raised, so #9 stores it like any other score and the
    digest cannot alert on a 0. The reason is built from this module's own words
    and a status: never the model's raw output and never the key.
    """
    return MatchResult(score=0, reason=NOT_SCORED_PREFIX + reason, gaps=[])
