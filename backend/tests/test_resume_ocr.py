"""The scan reader's seam, the one vision call it sends, and the answer shapes it reads.

The interface, the fake and the real reader are #27's; the request body, the
endpoint, the timeout, the attempt count and the failure wording are #8's,
reused through `app.matching.scorer`. `LlmResumeOcr` is driven here the way the
suite drives every HTTP client in this project: through `httpx2.MockTransport`,
with the autouse fixtures below patching `socket.socket.connect` to raise and
deleting `LLM_API_KEY`, so an implementation that fell through to a client of its
own, or that read a real key from the shell, fails here instead of quietly
passing. This module is the only place the real reader runs: every route case
installs `FakeResumeOcr`.

The scan is one small synthetic PNG made by this module: the repository is
public and holds no resume, real or otherwise. Every assertion is on a value --
the exact request and the count of them, the exact text or the exact exception
each answer shape produces. The key, the scan and the provider's own body are
sentinels, and the case at the end proves none of them reaches a message.
"""

import ast
import base64
import inspect
import json
import logging
import socket
from io import BytesIO
from pathlib import Path

import httpx2
import pytest
from PIL import Image

from app.matching import resume_ocr as resume_ocr_module
from app.matching import scorer as scorer_module
from app.matching.resume_ocr import (
    OCR_PROMPT_PATH,
    FakeResumeOcr,
    LlmResumeOcr,
    ResumeOcr,
    ResumeOcrError,
)
from app.matching.scorer import (
    DEFAULT_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    MODEL_TIMEOUT_SECONDS,
    SCORER_MAX_ATTEMPTS,
    ScorerConfigurationError,
    api_key,
    chat_completions_url,
)
from app.services import resume as resume_service

BACKEND_ROOT = Path(__file__).resolve().parent.parent
MATCHING_ROOT = BACKEND_ROOT / "app" / "matching"
RESUME_OCR_MODULE = MATCHING_ROOT / "resume_ocr.py"
RESUME_OCR_SOURCE = RESUME_OCR_MODULE.read_text(encoding="utf-8")
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The endpoint, spelled out rather than imported, so a moved one fails here.
THE_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# A synthetic gateway: not a real host, and the base URL no other case configures.
TEST_BASE_URL = "https://gateway.example.test/v1"

IMAGE_PNG = "image/png"
APPLICATION_PDF = "application/pdf"

# A recognisable key value and a recognisable scan: neither is a real credential,
# a real resume, or allowed to appear in a raised message.
THE_KEY = "unit-test-key-not-a-real-credential"
SCAN_TEXT = "Scanned resume: Python, PostgreSQL, and a little Rust."

# A PDF whose page carries no image, so the reader has nothing to send.
BLANK_PAGE_PDF = FIXTURES / "blank-page.pdf"


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """No case here reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key and no gateway, whatever its shell holds."""
    monkeypatch.delenv(LLM_API_KEY, raising=False)
    monkeypatch.delenv(LLM_BASE_URL, raising=False)


def _scan_png(size: tuple[int, int] = (40, 30)) -> bytes:
    """One synthetic one-page scan: a small greyscale PNG, made here, holding no real text."""
    buffer = BytesIO()
    Image.new("L", size, color=200).save(buffer, format="PNG")
    return buffer.getvalue()


def _response(body: str, *, status: int = 200) -> httpx2.Response:
    """One HTTP answer whose body is exactly `body`: the response's own shape is the case."""
    return httpx2.Response(status, content=body)


def _completion(content: str) -> str:
    """A well-formed Chat Completions body whose one message's content is `content`."""
    return json.dumps({"choices": [{"message": {"content": content}}]})


def _answer(stated: dict) -> str:
    """One well-formed answer whose inner object is `stated`."""
    return _completion(json.dumps(stated))


def _serving(
    responses: list[httpx2.Response | Exception], requests: list[httpx2.Request]
) -> httpx2.Client:
    """A stub provider that answers with each response in turn, repeating the last.

    It records every request and serves this module's endpoint and no other: a
    second URL, a second request shape or a second provider fails here. A response
    that is an exception is raised instead, so a timeout or a refused connection
    is one case of the same stub.
    """
    served: list[httpx2.Response | Exception] = responses or [
        _response(_answer({"text": SCAN_TEXT}))
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


def _prompt_text() -> str:
    """The prompt file itself, read here rather than through the module's own reader."""
    return OCR_PROMPT_PATH.read_text(encoding="utf-8")


def _expected_content(png: bytes) -> list[dict]:
    """The message's content for that scan: the prompt, then one data-URL part per page image."""
    return [
        {"type": "text", "text": _prompt_text()},
        *(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(page).decode("ascii")
                },
            }
            for page in resume_service.scan_images(IMAGE_PNG, png)
        ),
    ]


def _distinct_pages(count: int) -> bytes:
    """A scan of `count` page images, each a different size, so their order is provable."""
    images = [Image.new("L", (40 + index, 30), color=200) for index in range(count)]
    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def _window_pages(request: httpx2.Request) -> list[bytes]:
    """The page images one request carried, decoded back from its data URLs."""
    parts = _body_of(request)["messages"][0]["content"]
    return [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in parts[1:]
    ]


# The seam: one method over a media type and the upload's bytes, and one fake to read.


def test_a_fake_reader_is_a_resume_ocr() -> None:
    assert isinstance(FakeResumeOcr("text"), ResumeOcr)


def test_a_call_returns_the_text_the_fake_was_built_with_unchanged() -> None:
    text = SCAN_TEXT
    reader = FakeResumeOcr(text)

    assert reader.read(IMAGE_PNG, b"synthetic scan bytes") is text


def test_the_fake_records_every_call_as_the_content_type_and_the_bytes() -> None:
    """What a route test reads to prove a scan was read once, and which bytes were read."""
    reader = FakeResumeOcr(SCAN_TEXT)

    reader.read(IMAGE_PNG, b"first scan")
    reader.read(APPLICATION_PDF, b"second scan")

    assert reader.calls == [(IMAGE_PNG, b"first scan"), (APPLICATION_PDF, b"second scan")]


def test_the_seam_is_one_method_over_a_media_type_and_the_bytes() -> None:
    """The shape the route calls, named here so a change to it fails loudly."""
    assert list(inspect.signature(ResumeOcr.read).parameters) == [
        "self",
        "content_type",
        "data",
    ]
    assert inspect.signature(ResumeOcr.read).return_annotation is str
    assert list(inspect.signature(FakeResumeOcr).parameters) == ["text"]
    assert list(inspect.signature(LlmResumeOcr).parameters) == ["client"]
    assert hasattr(FakeResumeOcr(SCAN_TEXT), "calls")


def test_the_real_reader_satisfies_the_protocol_without_moving_it() -> None:
    """One method, the protocol's, and one constructor parameter: the injected client."""
    assert isinstance(LlmResumeOcr(), ResumeOcr)
    assert ResumeOcr not in LlmResumeOcr.__mro__
    assert list(inspect.signature(LlmResumeOcr.read).parameters) == [
        "self",
        "content_type",
        "data",
    ]
    assert [name for name in vars(LlmResumeOcr) if not name.startswith("_")] == ["read"]


def test_constructing_the_reader_reads_no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `LlmResumeOcr` is inert: reading nothing and opening nothing."""

    def refuse(*args: object, **kwargs: object) -> httpx2.Client:
        raise AssertionError("constructing the reader built a client")

    monkeypatch.setattr(httpx2, "Client", refuse)

    LlmResumeOcr()

    assert api_key() == ""


# One spelling per thing: the scorer's constants, its endpoint and no other.


def test_the_call_bounds_are_the_scorer_s_own_constants() -> None:
    assert resume_ocr_module.SCORER_MAX_ATTEMPTS is scorer_module.SCORER_MAX_ATTEMPTS == 3
    assert resume_ocr_module.MODEL_TIMEOUT_SECONDS is scorer_module.MODEL_TIMEOUT_SECONDS
    assert resume_ocr_module.LLM_API_KEY is scorer_module.LLM_API_KEY


def test_the_module_declares_no_endpoint_of_its_own() -> None:
    """A second URL would be a second provider: the call is `scorer.ask_model`'s."""
    declared = [
        node.value
        for node in ast.walk(ast.parse(RESUME_OCR_SOURCE))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert [value for value in declared if value.startswith("http")] == []


def test_the_module_reads_the_environment_nowhere() -> None:
    """The key comes from `scorer.api_key()`, which stays the one reader of it in `app/`."""
    reads = [
        node.attr
        for node in ast.walk(ast.parse(RESUME_OCR_SOURCE))
        if isinstance(node, ast.Attribute) and node.attr == "environ"
    ]

    assert reads == []
    assert "import os" not in RESUME_OCR_SOURCE


# The configuration: the scorer's refusal, before any request and before any page image.


def test_a_missing_key_is_a_configuration_error_before_any_request() -> None:
    requests: list[httpx2.Request] = []
    reader = LlmResumeOcr(client=_serving([], requests))

    with pytest.raises(ScorerConfigurationError) as raised:
        reader.read(IMAGE_PNG, _scan_png())

    assert LLM_API_KEY in str(raised.value)
    assert str(raised.value) == f"{LLM_API_KEY} is not set: reading a scan needs a model key"
    assert requests == []


@pytest.mark.parametrize("configured", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_key_is_a_configuration_error_too(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, configured)
    reader = LlmResumeOcr(client=_serving([], requests))

    with pytest.raises(ScorerConfigurationError):
        reader.read(IMAGE_PNG, _scan_png())

    assert requests == []


# The request: one vision call, with #8's body and #8's timeout.


def test_one_vision_request_carries_the_prompt_and_the_page_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    png = _scan_png()
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(client=_serving([_response(_answer({"text": SCAN_TEXT}))], requests))

    assert reader.read(IMAGE_PNG, png) == SCAN_TEXT

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == THE_ENDPOINT == chat_completions_url()
    assert request.headers["authorization"] == f"Bearer {THE_KEY}"
    assert _body_of(request) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": _expected_content(png)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    assert request.extensions["timeout"]["connect"] == MODEL_TIMEOUT_SECONDS
    assert request.extensions["timeout"]["read"] == MODEL_TIMEOUT_SECONDS


def test_the_configured_base_url_is_where_the_scan_request_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint is the scorer's, not a copy of it: `LLM_BASE_URL` moves this call too."""
    requests: list[httpx2.Request] = []
    png = _scan_png()
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    monkeypatch.setenv(LLM_BASE_URL, TEST_BASE_URL)

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _response(_answer({"text": SCAN_TEXT}))

    reader = LlmResumeOcr(client=httpx2.Client(transport=httpx2.MockTransport(handle)))

    assert reader.read(IMAGE_PNG, png) == SCAN_TEXT
    assert [str(request.url) for request in requests] == [f"{TEST_BASE_URL}/chat/completions"]


def test_the_image_parts_are_one_png_data_url_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one format `scan_images` produces reaches the model, whatever the scanner stored."""
    requests: list[httpx2.Request] = []
    png = _scan_png()
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    LlmResumeOcr(client=_serving([_response(_answer({"text": SCAN_TEXT}))], requests)).read(
        IMAGE_PNG, png
    )

    parts = _body_of(requests[0])["messages"][0]["content"]
    pages = resume_service.scan_images(IMAGE_PNG, png)
    assert len(pages) == 1 == len(parts) - 1
    (image_part,) = parts[1:]
    assert image_part["type"] == "image_url"
    data_url = image_part["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(data_url.split(",", 1)[1])
    assert decoded == pages[0]
    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")


def test_request_body_without_images_is_the_body_the_scorer_sends_byte_for_byte() -> None:
    """Every call site #8 and #26 have passes a prompt and nothing else."""
    expected = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "A prompt, and no pictures."}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    body = scorer_module.request_body("A prompt, and no pictures.")

    assert body == expected
    assert list(body) == ["model", "messages", "temperature", "response_format"]
    assert json.dumps(body) == json.dumps(expected)
    # "No images" is spelled both ways a caller might: absent, and an empty list.
    assert scorer_module.request_body("A prompt, and no pictures.", None) == expected
    assert scorer_module.request_body("A prompt, and no pictures.", []) == expected


def test_a_scan_with_no_page_images_answers_nothing_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF whose pages hold no image has nothing to send, so no call is made for it."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(client=_serving([], requests))

    assert reader.read(APPLICATION_PDF, BLANK_PAGE_PDF.read_bytes()) == ""
    assert requests == []


# The window: one request per `OCR_MAX_IMAGES` page images, answers joined in page order.


def test_a_scan_at_the_cap_makes_one_request_with_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    pdf = _distinct_pages(resume_service.OCR_MAX_IMAGES)
    reader = LlmResumeOcr(client=_serving([_response(_answer({"text": SCAN_TEXT}))], requests))

    assert reader.read(APPLICATION_PDF, pdf) == SCAN_TEXT

    assert len(requests) == 1
    pages = resume_service.scan_images(APPLICATION_PDF, pdf)
    assert len(pages) == resume_service.OCR_MAX_IMAGES == 5
    assert _window_pages(requests[0]) == pages


def test_a_scan_one_page_past_the_cap_makes_two_requests_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    pdf = _distinct_pages(resume_service.OCR_MAX_IMAGES + 1)
    reader = LlmResumeOcr(
        client=_serving(
            [
                _response(_answer({"text": "pages one to five"})),
                _response(_answer({"text": "the sixth page"})),
            ],
            requests,
        )
    )

    assert reader.read(APPLICATION_PDF, pdf) == "pages one to five\nthe sixth page"

    assert len(requests) == 2
    pages = resume_service.scan_images(APPLICATION_PDF, pdf)
    assert len(pages) == 6
    assert [_window_pages(request) for request in requests] == [pages[0:5], pages[5:6]]


def test_a_scan_many_times_the_cap_makes_one_request_per_window_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    pdf = _distinct_pages(12)
    answers = ["window one", "window two", "window three"]
    reader = LlmResumeOcr(
        client=_serving([_response(_answer({"text": text})) for text in answers], requests)
    )

    assert reader.read(APPLICATION_PDF, pdf) == "\n".join(answers)

    assert len(requests) == 3
    pages = resume_service.scan_images(APPLICATION_PDF, pdf)
    assert len(pages) == 12
    # Two windows of five and one of two: no body carries more than the cap.
    assert [len(_window_pages(request)) for request in requests] == [5, 5, 2]
    assert [_window_pages(request) for request in requests] == [
        pages[0:5],
        pages[5:10],
        pages[10:12],
    ]


def test_the_joined_text_is_the_window_answers_joined_by_one_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window's answer is stored exactly as it arrived, and one `"\n"` joins the windows."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    first = 'Line one\n\n  indented "quotes" \u2014 \u00fcn\u00efcode'
    second = "\tthe sixth page\u00a0"
    reader = LlmResumeOcr(
        client=_serving(
            [
                _response(_answer({"text": first, "confidence": 0.9})),
                _response(_answer({"text": second, "confidence": 0.9})),
            ],
            requests,
        )
    )

    assert reader.read(APPLICATION_PDF, _distinct_pages(6)) == f"{first}\n{second}"


def test_a_window_answering_null_contributes_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(
        client=_serving(
            [
                _response(_answer({"text": None})),
                _response(_answer({"text": "the middle window"})),
                _response(_answer({"text": None})),
            ],
            requests,
        )
    )

    assert reader.read(APPLICATION_PDF, _distinct_pages(12)) == "\nthe middle window\n"
    assert len(requests) == 3


def test_a_multi_window_scan_whose_windows_all_answer_null_is_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing legible in any window joins to whitespace: the route's `422`, not a retry."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(
        client=_serving(
            [_response(_answer({"text": None})), _response(_answer({"text": None}))], requests
        )
    )

    assert reader.read(APPLICATION_PDF, _distinct_pages(6)) == "\n"
    assert len(requests) == 2


def test_a_second_window_that_fails_mid_scan_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The window that failed raises the same typed error: the whole scan fails, not part of it."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(
        client=_serving(
            [
                _response(_answer({"text": "pages one to five"})),
                httpx2.ConnectError("no route to host"),
            ],
            requests,
        )
    )

    with pytest.raises(ResumeOcrError) as raised:
        reader.read(APPLICATION_PDF, _distinct_pages(6))

    assert str(raised.value) == "the model call failed"
    assert len(requests) == 2, "the first window was read, then the second failed at once"


def test_a_second_window_that_answers_uselessly_costs_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(
        client=_serving(
            [_response(_answer({"text": "pages one to five"})), _response("junk")], requests
        )
    )

    with pytest.raises(ResumeOcrError) as raised:
        reader.read(APPLICATION_PDF, _distinct_pages(6))

    assert str(raised.value) == "the model's answer was unusable after 3 attempts"
    assert len(requests) == 1 + SCORER_MAX_ATTEMPTS


def test_the_log_line_names_the_counts_and_the_media_type_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One line, written before the first request: counts and the media type, no page."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    pdf = _distinct_pages(6)
    caplog.set_level(logging.INFO, logger=resume_ocr_module.__name__)
    reader = LlmResumeOcr(
        client=_serving(
            [
                _response(_answer({"text": "pages one to five"})),
                _response(_answer({"text": "the sixth page"})),
            ],
            requests,
        )
    )

    reader.read(APPLICATION_PDF, pdf)

    (record,) = [
        record for record in caplog.records if record.name == resume_ocr_module.__name__
    ]
    assert record.levelno == logging.INFO
    assert record.args == (APPLICATION_PDF, len(pdf), 6, 2)
    line = record.getMessage()
    assert f"{APPLICATION_PDF}, {len(pdf)} bytes, 6 page images, 2 requests" in line
    first_page = base64.b64encode(resume_service.scan_images(APPLICATION_PDF, pdf)[0]).decode(
        "ascii"
    )
    assert first_page not in line
    assert "data:image" not in line
    assert THE_KEY not in line


# The prompt: one file beside this module, read on every scan.


def test_the_prompt_sits_beside_the_module_and_asks_for_the_one_key() -> None:
    assert OCR_PROMPT_PATH == MATCHING_ROOT / "ocr_prompt.txt"
    assert OCR_PROMPT_PATH.is_absolute()
    template = _prompt_text()

    assert '"text"' in template
    assert "null" in template
    assert "image" in template.lower()


def test_the_text_part_is_the_prompt_file_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)

    LlmResumeOcr(client=_serving([], requests)).read(IMAGE_PNG, _scan_png())

    assert _body_of(requests[0])["messages"][0]["content"][0] == {
        "type": "text",
        "text": _prompt_text(),
    }


def test_the_prompt_is_read_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Editing the file changes the next request: nothing is read at import."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(client=_serving([], requests))
    reader.read(IMAGE_PNG, _scan_png())

    edited = tmp_path / "ocr_prompt.txt"
    edited.write_text("Edited: transcribe the pages.", encoding="utf-8")
    monkeypatch.setattr(resume_ocr_module, "OCR_PROMPT_PATH", edited)
    reader.read(IMAGE_PNG, _scan_png())

    first, second = (
        _body_of(request)["messages"][0]["content"][0]["text"] for request in requests
    )
    assert first != second
    assert second == "Edited: transcribe the pages."


# The answer: one shape is read, and the rest of the shapes cost an attempt each.


def test_a_well_formed_answer_is_the_text_it_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    requests: list[httpx2.Request] = []
    reader = LlmResumeOcr(
        client=_serving([_response(_answer({"text": SCAN_TEXT}))], requests)
    )

    assert reader.read(IMAGE_PNG, _scan_png()) == SCAN_TEXT
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        pytest.param(None, "", id="null"),
        pytest.param("", "", id="empty"),
        pytest.param("   \n\t ", "   \n\t ", id="whitespace"),
    ],
)
def test_null_an_empty_and_a_whitespace_answer_are_answers_rather_than_errors(
    stated: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model saying nothing was legible is a `422` for the route to make, not a retry."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(
        client=_serving([_response(_answer({"text": stated}))], requests)
    )

    assert reader.read(IMAGE_PNG, _scan_png()) == expected
    assert len(requests) == 1


def test_extra_keys_are_ignored_and_the_text_is_returned_exactly_as_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not truncated, not unescaped, not re-wrapped: the words the model wrote."""
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    requests: list[httpx2.Request] = []
    text = 'Line one\n\n  indented "quotes" \u2014 \u00fcn\u00efcode\n' + ("padding " * 500)
    reader = LlmResumeOcr(
        client=_serving(
            [_response(_answer({"text": text, "confidence": 0.9, "pages": [1]}))], requests
        )
    )

    assert reader.read(IMAGE_PNG, _scan_png()) == text
    assert len(requests) == 1


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
    pytest.param(_completion("5"), id="content that is a JSON number"),
    pytest.param(_answer({"text": 5}), id="a text that is a number"),
    pytest.param(_answer({"text": True}), id="a text that is a boolean"),
    pytest.param(_answer({"text": ["a", "b"]}), id="a text that is a list"),
]


@pytest.mark.parametrize("answer", MALFORMED_ANSWERS)
def test_a_malformed_answer_costs_every_attempt_and_is_then_reported(
    answer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(client=_serving([_response(answer)], requests))

    with pytest.raises(ResumeOcrError) as raised:
        reader.read(IMAGE_PNG, _scan_png())

    assert len(requests) == SCORER_MAX_ATTEMPTS == 3
    assert str(raised.value) == "the model's answer was unusable after 3 attempts"


@pytest.mark.parametrize("malformed", [0, 1, 2], ids=["none", "one", "two"])
def test_one_malformed_answer_costs_one_attempt_and_the_good_one_ends_it(
    malformed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    answers = [_response("junk") for _ in range(malformed)] + [
        _response(_answer({"text": SCAN_TEXT}))
    ]
    reader = LlmResumeOcr(client=_serving(answers, requests))

    assert reader.read(IMAGE_PNG, _scan_png()) == SCAN_TEXT
    assert len(requests) == malformed + 1


def test_a_retry_is_a_fresh_request_with_the_same_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(client=_serving([_response("junk")], requests))

    with pytest.raises(ResumeOcrError):
        reader.read(IMAGE_PNG, _scan_png())

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
    """No `httpx2` exception escapes `read`: a model outage is a typed error, not a traceback."""
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(LLM_API_KEY, THE_KEY)
    reader = LlmResumeOcr(client=_serving([failure], requests))

    with pytest.raises(ResumeOcrError) as raised:
        reader.read(IMAGE_PNG, _scan_png())

    assert len(requests) == 1, "a transport failure does not retry"
    assert str(raised.value) == message


# No message carries the key, the scan or the provider's body.


def test_no_failure_message_carries_the_key_the_scan_or_the_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every failure shape, with sentinels in everything the provider sends back."""
    key = "key-sentinel-7f4c1d9ab38e"
    body_sentinel = "UPSTREAM-SENTINEL-TEXT-THAT-MUST-NOT-BE-ECHOED"
    monkeypatch.setenv(LLM_API_KEY, key)
    failures: list[httpx2.Response | Exception] = [
        httpx2.ConnectTimeout("slow"),
        httpx2.ConnectError("no route to host"),
        _response(f"<html>{body_sentinel}</html>", status=503),
        _response(_completion(body_sentinel)),
        _response(body_sentinel),
    ]

    messages: list[str] = []
    for failure in failures:
        reader = LlmResumeOcr(client=_serving([failure], []))
        with pytest.raises(ResumeOcrError) as raised:
            reader.read(IMAGE_PNG, _scan_png())
        messages.append(str(raised.value))

    assert len(messages) == len(failures)
    for leaked in (key, body_sentinel, SCAN_TEXT):
        assert [message for message in messages if leaked in message] == []
    assert messages == [
        "the model call did not answer in time",
        "the model call failed",
        "the model call failed (HTTP 503)",
        "the model's answer was unusable after 3 attempts",
        "the model's answer was unusable after 3 attempts",
    ]
