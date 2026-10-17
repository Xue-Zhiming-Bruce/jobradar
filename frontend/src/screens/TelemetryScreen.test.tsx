import { cleanup, render, screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { routeMetrics, traces } from '../mocks/fixtures'
import { server } from '../mocks/server'
import TelemetryScreen from './TelemetryScreen'

// The suite runs without Vitest globals, so Testing Library's auto-cleanup is off.
afterEach(cleanup)

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
