# JobRadar — backlog

Decomposed from `_docs/plan.md`. Order is build order; lowest number first.
Each task is reviewed by the PM before implementation (`_docs/team/pm.md`).

Rubric tags map to the course's published peer-review criteria, so nothing in
the graded list is left without an owner.

---

## T1 — Backend skeleton with a passing test and a committed contract

**Goal.** A FastAPI app that starts, answers `/healthz`, and whose OpenAPI
document is generated and committed.

**Acceptance criteria**
- [ ] `uv run pytest` passes from a clean checkout
- [ ] `GET /healthz` returns 200 with a JSON body
- [ ] `openapi.json` is committed and regenerating it produces no diff
- [ ] `docker build` succeeds on the backend image

**Out of scope.** Any domain tables or endpoints.
**Constraints.** `backend/`. Python via `uv`. No database yet.
**Rubric.** Back-end implementation; API contract; containerization.

---

## T2 — Data model and dual-database configuration

Split into four issues during grooming: #2 carried 40 acceptance criteria, which is
not a one-session task. The content is unchanged, only divided.

- **T2a — Models, tables, and the SQLite path.** #2
- **T2b — Postgres parity and the engine matrix.** #22
- **T2c — Constraints the database enforces.** #23
- **T2d — Runtime configuration and the connection lifecycle.** #24 (follow-up,
filed because the 12-criteria ceiling left six lifecycle and configuration
criteria with no home)

Migration tooling is out of scope for all four and is owned by #21.

**Rubric.** Database integration (different environments).

---

## T3 — Resume upload

**Goal.** `POST /resume` accepts a file, extracts its text, and stores it as the
active resume.

**Acceptance criteria**
- [ ] A PDF, DOCX and TXT upload each yield extracted text
- [ ] A wrong content type is rejected with a clear error
- [ ] An oversized upload is rejected with a clear error
- [ ] An unreadable or text-less file is rejected with a clear error
- [ ] The stored file lands outside the repository and its text never appears in logs

**Out of scope.** Multiple resumes; parsing the resume into structured fields.
**Constraints.** `backend/app/routes/`, `backend/app/services/resume.py`. Uploads dir from config.
**Rubric.** Back-end implementation; validation at a trust boundary.

---

## T4 — Greenhouse source adapter

**Goal.** One adapter that turns a company's public Greenhouse job board into
normalised postings.

**Acceptance criteria**
- [ ] `fetch(slug)` returns normalised postings from a recorded fixture with no live network call
- [ ] An unknown slug is reported as a distinguishable error, not a crash
- [ ] An empty board returns an empty list
- [ ] A malformed payload and a timeout each raise a typed error
- [ ] The adapter is the only module that knows the Greenhouse payload shape

**Out of scope.** Lever, Ashby, LinkedIn, Indeed, Workday.
**Constraints.** `backend/app/sources/`, fixtures under `backend/tests/fixtures/`.
**Rubric.** Back-end implementation; the one external integration.

---

## T5 — Watchlist API

**Goal.** Add, list and remove watched companies.

**Acceptance criteria**
- [ ] `GET /watchlist` lists companies with their slug and active state
- [ ] `POST /watchlist` adds a company and returns it
- [ ] Adding a slug that already exists is rejected, not silently duplicated
- [ ] Adding a slug that does not resolve at the source is rejected with a clear error
- [ ] `DELETE /watchlist/{id}` removes it and 404s for an unknown id

**Out of scope.** Editing a company; company auto-discovery from a name.
**Constraints.** `backend/app/routes/watchlist.py`.
**Rubric.** Back-end implementation; front-end needs.

---

## T6 — Ingest with idempotent dedupe

**Goal.** Turning fetched postings into rows that never double-count.

**Acceptance criteria**
- [ ] New postings are inserted
- [ ] Re-running the same fetch inserts nothing new
- [ ] A posting whose content changed is updated in place
- [ ] A posting no longer in the source is marked closed rather than deleted
- [ ] A closed posting that reappears is reopened without alerting twice

**Out of scope.** Matching and scoring.
**Constraints.** `backend/app/services/ingest.py`.
**Rubric.** Back-end implementation; database integration.

---

## T7 — Prefilter and the MatchScorer seam

**Goal.** A deterministic prefilter plus the interface the real scorer implements.

**Acceptance criteria**
- [ ] The prefilter rejects on title, seniority, location and must-have keywords, each driven by config
- [ ] Each rejection reason is recorded so a rejection can be explained later
- [ ] `MatchScorer` is a narrow interface with a fake implementation used by tests
- [ ] Tests assert prefilter decisions without any network or API key

**Out of scope.** The LLM call itself.
**Constraints.** `backend/app/matching/`.
**Rubric.** Back-end implementation; testability of the AI core.

---

## T8 — LLM scorer

**Goal.** The real scorer: one call per survivor returning a score, a reason and
the gaps.

**Acceptance criteria**
- [ ] A posting and the active resume produce a score, a why-it-fits string and a gaps list
- [ ] A malformed model response is retried, then reported as a scored failure rather than crashing the run
- [ ] A run scores at most a configured maximum number of postings
- [ ] No API key present yields a clear configuration error, not a stack trace
- [ ] Tests use a stubbed transport and need no key

**Out of scope.** Embeddings or a vector store.
**Constraints.** `backend/app/matching/`; prompt in a template file.
**Rubric.** AI system development; back-end implementation.

---

## T9 — Tick endpoint with background execution

**Goal.** `POST /internal/tick` runs the whole pipeline without blocking a request.

**Acceptance criteria**
- [ ] A request with a wrong or missing shared secret is rejected
- [ ] A valid request returns `202` immediately and does not wait for scoring
- [ ] A run row records status, counts and any error, and the UI can read it
- [ ] A second tick while one is running is refused instead of running concurrently
- [ ] A failure inside the run is captured on the run row, not lost

**Out of scope.** Progress streaming; a queue or worker service.
**Constraints.** `backend/app/routes/internal.py`. Signature from `_docs/decisions/2026-10-11-deploy-and-repo.md`.
**Rubric.** Back-end implementation; the constraint the Jev decision identified.

---

## T10 — Digest email

**Goal.** Send one digest of new matches above the cutoff, and nothing when there
is nothing to say.

**Acceptance criteria**
- [ ] New matches above the cutoff appear in one email, newest first, each with score, reason and gaps
- [ ] Matches below the cutoff are stored and never emailed
- [ ] A run with no new qualifying matches sends no email
- [ ] A posting already emailed is not emailed again in a later run
- [ ] A mailer failure is recorded on the run row without corrupting match state

**Out of scope.** Instant per-posting alerts; digests per company; unsubscribe.
**Constraints.** `backend/app/services/digest.py`; mailer behind an interface, faked in tests.
**Rubric.** Back-end implementation; the product's core promise.

---

## T11 — Frontend scaffold with a centralised API module

**Goal.** A React app whose every backend call lives in one module, mocked for now.

**Acceptance criteria**
- [ ] `npm test` and `npm run dev` both work from a clean checkout
- [ ] Every backend call is defined in exactly one module, with no `fetch` elsewhere
- [ ] The mock backend covers the endpoints the UI needs
- [ ] A test asserts the API module maps a response to the UI's expected shape

**Out of scope.** Real backend wiring.
**Constraints.** `frontend/`.
**Rubric.** Front-end implementation (centralised backend communication).

---

## T12 — Frontend features and their tests

**Goal.** The working UI: watchlist, matches, feedback, and a run-now button.

**Acceptance criteria**
- [ ] A company can be added, listed and removed from the watchlist
- [ ] Matches show score, reason and gaps, and can be saved or dismissed
- [ ] A match already saved or dismissed does not reappear as new
- [ ] "Run digest now" starts a run and the UI shows its outcome
- [ ] Empty, loading and error states are each visible rather than a blank page
- [ ] Unit tests cover the core logic and the README says how to run them

**Out of scope.** Styling polish; auth; mobile layout.
**Constraints.** `frontend/src/`; backend calls only via the module from T11.
**Rubric.** Front-end implementation with tests.

---

## T13 — One container: static build, compose, no CORS

**Goal.** The frontend build served by the backend, and the whole system up with
one command.

**Acceptance criteria**
- [ ] `docker compose up --build --wait` reaches a healthy state with no manual step
- [ ] The UI is served from the same origin as the API
- [ ] No CORS middleware is needed for the UI to work
- [ ] Local compose runs against Postgres, and the app still runs against SQLite outside compose
- [ ] Data survives a container restart

**Out of scope.** Production hosting.
**Constraints.** `Dockerfile`, `compose.yaml`, `.dockerignore`.
**Rubric.** Containerization; reproducibility.

---

## T14 — Integration test suite

**Goal.** A suite separate from the unit tests that exercises the real workflow.

**Acceptance criteria**
- [ ] The suite is a separate command from the unit tests and is documented in the README
- [ ] It runs against a real Postgres, not SQLite
- [ ] It covers upload resume → add company → tick → match → digest recorded, end to end
- [ ] It fails if two ticks run concurrently
- [ ] It makes no live network or LLM call

**Out of scope.** Load or performance testing.
**Constraints.** `backend/tests/integration/`.
**Rubric.** Integration testing.

---

## T15 — CI/CD: test on every push, deploy on pass

**Goal.** GitHub Actions runs the suites and deploys when they pass.

**Acceptance criteria**
- [ ] Every push and pull request runs backend, frontend and integration suites
- [ ] Integration tests run against a Postgres service container
- [ ] The workflow fails if `openapi.json` is stale
- [ ] A push to `main` that passes deploys to the live environment
- [ ] A failing test blocks the deploy
- [ ] A scheduled workflow calls `/internal/tick` with the secret, and the README notes that a missed or late run is expected

**Out of scope.** Preview environments; staging.
**Constraints.** `.github/workflows/`.
**Rubric.** CI/CD pipeline.

---

## T16 — Deploy to Render and Neon

**Goal.** A public URL a reviewer can open.

**Acceptance criteria**
- [ ] The app is reachable at a public HTTPS URL
- [ ] The database is Neon Postgres, not Render's 30-day free database
- [ ] The README documents the deployment steps and the environment variables
- [ ] The resume upload works on the deployed instance
- [ ] The README states the 15-minute idle spin-down and the ~1 minute cold start

**Out of scope.** Custom domain; scaling; paid plans.
**Constraints.** Deploy target per `_docs/decisions/2026-10-11-deploy-and-repo.md`.
**Rubric.** Deployment.

---

## T17 — MCP server for the watchlist and matches

**Goal.** Bounded MCP tools so a coding agent can drive the app.

**Acceptance criteria**
- [ ] MCP tools expose adding a company, listing matches and triggering a run
- [ ] Each tool is documented with the arguments it accepts
- [ ] A tool call against the running app is demonstrated in the README
- [ ] The MCP server holds no credentials of its own beyond the app's URL and secret

**Out of scope.** Write access to resume contents; arbitrary SQL.
**Constraints.** `mcp/`.
**Rubric.** AI system development (the MCP point).

---

## T18 — README and the AI-assisted workflow write-up

**Goal.** The README a peer reviewer needs, including how AI was used.

**Acceptance criteria**
- [ ] The problem, the users and what the system does are stated up front
- [ ] Architecture, technologies and how they fit together are explained
- [ ] The AI-assisted workflow is documented, citing `_docs/decisions/` and the role prompts
- [ ] Deliberately excluded sources and their reasons are stated, not glossed
- [ ] Setup, test and deploy instructions work from a clean checkout
- [ ] Known limitations and the live URL are listed

**Out of scope.** A separate marketing page.
**Constraints.** `README.md`.
**Rubric.** Problem description; AI system development; technologies and architecture; reproducibility.
