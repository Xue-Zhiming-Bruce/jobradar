"""The fake app the tool cases drive, and the readers of what a tool answered.

Nothing here starts a server or opens a socket: `FakeApp` is an
`httpx2.MockTransport` handler that records every request and answers it with the
body the case gave it -- the shape `backend/tests/test_greenhouse_source.py` uses
for the Greenhouse board -- and `FakeApp.client` is the factory each server under
test is built with. `call` drives a tool by name through the server's own
dispatch, so the SDK does the argument validation, the output conversion and the
error wrapping, and `as_a_client` drives the same call through the SDK's own
client, in process, to read what a real client sees.
"""

import asyncio
import json
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult

# A literal, local throwaway, and the base URL of an app no case starts: every
# request goes to the fake app, so neither value has any power over anything.
BASE_URL = "http://127.0.0.1:8123"
TICK_SECRET = "test-throwaway-secret"

# The seven tools, in the order the server registers them: `test_tools.py`
# asserts each one's input schema against this list, and `test_server_process.py`
# reads them over a real stdio session.
TOOL_NAMES = (
    "add_company",
    "list_watchlist",
    "list_matches",
    "trigger_run",
    "get_run",
    "remove_company",
    "set_match_state",
)


class FakeApp:
    """A stand-in for the running app: one recorded request list, one answer per call.

    A case sets the answer for a method and a path and nothing else: a tool that
    asks for a request the case did not set fails here rather than reading an
    answer nobody wrote.
    """

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self._answers: dict[tuple[str, str], httpx2.Response] = {}

    def answers(
        self, method: str, path: str, *, status: int = 200, body: Any = None, text: str | None = None
    ) -> None:
        """Set what the app answers for one request."""
        self._answers[(method, path)] = (
            httpx2.Response(status, text=text)
            if text is not None
            else httpx2.Response(status, json=body)
        )

    def send(self, request: httpx2.Request) -> httpx2.Response:
        """The transport handler: record the request, then answer it."""
        self.requests.append(request)
        answer = self._answers.get((request.method, request.url.path))
        assert answer is not None, (
            f"a tool asked for {request.method} {request.url.path}, which this case did not set"
        )
        return answer

    def client(self) -> httpx2.Client:
        """A client with the fake app on the other end and no socket anywhere."""
        return httpx2.Client(transport=httpx2.MockTransport(self.send))

    @property
    def sent(self) -> list[tuple[str, str]]:
        """Every request the tools made, as method and path, in order."""
        return [(request.method, request.url.path) for request in self.requests]


def call(server: MCPServer, name: str, arguments: dict[str, Any]) -> CallToolResult:
    """One tool call through the server's own dispatch, as a client would make it."""
    return asyncio.run(server.call_tool(name, arguments))


def as_a_client(server: MCPServer, name: str, arguments: dict[str, Any]) -> CallToolResult:
    """The same call the way a real MCP client sees it, over the SDK's in-process client.

    Nothing is started and nothing is connected: `mcp.Client` drives this
    `MCPServer` object in the calling process, which is how a failure can be read
    as the `isError` result a client receives rather than as a raised exception.
    """

    async def make_the_call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(make_the_call())


def listed_tools(server: MCPServer) -> list[Any]:
    """The tools the server advertises, through its own `list_tools()`."""
    return asyncio.run(server.list_tools())


def tool_error(server: MCPServer, name: str, arguments: dict[str, Any]) -> ToolError:
    """The failure one call raised: an anticipated `ToolError`, never a wrapped crash."""
    with pytest.raises(ToolError) as raised:
        call(server, name, arguments)
    assert not isinstance(raised.value, UnexpectedToolError), raised.value
    return raised.value


def answered(result: CallToolResult) -> Any:
    """The one JSON body a tool answered with, decoded from the content the SDK rendered."""
    blocks = result.content
    assert len(blocks) == 1, f"expected one content block, got {len(blocks)}"
    return json.loads(blocks[0].text)


def rows(result: CallToolResult) -> list[Any]:
    """The list a list tool answered with: the SDK renders one JSON block per row."""
    return [json.loads(block.text) for block in result.content]
