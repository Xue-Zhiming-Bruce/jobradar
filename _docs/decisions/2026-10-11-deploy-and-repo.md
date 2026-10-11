# Decision record — deploy target, repository, digest trigger

Date: 2026-10-11
Decided by: Jev (`typesafe/jev-1.13-20260917` via OpenRouter), questions reviewed and
escalations resolved by the orchestrator.

## Why this file exists

The project rubric awards a point for describing how AI tools were used to build
the system, and a second for using MCP. Handing a bounded architecture decision to
Jev, recording the exact request and verdict, and recording what happened to the
escalations is the evidence for both.

## What was asked

The full request is preserved at `jev-request.json`; the raw verdicts at
`jev-verdicts.json`. State given to Jev: single-user scope, Greenhouse-only source,
prefilter-then-LLM scoring, one container, poller as an HTTP endpoint, and the
verified free-tier constraints (Render spins down after 15 min idle; Render free
Postgres is deleted 30 days after creation; GitHub Actions cron drifts by minutes
to hours, can skip entirely, and is disabled after 60 days of repo inactivity).

## Verdicts

| Question | Verdict | Confidence | Escalated |
| --- | --- | --- | --- |
| Deploy target | `render_plus_neon` (0.98) | 0.97 | no |
| Repository | `public_github` (1.00) | 1.00 | no |
| Digest trigger | `external_cron` (0.53) vs `button_primary` (0.47) | 0.30 | **yes** — unsure |
| Is single-container poller sound? | 0.55 | 0.10 | **yes** — unsure |

## Resolutions

**Deploy: Render free web service + Neon Postgres.** Accepted as decided. Render
spins down on idle and its free database is deleted after 30 days, which would
break a review window that starts weeks after submission; Neon's free Postgres does
not expire, so the database lives outside Render.

**Repository: public on GitHub.** Accepted. This overrides the skill's default of a
private repository, because peer reviewers must read the code and the README to
score the project. Consequence: nothing private may ever be committed. The resume
upload path is therefore built so that uploads are stored outside the repository and
never logged.

**Digest trigger.** Jev escalated at 0.30 confidence, so the answer is a prior, not a
decision. Resolution: the in-app "Run digest now" button is the reliable path; a
GitHub Actions scheduled workflow is a best-effort convenience with its drift
documented in the README. An external cron service is recorded as an optional
upgrade, noting the side benefit that a frequent ping would also keep the Render
instance warm and hide the cold start from reviewers. Not adopted for v1 because it
adds a third-party dependency and a second place a secret lives.

**Single-container poller.** Jev answered 0.55 at 0.10 confidence and escalated, so
this is unresolved by Jev and resolved here. The design stands, with one constraint
it implies: a tick that scores many postings can outlive an HTTP timeout, so
`POST /internal/tick` starts the work as a background task, returns `202`
immediately, and records a run row that the UI reads for progress and results. A
request that blocks on scoring is the failure mode Jev was right to be unsure about.
