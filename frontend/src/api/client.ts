/**
 * The one module that talks to the API: `#12` calls these functions, and no
 * other file under `src/` may reach the network (held by
 * `src/api/noStrayFetch.test.ts`).
 *
 * Every path is relative to this app's own origin: `#13` serves this build and
 * the API from one origin, so there is no base URL, no proxy and no CORS.
 */

/** One watchlist entry, as the UI reads it. */
export type WatchedCompany = {
  id: number
  name: string
  slug: string
  isActive: boolean
}

/** Body of an addition: `CompanyCreate` in `backend/openapi.json`. */
export type CompanyCreate = {
  name: string
  slug: string
}

/** Which resume is now the active one, as the UI reads it. */
export type ResumeUploaded = {
  id: number
  filename: string
  contentType: string
  isActive: boolean
}

/**
 * One run row, as the UI reads it.
 *
 * `started_at` and `finished_at` keep the contract's own names and stay the ISO
 * strings it sends, never `Date` objects.
 */
export type RunSummary = {
  id: number
  status: string
  started_at: string
  finished_at: string | null
  error: string | null
  postingsSeen: number
  matchesCreated: number
}

/** A non-2xx answer: `message` is the server's own `detail`, and nothing else. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

type WireCompany = {
  id: number
  name: string
  slug: string
  is_active: boolean
}

type WireResumeUploaded = {
  id: number
  filename: string
  content_type: string
  is_active: boolean
}

type WireRunSummary = {
  id: number
  status: string
  started_at: string
  finished_at: string | null
  error: string | null
  postings_seen: number
  matches_created: number
}

const decodeCompany = (wire: WireCompany): WatchedCompany => ({
  id: wire.id,
  name: wire.name,
  slug: wire.slug,
  isActive: wire.is_active,
})

const decodeResume = (wire: WireResumeUploaded): ResumeUploaded => ({
  id: wire.id,
  filename: wire.filename,
  contentType: wire.content_type,
  isActive: wire.is_active,
})

const decodeRun = (wire: WireRunSummary): RunSummary => ({
  id: wire.id,
  status: wire.status,
  started_at: wire.started_at,
  finished_at: wire.finished_at,
  error: wire.error,
  postingsSeen: wire.postings_seen,
  matchesCreated: wire.matches_created,
})

/** The server's `detail`, or the status text when the answer carries no JSON. */
async function detailOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    return typeof body.detail === 'string' ? body.detail : response.statusText
  } catch {
    return response.statusText
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // Resolved against the app's own origin: a bare relative path cannot be sent
  // by the test runner, which has no document to resolve one against.
  const response = await fetch(new URL(path, location.origin), init)
  if (!response.ok) throw new ApiError(response.status, await detailOf(response))
  // `204` carries no body, and reading one would throw.
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T)
}

const sendJson = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export async function listWatchlist(): Promise<WatchedCompany[]> {
  const rows = await request<WireCompany[]>('/watchlist')
  return rows.map(decodeCompany)
}

export async function addCompany(company: CompanyCreate): Promise<WatchedCompany> {
  return decodeCompany(await request<WireCompany>('/watchlist', sendJson(company)))
}

export async function removeCompany(id: number): Promise<void> {
  await request<void>(`/watchlist/${id}`, { method: 'DELETE' })
}

export async function uploadResume(file: File): Promise<ResumeUploaded> {
  // The part is named `file`, and the browser sets the multipart boundary.
  const body = new FormData()
  body.append('file', file)
  return decodeResume(await request<WireResumeUploaded>('/resume', { method: 'POST', body }))
}

/** Sends no secret: this bundle is public, so how a browser tick is authorised is `#13`'s. */
export async function startTick(): Promise<RunSummary> {
  return decodeRun(await request<WireRunSummary>('/internal/tick', { method: 'POST' }))
}

export async function getRun(id: number): Promise<RunSummary> {
  return decodeRun(await request<WireRunSummary>(`/runs/${id}`))
}
