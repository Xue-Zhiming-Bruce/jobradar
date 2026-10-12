"""The five tools this server offers, and the server that carries them.

Each tool is one request to an endpoint the app already serves -- `POST
/watchlist`, `GET /watchlist`, `GET /matches`, `POST /internal/tick`,
`GET /runs/{id}` -- and each returns the app's own answer, decoded JSON, field
for field: nothing is computed, filtered, renamed or defaulted here. `add_company`
answers the `201` body, `list_watchlist` and `list_matches` answer the app's list
whole (a prefiltered row with score 0 and a not-scored row are in it exactly as
stored, because keeping either out is the digest's business and the screen's),
`trigger_run` answers the `202` body and does not wait for the run or poll it,
and `get_run` answers that run's row.

The functions take an `httpx2.Client` as their first parameter and are plain
`def`s, so the suite injects `httpx2.Client(transport=httpx2.MockTransport(...))`
and the SDK runs each call on a worker thread. `jobradar_mcp.__main__`'s
`build_server()` is what registers one tool per function on an `MCPServer`,
wiring in a client per call from `jobradar_mcp.client.new_client` unless a caller
injects its own factory.

Every way this can fail is a `ToolError`, never a raw `httpx2` exception and
never a traceback: the app's own `detail` reaches the caller for the statuses it
returns, an unexpected status is an error naming that status, an answer that is
not JSON is an error naming the status rather than a decode traceback, and an app
that is not running is an error naming `JOBRADAR_URL`.
"""

from typing import Any

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from jobradar_mcp.client import URL_VARIABLE, base_url, tick_secret

WATCHLIST_PATH = "/watchlist"
MATCHES_PATH = "/matches"
TICK_PATH = "/internal/tick"
RUN_PATH = "/runs/{run_id}"

# The header the app checks, spelled as `app.routes.internal.TICK_SECRET_HEADER`
# spells it. The app reads the value itself from `TICK_SECRET`.
TICK_SECRET_HEADER = "X-Tick-Secret"


def add_company(client: httpx2.Client, name: str, slug: str) -> dict[str, Any]:
    """Add one company to the watchlist: `POST /watchlist`, and its `201` body."""
    return _answer_of(
        client,
        "POST",
        WATCHLIST_PATH,
        expected=201,
        json_body={"name": name, "slug": slug},
    )


def list_watchlist(client: httpx2.Client) -> list[dict[str, Any]]:
    """Every watched company, ascending id, active or not: `GET /watchlist`."""
    return _answer_of(client, "GET", WATCHLIST_PATH, expected=200)


def list_matches(client: httpx2.Client) -> list[dict[str, Any]]:
    """Every stored match row, newest first, unfiltered: `GET /matches`."""
    return _answer_of(client, "GET", MATCHES_PATH, expected=200)


def trigger_run(client: httpx2.Client) -> dict[str, Any]:
    """Start one digest run and answer its `202` row: one request, no waiting, no polling.

    The secret is read before the request, so an unset `JOBRADAR_TICK_SECRET`
    sends nothing at all, and it is put in the header and nowhere else.
    """
    headers = {TICK_SECRET_HEADER: tick_secret()}
    return _answer_of(client, "POST", TICK_PATH, expected=202, headers=headers)


def get_run(client: httpx2.Client, run_id: int) -> dict[str, Any]:
    """One run's row, in whichever status it is in: `GET /runs/{id}`."""
    return _answer_of(client, "GET", RUN_PATH.format(run_id=run_id), expected=200)


def _answer_of(
    client: httpx2.Client,
    method: str,
    path: str,
    *,
    expected: int,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """The app's own answer to one request, decoded, or a `ToolError` saying why not.

    The body is returned exactly as the app sent it: this is where a status the
    tool does not expect, a body that is not JSON and an app that cannot be
    reached all become one `ToolError` for the client to read.
    """
    url = f"{base_url()}{path}"
    try:
        answer = client.request(method, url, json=json_body, headers=headers)
    except httpx2.HTTPError as error:
        raise ToolError(f"{URL_VARIABLE} ({url}) could not be reached") from error
    if answer.status_code != expected:
        raise ToolError(_unexpected(method, path, answer))
    try:
        return answer.json()
    except ValueError as error:
        raise ToolError(
            f"{URL_VARIABLE} answered HTTP {answer.status_code} to {method} {path} "
            "with a body that is not JSON"
        ) from error


def _unexpected(method: str, path: str, answer: httpx2.Response) -> str:
    """One status the tool does not expect, named with the app's own detail when it sent one."""
    detail = _detail(answer)
    named = f"{URL_VARIABLE} answered HTTP {answer.status_code} to {method} {path}"
    return f"{named}: {detail}" if detail else named


def _detail(answer: httpx2.Response) -> str:
    """The `detail` of a refusal the app renders, as text, or an empty string.

    A route raises `HTTPException`, so `detail` is a string; a path or a body the
    app's own validation refuses carries a list of errors instead, and the field
    each one names is what a caller has to read -- so the field names are rendered
    rather than the whole structure. Anything else, a body with no JSON in it
    included, renders nothing: the status is already in the message.
    """
    try:
        payload = answer.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
            for error in detail
            if isinstance(error, dict)
        )
    return ""
