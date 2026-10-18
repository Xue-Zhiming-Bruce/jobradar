import { cleanup, render, screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routeMetrics, traces } from '../mocks/fixtures'
import { server } from '../mocks/server'
import TelemetryScreen from './TelemetryScreen'

// The suite runs without Vitest globals, so Testing Library's auto-cleanup is off.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const LOADING_METRICS = 'Loading metrics...'
const EMPTY_METRICS = 'No metrics in this window yet.'
const LOADING_TRACES = 'Loading traces...'
const EMPTY_TRACES = 'No traces yet.'
const METRICS_CAPTION = 'Requests, errors and p95 duration per route in the window'

/** The trace id `#90` writes the digest journey under: the tick's span and its run's. */
const DIGEST_TRACE = traces[0].trace_id

const metricsTable = () => screen.getByRole('table', { name: METRICS_CAPTION })

/** The readable text of every table on screen, in document order. */
const captions = () =>
  screen.getAllByRole('table').map((table) => table.querySelector('caption')?.textContent)

/** The text of one row's cells, the row header aside. */
const cellsOf = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent)

/** An answer the test holds back: the handler returns this promise before it is resolved. */
function withheld() {
  let release!: (response: Response) => void
  const promise = new Promise<Response>((resolve) => (release = resolve))
  return { promise, release }
}

describe('the telemetry screen', () => {
  it('renders one metrics row per route, with its requests, errors and p95 (AC1)', async () => {
    render(<TelemetryScreen />)

    await screen.findByRole('table', { name: METRICS_CAPTION })

    expect(within(metricsTable()).getAllByRole('rowheader').map((row) => row.textContent)).toEqual(
      routeMetrics.map((metric) => metric.route),
    )
    const tick = screen.getByRole('rowheader', { name: '/internal/tick' }).closest('tr') as HTMLElement
    const matches = screen.getByRole('rowheader', { name: '/matches' }).closest('tr') as HTMLElement
    expect(cellsOf(tick)).toEqual(['3', '1', '128 ms'])
    expect(cellsOf(matches)).toEqual(['12', '0', '41 ms'])
  })

  it('lists the traces in the order the route sent them, each under its own trace id (AC2)', async () => {
    render(<TelemetryScreen />)

    await screen.findByRole('table', { name: `Trace ${DIGEST_TRACE}` })

    expect(captions()).toEqual([
      METRICS_CAPTION,
      ...traces.map((trace) => `Trace ${trace.trace_id}`),
    ])
    // Every span of one trace sits in that trace's own table, and no other's.
    expect(
      within(screen.getByRole('table', { name: `Trace ${traces[1].trace_id}` }))
        .getAllByRole('rowheader')
        .map((row) => row.textContent),
    ).toEqual(traces[1].spans.map((span) => span.name))
  })

  it('shows the tick request and its run as two spans under the one shared trace id (AC3)', async () => {
    render(<TelemetryScreen />)

    const digest = await screen.findByRole('table', { name: `Trace ${DIGEST_TRACE}` })

    expect(within(digest).getAllByRole('rowheader').map((row) => row.textContent)).toEqual([
      'http',
      'run',
    ])
    const request = within(digest).getByRole('rowheader', { name: 'http' })
    expect(cellsOf(request.closest('tr') as HTMLElement)).toEqual(['/internal/tick', '202', '4 ms'])
  })

  it("renders the run's own span, which has no route and no status, rather than a blank row (AC6)", async () => {
    render(<TelemetryScreen />)

    const run = await screen.findByRole('rowheader', { name: 'run' })

    expect(cellsOf(run.closest('tr') as HTMLElement)).toEqual(['—', '—', '3012 ms'])
    expect(within(screen.getByRole('table', { name: `Trace ${DIGEST_TRACE}` })).getByText('run')).toBeDefined()
  })
})

describe('the telemetry screen renders three states', () => {
  it('shows the loading text, and neither the empty nor the error text, until a read settles (AC4)', async () => {
    const metricsAnswer = withheld()
    const tracesAnswer = withheld()
    server.use(
      http.get('/telemetry/metrics', () => metricsAnswer.promise),
      http.get('/telemetry/traces', () => tracesAnswer.promise),
    )
    render(<TelemetryScreen />)

    expect(screen.getByText(LOADING_METRICS)).toBeDefined()
    expect(screen.getByText(LOADING_TRACES)).toBeDefined()
    expect(screen.queryByText(EMPTY_METRICS)).toBeNull()
    expect(screen.queryByText(EMPTY_TRACES)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()

    metricsAnswer.release(HttpResponse.json([]))
    tracesAnswer.release(HttpResponse.json([]))
    expect(await screen.findByText(EMPTY_METRICS)).toBeDefined()
    expect(await screen.findByText(EMPTY_TRACES)).toBeDefined()
    expect(screen.queryByText(LOADING_METRICS)).toBeNull()
  })

  it('shows an explicit empty state on both halves when there is no telemetry yet (AC4)', async () => {
    server.use(
      http.get('/telemetry/metrics', () => HttpResponse.json([])),
      http.get('/telemetry/traces', () => HttpResponse.json([])),
    )
    render(<TelemetryScreen />)

    expect(await screen.findByText(EMPTY_METRICS)).toBeDefined()
    expect(await screen.findByText(EMPTY_TRACES)).toBeDefined()
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it("shows each read's own server detail when both reads fail, and no table (AC4)", async () => {
    const metricsDetail = 'The metrics could not be read'
    const tracesDetail = 'The traces could not be read'
    server.use(
      http.get('/telemetry/metrics', () =>
        HttpResponse.json({ detail: metricsDetail }, { status: 500 }),
      ),
      http.get('/telemetry/traces', () =>
        HttpResponse.json({ detail: tracesDetail }, { status: 500 }),
      ),
    )
    render(<TelemetryScreen />)

    expect((await screen.findByText(metricsDetail)).textContent).toBe(metricsDetail)
    expect((await screen.findByText(tracesDetail)).textContent).toBe(tracesDetail)

    expect(screen.getAllByRole('alert').map((alert) => alert.textContent)).toEqual([
      metricsDetail,
      tracesDetail,
    ])
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByText(LOADING_METRICS)).toBeNull()
    expect(screen.queryByText(EMPTY_METRICS)).toBeNull()
  })

  it.each([
    ['/telemetry/metrics', `Trace ${DIGEST_TRACE}`],
    ['/telemetry/traces', METRICS_CAPTION],
  ])("shows %s's failure and still renders what the other read answered (AC5)", async (path, other) => {
    const detail = `The read of ${path} was refused`
    server.use(http.get(path, () => HttpResponse.json({ detail }, { status: 500 })))
    render(<TelemetryScreen />)

    expect((await screen.findByRole('alert')).textContent).toBe(detail)

    expect(await screen.findByRole('table', { name: other })).toBeDefined()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.queryByText(EMPTY_METRICS)).toBeNull()
    expect(screen.queryByText(EMPTY_TRACES)).toBeNull()
  })
})

const EMPTY_CHART = 'No spans in the last 60 minutes to chart.'
const CHART_CAPTION =
  'Requests, errors and p95 duration per clock-aligned minute over the last 60 minutes (UTC)'

/** One span in the wire shape `GET /telemetry/traces` answers a trace with. */
type WireSpan = {
  id: number
  name: string
  route: string | null
  method: string | null
  status_code: number | null
  duration_ms: number
  started_at: string
}

/** A minute of the fixture's own day, as a span's `started_at`: `at('09:04')`. */
const at = (clock: string) => `2026-01-01T${clock}:00.000000+00:00`

/** An HTTP request span, unless an override makes it the run's own route-less span. */
const spanAt = (id: number, clock: string, over: Partial<WireSpan> = {}): WireSpan => ({
  id,
  name: 'http',
  route: '/matches',
  method: 'GET',
  status_code: 200,
  duration_ms: 41,
  started_at: at(clock),
  ...over,
})

/** Answers the traces read with one trace holding these spans; no span is an empty window. */
function answerTraces(spans: WireSpan[] = []) {
  server.use(
    http.get('/telemetry/traces', () =>
      HttpResponse.json(spans.length === 0 ? [] : [{ trace_id: 'chart', spans }]),
    ),
  )
}

/**
 * The chart's window is read off the clock, so each test fixes it the way
 * `App.test.tsx` does -- fake timers faking `Date` -- at the fixture's own
 * minute, so the fixture's spans are the buckets drawn (#94 AC9).
 *
 * `setTimeout` stays real: Testing Library's `findBy*` waits on it.
 */
function useChartClock(instant = '2026-01-01T09:01:30Z') {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(instant))
}

/** The chart's plot: the one `img`-roled svg, named for the three series it draws. */
const plot = () => screen.getByRole('img', { name: /requests, errors and p95/i })

/** One series' drawn values, oldest bucket first: what each bar or point carries. */
const drawn = (series: string) =>
  [...plot().querySelectorAll(`g.${series} > *`)].map((element) => element.textContent)

/** The minutes that carry a request, and only those. */
const requestingMinutes = () => drawn('requests').filter((value) => !value.endsWith('requests 0'))

/** The chart of #94, drawn off the spans the traces read already answered with. */
describe('the telemetry chart', () => {
  it('draws 60 clock-aligned minutes, the last of them the read\'s own (AC1)', async () => {
    useChartClock()
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    // 08:02 .. 09:01: 60 minutes, none missing, the read's own minute last.
    expect(drawn('requests')).toHaveLength(60)
    expect(drawn('errors')).toHaveLength(60)
    expect(drawn('requests')[0]).toBe('08:02 requests 0')
    expect(drawn('requests')[59]).toBe('09:01 requests 1')
    // The fixture's two spans, each in its own minute and nowhere else.
    expect(requestingMinutes()).toEqual(['09:00 requests 1', '09:01 requests 1'])
    // Each series' own value on both of those minutes: 09:00 answers one 41 ms
    // request, 09:01 one 4 ms, and neither the 202 nor anything else is an error.
    expect(drawn('p95')).toEqual(['09:00 p95 41 ms', '09:01 p95 4 ms'])
    expect(drawn('errors')[59]).toBe('09:01 errors 0')
  })

  it("counts a span in its own startedAt minute, and drops a span outside the window (AC2)", async () => {
    useChartClock()
    answerTraces([
      spanAt(1, '09:00'),
      // Four hours before the window, and three minutes after the read's own minute.
      spanAt(2, '05:00', { duration_ms: 900 }),
      spanAt(3, '09:04', { duration_ms: 7 }),
    ])
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    expect(requestingMinutes()).toEqual(['09:00 requests 1'])
    expect(drawn('p95')).toEqual(['09:00 p95 41 ms'])
  })

  it("does not count the run's own route-less span as a request (AC2)", async () => {
    useChartClock()
    answerTraces([
      spanAt(1, '09:00', { name: 'run', route: null, method: null, status_code: null, duration_ms: 3012 }),
      spanAt(2, '09:01'),
    ])
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    expect(drawn('requests')[58]).toBe('09:00 requests 0')
    expect(requestingMinutes()).toEqual(['09:01 requests 1'])
  })

  it('counts a 500 as an error, and a 4xx or a missing status as a request that is not one (AC2)', async () => {
    useChartClock()
    answerTraces([
      spanAt(1, '09:01', { status_code: null }),
      spanAt(2, '09:01', { status_code: 404 }),
      spanAt(3, '09:01', { status_code: 500 }),
    ])
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    expect(drawn('requests')[59]).toBe('09:01 requests 3')
    expect(drawn('errors')[59]).toBe('09:01 errors 1')
  })

  it('draws a quiet minute as 0 requests, 0 errors and no p95 point (AC3)', async () => {
    useChartClock()
    answerTraces([
      // 09:00 holds the run's own span and no request: a quiet minute, never a 0 ms reply.
      spanAt(1, '09:00', { name: 'run', route: null, method: null, status_code: null, duration_ms: 3012 }),
      spanAt(2, '09:01', { duration_ms: 7 }),
    ])
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    expect(drawn('requests')[58]).toBe('09:00 requests 0')
    expect(drawn('errors')[58]).toBe('09:00 errors 0')
    // The gap, never a `0 ms` point.
    expect(drawn('p95')).toEqual(['09:01 p95 7 ms'])
  })

  it("draws a bucket's p95 by the metrics read's own nearest-rank rule (AC4)", async () => {
    useChartClock()
    answerTraces([
      // Ten requests in one minute, durations 1 .. 10 ms: the longest is the 95th.
      ...Array.from({ length: 10 }, (_, index) => spanAt(index + 1, '09:01', { duration_ms: index + 1 })),
      // One request in the minute before it: its own duration.
      spanAt(11, '09:00', { duration_ms: 7 }),
    ])
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    expect(drawn('requests')[59]).toBe('09:01 requests 10')
    expect(drawn('p95')).toEqual(['09:00 p95 7 ms', '09:01 p95 10 ms'])
  })

  it('draws one series per measure over every route, leaving the per-route table as it was (AC5)', async () => {
    useChartClock()
    answerTraces([spanAt(1, '09:01'), spanAt(2, '09:01', { route: '/internal/tick' })])
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    // Two routes in one minute are two requests on one series, not a series each.
    expect(drawn('requests')[59]).toBe('09:01 requests 2')
    const table = await screen.findByRole('table', { name: METRICS_CAPTION })
    expect(within(table).getAllByRole('rowheader').map((row) => row.textContent)).toEqual(
      routeMetrics.map((metric) => metric.route),
    )
  })

  it('reads each unit against its own labelled panel (AC6)', async () => {
    useChartClock()
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    const panels = [...screen.getByRole('figure').querySelectorAll('svg > g > text')].map(
      (text) => text.textContent,
    )
    expect(panels).toEqual(['requests', 'ms'])
  })

  it('names itself in a caption and a legend, and labels the plot for a reader who cannot see it (AC7)', async () => {
    const spies = ['error', 'log'].map((name) => vi.spyOn(console, name as 'error'))
    useChartClock()
    render(<TelemetryScreen />)

    await screen.findByRole('img')

    const figure = screen.getByRole('figure')
    expect(figure.querySelector('figcaption')?.textContent).toBe(CHART_CAPTION)
    expect(within(figure).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Requests',
      'Errors',
      'p95 (ms)',
    ])
    // The one svg is one image, so its label names all three series (#94 AC7).
    expect(plot()).toBeDefined()
    // And the chart logs nothing (#94 AC10).
    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })
})

/** The chart's four boundary states: withheld, refused, an empty window and an empty read (AC8). */
describe('the telemetry chart draws only off a settled traces read', () => {
  it('is withheld, and reads the traces route once, until that read answers (AC8)', async () => {
    useChartClock()
    const tracesAnswer = withheld()
    let reads = 0
    server.use(
      http.get('/telemetry/traces', () => {
        reads += 1
        return tracesAnswer.promise
      }),
    )
    render(<TelemetryScreen />)

    // The metrics read settles on its own and the chart is still not drawn.
    expect(await screen.findByRole('table', { name: METRICS_CAPTION })).toBeDefined()
    expect(screen.getByText(LOADING_TRACES)).toBeDefined()
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.queryByRole('figure')).toBeNull()
    expect(reads).toBe(1)
  })

  it('is not drawn when the traces read failed, beside that read\'s one alert (AC8)', async () => {
    useChartClock()
    const detail = 'The traces could not be read'
    server.use(http.get('/telemetry/traces', () => HttpResponse.json({ detail }, { status: 500 })))
    render(<TelemetryScreen />)

    expect((await screen.findByText(detail)).textContent).toBe(detail)
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.queryByRole('img')).toBeNull()
    // The metrics table the chart sits under is untouched.
    expect(screen.getByRole('table', { name: METRICS_CAPTION })).toBeDefined()
  })

  it('draws a sentence in place of the plot when the read answered with nothing in the window (AC8)', async () => {
    useChartClock()
    answerTraces()
    render(<TelemetryScreen />)

    expect(await screen.findByText(EMPTY_CHART)).toBeDefined()
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.queryByRole('figure')).toBeNull()
    // No state of the chart hides the metrics table or the trace list.
    expect(screen.getByRole('table', { name: METRICS_CAPTION })).toBeDefined()
    expect(screen.getByText(EMPTY_TRACES)).toBeDefined()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('is that same sentence when every span the read carried is outside the window (AC8)', async () => {
    useChartClock()
    answerTraces([spanAt(1, '05:00')])
    render(<TelemetryScreen />)

    expect(await screen.findByText(EMPTY_CHART)).toBeDefined()
    expect(screen.queryByRole('img')).toBeNull()
    // The trace list beside it still renders the span the chart could not draw.
    expect(await screen.findByRole('table', { name: 'Trace chart' })).toBeDefined()
  })
})
