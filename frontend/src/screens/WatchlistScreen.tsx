import { useEffect, useState, type FormEvent } from 'react'
import {
  addCompany,
  listWatchlist,
  removeCompany,
  updateCompany,
  type CompanySource,
  type CompanyUpdate,
  type WatchedCompany,
} from '../api/client'

// Three outcomes a reader can tell apart: not read yet, read and empty, refused.
const LOADING = 'Loading the watchlist...'
const EMPTY = 'No watched companies yet.'

/**
 * Adds a company by name, board slug and source, lists the watchlist, renames a
 * row, points one at a corrected board, takes one out of the poll and puts it
 * back, removes a row.
 */
export default function WatchlistScreen() {
  // `null` is "not read yet", so loading is not the same screen as empty.
  const [companies, setCompanies] = useState<WatchedCompany[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  // The form's own error sits beside the list: a refused addition leaves it as it was.
  const [formError, setFormError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [source, setSource] = useState<CompanySource>('greenhouse')

  useEffect(() => {
    listWatchlist().then(setCompanies, (failure: unknown) => {
      setListError(failure instanceof Error ? failure.message : String(failure))
    })
  }, [])

  async function add(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    try {
      const added = await addCompany({ name, slug, source })
      setCompanies((rows) => [...(rows ?? []), added])
      setName('')
      setSlug('')
      setSource('greenhouse')
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

  // Only the row the answer carries is replaced, and a refusal leaves the list alone.
  async function edit(id: number, body: CompanyUpdate) {
    try {
      const updated = await updateCompany(id, body)
      setFormError(null)
      setCompanies((rows) => (rows ?? []).map((row) => (row.id === updated.id ? updated : row)))
    } catch (failure) {
      setFormError(failure instanceof Error ? failure.message : String(failure))
    }
  }

  function rename(event: FormEvent<HTMLFormElement>, id: number) {
    event.preventDefault()
    // The field is uncontrolled: the route stores the name it was sent and answers it back.
    const typed = new FormData(event.currentTarget).get('name') as string
    void edit(id, { name: typed })
  }

  // A move is the heavier edit, so its cost is on the screen before the button is
  // clicked: the route closes the old board's open postings in the same write.
  function move(event: FormEvent<HTMLFormElement>, id: number) {
    event.preventDefault()
    // Uncontrolled like the name: the route normalises the slug it is sent and answers it back.
    const typed = new FormData(event.currentTarget).get('slug') as string
    void edit(id, { slug: typed })
  }

  return (
    <section id="watchlist" className="panel">
      <h2>Watched companies</h2>
      <form onSubmit={add}>
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          Board slug
          <input value={slug} onChange={(event) => setSlug(event.target.value)} />
        </label>
        <label>
          Source
          <select
            value={source}
            onChange={(event) => setSource(event.target.value as CompanySource)}
          >
            <option value="greenhouse">Greenhouse</option>
            <option value="lever">Lever</option>
          </select>
        </label>
        <button type="submit">Add company</button>
      </form>
      {formError !== null && <p role="alert">{formError}</p>}
      {listError !== null ? (
        <p role="alert">{listError}</p>
      ) : companies === null ? (
        <p className="state">{LOADING}</p>
      ) : companies.length === 0 ? (
        <p className="state">{EMPTY}</p>
      ) : (
        // One row per company, its own controls in its own cells (#114 AC4).
        <div className="table-scroll">
          <table className="watchlist">
            <caption>Watched companies, their boards and their controls</caption>
            <thead>
              <tr>
                <th scope="col">Company</th>
                <th scope="col">Source</th>
                <th scope="col">Status</th>
                <th scope="col">Rename</th>
                <th scope="col">Move to board</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {companies.map((company) => (
                <tr key={company.id}>
                  <th scope="row">
                    {company.name} ({company.slug})
                  </th>
                  <td>{company.source}</td>
                  <td>{company.isActive ? 'Active' : 'Inactive'}</td>
                  <td>
                    <form onSubmit={(event) => rename(event, company.id)}>
                      <label>
                        New name
                        <input name="name" defaultValue={company.name} />
                      </label>
                      <button type="submit">Rename</button>
                    </form>
                  </td>
                  <td>
                    <form onSubmit={(event) => move(event, company.id)}>
                      <label>
                        New board slug
                        <input
                          name="slug"
                          defaultValue={company.slug}
                          aria-describedby={`slug-cost-${company.id}`}
                        />
                      </label>
                      <button type="submit">Move to board</button>
                      <span id={`slug-cost-${company.id}`} className="hint">
                        Moving closes this board's open postings; its matches are kept.
                      </span>
                    </form>
                  </td>
                  <td>
                    <div className="match-actions">
                      <button
                        type="button"
                        onClick={() => edit(company.id, { is_active: !company.isActive })}
                      >
                        {company.isActive ? 'Deactivate' : 'Activate'}
                      </button>
                      <button type="button" onClick={() => remove(company.id)}>
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
