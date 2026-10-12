"""The two variables, the one module that reads them, and what a missing one does.

The needles below are spelled here the way `PAYLOAD_TERMS` is spelled in
`backend/tests/test_greenhouse_source.py`: the assertion is about which module of
the server names them, and this module is the one that names them in order to
check the others. No case here reads either variable except through a fixture.
"""

import json
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from jobradar_mcp.__main__ import build_server
from jobradar_mcp.client import TICK_SECRET_VARIABLE, URL_VARIABLE
from tests.support import (
    TICK_SECRET,
    FakeApp,
    answered,
    call,
    listed_tools,
    rows,
    tool_error,
)

MCP_ROOT = Path(__file__).resolve().parents[1]
SERVER_MODULES = tuple((MCP_ROOT / "jobradar_mcp").rglob("*.py"))
CLIENT_MODULE = MCP_ROOT / "jobradar_mcp" / "client.py"
LIVE_CASE = MCP_ROOT / "tests" / "test_live_app.py"
THIS_MODULE = Path(__file__).resolve()

# What the one reading module calls on `os`, taken from its own source rather
# than spelled here: the criterion's own grep for the two ways a process reads its
# settings is then answered by the module that reads and nothing else, and this
# test follows that module if the word it reads through ever changes.
READS_THE_SETTINGS = tuple(
    sorted(set(re.findall(r"\bos\.(\w+)", CLIENT_MODULE.read_text())))
)

# The variables of the app that no module of this server may name: not the model
# key, not the database URL, not the mail settings, not the upload limits, not
# the scoring or digest rules.
APP_VARIABLES = (
    "DATABASE_URL",
    "UPLOADS_DIR",
    "MAX_UPLOAD_BYTES",
    "STATIC_DIR",
    "LLM_API_KEY",
    "LLM_MODEL",
    "SCORER_MAX_POSTINGS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "DIGEST_FROM",
    "DIGEST_TO",
    "DIGEST_MIN_SCORE",
    "PREFILTER_TITLE_TERMS",
)

COMPANY = {"id": 2, "name": "Acme", "slug": "acme", "is_active": True}
STARTED_RUN = {
    "id": 3,
    "status": "running",
    "started_at": "2026-10-13T09:00:00Z",
    "finished_at": None,
    "error": None,
    "postings_seen": 0,
    "matches_created": 0,
}


def files_naming(needles: tuple[str, ...]) -> set[Path]:
    """Every file under `mcp/` that names one of these, this module included."""
    return {
        path
        for path in MCP_ROOT.rglob("*")
        if path.is_file()
        and ".venv" not in path.parts
        and path.suffix in (".py", ".toml", ".lock", ".md", ".cfg", ".json")
        and any(needle in path.read_text() for needle in needles)
    }


def test_the_two_variables_are_the_two_documented_names() -> None:
    """Exactly these two, and the app's own name for the secret's value is not this server's name."""
    assert (URL_VARIABLE, TICK_SECRET_VARIABLE) == ("JOBRADAR_URL", "JOBRADAR_TICK_SECRET")


def test_only_the_client_module_reads_the_process_settings() -> None:
    """The server's reads live in one module, the way `app.db` is the only reader of `DATABASE_URL`."""
    assert READS_THE_SETTINGS  # an empty tuple would make the assertion below vacuous
    assert files_naming(READS_THE_SETTINGS) == {CLIENT_MODULE}


def test_no_module_of_the_server_names_a_variable_of_the_app() -> None:
    """No key, no database URL, no mail setting, no upload limit and no digest rule."""
    assert files_naming(APP_VARIABLES).intersection(SERVER_MODULES) == set()


def test_only_the_case_that_starts_the_app_names_an_app_variable() -> None:
    """The one exception hands the *app* a temporary database URL; the server reads none of them."""
    assert files_naming(APP_VARIABLES) == {LIVE_CASE, THIS_MODULE}


def test_the_server_starts_and_advertises_its_tools_with_neither_variable_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the server reads no variable: a client can list the tools with neither one set."""
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    monkeypatch.delenv(TICK_SECRET_VARIABLE, raising=False)

    assert len(listed_tools(build_server())) == 5


def test_an_unset_secret_is_a_named_failure_that_sends_nothing(
    server: MCPServer, fake_app: FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`trigger_run` without `JOBRADAR_TICK_SECRET` names it, and no request leaves."""
    monkeypatch.delenv(TICK_SECRET_VARIABLE)

    error = tool_error(server, "trigger_run", {})

    assert TICK_SECRET_VARIABLE in str(error)
    assert fake_app.requests == []


def test_an_unset_url_is_a_named_failure(server: MCPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool that needs the app names `JOBRADAR_URL` when it is unset, rather than guessing a host."""
    monkeypatch.delenv(URL_VARIABLE)

    error = tool_error(server, "list_watchlist", {})

    assert URL_VARIABLE in str(error)


def test_the_reads_still_answer_with_only_the_secret_unset(
    server: MCPServer, fake_app: FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secret is the tick's alone: listing the watchlist and the matches does not need it."""
    monkeypatch.delenv(TICK_SECRET_VARIABLE)
    fake_app.answers("GET", "/watchlist", body=[COMPANY])
    fake_app.answers("GET", "/matches", body=[])

    assert rows(call(server, "list_watchlist", {})) == [COMPANY]
    assert rows(call(server, "list_matches", {})) == []


def test_the_secret_never_reaches_a_result_or_an_error(
    server: MCPServer, fake_app: FakeApp
) -> None:
    """The value goes into one header and nowhere else -- not into a refusal, not into a run row."""
    fake_app.answers(
        "POST",
        "/internal/tick",
        status=401,
        body={"detail": "The X-Tick-Secret header is missing, empty or wrong"},
    )

    error = tool_error(server, "trigger_run", {})

    assert TICK_SECRET not in str(error)
    # The header did carry it; the value is in the request and nowhere else.
    assert fake_app.requests[0].headers["X-Tick-Secret"] == TICK_SECRET

    fake_app.answers("POST", "/internal/tick", status=202, body=STARTED_RUN)
    assert TICK_SECRET not in json.dumps(answered(call(server, "trigger_run", {})))
