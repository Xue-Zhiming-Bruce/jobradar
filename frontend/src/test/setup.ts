import { afterAll, afterEach, beforeAll } from 'vitest'
import { server } from '../mocks/server'

// Every suite runs against the mock backend, and an unmocked call fails loudly
// rather than reaching the network.
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))

afterEach(() => server.resetHandlers())

afterAll(() => server.close())
