"""The seven tools this server offers, and the server that carries them.

Each tool is one request to an endpoint the app already serves -- `POST
/watchlist`, `GET /watchlist`, `GET /matches`, `POST /internal/tick`,
`GET /runs/{id}`, `DELETE /watchlist/{id}`, `PATCH /matches/{id}` -- and each
returns the app's own answer, decoded JSON, field for field: nothing is computed,
filtered, renamed or defaulted here. `add_company` answers the `201` body,
`list_watchlist` answers the app's list whole and `list_matches` answers the rows
the filters it was given leave -- with none of them, the app's list whole (a
prefiltered row with score 0 and a not-scored row are in it exactly as stored,
because keeping either out is the digest's business and the screen's),
`trigger_run` answers the
`202` body and does not wait for the run or poll it, `get_run` answers that run's
row, and `set_match_state` answers the row its `PATCH` writes. `remove_company` is
the one tool whose route answers no body at all: its `204` becomes the small
confirmation this module builds, because there is nothing of the app's to hand
back and a caller needs something it can read.

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

from typing import Any, Literal

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from jobradar_mcp.client import URL_VARIABLE, base_url, tick_secret

WATCHLIST_PATH = "/watchlist"
MATCHES_PATH = "/matches"
MATCH_PATH = MATCHES_PATH + "/{match_id}"
TICK_PATH = "/internal/tick"
RUN_PATH = "/runs/{run_id}"

# The two values the route's own body takes, spelled as `MatchVerdict` spells
# them. `new` is not one of them: re-opening a match is #12's business, not this
# tool's, so a third value never reaches the app.
Verdict = Literal["saved", "dismissed"]

# The three values the read's own `?state=` takes, spelled as the route's
# `MatchState` spells them: the column's whole vocabulary, `new` included,
# because a filter is not a verdict and a caller may ask for the rows nobody has
# judged yet.
MatchState = Literal["new", "saved", "dismissed"]

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


def list_matches(
    client: httpx2.Client,
    min_score: int | None = None,
    state: MatchState | None = None,
    company_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every stored match row the filters leave, newest first: `GET /matches`.

    No filter means every row, which is what this tool has always answered; each
    argument is passed on as the route's own query parameter and none of them
    hides a row here. Only the arguments that were given are sent, so a call with
    none of them is a request with no query string at all. The route refuses a
    value outside a parameter's range or vocabulary, and that refusal reaches the
    caller as the app's own `detail`.
    """
    given = {
        "min_score": min_score,
        "state": state,
        "company_id": company_id,
        "limit": limit,
    }
    return _answer_of(
        client,
        "GET",
        MATCHES_PATH,
        expected=200,
        params={name: value for name, value in given.items() if value is not None},
    )


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


def remove_company(client: httpx2.Client, company_id: int) -> dict[str, Any]:
    """Stop watching one company: `DELETE /watchlist/{id}`, and this tool's own confirmation.

    The route answers `204` with no body, so this is the one tool that does not
    hand back the app's own JSON -- there is none, and a caller needs something it
    can read. A refusal is still the app's own `detail`.
    """
    _answer_of(client, "DELETE", f"{WATCHLIST_PATH}/{company_id}", expected=204, has_body=False)
    return {"id": company_id, "removed": True}


def set_match_state(client: httpx2.Client, match_id: int, state: Verdict) -> dict[str, Any]:
    """Record `saved` or `dismissed` on one match: `PATCH /matches/{id}`, and its `200` row.

    The body is `state` and nothing else, and the answer is the row as
    `GET /matches` renders it.
    """
    return _answer_of(
        client,
        "PATCH",
        MATCH_PATH.format(match_id=match_id),
        expected=200,
        json_body={"state": state},
    )


def _answer_of(
    client: httpx2.Client,
    method: str,
    path: str,
    *,
    expected: int,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    has_body: bool = True,
) -> Any:
    """The app's own answer to one request, decoded, or a `ToolError` saying why not.

    The body is returned exactly as the app sent it: this is where a status the
    tool does not expect, a body that is not JSON and an app that cannot be
    reached all become one `ToolError` for the client to read. A route that
    answers no body (`DELETE /watchlist/{id}`'s `204`) is asked for none:
    `has_body=False` answers `None`, where decoding an empty body would invent the
    one failure this module does not have. `params` is what a read's own query
    string is built from, and a caller passes only the parameters it was given:
    a `None` here would reach the app as an empty value rather than as an absent
    one.
    """
    url = f"{base_url()}{path}"
    try:
        answer = client.request(method, url, json=json_body, params=params, headers=headers)
    except httpx2.HTTPError as error:
        raise ToolError(f"{URL_VARIABLE} ({url}) could not be reached") from error
    if answer.status_code != expected:
        raise ToolError(_unexpected(method, path, answer))
    if not has_body:
        return None
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
