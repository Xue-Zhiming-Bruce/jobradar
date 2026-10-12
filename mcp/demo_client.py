"""One stdio session, one tool call: the demonstration the README documents.

Run it with both variables set -- the app's URL and the throwaway secret the app
was started with -- and it starts `python -m jobradar_mcp` on the other end of a
pipe, initialises a session, calls `trigger_run` once and prints what came back:

    cd mcp && JOBRADAR_URL=http://127.0.0.1:8123 \
      JOBRADAR_TICK_SECRET=throwaway-local-secret uv run python demo_client.py

The two values are read through `jobradar_mcp.client`, the one module of the
server that reads them, and handed to the server it starts by name -- a client's
`command`/`args`/`env` entry, written out. This script reads no other variable
and holds no credential of its own.
"""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jobradar_mcp.client import TICK_SECRET_VARIABLE, URL_VARIABLE, base_url, tick_secret

SERVER_DIRECTORY = Path(__file__).resolve().parent
TOOL = "trigger_run"


async def main() -> None:
    """Call that one tool over stdio and print its answer."""
    parameters = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "jobradar_mcp"],
        cwd=SERVER_DIRECTORY,
        env={URL_VARIABLE: base_url(), TICK_SECRET_VARIABLE: tick_secret()},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(TOOL, {})
    print(f"{TOOL} -> " + "\n".join(block.text for block in result.content))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # a named failure rather than a traceback: print the message alone
        sys.exit(str(error))
