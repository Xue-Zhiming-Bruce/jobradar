"""The seam every case drives: the fake app, the server over it, and the two variables.

`fake_app` is a `httpx2.MockTransport` handler and `server` is built with a
factory that returns a client on it, so the same seam the tools take in
production is the one the suite replaces. The autouse guard below refuses a real
connection outright, so a tool that built a client of its own fails here instead
of quietly passing; the one `live` case, which starts a real app on a real port,
is the only case that opts out.
"""

import socket

import pytest
from mcp.server.mcpserver import MCPServer

from jobradar_mcp.__main__ import build_server
from jobradar_mcp.client import TICK_SECRET_VARIABLE, URL_VARIABLE
from tests.support import BASE_URL, TICK_SECRET, FakeApp


@pytest.fixture(autouse=True)
def no_outbound_sockets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing but the one live case reaches the network."""
    if request.node.get_closest_marker("live") is not None:
        return

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this case tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def fake_app() -> FakeApp:
    """The app's answers, and the requests the tools sent to get them."""
    return FakeApp()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two variables set to the literal throwaways: no case needs them from its own shell."""
    monkeypatch.setenv(URL_VARIABLE, BASE_URL)
    monkeypatch.setenv(TICK_SECRET_VARIABLE, TICK_SECRET)


@pytest.fixture
def server(fake_app: FakeApp, settings: None) -> MCPServer:
    """The server under test, over the fake app: the five tools and their real dispatch."""
    return build_server(fake_app.client)
