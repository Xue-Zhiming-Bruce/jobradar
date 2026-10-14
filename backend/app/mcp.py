"""The hosted MCP endpoint: the seven tools of `mcp/` served over MCP streamable HTTP.

`mcp/` serves those tools over stdio, in a process of its own (#17). This module
mounts the *same* tool source into this app's process, so one container serves
the API, the UI and the MCP endpoint on one port and one origin -- no second
service, no second exposed port and no CORS (#43). That is why `mcp` and
`jobradar_mcp` are importable here, deliberately reversing #17's dependency
isolation; the decision and its consequences are in
`_docs/decisions/2026-10-14-hosted-mcp.md`, and `mcp/` stays the single
implementation of the tools.

`MCP_AUTH_TOKEN` guards it, read per request the way `app.routes.internal` reads
its own shared secret: every request to the endpoint must carry `Authorization:
Bearer <token>`, and an unset or blank token refuses every request with `401`, so
a deployment that does not configure the token exposes nothing. The value is
compared with `hmac.compare_digest` and never echoed -- not in the refusal, a log
line, a response body, the browser or the repository -- and it is read in this
one module.

A session manager can be run once per instance and an app's lifespan may run more
than once in one process (the suite starts and stops `TestClient` many times), so
`HostedEndpoint` builds the server, the transport and the session manager when
its lifespan starts and drops them when it ends. `app.main` serves it at
`MCP_PATH`, with and without the trailing slash, and enters its lifespan;
`README.md` states the path and the variable for an operator.
"""

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from jobradar_mcp.__main__ import build_server
from jobradar_mcp.hosted import build_http_app

# The path the endpoint answers on, mounted in `app.main`, and the variable the
# guard reads, read here and in no other module. `import app.mcp` in the
# `tests/test_env_example.py` scan is where the name is checked against `.env.example`.
MCP_PATH = "/mcp"
# The variable's name, not a value: the token itself is read by `auth_token` at call time.
AUTH_TOKEN_VARIABLE = "MCP_AUTH_TOKEN"  # noqa: S105
AUTHORIZATION_HEADER = b"authorization"
BEARER_PREFIX = "Bearer "

# The one status a missing, blank and wrong token all answer, so the three are
# indistinguishable to a caller and a probe learns nothing from the difference.
REFUSAL_STATUS = 401
# The status a request gets before the lifespan has started the transport: the
# mount exists from import, the transport only from startup.
NOT_RUNNING_STATUS = 503


def auth_token() -> str:
    """`MCP_AUTH_TOKEN`, or an empty string when it is unset or blank.

    Read per request rather than at import, so an operator changes the token by
    setting the variable and an unset one refuses every request rather than
    leaving a value nobody set.
    """
    return os.environ.get(AUTH_TOKEN_VARIABLE, "").strip()


def _is_endpoint_path(path: str) -> bool:
    """Whether a request path belongs to the endpoint: `MCP_PATH` or something below it."""
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


def _at_transport_root(scope: Scope) -> Scope:
    """The scope as the transport sees it: the one endpoint at `/`, whatever path spelled it.

    The transport serves a single endpoint at `/`, so both `/mcp` and `/mcp/`
    have to reach it at the same path. A `Mount` would do that for the slashed
    form only; `app.main` registers both spellings as routes and this makes them
    identical below the router, so the documented path cannot mean two things.
    """
    return {**scope, "path": "/", "raw_path": b"/"}


def _offered_token(scope: Scope) -> str | None:
    """The token of a request's `Authorization: Bearer ...` header, or `None`.

    The scope's header names are lowercased by the server; the value is decoded as
    latin-1 so a header sent as arbitrary bytes cannot raise here.
    """
    for name, value in scope["headers"]:
        if name == AUTHORIZATION_HEADER:
            text = value.decode("latin-1")
            return text[len(BEARER_PREFIX) :] if text.startswith(BEARER_PREFIX) else None
    return None


class BearerAuthMiddleware:
    """Refuse every request to the endpoint that does not carry the configured bearer token.

    A pure ASGI middleware, not a `BaseHTTPMiddleware`, because the transport
    streams an SSE response and a middleware that buffers it would break the
    stream. The check runs before the transport, so a refused request performs no
    tool action: its whole answer is the `401`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _is_endpoint_path(scope["path"]) and not self._authorised(scope):
            await JSONResponse(
                {"detail": f"The MCP endpoint requires an Authorization: Bearer header"},
                status_code=REFUSAL_STATUS,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _authorised(scope: Scope) -> bool:
        """Whether the request carries the configured token, compared without an early exit."""
        configured = auth_token()
        offered = _offered_token(scope)
        return bool(configured) and offered is not None and hmac.compare_digest(
            offered.encode("utf-8"), configured.encode("utf-8")
        )


class HostedEndpoint:
    """The endpoint's mount: the MCP transport, started and stopped with the app's lifespan.

    A session manager can be run once per instance, so this builds a fresh server
    and transport each time its lifespan starts. The mount is this object, which
    exists from import; a request that reaches it before the lifespan started is
    answered `503` rather than crashed.
    """

    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the transport for as long as the app serves, dropping it when the app stops."""
        server = build_server()
        self._app = build_http_app(server)
        async with server.session_manager.run():
            try:
                yield
            finally:
                self._app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            await JSONResponse(
                {"detail": "The MCP endpoint is not running"}, status_code=NOT_RUNNING_STATUS
            )(scope, receive, send)
            return
        await self._app(_at_transport_root(scope), receive, send)


# The one instance `app.main` serves at `MCP_PATH`, with and without its trailing
# slash, and starts in its lifespan.
endpoint = HostedEndpoint()
