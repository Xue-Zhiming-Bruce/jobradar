"""Parsing the active resume's text into the two fields a later comparison reads.

`ResumeFields` is what a parse answers with: the name the resume states and the
skills it claims, and `None`/`[]` for a value it does not state. `ResumeFieldParser`
is the interface -- one method, `parse`, over the resume's text and nothing else --
`FakeResumeParser` stands in for the real one so a test can assert what was parsed
and with which text without a model, a key or a network, and `LlmResumeParser` is
the real one.

The parse is one model call, and it is #8's call rather than a second one:
`LlmResumeParser` builds its request body with `scorer.request_body` and sends it
with `scorer.ask_model`, so the endpoint, the model, `temperature: 0`, the
JSON-object answer format, `MODEL_TIMEOUT_SECONDS` and the failure wording are the
scorer's, and a retry costs one of the scorer's own `SCORER_MAX_ATTEMPTS`. The
prompt is `resume_prompt.txt` beside this module, read on every parse rather than
at import, and it carries the resume's text verbatim.

The answer is read strictly and nothing is coerced: `name` has to be a non-blank
string or JSON `null`, `skills` a list of non-blank strings or JSON `null`, and
every other shape -- including a blank entry, a string sent where a list belongs,
or a body that is not the provider's envelope -- is malformed. A malformed answer
costs one attempt, and an answer still unusable after the last attempt raises
`ResumeParseError`. A transport failure does not retry: it answers at once, the
way the scorer's own call does.

The resume's text is user data and `LLM_API_KEY` is a credential, so neither
reaches a message: every failure this module raises is one of the scorer's own
wordings plus at most a status code, and the two configuration failures -- no key,
a blank resume -- are the scorer's `ScorerConfigurationError`, raised before any
request. Nothing here reads `os.environ`: the key comes from `scorer.api_key()`,
which is the one reader of that variable in `app/`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx2

from app.matching.scorer import (
    LLM_API_KEY,
    MODEL_TIMEOUT_SECONDS,
    SCORER_MAX_ATTEMPTS,
    ScorerConfigurationError,
    api_key,
    ask_model,
    request_body,
)

# The prompt, resolved from this file's location and never from the process's
# working directory, so `Dockerfile`'s `COPY app ./app` ships it and a run from
# `backend/` or from the repository root reads the same file.
PARSE_PROMPT_PATH = Path(__file__).parent / "resume_prompt.txt"


class ResumeParseError(RuntimeError):
    """The parse produced no fields: the call failed, timed out, or answered unusably.

    Raised rather than turned into an empty `ResumeFields`, because an unparsed
    resume and a resume with no name are two different states -- the first is a
    NULL column and a call to make, the second is a stored answer. The message is
    one of the scorer's own wordings plus at most a status code, and never the
    provider's response body, its raw output, the key or the resume's text.
    """


@dataclass(frozen=True)
class ResumeFields:
    """What one resume says about itself: its name, and the skills it claims.

    `None` and `[]` are the absent values the prompt asks the model for, so a
    resume that states no name and claims no skill is `ResumeFields(None, [])`
    and not an error: the parse succeeded and said so.
    """

    name: str | None
    skills: list[str]


@runtime_checkable
class ResumeFieldParser(Protocol):
    """Parsing one resume's text into its fields. The only method the route knows."""

    def parse(self, resume_text: str) -> ResumeFields:
        """Parse `resume_text` and answer the fields it states."""
        ...


class FakeResumeParser:
    """A `ResumeFieldParser` that reads nothing: it returns the fields it was built with.

    Every `resume_text` it was called with is appended to `calls`, so a test of
    the route can assert a parse ran once, with the active resume's text, without
    a model anywhere in reach.
    """

    def __init__(self, fields: ResumeFields) -> None:
        self.fields = fields
        self.calls: list[str] = []

    def parse(self, resume_text: str) -> ResumeFields:
        """Record the call and return the fields this fake was built with, unchanged."""
        self.calls.append(resume_text)
        return self.fields


class LlmResumeParser:
    """The real `ResumeFieldParser`: one bounded model call per resume, or a raised error.

    It satisfies the protocol by that one method and adds no method to it; the
    client is the only thing a caller may pass in, the way `LlmScorer` takes one
    and `greenhouse.fetch` does, so no key, model or timeout travels through a
    call. Nothing is read at construction: the configuration is read per parse.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = client

    def parse(self, resume_text: str) -> ResumeFields:
        """The fields `resume_text` states, or the typed error saying why there are none.

        Both configuration checks happen before any request, so an unconfigured
        JobRadar fails once and loudly instead of asking a model with no
        credential. A transport failure answers on the spot -- one request -- and
        a malformed answer costs an attempt: `SCORER_MAX_ATTEMPTS` requests and
        then `ResumeParseError`.
        """
        key = api_key()
        if not key:
            raise ScorerConfigurationError(
                f"{LLM_API_KEY} is not set: parsing a resume needs a model key"
            )
        if not resume_text.strip():
            raise ScorerConfigurationError(
                "No active resume text was supplied: upload a resume before parsing it"
            )

        body = request_body(_rendered_prompt(resume_text))
        client = self._client
        if client is None:
            client = httpx2.Client(timeout=MODEL_TIMEOUT_SECONDS)
        for _ in range(SCORER_MAX_ATTEMPTS):
            answer, failure = ask_model(client, body, key)
            if failure:
                raise ResumeParseError(failure)
            fields = _parsed(answer)
            if fields is not None:
                return fields
        raise ResumeParseError(
            f"the model's answer was unusable after {SCORER_MAX_ATTEMPTS} attempts"
        )


def _rendered_prompt(resume_text: str) -> str:
    """The whole prompt for one resume: the template, with the resume verbatim.

    The template is read here, on every parse, from this module's own directory,
    so editing `resume_prompt.txt` changes the next request without a restart and
    the file the image ships is the file this run reads. The text is substituted
    into the template, so it needs no escaping and is neither truncated nor
    summarised: the model is asked about the resume the owner uploaded.
    """
    template = PARSE_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(resume_text=resume_text)


def _parsed(answer: str) -> ResumeFields | None:
    """The fields in one answer, or `None` when the answer is unusable.

    The whole response contract in one place: the body is JSON,
    `choices[0].message.content` is a string, and that string is a JSON object
    whose `name` is a non-blank string or JSON `null` and whose `skills` is a list
    of non-blank strings or JSON `null`. Nothing is coerced, extra keys are
    ignored, and the values that do pass are returned as the model sent them, in
    its order.
    """
    try:
        payload = json.loads(answer)
    except ValueError:
        return None
    content = _content_of(payload)
    if content is None:
        return None
    try:
        stated = json.loads(content)
    except ValueError:
        return None
    if not isinstance(stated, dict):
        return None
    name = stated.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return None
    skills = stated.get("skills")
    if skills is None:
        skills = []
    elif not isinstance(skills, list) or not all(
        isinstance(skill, str) and skill.strip() for skill in skills
    ):
        return None
    return ResumeFields(name=name, skills=skills)


def _content_of(payload: object) -> str | None:
    """`choices[0].message.content` when the answer carries one, and `None` otherwise.

    The same envelope the scorer reads, spelled the same way: one provider, one
    Chat Completions answer, and the field sets the two parsers read are what
    differs between them.
    """
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
