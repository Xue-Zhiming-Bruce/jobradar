import { http, HttpResponse } from 'msw'
import { companies, resumeUploaded, runSummary } from './fixtures'

/** The mock backend: one handler per method-and-path pair in `backend/openapi.json`. */
export const handlers = [
  http.get('/watchlist', () => HttpResponse.json(companies)),

  http.post('/watchlist', () => HttpResponse.json(companies[0], { status: 201 })),

  http.delete('/watchlist/:id', () => new HttpResponse(null, { status: 204 })),

  http.post('/resume', () => HttpResponse.json(resumeUploaded, { status: 201 })),

  http.post('/internal/tick', () => HttpResponse.json(runSummary, { status: 202 })),

  http.get('/runs/:id', () => HttpResponse.json(runSummary)),
]
