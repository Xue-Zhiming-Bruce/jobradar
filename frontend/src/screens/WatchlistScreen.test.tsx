import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import WatchlistScreen from './WatchlistScreen'

// The suite runs without Vitest globals, so Testing Library's auto-cleanup is off.
afterEach(cleanup)

const LOADING = 'Loading the watchlist...'
const EMPTY = 'No watched companies yet.'

/** An answer the test holds back: the handler returns this promise before it is resolved. */
function withheld() {
  let release!: (response: Response) => void
  const promise = new Promise<Response>((resolve) => (release = resolve))
  return { promise, release }
}

/** The list row a company is rendered in, so a button is clicked on its own row. */
const row = (text: string) => screen.getByText(text).closest('li') as HTMLElement

/** The form's own controls, typed the way they are read back. */
const typedInto = (label: string) => (screen.getByLabelText(label) as HTMLInputElement).value
const selectedSource = () => (screen.getByLabelText('Source') as HTMLSelectElement).value

function fillAndSubmit(name: string, slug: string) {
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: name } })
  fireEvent.change(screen.getByLabelText('Board slug'), { target: { value: slug } })
  fireEvent.click(screen.getByRole('button', { name: 'Add company' }))
}

describe('the watchlist screen', () => {
  it('adds the submitted company, sending the name, the slug and the default source once', async () => {
    const requests: { method: string; path: string; body: unknown }[] = []
    server.use(
      http.post('/watchlist', async ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: await request.json(),
        })
        // The field is absent from this answer, as `WatchedCompany.required` allows.
        return HttpResponse.json(
          { id: 9, name: 'Initech', slug: 'initech', is_active: true },
          { status: 201 },
        )
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fillAndSubmit('Initech', 'initech')

    expect(await screen.findByText('Initech (initech)')).toBeDefined()
    expect(requests).toEqual([
      {
        method: 'POST',
        path: '/watchlist',
        body: { name: 'Initech', slug: 'initech', source: 'greenhouse' },
      },
    ])
    // The answer carried none, so the created row shows the contract's own default.
    expect(within(row('Initech (initech)')).getByText('greenhouse')).toBeDefined()
  })

  it('offers exactly the contract\'s two sources, Greenhouse selected on the first render', async () => {
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    const select = screen.getByLabelText('Source') as HTMLSelectElement

    expect(screen.getAllByLabelText('Source')).toHaveLength(1)
    expect(screen.getByLabelText('Name')).toBeDefined()
    expect(screen.getByLabelText('Board slug')).toBeDefined()
    expect([...select.options].map((option) => option.value)).toEqual(['greenhouse', 'lever'])
    expect(select.value).toBe('greenhouse')
  })

  it('sends lever when Lever is chosen, and shows the source the answer carried', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/watchlist', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(
          { id: 9, name: 'Initech', slug: 'initech', is_active: true, source: 'lever' },
          { status: 201 },
        )
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'lever' } })
    fillAndSubmit('Initech', 'initech')

    expect(await screen.findByText('Initech (initech)')).toBeDefined()
    expect(bodies).toEqual([{ name: 'Initech', slug: 'initech', source: 'lever' }])
    expect(within(row('Initech (initech)')).getByText('lever')).toBeDefined()
  })

  it('shows each row its own stored source, reading a row without one as Greenhouse', async () => {
    server.use(
      http.get('/watchlist', () =>
        HttpResponse.json([
          { id: 1, name: 'Acme', slug: 'acme', is_active: true, source: 'lever' },
          { id: 2, name: 'Globex', slug: 'globex', is_active: true },
        ]),
      ),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    expect(within(row('Acme (acme)')).getByText('lever')).toBeDefined()
    expect(within(row('Globex (globex)')).getByText('greenhouse')).toBeDefined()
  })

  it('keeps the source control in the form, so no request names a source for an existing row', async () => {
    const deleteBodies: string[] = []
    server.use(
      http.delete('/watchlist/:id', async ({ request }) => {
        deleteBodies.push(await request.text())
        return new HttpResponse(null, { status: 204 })
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    expect(within(row('Acme (acme)')).queryByRole('combobox')).toBeNull()
    expect(screen.getAllByRole('combobox')).toHaveLength(1) // the add form's select, nowhere else

    fireEvent.click(within(row('Acme (acme)')).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(screen.queryByText('Acme (acme)')).toBeNull())
    expect(deleteBodies).toEqual(['']) // the one request about an existing row carries no body
  })

  it("removes the clicked row by that row's own id", async () => {
    const paths: string[] = []
    server.use(
      http.delete('/watchlist/:id', ({ request }) => {
        paths.push(new URL(request.url).pathname)
        return new HttpResponse(null, { status: 204 })
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fireEvent.click(within(row('Acme (acme)')).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(screen.queryByText('Acme (acme)')).toBeNull())
    expect(paths).toEqual(['/watchlist/1'])
    expect(screen.queryByText('Globex (globex)')).not.toBeNull()
  })

  it("shows the server's own message and leaves the list and the form alone on a duplicate slug", async () => {
    const detail = "The slug 'acme' is already watched"
    server.use(http.post('/watchlist', () => HttpResponse.json({ detail }, { status: 409 })))
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'lever' } })
    fillAndSubmit('Acme', 'acme')

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(await screen.findByText('Acme (acme)')).toBeDefined()
    expect(screen.queryByText('Globex (globex)')).not.toBeNull()
    // A refusal leaves what was typed where it was, the chosen board included.
    expect(typedInto('Name')).toBe('Acme')
    expect(typedInto('Board slug')).toBe('acme')
    expect(selectedSource()).toBe('lever')
  })

  it('clears the form and returns the select to Greenhouse after a 201', async () => {
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'lever' } })
    fillAndSubmit('Initech', 'initech')

    expect(await screen.findByText('Initech (initech)')).toBeDefined()
    expect(typedInto('Name')).toBe('')
    expect(typedInto('Board slug')).toBe('')
    expect(selectedSource()).toBe('greenhouse')
  })
})

describe("the watchlist screen's three outcomes", () => {
  it('shows the loading text, and neither the empty nor the error text, until the mock answers', async () => {
    const answer = withheld()
    server.use(http.get('/watchlist', () => answer.promise))
    render(<WatchlistScreen />)

    expect(screen.getByText(LOADING)).toBeDefined()
    expect(screen.queryByText(EMPTY)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()

    answer.release(HttpResponse.json([]))
    expect(await screen.findByText(EMPTY)).toBeDefined()
  })

  it('shows the empty text, and neither the loading nor the error text, on an empty list', async () => {
    server.use(http.get('/watchlist', () => HttpResponse.json([])))
    render(<WatchlistScreen />)

    expect(await screen.findByText(EMPTY)).toBeDefined()
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows the error text carrying the detail, and neither of the other two, on a 500', async () => {
    const detail = 'The watchlist could not be read'
    server.use(http.get('/watchlist', () => HttpResponse.json({ detail }, { status: 500 })))
    render(<WatchlistScreen />)

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(screen.queryByText(EMPTY)).toBeNull()
  })
})
