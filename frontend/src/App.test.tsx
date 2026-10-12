import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App from './App'

describe('the placeholder screen', () => {
  it('lists the companies the mock backend answers with', async () => {
    render(<App />)

    expect(await screen.findByText('Acme (acme)')).toBeDefined()
    expect(screen.getByText('Globex (globex)')).toBeDefined()
  })
})
