# Decision record — the CI/CD third tier: declined

Date: 2026-10-18
Decided by: the owner, as issue #103's cost call ("the outcome is the owner's
call, not the PM's and not the implementer's"); recorded by the software
engineer implementing #103.

## Why this file exists

The course rubric's CI/CD answer has tiers, and this project has only ever
written down the lower one. The line the rubric grades the top of it with is,
in the rubric's own words (`project/README.md`):

> CI runs tests, CD deploys to dev, and there's a manual deploy to production

That is the rubric's **3-point line**. This project's CI/CD sits on the
**2-point line**: "GitHub Actions runs the test suites, and deploys when they
pass" (`_docs/plan.md`). Requiring a second service and a second database to
climb one point is a cost decision, so it is recorded here rather than left as
an omission a reviewer would read as unfinished work.

## The decision

**Declined.** The project does not build a dev environment and does not turn
production deploys into a manual action. It claims the rubric's 2-point CI/CD
line in full and consciously leaves the third tier's point on the table.

## The CI/CD this project has

- **`.github/workflows/ci.yml`** runs five jobs — `backend`, `frontend`,
  `integration`, `contract`, `mcp` — on every push and every pull request, with
  `permissions: contents: read` and no credential beyond the throwaway Postgres
  password the README already publishes for an ephemeral container.
- **`.github/workflows/deploy.yml`** deploys the revision CI passed. Its
  `on.workflow_run` trigger plus the job's `if` admit only a successful `CI`
  run that was a `push` to `main`, so a red or cancelled CI run leaves the job
  `skipped` and no request reaches Render; the step deploys
  `github.event.workflow_run.head_sha`, not the possibly-newer tip of `main`,
  and fails if no deploy of that commit appears. Credentials arrive as
  `secrets.RENDER_API_KEY` and `secrets.RENDER_SERVICE_ID` and the health check
  reads `vars.JOBRADAR_URL`; no value of any is written down.
- **`ops/rollback.sh`** is the rollback path (#89), described in the README's
  `### Rollback` section.

This is the "test then deploy" line `_docs/plan.md` already states, and it takes
that line's score in full: the rubric asks for a pipeline that runs the tests
and deploys the result, and this one does, from a green `main` with no human in
the loop.

## The cost that is not paid

- **A second service.** Dev would be another Render web service on the same
  image, on a free plan that spins down on idle, so it is a second thing to keep
  awake and a second cold start to explain.
- **A second database.** Dev would need a Postgres of its own
  (`DATABASE_URL` pointing at it, not production's), holding nothing that
  matters — no real uploaded resume, no production credential.
- **Production deploy becoming a human action.** Today a green `main` deploys
  itself (`deploy.yml`, and the README's `### Rollback` opens with exactly that
  sentence). A manual production deploy adds a `workflow_dispatch` path, which
  is what `deploy.yml`'s header comment refuses today: "a manual dispatch would
  be a path where a failing test does not block a deploy." The tier is not free
  — it buys one rubric point by moving a safety property out of the pipeline.

Not paid because the project has one user, one environment and no data that
matters in dev, so a dev environment would exist to be reviewed rather than to be
used.

## Ready to roll back, whichever way it was deployed

`ops/rollback.sh` selects by commit, not by workflow: it reads Render's newest
deploys, takes the `commit.id` of the newest `live` deploy as the commit being
served, and picks as its target the newest deploy that is another commit whose
build succeeded (#89). That is the same call `deploy.yml` makes, so a commit
deployed by hand reverts by the same script as one deployed by the workflow.
Declining the tier changes nothing about that script. The mechanics are in
`ops/rollback.sh` and the README's `### Rollback`; this record does not restate
them.

## When this would be revisited

The decision would be revisited if the project gained more than one user or more
than one environment — that is, if a deploy needed to be tested by someone other
than the person who merged it, or if a change ever had to be exercised against
real data before production. The shape then is the one issue #103 describes: dev
is a second service on the same image with a database of its own and no
production credential, a green `main` deploys dev automatically, and production
is deployed only by an explicit owner action. Until one of those conditions
holds, the second service, the second database and the manual production deploy
are cost without a user.

## Not decided here

The automatic production deploy on a green `main` (#45), the rollback path
(#89), the deployed instance and its Render and Neon configuration (#16), and
the live evidence in the README (#46). This record covers only whether the
rubric's third CI/CD tier is taken; it changes no workflow, no script and no
infrastructure. The deploy target and the public repository are
`_docs/decisions/2026-10-11-deploy-and-repo.md`.
