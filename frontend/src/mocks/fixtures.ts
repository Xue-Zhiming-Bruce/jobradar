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
  postings_not_rematched: null,
  // The trace of the request that started this run (#102): the id `traces` below
  // holds the tick's own span and the run's under.
  trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
  // No company has been attempted yet, so the list the `202` answers with is empty.
  company_outcomes: [],
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
  postings_not_rematched: null,
  // The same request's trace as `runSummary` above: the mock's one press (#102).
  trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
  company_outcomes: [
    { company_id: 1, slug: 'acme', outcome: 'fetched', error: null },
    { company_id: 2, slug: 'globex', outcome: 'not_modified', error: null },
  ],
}

/** One route's window, as `GET /telemetry/metrics` answers it: `RouteMetric`, four keys. */
export const routeMetrics = [
  { route: '/matches', count: 12, errors: 0, p95_ms: 41 },
  { route: '/internal/tick', count: 3, errors: 1, p95_ms: 128 },
]

/**
 * The traces `GET /telemetry/traces` answers with, newest first as the route orders
 * them. The first is the one trace id `#90` writes for a "Run digest now" press:
 * the tick's own request span, and the run's span under the same id -- so the
 * request and the run read together. The run's span is not an HTTP request, so
 * it carries no route, method or status code (all three null).
 */
export const traces = [
  {
    trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
    spans: [
      {
        id: 1,
        name: 'http',
        route: '/internal/tick',
        method: 'POST',
        status_code: 202,
        duration_ms: 4,
        started_at: '2026-01-01T09:01:00+00:00',
      },
      {
        id: 2,
        name: 'run',
        route: null,
        method: null,
        status_code: null,
        duration_ms: 3012,
        started_at: '2026-01-01T09:01:00.004000+00:00',
      },
    ],
  },
  {
    trace_id: '0af7651916cd43dd8448eb211c80319c',
    spans: [
      {
        id: 3,
        name: 'http',
        route: '/matches',
        method: 'GET',
        status_code: 200,
        duration_ms: 41,
        started_at: '2026-01-01T09:00:00+00:00',
      },
    ],
  },
]

/** A run the board-call budget stopped before it reached every active company (#56). */
export const runBudgetStopped = {
  ...runSucceeded,
  companies_not_fetched: 3,
}

/** One company short, so the sentence's singular is exercised. */
export const runBudgetStoppedOne = {
  ...runSucceeded,
  companies_not_fetched: 1,
}

/** Still running: `_record_not_fetched` wrote the count before scoring and the digest. */
export const runBudgetStoppedRunning = {
  ...runSummary,
  companies_not_fetched: 3,
}

/** A run that failed after the budget stopped it: both facts are on the wire. */
export const runBudgetStoppedFailed = {
  ...runSummary,
  status: 'failed',
  error: 'Greenhouse timed out',
  companies_not_fetched: 3,
}

/** 0 is contract-legal but never written, so the guard must not print it (#72 AC7). */
export const runBudgetStoppedZero = {
  ...runSucceeded,
  companies_not_fetched: 0,
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
