"""Reading a scanned resume's pages into text, through the one model path this app has.

`ResumeOcr` is the interface -- one method, `read`, over an upload's media type and
its bytes -- `FakeResumeOcr` stands in for the real one so a route test can assert
what was read and with which bytes without a model, a key or a network, and
`LlmResumeOcr` is the real one: one vision call per window of at most
`OCR_MAX_IMAGES` page images, and nothing else in this project calls a model for
an upload.

The call is the scorer's, not a second one. `LlmResumeOcr` builds its request body
with `scorer.request_body` -- the page images attached, which is the one argument
#26's parser does not pass -- and sends it with `scorer.ask_model`, so the
endpoint, the model, `temperature: 0`, the JSON-object answer format,
`MODEL_TIMEOUT_SECONDS` and the failure wording are the scorer's, and a retry costs
one of the scorer's own `SCORER_MAX_ATTEMPTS`. The pages come from
`app.services.resume.scan_images`: this module owns the call, that one owns what a
scan's images are. A scan with more page images than `OCR_MAX_IMAGES` is read by
consecutive bounded requests -- one per window of at most `OCR_MAX_IMAGES`
images, in page order -- and the window answers are joined in order by one `"\n"`,
the separator `_pdf_text` puts between a PDF's own pages. A scan is bounded as a
whole as well as per request: `read` takes the clock before it starts decoding and
ever sends an attempt once `OCR_SCAN_TIMEOUT_SECONDS` is spent, which is the one
ceiling on how long one upload may hold its request open. The prompt is
`ocr_prompt.txt` beside this module, read on every scan rather than at import, and
it carries no text -- the pages are the input.

The answer is read strictly and nothing is coerced: `choices[0].message.content`
has to be a JSON object whose `text` is a string or JSON `null`, and every other
shape -- a body that is not the provider's envelope, a number, a boolean, a list --
is malformed. JSON `null` is the model saying nothing was legible, and an empty or
whitespace-only answer is an answer too: what to do with a scan that yielded no
text is the route's refusal to make, not this module's. A malformed answer costs
one attempt, and an answer still unusable after the last attempt raises
`ResumeOcrError`. A transport failure does not retry: it answers at once, the way
the scorer's own call does.

The page images are user data and `LLM_API_KEY` is a credential, so neither reaches
a message or a log line: every failure this module raises is one of the scorer's
own wordings, this module's own wording for a scan over its time budget, plus at
most a status code, the one log line it writes names the media type, the byte
count, the number of page images and the number of requests and never a pixel of
the scan, and the key is read through `scorer.api_key()`.
Nothing here reads `os.environ`, and no byte of the scan is written to a file: the
images are request payloads and nothing else.
"""

import json
import logging
import time
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
from app.services import resume as resume_service

logger = logging.getLogger(__name__)

# One scan's wall clock: the decoding of its page images and every request made
# for them, measured from the moment `read` starts that work. This repository
# documents no platform request timeout, so this is the only ceiling an upload
# holds -- and 60 s is stated against the worst case it replaces and against the
# one request budget the repository names. `ceil(OCR_MAX_SCAN_IMAGES /
# OCR_MAX_IMAGES)` = 4 windows of up to `SCORER_MAX_ATTEMPTS` x
# `MODEL_TIMEOUT_SECONDS` = 90 s each is 360 s for one upload, which this bounds
# at `OCR_SCAN_TIMEOUT_SECONDS` + `MODEL_TIMEOUT_SECONDS` = 90 s: the budget is
# checked before an attempt is sent, and `ask_model` sets that attempt's own
# `timeout=MODEL_TIMEOUT_SECONDS`, so the last attempt may start just inside the
# budget and still answer up to one model timeout later. `tick.yml` polls on the
# same 60 s. A module constant rather than a setting, the way
# `resume.OCR_MAX_SCAN_IMAGES` is.
OCR_SCAN_TIMEOUT_SECONDS = 60.0

# The prompt, resolved from this file's location and never from the process's
# working directory, so `Dockerfile`'s `COPY app ./app` ships it and a run from
# `backend/` or from the repository root reads the same file.
OCR_PROMPT_PATH = Path(__file__).parent / "ocr_prompt.txt"


class ResumeOcrError(RuntimeError):
    """A scan produced no text: the call failed, timed out, or answered unusably.

    Raised rather than turned into the empty string, because "the model could not be
    reached" and "the model read the pages and they said nothing" are two different
    answers -- a `502` and a `422`. The message is one of the scorer's own wordings
    plus at most a status code, and never the provider's response body, its raw
    output, the key or any part of the scan.
    """


@runtime_checkable
class ResumeOcr(Protocol):
    """Reading one scanned upload into text. The only method the route knows."""

    def read(self, content_type: str, data: bytes) -> str:
        """Read `data`, an upload declared `content_type`, and answer the text it carries."""
        ...


class FakeResumeOcr:
    """A `ResumeOcr` that reads nothing: it returns the text it was built with.

    Every `(content_type, data)` pair it was called with is appended to `calls`, so
    a test of the route can assert that a scan was read, and which upload's bytes it
    was read from, without a model anywhere in reach. `data` is what the route was
    sent, not the page images: proving the images is `LlmResumeOcr`'s own test.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, bytes]] = []

    def read(self, content_type: str, data: bytes) -> str:
        """Record the call and return the text this fake was built with, unchanged."""
        self.calls.append((content_type, data))
        return self.text


class LlmResumeOcr:
    """The real `ResumeOcr`: one bounded vision call per scan, or a raised error.

    It satisfies the protocol by that one method and adds no method to it; the
    client is the only thing a caller may pass in, the way `LlmResumeParser` takes
    one and `greenhouse.fetch` does, so no key, model or timeout travels through a
    call. Nothing is read at construction: the configuration is read per scan.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = client

    def read(self, content_type: str, data: bytes) -> str:
        """The text the pages of `data` carry, or the typed error saying why there is none.

        The configuration check happens before any request, so an unconfigured
        JobRadar fails once and loudly instead of asking a model with no
        credential. A scan whose pages hold no readable image answers the empty
        string without a request -- there is nothing to send. Otherwise the page
        images are sliced into consecutive windows of at most `OCR_MAX_IMAGES`
        and one request is made per window, in page order, the answers joined in
        order by one `"\n"`. A transport failure answers on the spot -- one
        request for that window -- and a malformed answer costs an attempt:
        `SCORER_MAX_ATTEMPTS` requests and then `ResumeOcrError`, which fails the
        whole scan rather than answering part of it.

        The clock is taken here, once, before the page images are decoded, so the
        whole scan -- that decoding and every request -- is bounded by
        `OCR_SCAN_TIMEOUT_SECONDS`: a scan whose budget is spent fails as the same
        `ResumeOcrError`, raised before the attempt that would have outspent it
        rather than after it.
        """
        key = api_key()
        if not key:
            raise ScorerConfigurationError(
                f"{LLM_API_KEY} is not set: reading a scan needs a model key"
            )

        deadline = time.monotonic() + OCR_SCAN_TIMEOUT_SECONDS
        images = resume_service.scan_images(content_type, data)
        if not images:
            return ""
        windows = [
            images[start : start + resume_service.OCR_MAX_IMAGES]
            for start in range(0, len(images), resume_service.OCR_MAX_IMAGES)
        ]
        client = self._client
        if client is None:
            client = httpx2.Client(timeout=MODEL_TIMEOUT_SECONDS)
        logger.info(
            "Reading a scan: %s, %s bytes, %s page images, %s requests",
            content_type,
            len(data),
            len(images),
            len(windows),
        )
        return "\n".join(
            _read_window(client, window, key, deadline, index, len(windows))
            for index, window in enumerate(windows)
        )


def _read_window(
    client: httpx2.Client,
    window: list[bytes],
    key: str,
    deadline: float,
    window_index: int,
    window_count: int,
) -> str:
    """One window's text, read by one request, or the typed error saying why there is none.

    The body is `scorer.request_body(_prompt(), window)` and the call is
    `scorer.ask_model`'s, so a window is the one request #27 pinned: at most
    `OCR_MAX_IMAGES` image parts. A malformed answer costs one of the scorer's
    own `SCORER_MAX_ATTEMPTS`; a transport failure answers at once. Every attempt
    -- the first try and each retry -- checks `deadline` before it is sent, so a
    scan cannot outspend `OCR_SCAN_TIMEOUT_SECONDS` however the model answers;
    what the check bounds is the sending, not the answer after it.
    """
    spent = _spent_budget(deadline, window_index, window_count)
    if spent:
        raise ResumeOcrError(spent)
    body = request_body(_prompt(), window)
    for _ in range(SCORER_MAX_ATTEMPTS):
        spent = _spent_budget(deadline, window_index, window_count)
        if spent:
            raise ResumeOcrError(spent)
        answer, failure = ask_model(client, body, key)
        if failure:
            raise ResumeOcrError(failure)
        text = _read(answer)
        if text is not None:
            return text
    raise ResumeOcrError(
        f"the model's answer was unusable after {SCORER_MAX_ATTEMPTS} attempts"
    )


def _spent_budget(deadline: float, window_index: int, window_count: int) -> str | None:
    """The wording for a scan whose budget is spent, or `None` while there is time left.

    One wording for both places an attempt is checked -- a window's first try and
    each retry -- so a failure names the same budget and the same window counts
    however late in a scan it happens: how many windows were read whole, and how
    many the scan holds. No page image, byte or text of the scan is in it. The
    rule is `>`, as the byte limit's `len(data) > limit` reads: an attempt is
    refused once the deadline is passed, and an instant exactly on it still has
    its attempt sent.
    """
    if time.monotonic() <= deadline:
        return None
    return (
        f"the scan spent its {OCR_SCAN_TIMEOUT_SECONDS:g} second budget after "
        f"{window_index} of {window_count} windows"
    )


def _prompt() -> str:
    """The prompt for one scan, read from this module's own directory on every call.

    Read here rather than at import, so editing `ocr_prompt.txt` changes the next
    request without a restart and the file the image ships is the file this run
    reads. It is used whole: unlike the scorer's and the parser's prompts it
    substitutes nothing, because the scan's pages are the input.
    """
    return OCR_PROMPT_PATH.read_text(encoding="utf-8")


def _read(answer: str) -> str | None:
    """The text in one answer, or `None` when the answer is unusable.

    The whole response contract in one place: the body is JSON,
    `choices[0].message.content` is a string, and that string is a JSON object
    whose `text` is a string or JSON `null`. Nothing is coerced -- a number, a
    boolean and a list are all malformed -- extra keys are ignored, and the text
    that does pass is returned exactly as the model sent it, unescaped and
    untruncated. JSON `null` and `""` are both answers: the pages held no legible
    text, which is a `422` and not a malformed answer to retry.
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
    text = stated.get("text")
    if text is None:
        return ""
    return text if isinstance(text, str) else None


def _content_of(payload: object) -> str | None:
    """`choices[0].message.content` when the answer carries one, and `None` otherwise.

    The same envelope the scorer and the parser read, spelled the same way: one
    provider, one Chat Completions answer, and the field the three of them read
    is what differs between them.
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
