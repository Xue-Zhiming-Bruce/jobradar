import { http, HttpResponse } from 'msw'
import { companies, matches, resumeUploaded, runSummary, runSucceeded } from './fixtures'

/** The mock backend: one handler per method-and-path pair in `backend/openapi.json`. */
export const handlers = [
  http.get('/watchlist', () => HttpResponse.json(companies)),

  // The submitted fields echo back over the placeholder row, so `npm run dev`
  // shows the company that was just added, on the board it was added to.
  http.post('/watchlist', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    return HttpResponse.json({ ...companies[0], ...body }, { status: 201 })
  }),

  http.delete('/watchlist/:id', () => new HttpResponse(null, { status: 204 })),

  // The fields the body named echo back over the row it was sent to, so a rename
  // and a toggle both show their write without the mock keeping state.
  http.patch('/watchlist/:id', async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>
    const id = Number(params.id)
    const row = companies.find((company) => company.id === id) ?? companies[0]
    return HttpResponse.json({ ...row, ...body, id })
  }),

  http.post('/resume', () => HttpResponse.json(resumeUploaded, { status: 201 })),

  http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),

  http.get('/runs/:id', () => HttpResponse.json(runSucceeded)),

  http.get('/matches', () => HttpResponse.json(matches)),

  // The verdict echoes back, so a test can see the round trip without the mock
  // keeping state: the answer is the row it found, in the state it was sent.
  http.patch('/matches/:id', async ({ request, params }) => {
    const { state } = (await request.json()) as { state: string }
    const id = Number(params.id)
    const row = matches.find((match) => match.id === id) ?? matches[0]
    return HttpResponse.json({ ...row, id, state })
  }),
]
