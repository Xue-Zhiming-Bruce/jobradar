import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { resumeUploaded } from '../mocks/fixtures'
import ResumeUploadScreen from './ResumeUploadScreen'

// The suite runs without Vitest globals, so Testing Library's auto-cleanup is off.
afterEach(cleanup)

const LOADING = 'Uploading the resume...'
const EMPTY = 'No resume uploaded yet.'

/** How the mock answers a `POST /resume`, as `backend/openapi.json` shapes it. */
const uploaded = (filename: string) =>
  HttpResponse.json({ ...resumeUploaded, filename }, { status: 201 })

/** An answer the test holds back: the handler returns this promise before it is resolved. */
function withheld() {
  let release!: (response: Response) => void
  const promise = new Promise<Response>((resolve) => (release = resolve))
  return { promise, release }
}

/** Every `console` method the screen and the module reach: a resume may not reach one. */
const silenceConsole = () =>
  ['log', 'info', 'warn', 'error'].map((name) => vi.spyOn(console, name as 'log'))

const expectSilence = (spies: ReturnType<typeof silenceConsole>) => {
  expect(spies.flatMap((spy) => spy.mock.calls)).toEqual([])
  spies.forEach((spy) => spy.mockRestore())
}

/** Chooses a file by name in the input: the placeholder stands in for a resume. */
function choose(name: string) {
  const file = new File(['placeholder bytes, not resume text'], name, { type: 'text/plain' })
  fireEvent.change(screen.getByLabelText('Resume file'), { target: { files: [file] } })
}

const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Upload resume' }))

/**
 * Records the requests `POST /resume` answered, without reading the body.
 *
 * The multipart body is the browser's to build: #11's `uploadResume` case makes
 * the same choice, because reading a `FormData` body back through the mock is
 * not what these cases assert. The content type is enough to prove the call came
 * from the API module (multipart with a boundary, never JSON) and the count
 * proves how many uploads the screen sent.
 */
function recordRequests(seen: { method: string; contentType: string | null }[]) {
  return http.post('/resume', ({ request }) => {
    seen.push({ method: request.method, contentType: request.headers.get('content-type') })
    return uploaded('resume.txt')
  })
}

describe('the resume upload screen', () => {
  it('offers .pdf, .docx and .txt in the file input', () => {
    render(<ResumeUploadScreen />)

    expect(screen.getByLabelText('Resume file').getAttribute('accept')).toBe('.pdf,.docx,.txt')
  })

  it('uploads the chosen file and renders the answered filename as the active resume', async () => {
    const spies = silenceConsole()
    const seen: { method: string; contentType: string | null }[] = []
    server.use(recordRequests(seen))
    render(<ResumeUploadScreen />)

    choose('resume.txt')
    submit()

    expect(await screen.findByText('Active resume: resume.txt')).toBeDefined()
    // One upload, sent as multipart by the API module and not as JSON by the screen.
    expect(seen.map((request) => request.method)).toEqual(['POST'])
    expect(seen).toHaveLength(1)
    expect(seen[0].contentType?.startsWith('multipart/form-data; boundary=')).toBe(true)
    expectSilence(spies)
  })

  it('sends nothing until a file is chosen', async () => {
    const posts: string[] = []
    server.use(
      http.post('/resume', ({ request }) => {
        posts.push(request.method)
        return HttpResponse.json(resumeUploaded, { status: 201 })
      }),
    )
    render(<ResumeUploadScreen />)

    submit()

    expect(screen.getByText(EMPTY)).toBeDefined()
    expect(posts).toEqual([])
  })

  it.each([
    ['415', 415, 'The declared type is not a resume'],
    ['413', 413, 'The upload is 9000000 bytes: the limit is 5000000 bytes'],
  ])('shows the answer\'s detail on a %s, and leaves the active resume as it was', async (_case, status, detail) => {
    const spies = silenceConsole()
    let uploads = 0
    // The first upload is accepted, the second one is the refusal under test.
    server.use(
      http.post('/resume', () => {
        uploads += 1
        return uploads === 1 ? uploaded('first-resume.txt') : HttpResponse.json({ detail }, { status })
      }),
    )
    render(<ResumeUploadScreen />)

    choose('first-resume.txt')
    submit()
    expect(await screen.findByText('Active resume: first-resume.txt')).toBeDefined()

    choose('second-resume.txt')
    submit()

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.getByText('Active resume: first-resume.txt')).toBeDefined()
    expect(screen.queryByText(EMPTY)).toBeNull()
    // Both uploads were sent: a refusal is the answer's, not the screen withholding the file.
    expect(uploads).toBe(2)
    expectSilence(spies)
  })
})

describe("the resume screen's three outcomes", () => {
  it('shows the loading text, and neither the empty nor the error text, while the answer is withheld', async () => {
    const answer = withheld()
    server.use(http.post('/resume', () => answer.promise))
    render(<ResumeUploadScreen />)

    choose('resume.txt')
    submit()

    expect(screen.getByText(LOADING)).toBeDefined()
    expect(screen.queryByText(EMPTY)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()

    answer.release(uploaded('resume.txt'))
    expect(await screen.findByText('Active resume: resume.txt')).toBeDefined()
  })

  it('shows the empty text, and neither the loading nor the error text, with no resume yet', () => {
    render(<ResumeUploadScreen />)

    expect(screen.getByText(EMPTY)).toBeDefined()
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows the error text carrying the detail, and neither of the other two, on a 500', async () => {
    const detail = 'The resume could not be read'
    server.use(http.post('/resume', () => HttpResponse.json({ detail }, { status: 500 })))
    render(<ResumeUploadScreen />)

    choose('resume.txt')
    submit()

    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByText(LOADING)).toBeNull()
    expect(screen.queryByText(EMPTY)).toBeNull()
  })
})
