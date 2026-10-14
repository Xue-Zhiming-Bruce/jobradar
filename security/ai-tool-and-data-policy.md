# AI tool and data policy (#82)

Which data an AI tool or an agent working on this repository may see, which it
may not, and where each of those rules is enforced rather than promised.

## The data classes

- **The resume's text and its file: user data.** It is uploaded at runtime by
  whoever runs the instance, it belongs to that person, and it is the one class
  here that is both personal and unbounded.
- **`.env` and every deployment variable: secrets.** `TICK_SECRET`,
  `MCP_AUTH_TOKEN`, `LLM_API_KEY`, `DATABASE_URL` and `SMTP_PASSWORD` are values,
  not names; the names are the configuration table's and `.env.example`'s.
- **The database: the rows, which include the resume's text.** A row holds a
  person's resume as a column value, so a database dump is user data whatever it
  is called. Under compose it is a named volume; locally it is
  `backend/data/jobradar.db`.
- **The repository itself: public.** Everything tracked here is readable by
  anyone, including every prompt, note, issue and comment an agent writes.

## What an agent here may and may not read

May read: this repository and everything tracked in it; the synthetic fixtures
(`backend/tests/fixtures/`, and the sentinels the suite assembles from parts);
and `.env.example`, whose whole content is variable **names** with placeholders.

May not read: a real `.env`; `~/.jobradar/uploads` (or any other `UPLOADS_DIR`);
a production database or any dump of one; and a real resume in any form.

A real resume's text never goes into a commit, a log line, an issue, a prompt or
a test fixture. That sentence is the rule the enforcement points below exist to
keep true, and it is a data rule before it is an AI rule: `AGENTS.md` and
`_docs/process.md` state it for every author, human or agent.

## Where each rule is enforced

| the rule | the enforcement, in code or in a test |
| --- | --- |
| no secret, resume or database file reaches the repository | `backend/tests/test_committed_data_guard.py` reads the git index -- the tracked paths and their staged content -- and fails the build on a staged `.env`, a `*.db`/`*.sqlite3` path, anything under `uploads/` or `data/`, a resume file, a credential shape, or a non-empty value assigned to a secret-shaped name. It runs in the default suite and as one step of `ci.yml`'s `backend` job. |
| a resume's text reaches no log record | `backend/tests/test_resume_text_logging.py`: its docstring is the reason -- SQLAlchemy logs a statement's parameters and a result's rows at `DEBUG`, `app/db.py` holds the ORM's own loggers at `WARNING` while the engine is built, and the reads that do not need the text do not select it -- and its cases drive the upload and every read with the loggers at `DEBUG`. |
| no secret value is written into a tracked file | `backend/tests/test_env_example.py` compares the names in `.env.example` with the names the code reads, in both directions, so a variable added to the code with no line in the file is a failing test; the file holds names and empty values only, and the guard above fails on a value. |
| a local `.env`, database or upload never reaches an image | `.gitignore` and `.dockerignore`, with `backend/tests/test_dockerignore.py` modelling the ignore rules against the trees the image needs. |
| resume text leaves the process only on the one path that needs it | `backend/app/matching/scorer.py` is the only outbound request that carries resume text: one `POST` to `<LLM_BASE_URL>/chat/completions` with `LLM_MODEL`, authenticated with `LLM_API_KEY`. The suite never makes that call -- it drives `FakeScorer`, which no socket. |

Each of those is a check that fails a build or an ignore that keeps a path out,
which is what makes this a policy rather than an intention.

## The limit, stated honestly

Nothing in this repository can stop an agent from reading a file on the machine
it runs on, and nothing can stop it from putting text into a prompt: those are
properties of the tool and the machine, not of a file here. What is enforced is
what can **leave** -- the public repository, the image, a log record, and the
provider call above. Two things are therefore outside this policy's reach: the
model provider's own handling of a request that carries resume text, which is
that provider's policy, and what a deployment's own variables do at runtime,
which is #16's, and #16 is still open.
