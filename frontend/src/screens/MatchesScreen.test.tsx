import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { matches } from '../mocks/fixtures'
import MatchesScreen from './MatchesScreen'

// The suite runs without Vitest globals, so Testing Library's auto-cleanup is off.
afterEach(cleanup)

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
