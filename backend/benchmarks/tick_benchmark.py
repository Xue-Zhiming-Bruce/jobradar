"""What one tick costs, measured: a synthetic watchlist, a real PostgreSQL, the real pipeline.

`cd backend && TEST_DATABASE_URL=... uv run python benchmarks/tick_benchmark.py
--companies 10 --postings 50` drives one whole tick -- watchlist, fetch, ingest,
prefilter, score, record, digest -- and prints the numbers `_docs/performance.md`
records. It is deliberately not a pytest case: a wall-time run in the default
suite is a flaky gate, and `pyproject.toml`'s `testpaths` is `tests`, so nothing
here is collected (`check_own_module()` below still asserts the two rules this
file has to keep).

Two things are faked and everything else is the app's. The board is
`SyntheticBoard`, injected as the `sources` mapping, and the model is a
`FakeScorer`, injected as the scorer: `pipeline.run_pipeline` and
`digest.send_digest` are the real ones, so the writes, the dedupe, the prefilter,
the counts, the marker and the message are the code the app runs. No board and no
model is reached, no key is read, and `TEST_DATABASE_URL` is the only variable
this script reads -- setting `DATABASE_URL` from it is how the app's own
`db.get_engine()` is pointed at the throwaway server, the way
`tests/integration/conftest.py` does.

PostgreSQL is required rather than preferred, and its absence is loud: an unset
or empty `TEST_DATABASE_URL` exits non-zero naming the variable and the
`docker run postgres:17` line, and a URL that names another engine is refused
before a row is written, because SQLite timings say nothing about the engine the
app deploys on. The database is a throwaway one and this script owns the five
table names in it: the tables are emptied before and after every run, so a second
run starts from empty. Nothing is written to a file -- not under `backend/data/`,
not under `~/.jobradar` -- so `git status --short` is clean after a run.
"""

import argparse
import ast
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The documented command runs this file as a path, which puts `benchmarks/` on
# `sys.path` and not `backend/`; `app/` lives beside this script's directory, so
# the backend root is added before the app is imported. pytest's `pythonpath`
# setting does the same for the suite and covers no script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db
from app.matching.scorer import FakeScorer, MatchResult
from app.models import Base, Company, Match, Posting, Run
from app.services import resume
from app.services.digest import FakeMailer, send_digest
from app.services.pipeline import run_pipeline
from app.sources.base import SourcePosting

# The one variable this script reads, and the database it is pointed at. The
# variable's name and the server it must name are the integration suite's, and so
# is the throwaway command: one Postgres serves both, on port 5433 because 5432
# is taken on this machine.
TEST_DATABASE_URL = "TEST_DATABASE_URL"
DATABASE_URL = "DATABASE_URL"
POSTGRES = "postgresql"

THROWAWAY_POSTGRES = (
    "docker run --rm -d --name jobradar-test-db -p 5433:5432 "
    "-e POSTGRES_USER=jobradar -e POSTGRES_PASSWORD=jobradar-throwaway "
    "-e POSTGRES_DB=jobradar postgres:17"
)
THROWAWAY_TEST_DATABASE_URL = (
    "postgresql+psycopg://jobradar:jobradar-throwaway@127.0.0.1:5433/jobradar"
)

# The defaults `--help` documents: the scale criteria 1 names, so the command in
# the issue and the command in `README.md` are the same run.
DEFAULT_COMPANIES = 10
DEFAULT_POSTINGS = 50

# The scale's seed: slugs are generated from it, so the same command measures the
# same watchlist every time and two runs compare.
SEED = 41

# The active resume the real pipeline reads once per run: synthetic text, and a
# path no file is ever written to or read from -- the row's `stored_path` only
# names where an upload would live.
RESUME_TEXT = "Synthetic benchmark resume: Python, PostgreSQL and tests."
RESUME_PATH = Path("synthetic-benchmark-resume.txt")

# The result the fake model returns for every survivor: at or above the digest's
# default cutoff of 70, so the real digest selects, emails and marks rows instead
# of finding nothing to send.
MATCH_RESULT = MatchResult(
    score=85, reason="Synthetic benchmark fit.", gaps=["Synthetic benchmark gap."]
)

# The two `run.status` values this script writes, spelled the way
# `routes.internal` and the check constraint spell them. #9's `_run` belongs to
# the route, so this script writes its own row's end.
RUNNING = "running"
SUCCEEDED = "succeeded"

# The modules this script must not import, and the rule `check_own_module()`
# asserts from this file's own text, the way `tests/test_pipeline.py` reads
# `app/services/pipeline.py`.
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}


class SyntheticBoard:
    """The faked board: `postings` postings per slug, from a seed, and no network anywhere.

    It has the `Source` shape -- one call, one slug in, that board's postings out --
    so it is injected as the `sources` mapping the real pipeline walks. Every slug
    it is asked for is recorded, which is the check that the walk really covered
    the watchlist.
    """

    def __init__(self, postings: int) -> None:
        self.postings = postings
        self.calls: list[str] = []

    def __call__(self, slug: str) -> list[SourcePosting]:
        """This slug's synthetic board: `postings` postings with deterministically unique ids."""
        self.calls.append(slug)
        return [
            SourcePosting(
                external_id=f"{slug}-{index}",
                title=f"Synthetic Engineer {index}",
                location="Berlin",
                url=f"https://example.test/{slug}/{index}",
                description=f"Synthetic posting {index} for {slug}.",
            )
            for index in range(self.postings)
        ]


@dataclass(frozen=True)
class Report:
    """What one measured run recorded, read back from the database rather than counted here."""

    wall_seconds: float
    postings_seen: int
    matches_created: int
    matches_emailed: int | None
    posting_rows_before: int
    posting_rows_after: int
    match_rows_before: int
    match_rows_after: int
    status: str


def check_own_module() -> None:
    """The rules this file cannot be trusted to keep by inspection, asserted from its own text.

    Criterion 3 asks for a test of two properties; the benchmark is not a pytest
    module (`testpaths` is `tests`), so the check lives here and runs on every
    invocation. The imported roots are this file's own, so `app.matching.scorer`'s
    own `httpx2` import is unaffected; the environment names are resolved through
    this module's constants, so `os.environ.get(TEST_DATABASE_URL, "")` is seen as
    the read it is; and `DATABASE_URL` may be written, because pointing the app's
    own `db.get_engine()` at the throwaway server is the one write this script makes.
    """
    path = Path(__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_roots(tree) & NETWORK_MODULES
    if imported:
        raise SystemExit(f"{path.name} imports a network client: {sorted(imported)}")
    reads, writes = _environment_names(tree)
    if reads != {TEST_DATABASE_URL}:
        raise SystemExit(
            f"{path.name} reads {sorted(reads)}: {TEST_DATABASE_URL} is the only variable it may read"
        )
    if writes - {DATABASE_URL}:
        raise SystemExit(
            f"{path.name} writes {sorted(writes - {DATABASE_URL})}: it may only set {DATABASE_URL}"
        )


def main(argv: list[str] | None = None) -> int:
    """Measure one tick at the scale asked for, print what it recorded, and leave no row behind."""
    check_own_module()
    arguments = _arguments(argv)
    current = _test_database_url()
    os.environ[DATABASE_URL] = current
    engine = db.get_engine()
    _require_postgres(engine)
    # The tables exist first, so an unreachable server is the app's own named error
    # rather than a traceback from the first statement, and then this run starts on
    # five empty tables.
    db.init_db()
    _reset_tables(engine)
    try:
        report = _measure(arguments.companies, arguments.postings)
    finally:
        _reset_tables(engine)
        engine.dispose()
    _print_report(arguments, report)
    return 0


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    """The two scale arguments and their documented defaults, and nothing else."""
    parser = argparse.ArgumentParser(
        description=(
            "Measure one real tick -- watchlist, fetch, ingest, prefilter, score, record, "
            "digest -- against a real PostgreSQL, with a synthetic board and a fake scorer."
        ),
        epilog=(
            f"Needs a throwaway PostgreSQL in {TEST_DATABASE_URL}, with no key and no mail "
            f"credential anywhere:\n\n    {THROWAWAY_POSTGRES}\n"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--companies",
        type=_positive,
        default=DEFAULT_COMPANIES,
        help="active companies in the synthetic watchlist",
    )
    parser.add_argument(
        "--postings",
        type=_positive,
        default=DEFAULT_POSTINGS,
        help="postings each company's synthetic board answers with",
    )
    return parser.parse_args(argv)


def _positive(value: str) -> int:
    """One scale argument: a whole number of at least 1, refused rather than run as nonsense."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive count")
    return number


def _test_database_url() -> str:
    """`TEST_DATABASE_URL`, or an exit naming the variable and the command that provides a server.

    Read the way `tests/integration/conftest.py` reads it and, like that suite,
    failing rather than skipping: there is no SQLite fallback, because timings on
    another engine say nothing about the one the app deploys on.
    """
    url = os.environ.get(TEST_DATABASE_URL, "").strip()
    if url:
        return url
    raise SystemExit(
        f"{TEST_DATABASE_URL} is unset or empty: this benchmark drives a real tick against a "
        f"real PostgreSQL server and fails rather than skipping without one. Start a "
        f"throwaway server:\n\n    {THROWAWAY_POSTGRES}\n\nand run the benchmark at it:\n\n"
        f"    cd backend && {TEST_DATABASE_URL}={THROWAWAY_TEST_DATABASE_URL} \\\n"
        f"        uv run python benchmarks/tick_benchmark.py --companies {DEFAULT_COMPANIES} "
        f"--postings {DEFAULT_POSTINGS}\n"
    )


def _require_postgres(engine: Engine) -> None:
    """Refuse an engine that is not PostgreSQL, before a single row is written."""
    if engine.dialect.name != POSTGRES:
        raise SystemExit(
            f"{TEST_DATABASE_URL} names {engine.dialect.name}, not {POSTGRES}: this benchmark "
            f"measures the engine the app deploys on, and was pointed at another one"
        )


def _reset_tables(engine: Engine) -> None:
    """Empty the five tables, so a run starts from nothing and leaves nothing behind.

    This script owns these table names in the throwaway database it was given, the
    way `tests/integration/conftest.py` does: rows a previous run left would make
    this run's numbers incomparable, and rows this run leaves would make the next
    one's.
    """
    Base.metadata.drop_all(engine)
    db.init_db()


def _measure(companies: int, postings: int) -> Report:
    """One whole tick at that scale, timed, with the numbers read back from the database.

    The clock starts before the first fetch and stops after the digest returned:
    the run row, the resume and the watchlist are written first, so what is
    measured is the tick and not the seeding.
    """
    _watchlist(companies)
    _active_resume()
    board = SyntheticBoard(postings)
    run_id = _start_run()
    posting_rows_before = _rows(Posting)
    match_rows_before = _rows(Match)

    started = time.perf_counter()
    run_pipeline(run_id, sources={"greenhouse": board}, scorer=FakeScorer(MATCH_RESULT))
    send_digest(FakeMailer(), run_id)
    wall_seconds = time.perf_counter() - started

    _end_run(run_id)
    status, postings_seen, matches_created, matches_emailed = _recorded(run_id)
    return Report(
        wall_seconds=wall_seconds,
        postings_seen=postings_seen,
        matches_created=matches_created,
        matches_emailed=matches_emailed,
        posting_rows_before=posting_rows_before,
        posting_rows_after=_rows(Posting),
        match_rows_before=match_rows_before,
        match_rows_after=_rows(Match),
        status=status,
    )


def _watchlist(companies: int) -> None:
    """`companies` active Greenhouse companies, generated from this module's seed."""
    with db.session_scope() as session:
        for index in range(companies):
            session.add(
                Company(
                    name=f"Synthetic Company {index}",
                    slug=f"benchmark-{SEED}-{index}",
                    source="greenhouse",
                )
            )


def _active_resume() -> None:
    """The one active resume a run needs, stored through #3's own writer and no file."""
    resume.record_active_resume(
        "synthetic-benchmark-resume.txt", resume.TEXT_PLAIN, RESUME_TEXT, RESUME_PATH
    )


def _start_run() -> int:
    """One `run` row in `running` with both counts 0, written the way #9's route writes it."""
    with db.session_scope() as session:
        run = Run(status=RUNNING)
        session.add(run)
        session.flush()
        return run.id


def _end_run(run_id: int) -> None:
    """Write this run's own end the way #9's `_end_run` does: the status and the instant."""
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        run.status = SUCCEEDED
        run.finished_at = datetime.now(UTC)


def _recorded(run_id: int) -> tuple[str, int, int, int | None]:
    """The run's own four numbers as plain values, read while its session is open."""
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        return run.status, run.postings_seen, run.matches_created, run.matches_emailed


def _rows(model: type) -> int:
    """How many rows one table holds, so what the run wrote is read and not counted here."""
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(model))


def _print_report(arguments: argparse.Namespace, report: Report) -> None:
    """The scale, the wall time, and the run's own numbers: one labelled line each."""
    lines = (
        ("scale", f"{arguments.companies} companies x {arguments.postings} postings"),
        ("tick wall time", f"{report.wall_seconds:.1f} s"),
        ("run.postings_seen", report.postings_seen),
        ("run.matches_created", report.matches_created),
        ("run.matches_emailed", report.matches_emailed),
        (
            "posting rows",
            f"{report.posting_rows_before} -> {report.posting_rows_after}",
        ),
        ("match rows", f"{report.match_rows_before} -> {report.match_rows_after}"),
        ("run.status", report.status),
    )
    for label, value in lines:
        print(f"{label + ':':<22}{value}")


def _imported_roots(tree: ast.AST) -> set[str]:
    """The top-level names this module's text imports."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _environment_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Every variable this module's text reads and writes through `os.environ`, as names.

    Names are resolved through this module's own string constants, because
    `os.environ.get(TEST_DATABASE_URL, "")` names the variable without spelling it
    at the call.
    """
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

    def named(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return constants.get(node.id) if isinstance(node, ast.Name) else None

    reads: set[str] = set()
    writes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            name = named(node.slice)
            if name is not None:
                (reads if isinstance(node.ctx, ast.Load) else writes).add(name)
        elif isinstance(node, ast.Call) and _is_environ(node.func) and node.args:
            name = named(node.args[0])
            if name is not None:
                reads.add(name)
    return reads, writes


def _is_environ(node: ast.expr) -> bool:
    """Whether a node is `os.environ` itself or one call or attribute read on it."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute) and node.attr != "environ":
        node = node.value  # a method or a key on it, `os.environ.get` say
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr == "environ"
    )


if __name__ == "__main__":
    raise SystemExit(main())
