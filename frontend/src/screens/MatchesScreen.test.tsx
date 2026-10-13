import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { matches, runSummary, runSucceeded } from '../mocks/fixtures'
import MatchesScreen from './MatchesScreen'
import RunDigestNow from './RunDigestNow'

// The suite runs without Vitest globals, so Testing Library's auto-cleanup is off.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const LOADING = 'Loading matches...'
const EMPTY = 'No matches yet.'

const newList = () => screen.getByRole('list', { name: 'New matches' })
const savedList = () => screen.getByRole('list', { name: 'Saved matches' })
const dismissedList = () => screen.getByRole('list', { name: 'Dismissed matches' })

/** The row a match is rendered in, so its buttons and its state are looked up on it. */
const rowOf = async (label: string) =>
  (await screen.findByText(label)).closest('li') as HTMLElement

/** An answer the test holds back: the handler returns this promise before it is resolved. */
function withheld() {
  let release!: (response: Response) => void
  const promise = new Promise<Response>((resolve) => (release = resolve))
  return { promise, release }
}

/** Answers a verdict with the row that was asked about, in the state the body carried. */
const echoVerdict = (requests: { method: string; path: string; body: unknown }[]) =>
  http.patch('/matches/:id', async ({ request, params }) => {
    const body = await request.json()
    requests.push({ method: request.method, path: new URL(request.url).pathname, body })
    const row = matches.find((match) => match.id === Number(params.id))
    return HttpResponse.json({ ...row, state: (body as { state: string }).state })
  })

describe('the matches screen', () => {
  it('shows every field the contract carries, on the row that carries it', async () => {
    render(<MatchesScreen />)

    const acme = await rowOf('Acme: Backend engineer')
    expect(within(acme).getByText('Score: 88')).toBeDefined()
    expect(within(acme).getByText('Python and FastAPI line up with this role')).toBeDefined()
    expect(within(acme).getByText('No Kubernetes')).toBeDefined()
    expect(within(acme).getByText('No Go')).toBeDefined()
    expect(within(acme).getByText('State: new')).toBeDefined()

    expect(within(await rowOf('Globex: Data engineer')).getByText('State: saved')).toBeDefined()
    expect(within(await rowOf('Initech: Platform engineer')).getByText('State: dismissed')).toBeDefined()
  })

  it('groups the rows by their own state, and the new list holds only the new ones', async () => {
    render(<MatchesScreen />)
    await screen.findByText('Acme: Backend engineer')

    expect(within(newList()).getByText('Umbrella: Senior staff engineer')).toBeDefined()
    expect(within(newList()).queryByText('Globex: Data engineer')).toBeNull()
    expect(within(savedList()).getByText('Globex: Data engineer')).toBeDefined()
    expect(within(dismissedList()).getByText('Initech: Platform engineer')).toBeDefined()
  })

  it('shows a score-0 prefiltered row with no gaps like any other row', async () => {
    render(<MatchesScreen />)

    const umbrella = await rowOf('Umbrella: Senior staff engineer')
    expect(within(umbrella).getByText('Score: 0')).toBeDefined()
    expect(within(umbrella).getByText('Prefiltered out: senior title')).toBeDefined()
    expect(within(umbrella).queryAllByRole('listitem')).toHaveLength(0)
  })

  it('records saved once for the clicked row, and shows the state the mock answered', async () => {
    const requests: { method: string; path: string; body: unknown }[] = []
    server.use(echoVerdict(requests))
    render(<MatchesScreen />)

    fireEvent.click(within(await rowOf('Acme: Backend engineer')).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(within(savedList()).getByText('Acme: Backend engineer')).toBeDefined())
    expect(within(newList()).queryByText('Acme: Backend engineer')).toBeNull()
    expect(within(await rowOf('Acme: Backend engineer')).getByText('State: saved')).toBeDefined()
    expect(requests).toEqual([{ method: 'PATCH', path: '/matches/1', body: { state: 'saved' } }])
  })

  it("records dismissed once, with the clicked row's own id", async () => {
    const requests: { method: string; path: string; body: unknown }[] = []
    server.use(echoVerdict(requests))
    render(<MatchesScreen />)

    const umbrella = await rowOf('Umbrella: Senior staff engineer')
    fireEvent.click(within(umbrella).getByRole('button', { name: 'Dismiss' }))

    await waitFor(() =>
      expect(within(dismissedList()).getByText('Umbrella: Senior staff engineer')).toBeDefined(),
    )
    expect(requests).toEqual([{ method: 'PATCH', path: '/matches/4', body: { state: 'dismissed' } }])
  })

  it('keeps a dismissed match out of the new list on the verdict, and on a later read', async () => {
    const requests: { method: string; path: string; body: unknown }[] = []
    let reads = 0
    server.use(
      echoVerdict(requests),
      http.get('/matches', () => {
        reads += 1
        return HttpResponse.json(matches)
      }),
    )
    const firstRender = render(<MatchesScreen />)

    const umbrella = await rowOf('Umbrella: Senior staff engineer')
    fireEvent.click(within(umbrella).getByRole('button', { name: 'Dismiss' }))

    await waitFor(() =>
      expect(within(dismissedList()).getByText('Umbrella: Senior staff engineer')).toBeDefined(),
    )
    // The move came from the verdict's own answer, not from a read of the list.
    expect(reads).toBe(1)
    expect(requests).toHaveLength(1)
    expect(within(newList()).queryByText('Umbrella: Senior staff engineer')).toBeNull()

    // A later read of the same list, with the mock answering that id dismissed.
    firstRender.unmount()
    server.use(
      http.get('/matches', () =>
        HttpResponse.json(
          matches.map((match) => (match.id === 4 ? { ...match, state: 'dismissed' } : match)),
        ),
      ),
    )
    render(<MatchesScreen />)
    await screen.findByText('Acme: Backend engineer')

    expect(within(newList()).queryByText('Umbrella: Senior staff engineer')).toBeNull()
    expect(within(dismissedList()).getByText('Umbrella: Senior staff engineer')).toBeDefined()
  })

  it("shows the server's message on a refused verdict and leaves the displayed state", async () => {
    const detail = 'There is no match with id 1'
    server.use(http.patch('/matches/:id', () => HttpResponse.json({ detail }, { status: 404 })))
    render(<MatchesScreen />)

    fireEvent.click(within(await rowOf('Acme: Backend engineer')).getByRole('button', { name: 'Save' }))

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()
    expect(within(await rowOf('Acme: Backend engineer')).getByText('State: new')).toBeDefined()
  })
})

describe("the matches screen's three outcomes", () => {
  it('shows the loading text, and neither the empty nor the error text, until the mock answers', async () => {
    const answer = withheld()
    server.use(http.get('/matches', () => answer.promise))
    render(<MatchesScreen />)

    expect(screen.getByText(LOADING)).toBeDefined()
    expect(screen.queryByText(EMPTY)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()

    answer.release(HttpResponse.json([]))
    expect(await screen.findByText(EMPTY)).toBeDefined()
  })

  it('shows the empty text, and neither the loading nor the error text, on an empty list', async () => {
    server.use(http.get('/matches', () => HttpResponse.json([])))
    render(<MatchesScreen />)

    expect(await screen.findByText(EMPTY)).toBeDefined()
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows the error text carrying the detail, and neither of the other two, on a 500', async () => {
    const detail = 'The matches could not be read'
    server.use(http.get('/matches', () => HttpResponse.json({ detail }, { status: 500 })))
    render(<MatchesScreen />)

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(screen.queryByText(EMPTY)).toBeNull()
  })
})

/**
 * A run the page started ends, and the list it rewrote is read again (#79). The
 * two sibling screens are rendered together because the signal crosses them by
 * module: only a real run control can publish it, and only the screen subscribes.
 */
describe('the matches list after a run the page started', () => {
  const POLL_MS = 1000

  /** The fake clock `RunDigestNow.test.tsx` installs, so no test waits on a wall clock. */
  const useTestClock = () =>
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
    })

  /**
   * Lets the mocked answers and React's commits run out. `findBy`/`waitFor` cannot
   * be used while this clock is installed, so a test flushes and then looks.
   */
  const flush = () => act(async () => {})

  /** Lets one poll interval pass, and the read it starts come back. */
  const poll = async () => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS)
    })
  }

  /** A row already on screen, looked up without `findBy` for the same clock reason. */
  const rowNow = (label: string) => screen.getByText(label).closest('li') as HTMLElement

  /** Renders the shipped digest control and the matches list, the two siblings the signal joins. */
  const renderPage = () =>
    render(
      <>
        <RunDigestNow />
        <MatchesScreen />
      </>,
    )

  /** The press and the run row a digest run needs, so it ends terminal on the first poll. */
  const digestRunHandlers = () => [
    http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),
    http.get('/runs/:id', () => HttpResponse.json(runSucceeded)),
  ]

  /** Presses `Run digest now`, lets its terminal read answer, and the publish reach the list. */
  const endDigestRun = async () => {
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Run digest now' })))
    await poll()
    await flush()
  }

  it('keeps the last rows on screen while the re-read is in flight, never the loading text (AC6)', async () => {
    useTestClock()
    const answer = withheld()
    const rewritten = matches.map((match) =>
      match.id === 1 ? { ...match, reason: 'Rewritten by the run' } : match,
    )
    let reads = 0
    server.use(
      ...digestRunHandlers(),
      http.get('/matches', () => {
        reads += 1
        return reads === 1 ? HttpResponse.json(matches) : answer.promise
      }),
    )
    renderPage()
    await flush()
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()

    await endDigestRun()

    // The re-read is in flight: the mount read's rows are still there, no loading text.
    expect(reads).toBe(2)
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()

    answer.release(HttpResponse.json(rewritten))
    await flush()
    // When the answer lands the rows are the answer's -- a replace, not a merge.
    expect(within(newList()).getByText('Rewritten by the run')).toBeDefined()
    expect(screen.queryByText(LOADING)).toBeNull()
  })

  it('clears a previous read error when the re-read succeeds, leaving no alert over the rows (AC7)', async () => {
    useTestClock()
    const detail = 'The matches could not be read'
    let reads = 0
    server.use(
      ...digestRunHandlers(),
      http.get('/matches', () => {
        reads += 1
        return reads === 1
          ? HttpResponse.json({ detail }, { status: 500 })
          : HttpResponse.json(matches)
      }),
    )
    renderPage()
    await flush()
    expect(screen.getByRole('alert').textContent).toBe(detail)

    await endDigestRun()

    expect(reads).toBe(2)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()
  })

  it('shows a failed re-read on the existing error branch, in place of the rows (AC7)', async () => {
    useTestClock()
    const detail = 'The re-read was refused'
    let reads = 0
    server.use(
      ...digestRunHandlers(),
      http.get('/matches', () => {
        reads += 1
        return reads === 1
          ? HttpResponse.json(matches)
          : HttpResponse.json({ detail }, { status: 500 })
      }),
    )
    renderPage()
    await flush()
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()

    await endDigestRun()

    expect(reads).toBe(2)
    expect(screen.getByRole('alert').textContent).toBe(detail)
    expect(screen.queryByText('Acme: Backend engineer')).toBeNull()
  })

  it('replaces the rows with the answer, and a verdict recorded on the screen survives it (AC8)', async () => {
    useTestClock()
    let reads = 0
    server.use(
      ...digestRunHandlers(),
      http.get('/matches', () => {
        reads += 1
        return HttpResponse.json(
          reads === 1
            ? matches
            : matches.map((match) => (match.id === 1 ? { ...match, state: 'saved' } : match)),
        )
      }),
    )
    renderPage()
    await flush()

    await act(async () =>
      fireEvent.click(within(rowNow('Acme: Backend engineer')).getByRole('button', { name: 'Save' })),
    )
    expect(within(savedList()).getByText('Acme: Backend engineer')).toBeDefined()

    await endDigestRun()

    expect(reads).toBe(2)
    // The verdict was written to the same server the re-read reads, so the group holds.
    expect(within(savedList()).getByText('Acme: Backend engineer')).toBeDefined()
    expect(within(newList()).queryByText('Acme: Backend engineer')).toBeNull()
  })

  it('reads nothing again when a press is refused with 401 (AC4)', async () => {
    useTestClock()
    const detail = 'The request is not allowed'
    let reads = 0
    server.use(
      http.post('/internal/tick', () => HttpResponse.json({ detail }, { status: 401 })),
      http.get('/runs/:id', () => HttpResponse.json(runSucceeded)),
      http.get('/matches', () => {
        reads += 1
        return HttpResponse.json(matches)
      }),
    )
    renderPage()
    await flush()

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Run digest now' })))

    expect(screen.getByRole('alert').textContent).toBe(detail)
    // No run row, so no terminal status and no publication: the mount read stands alone.
    expect(reads).toBe(1)
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()
  })

  it('reads nothing again when a press is answered 409 (AC4)', async () => {
    useTestClock()
    const detail = 'A run is already in progress in this process'
    let reads = 0
    server.use(
      http.post('/internal/tick', () => HttpResponse.json({ detail }, { status: 409 })),
      http.get('/matches', () => {
        reads += 1
        return HttpResponse.json(matches)
      }),
    )
    renderPage()
    await flush()

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Run digest now' })))
    await poll()

    expect(screen.getByRole('alert').textContent).toBe(detail)
    expect(reads).toBe(1)
    expect(within(newList()).getByText('Acme: Backend engineer')).toBeDefined()
  })
})
