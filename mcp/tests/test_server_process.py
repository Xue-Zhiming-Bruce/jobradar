"""The documented command, started for real over a pipe, with neither variable set.

No app, no socket and no case data: `python -m jobradar_mcp` is started as a
subprocess with only the variables a shell gives every child (nothing from
`JOBRADAR_*`), spoken to over stdio the way the README's demo client speaks to it,
and asked for its tool list and one tool call. That it answers at all is the claim
that building it reads no variable; that the call then names `JOBRADAR_URL` is the
claim that nothing was inherited behind this test's back.
"""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jobradar_mcp.client import URL_VARIABLE
from tests.support import TOOL_NAMES


def test_the_documented_command_answers_over_stdio_with_neither_variable_set() -> None:
    """Five tools, and a missing `JOBRADAR_URL` named as a tool error rather than inherited."""
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "jobradar_mcp"], env={})

    async def speak() -> tuple[list[str], bool, str]:
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            failed = await session.call_tool("list_watchlist", {})
            return [tool.name for tool in listed.tools], failed.is_error, failed.content[0].text

    names, is_error, message = asyncio.run(speak())

    assert names == list(TOOL_NAMES)
    assert is_error is True
    assert URL_VARIABLE in message
