"""The same seven tools over MCP streamable HTTP, for a host app to mount.

`mcp/` serves the tools of `jobradar_mcp.tools` two ways: over **stdio** (`python
-m jobradar_mcp`, the coding agent's transport) and over the network, as MCP
**streamable HTTP**. This module is the network half and nothing else -- the
transport, not a second implementation: `build_http_app(server)` turns a server
from `jobradar_mcp.__main__.build_server` into the ASGI app a host mounts, so
both transports drive the same tool functions.

The app serves the tools at `/` relative to wherever the host mounts it, and the
host answers the path its clients are given and puts its own authentication in
front of the app (#43). The transport needs its session manager running, and a
mounted ASGI app's lifespan is not entered by Starlette, so the host runs
`server.session_manager.run()` in its own lifespan for as long as the app serves;
a session manager can be run once per instance, so a host that may start more
than once builds a fresh server each time.

DNS rebinding protection is off: it defaults to localhost-only hosts, which would
refuse the deployed host's own name, and the endpoint is guarded by the host's
bearer credential rather than by a localhost assumption.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette


def build_http_app(server: MCPServer) -> Starlette:
    """The MCP streamable-HTTP ASGI app for `server`, served at `/` under any mount."""
    return server.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
