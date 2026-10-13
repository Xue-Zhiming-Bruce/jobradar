/**
 * The dev placeholder's data, in the contract's own `snake_case` shape.
 *
 * Nothing here ships: the build drops the branch that starts the mock.
 */
export const companies = [
  { id: 1, name: 'Acme', slug: 'acme', is_active: true },
  { id: 2, name: 'Globex', slug: 'globex', is_active: true },
]

export const resumeUploaded = {
  id: 1,
  filename: 'resume.txt',
  content_type: 'text/plain',
  is_active: true,
}

export const runSummary = {
  id: 1,
  status: 'running',
  started_at: '2026-01-01T09:00:00+00:00',
  finished_at: null,
  error: null,
  postings_seen: 0,
  matches_created: 0,
  matches_emailed: null,
  companies_not_fetched: null,
}

/** The terminal row `GET /runs/{id}` answers with, so the dev button reaches an outcome. */
export const runSucceeded = {
  id: 1,
  status: 'succeeded',
  started_at: '2026-01-01T09:00:00+00:00',
  finished_at: '2026-01-01T09:00:04+00:00',
  error: null,
  postings_seen: 12,
  matches_created: 4,
  matches_emailed: 3,
  companies_not_fetched: null,
}

// One row per state, newest first, so a test can tell all three treatments apart:
// a `new` row is worth a verdict, a `saved` or `dismissed` one already has it.
// The score-0 row is a real row the screen shows like any other -- `#12` filters
// neither on score nor on the reason's prefix. `outcome` is the row's own marker,
// which the contract requires since `#30`: the score-0 row is a rejection.
export const matches = [
  {
    id: 1,
    company_name: 'Acme',
    title: 'Backend engineer',
    url: 'https://boards.greenhouse.io/acme/jobs/1',
    score: 88,
    reason: 'Python and FastAPI line up with this role',
    gaps: ['No Kubernetes', 'No Go'],
    state: 'new',
    outcome: 'scored',
    created_at: '2026-05-03T09:00:00+00:00',
  },
  {
    id: 2,
    company_name: 'Globex',
    title: 'Data engineer',
    url: 'https://boards.greenhouse.io/globex/jobs/2',
    score: 74,
    reason: 'ETL work and Postgres match the listing',
    gaps: ['No Spark'],
    state: 'saved',
    outcome: 'scored',
    created_at: '2026-05-02T09:00:00+00:00',
  },
  {
    id: 3,
    company_name: 'Initech',
    title: 'Platform engineer',
    url: 'https://boards.greenhouse.io/initech/jobs/3',
    score: 61,
    reason: 'On-call overlap, nothing else',
    gaps: [],
    state: 'dismissed',
    outcome: 'scored',
    created_at: '2026-05-01T09:00:00+00:00',
  },
  {
    id: 4,
    company_name: 'Umbrella',
    title: 'Senior staff engineer',
    url: 'https://boards.greenhouse.io/umbrella/jobs/4',
    score: 0,
    reason: 'Prefiltered out: senior title',
    gaps: [],
    state: 'new',
    outcome: 'rejected',
    created_at: '2026-04-30T09:00:00+00:00',
  },
]
