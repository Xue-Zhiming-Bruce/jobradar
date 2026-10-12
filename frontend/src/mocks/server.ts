import { setupServer } from 'msw/node'
import { handlers } from './handlers'

/** The mock backend in tests: started for a suite, reset between its tests. */
export const server = setupServer(...handlers)
