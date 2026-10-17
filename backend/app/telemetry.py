"""The trace, the JSON log line, and the two reads that make the digest journey visible.

One trace id holds the whole journey: the `POST /internal/tick` request, the
background run that request starts, and every log line and span either of them
writes. It is the caller's own `X-Request-ID` when that value may be adopted --
at most `REQUEST_ID_LENGTH` characters of `[A-Za-z0-9._-]` -- and a generated id
otherwise, so a caller that sends one bad header value cannot put arbitrary text
in the stored column or in the response header. `TelemetryMiddleware` adopts it,
binds it for the request's duration, writes the request's own span, and puts the
value back on the response whatever its status.

The hand-off to a run is explicit rather than inherited: `app.routes.internal`
reads `current_trace_id()` while the request is being served and passes it into
the work it hands the starter, because `_start_on_a_daemon_thread` starts a
thread whose context is empty and would otherwise see no id at all. `run_span()`
binds it in that thread and records the run's own span -- named `run`, with no
route, method or status code, because it is not an HTTP request.

Spans are stored in `telemetry_span` (`app.models`) by `store_span()`, the one
writer, and the table holds seven facts and no eighth: a request body, a query
string, an `Authorization` value, the `X-Tick-Secret`, the database URL, resume
text and a resume's file name have nowhere to go. `route` is the matched route's
*template* and never a path with an id, and a path no route matched is
`UNMATCHED_ROUTE` rather than the path itself. `record_span()` is the only caller
of the writer, and it never raises: telemetry that fails the request it observes
would be worse than no telemetry, so a database that is down or a table that is
missing is one warning line naming the database through `db.redacted_database_url()`
and a response that is exactly what it would have been.

`JsonFormatter` is the app's own log format, installed once from `app.main` so
`uvicorn`, the app and the suite all log that way: one JSON object per line, with
the line's trace id in a field of its own and `null` on a line written outside any
trace -- the startup reap, a startup failure. A record carrying an exception is
still one line: the traceback is a field, escaped into the JSON string, so the
log stream parses line by line.

The two reads are the backend half of #91's screen. `GET /telemetry/metrics`
answers one object per route over the last `METRICS_WINDOW`, and
`GET /telemetry/traces` answers the newest traces grouped by trace id. Those key
names are a contract: #91 renders this body and #92 and #93 quote these trace ids,
so a rename here breaks them. Both windows are constants here and neither is a
deployment variable or a request parameter.

`prune_spans()` is the only deleter of spans, called once per process start from
`app.main`'s lifespan beside `reap_stale_runs()`. `SPAN_RETENTION` is strictly
longer than `METRICS_WINDOW`, so every span a metric counted is still stored when
the metric is read.
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import db
from app.models import TelemetrySpan

logger = logging.getLogger(__name__)

router = APIRouter()

# The header a trace id arrives in and goes back out in, spelled once here and
# read by nothing else: neither the value nor the absence of the header is
# stored, and only a value that may be adopted reaches a column or a log line.
REQUEST_ID_HEADER = "X-Request-ID"

# What may be adopted: at most `REQUEST_ID_LENGTH` characters, all of them in this
# class. A header value is text a client chose, and it ends up in a database
# column and in a response header, so anything else -- empty, whitespace, longer,
# a control character, a newline -- is refused and a fresh id is generated in its
# place. Nothing else about the value is inspected and nothing is sanitised.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

# The bound above, as the number it is, so the rule is spelled once.
REQUEST_ID_LENGTH = 64

# The route a path no route matched is recorded under. A named constant rather
# than the path the caller asked for: `/resume/12` and `/resume/13` are one route
# and a path is caller-chosen text.
UNMATCHED_ROUTE = "<unmatched>"

# The two names a span carries: a served HTTP request, and the work a tick
# started. The run's span is the one with no route, method or status code.
HTTP_SPAN_NAME = "http"
RUN_SPAN_NAME = "run"

# What a request's span records when the route raised instead of answering. The
# middleware re-raises, so the answer is still the app's own `500`.
ERROR_STATUS_CODE = 500

# The window `GET /telemetry/metrics` counts over, and how long a span is kept.
# Both are constants of this module and neither is configurable: a window that can
# be set per deployment or per request is a window two readers cannot compare.
# Retention is strictly longer, so a span a metric counted is still stored.
METRICS_WINDOW = timedelta(hours=1)
SPAN_RETENTION = timedelta(days=7)

# The nearest-rank percentile the metrics read answers, as the fraction it is.
P95 = 0.95

# The most traces one read answers. This mirrors `MATCHES_MAX_LIMIT` (200) in
# `app.routes.matches` rather than importing it: this module would otherwise
# depend on a route module for an unrelated read's bound, and
# `tests/test_telemetry.py` holds the two to each other.
TRACES_MAX_LIMIT = 200

# The loggers uvicorn's own configuration gives handlers to. They do not
# propagate, so a formatter installed on the root logger alone would leave the
# access and error lines in uvicorn's plain-text format beside the app's JSON.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# The trace id of the request being served, or of the run being run, in whichever
# thread is logging. A `ContextVar` rather than a thread local: a request's log
# lines and spans run in the request's context, and a run's own work in its own
# thread, where `run_span` binds the value it was handed.
_trace_id: ContextVar[str | None] = ContextVar("jobradar_trace_id", default=None)


def current_trace_id() -> str | None:
    """The trace id this thread's work is under, or `None` when it is under none."""
    return _trace_id.get()


def new_trace_id() -> str:
    """A fresh trace id: 128 random bits, in characters the pattern above accepts."""
    return uuid.uuid4().hex


def adopted_trace_id(offered: str | None) -> str:
    """The caller's id when it may be adopted, and a generated one when it may not.

    `None`, an empty value, whitespace, anything longer than `REQUEST_ID_LENGTH`
    and any character outside the class is refused -- not trimmed, not escaped,
    not truncated -- and the caller gets an id this app chose. A caller that sends
    no header is the same case, and a refused value is never echoed back, so the
    response header holds only an id that may be stored.
    """
    if offered is not None and REQUEST_ID_PATTERN.fullmatch(offered):
        return offered
    return new_trace_id()


def route_template(scope: Scope) -> str:
    """The matched route's template, and `UNMATCHED_ROUTE` when nothing matched.

    The router it hands the request to records the route it matched on the scope,
    so this is read after the response and not from the path: `/matches/7` is
    recorded as `/matches/{id}`. A path no route matched -- and the built UI's
    mount at `/`, whose route has no path of its own -- is the sentinel, never the
    requested path.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


def elapsed_milliseconds(started: float) -> int:
    """Whole milliseconds since `started`, a `time.perf_counter()` reading."""
    return max(0, round((time.perf_counter() - started) * 1000))


@dataclass(frozen=True)
class Span:
    """One span, in the seven facts `telemetry_span` holds.

    `route`, `method` and `status_code` are `None` on the run's own span and set on
    an HTTP request's. There is no eighth field: a span cannot carry a body, a
    query string, a header value or a resume, because this is what is stored.
    """

    trace_id: str
    name: str
    route: str | None
    method: str | None
    status_code: int | None
    duration_ms: int
    started_at: datetime


def store_span(span: Span) -> None:
    """Write one span. The one writer of `telemetry_span`, and the only one.

    One `db.session_scope()`, so the connection is closed on every path -- a
    failure leaves no connection behind. It raises whatever the database raises:
    whether that may reach the response is `record_span`'s decision and not this
    function's, and a test replaces this name to drive that decision.
    """
    with db.session_scope() as session:
        session.add(TelemetrySpan(**asdict(span)))


def record_span(span: Span) -> None:
    """Store one span, and never raise: telemetry must not fail the request it observes.

    A database that is down, a table that is missing and anything else the writer
    raises is one warning line naming the database through `db.redacted_database_url()`
    -- never the URL itself, never a request body -- and the response is exactly
    what it would have been. The exception's own type is named and its message is
    not, because a driver's message can carry the connection string.
    """
    try:
        store_span(span)
    except Exception as error:
        logger.warning(
            "Could not store a %s span in the database at %s: %s",
            span.name,
            db.redacted_database_url(),
            type(error).__name__,
        )


@contextmanager
def run_span(trace_id: str | None) -> Iterator[None]:
    """Bind a run's trace id for this thread's work, and record the run's own span.

    The value is the one the tick's request handed over, so the run's span and its
    log lines carry the request's trace id and one trace holds both. A caller that
    hands nothing -- a run started from a test's own thread, say -- gets a fresh id
    rather than no span, because the table's `trace_id` is not nullable and a run
    without a span would be a hole in the story the reads tell. The span is written
    on the way out, whatever the work did, and `record_span` is what keeps a
    failing write from changing how the run ended.
    """
    shared = trace_id or new_trace_id()
    token = _trace_id.set(shared)
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    try:
        yield
    finally:
        _trace_id.reset(token)
        record_span(
            Span(
                trace_id=shared,
                name=RUN_SPAN_NAME,
                route=None,
                method=None,
                status_code=None,
                duration_ms=elapsed_milliseconds(started),
                started_at=started_at,
            )
        )


class TelemetryMiddleware:
    """One trace id and one span per HTTP request, whatever the response turns out to be.

    Added after the bearer guard, so it is the outermost of this app's middleware
    and sees every response the app produces: a `200`, a `202`, a `401` from the
    guard or the tick, a `404` from a path no route matched, a `422` a route
    refused and a `500` a route answered are all responses that carry the id back.
    A route that *raised* is the other path -- the plain `500` the server-error
    handler builds outside this middleware cannot carry a header -- so the span is
    recorded with `ERROR_STATUS_CODE` and the exception is re-raised rather than
    swallowed. Exactly one span is written per request: the error path returns
    through this function and does not reach the ordinary write below it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = adopted_trace_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        status_code: int | None = None

        async def send_with_the_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = trace_id
            await send(message)

        token = _trace_id.set(trace_id)
        try:
            try:
                await self.app(scope, receive, send_with_the_id)
            except Exception:
                record_span(self._span(trace_id, scope, ERROR_STATUS_CODE, started, started_at))
                raise

            # No response start means the request never got an answer -- a client
            # that went away -- and a span with no status code would be a request
            # the app did not serve. The path above is the other one, and the two
            # cannot both write a span for one request.
            if status_code is not None:
                record_span(self._span(trace_id, scope, status_code, started, started_at))
        finally:
            # Released after the span is recorded, so a write that failed is logged
            # while the request's trace id is still this thread's -- the failure and
            # the request it belongs to then read as one trace.
            _trace_id.reset(token)

    @staticmethod
    def _span(
        trace_id: str, scope: Scope, status_code: int, started: float, started_at: datetime
    ) -> Span:
        """This request's span, read from the scope after the response was built."""
        return Span(
            trace_id=trace_id,
            name=HTTP_SPAN_NAME,
            route=route_template(scope),
            method=scope.get("method"),
            status_code=status_code,
            duration_ms=elapsed_milliseconds(started),
            started_at=started_at,
        )


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, and the line's trace id in a field of its own.

    `trace_id` is `null` when the line was written outside any trace -- the
    startup reap, a startup failure -- rather than absent or raised over, so a
    reader can parse every line the same way. A record carrying an exception
    stays one line: the traceback is a field, and `json.dumps` escapes its
    newlines, so the log stream is one JSON object per line and nothing else.
    """

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": current_trace_id(),
        }
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, ensure_ascii=False) + "\n"


def configure_logging() -> None:
    """Install the app's JSON formatter once, at process start, from `app.main`.

    The root logger gets one `StreamHandler` carrying the formatter, and uvicorn's
    own handlers -- the ones its default configuration gives the loggers above
    before it imports this app -- are re-pointed at the same format, because they
    do not propagate and would otherwise print plain text beside the app's lines.
    Replacing the root handlers rather than adding to them is what makes a second
    call leave one handler instead of two.

    No level is raised anywhere: `app/db.py` holds `sqlalchemy.engine` at WARNING
    while the engine is built, and that floor is what keeps a statement's
    parameters and a result's rows out of the stream. The root level is the app's
    own lines, so a run's progress is visible; `sqlalchemy.engine`'s explicit
    level still wins over it.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in UVICORN_LOGGERS:
        for uvicorn_handler in logging.getLogger(name).handlers:
            uvicorn_handler.setFormatter(handler.formatter)


class RouteMetric(BaseModel):
    """One route's window, as `GET /telemetry/metrics` answers it.

    Four fields and no fifth: `count` is every span of that route in the window,
    `errors` those whose status code is 500 or more, and `p95_ms` the duration
    below which 95% of them fall. The route is the stored template -- `UNMATCHED_ROUTE`
    for a path no route matched -- and never a path a caller chose.
    """

    route: str
    count: int
    errors: int
    p95_ms: int


class SpanSummary(BaseModel):
    """One span as the trace read renders it: the row's own facts, minus the trace id.

    The id belongs to the trace this span is listed under, so it is not repeated
    here, and `route`, `method` and `status_code` are `None` on a run's span.
    """

    id: int
    name: str
    route: str | None
    method: str | None
    status_code: int | None
    duration_ms: int
    started_at: datetime


class TraceSummary(BaseModel):
    """One trace: the id its spans share, and those spans in the order they started.

    These two key names are what #91 renders and what #92 and #93 quote when they
    name a trace id, so neither may be renamed here.
    """

    trace_id: str
    spans: list[SpanSummary]


# The refusal this read answers with, named in the contract the way
# `app.routes.matches` names its own: `limit` outside its range is a `422` from
# the parameter's own validation, before the function below runs.
UNUSABLE_LIMIT_RESPONSE = {
    422: {"description": "The limit is not a whole number of traces this read answers"}
}


def nearest_rank_p95(durations: Sequence[int]) -> int:
    """The nearest-rank 95th percentile of these whole-millisecond durations.

    The rank is `ceil(P95 * n)` of the durations in ascending order, so one known
    set of durations answers one known number: ten durations answer the largest of
    them, and four answer the largest too. `durations` must not be empty -- the one
    caller groups spans by route, and a group exists because a span does.
    """
    ordered = sorted(durations)
    return ordered[ceil(P95 * len(ordered)) - 1]


@router.get(
    "/telemetry/metrics",
    response_model=list[RouteMetric],
    tags=["telemetry"],
    summary="Read one window of per-route metrics",
)
def read_metrics() -> list[RouteMetric]:
    """One object per route with a span in the last `METRICS_WINDOW`, ordered by route.

    The window is `datetime.now(UTC) - METRICS_WINDOW` and a span that started
    before it is not counted, so the same window read twice answers the same body
    and a span that ages out of it stops being counted. Every span of a route in
    the window is one of its `count`, the ones whose status code is >= 500 are its
    `errors`, and `p95_ms` is the nearest-rank 95th percentile of its durations.

    The run's own span has no route and is not a route's: it is stored, and read by
    `GET /telemetry/traces`, but it is not counted here. A path no route matched is
    counted under `UNMATCHED_ROUTE`, so a caller walking a 404 is visible too.
    """
    since = datetime.now(UTC) - METRICS_WINDOW
    with db.session_scope() as session:
        rows = session.execute(
            select(TelemetrySpan.route, TelemetrySpan.status_code, TelemetrySpan.duration_ms)
            .where(TelemetrySpan.started_at >= since, TelemetrySpan.route.is_not(None))
            .order_by(TelemetrySpan.route)
        ).all()

    durations: dict[str, list[int]] = {}
    errors: dict[str, int] = {}
    for route, status_code, duration_ms in rows:
        durations.setdefault(route, []).append(duration_ms)
        if status_code is not None and status_code >= ERROR_STATUS_CODE:
            errors[route] = errors.get(route, 0) + 1

    # The rows were read ordered by route, so the mapping is iterated in that
    # order and the body is ordered by route ascending without a second sort.
    return [
        RouteMetric(
            route=route,
            count=len(durations[route]),
            errors=errors.get(route, 0),
            p95_ms=nearest_rank_p95(durations[route]),
        )
        for route in durations
    ]


@router.get(
    "/telemetry/traces",
    response_model=list[TraceSummary],
    tags=["telemetry"],
    summary="Read the newest traces, each with its spans",
    responses=UNUSABLE_LIMIT_RESPONSE,
)
def read_traces(limit: int | None = Query(None, ge=1, le=TRACES_MAX_LIMIT)) -> list[TraceSummary]:
    """The newest traces grouped by trace id, newest first, and `[]` when there are none.

    `limit` counts traces and not spans: the newest N trace ids and every span each
    one holds. Absent means every stored trace, which `SPAN_RETENTION` bounds --
    the prune keeps the table finite, so this read is finite too -- and a limit of
    `0`, a negative one or more than `TRACES_MAX_LIMIT` is refused as `422` before
    this function runs.

    A trace is as new as its latest span, and its spans are ordered by the instant
    they started: a run's span starts after the request's own, so the two read in
    the order the journey happened. Two traces whose latest spans share an instant
    -- and two spans of one trace that do -- are ordered by id under it, so one
    window reads the same body twice.
    """
    with db.session_scope() as session:
        stored = select(TelemetrySpan).order_by(TelemetrySpan.started_at, TelemetrySpan.id)
        grouped: dict[str, list[TelemetrySpan]] = {}
        for span in session.scalars(stored):
            grouped.setdefault(span.trace_id, []).append(span)

        # Newest first by the trace's latest span, id under a shared instant, and
        # the limit counts traces: the newest N ids, with every span each one holds.
        newest_first = sorted(
            grouped.items(),
            key=lambda trace: (trace[1][-1].started_at, trace[1][-1].id),
            reverse=True,
        )
        if limit is not None:
            newest_first = newest_first[:limit]

        # Built here, while the session is open: the rows' own attributes are all
        # this needs, and an expired instance has none of them.
        return [
            TraceSummary(
                trace_id=trace_id,
                spans=[_span_summary(span) for span in trace_spans],
            )
            for trace_id, trace_spans in newest_first
        ]


def _span_summary(span: TelemetrySpan) -> SpanSummary:
    """One stored span as the trace read renders it, read while its session is open.

    The trace id is not here: every span in the answer is listed under the trace it
    belongs to, and a reader already has that value.
    """
    return SpanSummary(
        id=span.id,
        name=span.name,
        route=span.route,
        method=span.method,
        status_code=span.status_code,
        duration_ms=span.duration_ms,
        started_at=span.started_at,
    )


def prune_spans() -> int:
    """Delete every span older than `SPAN_RETENTION`, and answer how many there were.

    The only deleter of spans, called once per process start from `app.main`'s
    lifespan beside `reap_stale_runs()`, and never a route. One statement in one
    `db.session_scope()`, so nothing is held open: the retained window is the whole
    story the reads tell, and a span that ages out of it is a row nobody will ask
    for again. `SPAN_RETENTION` is longer than `METRICS_WINDOW`, so no span a
    metric counted is deleted while that metric still counts it.
    """
    cutoff = datetime.now(UTC) - SPAN_RETENTION
    with db.session_scope() as session:
        return session.execute(
            delete(TelemetrySpan).where(TelemetrySpan.started_at < cutoff)
        ).rowcount
