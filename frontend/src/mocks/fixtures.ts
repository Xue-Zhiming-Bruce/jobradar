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
}
