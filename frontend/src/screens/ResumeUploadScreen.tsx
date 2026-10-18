import { useState, type FormEvent } from 'react'
import { uploadResume, type ResumeUploaded } from '../api/client'

// Three outcomes a reader can tell apart: nothing uploaded yet, an upload in
// flight, and a refused upload.
const LOADING = 'Uploading the resume...'
const EMPTY = 'No resume uploaded yet.'

/** The file kinds the input offers; whether the bytes are readable stays the server's call. */
const ACCEPT = '.pdf,.docx,.txt,.png,.jpg,.jpeg,.tif,.tiff,.heic,.webp'

/**
 * Uploads one resume file and shows which resume the answer made active, or why
 * the upload was refused.
 *
 * The file's contents are never read, rendered or logged: the request carries
 * the file itself, and only `uploadResume` sends it.
 */
export default function ResumeUploadScreen() {
  const [file, setFile] = useState<File | null>(null)
  // `null` is "no answer has named an active resume yet", so neither the empty
  // text nor the loading text stands for one.
  const [active, setActive] = useState<ResumeUploaded | null>(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function upload(event: FormEvent) {
    event.preventDefault()
    if (file === null) return
    setUploading(true)
    setError(null)
    try {
      setActive(await uploadResume(file))
    } catch (failure) {
      // A refusal leaves the active resume, and the chosen file, as they were.
      setError(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setUploading(false)
    }
  }

  return (
    <section id="resume" className="panel">
      <h2>Resume</h2>
      <form onSubmit={upload}>
        <label>
          Resume file
          <input
            type="file"
            accept={ACCEPT}
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <button type="submit">Upload resume</button>
      </form>
      {active !== null && <p className="state">Active resume: {active.filename}</p>}
      {error !== null ? (
        <p role="alert">{error}</p>
      ) : uploading ? (
        <p className="state">{LOADING}</p>
      ) : active === null ? (
        <p className="state">{EMPTY}</p>
      ) : null}
    </section>
  )
}
