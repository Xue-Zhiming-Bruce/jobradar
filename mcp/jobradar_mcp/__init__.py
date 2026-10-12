"""JobRadar's MCP server: five bounded tools that drive the app over HTTP.

The tools are in `jobradar_mcp.tools`, the two variables this server reads and
the client it makes are in `jobradar_mcp.client`, and `python -m jobradar_mcp`
is `jobradar_mcp.__main__`. Importing this package builds no client, opens no
socket and reads no variable.
"""
