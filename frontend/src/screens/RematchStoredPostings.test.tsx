import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { runSummary, runSucceeded } from '../mocks/fixtures'
import RematchStoredPostings from './RematchStoredPostings'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const POLL_MS = 1000

/** The poll runs on the test's clock, so no test waits on a wall clock. */
const useTestClock = () =>
  vi.useFakeTimers({
    toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
  })

/** Clicks the button and lets the mocked `POST /internal/rematch` answer. */
async function rematchNow() {
  await act(async () =>
    fireEvent.click(screen.getByRole('button', { name: 'Re-match stored postings' })),
  )
}

/** Lets one poll interval pass, and the read it starts come back. */
async function poll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(POLL_MS)
  })
}

/** Installs the clock, answers the press with the fresh row, and every read with `row`. */
function useRunReads(row: JsonBodyType) {
  useTestClock()
  server.use(
    http.post('/internal/rematch', () => HttpResponse.json(runSummary, { status: 202 })),
    http.get('/runs/:id', () => HttpResponse.json(row)),
  )
}

describe('the re-match stored postings button', () => {
  it('starts one run, reads its id until it ends, and stops reading once it is terminal', async () => {
    useTestClock()
    const presses: string[] = []
    const reads: string[] = []
    server.use(
      http.post('/internal/rematch', ({ request }) => {
        presses.push(new URL(request.url).pathname)
        return HttpResponse.json(runSummary, { status: 202 })
      }),
      http.get('/runs/:id', ({ request }) => {
        reads.push(new URL(request.url).pathname)
        return HttpResponse.json(reads.length === 1 ? runSummary : runSucceeded)
      }),
    )
    render(<RematchStoredPostings />)

    await rematchNow()

    expect(presses).toEqual(['/internal/rematch'])
    expect(screen.getByText('Re-match running...')).toBeDefined()
    expect(screen.queryByText(/finished/)).toBeNull()

    await poll()
    expect(reads).toEqual(['/runs/1'])
    expect(screen.getByText('Re-match running...')).toBeDefined()

    await poll()
    expect(reads).toEqual(['/runs/1', '/runs/1'])
    expect(screen.getByText('Re-match finished: 4 postings scored.')).toBeDefined()

    // Terminal: far past any poll interval, the mock is asked for nothing more.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 10)
    })
    expect(reads).toEqual(['/runs/1', '/runs/1'])
  })

  it('reads a count of 1 as singular: "1 posting scored"', async () => {
    useRunReads({ ...runSucceeded, matches_created: 1 })
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(screen.getByText('Re-match finished: 1 posting scored.')).toBeDefined()
  })

  it('adds the cap line beside the finished line, replacing neither', async () => {
    useRunReads({ ...runSucceeded, postings_not_rematched: 2 })
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(screen.getByText('Re-match finished: 4 postings scored.')).toBeDefined()
    expect(
      screen.getByText("2 stored postings were left: the re-match's cap was reached."),
    ).toBeDefined()
  })

  it('reads a count of 1 as singular: "1 stored posting was left"', async () => {
    useRunReads({ ...runSucceeded, postings_not_rematched: 1 })
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(
      screen.getByText("1 stored posting was left: the re-match's cap was reached."),
    ).toBeDefined()
  })

  it('renders no cap line for a null count, so a complete pass reads like a complete pass', async () => {
    useRunReads(runSucceeded) // postings_not_rematched: null
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(screen.getByText('Re-match finished: 4 postings scored.')).toBeDefined()
    expect(screen.queryByText(/left/)).toBeNull()
  })

  it('renders no cap line for a count of 0, which is contract-legal but never written', async () => {
    useRunReads({ ...runSucceeded, postings_not_rematched: 0 })
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(screen.getByText('Re-match finished: 4 postings scored.')).toBeDefined()
    expect(screen.queryByText(/left/)).toBeNull()
  })

  it('shows the failed run\'s own error verbatim, including the missing resume', async () => {
    const error = 'No active resume: upload one before a run can score postings'
    useRunReads({ ...runSummary, status: 'failed', error })
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(screen.getByText(`Re-match failed: ${error}`)).toBeDefined()
  })

  it('renders no failed-board group, since a re-match writes no run_company row', async () => {
    useRunReads({
      ...runSucceeded,
      company_outcomes: [{ company_id: 1, slug: 'acme', outcome: 'failed', error: 'Timeout' }],
    })
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()

    expect(screen.getByText('Re-match finished: 4 postings scored.')).toBeDefined()
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.queryByText('Boards the run could not fetch:')).toBeNull()
  })

  it('shows a refused press as the server detail and starts no read', async () => {
    useTestClock()
    const spies = ['error', 'log'].map((name) => vi.spyOn(console, name as 'error'))
    const detail = 'The request is not admitted: it carried neither the shared internal header nor an Origin naming this instance\'s own host'
    const reads: string[] = []
    server.use(
      http.post('/internal/rematch', () => HttpResponse.json({ detail }, { status: 401 })),
      http.get('/runs/:id', ({ request }) => {
        reads.push(new URL(request.url).pathname)
        return HttpResponse.json(runSummary)
      }),
    )
    render(<RematchStoredPostings />)

    await rematchNow()

    expect(screen.getByRole('alert').textContent).toBe(detail)
    expect(reads).toEqual([])
    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })

  it('leaves no stale count from an earlier press when a later press is refused', async () => {
    useTestClock()
    const detail = 'A run is already in progress in this process'
    let refused = false
    server.use(
      http.post('/internal/rematch', () =>
        refused
          ? HttpResponse.json({ detail }, { status: 409 })
          : HttpResponse.json(runSummary, { status: 202 }),
      ),
      http.get('/runs/:id', () => HttpResponse.json({ ...runSucceeded, postings_not_rematched: 2 })),
    )
    render(<RematchStoredPostings />)

    await rematchNow()
    await poll()
    expect(
      screen.getByText("2 stored postings were left: the re-match's cap was reached."),
    ).toBeDefined()

    refused = true
    await rematchNow()

    expect(screen.getByRole('alert').textContent).toBe(detail)
    expect(screen.queryByText(/left/)).toBeNull()
    expect(screen.queryByText(/finished/)).toBeNull()
    expect(screen.queryByText('Re-match running...')).toBeNull()
  })
})
