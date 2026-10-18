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

The second sink is `#101`'s: with `OTEL_EXPORTER_OTLP_ENDPOINT` set to an OTLP/HTTP
endpoint, `configure_export()` -- called once at process start from `app.main`
beside `configure_logging()` -- builds the one `OtlpExporter`, and `record_span()`
hands it the same span it stored. The export rides beside the database write and
behind the response the request already got, and `OtlpExporter.export()` never
raises, so a collector that is unreachable changes neither. Unset or empty builds
no exporter at all, which is exactly the deployment `#90` shipped. What leaves the
process is one OTLP `resourceSpans` document holding the same facts the row holds
and no eighth: the duration is the row's own whole milliseconds, and a body, a
query string, a header value, a resume text and a file name have no attribute to
reach.

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

`GET /telemetry/metrics/buckets` is the same window cut into `METRICS_BUCKET`s: one
row per route and clock minute, so a chart reads a series with a time on every point
instead of one row per route (#105). `store_span` writes the span and its bucket in
one transaction, and the bucket's count, errors and p95 are recounted from the
minute's stored spans rather than accumulated, so a bucket is the metrics read's own
numbers for that route and minute and a span write that failed leaves no bucket
behind. `bucket_start` is the minute the bucket begins, in UTC. A minute with no
span of that route answers no row, because a bucket exists because a span does.

`prune_spans()` is the only deleter of spans, called once per process start from
`app.main`'s lifespan beside `reap_stale_runs()`. It deletes a span's bucket with the
span: `SPAN_RETENTION` is strictly longer than `METRICS_WINDOW`, so every span a
metric counted and every bucket that counted it are still stored when the metric is
read."""

import json
import logging
import os
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

import httpx2
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import db
from app.models import MetricBucket, TelemetrySpan

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

# The window `GET /telemetry/metrics` counts over, how long a span is kept, and the
# bucket that window is stored in. All three are constants of this module and none is
# configurable: a window that can be set per deployment or per request is a window two
# readers cannot compare. Retention is strictly longer, so a span a metric counted is
# still stored.
METRICS_WINDOW = timedelta(hours=1)
SPAN_RETENTION = timedelta(days=7)
METRICS_BUCKET = timedelta(minutes=1)

# The one variable the OTLP export reads, and the path an OTLP/HTTP traces request
# is posted to under it. There is no default endpoint: unset or empty builds no
# exporter, so a deployment that does not configure one makes no outbound call
# (`#101` AC1).
OTLP_ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_SPANS_PATH = "/v1/traces"

# The `service.name` and scope name a collector groups these spans by, and how long
# one export may take before it is abandoned. The timeout is short because the
# export rides beside the request and never holds it for a collector.
OTLP_SERVICE_NAME = "jobradar"
OTLP_SCOPE_NAME = "jobradar.telemetry"
OTLP_TIMEOUT_SECONDS = 5.0

# Nanoseconds in one second and in one millisecond, so a duration -- whole
# milliseconds in the row -- is the instant arithmetic OTLP carries.
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MILLISECOND = 1_000_000

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


def bucket_start_of(started_at: datetime) -> datetime:
    """The minute-aligned start of the `METRICS_BUCKET` this instant falls in, in UTC.

    `METRICS_BUCKET` is one clock minute, so dropping the sub-minute fields is the
    bucket: there is no part of a minute a bucket could begin inside. The instant
    keeps its own UTC offset, and every writer stores one from a stored span.
    """
    return started_at.replace(second=0, microsecond=0)


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
    """Write one span and, for an HTTP span, count it into its route's minute.

    One `db.session_scope()`, so the connection is closed on every path -- a
    failure leaves no connection behind -- and the span and its bucket are one
    transaction: a write that fails leaves neither the row nor a bucket counting
    it. It raises whatever the database raises: whether that may reach the response
    is `record_span`'s decision and not this function's, and a test replaces this
    name to drive that decision.

    A span with no route is the run's own and has no bucket: a bucket is a route's
    minute, and `metric_bucket.route` is not nullable.
    """
    with db.session_scope() as session:
        session.add(TelemetrySpan(**asdict(span)))
        if span.route is not None:
            update_bucket(session, span)


def update_bucket(session: Session, span: Span) -> None:
    """Count one HTTP span into its route's minute, in the caller's transaction.

    The bucket is recounted from the stored spans of that route and minute -- the
    span just added is one of them, because the session flushes before this reads
    -- so its `count`, `errors` and `p95_ms` are the numbers `GET /telemetry/metrics`
    answers for that route and minute, by the nearest-rank rule rather than a running
    estimate that could drift. `route` and `bucket_start` are the stored row's key,
    so this finds the one row or adds it; the count, the error count and the
    percentile are written from the spans and never from the span's own fields alone.

    shortcut: the recount reads that minute's rows once per span, so one busy minute
    costs its own length per request; store a duration distribution instead if a
    minute ever holds thousands of spans.
    """
    start = bucket_start_of(span.started_at)
    minute = session.execute(
        select(TelemetrySpan.status_code, TelemetrySpan.duration_ms).where(
            TelemetrySpan.route == span.route,
            TelemetrySpan.started_at >= start,
            TelemetrySpan.started_at < start + METRICS_BUCKET,
        )
    ).all()
    bucket = session.scalars(
        select(MetricBucket).where(
            MetricBucket.route == span.route, MetricBucket.bucket_start == start
        )
    ).one_or_none()
    if bucket is None:
        bucket = MetricBucket(route=span.route, bucket_start=start, count=0, errors=0, p95_ms=0)
        session.add(bucket)
    bucket.count = len(minute)
    bucket.errors = sum(1 for status_code, _ in minute if is_an_error(status_code))
    bucket.p95_ms = nearest_rank_p95([duration_ms for _, duration_ms in minute])


def record_span(span: Span) -> None:
    """Store one span, export it beside the write, and never raise.

    A database that is down, a table that is missing and anything else the writer
    raises is one warning line naming the database through `db.redacted_database_url()`
    -- never the URL itself, never a request body -- and the response is exactly
    what it would have been. The exception's own type is named and its message is
    not, because a driver's message can carry the connection string.

    The export (`#101`) is attempted whatever the write did, so an endpoint that is
    configured still sees the span when the table write failed; `export_span` is a
    no-op with no exporter and never raises with one, so neither sink can reach the
    response the request already got.
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
    export_span(span)


# The one exporter this process built, or `None` when no endpoint is configured.
# `configure_export` sets it once at process start, and `export_span` reads it.
_exporter: "OtlpExporter | None" = None


def otlp_endpoint() -> str:
    """`OTEL_EXPORTER_OTLP_ENDPOINT`, or "" when it is unset or empty."""
    return os.environ.get(OTLP_ENDPOINT_VARIABLE, "").strip()


def otlp_payload(span: Span) -> dict[str, object]:
    """One span as an OTLP/HTTP JSON body: `resourceSpans` -> `scopeSpans` -> one span.

    The facts the row holds and nothing else. The name is the span's own, and the
    route template, the method and the status code are the three standard
    attributes OTLP has for an HTTP span -- written only when the row sets them, so
    the run's own span carries none. A span's duration is carried the way OTLP
    carries one, as the end instant minus the start, and the end is the row's start
    plus its whole milliseconds, so the two differ by exactly the number stored.

    Nothing here is read from a request: no body, no query string, no header value,
    no resume text, no file name and no credential has a field or an attribute to
    reach, because the `Span` itself holds none of them and this copies its fields
    and no other value. The trace id goes over verbatim -- see the note beside it.
    """
    attributes: list[dict[str, object]] = []
    if span.route is not None:
        attributes.append({"key": "http.route", "value": {"stringValue": span.route}})
    if span.method is not None:
        attributes.append({"key": "http.request.method", "value": {"stringValue": span.method}})
    if span.status_code is not None:
        attributes.append(
            {"key": "http.response.status_code", "value": {"intValue": str(span.status_code)}}
        )

    started_nanoseconds = int(span.started_at.timestamp() * NANOSECONDS_PER_SECOND)
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": OTLP_SERVICE_NAME}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": OTLP_SCOPE_NAME},
                        "spans": [
                            {
                                # The row's trace id, verbatim: a generated one is
                                # 32 hex (16 bytes, what OTLP wants), an adopted
                                # X-Request-ID need not be and goes over as it is.
                                # shortcut: a non-hex adopted id may be refused by a
                                # strict collector, upgrade if it must round-trip.
                                "traceId": span.trace_id,
                                # 8 bytes of span id, which is what OTLP carries.
                                "spanId": uuid.uuid4().hex[:16],
                                "name": span.name,
                                "startTimeUnixNano": str(started_nanoseconds),
                                "endTimeUnixNano": str(
                                    started_nanoseconds
                                    + span.duration_ms * NANOSECONDS_PER_MILLISECOND
                                ),
                                "attributes": attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }


class OtlpExporter:
    """The one exporter: posts a span's OTLP/HTTP JSON body to the configured endpoint.

    Built by `configure_export()` from `OTEL_EXPORTER_OTLP_ENDPOINT`, whose value
    gets `/v1/traces` -- the OTLP/HTTP traces path -- appended. `export()` never
    raises: a collector that is down, slow or answering an error is one warning
    line naming the failure and never a span's own facts, so the response and the
    stored row are exactly what they would have been with no exporter at all
    (`#101` AC3). The endpoint is not logged: a URL can carry a credential.
    """

    def __init__(self, endpoint: str, client: httpx2.Client | None = None) -> None:
        self.url = endpoint.rstrip("/") + OTLP_SPANS_PATH
        self._client = client if client is not None else httpx2.Client(timeout=OTLP_TIMEOUT_SECONDS)

    def export(self, span: Span) -> None:
        """Post one span and swallow every failure: the request is already answered."""
        try:
            response = self._client.post(self.url, json=otlp_payload(span))
        except Exception as error:
            logger.warning(
                "Could not export a %s span over OTLP: %s", span.name, type(error).__name__
            )
            return
        if response.is_error:
            logger.warning(
                "The OTLP endpoint answered %s to a %s span", response.status_code, span.name
            )


def configure_export() -> OtlpExporter | None:
    """Build the one exporter, or none when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

    Called once at process start from `app.main`, beside `configure_logging`. Unset
    or empty stores `None`, so no span leaves the process and the app makes no
    outbound call (`#101` AC1); a value builds exactly one exporter for the
    process. A second call replaces the first, as a second `configure_logging`
    does.
    """
    global _exporter
    endpoint = otlp_endpoint()
    _exporter = OtlpExporter(endpoint) if endpoint else None
    return _exporter


def export_span(span: Span) -> None:
    """Send one span to the configured endpoint, and never raise.

    A no-op when no exporter was configured -- the default, and the deployment
    `#90` shipped. Otherwise it rides beside `store_span` in `record_span`, after
    the database write and behind the response that was already sent, and
    `OtlpExporter.export()` is what keeps an unreachable collector from changing
    either (`#101` AC3).
    """
    if _exporter is not None:
        _exporter.export(span)


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


def is_an_error(status_code: int | None) -> bool:
    """Whether a span's status code is a failure: 500 or more, and `None` is not one.

    The one spelling of the rule, read by both metric reads and by the bucket the
    writer counts a span into, so no two of them can disagree about what an error is.
    """
    return status_code is not None and status_code >= ERROR_STATUS_CODE


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
        if is_an_error(status_code):
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
    """Delete every span older than `SPAN_RETENTION`, and the buckets that counted it.

    The only deleter of spans, called once per process start from `app.main`'s
    lifespan beside `reap_stale_runs()`, and never a route. The buckets go with the
    spans in the same `db.session_scope()`, so the table the bucket read answers from
    is as bounded as the spans are, and it answers the number of spans deleted -- the
    unit the caller's own log line names. `SPAN_RETENTION` is longer than
    `METRICS_WINDOW`, so no span a metric counted and no bucket that counted it is
    deleted while that metric still reads them: a bucket inside the window began at
    most an hour ago and the cutoff is seven days back.
    """
    cutoff = datetime.now(UTC) - SPAN_RETENTION
    with db.session_scope() as session:
        session.execute(delete(MetricBucket).where(MetricBucket.bucket_start < cutoff))
        return session.execute(
            delete(TelemetrySpan).where(TelemetrySpan.started_at < cutoff)
        ).rowcount
