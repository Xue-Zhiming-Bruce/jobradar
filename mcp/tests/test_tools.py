"""The tool surface, and what each tool answers with the app's own body.

Every case drives the server's own dispatch -- `MCPServer.list_tools()` and
`MCPServer.call_tool(name, arguments)` -- over the fake app in `tests/support.py`,
so the SDK does the argument validation, the output conversion and the error
wrapping, and no case needs a server, a socket or the network. What is asserted
is the app's answer coming back unchanged: every body below is one the tool's
arguments could not have produced.
"""

import json
import re
from itertools import takewhile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from tests.support import TOOL_NAMES, TICK_SECRET, FakeApp, answered, call, listed_tools, rows

README = Path(__file__).resolve().parents[2] / "README.md"

# The seven tools, and the input schema the SDK builds from each function: the
# argument names, their JSON types and which of them are required. A renamed,
# dropped, added or accidentally-optional argument fails this dict.
EXPECTED_INPUT_SCHEMAS = {
    "add_company": {
        "properties": {
            "name": {"title": "Name", "type": "string"},
            "slug": {"title": "Slug", "type": "string"},
        },
        "required": ["name", "slug"],
        "title": "add_companyArguments",
        "type": "object",
    },
    "list_watchlist": {
        "properties": {},
        "title": "list_watchlistArguments",
        "type": "object",
    },
    "list_matches": {
        "properties": {},
        "title": "list_matchesArguments",
        "type": "object",
    },
    "trigger_run": {
        "properties": {},
        "title": "trigger_runArguments",
        "type": "object",
    },
    "get_run": {
        "properties": {"run_id": {"title": "Run Id", "type": "integer"}},
        "required": ["run_id"],
        "title": "get_runArguments",
        "type": "object",
    },
    "remove_company": {
        "properties": {"company_id": {"title": "Company Id", "type": "integer"}},
        "required": ["company_id"],
        "title": "remove_companyArguments",
        "type": "object",
    },
    # The verdict's own restriction: the schema the SDK builds from
    # `tools.Verdict` carries the two values and no third, so `new` is refused
    # before any function body runs.
    "set_match_state": {
        "properties": {
            "match_id": {"title": "Match Id", "type": "integer"},
            "state": {"enum": ["saved", "dismissed"], "title": "State", "type": "string"},
        },
        "required": ["match_id", "state"],
        "title": "set_match_stateArguments",
        "type": "object",
    },
}

# What the app answers, in its own field names and its own order. The company's
# id is 7 although no request carried one, and the match rows include a
# prefiltered one and a not-scored one, because one is the digest's business and
# the other is the screen's (#10, #12).
COMPANY = {"id": 7, "name": "Acme Corporation", "slug": "acme", "is_active": True}
COMPANIES = [
    {"id": 2, "name": "Acme", "slug": "acme", "is_active": True},
    {"id": 9, "name": "Globex", "slug": "globex", "is_active": False},
]
MATCHES = [
    {
        "id": 12,
        "company_name": "Acme",
        "title": "Backend Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "score": 88,
        "reason": "Python and PostgreSQL throughout",
        "gaps": ["Kubernetes"],
        "state": "new",
        "created_at": "2026-10-13T09:00:00Z",
    },
    {
        "id": 11,
        "company_name": "Globex",
        "title": "Staff Engineer",
        "url": "https://boards.greenhouse.io/globex/jobs/2",
        "score": 0,
        "reason": "Prefiltered out: the location is not one to watch",
        "gaps": [],
        "state": "dismissed",
        "created_at": "2026-10-12T09:00:00Z",
    },
    {
        "id": 10,
        "company_name": "Initech",
        "title": "Site Reliability Engineer",
        "url": "https://boards.greenhouse.io/initech/jobs/3",
        "score": 0,
        "reason": "Not scored: the run's cap was reached",
        "gaps": [],
        "state": "new",
        "created_at": "2026-10-11T09:00:00Z",
    },
]
STARTED_RUN = {
    "id": 3,
    "status": "running",
    "started_at": "2026-10-13T09:00:00Z",
    "finished_at": None,
    "error": None,
    "postings_seen": 0,
    "matches_created": 0,
}
ENDED_RUNS = [
    STARTED_RUN,
    {
        "id": 3,
        "status": "succeeded",
        "started_at": "2026-10-13T09:00:00Z",
        "finished_at": "2026-10-13T09:00:12Z",
        "error": None,
        "postings_seen": 41,
        "matches_created": 2,
    },
    {
        "id": 3,
        "status": "failed",
        "started_at": "2026-10-13T09:00:00Z",
        "finished_at": "2026-10-13T09:00:04Z",
        "error": "The board 'globex' refused to answer: HTTP 503",
        "postings_seen": 0,
        "matches_created": 0,
    },
]


def test_the_tool_list_is_exactly_these_seven_with_these_arguments(server: MCPServer) -> None:
    """Seven tools, these argument names, these JSON types, these required flags, and no eighth."""
    listed = listed_tools(server)

    assert tuple(EXPECTED_INPUT_SCHEMAS) == TOOL_NAMES
    assert {tool.name: tool.input_schema for tool in listed} == EXPECTED_INPUT_SCHEMAS
    # `structured_output=False`: the answer is the body, not a shape the SDK derives from it.
    assert [tool.output_schema for tool in listed] == [None] * 7


def test_the_readme_tool_table_names_the_same_seven_tools_and_arguments(server: MCPServer) -> None:
    """The table a reviewer reads is this tool list, one row per tool: name and arguments both."""
    lines = README.read_text().splitlines()
    table = list(takewhile(lambda line: line.startswith("|"), lines[lines.index("| Tool | Arguments | Returns |") :]))
    rows = [line.split("|") for line in table if line.startswith("| `")]

    assert [(cells[1].strip().strip("`"), tuple(re.findall(r"`(\w+): ", cells[2]))) for cells in rows] == [
        (tool.name, tuple(tool.input_schema.get("properties", {}))) for tool in listed_tools(server)
    ]


def test_add_company_sends_the_post_the_app_serves_and_answers_its_201_body(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The request is the app's own route, and the answer is the app's body rather than the arguments."""
    fake_app.answers("POST", "/watchlist", status=201, body=COMPANY)

    result = call(server, "add_company", {"name": "Whoever", "slug": "not-the-answer"})

    assert answered(result) == COMPANY
    assert fake_app.sent == [("POST", "/watchlist")]
    request = fake_app.requests[0]
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"name": "Whoever", "slug": "not-the-answer"}


def test_list_watchlist_answers_every_company_the_app_sent_in_order(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """Active or not, ascending id, unchanged -- and one read that writes nothing."""
    fake_app.answers("GET", "/watchlist", body=COMPANIES)

    result = call(server, "list_watchlist", {})

    assert rows(result) == COMPANIES
    assert result.is_error is False
    assert fake_app.sent == [("GET", "/watchlist")]


def test_an_empty_watchlist_is_an_empty_list(server: MCPServer, fake_app: FakeApp) -> None:
    """Nothing watched is an empty list, not an error and not a message."""
    fake_app.answers("GET", "/watchlist", body=[])

    result = call(server, "list_watchlist", {})

    assert rows(result) == []
    assert result.is_error is False


def test_list_matches_answers_every_row_including_the_prefiltered_and_the_unscored_one(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """No filter of its own: a score-0 row and a not-scored row come back exactly as stored."""
    fake_app.answers("GET", "/matches", body=MATCHES)

    result = call(server, "list_matches", {})

    assert rows(result) == MATCHES
    assert result.is_error is False
    assert fake_app.sent == [("GET", "/matches")]


def test_an_empty_matches_table_is_an_empty_list(server: MCPServer, fake_app: FakeApp) -> None:
    """Nothing scored is an empty list, not an error and not a message."""
    fake_app.answers("GET", "/matches", body=[])

    result = call(server, "list_matches", {})

    assert rows(result) == []
    assert result.is_error is False


def test_trigger_run_sends_one_post_with_the_secret_and_answers_the_202_body(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """One request, the app's header, the app's row -- and no polling for the run it started."""
    fake_app.answers("POST", "/internal/tick", status=202, body=STARTED_RUN)

    result = call(server, "trigger_run", {})

    assert answered(result) == STARTED_RUN
    assert fake_app.sent == [("POST", "/internal/tick")]
    request = fake_app.requests[0]
    assert request.headers["X-Tick-Secret"] == TICK_SECRET
    assert request.content == b""  # nothing to send: the app reads the header alone


def test_get_run_answers_that_row_however_it_ended(server: MCPServer, fake_app: FakeApp) -> None:
    """Running, succeeded or failed: the row's own seven fields, read back by the id that names it."""
    for row in ENDED_RUNS:
        fake_app.answers("GET", "/runs/3", body=row)

        result = call(server, "get_run", {"run_id": 3})

        assert answered(result) == row
    assert fake_app.sent == [("GET", "/runs/3")] * len(ENDED_RUNS)
