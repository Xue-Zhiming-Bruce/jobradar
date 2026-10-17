import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import {
  ApiError,
  addCompany,
  getRun,
  listMatches,
  listRouteMetrics,
  listTraces,
  listWatchlist,
  rematch,
  removeCompany,
  setMatchState,
  startTick,
  updateCompany,
  uploadResume,
} from './client'

const refused = 'The slug is already watched'

/** The one trace id `#90` writes the digest journey under, and the two spans that share it. */
const traceId = '4bf92f3577b34da6a3ce929d0e0e4736'
const tickTrace = {
  trace_id: traceId,
  spans: [
    {
      id: 1,
      name: 'http',
      route: '/internal/tick',
      method: 'POST',
      status_code: 202,
      duration_ms: 4,
      started_at: '2026-05-01T09:00:00+00:00',
    },
    {
      id: 2,
      name: 'run',
      route: null,
      method: null,
      status_code: null,
      duration_ms: 3012,
      started_at: '2026-05-01T09:00:00+00:00',
    },
  ],
}

/** One `MatchSummary` as `GET /matches` sends it: all nine fields, in the wire's names. */
const matchRow = {
  id: 7,
  company_name: 'Acme',
  title: 'Backend engineer',
  url: 'https://boards.greenhouse.io/acme/jobs/7',
  score: 88,
  reason: 'Python and FastAPI line up with this role',
  gaps: ['No Kubernetes', 'No Go'],
  state: 'new',
  created_at: '2026-05-01T09:00:00+00:00',
}

const rejecting = (status: number, message = refused) => () =>
  HttpResponse.json({ detail: message }, { status })

/** Runs a call that must fail, and hands back the error it rejected with. */
async function rejection(call: () => Promise<unknown>): Promise<ApiError> {
  const failure = await call().catch((error: unknown) => error)
  expect(failure).toBeInstanceOf(ApiError)
  return failure as ApiError
}

describe('the wire is decoded into the UI', () => {
  it('listWatchlist turns is_active into isActive, and changes nothing else', async () => {
    server.use(
      http.get('/watchlist', () =>
        HttpResponse.json([
          { id: 1, name: 'Acme', slug: 'acme', is_active: true, source: 'greenhouse' },
        ]),
      ),
    )

    expect(await listWatchlist()).toEqual([
      { id: 1, name: 'Acme', slug: 'acme', isActive: true, source: 'greenhouse' },
    ])
  })

  it('listWatchlist reads a row with no source as the contract\'s own default', async () => {
    // `source` is not in `WatchedCompany.required`: a row without it is a row the
    // contract allows, and its declared default is Greenhouse.
    server.use(
      http.get('/watchlist', () =>
        HttpResponse.json([{ id: 1, name: 'Acme', slug: 'acme', is_active: true }]),
      ),
    )

    expect(await listWatchlist()).toEqual([
      { id: 1, name: 'Acme', slug: 'acme', isActive: true, source: 'greenhouse' },
    ])
  })

  it('listWatchlist keeps a row\'s own lever source as the wire spelled it', async () => {
    server.use(
      http.get('/watchlist', () =>
        HttpResponse.json([
          { id: 1, name: 'Acme', slug: 'acme', is_active: true, source: 'lever' },
        ]),
      ),
    )

    expect(await listWatchlist()).toEqual([
      { id: 1, name: 'Acme', slug: 'acme', isActive: true, source: 'lever' },
    ])
  })

  it('getRun turns matches_created into matchesCreated and decodes the company outcomes', async () => {
    server.use(
      http.get('/runs/7', () =>
        HttpResponse.json({
          id: 7,
          status: 'failed',
          started_at: '2026-05-01T10:00:00+00:00',
          finished_at: '2026-05-01T10:00:04+00:00',
          error: "1 of 2 companies failed: 'beta'",
          postings_seen: 12,
          matches_created: 4,
          matches_emailed: 3,
          companies_not_fetched: 3,
          postings_not_rematched: 2,
          company_outcomes: [
            { company_id: 1, slug: 'acme', outcome: 'fetched', error: null },
            {
              company_id: 2,
              slug: 'beta',
              outcome: 'failed',
              error: "The board for company 'beta' could not be fetched: HTTP 503",
            },
          ],
        }),
      ),
    )

    expect(await getRun(7)).toEqual({
      id: 7,
      status: 'failed',
      started_at: '2026-05-01T10:00:00+00:00',
      finished_at: '2026-05-01T10:00:04+00:00',
      error: "1 of 2 companies failed: 'beta'",
      postingsSeen: 12,
      matchesCreated: 4,
      matchesEmailed: 3,
      companiesNotFetched: 3,
      postingsNotRematched: 2,
      companyOutcomes: [
        { companyId: 1, slug: 'acme', outcome: 'fetched', error: null },
        {
          companyId: 2,
          slug: 'beta',
          outcome: 'failed',
          error: "The board for company 'beta' could not be fetched: HTTP 503",
        },
      ],
    })
  })

  it('uploadResume posts a multipart form and decodes the answer', async () => {
    const contentTypes: (string | null)[] = []
    server.use(
      http.post('/resume', ({ request }) => {
        // Reading the mocked body is not what this asserts: the part is named in
        // `uploadResume`, and the wire body is the browser's to build.
        contentTypes.push(request.headers.get('content-type'))
        return HttpResponse.json(
          { id: 3, filename: 'resume.txt', content_type: 'text/plain', is_active: true },
          { status: 201 },
        )
      }),
    )

    const uploaded = await uploadResume(new File(['a resume'], 'resume.txt', { type: 'text/plain' }))

    expect(contentTypes[0]?.startsWith('multipart/form-data; boundary=')).toBe(true)
    expect(uploaded).toEqual({
      id: 3,
      filename: 'resume.txt',
      contentType: 'text/plain',
      isActive: true,
    })
  })

  it('listRouteMetrics turns p95_ms into p95Ms and changes nothing else', async () => {
    server.use(
      http.get('/telemetry/metrics', () =>
        HttpResponse.json([
          { route: '/matches', count: 12, errors: 1, p95_ms: 41 },
          { route: '<unmatched>', count: 3, errors: 3, p95_ms: 7 },
        ]),
      ),
    )

    expect(await listRouteMetrics()).toEqual([
      { route: '/matches', count: 12, errors: 1, p95Ms: 41 },
      { route: '<unmatched>', count: 3, errors: 3, p95Ms: 7 },
    ])
  })

  it('listTraces groups the spans under their trace id, route-less span included', async () => {
    server.use(http.get('/telemetry/traces', () => HttpResponse.json([tickTrace])))

    expect(await listTraces()).toEqual([
      {
        traceId: traceId,
        spans: [
          {
            id: 1,
            name: 'http',
            route: '/internal/tick',
            statusCode: 202,
            durationMs: 4,
            startedAt: '2026-05-01T09:00:00+00:00',
          },
          {
            id: 2,
            name: 'run',
            route: null,
            statusCode: null,
            durationMs: 3012,
            startedAt: '2026-05-01T09:00:00+00:00',
          },
        ],
      },
    ])
  })

  it('listMatches keeps createdAt an ISO string and renames only company_name', async () => {
    server.use(http.get('/matches', () => HttpResponse.json([matchRow])))

    expect(await listMatches()).toEqual([
      {
        id: 7,
        companyName: 'Acme',
        title: 'Backend engineer',
        url: 'https://boards.greenhouse.io/acme/jobs/7',
        score: 88,
        reason: 'Python and FastAPI line up with this role',
        gaps: ['No Kubernetes', 'No Go'],
        state: 'new',
        createdAt: '2026-05-01T09:00:00+00:00',
      },
    ])
  })

  it('setMatchState sends the verdict to PATCH /matches/7 and decodes the answer', async () => {
    const requests: { method: string; path: string; body: unknown }[] = []
    server.use(
      http.patch('/matches/:id', async ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: await request.json(),
        })
        return HttpResponse.json({ ...matchRow, state: 'saved' })
      }),
    )

    expect(await setMatchState(7, 'saved')).toEqual({
      id: 7,
      companyName: 'Acme',
      title: 'Backend engineer',
      url: 'https://boards.greenhouse.io/acme/jobs/7',
      score: 88,
      reason: 'Python and FastAPI line up with this role',
      gaps: ['No Kubernetes', 'No Go'],
      state: 'saved',
      createdAt: '2026-05-01T09:00:00+00:00',
    })
    expect(requests).toEqual([{ method: 'PATCH', path: '/matches/7', body: { state: 'saved' } }])
  })

  it('updateCompany sends the rename to PATCH /watchlist/7 and decodes the answered row', async () => {
    const requests: { method: string; path: string; body: unknown }[] = []
    server.use(
      http.patch('/watchlist/:id', async ({ request }) => {
        requests.push({
          method: request.method,
          path: new URL(request.url).pathname,
          body: await request.json(),
        })
        return HttpResponse.json({
          id: 7,
          name: 'Acme Corporation',
          slug: 'acme',
          is_active: true,
          source: 'lever',
        })
      }),
    )

    expect(await updateCompany(7, { name: 'Acme Corporation' })).toEqual({
      id: 7,
      name: 'Acme Corporation',
      slug: 'acme',
      isActive: true,
      source: 'lever',
    })
    expect(requests).toEqual([
      { method: 'PATCH', path: '/watchlist/7', body: { name: 'Acme Corporation' } },
    ])
  })

  it('updateCompany carries the toggle alone, and decodes is_active into isActive', async () => {
    const bodies: unknown[] = []
    server.use(
      http.patch('/watchlist/:id', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          id: 7,
          name: 'Acme',
          slug: 'acme',
          is_active: false,
          source: 'greenhouse',
        })
      }),
    )

    expect(await updateCompany(7, { is_active: false })).toEqual({
      id: 7,
      name: 'Acme',
      slug: 'acme',
      isActive: false,
      source: 'greenhouse',
    })
    expect(bodies).toEqual([{ is_active: false }])
    // One key exactly: the call adds nothing of its own to the body it was given.
    expect(Object.keys(bodies[0] as Record<string, unknown>)).toEqual(['is_active'])
  })

  it('startTick answers with the run it started, and sends no secret', async () => {
    const requests: Request[] = []
    server.use(
      http.post('/internal/tick', ({ request }) => {
        requests.push(request)
        return HttpResponse.json(
          {
            id: 1,
            status: 'running',
            started_at: '2026-05-01T10:00:00+00:00',
            finished_at: null,
            error: null,
            postings_seen: 0,
            matches_created: 0,
            matches_emailed: null,
            companies_not_fetched: null,
            postings_not_rematched: null,
            company_outcomes: [],
          },
          { status: 202 },
        )
      }),
    )

    expect(await startTick()).toEqual({
      id: 1,
      status: 'running',
      started_at: '2026-05-01T10:00:00+00:00',
      finished_at: null,
      error: null,
      postingsSeen: 0,
      matchesCreated: 0,
      matchesEmailed: null,
      companiesNotFetched: null,
      postingsNotRematched: null,
      companyOutcomes: [],
    })
    expect(requests).toHaveLength(1)
    expect(requests[0].headers.has('x-tick-secret')).toBe(false)
  })

  it('rematch posts to /internal/rematch, sends no secret, and decodes the new field', async () => {
    const requests: Request[] = []
    server.use(
      http.post('/internal/rematch', ({ request }) => {
        requests.push(request)
        return HttpResponse.json(
          {
            id: 9,
            status: 'running',
            started_at: '2026-05-01T10:00:00+00:00',
            finished_at: null,
            error: null,
            postings_seen: 0,
            matches_created: 0,
            matches_emailed: null,
            companies_not_fetched: null,
            postings_not_rematched: 7,
            company_outcomes: [],
          },
          { status: 202 },
        )
      }),
    )

    expect(await rematch()).toEqual({
      id: 9,
      status: 'running',
      started_at: '2026-05-01T10:00:00+00:00',
      finished_at: null,
      error: null,
      postingsSeen: 0,
      matchesCreated: 0,
      matchesEmailed: null,
      companiesNotFetched: null,
      postingsNotRematched: 7,
      companyOutcomes: [],
    })
    expect(requests).toHaveLength(1)
    expect(new URL(requests[0].url).pathname).toBe('/internal/rematch')
    expect(requests[0].headers.has('x-tick-secret')).toBe(false)
  })
})

describe('a 204 is empty and a non-2xx is an error', () => {
  it('removeCompany resolves on a 204 that carries no body', async () => {
    expect(await removeCompany(1)).toBeUndefined()
  })

  it('removeCompany rejects on a 404 with the server detail', async () => {
    const message = 'There is no watched company with id 9'
    server.use(http.delete('/watchlist/9', rejecting(404, message)))

    const failure = await rejection(() => removeCompany(9))

    expect(failure.message).toBe(message)
    expect(failure.status).toBe(404)
  })

  it('setMatchState rejects on a 404 with the server detail, and logs nothing', async () => {
    const spies = ['log', 'info', 'warn', 'error'].map((name) => vi.spyOn(console, name as 'log'))
    const message = 'There is no match with id 7'
    server.use(http.patch('/matches/:id', rejecting(404, message)))

    const failure = await rejection(() => setMatchState(7, 'saved'))

    expect(failure.message).toBe(message)
    expect(failure.status).toBe(404)
    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })

  it.each([
    ['a 409 on the duplicate slug', 409, 'The slug \'acme\' is already watched'],
    ['a 415 on the declared type', 415, 'The declared type is not a resume'],
    ['a 413 on an oversized upload', 413, 'Resume is 9000000 bytes: the upload limit is 5000000 bytes'],
  ])('addCompany and uploadResume surface %s verbatim', async (_case, status, message) => {
    server.use(
      http.post('/watchlist', rejecting(status, message)),
      http.post('/resume', rejecting(status, message)),
    )

    expect((await rejection(() => addCompany({ name: 'Acme', slug: 'acme' }))).message).toBe(message)
    expect(
      (await rejection(() => uploadResume(new File(['too big'], 'resume.pdf')))).message,
    ).toBe(message)
  })

  it.each([
    ['a 404 on an id no row has', 404, 'There is no watched company with id 9'],
    ['a 422 on a blank name', 422, 'name must not be blank'],
  ])('updateCompany surfaces %s verbatim as a refusal', async (_case, status, message) => {
    server.use(http.patch('/watchlist/:id', rejecting(status, message)))

    const failure = await rejection(() => updateCompany(9, { name: 'Acme' }))

    expect(failure.message).toBe(message)
    expect(failure.status).toBe(status)
  })

  it.each([
    ['listWatchlist', (): Promise<unknown> => listWatchlist()],
    ['addCompany', (): Promise<unknown> => addCompany({ name: 'Acme', slug: 'acme' })],
    ['removeCompany', (): Promise<unknown> => removeCompany(9)],
    ['updateCompany', (): Promise<unknown> => updateCompany(9, { name: 'Acme' })],
    ['uploadResume', (): Promise<unknown> => uploadResume(new File(['x'], 'resume.txt'))],
    ['listMatches', (): Promise<unknown> => listMatches()],
    ['setMatchState', (): Promise<unknown> => setMatchState(9, 'saved')],
    ['startTick', (): Promise<unknown> => startTick()],
    ['rematch', (): Promise<unknown> => rematch()],
    ['getRun', (): Promise<unknown> => getRun(9)],
    ['listRouteMetrics', (): Promise<unknown> => listRouteMetrics()],
    ['listTraces', (): Promise<unknown> => listTraces()],
  ])('%s rejects rather than returning the error body as data', async (_name, call) => {
    server.use(
      http.all('/watchlist', rejecting(500)),
      http.all('/watchlist/:id', rejecting(500)),
      http.all('/resume', rejecting(500)),
      http.all('/matches', rejecting(500)),
      http.all('/matches/:id', rejecting(500)),
      http.all('/internal/tick', rejecting(500)),
      http.all('/internal/rematch', rejecting(500)),
      http.all('/runs/:id', rejecting(500)),
      http.all('/telemetry/metrics', rejecting(500)),
      http.all('/telemetry/traces', rejecting(500)),
    )

    expect((await rejection(call)).message).toBe(refused)
  })

  it('never logs a response body or a request url, even on a failure', async () => {
    const spies = ['log', 'info', 'warn', 'error'].map((name) => vi.spyOn(console, name as 'log'))
    server.use(http.get('/watchlist', rejecting(500)))

    await rejection(() => listWatchlist())

    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })
})
