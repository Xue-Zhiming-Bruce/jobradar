import { useEffect, useState, type FormEvent } from 'react'
import { addCompany, listWatchlist, removeCompany, type WatchedCompany } from '../api/client'

// Three outcomes a reader can tell apart: not read yet, read and empty, refused.
const LOADING = 'Loading the watchlist...'
const EMPTY = 'No watched companies yet.'

/** Adds a company by name and Greenhouse slug, lists the watchlist, removes a row. */
export default function WatchlistScreen() {
  // `null` is "not read yet", so loading is not the same screen as empty.
  const [companies, setCompanies] = useState<WatchedCompany[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  // The form's own error sits beside the list: a refused addition leaves it as it was.
  const [formError, setFormError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')

  useEffect(() => {
    listWatchlist().then(setCompanies, (failure: unknown) => {
      setListError(failure instanceof Error ? failure.message : String(failure))
    })
  }, [])

  async function add(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    try {
      const added = await addCompany({ name, slug })
      setCompanies((rows) => [...(rows ?? []), added])
      setName('')
      setSlug('')
    } catch (failure) {
      setFormError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  async function remove(id: number) {
    try {
      await removeCompany(id)
      setCompanies((rows) => (rows ?? []).filter((company) => company.id !== id))
    } catch (failure) {
      setFormError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  return (
    <section>
      <h2>Watched companies</h2>
      <form onSubmit={add}>
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          Greenhouse slug
          <input value={slug} onChange={(event) => setSlug(event.target.value)} />
        </label>
        <button type="submit">Add company</button>
      </form>
      {formError !== null && <p role="alert">{formError}</p>}
      {listError !== null ? (
        <p role="alert">{listError}</p>
      ) : companies === null ? (
        <p>{LOADING}</p>
      ) : companies.length === 0 ? (
        <p>{EMPTY}</p>
      ) : (
        <ul>
          {companies.map((company) => (
            <li key={company.id}>
              <span>
                {company.name} ({company.slug})
              </span>
              <button type="button" onClick={() => remove(company.id)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
