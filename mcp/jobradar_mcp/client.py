"""The MCP server's own two variables, and the one place an `httpx2.Client` is made.

This is the server's whole configuration: `JOBRADAR_URL`, the base URL of the
app the tools drive, and `JOBRADAR_TICK_SECRET`, the value the app compares
against `X-Tick-Secret` (its own variable is `TICK_SECRET`). Both are read here
and in no other module, the way `app.db` is the one module of the app that reads
the URL its database lives at, and both are read per call rather than at import,
the way `app.routes.internal.tick_secret()` reads its own: a client can start
this server, list its tools and read the watchlist with neither variable set.

A value that is unset or blank is a `ToolError` naming the variable -- never the
value, and never a crash: the client has to read it as an anticipated failure
rather than as a broken server. The tick secret goes into a request header and
nowhere else, so no tool result, error message or log line carries it.

No third variable is read by anything in this package: not the app's model key,
not the URL its own database lives at, not its mail settings, upload limits or
digest rules. This server holds no credential of its own beyond those two values.
"""

import os

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

URL_VARIABLE = "JOBRADAR_URL"
TICK_SECRET_VARIABLE = "JOBRADAR_TICK_SECRET"

# One timeout for a request to the app, read here and in no other module. A tick
# answers at once and the other four routes are reads, so a request that hangs is
# a tool error in ten seconds rather than a client that waits.
REQUEST_TIMEOUT_SECONDS = 10.0


def base_url() -> str:
    """`JOBRADAR_URL` without a trailing slash, or a `ToolError` naming that variable."""
    url = os.environ.get(URL_VARIABLE, "").strip()
    if not url:
        raise ToolError(
            f"{URL_VARIABLE} is not set: it must name the app's base URL, "
            "for example http://127.0.0.1:8123"
        )
    return url.rstrip("/")


def tick_secret() -> str:
    """`JOBRADAR_TICK_SECRET`, or a `ToolError` naming that variable.

    The value is returned to the one caller that puts it in the `X-Tick-Secret`
    header and is never printed: not in this message, not in a result.
    """
    secret = os.environ.get(TICK_SECRET_VARIABLE, "").strip()
    if not secret:
        raise ToolError(
            f"{TICK_SECRET_VARIABLE} is not set: it must hold the value the app "
            "reads from TICK_SECRET"
        )
    return secret


def new_client() -> httpx2.Client:
    """A fresh client for one tool call: no base URL, no default header, no shared state.

    The tool functions take a client as a parameter -- the shape
    `app.sources.greenhouse.fetch(slug, client=...)` already takes -- and this is
    the one that a running server hands them. It is built lazily, so importing
    this module opens no socket and reads no variable.

    `trust_env=False`, because the one address this server may reach is the base
    URL it was configured with: a proxy taken from the machine's own settings must
    not stand between the two, which is exactly what happens to the loopback URL
    this project documents its demo with on a machine that runs one.
    """
    return httpx2.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False)
