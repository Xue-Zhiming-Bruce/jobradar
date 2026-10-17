import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { companies, matches, runSummary, runSucceeded } from './mocks/fixtures'
import { server } from './mocks/server'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('the app', () => {
  it('renders the resume upload, the watchlist, the matches and the two run controls', async () => {
    render(<App />)

    expect(screen.getByLabelText('Resume file')).toBeDefined()
    expect(await screen.findByText('Acme (acme)')).toBeDefined()
    expect(await screen.findByText('Globex: Data engineer')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Run digest now' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Re-match stored postings' })).toBeDefined()
    expect(await screen.findByRole('heading', { name: 'Telemetry' })).toBeDefined()
  })

  it('shows a refused telemetry read without hiding the rest of the page (#91 AC4)', async () => {
    const detail = 'The metrics could not be read'
    server.use(
      http.get('/telemetry/metrics', () => HttpResponse.json({ detail }, { status: 500 })),
      http.get('/telemetry/traces', () => HttpResponse.json({ detail }, { status: 500 })),
    )
    render(<App />)

    expect(await screen.findAllByRole('alert')).toHaveLength(2)
    // The two failed reads sit beside the other sections, which are still there.
    expect(screen.getByLabelText('Resume file')).toBeDefined()
    expect(screen.getByText('Acme (acme)')).toBeDefined()
    expect(screen.getByText('Globex: Data engineer')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Run digest now' })).toBeDefined()
  })

  it('shows a refused tick as an error and leaves the rest of the screen usable', async () => {
    const spies = ['error', 'log'].map((name) => vi.spyOn(console, name as 'error'))
    const detail = 'The request is not allowed'
    server.use(http.post('/internal/tick', () => HttpResponse.json({ detail }, { status: 401 })))
    render(<App />)
    await screen.findByText('Acme (acme)')

    fireEvent.click(screen.getByRole('button', { name: 'Run digest now' }))

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.getByText('Acme (acme)')).toBeDefined()
    expect(screen.getByText('Globex: Data engineer')).toBeDefined()
    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })
})

/**
 * The one test that shows the two sibling screens actually wired to each other:
 * a run a control on the page started ends, and the list on the same page shows
 * what that run wrote, with no reload (#79).
 */
describe('a run the page started re-reads the matches list', () => {
  const POLL_MS = 1000

  /** The fake clock `RunDigestNow.test.tsx` installs, so no test waits on a wall clock. */
  const useTestClock = () =>
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
    })

  /** Lets the mocked answers and React's commits run out; `findBy` cannot run on this clock. */
  const flush = () => act(async () => {})

  /** Lets one poll interval pass, and the read it starts come back. */
  const poll = async () => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS)
    })
  }

  /**
   * Answers the run row `running` for its first two reads, so the run takes
   * several polls and one publication is not the same thing as one poll tick.
   */
  const runTakesThreePolls = () => {
    let reads = 0
    return http.get('/runs/:id', () => {
      reads += 1
      return HttpResponse.json(reads < 3 ? runSummary : runSucceeded)
    })
  }

  it('re-reads /matches exactly once when a digest run ends (AC2)', async () => {
    useTestClock()
    let matchesReads = 0
    let watchlistReads = 0
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      runTakesThreePolls(),
      http.get('/watchlist', () => {
        watchlistReads += 1
        return HttpResponse.json(companies)
      }),
      http.get('/matches', () => {
        matchesReads += 1
        return HttpResponse.json(
          matchesReads === 1
            ? matches
            : [
                ...matches.map((match) =>
                  match.id === 1 ? { ...match, reason: 'Rewritten by the run' } : match,
                ),
                { ...matches[0], id: 9, company_name: 'Zeta', title: 'New role' },
              ],
        )
      }),
    )
    render(<App />)
    await flush()
    expect(matchesReads).toBe(1)

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Run digest now' })))
    await poll()
    await poll()
    await poll()
    await flush()

    // The run's own row is terminal, and the list shows the new row and the rewritten reason.
    expect(screen.getByText('Digest finished: 4 matches created.')).toBeDefined()
    expect(screen.getByText('Zeta: New role')).toBeDefined()
    expect(screen.getByText('Rewritten by the run')).toBeDefined()
    expect(screen.queryByText('Loading matches...')).toBeNull()
    // The mount read plus one re-read: one publication for the run, not one per poll.
    expect(matchesReads).toBe(2)
    // No reload happened: /watchlist was still read only by the mount.
    expect(watchlistReads).toBe(1)

    // Far past any poll interval, neither the run row nor the list is read again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 10)
    })
    expect(matchesReads).toBe(2)
  })

  it("re-reads /matches after a run that ends failed, the hook's own terminal set (AC5)", async () => {
    useTestClock()
    let matchesReads = 0
    server.use(
      http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
      // A failed run still walked, scored and wrote rows before it failed.
      http.get('/runs/:id', () =>
        HttpResponse.json({ ...runSucceeded, status: 'failed', error: '2 of 3 companies failed' }),
      ),
      http.get('/matches', () => {
        matchesReads += 1
        return HttpResponse.json(
          matchesReads === 1
            ? matches
            : [
                ...matches,
                { ...matches[0], id: 9, company_name: 'Zeta', title: 'Scored before the failure' },
              ],
        )
      }),
    )
    render(<App />)
    await flush()

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Run digest now' })))
    await poll()
    await flush()

    expect(screen.getByText('Digest failed: 2 of 3 companies failed')).toBeDefined()
    expect(screen.getByText('Zeta: Scored before the failure')).toBeDefined()
    expect(matchesReads).toBe(2)
  })

  it('re-reads /matches exactly once when a re-match the page started ends (AC3)', async () => {
    useTestClock()
    let matchesReads = 0
    server.use(
      http.post('/internal/rematch', () => HttpResponse.json(runSummary, { status: 202 })),
      runTakesThreePolls(),
      http.get('/matches', () => {
        matchesReads += 1
        return HttpResponse.json(
          matchesReads === 1
            ? matches
            : matches.map((match) =>
                match.id === 1 ? { ...match, reason: 'Rewritten by the re-match' } : match,
              ),
        )
      }),
    )
    render(<App />)
    await flush()
    expect(matchesReads).toBe(1)

    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Re-match stored postings' })),
    )
    await poll()
    await poll()
    await poll()
    await flush()

    expect(screen.getByText('Re-match finished: 4 postings scored.')).toBeDefined()
    expect(screen.getByText('Rewritten by the re-match')).toBeDefined()
    expect(matchesReads).toBe(2)
  })
})
