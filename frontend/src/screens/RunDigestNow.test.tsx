import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { runSummary, runSucceeded, runBudgetStopped, runBudgetStoppedOne, runBudgetStoppedRunning, runBudgetStoppedFailed, runBudgetStoppedZero } from '../mocks/fixtures'
import RunDigestNow from './RunDigestNow'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const POLL_MS = 1000

/**
 * The poll runs on the test's clock, so no test waits on a wall clock.
 *
 * `setImmediate` stays real: React schedules its own effect flush on it, and
 * faking it would make the install of the poll a clock beat to guess at rather
 * than a thing that has happened by the time the test looks.
 */
const useTestClock = () =>
  vi.useFakeTimers({
    toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
  })

/** Clicks the button and lets the mocked `POST /internal/tick` answer. */
async function runNow() {
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Run digest now' })))
}

/** Lets one poll interval pass, and the read it starts come back. */
async function poll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(POLL_MS)
  })
}

describe('the run digest now button', () => {
  it('starts one run, reads its id until it ends, and shows the counts it ended with', async () => {
    useTestClock()
    const ticks: string[] = []
    const reads: string[] = []
    server.use(
      http.post('/internal/tick', ({ request }) => {
        ticks.push(request.method)
        return HttpResponse.json(runSummary, { status: 202 })
      }),
      http.get('/runs/:id', ({ request }) => {
        reads.push(new URL(request.url).pathname)
        return HttpResponse.json(reads.length === 1 ? runSummary : runSucceeded)
      }),
    )
    render(<RunDigestNow />)

    await runNow()

    expect(ticks).toEqual(['POST'])
    expect(screen.getByText('Digest running...')).toBeDefined()

    await poll()
    expect(reads).toEqual(['/runs/1'])
    expect(screen.getByText('Digest running...')).toBeDefined()

    await poll()
    expect(reads).toEqual(['/runs/1', '/runs/1'])
    expect(screen.getByText('Digest finished: 4 matches created.')).toBeDefined()

    // Terminal: far past any poll interval, the mock is asked for nothing more.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 10)
    })
    expect(reads).toEqual(['/runs/1', '/runs/1'])
  })

  it('shows the error the failed run carried, and stops reading once it is terminal', async () => {
    useTestClock()
    const reads: string[] = []
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      http.get('/runs/:id', ({ request }) => {
        reads.push(new URL(request.url).pathname)
        return HttpResponse.json({
          ...runSummary,
          status: 'failed',
          error: 'Greenhouse timed out',
          company_outcomes: [
            { company_id: 1, slug: 'acme', outcome: 'failed', error: 'Connection refused' },
          ],
        })
      }),
    )
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest failed: Greenhouse timed out')).toBeDefined()
    // The per-board list is added beside the run-level line, never in place of it.
    expect(screen.getByText('acme: Connection refused')).toBeDefined()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 10)
    })
    expect(reads).toEqual(['/runs/1'])
  })

  it('lists every failed board, in walk order, and only the failed ones', async () => {
    useTestClock()
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      http.get('/runs/:id', () =>
        HttpResponse.json({
          ...runSummary,
          status: 'failed',
          error: '2 of 4 companies failed',
          company_outcomes: [
            { company_id: 1, slug: 'acme', outcome: 'fetched', error: null },
            { company_id: 2, slug: 'globex', outcome: 'not_modified', error: null },
            { company_id: 3, slug: 'alpha', outcome: 'failed', error: 'Connection refused' },
            { company_id: 4, slug: 'beta', outcome: 'failed', error: 'Greenhouse timed out' },
          ],
        }),
      ),
    )
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest failed: 2 of 4 companies failed')).toBeDefined()
    // Each failed board's slug and its own message, in the order the walk visited them.
    expect(screen.getAllByRole('listitem').map((board) => board.textContent)).toEqual([
      'alpha: Connection refused',
      'beta: Greenhouse timed out',
    ])
    expect(screen.queryByText(/acme|globex/)).toBeNull()
  })

  it('shows no board group when a failed run has no failed board', async () => {
    useTestClock()
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      // The pipeline-raised path: a failed run with an error and no `run_company` row.
      http.get('/runs/:id', () =>
        HttpResponse.json({ ...runSummary, status: 'failed', error: 'tick blew up' }),
      ),
    )
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest failed: tick blew up')).toBeDefined()
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.queryByText('Boards the run could not fetch:')).toBeNull()
  })

  it('shows a failed board whose error is null as its slug alone', async () => {
    useTestClock()
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      http.get('/runs/:id', () =>
        HttpResponse.json({
          ...runSummary,
          status: 'failed',
          error: '1 of 2 companies failed',
          company_outcomes: [{ company_id: 9, slug: 'gamma', outcome: 'failed', error: null }],
        }),
      ),
    )
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByRole('listitem').textContent).toBe('gamma')
    expect(screen.queryByText(/null|undefined/)).toBeNull()
  })

  it('lists a failed board beside the running line, since the list is not a status branch', async () => {
    useTestClock()
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      http.get('/runs/:id', () =>
        HttpResponse.json({
          ...runSummary,
          company_outcomes: [{ company_id: 9, slug: 'delta', outcome: 'failed', error: 'Timeout' }],
        }),
      ),
    )
    render(<RunDigestNow />)

    // The `202`'s empty list: the running line, and no group at all.
    await runNow()
    expect(screen.getByText('Digest running...')).toBeDefined()
    expect(screen.queryByRole('list')).toBeNull()

    // A run the tick has not finished can already carry a board the walk gave up on.
    await poll()
    expect(screen.getByText('Digest running...')).toBeDefined()
    expect(screen.getByText('delta: Timeout')).toBeDefined()
  })

  it('shows a refused tick as an error instead of a crash, and starts no read', async () => {
    useTestClock()
    const spies = ['error', 'log'].map((name) => vi.spyOn(console, name as 'error'))
    const detail = 'The request is not allowed'
    const reads: string[] = []
    server.use(
      http.post('/internal/tick', () => HttpResponse.json({ detail }, { status: 401 })),
      http.get('/runs/:id', ({ request }) => {
        reads.push(new URL(request.url).pathname)
        return HttpResponse.json(runSummary)
      }),
    )
    render(<RunDigestNow />)

    await runNow()

    expect(screen.getByRole('alert').textContent).toBe(detail)
    expect(reads).toEqual([])
    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })
})

/** One test per case in #72's AC1-AC7: the run row is always read from a fixture. */
describe('the budget-stopped line', () => {
  /** Test helper: installs the fake clock, then answers the tick with the fresh row and the read with `row`. */
  function useRunReads(row: JsonBodyType) {
    useTestClock()
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      http.get('/runs/:id', () => HttpResponse.json(row)),
    )
  }

  it('names the companies the budget stopped the walk from fetching (AC1)', async () => {
    useRunReads(runBudgetStopped)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(
      screen.getByText("3 companies were not fetched: the run's request budget was reached."),
    ).toBeDefined()
  })

  it('says "1 company was", never "1 companies" (AC2)', async () => {
    useRunReads(runBudgetStoppedOne)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(
      screen.getByText("1 company was not fetched: the run's request budget was reached."),
    ).toBeDefined()
  })

  it('adds the sentence beside "Digest finished", replacing neither (AC3)', async () => {
    useRunReads(runBudgetStopped)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest finished: 4 matches created.')).toBeDefined()
    expect(
      screen.getByText("3 companies were not fetched: the run's request budget was reached."),
    ).toBeDefined()
  })

  it('renders the sentence while the row is still running (AC4)', async () => {
    useRunReads(runBudgetStoppedRunning)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest running...')).toBeDefined()
    expect(
      screen.getByText("3 companies were not fetched: the run's request budget was reached."),
    ).toBeDefined()
  })

  it('renders the sentence next to a failed run\'s error (AC5)', async () => {
    useRunReads(runBudgetStoppedFailed)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest failed: Greenhouse timed out')).toBeDefined()
    expect(
      screen.getByText("3 companies were not fetched: the run's request budget was reached."),
    ).toBeDefined()
  })

  it('renders no sentence for a null count (AC6)', async () => {
    useRunReads(runSucceeded)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest finished: 4 matches created.')).toBeDefined()
    expect(screen.queryByText(/not fetched/)).toBeNull()
  })

  it('renders no sentence for a count of 0 (AC7)', async () => {
    useRunReads(runBudgetStoppedZero)
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest finished: 4 matches created.')).toBeDefined()
    expect(screen.queryByText(/not fetched/)).toBeNull()
  })
})
