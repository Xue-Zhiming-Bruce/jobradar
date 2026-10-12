"""Every failure a client can see, read through dispatch and read as a client.

Nothing here starts a server: each refusal is a body the fake transport serves, so
what these cases cover is the app's own statuses -- `404`, `409`, `422`, `502`,
`401` -- plus one nothing in the app answers, one body that is not JSON, and an
app that is not listening. Every one of them is asserted to be an anticipated
`ToolError` -- `isError` on the wire -- rather than the `UnexpectedToolError` the
SDK wraps an unhandled exception in.
"""

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer

from jobradar_mcp.__main__ import build_server
from jobradar_mcp.client import URL_VARIABLE
from tests.support import FakeApp, as_a_client, tool_error

BOARD_NOT_FOUND = {"detail": "The board 'nope' does not exist"}
ALREADY_WATCHED = {"detail": "The slug 'acme' is already watched"}
UNUSABLE_BODY = {
    "detail": [
        {
            "type": "value_error",
            "loc": ["body", "name"],
            "msg": "Value error, must not be blank",
        }
    ]
}
BOARD_UNREADABLE = {"detail": "The board 'acme' could not be read"}
WRONG_SECRET = {"detail": "The X-Tick-Secret header is missing, empty or wrong"}
RUN_IN_PROGRESS = {"detail": "A run is already in progress in this process"}
NO_SUCH_RUN = {"detail": "There is no run with id 9"}


@pytest.mark.parametrize(
    ("status", "body", "named"),
    [
        pytest.param(404, BOARD_NOT_FOUND, BOARD_NOT_FOUND["detail"], id="no board goes by that slug"),
        pytest.param(409, ALREADY_WATCHED, ALREADY_WATCHED["detail"], id="the slug is already watched"),
        pytest.param(502, BOARD_UNREADABLE, BOARD_UNREADABLE["detail"], id="the board could not be read"),
    ],
)
def test_add_company_answers_the_apps_refusal_as_a_tool_error(
    server: MCPServer, fake_app: FakeApp, status: int, body: dict, named: str
) -> None:
    """The app's own `detail` reaches the caller, and a refusal is never a success."""
    fake_app.answers("POST", "/watchlist", status=status, body=body)

    error = tool_error(server, "add_company", {"name": "Acme", "slug": "acme"})

    assert named in str(error)
    assert str(status) in str(error)
    assert fake_app.sent == [("POST", "/watchlist")]  # asked once, and no row invented


def test_a_blank_argument_is_the_apps_422_naming_the_field(server: MCPServer, fake_app: FakeApp) -> None:
    """The field the app refused is what the caller has to read, not the whole structure."""
    fake_app.answers("POST", "/watchlist", status=422, body=UNUSABLE_BODY)

    error = tool_error(server, "add_company", {"name": "   ", "slug": "acme"})

    assert "name" in str(error)
    assert "must not be blank" in str(error)
    assert "slug" not in str(error)


def test_trigger_run_answers_a_refusal_without_reading_as_a_started_run(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """A wrong secret is `401` and a run already in flight is `409`: an error, and no run row read."""
    for status, body in ((401, WRONG_SECRET), (409, RUN_IN_PROGRESS)):
        fake_app.answers("POST", "/internal/tick", status=status, body=body)

        error = tool_error(server, "trigger_run", {})

        assert body["detail"] in str(error)
    assert fake_app.sent == [("POST", "/internal/tick")] * 2  # no polling behind either refusal


def test_get_run_names_the_id_that_found_no_row(server: MCPServer, fake_app: FakeApp) -> None:
    """An id nobody holds is the app's `404`, and the id is what the caller reads."""
    fake_app.answers("GET", "/runs/9", status=404, body=NO_SUCH_RUN)

    error = tool_error(server, "get_run", {"run_id": 9})

    assert NO_SUCH_RUN["detail"] in str(error)
    assert fake_app.sent == [("GET", "/runs/9")]


def test_an_unexpected_status_is_an_error_naming_the_status(server: MCPServer, fake_app: FakeApp) -> None:
    """A status no tool expects is still a tool error, and the status is named rather than guessed at."""
    fake_app.answers("GET", "/matches", status=503, text="<html>503 Service Unavailable</html>")

    error = tool_error(server, "list_matches", {})

    assert "503" in str(error)


def test_a_body_that_is_not_json_is_an_error_naming_the_status(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """A `2xx` holding something other than JSON is a named error, never a decode traceback."""
    fake_app.answers("GET", "/watchlist", text="<html>this is not the API</html>")

    error = tool_error(server, "list_watchlist", {})

    assert "200" in str(error)
    assert "JSON" in str(error)


def test_an_app_that_is_not_listening_is_an_error_naming_the_url_variable(settings: None) -> None:
    """A refused connection is not a crash: it names the variable a caller has to set."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    server = build_server(lambda: httpx2.Client(transport=httpx2.MockTransport(refuse)))

    error = tool_error(server, "list_watchlist", {})

    assert URL_VARIABLE in str(error)


def test_a_client_reads_a_failure_as_an_error_result_and_never_as_a_crash(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The wire's own verdict: `isError`, carrying the message, with nothing raised through the client."""
    fake_app.answers("GET", "/matches", status=500, body={"detail": "The run could not be recorded"})

    result = as_a_client(server, "list_matches", {})

    assert result.is_error is True
    assert "500" in result.content[0].text
    assert "The run could not be recorded" in result.content[0].text
