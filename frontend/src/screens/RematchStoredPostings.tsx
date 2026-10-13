import { rematch } from '../api/client'
import useRun from './useRun'

/** Starts one re-match and shows how it ended, reading the run row until it does. */
export default function RematchStoredPostings() {
  const { run, error, press } = useRun(rematch)

  // What the re-match's model cap left (#64). The column is three-way: `null` is
  // "no pass was bounded" -- a tick, or a re-match that left nothing -- and 0 is
  // contract-legal but never written, so both render no line and are not the same
  // answer as a positive count.
  const notRematched = run?.postingsNotRematched ?? 0

  return (
    <section>
      <h2>Re-match</h2>
      <button type="button" onClick={press}>
        Re-match stored postings
      </button>
      {error !== null && <p role="alert">{error}</p>}
      {run !== null &&
        (run.status === 'succeeded' ? (
          <p>
            Re-match finished: {run.matchesCreated}{' '}
            {run.matchesCreated === 1 ? 'posting' : 'postings'} scored.
          </p>
        ) : run.status === 'failed' ? (
          <p>Re-match failed: {run.error}</p>
        ) : (
          <p>Re-match running...</p>
        ))}
      {notRematched > 0 && (
        <p>
          {notRematched} stored {notRematched === 1 ? 'posting was' : 'postings were'} left: the
          re-match's cap was reached.
        </p>
      )}
    </section>
  )
}
