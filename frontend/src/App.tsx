import { useEffect, useState } from 'react'
import { listWatchlist, type WatchedCompany } from './api/client'

// Placeholder: the watchlist screen, the matches screen and their loading,
// empty and error states are #12.
export default function App() {
  const [companies, setCompanies] = useState<WatchedCompany[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listWatchlist().then(setCompanies, (failure: unknown) => {
      setError(failure instanceof Error ? failure.message : String(failure))
    })
  }, [])

  return (
    <main>
      <h1>JobRadar</h1>
      <h2>Watched companies</h2>
      {error !== null ? (
        <p role="alert">{error}</p>
      ) : (
        <ul>
          {companies.map((company) => (
            <li key={company.id}>
              {company.name} ({company.slug})
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}
