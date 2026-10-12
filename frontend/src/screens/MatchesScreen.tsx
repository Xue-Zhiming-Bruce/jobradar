import { useEffect, useState } from 'react'
import { listMatches, setMatchState, type Match, type MatchState } from '../api/client'

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

function MatchRow({ match, judge }: { match: Match; judge: (state: MatchState) => void }) {
  return (
    <li>
      <p>
        {match.companyName}: {match.title}
      </p>
      <p>Score: {match.score}</p>
      <p>{match.reason}</p>
      <ul>
        {match.gaps.map((gap) => (
          <li key={gap}>{gap}</li>
        ))}
      </ul>
      <p>State: {match.state}</p>
      <button type="button" onClick={() => judge('saved')}>
        Save
      </button>
      <button type="button" onClick={() => judge('dismissed')}>
        Dismiss
      </button>
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

  useEffect(() => {
    listMatches().then(setMatches, (failure: unknown) => {
      setListError(failure instanceof Error ? failure.message : String(failure))
    })
  }, [])

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
    <section>
      <h2>Matches</h2>
      {verdictError !== null && <p role="alert">{verdictError}</p>}
      {listError !== null ? (
        <p role="alert">{listError}</p>
      ) : matches === null ? (
        <p>{LOADING}</p>
      ) : matches.length === 0 ? (
        <p>{EMPTY}</p>
      ) : (
        GROUPS.map((group) => (
          <section key={group.state}>
            <h3>{group.title}</h3>
            <ul aria-label={group.title}>
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
