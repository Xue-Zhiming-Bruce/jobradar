import type { Trace, TraceSpan } from '../api/client'

/**
 * The window the chart covers, and its bucket size, mirroring `METRICS_WINDOW`
 * (`backend/app/telemetry.py`: 1 hour): one bucket per clock-aligned minute, and
 * the window is spelled once here rather than as a literal at the plot (#94 AC1).
 */
const WINDOW_MINUTES = 60
const MINUTE_MS = 60_000

/** The metrics read's own percentile rule: the durations ascending, at index `ceil(0.95 * n) - 1` (#94 AC4). */
const P95 = 0.95

/*
 * The plot's own geometry: hand-rolled svg, no charting library (#94 scope), and
 * one panel per unit, so no two units share a y-axis (#94 AC6).
 */
const WIDTH = 600
const PLOT_HEIGHT = 60
const PANEL_LABEL_HEIGHT = 14
const PANEL_HEIGHT = PANEL_LABEL_HEIGHT + PLOT_HEIGHT
const PANEL_GAP = 28
const HEIGHT = PANEL_HEIGHT * 2 + PANEL_GAP
/** One minute's slot on the x-axis, and the two count bars that share it. */
const SLOT = WIDTH / WINDOW_MINUTES
const BAR_WIDTH = SLOT / 2 - 1

/** Each panel's y-axis label: the unit its series are read against (#94 AC6). */
const REQUESTS_AXIS = 'requests'
const DURATION_AXIS = 'ms'

/** One color per series, carried by both the plot and its legend entry, so the two cannot drift. */
const REQUESTS_COLOR = '#aa3bff'
const ERRORS_COLOR = '#d7263d'
const P95_COLOR = '#0a7ea4'

/** The three series, each named in the legend (#94 AC7). */
const SERIES = [
  { name: 'Requests', color: REQUESTS_COLOR },
  { name: 'Errors', color: ERRORS_COLOR },
  { name: 'p95 (ms)', color: P95_COLOR },
]

/** Names the window and its bucket size, and labels the plot for a reader who cannot see it (#94 AC7). */
const CAPTION = `Requests, errors and p95 duration per clock-aligned minute over the last ${WINDOW_MINUTES} minutes (UTC)`

/** What a read that answered with no span inside the window says in place of the plot (#94 AC8). */
const EMPTY_CHART = `No spans in the last ${WINDOW_MINUTES} minutes to chart.`

/** One minute of the window and what the spans that started in it add up to (#94 AC1). */
type Bucket = {
  /** The minute itself, clock-aligned, in ms since the epoch. */
  minute: number
  /** The spans in it that have a route: the run's own span is not a request (#94 AC2). */
  requests: number
  /** Of the requests, the ones with a status of 500 or more (#94 AC2). */
  errors: number
  /** `null` on a minute with no request: a gap in the series, never `0 ms` (#94 AC3). */
  p95Ms: number | null
}

/** The minute an instant sits in, clock-aligned to the minute as a clock reads it. */
const minuteOf = (instant: number) => Math.floor(instant / MINUTE_MS) * MINUTE_MS

/** A minute as a reader reads it: `09:01`. The route's own timestamps are UTC, so the label is too. */
const clockOf = (minute: number) => new Date(minute).toISOString().slice(11, 16)

/** Whether the span's own `startedAt` minute is one of the window's (#94 AC2). */
function insideWindow(span: TraceSpan, firstMinute: number, lastMinute: number): boolean {
  const minute = minuteOf(Date.parse(span.startedAt))
  return minute >= firstMinute && minute <= lastMinute
}

/** The nearest rank `P95` of these whole-millisecond durations, the metrics read's own rule (#94 AC4). */
function nearestRankP95(durations: number[]): number {
  const ordered = [...durations].sort((left, right) => left - right)
  return ordered[Math.ceil(P95 * ordered.length) - 1]
}

/**
 * The `WINDOW_MINUTES` buckets beginning at `firstMinute`, each carrying its
 * requests, its errors and its p95, from the spans inside the window alone.
 *
 * Every span counts in its own minute: a span with no route -- the run's own,
 * named `run` -- is not a request, a span with no status code is not an error, a
 * 4xx is a request and not an error, and 500 or more is both (#94 AC2).
 */
function bucketsOf(spans: TraceSpan[], firstMinute: number): Bucket[] {
  const buckets = Array.from({ length: WINDOW_MINUTES }, (_, index) => ({
    minute: firstMinute + index * MINUTE_MS,
    requests: 0,
    errors: 0,
    durations: [] as number[],
  }))

  for (const span of spans) {
    if (span.route === null) continue
    const bucket = buckets[(minuteOf(Date.parse(span.startedAt)) - firstMinute) / MINUTE_MS]
    bucket.requests += 1
    bucket.durations.push(span.durationMs)
    if (span.statusCode !== null && span.statusCode >= 500) bucket.errors += 1
  }

  return buckets.map(({ minute, requests, errors, durations }) => ({
    minute,
    requests,
    errors,
    p95Ms: durations.length === 0 ? null : nearestRankP95(durations),
  }))
}

/**
 * One minute's count, as a bar against its panel's own axis. A quiet minute is
 * still drawn, at no height, rather than left out of the series (#94 AC3).
 */
function Bar({
  x,
  value,
  max,
  color,
  label,
}: {
  x: number
  value: number
  max: number
  color: string
  label: string
}) {
  const height = (value / max) * PLOT_HEIGHT
  return (
    <rect x={x} y={PLOT_HEIGHT - height} width={BAR_WIDTH} height={height} fill={color}>
      <title>{label}</title>
    </rect>
  )
}

/** One minute's p95, as a point against its panel's own axis (#94 AC1). */
function Point({
  x,
  value,
  max,
  label,
}: {
  x: number
  value: number
  max: number
  label: string
}) {
  const height = (value / max) * PLOT_HEIGHT
  return (
    <circle cx={x} cy={PLOT_HEIGHT - height} r={2.5} fill={P95_COLOR}>
      <title>{label}</title>
    </circle>
  )
}

/** The counts panel: requests and errors, one count on each side of the same minute (#94 AC5). */
function CountsPanel({ buckets }: { buckets: Bucket[] }) {
  // One scale for the panel, floored at 1 so a window of quiet minutes cannot divide by zero.
  const max = Math.max(1, ...buckets.map((bucket) => Math.max(bucket.requests, bucket.errors)))
  return (
    <g>
      <text x={0} y={12}>
        {REQUESTS_AXIS}
      </text>
      <g transform={`translate(0, ${PANEL_LABEL_HEIGHT})`}>
        <g className="requests">
          {buckets.map((bucket, index) => (
            <Bar
              key={bucket.minute}
              x={index * SLOT + 1}
              value={bucket.requests}
              max={max}
              color={REQUESTS_COLOR}
              label={`${clockOf(bucket.minute)} requests ${bucket.requests}`}
            />
          ))}
        </g>
        <g className="errors">
          {buckets.map((bucket, index) => (
            <Bar
              key={bucket.minute}
              x={index * SLOT + SLOT / 2}
              value={bucket.errors}
              max={max}
              color={ERRORS_COLOR}
              label={`${clockOf(bucket.minute)} errors ${bucket.errors}`}
            />
          ))}
        </g>
      </g>
    </g>
  )
}

/** The duration panel, on its own `ms` axis: a quiet minute leaves a gap, not a zero (#94 AC3). */
function DurationPanel({ buckets }: { buckets: Bucket[] }) {
  const max = Math.max(1, ...buckets.map((bucket) => bucket.p95Ms ?? 0))
  return (
    <g transform={`translate(0, ${PANEL_HEIGHT + PANEL_GAP})`}>
      <text x={0} y={12}>
        {DURATION_AXIS}
      </text>
      <g transform={`translate(0, ${PANEL_LABEL_HEIGHT})`}>
        <g className="p95">
          {buckets.map((bucket, index) =>
            bucket.p95Ms === null ? null : (
              <Point
                key={bucket.minute}
                x={index * SLOT + SLOT / 2}
                value={bucket.p95Ms}
                max={max}
                label={`${clockOf(bucket.minute)} p95 ${bucket.p95Ms} ms`}
              />
            ),
          )}
        </g>
      </g>
    </g>
  )
}

/**
 * The window's requests, errors and p95 duration, minute by minute, ending at the
 * minute the traces read answered in (`readAt`).
 *
 * The series are derived here, in the browser, from the spans the traces read
 * already answered with: the metrics route carries no time axis, so it has no
 * series to plot from (#94, #105 owns serving per-minute buckets).
 */
export default function TelemetryChart({ traces, readAt }: { traces: Trace[]; readAt: number }) {
  const lastMinute = minuteOf(readAt)
  const firstMinute = lastMinute - (WINDOW_MINUTES - 1) * MINUTE_MS
  const spans = traces
    .flatMap((trace) => trace.spans)
    .filter((span) => insideWindow(span, firstMinute, lastMinute))

  if (spans.length === 0) return <p>{EMPTY_CHART}</p>

  const buckets = bucketsOf(spans, firstMinute)

  return (
    <figure>
      <figcaption>{CAPTION}</figcaption>
      <svg role="img" aria-label={CAPTION} viewBox={`0 0 ${WIDTH} ${HEIGHT}`}>
        <CountsPanel buckets={buckets} />
        <DurationPanel buckets={buckets} />
      </svg>
      <ul className="chart-legend">
        {SERIES.map((series) => (
          <li key={series.name}>
            <span className="chart-swatch" style={{ background: series.color }} aria-hidden="true" />
            {series.name}
          </li>
        ))}
      </ul>
    </figure>
  )
}
