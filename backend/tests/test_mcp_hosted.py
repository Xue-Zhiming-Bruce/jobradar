"""The hosted MCP endpoint: one container, a bearer token, off by default (#43).

`app/mcp.py` mounts the seven tools of `mcp/` into the app's own process at
`/mcp`, guarded by `MCP_AUTH_TOKEN`. Every case here drives the real app object
in-process -- `TestClient` for the refusals, and the SDK's own streamable-HTTP
client over an in-process ASGI transport for the success path -- so nothing needs
a port, a socket or a deployment.

What is asserted: a request without a token, with a blank one or with a wrong one
is `401` and reaches no tool; an unset `MCP_AUTH_TOKEN` refuses every request with
the same `401` (the endpoint is off, AC4); the token is read per request, so
changing the variable changes the verdict without a restart; the right token lists
exactly the seven tools the stdio server lists, with the same input schemas (the
same implementation, AC2); and the token's value appears in no refusal body.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from app import mcp
from app.main import app
from jobradar_mcp.__main__ import build_server

BASE_URL = "http://testserver"
MCP_URL = f"{BASE_URL}{mcp.MCP_PATH}"

# A literal, local throwaway: it guards an app no case starts and has no power
# over anything. It is what the case sets `MCP_AUTH_TOKEN` to.
THROWAWAY_TOKEN = "test-throwaway-mcp-token"

# A JSON-RPC call that would run a tool if it ever reached the transport, so a
# refusal is proven to stop the request before any tool could act.
TOOL_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "trigger_run", "arguments": {}},
}

TOOL_NAMES = (
    "add_company",
    "list_watchlist",
    "list_matches",
    "trigger_run",
    "get_run",
    "remove_company",
    "set_match_state",
)


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    """`MCP_AUTH_TOKEN` set to the throwaway, the value an operator would configure."""
    monkeypatch.setenv(mcp.AUTH_TOKEN_VARIABLE, THROWAWAY_TOKEN)
    return THROWAWAY_TOKEN


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str) -> Iterator[TestClient]:
    """The app over a temporary SQLite file, with the endpoint's token set."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    with TestClient(app) as test_client:
        yield test_client


def _authorisation(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_a_request_without_a_token_is_refused_with_401(client: TestClient) -> None:
    response = client.post(f"{mcp.MCP_PATH}/", json=TOOL_CALL)

    assert response.status_code == mcp.REFUSAL_STATUS
    assert "jsonrpc" not in response.text, "the request reached the transport"


def test_a_blank_token_is_refused_with_401(client: TestClient) -> None:
    response = client.post(f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation(""))

    assert response.status_code == mcp.REFUSAL_STATUS
    assert "jsonrpc" not in response.text


def test_a_wrong_token_is_refused_with_401(client: TestClient) -> None:
    response = client.post(f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation("not-it"))

    assert response.status_code == mcp.REFUSAL_STATUS
    assert "jsonrpc" not in response.text


def test_the_refusal_never_echoes_the_configured_token(client: TestClient) -> None:
    response = client.post(f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation("not-it"))

    assert THROWAWAY_TOKEN not in response.text


def test_the_path_without_its_trailing_slash_is_guarded_too(client: TestClient) -> None:
    response = client.post(mcp.MCP_PATH, json=TOOL_CALL)

    assert response.status_code == mcp.REFUSAL_STATUS


def test_an_unset_token_turns_the_endpoint_off_with_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a deployment that configures no token exposes no working MCP surface."""
    monkeypatch.delenv(mcp.AUTH_TOKEN_VARIABLE)

    response = client.post(f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation(""))

    assert response.status_code == mcp.REFUSAL_STATUS
    assert mcp.auth_token() == ""
    assert "jsonrpc" not in response.text


def test_the_token_is_read_per_request_not_at_import(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: rotating the variable changes the verdict with no restart."""
    monkeypatch.setenv(mcp.AUTH_TOKEN_VARIABLE, "first-throwaway")
    first = client.post(
        f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation("first-throwaway")
    )
    assert first.status_code != mcp.REFUSAL_STATUS

    monkeypatch.setenv(mcp.AUTH_TOKEN_VARIABLE, "second-throwaway")
    old = client.post(
        f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation("first-throwaway")
    )
    new = client.post(
        f"{mcp.MCP_PATH}/", json=TOOL_CALL, headers=_authorisation("second-throwaway")
    )
    assert old.status_code == mcp.REFUSAL_STATUS
    assert new.status_code != mcp.REFUSAL_STATUS


async def _tools_over_http(token: str) -> list[Any]:
    """The tools the hosted transport advertises, through the SDK's own client."""
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=_authorisation(token)
        ) as http_client:
            async with streamable_http_client(MCP_URL, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return (await session.list_tools()).tools


def test_the_hosted_transport_serves_the_same_seven_tools_as_the_stdio_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """AC2: one implementation, two transports -- same names, same arguments, no eighth tool."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")

    hosted = asyncio.run(_tools_over_http(token))
    stdio: MCPServer = build_server()
    over_stdio = asyncio.run(stdio.list_tools())

    assert tuple(tool.name for tool in hosted) == TOOL_NAMES
    assert [(tool.name, tool.input_schema) for tool in hosted] == [
        (tool.name, tool.input_schema) for tool in over_stdio
    ]


def test_the_mount_answers_503_before_its_lifespan_starts(token: str) -> None:
    """The mount exists from import; the transport only from startup."""

    async def post() -> httpx2.Response:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=_authorisation(token)
        ) as http_client:
            return await http_client.post(f"{mcp.MCP_PATH}/", json=TOOL_CALL)

    assert asyncio.run(post()).status_code == mcp.NOT_RUNNING_STATUS
