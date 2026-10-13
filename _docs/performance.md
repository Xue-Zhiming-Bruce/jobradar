# Performance: what one tick costs

This note records one measurement: what a whole tick -- watchlist, fetch, ingest,
prefilter, score, record, digest -- costs against a real PostgreSQL, over a
synthetic watchlist, with the board and the model faked at the seams the app
already has. It answers the question `_docs/plan.md` left open when it fixed
`SCORER_MAX_POSTINGS` (50) and made `POST /internal/tick` answer `202` "because a
tick that scores many postings can outlive an HTTP timeout" -- a design decision
that until now had no measured number behind it.

The measurement is `backend/benchmarks/tick_benchmark.py`, and it is a script
rather than a pytest case on purpose: a wall-time run in the default suite is a
flaky gate, and `pyproject.toml`'s `testpaths` is `tests`, so pytest never
collects it. What it drives is the app: the real `pipeline.run_pipeline` and the
real `digest.send_digest`, so the writes, the dedupe, the prefilter, the counts,
the marker and the message are the code the app runs. Only the board (a synthetic
`sources` mapping) and the model (a `FakeScorer`) are fakes, and no board, no
model, no key and no mail credential is reached.

## The machine and the command

The numbers below were produced on one machine, the app and the database sharing
it over the loopback:

- Apple M3, 16 GiB, macOS 27.0.1 (build 26A434);
- Python 3.12.12, through `uv run`;
- PostgreSQL 17.11, in a `postgres:17` container on `127.0.0.1:5433` (Docker
  Desktop, on this host), the throwaway server the README and the integration
  suite name -- on 5433 because 5432 is taken on this machine.

    docker run --rm -d --name jobradar-test-db -p 5433:5432 \
      -e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway \
      -e POSTGRES_DB=jobradar postgres:17
    cd backend && TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar \
      uv run python benchmarks/tick_benchmark.py --companies 10 --postings 50
    docker rm -f jobradar-test-db

A different scale is the same command with different `--companies`/`--postings`
values; `--help` documents both and their defaults (10 and 50). The script prints
the wall time and does not assert on it -- the assertions in this task are about
counts, never about how fast the machine is.

## The runs

Every value in this table is one the script printed, read back from the database
after the run rather than counted in the script's own bookkeeping. Wall time is
`time.perf_counter()` from before the first fetch to after the digest returned,
in seconds with one decimal. Rows are the `posting` and `match` table counts
before and after the run; the tables are emptied before and after it, so
`before` is always 0 and a second run is comparable.

| scale (companies x postings) | postings | wall time | postings_seen | matches_created | matches_emailed | posting rows | match rows |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5 x 20 | 100 | 0.2 s | 100 | 100 | 100 | 0 -> 100 | 0 -> 100 |
| 10 x 50 | 500 | 0.9 s | 500 | 500 | 500 | 0 -> 500 | 0 -> 500 |
| 20 x 50 | 1000 | 1.7 s | 1000 | 1000 | 1000 | 0 -> 1000 | 0 -> 1000 |
| 40 x 50 | 2000 | 3.3 s | 2000 | 2000 | 2000 | 0 -> 2000 | 0 -> 2000 |

Every run ended `succeeded`, and the four runs above are the four runs that were
performed -- no value here is extrapolated.

Two properties of these runs are the setup's and not the app's. `matches_created`
equals `postings_seen` because the fake model returns one result at 85 for every
survivor and no prefilter term was set, so nothing was rejected and nothing fell
below `DIGEST_MIN_SCORE`'s default of 70; `matches_emailed` equals it because one
run sends one message carrying every qualifying match. The script reads only
`TEST_DATABASE_URL`, but the app reads its own prefilter terms and cutoff per
call, so a shell that exports any of them yields different counts in those three
columns for the same scale -- the wall time is the number this note reads.

The cost is linear in the postings: 500 postings took 0.9 s and 2000 took 3.3 s,
which is 1.8 ms and 1.65 ms per posting -- the row's insert, the prefilter, the
`match` write, the two `run` counter updates and the digest's own select, write
and markup, per posting. Call it 1.7 ms of app-side work per posting.

## What the numbers mean for the three constants

**`SCORER_MAX_POSTINGS` (50).** 50 postings at the measured 1.7 ms each is about
0.09 s of app work, so the pipeline is not why the cap exists: the cap bounds
*model calls*, and one model call costs what this benchmark deliberately does not
measure. What the cap is worth is the worst case it stops: at
`MODEL_TIMEOUT_SECONDS` (30 s) per call, 50 postings is 1500 s -- 25 minutes -- of
hung model time before the run ends, and with `LLM_FALLBACK_MODEL` set each model
gets its own `SCORER_MAX_ATTEMPTS` budget, so the same run can reach 2 x 50 x 30 s
= 3000 s. 50 is therefore a bound on the model's worst case and not on the tick's
own work, which at that scale is 0.09 s; whether it is a bound the tick can live
with in wall time is decided entirely by the model's real per-call latency, which
is not in this measurement.

**The tick's HTTP budget.** The design answers `202` immediately because the
request must never wait for the run: the benchmark measures the run itself
(0.2 s to 3.3 s above), and the request's own work is writing one `run` row, which
is not in these numbers at all. The caller's timeout is `tick.yml`'s
`curl --max-time 60` on the `POST`, and the `202` design keeps the request far
inside it whatever the run costs. What `202` does not do is bound the run: the
run holds the process's in-memory guard until it finishes, and
`reap_stale_runs()` -- called once at process start, in `app.main`'s lifespan --
finishes as `failed` any row still `running`. So a run that outlives its process
(a restart or a redeploy mid-run) is reaped as failed and its matches go out with
the next successful run: the run's horizon is the process's lifetime, not a
timeout the platform applies. At the measured app-side cost that horizon would
need about 35 000 postings in one run to be reached (see below); the model is what
makes a run long, not the pipeline.

**`MODEL_TIMEOUT_SECONDS` and `SMTP_TIMEOUT_SECONDS` (30 s each, per call).** One
run makes at most `SCORER_MAX_POSTINGS` model calls -- 50 by default, plus a
second budget each with the fallback -- and exactly one `send`. The measured
app-side work of a whole 500-posting run is 0.9 s, about a thirtieth of a single
model timeout: 30 s of timeout, not 0.9 s of app work, is what a run's duration is
made of. The ceiling those two constants put on one run is therefore
50 x 30 s = 1500 s (3000 s with a fallback) before the digest's one `send` adds
its own 30 s. A timeout that moves down moves that ceiling down proportionally and
makes a hung model cost less of the run; neither constant is anywhere near the
measured per-posting cost.

## Where the numbers stop being acceptable

The stated budget to measure against is the deployment's own: the caller's 60 s
(`tick.yml`'s `--max-time 60`) applies to the `POST`, which never waits, so the
budget that a *run* has to fit inside is the process lifetime before a restart
reaps it as failed -- the reap has no time horizon of its own.

At the measured 1.7 ms per posting, the app's own work reaches 60 s only at about
35 000 postings in one run: 700 companies with 50-posting boards, or 50 companies
with 700-posting boards. That is 70 times the default watchlist this benchmark was
asked about, and 700 times `SCORER_MAX_POSTINGS`, so in practice the cap is what
stops a run growing, not the pipeline's throughput: a run scoring 50 postings
spends about 0.09 s in the app. Nothing here says the system is fast; it says the
app-side cost per posting is 1.7 ms, and it names the scale at which that alone
would matter.

The constant that has to move first is `SCORER_MAX_POSTINGS`, because it is the
only one of the three that bounds the number of *calls* a run makes -- the two
30 s timeouts bound one call each, and the digest sends one message per run
whatever the match count. If a run's ceiling of 1500 s (3000 s with a fallback)
is too long for the process lifetime a deployment gives it, `SCORER_MAX_POSTINGS`
comes down; `MODEL_TIMEOUT_SECONDS` is the second lever, for the case where a
single hung call is the problem rather than the number of them. Neither change is
this task's: the scorer's configuration is #32's and the digest's constant is
#10's, and this measurement changes no constant.

## What this did not measure

- **A real board's latency.** The synthetic source answers in-process; a real
  Greenhouse board's round trip, its ten-second `REQUEST_TIMEOUT_SECONDS` and its
  payload sizes are not in these numbers.
- **A real model's latency.** `FakeScorer` returns at once. The per-posting model
  call is the dominant cost of a real run and the reason `SCORER_MAX_POSTINGS`
  exists; measuring it belongs to a deployment (#16), not to a synthetic
  benchmark.
- **A real mail relay.** `FakeMailer` records the message; `SMTP_TIMEOUT_SECONDS`
  bounds a send that is not in this measurement.
- **Concurrent ticks and pacing.** One run, alone: #56 owns a run's outbound
  request budget.
- **The frontend.** No page timing, no bundle size; the graded exercise asks for
  none.
- **The deployment's machine.** These are one laptop's numbers, with the app and
  PostgreSQL sharing a CPU and a disk over the loopback; a deployed run on other
  hardware will differ, and the shape (1.7 ms per posting) is the part worth
  carrying over.
