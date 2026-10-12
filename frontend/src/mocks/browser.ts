import { setupWorker } from 'msw/browser'
import { handlers } from './handlers'

/** The mock backend `npm run dev` answers with, so a fresh checkout needs no API. */
export const worker = setupWorker(...handlers)
