"""The entry point: build the five-tool server, and run it over stdio.

`cd mcp && uv run python -m jobradar_mcp` is the whole way to start it -- no port,
no host, no HTTP server of its own: the client on the other end of stdin and
stdout is the coding agent that registered it. Building the server reads no
variable and opens no socket, so a client can list the five tools with nothing set,
and `run(transport="stdio")` is the only transport it has.

Each registered tool is one line over the function of the same name in
`jobradar_mcp.tools`, which takes the `httpx2.Client` this wires in -- a fresh
one per call from `jobradar_mcp.client.new_client`, or the one a caller injects.
The tool's name, its arguments, its JSON types and their required flags are the
function's own signature, and `structured_output=False` keeps the returned body
the whole answer.
"""

from collections.abc import Callable
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer

from jobradar_mcp import tools
from jobradar_mcp.client import new_client


def build_server(get_client: Callable[[], httpx2.Client] = new_client) -> MCPServer:
    """The five tools, registered on a server that is not yet running.

    `get_client` is the one seam: the suite passes a factory returning a client
    over `httpx2.MockTransport`, and a running server uses the default. Building
    the server opens no socket and reads nothing at build time -- each call reads
    the two variables it needs, when it needs them.
    """
    server: MCPServer = MCPServer("jobradar")

    @server.tool(structured_output=False)
    def add_company(name: str, slug: str) -> dict[str, Any]:
        """Add a company to the watchlist by name and its Greenhouse board slug."""
        return tools.add_company(get_client(), name, slug)

    @server.tool(structured_output=False)
    def list_watchlist() -> list[dict[str, Any]]:
        """List every watched company, ascending id, active or not."""
        return tools.list_watchlist(get_client())

    @server.tool(structured_output=False)
    def list_matches() -> list[dict[str, Any]]:
        """List every stored match, newest first, with its score, reason and gaps."""
        return tools.list_matches(get_client())

    @server.tool(structured_output=False)
    def trigger_run() -> dict[str, Any]:
        """Start one digest run and answer its row while it is still running."""
        return tools.trigger_run(get_client())

    @server.tool(structured_output=False)
    def get_run(run_id: int) -> dict[str, Any]:
        """Read one digest run by its id, however it ended."""
        return tools.get_run(get_client(), run_id)

    return server


if __name__ == "__main__":
    build_server().run(transport="stdio")
