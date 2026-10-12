import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The mock mirrors `backend/openapi.json` -- the committed contract, read here
 * at test time. Each handler's body must carry exactly the keys the matching
 * schema's `required` list names, so a mock that answers `{}` or invents a
 * field fails.
 */
const contract = JSON.parse(
  // Vitest runs from `frontend/`, and the contract is committed one directory up.
  readFileSync(join(process.cwd(), '..', 'backend', 'openapi.json'), 'utf8'),
) as { components: { schemas: Record<string, { required?: string[] }> } }

const requiredKeys = (schema: string) =>
  [...(contract.components.schemas[schema].required ?? [])].sort()

const api = (path: string, init?: RequestInit) => fetch(new URL(path, location.origin), init)

const keysOf = async (response: Response) =>
  Object.keys((await response.json()) as Record<string, unknown>).sort()

describe('the mock answers the shapes the contract requires', () => {
  it('GET /watchlist answers WatchedCompany rows', async () => {
    const response = await api('/watchlist')

    expect(response.status).toBe(200)
    const rows = (await response.json()) as unknown[]
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(Object.keys(row as Record<string, unknown>).sort()).toEqual(
        requiredKeys('WatchedCompany'),
      )
    }
  })

  it('POST /watchlist answers one WatchedCompany', async () => {
    const response = await api('/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Acme', slug: 'acme' }),
    })

    expect(response.status).toBe(201)
    expect(await keysOf(response)).toEqual(requiredKeys('WatchedCompany'))
  })

  it('DELETE /watchlist/:id answers 204 with no body', async () => {
    const response = await api('/watchlist/1', { method: 'DELETE' })

    expect(response.status).toBe(204)
    expect(await response.text()).toBe('')
  })

  it('POST /resume answers a ResumeUploaded', async () => {
    const form = new FormData()
    form.append('file', new File(['a resume'], 'resume.txt', { type: 'text/plain' }))
    const response = await api('/resume', { method: 'POST', body: form })

    expect(response.status).toBe(201)
    expect(await keysOf(response)).toEqual(requiredKeys('ResumeUploaded'))
  })

  it('POST /internal/tick answers a RunSummary', async () => {
    const response = await api('/internal/tick', { method: 'POST' })

    expect(response.status).toBe(202)
    expect(await keysOf(response)).toEqual(requiredKeys('RunSummary'))
  })

  it('GET /runs/:id answers a RunSummary', async () => {
    const response = await api('/runs/1')

    expect(response.status).toBe(200)
    expect(await keysOf(response)).toEqual(requiredKeys('RunSummary'))
  })

  it('GET /matches answers MatchSummary rows, a row per state', async () => {
    const response = await api('/matches')

    expect(response.status).toBe(200)
    const rows = (await response.json()) as { state: string }[]
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(Object.keys(row).sort()).toEqual(requiredKeys('MatchSummary'))
    }
    expect([...new Set(rows.map((row) => row.state))].sort()).toEqual([
      'dismissed',
      'new',
      'saved',
    ])
  })

  it('PATCH /matches/:id answers one MatchSummary in the state it was sent', async () => {
    const response = await api('/matches/1', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: 'dismissed' }),
    })

    expect(response.status).toBe(200)
    const row = (await response.json()) as { id: number; state: string }
    expect(Object.keys(row).sort()).toEqual(requiredKeys('MatchSummary'))
    expect(row.id).toBe(1)
    expect(row.state).toBe('dismissed')
  })
})
