import { useEffect, useState } from 'react'
import { getRun, type RunSummary } from '../api/client'
import { publishRunEnded } from './runEnded'

/** How long a screen waits between two reads of the run it started. */
const POLL_MS = 1000

/** The two statuses a run never leaves: stop reading once one of them is answered. */
const TERMINAL = ['succeeded', 'failed']

/**
 * One run control's state: it starts a run through `start` and reads that run's row
 * until its status is terminal.
 *
 * Both run buttons share this, so the two cannot drift on the one place they must
 * not: which status ends a run and what a refusal does to the row on screen. `start`
 * is the screen's own call -- `startTick` or `rematch` -- and a press calls it once.
 */
export default function useRun(start: () => Promise<RunSummary>) {
  // `null` is "no run started": the button has not been clicked, or the row ended unread.
  const [run, setRun] = useState<RunSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  // The id the `202` carried, and whether its status is still worth a read.
  const id = run?.id
  const polling = run !== null && !TERMINAL.includes(run.status)

  async function press() {
    setError(null)
    setRun(null)
    try {
      setRun(await start())
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  useEffect(() => {
    if (!polling || id === undefined) return
    const timer = setInterval(() => {
      getRun(id).then(
        (row) => {
          setRun(row)
          // The one place a terminal status is observed, so the one publish: a
          // `failed` run may have written rows before it failed, so the terminal
          // set is exactly what the poll already stops on (#79).
          if (TERMINAL.includes(row.status)) publishRunEnded()
        },
        (failure: unknown) => {
          setError(failure instanceof Error ? failure.message : String(failure))
          // A run row that could not be read is not one to keep reading.
          setRun(null)
        },
      )
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [polling, id])

  return { run, error, press }
}
