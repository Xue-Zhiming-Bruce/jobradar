import { useCallback, useEffect, useState } from 'react'
import { listMatches, setMatchState, type Match, type MatchState } from '../api/client'
import { subscribeRunEnded } from './runEnded'

// Three outcomes a reader can tell apart: not read yet, read and empty, refused.
const LOADING = 'Loading matches...'
const EMPTY = 'No matches yet.'

/**
 * The rows grouped by the row's own `state`, and nothing else groups them.
 * The new list holds exactly the `new` rows, and a verdict moves a row out of
 * it from the answer `PATCH` returned -- never from a cached pre-verdict row.
 */
const GROUPS = [
  { state: 'new', title: 'New matches' },
  { state: 'saved', title: 'Saved matches' },
  { state: 'dismissed', title: 'Dismissed matches' },
]

/**
 * One match, as a card of its own: who and what, the score, the reason, the gaps
 * as chips and the two verdict controls (#114 AC3). The gaps are chips and not a
 * nested list, so a row holds no list of its own.
 */
function MatchRow({ match, judge }: { match: Match; judge: (state: MatchState) => void }) {
  return (
    <li className="match">
      <div className="match-head">
        <p className="match-title">
          {match.companyName}: {match.title}
        </p>
        <span className="score">Score: {match.score}</span>
      </div>
      <p className="match-reason">{match.reason}</p>
      {match.gaps.length > 0 && (
        <p className="chips">
          {match.gaps.map((gap) => (
            <span className="chip" key={gap}>
              {gap}
            </span>
          ))}
        </p>
      )}
      <p>State: {match.state}</p>
      <div className="match-actions">
        <button type="button" onClick={() => judge('saved')}>
          Save
        </button>
        <button type="button" onClick={() => judge('dismissed')}>
          Dismiss
        </button>
      </div>
    </li>
  )
}

/** Browses the stored matches and records a saved or dismissed verdict on one. */
export default function MatchesScreen() {
  // `null` is "not read yet", so loading is not the same screen as empty.
  const [matches, setMatches] = useState<Match[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  // A refused verdict sits beside the rows: it changes no state and hides nothing.
  const [verdictError, setVerdictError] = useState<string | null>(null)

  // The one path that reads the list, shared by the mount effect and by a run's
  // end. It never resets `matches` to `null`: `null` means "not read yet" for the
  // mount read alone, so a re-read cannot flash the loading text over rows that
  // are already on screen. A successful read clears a previous `listError` (#79).
  const read = useCallback(() => {
    listMatches().then(
      (rows) => {
        setListError(null)
        setMatches(rows)
      },
      (failure: unknown) => {
        setListError(failure instanceof Error ? failure.message : String(failure))
      },
    )
  }, [])

  useEffect(() => {
    read()
    // A run the page started has ended, so the rows on screen may be stale.
    return subscribeRunEnded(read)
  }, [read])

  async function judge(id: number, state: MatchState) {
    try {
      const judged = await setMatchState(id, state)
      setVerdictError(null)
      setMatches((rows) => (rows ?? []).map((row) => (row.id === judged.id ? judged : row)))
    } catch (failure) {
      setVerdictError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  return (
    <section id="matches" className="panel">
      <h2>Matches</h2>
      {verdictError !== null && <p role="alert">{verdictError}</p>}
      {listError !== null ? (
        <p role="alert">{listError}</p>
      ) : matches === null ? (
        <p className="state">{LOADING}</p>
      ) : matches.length === 0 ? (
        <p className="state">{EMPTY}</p>
      ) : (
        GROUPS.map((group) => (
          <section key={group.state}>
            <h3>{group.title}</h3>
            <ul className="matches" aria-label={group.title}>
              {matches
                .filter((match) => match.state === group.state)
                .map((match) => (
                  <MatchRow key={match.id} match={match} judge={(state) => judge(match.id, state)} />
                ))}
            </ul>
          </section>
        ))
      )}
    </section>
  )
}
