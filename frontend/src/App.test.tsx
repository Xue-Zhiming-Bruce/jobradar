import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { server } from './mocks/server'

afterEach(cleanup)

describe('the app', () => {
  it('renders the resume upload, the watchlist, the matches and the run button', async () => {
    render(<App />)

    expect(screen.getByLabelText('Resume file')).toBeDefined()
    expect(await screen.findByText('Acme (acme)')).toBeDefined()
    expect(await screen.findByText('Globex: Data engineer')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Run digest now' })).toBeDefined()
  })

  it('shows a refused tick as an error and leaves the rest of the screen usable', async () => {
    const spies = ['error', 'log'].map((name) => vi.spyOn(console, name as 'error'))
    const detail = 'The request is not allowed'
    server.use(http.post('/internal/tick', () => HttpResponse.json({ detail }, { status: 401 })))
    render(<App />)
    await screen.findByText('Acme (acme)')

    fireEvent.click(screen.getByRole('button', { name: 'Run digest now' }))

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.getByText('Acme (acme)')).toBeDefined()
    expect(screen.getByText('Globex: Data engineer')).toBeDefined()
    expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
    spies.forEach((spy) => spy.mockRestore())
  })
})
