import { useEffect, useState } from 'react'
import {
  listRouteMetrics,
  listTraces,
  type RouteMetric,
  type Trace,
  type TraceSpan,
} from '../api/client'
import TelemetryChart from './TelemetryChart'
import { traceAnchorId } from './traceAnchor'

/*
 * Three outcomes a reader can tell apart, per read: not read yet, read and
 * empty, refused. The two reads settle on their own, so a failed one shows its
 * own error and still leaves the other's table on screen (#91 AC5).
 */
const LOADING_METRICS = 'Loading metrics...'
const EMPTY_METRICS = 'No metrics in this window yet.'
const LOADING_TRACES = 'Loading traces...'
const EMPTY_TRACES = 'No traces yet.'

/** What a span's route and status cell shows when the span is not an HTTP request. */
const ABSENT = '—'

/** One route's row: the route, its requests, its errors and its p95 duration. */
function MetricRow({ metric }: { metric: RouteMetric }) {
  return (
    <tr>
      <th scope="row">{metric.route}</th>
      <td>{metric.count}</td>
      <td>{metric.errors}</td>
      <td>{metric.p95Ms} ms</td>
    </tr>
  )
}

/**
 * A span's own row. A span that is not an HTTP request -- the run's own, named
 * `run` -- carries no route and no status code, and its row says so rather than
 * going blank (#91 AC6).
 */
function SpanRow({ span }: { span: TraceSpan }) {
  return (
    <tr>
      <th scope="row">{span.name}</th>
      <td>{span.route ?? ABSENT}</td>
      <td>{span.statusCode ?? ABSENT}</td>
      <td>{span.durationMs} ms</td>
    </tr>
  )
}

/** The traces read's answer, and the instant it answered: the chart's window ends at that minute. */
type TracesRead = {
  rows: Trace[]
  readAt: number
}

/** The metrics and the recent traces `#90` stores, read from the two telemetry routes. */
export default function TelemetryScreen() {
  // `null` is "not read yet" for each read alone, so loading and empty stay two
  // screens, and one read's outcome never stands in for the other's.
  const [metrics, setMetrics] = useState<RouteMetric[] | null>(null)
  const [metricsError, setMetricsError] = useState<string | null>(null)
  const [traces, setTraces] = useState<TracesRead | null>(null)
  const [tracesError, setTracesError] = useState<string | null>(null)

  useEffect(() => {
    listRouteMetrics().then(setMetrics, (failure: unknown) => {
      setMetricsError(failure instanceof Error ? failure.message : String(failure))
    })
    // The rows and the instant they answered are set together: the chart's window
    // ends at that minute and does not slide under a reader (#94 AC1).
    listTraces().then(
      (rows) => setTraces({ rows, readAt: Date.now() }),
      (failure: unknown) => {
        setTracesError(failure instanceof Error ? failure.message : String(failure))
      },
    )
  }, [])

  return (
    <section>
      <h2>Telemetry</h2>
      <section>
        <h3>Route metrics</h3>
        {metricsError !== null ? (
          <p role="alert">{metricsError}</p>
        ) : metrics === null ? (
          <p>{LOADING_METRICS}</p>
        ) : metrics.length === 0 ? (
          <p>{EMPTY_METRICS}</p>
        ) : (
          <table>
            <caption>Requests, errors and p95 duration per route in the window</caption>
            <thead>
              <tr>
                <th scope="col">Route</th>
                <th scope="col">Requests</th>
                <th scope="col">Errors</th>
                <th scope="col">p95</th>
              </tr>
            </thead>
            <tbody>
              {metrics.map((metric) => (
                <MetricRow key={metric.route} metric={metric} />
              ))}
            </tbody>
          </table>
        )}
      </section>
      {/*
       * Under the per-route table and over the trace list: the window's series,
       * derived from the traces read and never a read of its own. A refused read
       * never settles `traces`, so it draws no chart beside its own alert (#94 AC8).
       */}
      {traces !== null && <TelemetryChart traces={traces.rows} readAt={traces.readAt} />}
      <section>
        <h3>Recent traces</h3>
        {tracesError !== null ? (
          <p role="alert">{tracesError}</p>
        ) : traces === null ? (
          <p>{LOADING_TRACES}</p>
        ) : traces.rows.length === 0 ? (
          <p>{EMPTY_TRACES}</p>
        ) : (
          // Newest first, and each trace's spans in start order: the route's own
          // order, rendered as it arrived. Each table carries the anchor the run
          // screen's fragment names, so a run reaches its own trace (#95 AC2).
          traces.rows.map((trace) => (
            <table key={trace.traceId} id={traceAnchorId(trace.traceId)}>
              <caption>Trace {trace.traceId}</caption>
              <thead>
                <tr>
                  <th scope="col">Span</th>
                  <th scope="col">Route</th>
                  <th scope="col">Status</th>
                  <th scope="col">Duration</th>
                </tr>
              </thead>
              <tbody>
                {trace.spans.map((span) => (
                  <SpanRow key={span.id} span={span} />
                ))}
              </tbody>
            </table>
          ))
        )}
      </section>
    </section>
  )
}
