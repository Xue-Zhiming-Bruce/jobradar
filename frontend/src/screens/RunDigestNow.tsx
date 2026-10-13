import { startTick } from '../api/client'
import useRun from './useRun'

/** Starts one digest run and shows how it ended, reading the run row until it does. */
export default function RunDigestNow() {
  const { run, error, press } = useRun(startTick)

  // The boards the run could not fetch, in the order the walk visited them. A
  // filter over the outcome rows, not a branch on `status`: a run still
  // `running` can already carry a failed board, and it is answered as-is.
  const failedBoards = run?.companyOutcomes.filter((board) => board.outcome === 'failed') ?? []

  // The companies the board-call budget left for a later run. The walk writes it
  // during a run, so a still-`running` row can carry it: no branch on `status`.
  // 0 is contract-legal but never written -- the walk only records a stop.
  const notFetched = run?.companiesNotFetched ?? 0

  // The count this run's own digest emailed. Keyed off the count, not `status`:
  // the pipeline sends the digest before `_run` derives `failed` from a failed
  // board, so a `running` or `failed` row can already carry it. `null` is no
  // answer yet -- the `202`'s fresh row, or a send that raised -- and gets no
  // sentence, while `0` is the answer "ran and emailed nothing".
  const emailed = run?.matchesEmailed ?? null

  return (
    <section>
      <h2>Digest</h2>
      <button type="button" onClick={press}>
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
      {emailed !== null && (
        <p>
          {emailed === 0
            ? 'Digest emailed: no matches.'
            : `Digest emailed: ${emailed} ${emailed === 1 ? 'match' : 'matches'}.`}
        </p>
      )}
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
