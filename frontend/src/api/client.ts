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
  source: CompanySource
}

/** Body of an addition: `CompanyCreate` in `backend/openapi.json`. */
export type CompanyCreate = {
  name: string
  slug: string
  /** Optional as the contract has it: absent means the contract's own default, Greenhouse. */
  source?: CompanySource
}

/**
 * Body of an edit: `CompanyUpdate` in `backend/openapi.json`, all four optional
 * and `source` still refused by the route. Nothing sends it yet: the screen that
 * renames or moves a company is `#61`.
 */
export type CompanyUpdate = {
  name?: string
  is_active?: boolean
  source?: CompanySource
  slug?: string
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
  companiesNotFetched: number | null
  /** What this run did with each company it attempted: `RunCompanyOutcome` in the contract. */
  companyOutcomes: RunCompanyOutcome[]
}

/**
 * One company's outcome in one run, as the UI reads it: `'fetched'`,
 * `'not_modified'` or `'failed'`, with the failure's own message on a `'failed'`
 * row and none otherwise (#59). Nothing renders it yet: that is #74.
 */
export type RunCompanyOutcome = {
  companyId: number
  slug: string
  outcome: string
  error: string | null
}

/**
 * One stored match, as the UI reads it.
 *
 * `created_at` keeps the contract's own name and stays the ISO string it sends,
 * never a `Date`.
 */
export type Match = {
  id: number
  companyName: string
  title: string
  url: string
  score: number
  reason: string
  gaps: string[]
  state: string
  createdAt: string
}

/** The two verdicts `PATCH /matches/{id}` accepts: `MatchVerdict` in the contract. */
export type MatchState = 'saved' | 'dismissed'

/**
 * The two boards a company can be watched on: `CompanySource` in
 * `backend/openapi.json`, whose `WatchedCompany.source` default is the first.
 */
export type CompanySource = 'greenhouse' | 'lever'

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
  // Optional: the contract's `required` list is the four fields above, and
  // `source` is the one property with a declared default.
  source?: CompanySource
}

type WireResumeUploaded = {
  id: number
  filename: string
  content_type: string
  is_active: boolean
}

type WireRunCompanyOutcome = {
  company_id: number
  slug: string
  outcome: string
  error: string | null
}

type WireRunSummary = {
  id: number
  status: string
  started_at: string
  finished_at: string | null
  error: string | null
  postings_seen: number
  matches_created: number
  companies_not_fetched: number | null
  company_outcomes: WireRunCompanyOutcome[]
}

type WireMatch = {
  id: number
  company_name: string
  title: string
  url: string
  score: number
  reason: string
  gaps: string[]
  state: string
  created_at: string
}

const decodeCompany = (wire: WireCompany): WatchedCompany => ({
  id: wire.id,
  name: wire.name,
  slug: wire.slug,
  isActive: wire.is_active,
  source: wire.source ?? 'greenhouse',
})

const decodeResume = (wire: WireResumeUploaded): ResumeUploaded => ({
  id: wire.id,
  filename: wire.filename,
  contentType: wire.content_type,
  isActive: wire.is_active,
})

const decodeMatch = (wire: WireMatch): Match => ({
  id: wire.id,
  companyName: wire.company_name,
  title: wire.title,
  url: wire.url,
  score: wire.score,
  reason: wire.reason,
  gaps: wire.gaps,
  state: wire.state,
  createdAt: wire.created_at,
})

const decodeRunCompanyOutcome = (wire: WireRunCompanyOutcome): RunCompanyOutcome => ({
  companyId: wire.company_id,
  slug: wire.slug,
  outcome: wire.outcome,
  error: wire.error,
})

const decodeRun = (wire: WireRunSummary): RunSummary => ({
  id: wire.id,
  status: wire.status,
  started_at: wire.started_at,
  finished_at: wire.finished_at,
  error: wire.error,
  postingsSeen: wire.postings_seen,
  matchesCreated: wire.matches_created,
  companiesNotFetched: wire.companies_not_fetched,
  companyOutcomes: wire.company_outcomes.map(decodeRunCompanyOutcome),
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

const sendJson = (body: unknown, method: 'POST' | 'PATCH' = 'POST'): RequestInit => ({
  method,
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

export async function listMatches(): Promise<Match[]> {
  return (await request<WireMatch[]>('/matches')).map(decodeMatch)
}

/** Records one verdict and answers the row as `PATCH /matches/{id}` read it after the write. */
export async function setMatchState(id: number, state: MatchState): Promise<Match> {
  return decodeMatch(await request<WireMatch>(`/matches/${id}`, sendJson({ state }, 'PATCH')))
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
