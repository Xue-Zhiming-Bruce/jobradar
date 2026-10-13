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

/** The two rows the edit cases start from: Acme on Greenhouse, Globex on Lever. */
const twoRows = [
  { id: 1, name: 'Acme', slug: 'acme', is_active: true, source: 'greenhouse' },
  { id: 2, name: 'Globex', slug: 'globex', is_active: true, source: 'lever' },
]

/** One recorded write, so a case can assert the path and the body it carried. */
type Recorded = { path: string; body: unknown }

/** Records every `PATCH /watchlist/:id` and answers each with `answer`. */
const answeringPatch = (written: Recorded[], answer: Record<string, unknown>) =>
  http.patch('/watchlist/:id', async ({ request }) => {
    written.push({ path: new URL(request.url).pathname, body: await request.json() })
    return HttpResponse.json(answer)
  })

/** Types a name into a row's own field and submits that row's form. */
function renameRow(text: string, name: string) {
  fireEvent.change(within(row(text)).getByLabelText('New name'), { target: { value: name } })
  fireEvent.click(within(row(text)).getByRole('button', { name: 'Rename' }))
}

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

describe("the watchlist screen's edit controls", () => {
  it('gives a row a New name field holding its stored name, a Rename button and one toggle', async () => {
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    const acme = row('Acme (acme)')

    expect(within(acme).getByRole('textbox')).toBeDefined() // a text box, not a select
    expect((within(acme).getByLabelText('New name') as HTMLInputElement).value).toBe('Acme')
    expect(within(acme).getByRole('button', { name: 'Rename' })).toBeDefined()
    expect(within(acme).getByRole('button', { name: 'Deactivate' })).toBeDefined()
    expect(within(acme).queryByText('Inactive')).toBeNull()
    // The add form keeps the only `Name` field, and the only `combobox`.
    expect(screen.getByLabelText('Name')).toBeDefined()
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
  })

  it('renames one row from the answer, and leaves every other row byte-identical', async () => {
    const written: Recorded[] = []
    let reads = 0
    server.use(
      http.get('/watchlist', () => {
        reads += 1
        return HttpResponse.json(twoRows)
      }),
      // The answer disagrees with the pre-write row on the source, so a row built
      // from local state would show the source this screen already held.
      answeringPatch(written, {
        id: 1,
        name: 'Acme Corporation',
        slug: 'acme',
        is_active: true,
        source: 'lever',
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')
    const globex = row('Globex (globex)').outerHTML

    renameRow('Acme (acme)', 'Acme Corporation')

    expect(await screen.findByText('Acme Corporation (acme)')).toBeDefined()
    expect(written).toEqual([{ path: '/watchlist/1', body: { name: 'Acme Corporation' } }])
    expect(within(row('Acme Corporation (acme)')).getByText('lever')).toBeDefined()
    expect(row('Globex (globex)').outerHTML).toBe(globex)
    expect(reads).toBe(1) // no second read of the list
  })

  it('takes a row out of the poll on Deactivate, from the answer', async () => {
    const written: Recorded[] = []
    server.use(
      http.get('/watchlist', () => HttpResponse.json(twoRows)),
      answeringPatch(written, {
        id: 1,
        name: 'Acme',
        slug: 'acme',
        is_active: false,
        source: 'greenhouse',
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fireEvent.click(within(row('Acme (acme)')).getByRole('button', { name: 'Deactivate' }))

    expect(await within(row('Acme (acme)')).findByText('Inactive')).toBeDefined()
    expect(written).toEqual([{ path: '/watchlist/1', body: { is_active: false } }])
    expect(within(row('Acme (acme)')).getByRole('button', { name: 'Activate' })).toBeDefined()
    expect(within(row('Globex (globex)')).queryByText('Inactive')).toBeNull()
    expect(within(row('Globex (globex)')).getByRole('button', { name: 'Deactivate' })).toBeDefined()
  })

  it('puts a row the list read as inactive back in the poll from the same control', async () => {
    const written: Recorded[] = []
    server.use(
      http.get('/watchlist', () => HttpResponse.json([{ ...twoRows[0], is_active: false }, twoRows[1]])),
      answeringPatch(written, {
        id: 1,
        name: 'Acme',
        slug: 'acme',
        is_active: true,
        source: 'greenhouse',
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    expect(within(row('Acme (acme)')).getByText('Inactive')).toBeDefined()
    // A deactivated company is still renamed from its row.
    expect((within(row('Acme (acme)')).getByLabelText('New name') as HTMLInputElement).value).toBe(
      'Acme',
    )

    fireEvent.click(within(row('Acme (acme)')).getByRole('button', { name: 'Activate' }))

    await waitFor(() => expect(within(row('Acme (acme)')).queryByText('Inactive')).toBeNull())
    expect(written).toEqual([{ path: '/watchlist/1', body: { is_active: true } }])
    expect(within(row('Acme (acme)')).queryByRole('button', { name: 'Activate' })).toBeNull()
    expect(within(row('Acme (acme)')).getByRole('button', { name: 'Deactivate' })).toBeDefined()
  })

  it("shows the route's detail on a refused rename, and leaves the row and the typed name alone", async () => {
    const detail = 'There is no watched company with id 1'
    const written: Recorded[] = []
    server.use(
      http.get('/watchlist', () => HttpResponse.json(twoRows)),
      http.patch('/watchlist/:id', async ({ request }) => {
        written.push({ path: new URL(request.url).pathname, body: await request.json() })
        return HttpResponse.json({ detail }, { status: 404 })
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    renameRow('Acme (acme)', 'Acme Corporation')

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByText('Acme Corporation (acme)')).toBeNull()
    expect(within(row('Acme (acme)')).getByText('greenhouse')).toBeDefined()
    expect((within(row('Acme (acme)')).getByLabelText('New name') as HTMLInputElement).value).toBe(
      'Acme Corporation',
    )
    expect(written).toEqual([{ path: '/watchlist/1', body: { name: 'Acme Corporation' } }])
  })

  it('shows the blank name the route refused, sending that one field and changing nothing', async () => {
    const detail = 'name must not be blank'
    const bodies: unknown[] = []
    server.use(
      http.get('/watchlist', () => HttpResponse.json(twoRows)),
      http.patch('/watchlist/:id', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ detail }, { status: 422 })
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    renameRow('Acme (acme)', '')

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.getByText('Acme (acme)')).toBeDefined()
    expect((within(row('Acme (acme)')).getByLabelText('New name') as HTMLInputElement).value).toBe('')
    expect(bodies).toEqual([{ name: '' }])
  })

  it('leaves the row and its control as they were on a refused toggle', async () => {
    const detail = 'The company could not be written'
    const written: Recorded[] = []
    server.use(
      http.get('/watchlist', () => HttpResponse.json(twoRows)),
      http.patch('/watchlist/:id', async ({ request }) => {
        written.push({ path: new URL(request.url).pathname, body: await request.json() })
        return HttpResponse.json({ detail }, { status: 500 })
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    fireEvent.click(within(row('Acme (acme)')).getByRole('button', { name: 'Deactivate' }))

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(within(row('Acme (acme)')).queryByText('Inactive')).toBeNull()
    expect(within(row('Acme (acme)')).getByRole('button', { name: 'Deactivate' })).toBeDefined()
    expect(written).toEqual([{ path: '/watchlist/1', body: { is_active: false } }])
  })

  it('never puts a second field on the wire, and never a source or a slug', async () => {
    const bodies: Record<string, unknown>[] = []
    // The route stores what it was sent and answers it back, so the mock keeps one row.
    let stored: Record<string, unknown> = { ...twoRows[0] }
    server.use(
      http.get('/watchlist', () => HttpResponse.json([twoRows[0]])),
      http.patch('/watchlist/:id', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push(body)
        stored = { ...stored, ...body }
        return HttpResponse.json(stored)
      }),
    )
    render(<WatchlistScreen />)
    await screen.findByText('Acme (acme)')

    renameRow('Acme (acme)', 'Acme Ltd')
    await screen.findByText('Acme Ltd (acme)')
    fireEvent.click(within(row('Acme Ltd (acme)')).getByRole('button', { name: 'Deactivate' }))
    await within(row('Acme Ltd (acme)')).findByText('Inactive')
    fireEvent.click(within(row('Acme Ltd (acme)')).getByRole('button', { name: 'Activate' }))
    await waitFor(() => expect(within(row('Acme Ltd (acme)')).queryByText('Inactive')).toBeNull())

    expect(bodies).toEqual([{ name: 'Acme Ltd' }, { is_active: false }, { is_active: true }])
    for (const body of bodies) {
      expect(Object.keys(body)).toHaveLength(1)
      expect(Object.keys(body)).not.toContain('source')
      expect(Object.keys(body)).not.toContain('slug')
    }
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
