import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { runSummary, runSucceeded } from '../mocks/fixtures'
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
        return HttpResponse.json({ ...runSummary, status: 'failed', error: 'Greenhouse timed out' })
      }),
    )
    render(<RunDigestNow />)

    await runNow()
    await poll()

    expect(screen.getByText('Digest failed: Greenhouse timed out')).toBeDefined()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 10)
    })
    expect(reads).toEqual(['/runs/1'])
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
