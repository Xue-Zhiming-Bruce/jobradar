"""Every failure a client can see, read through dispatch and read as a client.

Nothing here starts a server: each refusal is a body the fake transport serves, so
what these cases cover is the app's own statuses -- `404`, `409`, `422`, `502`,
`401`, `500` -- plus one nothing in the app answers, one body that is not JSON, an
app that is not listening, and the two writes' refusals: an id that finds no row,
an unexpected status, and the one verdict the tool's own schema keeps from the app
(`new`) with no request made at all. Every one of them is asserted to be an anticipated
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
NO_SUCH_COMPANY = {"detail": "There is no watched company with id 9"}
NO_SUCH_MATCH = {"detail": "There is no match with id 9"}
UNUSABLE_SOURCE = {
    "detail": [
        {
            "type": "literal_error",
            "loc": ["body", "source"],
            "msg": "Input should be 'greenhouse' or 'lever'",
        }
    ]
}
UNUSABLE_VERDICT = {
    "detail": [
        {
            "type": "literal_error",
            "loc": ["body", "state"],
            "msg": "Input should be 'saved' or 'dismissed'",
        }
    ]
}


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


def test_a_source_the_tool_schema_refuses_never_reaches_the_app(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The schema takes the two boards the contract names: a third is an error with no request."""
    error = tool_error(
        server, "add_company", {"name": "Acme", "slug": "acme", "source": "workday"}
    )

    assert "greenhouse" in str(error) and "lever" in str(error)
    assert fake_app.requests == []


def test_a_source_the_app_refuses_for_another_reason_is_the_apps_own_detail(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The app's own `422` on `body.source` reaches the caller as that field and message."""
    fake_app.answers("POST", "/watchlist", status=422, body=UNUSABLE_SOURCE)

    error = tool_error(
        server, "add_company", {"name": "Acme", "slug": "acme", "source": "lever"}
    )

    assert "body.source" in str(error)
    assert "Input should be 'greenhouse' or 'lever'" in str(error)
    assert "422" in str(error)
    assert fake_app.sent == [("POST", "/watchlist")]


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


def test_remove_company_names_the_id_that_found_no_row(server: MCPServer, fake_app: FakeApp) -> None:
    """The app's own `404` detail reaches the caller, and no confirmation is invented on a refusal."""
    fake_app.answers("DELETE", "/watchlist/9", status=404, body=NO_SUCH_COMPANY)

    error = tool_error(server, "remove_company", {"company_id": 9})

    assert NO_SUCH_COMPANY["detail"] in str(error)
    assert "9" in str(error)
    assert "removed" not in str(error)  # nothing the route did not say
    assert fake_app.sent == [("DELETE", "/watchlist/9")]


def test_remove_company_answers_an_unexpected_status_as_an_error_naming_it(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """A `500` is a tool error naming the status, never a raw `httpx2` exception and never a success."""
    fake_app.answers("DELETE", "/watchlist/9", status=500, text="<html>500 Internal Server Error</html>")

    error = tool_error(server, "remove_company", {"company_id": 9})

    assert "500" in str(error)
    assert fake_app.sent == [("DELETE", "/watchlist/9")]


def test_set_match_state_names_the_id_that_found_no_row(server: MCPServer, fake_app: FakeApp) -> None:
    """An id nobody holds is the app's `404`, and the id is what the caller reads."""
    fake_app.answers("PATCH", "/matches/9", status=404, body=NO_SUCH_MATCH)

    error = tool_error(server, "set_match_state", {"match_id": 9, "state": "saved"})

    assert NO_SUCH_MATCH["detail"] in str(error)
    assert fake_app.sent == [("PATCH", "/matches/9")]


def test_new_is_refused_by_the_tool_schema_and_never_reaches_the_app(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The schema takes the two verdicts and no third: `new` is an error with no request behind it."""
    error = tool_error(server, "set_match_state", {"match_id": 1, "state": "new"})

    assert "saved" in str(error) and "dismissed" in str(error)
    assert fake_app.requests == []


def test_a_verdict_the_app_refuses_for_another_reason_is_the_apps_own_detail(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The app's own `422` is a tool error carrying its `detail`, not a silent write."""
    fake_app.answers("PATCH", "/matches/9", status=422, body=UNUSABLE_VERDICT)

    error = tool_error(server, "set_match_state", {"match_id": 9, "state": "dismissed"})

    assert "state" in str(error)
    assert "Input should be 'saved' or 'dismissed'" in str(error)
    assert "422" in str(error)
    assert fake_app.sent == [("PATCH", "/matches/9")]


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
