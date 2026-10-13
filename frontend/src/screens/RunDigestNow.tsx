import { useEffect, useState } from 'react'
import { getRun, startTick, type RunSummary } from '../api/client'

/** How long this screen waits between two reads of the run it started. */
const POLL_MS = 1000

/** The two statuses a run never leaves: stop reading once one of them is answered. */
const TERMINAL = ['succeeded', 'failed']

/** Starts one digest run and shows how it ended, reading the run row until it does. */
export default function RunDigestNow() {
  // `null` is "no run started": the button has not been clicked, or the run ended unread.
  const [run, setRun] = useState<RunSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  // The id the `202` carried, and whether its status is still worth a read.
  const id = run?.id
  const polling = run !== null && !TERMINAL.includes(run.status)

  async function start() {
    setError(null)
    setRun(null)
    try {
      setRun(await startTick())
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  // The boards the run could not fetch, in the order the walk visited them. A
  // filter over the outcome rows, not a branch on `status`: a run still
  // `running` can already carry a failed board, and it is answered as-is.
  const failedBoards = run?.companyOutcomes.filter((board) => board.outcome === 'failed') ?? []

  // The companies the board-call budget left for a later run. The walk writes it
  // during a run, so a still-`running` row can carry it: no branch on `status`.
  // 0 is contract-legal but never written -- the walk only records a stop.
  const notFetched = run?.companiesNotFetched ?? 0

  useEffect(() => {
    if (!polling || id === undefined) return
    const timer = setInterval(() => {
      getRun(id).then(setRun, (failure: unknown) => {
        setError(failure instanceof Error ? failure.message : String(failure))
        // A run row that could not be read is not one to keep reading.
        setRun(null)
      })
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [polling, id])

  return (
    <section>
      <h2>Digest</h2>
      <button type="button" onClick={start}>
        Run digest now
      </button>
      {error !== null && <p role="alert">{error}</p>}
      {run !== null &&
        (run.status === 'succeeded' ? (
          <p>Digest finished: {run.matchesCreated} matches created.</p>
        ) : run.status === 'failed' ? (
          <p>Digest failed: {run.error}</p>
        ) : (
          <p>Digest running...</p>
        ))}
      {notFetched > 0 && (
        <p>
          {notFetched} {notFetched === 1 ? 'company was' : 'companies were'} not fetched: the
          run's request budget was reached.
        </p>
      )}
      {failedBoards.length > 0 && (
        <>
          <p>Boards the run could not fetch:</p>
          <ul>
            {failedBoards.map((board) => (
              <li key={board.companyId}>
                {board.error === null ? board.slug : `${board.slug}: ${board.error}`}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
