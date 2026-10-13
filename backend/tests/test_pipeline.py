"""What one digest run does: the walk, the fetch, the ingest, the gate, the score and the counts.

Every case here runs with `socket.socket.connect` patched to raise the way
`tests/test_greenhouse_source.py` does, so a run that reached for a board fails
instead of quietly passing, and with the model's three variables, the prefilter's
five and the walk's own two deleted, so a key, a term or a request bound in the
developer's shell cannot decide a case. The source and the scorer are this module's own fakes: the source
records every slug it was asked for and answers from a script, and the scorer
records every `(posting, resume_text)` pair it was given.

The row-level cases ask for the shared `engine` fixture of `tests/conftest.py`,
so each runs on SQLite and on PostgreSQL, and the run row they exercise is
written the way #9's route writes it, `running` with both counts 0 -- the
pipeline fills the counts and never the lifecycle, which is what makes the row
still read `running` after a run that failed inside it. Two cases drive the real
`POST /internal/tick` instead, through `TestClient` and with the starter replaced
by one that runs the work inline: they are what proves the tick's own provider
runs this pipeline, and that a run's log lines, rows and response carry none of
the resume's text.

The resume is synthetic, and its text is a sentinel: no case reads a real one,
sets a key or opens a socket.
"""

import ast
import inspect
import json
import logging
import socket
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db
from app.main import app
from app.matching.prefilter import (
    PREFILTER_LOCATION_TERMS,
    PREFILTER_MUST_HAVE_FROM_RESUME,
    PREFILTER_MUST_HAVE_KEYWORDS,
    PREFILTER_SENIORITY_TERMS,
    PREFILTER_TITLE_TERMS,
    REJECTION_REASON_PREFIX,
)
from app.matching.scorer import (
    LLM_API_KEY,
    LLM_MODEL,
    NOT_SCORED_PREFIX,
    SCORER_MAX_POSTINGS,
    FakeScorer,
    MatchResult,
    ScorerConfigurationError,
)
from app.models import Company, Match, Posting, Run, RunCompany
from app.routes.internal import TICK_SECRET_HEADER, get_pipeline, get_run_starter
from app.services import pipeline as pipeline_module
from app.services import resume
from app.services.ingest import IngestResult
from app.services.pipeline import (
    DEFAULT_REQUEST_BUDGET,
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    NO_ACTIVE_RESUME,
    RUN_REQUEST_BUDGET_VARIABLE,
    RUN_REQUEST_INTERVAL_SECONDS_VARIABLE,
    run_pipeline,
)
from app.sources import greenhouse
from app.sources.base import (
    MAX_BODY_BYTES,
    BoardPostings,
    BoardUnavailable,
    MalformedPayload,
    NotModified,
    OversizedPayload,
    Source,
    SourceError,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_MODULE = BACKEND_ROOT / "app" / "services" / "pipeline.py"
PIPELINE_SOURCE = PIPELINE_MODULE.read_text(encoding="utf-8")

# The variables no case may inherit from the developer's shell, and the modules the
# pipeline must not import: the source and the scorer arrive as arguments, and the
# walk's own two settings are set explicitly by the cases that are about them.
CONFIGURATION_VARIABLES = (
    LLM_API_KEY,
    LLM_MODEL,
    SCORER_MAX_POSTINGS,
    PREFILTER_TITLE_TERMS,
    PREFILTER_SENIORITY_TERMS,
    PREFILTER_LOCATION_TERMS,
    PREFILTER_MUST_HAVE_KEYWORDS,
    PREFILTER_MUST_HAVE_FROM_RESUME,
    RUN_REQUEST_BUDGET_VARIABLE,
    RUN_REQUEST_INTERVAL_SECONDS_VARIABLE,
)
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}

# The secret a tick carries, and a resume with a value in it that could not reach
# a log line, a column or a response by accident.
TICK_SECRET = "tick-secret-sentinel-7f4c1d9ab38e"
RESUME_TEXT = "QA-RESUME-SENTINEL-TEXT: synthetic resume, Python and PostgreSQL."

# The skill one resume's parsed fields carry, and a value no case could reach by
# accident. It is lower-cased, because a term is trimmed and lower-cased before it
# is matched and the clause it names carries that form.
SKILL_SENTINEL = "qa-skill-sentinel-3a8e51"

# Every column of a run row: the three counts the walk writes -- the postings the
# boards returned, the matches that landed and the active companies its budget left
# -- and the three the lifecycle owns and the walk must leave alone.
RUN_COLUMNS = (
    "id",
    "status",
    "started_at",
    "finished_at",
    "error",
    "postings_seen",
    "matches_created",
    "companies_not_fetched",
)
MATCH_COLUMNS = (
    "id",
    "posting_id",
    "score",
    "reason",
    "gaps",
    "state",
    "created_at",
    "notified_at",
)
POSTING_COLUMNS = (
    "id",
    "company_id",
    "external_id",
    "title",
    "location",
    "url",
    "description",
    "first_seen_at",
    "updated_at",
    "closed_at",
)

# The alert state a digest (#10) would have written, seeded in one case to prove a
# run leaves a row the owner acted on exactly as it found it.
NOTIFIED_AT = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)


class FakeSource:
    """Stands in for `greenhouse.fetch`: it records every slug and answers from a script.

    `boards` is the postings each slug's board returns and `failing` the
    `SourceError` a slug's board raises. A slug in neither is refused rather than
    answered empty, so a case that forgot to script a board fails instead of
    passing as a board nobody listed anything on. `not_modified` names the boards
    that answer a conditional `304`, and `validators` the validator a `200` board
    hands out beside its postings -- `None`/`None` for a board that sent neither.
    `asked` records the validator each call carried, which is what proves the
    walk sent the company's stored pair.
    """

    def __init__(
        self,
        boards: dict[str, list[SourcePosting]] | None = None,
        *,
        failing: dict[str, SourceError] | None = None,
        not_modified: Iterable[str] = (),
        validators: dict[str, tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.boards = boards or {}
        self.failing = failing or {}
        self.not_modified = set(not_modified)
        self.validators = validators or {}
        self.calls: list[str] = []
        self.asked: list[tuple[str, str | None, str | None]] = []

    def __call__(
        self, slug: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> BoardPostings | NotModified:
        """The board this slug was scripted with, or the error it was scripted to raise."""
        self.calls.append(slug)
        self.asked.append((slug, etag, last_modified))
        failure = self.failing.get(slug)
        if failure is not None:
            raise failure
        if slug in self.not_modified:
            return NotModified()
        if slug not in self.boards:
            raise UnknownSlug(f"No board is scripted for '{slug}'")
        board_etag, board_last_modified = self.validators.get(slug, (None, None))
        return BoardPostings(self.boards[slug], board_etag, board_last_modified)


class RaisingScorer:
    """A `MatchScorer` whose call fails: the run's failure path, not a per-posting skip."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def score(self, posting: SourcePosting, resume_text: str) -> MatchResult:
        raise self.error


def _run_inline(work: Callable[[], None]) -> None:
    """The starter's body: run the work here and now, so the row is final when the tick answers."""
    work()


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module reaches the network: an unpatched client fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no key, no model, no cap, no prefilter term and no bound set."""
    for name in CONFIGURATION_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app over this test's own database, with the tick's secret set.

    The engine fixture is what points `DATABASE_URL` at the test's database and
    creates its tables, so a tick here runs on whichever engine the case is
    parametrised over. No server and no port: `TestClient` drives the app object.
    """
    monkeypatch.setenv("TICK_SECRET", TICK_SECRET)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def inline_starter() -> Iterator[None]:
    """The starter replaced by one that runs the work inside the request, on this thread."""
    app.dependency_overrides[get_run_starter] = lambda: _run_inline
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_run_starter, None)


def _posting(external_id: str = "1", **overrides: object) -> SourcePosting:
    """A posting as the adapter would hand it over: synthetic, and this module's own."""
    fields: dict[str, object] = {
        "external_id": external_id,
        "title": f"Backend Engineer {external_id}",
        "location": "Berlin",
        "url": f"https://example.test/{external_id}",
        "description": f"Synthetic description for {external_id}.",
    }
    fields.update(overrides)
    return SourcePosting(**fields)


def _result() -> MatchResult:
    return MatchResult(score=87, reason="Synthetic fit.", gaps=["No Kubernetes."])


def _company(
    slug: str = "acme", *, name: str | None = None, is_active: bool = True, source: str = "greenhouse"
) -> int:
    """One watchlist company, flushed so its id can name its postings: returns that id."""
    with db.session_scope() as session:
        company = Company(
            name=name or slug.capitalize(), slug=slug, is_active=is_active, source=source
        )
        session.add(company)
        session.flush()
        return company.id


def _active_resume(text: str = RESUME_TEXT) -> int:
    """The one active resume, written through #3's own writer and no file at all: its row id."""
    return resume.record_active_resume(
        "synthetic-resume.txt", resume.TEXT_PLAIN, text, Path("/tmp/synthetic-resume.txt")
    )


def _active_resume_with_skills(skills: list[str]) -> None:
    """The active resume, with the parsed fields #26's parse stored for it: one writer each."""
    row_id = _active_resume()
    resume.record_parsed_fields(
        row_id, json.dumps({"name": "Synthetic Candidate", "skills": skills})
    )


def _run() -> int:
    """One `run` row in `running` with both counts 0, written the way #9's route writes it."""
    with db.session_scope() as session:
        run = Run(status="running")
        session.add(run)
        session.flush()
        return run.id


def _stored_posting(company_id: int, external_id: str = "9", **overrides: object) -> int:
    """One `posting` row inserted straight through the app's own session, and its id."""
    fields: dict[str, object] = {
        "title": f"Stored Engineer {external_id}",
        "location": "Stored City",
        "url": f"https://stored.test/{external_id}",
        "description": f"Stored description for {external_id}.",
    }
    fields.update(overrides)
    with db.session_scope() as session:
        row = Posting(company_id=company_id, external_id=external_id, **fields)
        session.add(row)
        session.flush()
        return row.id


def _run_row(run_id: int) -> dict[str, Any]:
    """One `run` row as plain column values, so a write the pipeline is not allowed is visible."""
    with db.session_scope() as session:
        row = session.get(Run, run_id)
        return {column: getattr(row, column) for column in RUN_COLUMNS}


def _matches() -> list[dict[str, Any]]:
    """Every `match` row as plain column values, in id order."""
    with db.session_scope() as session:
        return [
            {column: getattr(row, column) for column in MATCH_COLUMNS}
            for row in session.scalars(select(Match).order_by(Match.id))
        ]


def _postings_of(company_id: int) -> dict[str, dict[str, Any]]:
    """Every stored posting of one company, by external id, as plain column values."""
    with db.session_scope() as session:
        return {
            row.external_id: {column: getattr(row, column) for column in POSTING_COLUMNS}
            for row in session.scalars(select(Posting).where(Posting.company_id == company_id))
        }


def _company_outcomes(run_id: int) -> list[dict[str, Any]]:
    """Every `run_company` row of that run, with its company's slug, in the order it was written."""
    with db.session_scope() as session:
        return [
            {
                "company_id": row.company_id,
                "slug": slug,
                "outcome": row.outcome,
                "error": row.error,
            }
            for row, slug in session.execute(
                select(RunCompany, Company.slug)
                .join(Company, RunCompany.company_id == Company.id)
                .where(RunCompany.run_id == run_id)
                .order_by(RunCompany.id)
            )
        ]


def _rows(model: type) -> int:
    """How many rows one table holds at all, so a stray insert cannot hide."""
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(model))


def _mark_saved(match_id: int) -> None:
    """The state and the alert time #10 leaves on a row the owner acted on."""
    with db.session_scope() as session:
        row = session.get(Match, match_id)
        row.state = "saved"
        row.notified_at = NOTIFIED_AT


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# The walk: the active watchlist, one fetch per company, with the slug the row stores.


def test_one_fetch_per_active_company_with_the_stored_slug_in_ascending_id_order(
    engine: Engine,
) -> None:
    """Beta is watched first and has the lower id, so an alphabetical walk fails here."""
    first = _company("beta", name="Beta GmbH")
    second = _company("acme", name="Acme Corporation")
    source = FakeSource({"beta": [_posting("1")], "acme": [_posting("2")]})
    _active_resume()
    run = _run()

    run_pipeline(run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == ["beta", "acme"]  # the stored slug, not a value built from the name
    assert set(_postings_of(first)) == {"1"}
    assert set(_postings_of(second)) == {"2"}


def test_an_inactive_company_is_never_fetched(engine: Engine) -> None:
    """The inactive company's board raises: reaching for it fails this case, not the run."""
    _company("acme")
    stopped = _company("dead", is_active=False)
    source = FakeSource(
        {"acme": [_posting("1")]}, failing={"dead": SourceError("the inactive board was fetched")}
    )
    _active_resume()
    run = _run()

    run_pipeline(run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == ["acme"]
    assert _postings_of(stopped) == {}
    assert _run_row(run)["postings_seen"] == 1


def test_each_company_is_fetched_through_the_adapter_its_row_names(engine: Engine) -> None:
    """A lever row and a greenhouse row in id order: each fake is called once with its own slug."""
    greenhouse = _company("acme", name="Acme Corporation")
    lever = _company("globex", name="Globex GmbH", source="lever")
    greenhouse_source = FakeSource({"acme": [_posting("1")]})
    lever_source = FakeSource({"globex": [_posting("2")]})
    _active_resume()

    run_pipeline(
        _run(),
        sources={"greenhouse": greenhouse_source, "lever": lever_source},
        scorer=FakeScorer(_result()),
    )

    assert greenhouse_source.calls == ["acme"]  # never the lever company's slug
    assert lever_source.calls == ["globex"]  # never the greenhouse company's slug
    assert set(_postings_of(greenhouse)) == {"1"}
    assert set(_postings_of(lever)) == {"2"}


def test_a_lever_source_error_is_recorded_against_its_company_and_names_the_slug(
    engine: Engine,
) -> None:
    company = _company("globex", source="lever")
    _active_resume()
    failure = UnknownSlug("There is no Lever board named 'globex'")
    run = _run()

    run_pipeline(
        run,
        sources={"lever": FakeSource(failing={"globex": failure})},
        scorer=FakeScorer(_result()),
    )

    assert _company_outcomes(run) == [
        {
            "company_id": company,
            "slug": "globex",
            "outcome": "failed",
            "error": f"The board for company 'globex' could not be fetched: {failure}",
        }
    ]


# Ingest owns every `posting` row: one call per company, and no row from anywhere else.


def test_each_board_is_handed_to_ingest_once_in_the_walk_s_order(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _company("beta")
    second = _company("acme")
    source = FakeSource({"beta": [_posting("1")], "acme": [_posting("2"), _posting("3")]})
    calls: list[tuple[int, list[str]]] = []

    def recording_ingest(company_id: int, postings: list[SourcePosting]) -> IngestResult:
        calls.append((company_id, [posting.external_id for posting in postings]))
        return IngestResult(created_ids=[], reopened_ids=[])

    monkeypatch.setattr(pipeline_module, "ingest_postings", recording_ingest)
    _active_resume()

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert calls == [(first, ["1"]), (second, ["2", "3"])]
    assert _rows(Posting) == 0  # the fake wrote nothing, and the pipeline wrote no row itself


def test_no_posting_row_exists_that_ingest_did_not_create(engine: Engine) -> None:
    """The pipeline inserts nothing: every row is one the service was given the payload for."""
    company = _company("acme")
    payload = [_posting("1"), _posting("2")]
    _active_resume()

    run_pipeline(_run(), sources={"greenhouse": FakeSource({"acme": payload})}, scorer=FakeScorer(_result()))

    assert set(_postings_of(company)) == {"1", "2"}
    assert _rows(Posting) == 2


# Only this run's new postings are matched: a stored posting is offered to neither writer.


def test_a_second_run_over_an_unchanged_board_offers_nothing(engine: Engine) -> None:
    company = _company("acme")
    payload = [_posting("1"), _posting("2")]
    source = FakeSource({"acme": payload})
    _active_resume()
    first = _run()
    first_scorer = FakeScorer(_result())
    run_pipeline(first, sources={"greenhouse": source}, scorer=first_scorer)
    _mark_saved(_matches()[0]["id"])  # a digest already alerted on one of them
    before = _matches()
    postings_before = _postings_of(company)
    second = _run()
    second_scorer = FakeScorer(_result())

    run_pipeline(second, sources={"greenhouse": source}, scorer=second_scorer)

    assert second_scorer.calls == []
    assert _matches() == before  # score, reason, state and notified_at included
    assert _postings_of(company) == postings_before  # no row was re-inserted or rewritten
    assert _rows(Posting) == 2
    assert _run_row(first)["postings_seen"] == 2  # a stored posting was still one the board sent
    assert (_run_row(second)["postings_seen"], _run_row(second)["matches_created"]) == (2, 0)


def test_a_reopened_posting_is_left_alone_with_the_match_row_it_already_has(
    engine: Engine,
) -> None:
    """A close and a reopen round-trip on the row, and the row the owner acted on is not rewritten."""
    company = _company("acme")
    _active_resume()
    run_pipeline(
        _run(), sources={"greenhouse": FakeSource({"acme": [_posting("Y")]})}, scorer=FakeScorer(_result())
    )
    (match_id,) = [row["id"] for row in _matches()]
    _mark_saved(match_id)
    with db.session_scope() as session:
        row = session.get(Match, match_id)
        row.score = 77
        row.reason = "The owner saved this one."
    run_pipeline(_run(), sources={"greenhouse": FakeSource({"acme": []})}, scorer=FakeScorer(_result()))
    assert _postings_of(company)["Y"]["closed_at"] is not None
    before = _matches()
    reopened = _run()
    scorer = FakeScorer(_result())
    source = FakeSource({"acme": [_posting("Y")]})

    run_pipeline(reopened, sources={"greenhouse": source}, scorer=scorer)

    assert source.calls == ["acme"]
    assert scorer.calls == []  # a reopened posting is not re-scored
    assert _matches() == before
    assert _run_row(reopened)["matches_created"] == 0
    assert _postings_of(company)["Y"]["closed_at"] is None  # ingest reopened it, and only that


def test_the_prefilter_and_the_scorer_are_offered_the_stored_row_not_the_payload(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row the id names, read back as the source's shape: the payload's values never reach the rules."""
    company = _company("acme")
    posting_id = _stored_posting(company, title="Stored Title", location="Stored City")
    stored = SourcePosting(
        external_id="9",
        title="Stored Title",
        location="Stored City",
        url="https://stored.test/9",
        description="Stored description for 9.",
    )

    def ingest_that_stored_it(company_id: int, postings: list[SourcePosting]) -> IngestResult:
        return IngestResult(created_ids=[posting_id], reopened_ids=[])

    monkeypatch.setattr(pipeline_module, "ingest_postings", ingest_that_stored_it)
    source = FakeSource(
        {"acme": [_posting("9", title="Payload Title", location="Payload City")]}
    )
    scorer = FakeScorer(_result())
    _active_resume()

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=scorer)

    assert scorer.calls == [(stored, RESUME_TEXT)]


# The prefilter is a gate: a rejection is recorded, and the scorer never sees it.


def test_a_rejected_posting_is_recorded_and_never_offered_to_the_scorer(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    company = _company("acme")
    source = FakeSource(
        {
            "acme": [
                _posting("1", title="Intern Backend Engineer"),
                _posting("2", title="Backend Engineer"),
            ]
        }
    )
    scorer = FakeScorer(_result())
    _active_resume()
    run = _run()

    run_pipeline(run, sources={"greenhouse": source}, scorer=scorer)

    stored = _postings_of(company)
    rows = {row["posting_id"]: row for row in _matches()}
    rejected = rows[stored["1"]["id"]]
    assert rejected["score"] == 0
    assert rejected["reason"] == REJECTION_REASON_PREFIX + "title term 'intern'"
    assert rejected["gaps"] is None
    assert rejected["state"] == "new"
    scored = rows[stored["2"]["id"]]
    assert (scored["score"], scored["reason"]) == (87, "Synthetic fit.")
    assert json.loads(scored["gaps"]) == ["No Kubernetes."]  # the scorer's row, not a score-0 one
    assert [call[0].external_id for call in scorer.calls] == ["2"]
    assert _run_row(run)["matches_created"] == 1  # a rejection is not a match


# A survivor is scored once, with the active resume, and the result is stored.


def test_every_survivor_is_scored_once_with_the_active_resume_and_its_result_is_stored(
    engine: Engine,
) -> None:
    company = _company("acme")
    payload = [_posting("a"), _posting("b"), _posting("c")]
    scorer = FakeScorer(_result())
    _active_resume()
    run = _run()

    run_pipeline(run, sources={"greenhouse": FakeSource({"acme": payload})}, scorer=scorer)

    assert scorer.calls == [(posting, RESUME_TEXT) for posting in payload]
    stored = _postings_of(company)
    rows = {row["posting_id"]: row for row in _matches()}
    assert set(rows) == {stored[posting.external_id]["id"] for posting in payload}
    for posting in payload:
        row = rows[stored[posting.external_id]["id"]]
        assert (row["score"], row["reason"]) == (87, "Synthetic fit.")
        assert json.loads(row["gaps"]) == ["No Kubernetes."]
    assert _run_row(run)["matches_created"] == 3


def test_a_not_scored_result_is_stored_like_any_other_and_does_not_fail_the_run(
    engine: Engine,
) -> None:
    """A model outage is a result `LlmScorer` returns, so it is recorded and counted, not raised."""
    _company("acme")
    outage = MatchResult(
        score=0, reason=NOT_SCORED_PREFIX + "the model call failed", gaps=[]
    )
    _active_resume()
    run = _run()

    run_pipeline(run, sources={"greenhouse": FakeSource({"acme": [_posting("1")]})}, scorer=FakeScorer(outage))

    (row,) = _matches()
    assert (row["score"], row["reason"], row["gaps"]) == (0, outage.reason, "[]")
    assert (_run_row(run)["status"], _run_row(run)["matches_created"]) == ("running", 1)


def test_a_scorer_that_raises_fails_the_run_and_stops_the_walk(engine: Engine) -> None:
    """Not a per-posting skip: the failure leaves the loop and the next board is never asked."""
    _company("acme")
    _company("beta")
    failure = ScorerConfigurationError(f"{LLM_API_KEY} is not set: scoring needs a model key")
    source = FakeSource({"acme": [_posting("1")], "beta": [_posting("2")]})
    _active_resume()
    run = _run()

    with pytest.raises(ScorerConfigurationError) as raised:
        run_pipeline(run, sources={"greenhouse": source}, scorer=RaisingScorer(failure))

    assert raised.value is failure
    assert source.calls == ["acme"]
    assert _rows(Posting) == 1  # stored before the scoring failed, and still written
    assert _rows(Match) == 0
    assert (_run_row(run)["postings_seen"], _run_row(run)["matches_created"]) == (1, 0)


# The active resume: read once, before the network, and a run without one fails.


def test_the_active_resume_is_read_once_and_before_the_first_board(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _company("beta")
    _company("acme")
    source = FakeSource({"beta": [_posting("1")], "acme": [_posting("2")]})
    reads: list[int] = []
    real_reader = resume.active_resume_text

    def counted_reader() -> str | None:
        reads.append(len(source.calls))  # how many boards were called before this read
        return real_reader()

    monkeypatch.setattr(pipeline_module.resume, "active_resume_text", counted_reader)
    _active_resume()
    scorer = FakeScorer(_result())

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=scorer)

    assert reads == [0]  # once, and before the first fetch
    assert {text for _, text in scorer.calls} == {RESUME_TEXT}


def test_the_parsed_skills_gate_one_posting_and_let_the_other_through(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#50: with the rule on, the sentinel skill rejects exactly the posting that lacks it.

    No key and no socket: `FakeScorer` answers, and this module's autouse fixtures
    leave `LLM_API_KEY` unset and `socket.socket.connect` raising.
    """
    monkeypatch.setenv(PREFILTER_MUST_HAVE_FROM_RESUME, "1")
    company = _company("acme")
    payload = [
        _posting("1", title="Backend Engineer", description="We use Python daily."),
        _posting("2", title="Backend Engineer", description=f"We use {SKILL_SENTINEL} daily."),
    ]
    _active_resume_with_skills([SKILL_SENTINEL])
    run = _run()

    run_pipeline(
        run, sources={"greenhouse": FakeSource({"acme": payload})}, scorer=FakeScorer(_result())
    )

    stored = _postings_of(company)
    rows = {row["posting_id"]: row for row in _matches()}
    rejected = rows[stored["1"]["id"]]
    assert rejected["score"] == 0
    assert rejected["reason"] == REJECTION_REASON_PREFIX + f"missing must-have '{SKILL_SENTINEL}'"
    assert rejected["gaps"] is None
    scored = rows[stored["2"]["id"]]
    assert (scored["score"], scored["reason"]) == (87, "Synthetic fit.")
    assert _run_row(run)["matches_created"] == 1  # a rejection is not a match


def test_the_same_run_with_the_rule_unset_scores_both_postings(engine: Engine) -> None:
    """Off by default: with the variable unset the parsed skills decide nothing."""
    company = _company("acme")
    payload = [
        _posting("1", title="Backend Engineer", description="We use Python daily."),
        _posting("2", title="Backend Engineer", description=f"We use {SKILL_SENTINEL} daily."),
    ]
    _active_resume_with_skills([SKILL_SENTINEL])
    run = _run()

    run_pipeline(
        run, sources={"greenhouse": FakeSource({"acme": payload})}, scorer=FakeScorer(_result())
    )

    stored = _postings_of(company)
    rows = {row["posting_id"]: row for row in _matches()}
    assert [rows[stored[external_id]["id"]]["score"] for external_id in ("1", "2")] == [87, 87]
    assert _run_row(run)["matches_created"] == 2


def test_the_parsed_skills_are_read_once_per_run_and_never_per_posting(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two companies and five postings, and the one-column read happens once, before the walk."""
    _company("beta")
    _company("acme")
    source = FakeSource(
        {
            "beta": [_posting("1"), _posting("2")],
            "acme": [_posting("3"), _posting("4"), _posting("5")],
        }
    )
    reads: list[int] = []
    real_reader = resume.active_resume_skills

    def counted_reader() -> list[str]:
        reads.append(len(source.calls))  # how many boards were called before this read
        return real_reader()

    monkeypatch.setattr(pipeline_module.resume, "active_resume_skills", counted_reader)
    _active_resume()

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert reads == [0]  # once, and before the first fetch


def test_a_run_with_no_active_resume_raises_naming_it_and_calls_no_board(engine: Engine) -> None:
    company = _company("acme")
    source = FakeSource({"acme": [_posting("1")]})
    scorer = FakeScorer(_result())
    run = _run()

    with pytest.raises(RuntimeError) as raised:
        run_pipeline(run, sources={"greenhouse": source}, scorer=scorer)

    assert str(raised.value) == NO_ACTIVE_RESUME
    assert "resume" in str(raised.value)
    assert source.calls == []  # a missing resume costs no board request
    assert scorer.calls == []
    assert _rows(Posting) == 0  # and stores nothing it could never score
    assert (_run_row(run)["postings_seen"], _run_row(run)["matches_created"]) == (0, 0)


# The counts: what the boards returned, what `record_score` wrote, as they are earned.


def test_postings_seen_counts_everything_returned_and_matches_created_only_the_scores(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    _company("beta")
    _company("acme")
    beta = [
        _posting("1", title="Intern Backend Engineer"),
        _posting("2", title="Intern Data Engineer"),
        _posting("3", title="Backend Engineer"),
    ]
    acme = [
        _posting("4", title="Intern Platform Engineer"),
        _posting("5", title="Intern Analyst"),
    ]
    _active_resume()
    run = _run()

    run_pipeline(
        run, sources={"greenhouse": FakeSource({"beta": beta, "acme": acme})}, scorer=FakeScorer(_result())
    )

    row = _run_row(run)
    assert row["postings_seen"] == 5
    assert row["matches_created"] == 1  # four rejections are not matches
    rejections = [
        match
        for match in _matches()
        if match["score"] == 0 and str(match["reason"]).startswith(REJECTION_REASON_PREFIX)
    ]
    assert len(rejections) == 4


def test_a_run_that_records_a_failed_board_keeps_the_counts_and_the_rows_it_earned(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One company's failure, and nothing already written by the other is rolled back."""
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    watched = _company("acme")
    failed = _company("beta")
    source = FakeSource(
        {
            "acme": [
                _posting("1", title="Intern Backend Engineer"),
                _posting("2", title="Intern Data Engineer"),
                _posting("3", title="Backend Engineer"),
            ]
        },
        failing={"beta": BoardUnavailable("The board 'beta' refused to answer: HTTP 503")},
    )
    _active_resume()
    run = _run()

    run_pipeline(run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert [(row["slug"], row["outcome"]) for row in _company_outcomes(run)] == [
        ("acme", "fetched"),
        ("beta", "failed"),
    ]
    assert source.calls == ["acme", "beta"]  # the walk did not stop at the failure
    assert len(_postings_of(watched)) == 3
    assert len(_matches()) == 3
    row = _run_row(run)
    assert (row["postings_seen"], row["matches_created"]) == (3, 1)
    assert _rows(Posting) == 3  # the failing company stored nothing
    assert _postings_of(failed) == {}


def test_a_retry_after_a_failed_board_stores_and_matches_only_what_is_new(engine: Engine) -> None:
    _company("acme")
    _company("beta")
    _active_resume()
    first = _run()
    source = FakeSource(
        {"acme": [_posting("1")]},
        failing={"beta": SourceError("The board 'beta' could not be reached")},
    )
    run_pipeline(first, sources={"greenhouse": source}, scorer=FakeScorer(_result()))
    before = _matches()
    assert len(before) == 1
    assert [row["outcome"] for row in _company_outcomes(first)] == ["fetched", "failed"]

    second = _run()
    retried = FakeSource({"acme": [_posting("1")], "beta": [_posting("2")]})
    run_pipeline(second, sources={"greenhouse": retried}, scorer=FakeScorer(_result()))

    after = _matches()
    assert after[0] == before[0]  # the same row, not a second one for the same posting
    assert len(after) == 2
    row = _run_row(second)
    assert (row["postings_seen"], row["matches_created"]) == (2, 1)


def test_the_pipeline_writes_the_counts_and_leaves_the_run_rows_lifecycle_alone(
    engine: Engine,
) -> None:
    """`status`, `error` and `finished_at` are #9's, on the row #9 wrote."""
    _company("acme")
    _active_resume()
    succeeding = _run()

    run_pipeline(
        succeeding, sources={"greenhouse": FakeSource({"acme": [_posting("1")]})}, scorer=FakeScorer(_result())
    )

    failing = _run()
    run_pipeline(
        failing,
        sources={"greenhouse": FakeSource(failing={"acme": SourceError("no board today")})},
        scorer=FakeScorer(_result()),
    )

    for run_id, seen, matched in ((succeeding, 1, 1), (failing, 0, 0)):
        row = _run_row(run_id)
        assert (row["status"], row["finished_at"], row["error"]) == ("running", None, None)
        assert (row["postings_seen"], row["matches_created"]) == (seen, matched)
        assert [entry["outcome"] for entry in _company_outcomes(run_id)] != []  # its own row is written


# A board that fails is recorded against its company, and the message names the board.


@pytest.mark.parametrize(
    "failure",
    [
        UnknownSlug("There is no Greenhouse board named 'acme'"),
        BoardUnavailable("The board 'acme' refused to answer: HTTP 503"),
        MalformedPayload("The board's answer has no list of jobs"),
        SourceTimeout("The board 'acme' did not answer in time"),
    ],
    ids=["an unknown slug", "a board that refused", "a malformed payload", "a timeout"],
)
def test_each_source_error_is_recorded_against_its_company_and_names_the_slug(
    engine: Engine, failure: SourceError
) -> None:
    company = _company("acme")
    _active_resume()
    run = _run()

    run_pipeline(
        run, sources={"greenhouse": FakeSource(failing={"acme": failure})}, scorer=FakeScorer(_result())
    )

    assert _company_outcomes(run) == [
        {
            "company_id": company,
            "slug": "acme",
            "outcome": "failed",
            "error": f"The board for company 'acme' could not be fetched: {failure}",
        }
    ]
    assert _rows(Posting) == 0
    assert _run_row(run)["postings_seen"] == 0


# The seam, the wiring, and what the module is not allowed to reach.


def test_the_seam_is_the_run_id_the_two_collaborators_and_the_pacing() -> None:
    """The shape #9's provider returns and the tests drive, named so a change fails loudly."""
    parameters = inspect.signature(run_pipeline).parameters

    assert list(parameters) == ["run_id", "sources", "scorer", "sleep"]
    assert parameters["run_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["sources"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["scorer"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["sleep"].kind is inspect.Parameter.KEYWORD_ONLY
    # The production default, read from the source: the shared `waits` fixture
    # replaces it for every case, which is what makes a real wait impossible here.
    assert "sleep: Callable[[float], None] = time.sleep" in PIPELINE_SOURCE


def test_the_pipeline_reads_only_its_own_two_variables_and_imports_no_network_client() -> None:
    """The two settings a run has, and nothing else the process holds."""
    assert f"os.environ.get({RUN_REQUEST_BUDGET_VARIABLE}" in PIPELINE_SOURCE
    assert f"os.environ.get({RUN_REQUEST_INTERVAL_SECONDS_VARIABLE}" in PIPELINE_SOURCE
    assert PIPELINE_SOURCE.count("environ") == 2  # the bound and the interval, one read each
    roots = _imported_roots(PIPELINE_SOURCE)
    assert roots & NETWORK_MODULES == set()
    assert {"smtplib", "email"} & roots == set()


# The tick: the real provider runs this pipeline, and a run's record carries no resume text.


def test_a_tick_with_no_active_resume_ends_its_run_failed_with_that_message(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """#9's provider, not a placeholder: the run reads the resume first and records the failure."""
    _company("acme")  # a board the run never reaches, because no resume is active

    response = client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})

    assert response.status_code == 202
    row = _run_row(response.json()["id"])
    assert row["status"] == "failed"
    assert row["error"] == NO_ACTIVE_RESUME
    assert row["finished_at"] is not None
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert _rows(Posting) == 0


def test_a_tick_over_a_watchlist_with_no_active_company_succeeds_with_both_counts_zero(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """"Nothing new" is a success: the run fetches nothing, scores nothing and raises nothing."""
    _company("dead", is_active=False)
    _active_resume()

    response = client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})

    row = _run_row(response.json()["id"])
    assert (row["status"], row["error"]) == ("succeeded", None)
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert _rows(Posting) == 0 and _rows(Match) == 0


def test_the_resume_text_reaches_no_log_line_no_row_and_no_response(
    engine: Engine,
    client: TestClient,
    inline_starter: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A run's whole record, with a resume text that could not appear in it by accident."""
    caplog.set_level(logging.DEBUG)
    company = _company("acme")
    _active_resume()
    source = FakeSource({"acme": [_posting("1")]})
    scorer = FakeScorer(_result())
    app.dependency_overrides[get_pipeline] = lambda: partial(
        run_pipeline, sources={"greenhouse": source}, scorer=scorer
    )
    try:
        response = client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})
        run_id = response.json()["id"]
        body = client.get(f"/runs/{run_id}").text
    finally:
        app.dependency_overrides.pop(get_pipeline, None)

    row = _run_row(run_id)
    assert (row["status"], row["postings_seen"], row["matches_created"]) == ("succeeded", 1, 1)
    assert scorer.calls == [(_posting("1"), RESUME_TEXT)]  # the run really scored with it

    assert caplog.records, "the capture must be non-vacuous"
    assert [record.getMessage() for record in caplog.records if RESUME_TEXT in record.getMessage()] == []
    assert [record for record in caplog.records if RESUME_TEXT in str(record.args)] == []
    held = [_run_row(run_id), *_matches(), *_postings_of(company).values()]
    assert [name for row in held for name, value in row.items() if RESUME_TEXT in str(value)] == []
    assert RESUME_TEXT not in body


def test_a_parsed_skill_reaches_the_stored_reason_and_no_log_line(
    engine: Engine,
    client: TestClient,
    inline_starter: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#50: the derived term explains its own rejection and reaches no record anywhere."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(PREFILTER_MUST_HAVE_FROM_RESUME, "1")
    company = _company("acme")
    source = FakeSource(
        {
            "acme": [
                _posting("1", title="Backend Engineer", description="We use Python daily."),
                _posting(
                    "2", title="Backend Engineer", description=f"We use {SKILL_SENTINEL} daily."
                ),
            ]
        }
    )
    scorer = FakeScorer(_result())
    _active_resume_with_skills([SKILL_SENTINEL])
    app.dependency_overrides[get_pipeline] = lambda: partial(
        run_pipeline, sources={"greenhouse": source}, scorer=scorer
    )
    try:
        response = client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})
        run_id = response.json()["id"]
    finally:
        app.dependency_overrides.pop(get_pipeline, None)

    # The one place the term appears is the reason of the posting it rejected.
    stored = _postings_of(company)
    rows = {row["posting_id"]: row for row in _matches()}
    assert rows[stored["1"]["id"]]["reason"] == (
        REJECTION_REASON_PREFIX + f"missing must-have '{SKILL_SENTINEL}'"
    )
    assert rows[stored["2"]["id"]]["score"] == 87  # the run really read the parsed skills

    assert caplog.records, "the capture must be non-vacuous"
    assert [
        record.getMessage() for record in caplog.records if SKILL_SENTINEL in record.getMessage()
    ] == []
    assert [record for record in caplog.records if SKILL_SENTINEL in str(record.args)] == []
    assert [record for record in caplog.records if SKILL_SENTINEL in str(record.exc_info)] == []
    assert SKILL_SENTINEL not in client.get(f"/runs/{run_id}").text


def test_the_pipeline_writes_only_the_outcome_rows_and_the_runs_counts() -> None:
    """A posting row and a match row are written by the services: this module writes one row of its own."""
    assert PIPELINE_SOURCE.count("session.add(") == 1  # the `run_company` row, and nothing else
    assert "RunCompany(" in PIPELINE_SOURCE
    assert "session.delete(" not in PIPELINE_SOURCE
    assert ".commit(" not in PIPELINE_SOURCE


# The validator: sent with the call, stored after the payload, and what a 304 does to the walk.

STORED_ETAG = 'W/"v1"'
ANSWERED_ETAG = 'W/"v2"'
STORED_LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


def _seed_validator(company_id: int, *, etag: str | None, last_modified: str | None) -> None:
    """A company's stored validator, written straight through the app's own session."""
    with db.session_scope() as session:
        company = session.get(Company, company_id)
        company.etag = etag
        company.last_modified = last_modified


def _stored_validator(company_id: int) -> tuple[str | None, str | None]:
    """That company's two validator columns, as the database holds them."""
    with db.session_scope() as session:
        company = session.get(Company, company_id)
        return (company.etag, company.last_modified)


def test_the_company_s_stored_validator_is_sent_with_the_call(engine: Engine) -> None:
    """The pair the row holds is what the adapter is asked with, read from the fake's own record."""
    company = _company("acme")
    _seed_validator(company, etag=STORED_ETAG, last_modified=STORED_LAST_MODIFIED)
    source = FakeSource({"acme": [_posting("1")]})
    _active_resume()

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.asked == [("acme", STORED_ETAG, STORED_LAST_MODIFIED)]


def test_a_200_s_validator_is_stored_on_the_row_in_the_same_run(engine: Engine) -> None:
    """Exactly what the answer carried: the old value is replaced by this run's own fetch."""
    company = _company("acme")
    _seed_validator(company, etag=STORED_ETAG, last_modified=STORED_LAST_MODIFIED)
    source = FakeSource({"acme": [_posting("1")]}, validators={"acme": (ANSWERED_ETAG, None)})
    _active_resume()

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert _stored_validator(company) == (ANSWERED_ETAG, None)


def test_a_304_company_keeps_every_one_of_its_postings_open(engine: Engine) -> None:
    """The board was not read, so nothing it holds may be closed: the real ingest is in place."""
    company = _company("acme")
    _stored_posting(company, external_id="1")
    _stored_posting(company, external_id="2")
    before = _postings_of(company)
    _active_resume()

    run_pipeline(
        _run(),
        sources={"greenhouse": FakeSource({}, not_modified={"acme"})},
        scorer=FakeScorer(_result()),
    )

    assert _postings_of(company) == before  # not closed, not rewritten, not deleted
    assert all(row["closed_at"] is None for row in before.values())
    assert _open_count(company) == 2  # read by the module's own count, not by this case's copy


def test_a_304_company_is_never_handed_to_ingest(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """One `304` board and one `200` board: the spy's call list names only the board that answered."""
    skipped = _company("acme")
    ingested = _company("beta")
    calls: list[tuple[int, list[str]]] = []

    def recording_ingest(company_id: int, postings: list[SourcePosting]) -> IngestResult:
        calls.append((company_id, [posting.external_id for posting in postings]))
        return IngestResult(created_ids=[], reopened_ids=[])

    monkeypatch.setattr(pipeline_module, "ingest_postings", recording_ingest)
    _active_resume()

    run_pipeline(
        _run(),
        sources={
            "greenhouse": FakeSource(
                {"beta": [_posting("1")]}, not_modified={"acme"}
            )
        },
        scorer=FakeScorer(_result()),
    )

    assert calls == [(ingested, ["1"])]  # the skipped company's id is not in this list


def test_a_skipped_board_contributes_nothing_to_either_count(engine: Engine) -> None:
    """A `200` board of two postings and a `304` board: `postings_seen` is the two, and no more."""
    _company("acme")  # answers 304, and is walked first
    _company("beta")
    _active_resume()
    run = _run()

    run_pipeline(
        run,
        sources={
            "greenhouse": FakeSource(
                {"beta": [_posting("1"), _posting("2")]}, not_modified={"acme"}
            )
        },
        scorer=FakeScorer(_result()),
    )

    row = _run_row(run)
    assert (row["postings_seen"], row["matches_created"]) == (2, 2)  # the skipped board's 0 is not here


def test_a_304_leaves_the_stored_validator_exactly_as_it_was(engine: Engine) -> None:
    """The validator that produced the `304` is still the validator of the body we hold."""
    company = _company("acme")
    _seed_validator(company, etag=STORED_ETAG, last_modified=STORED_LAST_MODIFIED)
    _active_resume()

    run_pipeline(
        _run(),
        sources={"greenhouse": FakeSource({}, not_modified={"acme"})},
        scorer=FakeScorer(_result()),
    )

    assert _stored_validator(company) == (STORED_ETAG, STORED_LAST_MODIFIED)


def test_a_refused_payload_records_its_own_failure_and_leaves_the_validator_untouched(
    engine: Engine,
) -> None:
    """The guard refuses before the ingest, so the validator it answered with is not written."""
    company = _company("acme")
    _open_postings(company, 4)
    _seed_validator(company, etag=STORED_ETAG, last_modified=None)
    _active_resume()
    run = _run()

    run_pipeline(
        run,
        sources={"greenhouse": FakeSource({"acme": []}, validators={"acme": (ANSWERED_ETAG, None)})},
        scorer=FakeScorer(_result()),
    )

    assert _stored_validator(company) == (STORED_ETAG, None)  # next run asks the same question
    (recorded,) = _company_outcomes(run)
    assert recorded["outcome"] == "failed"
    assert "acme" in recorded["error"]  # the guard's own message, on the company's row
    assert _open_count(company) == 4


def test_a_payload_whose_ingest_raised_leaves_the_stored_validator_untouched(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validator written too early would buy a board skipped forever: this is the case it stops."""
    company = _company("acme")
    _seed_validator(company, etag=STORED_ETAG, last_modified=None)
    _active_resume()

    def refusing_ingest(company_id: int, postings: list[SourcePosting]) -> IngestResult:
        raise RuntimeError("the ingest failed after the board answered")

    monkeypatch.setattr(pipeline_module, "ingest_postings", refusing_ingest)

    with pytest.raises(RuntimeError):
        run_pipeline(
            _run(),
            sources={"greenhouse": FakeSource({"acme": [_posting("1")]}, validators={"acme": (ANSWERED_ETAG, None)})},
            scorer=FakeScorer(_result()),
        )

    assert _stored_validator(company) == (STORED_ETAG, None)


def test_a_run_in_which_every_board_answers_304_is_a_success_with_nothing_new(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """Through the real `POST /internal/tick`: a run is not a failure because there was nothing to read."""
    acme = _company("acme")
    beta = _company("beta")
    _stored_posting(acme, external_id="1")
    _stored_posting(beta, external_id="2")
    _active_resume()

    row = _tick(client, {"greenhouse": FakeSource({}, not_modified={"acme", "beta"})})

    assert (row["status"], row["error"]) == ("succeeded", None)
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert _rows(Posting) == 2  # both boards kept their rows
    assert [posting["closed_at"] for company in (acme, beta) for posting in _postings_of(company).values()] == [None, None]
    assert _rows(Match) == 0  # so the digest behind it has nothing to send


# The shrink guard: a board that lists far fewer postings than the company has open.

# A string a board's body carries that must reach no column of the run's row.
PAYLOAD_SENTINEL = "PAYLOAD-SENTINEL-TEXT-THAT-MUST-NOT-BE-ECHOED"

# An over-cap body's chunk size, so an over-cap body is written in a few chunks here.
OVER_CAP_CHUNK_BYTES = MAX_BODY_BYTES // 4 + 1


def _open_postings(company_id: int, count: int) -> None:
    """`count` stored postings of one company, all still open, so the guard has a count to read."""
    for index in range(count):
        _stored_posting(company_id, external_id=f"open-{index}")


def _open_count(company_id: int) -> int:
    """How many of that company's postings are open, read here rather than by the module under test."""
    with db.session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(Posting)
            .where(Posting.company_id == company_id, Posting.closed_at.is_(None))
        )


def _tick(client: TestClient, sources: Mapping[str, Source]) -> dict[str, Any]:
    """One tick whose work is this run's walk over those sources, and the row it left.

    The provider is replaced for the length of the request, the way the tick cases
    above replace it: the row's lifecycle stays `_run`'s, which is what makes the
    row readable for what the walk did to it.
    """
    app.dependency_overrides[get_pipeline] = lambda: partial(
        run_pipeline, sources=sources, scorer=FakeScorer(_result())
    )
    try:
        response = client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})
    finally:
        app.dependency_overrides.pop(get_pipeline, None)
    return _run_row(response.json()["id"])


def test_a_refused_board_is_never_handed_to_ingest(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard sits before the ingest: a refused payload reaches no `posting` row at all."""
    company = _company("acme")
    _open_postings(company, 4)
    calls: list[int] = []

    def recording_ingest(company_id: int, postings: list[SourcePosting]) -> IngestResult:
        calls.append(company_id)
        return IngestResult(created_ids=[], reopened_ids=[])

    monkeypatch.setattr(pipeline_module, "ingest_postings", recording_ingest)
    _active_resume()
    run = _run()

    run_pipeline(
        run, sources={"greenhouse": FakeSource({"acme": []})}, scorer=FakeScorer(_result())
    )

    assert calls == []  # the ingest was not called for the refused company
    (recorded,) = _company_outcomes(run)
    assert recorded["outcome"] == "failed"
    message = recorded["error"]
    assert "acme" in message
    assert "0" in message and "4" in message  # both numbers, and no payload
    assert _open_count(company) == 4
    assert _rows(Posting) == 4
    assert _run_row(run)["postings_seen"] == 0  # a refused board is counted by nobody


# The ratio and the floor: what the guard refuses, and the boards it must leave alone. The
# listed postings are the company's own external ids, so an allowed board closes its own rows.
_SHRINK_CASES = [
    pytest.param(3, 0, "succeeded", 3, id="three open, an empty board: all three close"),
    pytest.param(6, 5, "succeeded", 1, id="six open, five listed: one closes"),
    pytest.param(6, 3, "succeeded", 3, id="six open, exactly half listed: allowed"),
    pytest.param(0, 0, "succeeded", 0, id="no postings at all, an empty board: nothing happens"),
    pytest.param(20, 10, "succeeded", 10, id="twenty open, ten listed: allowed"),
    pytest.param(4, 0, "failed", 0, id="four open, an empty board: refused"),
    pytest.param(4, 1, "failed", 0, id="four open, one listed: refused"),
    pytest.param(20, 9, "failed", 0, id="twenty open, nine listed: refused"),
]


@pytest.mark.parametrize("open_count, listed, outcome, closed", _SHRINK_CASES)
def test_the_guard_matrix(
    engine: Engine,
    client: TestClient,
    inline_starter: None,
    open_count: int,
    listed: int,
    outcome: str,
    closed: int,
) -> None:
    """The same fixtures from both sides: a genuine board is still the truth, a shrunken one is not."""
    company = _company("acme")
    _open_postings(company, open_count)
    payload = [_posting(f"open-{index}") for index in range(listed)]
    _active_resume()

    row = _tick(client, {"greenhouse": FakeSource({"acme": payload})})

    assert row["status"] == outcome
    assert _rows(Posting) == open_count  # nothing inserted and nothing deleted, either way
    assert _open_count(company) == open_count - closed
    (recorded,) = _company_outcomes(row["id"])
    if outcome == "failed":
        assert (row["postings_seen"], row["matches_created"]) == (0, 0)
        assert row["error"] == "1 of 1 companies failed: 'acme'"  # the run names the board
        assert recorded["outcome"] == "failed"
        assert "acme" in recorded["error"]  # and the company's own row carries the reason
        assert str(open_count) in recorded["error"] and str(listed) in recorded["error"]
    else:
        assert row["error"] is None
        assert recorded["outcome"] == "fetched"
        # Nothing listed is new -- every posting the board named is one already stored --
        # so `created_ids` is empty and the run matches nothing.
        assert (row["postings_seen"], row["matches_created"]) == (listed, 0)


def test_a_second_run_over_the_same_refused_board_is_refused_again(engine: Engine) -> None:
    """No column, table or file holds a history: the same refusal, the same rows still open."""
    company = _company("acme")
    _open_postings(company, 4)
    _active_resume()
    source = FakeSource({"acme": []})
    refusals: list[str] = []

    for _ in range(2):
        run = _run()
        run_pipeline(run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))
        (recorded,) = _company_outcomes(run)
        assert recorded["outcome"] == "failed"
        refusals.append(recorded["error"])
        assert _open_count(company) == 4
        assert _rows(Posting) == 4

    assert refusals[0] == refusals[1]  # the same numbers, read from the same rows
    assert [column.name for column in Posting.__table__.columns] == list(POSTING_COLUMNS)


def test_a_refused_board_keeps_what_the_earlier_companies_earned(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """#59's rule: the refusal is recorded and the walk goes on, so what the other earned stays."""
    trusted = _company("acme")
    refused = _company("beta")
    _open_postings(refused, 4)
    _active_resume()
    source = FakeSource({"acme": [_posting("1"), _posting("2"), _posting("3")], "beta": []})

    row = _tick(client, {"greenhouse": source})

    assert row["status"] == "failed"
    assert row["error"] == "1 of 2 companies failed: 'beta'"
    assert (row["postings_seen"], row["matches_created"]) == (3, 3)  # the refused board's 0 is not here
    assert [(entry["slug"], entry["outcome"]) for entry in _company_outcomes(row["id"])] == [
        ("acme", "fetched"),
        ("beta", "failed"),
    ]
    assert set(_postings_of(trusted)) == {"1", "2", "3"}
    assert len(_matches()) == 3
    assert _open_count(refused) == 4
    assert _rows(Posting) == 7  # the three the run stored, and the four left as they were


class _OverCapStream(httpx2.SyncByteStream):
    """A body over the cap, arriving in chunks whose first one carries the sentinel."""

    def __init__(self, first: bytes) -> None:
        self.chunks = [first + b"x" * (OVER_CAP_CHUNK_BYTES - len(first))] * 6

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks


def _board(*answers: httpx2.Response | Exception) -> Callable[[str], list[SourcePosting]]:
    """The real Greenhouse adapter over a transport serving that script, and never waiting.

    The last answer is served for every later request, so "every attempt" is
    written once. The adapter is the one the app ships rather than this module's
    fake, because the text a payload carries has to exist for a leak to be visible.
    """
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        answer = answers[min(len(requests), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = httpx2.Client(transport=httpx2.MockTransport(handle))

    def board(slug: str, *, etag: str | None = None, last_modified: str | None = None):
        return greenhouse.fetch(
            slug,
            etag=etag,
            last_modified=last_modified,
            client=client,
            sleep=lambda seconds: None,
        )

    return board


FAILED_BOARD_ANSWERS = [
    pytest.param(
        lambda: httpx2.Response(
            503,
            text=f"<html><body>{PAYLOAD_SENTINEL}</body></html>",
            headers={"ETag": PAYLOAD_SENTINEL},
        ),
        "HTTP 503",
        id="a board that refused every attempt",
    ),
    pytest.param(
        lambda: httpx2.Response(200, stream=_OverCapStream(PAYLOAD_SENTINEL.encode())),
        str(MAX_BODY_BYTES),
        id="a body over the cap",
    ),
]


@pytest.mark.parametrize("answer, expected", FAILED_BOARD_ANSWERS)
def test_a_failed_board_leaves_no_payload_text_in_a_message_a_client_reads(
    engine: Engine,
    client: TestClient,
    inline_starter: None,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[], httpx2.Response],
    expected: str,
) -> None:
    """What a run says about a failure: the slug, the typed reason and no body, header or resume."""
    caplog.set_level(logging.DEBUG)
    _company("acme")
    _active_resume()

    row = _tick(client, {"greenhouse": _board(answer())})
    body = client.get(f"/runs/{row['id']}").text

    assert row["status"] == "failed"
    assert row["error"] == "1 of 1 companies failed: 'acme'"  # a slug, and no reason of its own
    (recorded,) = _company_outcomes(row["id"])
    assert recorded["outcome"] == "failed"
    assert "acme" in recorded["error"]
    assert expected in recorded["error"]  # the typed refusal reached the row, and not a traceback
    assert caplog.records, "the capture must be non-vacuous"
    for text in (recorded["error"], row["error"], body):
        assert PAYLOAD_SENTINEL not in text
    assert [
        record.getMessage()
        for record in caplog.records
        if PAYLOAD_SENTINEL in record.getMessage() or PAYLOAD_SENTINEL in str(record.args)
    ] == []
    assert PAYLOAD_SENTINEL not in body  # the served outcomes carry the message, never the body
    assert _rows(Posting) == 0


# The run's outbound budget and its pacing: how many boards one run may ask, how far
# apart, and what the companies it never reached keep.

# Three active companies, one posting each, scripted so a walk's call order is its
# ascending `company.id` order: `acme`, `beta`, `globex`.
THREE_BOARDS = {
    "acme": [_posting("1")],
    "beta": [_posting("2")],
    "globex": [_posting("3")],
}


def test_the_two_readers_answer_the_variable_or_the_module_s_own_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset and empty are the default, `0` is a value, and a set one is read as it stands."""
    for variable in (RUN_REQUEST_BUDGET_VARIABLE, RUN_REQUEST_INTERVAL_SECONDS_VARIABLE):
        monkeypatch.setenv(variable, "")
        assert pipeline_module.request_budget() == DEFAULT_REQUEST_BUDGET == 200
        assert (
            pipeline_module.request_interval_seconds()
            == DEFAULT_REQUEST_INTERVAL_SECONDS
            == 1.0
        )
        monkeypatch.delenv(variable, raising=False)
    assert pipeline_module.request_budget() == DEFAULT_REQUEST_BUDGET
    assert pipeline_module.request_interval_seconds() == DEFAULT_REQUEST_INTERVAL_SECONDS

    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "0")
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "0")
    assert pipeline_module.request_budget() == 0
    assert pipeline_module.request_interval_seconds() == 0.0

    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "7")
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "2.5")
    assert pipeline_module.request_budget() == 7
    assert pipeline_module.request_interval_seconds() == 2.5


def test_both_settings_are_read_once_per_run_and_never_at_import(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    """Two runs in one process: the second obeys what the environment holds when it starts.

    The first run is bounded to one board and paced at 2.5 seconds, the second is
    unbounded and paced at 0. A value read at import, or read once for the process,
    would make the second run behave like the first.
    """
    for slug in THREE_BOARDS:
        _company(slug)
    source = FakeSource(THREE_BOARDS)
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "1")
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "2.5")
    first = _run()

    run_pipeline(first, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == ["acme"]
    assert _run_row(first)["companies_not_fetched"] == 2
    assert waits == []  # one call, so no gap to wait in

    monkeypatch.delenv(RUN_REQUEST_BUDGET_VARIABLE)
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "0")
    second = _run()

    run_pipeline(second, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == ["acme", "acme", "beta", "globex"]  # the second run's own bound
    assert _run_row(second)["companies_not_fetched"] is None
    assert waits == [0.0, 0.0]  # the second run's own interval, one wait per gap


_BAD_VALUES = [
    pytest.param(RUN_REQUEST_BUDGET_VARIABLE, "-1", id="a negative budget"),
    pytest.param(RUN_REQUEST_BUDGET_VARIABLE, "soon", id="a budget that is not a number"),
    pytest.param(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "-0.5", id="a negative interval"),
    pytest.param(
        RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "soon", id="an interval that is not a number"
    ),
]


@pytest.mark.parametrize("variable, value", _BAD_VALUES)
def test_a_value_the_reader_cannot_read_fails_the_run_naming_the_variable_and_the_value(
    engine: Engine,
    client: TestClient,
    inline_starter: None,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    """Read before the first board call: no request, nothing stored, and the row says what."""
    _company("acme")
    source = FakeSource({"acme": [_posting("1")]})
    _active_resume()
    monkeypatch.setenv(variable, value)

    row = _tick(client, {"greenhouse": source})

    assert row["status"] == "failed"
    assert variable in row["error"] and value in row["error"]
    assert source.calls == []  # nothing was asked, because nothing was read
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert row["companies_not_fetched"] is None  # and the new column was not written
    assert _rows(Posting) == 0


_BUDGET_CASES = [
    pytest.param(0, [], 3, id="zero asks no board at all"),
    pytest.param(1, ["acme"], 2, id="one asks only the lowest id"),
    pytest.param(3, ["acme", "beta", "globex"], None, id="exactly n calls every board"),
    pytest.param(4, ["acme", "beta", "globex"], None, id="n + 1 behaves exactly as n"),
]


@pytest.mark.parametrize("budget, called, not_fetched", _BUDGET_CASES)
def test_the_budget_bounds_the_walk_and_never_writes_a_zero(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    called: list[str],
    not_fetched: int | None,
) -> None:
    """Three active companies, one posting each, and the row's own record of what the walk did."""
    for slug in THREE_BOARDS:
        _company(slug)
    source = FakeSource(THREE_BOARDS)
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, str(budget))
    run = _run()

    run_pipeline(run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    row = _run_row(run)
    assert source.calls == called  # ascending `company.id`, and nothing past the bound
    assert (row["postings_seen"], row["matches_created"]) == (len(called), len(called))
    assert row["companies_not_fetched"] == not_fetched
    assert _rows(Posting) == len(called)  # a refused company stored nothing


def test_the_companies_the_budget_refuses_are_untouched(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipped, not probed and skipped: no call, no ingest, no row moved and no validator written."""
    first = _company("acme")
    second = _company("beta")
    refused = _company("globex")
    source = FakeSource(
        THREE_BOARDS, validators={"globex": (ANSWERED_ETAG, STORED_LAST_MODIFIED)}
    )
    _active_resume()
    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))
    _mark_saved(_matches()[2]["id"])  # the owner acted on the refused company's match
    source.validators["globex"] = ("W/\"elsewhere\"", None)  # a call now would store this

    source.calls.clear()
    postings_before = _postings_of(refused)
    matches_before = _matches()
    validator_before = _stored_validator(refused)
    calls: list[int] = []

    def recording_ingest(company_id: int, postings: list[SourcePosting]) -> IngestResult:
        calls.append(company_id)
        return IngestResult(created_ids=[], reopened_ids=[])

    monkeypatch.setattr(pipeline_module, "ingest_postings", recording_ingest)
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "2")
    third_run = _run()

    run_pipeline(third_run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == ["acme", "beta"]  # the refused company's board was asked nothing
    assert calls == [first, second]  # and its payload reached no ingest
    assert _postings_of(refused) == postings_before
    assert all(row["closed_at"] is None for row in postings_before.values())
    assert _matches() == matches_before  # its saved match included, state and alert time too
    assert _stored_validator(refused) == validator_before
    assert _run_row(third_run)["companies_not_fetched"] == 1


@pytest.mark.parametrize(
    "budget, called, not_fetched, fetched",
    [
        pytest.param(2, ["acme", "beta"], 1, 1, id="the 304 spends the second unit"),
        pytest.param(
            3, ["acme", "beta", "globex"], None, 2, id="the same watchlist walked to the end"
        ),
    ],
)
def test_a_304_spends_its_unit_and_is_not_a_not_fetched_company(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    called: list[str],
    not_fetched: int | None,
    fetched: int,
) -> None:
    """The middle board answers `304`: it was asked, so it is counted and spends a unit."""
    _company("acme")
    unchanged = _company("beta")
    _stored_posting(unchanged, external_id="old")
    _seed_validator(unchanged, etag=STORED_ETAG, last_modified=STORED_LAST_MODIFIED)
    _company("globex")
    source = FakeSource(
        {"acme": [_posting("1")], "globex": [_posting("3")]}, not_modified={"beta"}
    )
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, str(budget))
    run = _run()

    run_pipeline(run, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    row = _run_row(run)
    assert source.calls == called
    assert (row["postings_seen"], row["matches_created"]) == (
        fetched,
        fetched,
    )  # the 200 boards' postings, and the 304 board's none
    assert row["companies_not_fetched"] == not_fetched
    assert {external: value["closed_at"] for external, value in _postings_of(unchanged).items()} == {
        "old": None
    }
    assert _stored_validator(unchanged) == (STORED_ETAG, STORED_LAST_MODIFIED)


def test_the_next_run_with_room_fetches_the_rest_and_closes_nothing_of_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the bound left is untouched, and a later run with room stores and scores it."""
    companies = [_company(slug) for slug in THREE_BOARDS]
    source = FakeSource(THREE_BOARDS)
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "2")
    first = _run()

    run_pipeline(first, sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    monkeypatch.delenv(RUN_REQUEST_BUDGET_VARIABLE)
    second = _run()
    second_scorer = FakeScorer(_result())

    run_pipeline(second, sources={"greenhouse": source}, scorer=second_scorer)

    assert source.calls == ["acme", "beta", "acme", "beta", "globex"]
    first_row = _run_row(first)
    assert (first_row["postings_seen"], first_row["matches_created"]) == (2, 2)
    assert first_row["companies_not_fetched"] == 1
    second_row = _run_row(second)
    assert second_row["companies_not_fetched"] is None
    assert (second_row["postings_seen"], second_row["matches_created"]) == (3, 1)
    assert len(second_scorer.calls) == 1  # only the company fetch 1 never reached was new
    assert all(
        row["closed_at"] is None
        for company in companies
        for row in _postings_of(company).values()
    )


def test_the_walk_waits_the_interval_between_calls_and_none_before_the_first(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    """Four boards at one interval: exactly three gaps, in the order the calls were made."""
    slugs = ("acme", "beta", "globex", "initech")
    for slug in slugs:
        _company(slug)
    source = FakeSource({slug: [_posting(slug)] for slug in slugs})
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "2.5")

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == list(slugs)
    assert waits == [2.5, 2.5, 2.5]


def test_a_one_company_walk_never_waits(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    """One call, no gap, and no wait after the last one either."""
    _company("acme")
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "2.5")

    run_pipeline(
        _run(),
        sources={"greenhouse": FakeSource({"acme": [_posting("1")]})},
        scorer=FakeScorer(_result()),
    )

    assert waits == []


def test_a_refused_company_is_never_waited_for(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    """Five companies bounded to two: one gap, and nothing for the three past the bound."""
    slugs = ("acme", "beta", "globex", "initech", "umbrella")
    for slug in slugs:
        _company(slug)
    source = FakeSource({slug: [_posting(slug)] for slug in slugs})
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "2.5")
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "2")

    run_pipeline(_run(), sources={"greenhouse": source}, scorer=FakeScorer(_result()))

    assert source.calls == ["acme", "beta"]
    assert waits == [2.5]


def test_a_304_board_is_paced_like_any_other_call(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    """Two companies where the second answers `304`: it was asked, so the gap before it was waited."""
    _company("acme")
    _company("beta")
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "2.5")

    run_pipeline(
        _run(),
        sources={"greenhouse": FakeSource({"acme": [_posting("1")]}, not_modified={"beta"})},
        scorer=FakeScorer(_result()),
    )

    assert waits == [2.5]


def test_an_interval_of_zero_calls_the_seam_once_per_gap_all_the_same(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    """`0` is a legitimate interval: the seam is called `k - 1` times whatever the value."""
    for slug in THREE_BOARDS:
        _company(slug)
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_INTERVAL_SECONDS_VARIABLE, "0")

    run_pipeline(
        _run(), sources={"greenhouse": FakeSource(THREE_BOARDS)}, scorer=FakeScorer(_result())
    )

    assert waits == [0.0, 0.0]


# The per-company outcome (#59): what the walk records for each company it
# attempted, and the run's status derived from it. A failed board is that
# company's failure and nothing else's; a company the budget never reached is not
# a failure at all.


def test_a_failing_board_between_two_healthy_ones_leaves_the_walk_going(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """The middle board raises: all three are asked, the two that answered score, the run fails."""
    answered = _company("acme")
    failing = _company("beta")
    also_answered = _company("globex")
    source = FakeSource(
        {"acme": [_posting("1")], "globex": [_posting("3")]},
        failing={"beta": BoardUnavailable("The board 'beta' refused to answer: HTTP 503")},
    )
    _active_resume()

    row = _tick(client, {"greenhouse": source})

    assert source.calls == ["acme", "beta", "globex"]  # every company was attempted
    assert row["status"] == "failed"
    assert row["error"] == "1 of 3 companies failed: 'beta'"  # a slug-only summary
    assert (row["postings_seen"], row["matches_created"]) == (2, 2)  # the failed board's 0 is absent
    assert set(_postings_of(answered)) == {"1"}
    assert set(_postings_of(also_answered)) == {"3"}
    assert _postings_of(failing) == {}  # the failed company stored nothing
    outcomes = _company_outcomes(row["id"])
    assert [(entry["slug"], entry["outcome"]) for entry in outcomes] == [
        ("acme", "fetched"),
        ("beta", "failed"),
        ("globex", "fetched"),
    ]
    failed_entry = outcomes[1]
    assert failed_entry["company_id"] == failing
    assert "beta" in failed_entry["error"] and "HTTP 503" in failed_entry["error"]


def test_every_board_failing_records_every_company_and_closes_nothing(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """Three boards raising: the run is `failed`, every attempt is recorded, no stored row moves."""
    companies = {slug: _company(slug) for slug in THREE_BOARDS}
    for company_id in companies.values():
        _stored_posting(company_id, external_id="stored")
    before = {slug: _postings_of(company_id) for slug, company_id in companies.items()}
    source = FakeSource(
        failing={
            slug: BoardUnavailable(f"The board '{slug}' refused to answer: HTTP 503")
            for slug in THREE_BOARDS
        }
    )
    _active_resume()

    row = _tick(client, {"greenhouse": source})

    assert source.calls == list(THREE_BOARDS)
    assert row["status"] == "failed"
    assert row["error"] == "3 of 3 companies failed: 'acme', 'beta', 'globex'"
    assert (row["postings_seen"], row["matches_created"]) == (0, 0)
    assert [(entry["slug"], entry["outcome"]) for entry in _company_outcomes(row["id"])] == [
        (slug, "failed") for slug in THREE_BOARDS
    ]
    assert _rows(Match) == 0
    assert {slug: _postings_of(company_id) for slug, company_id in companies.items()} == before
    assert all(
        posting["closed_at"] is None
        for postings in before.values()
        for posting in postings.values()
    )


def test_a_run_where_nothing_fails_records_every_attempted_company(
    engine: Engine, client: TestClient, inline_starter: None
) -> None:
    """Two `200` boards and one `304`: `succeeded`, one outcome each, and no failed row anywhere."""
    _company("acme")
    unchanged = _company("beta")
    _stored_posting(unchanged, external_id="stored")
    _company("globex")
    source = FakeSource(
        {"acme": [_posting("1")], "globex": [_posting("3")]}, not_modified={"beta"}
    )
    _active_resume()

    row = _tick(client, {"greenhouse": source})

    assert (row["status"], row["error"]) == ("succeeded", None)
    outcomes = _company_outcomes(row["id"])
    assert [(entry["slug"], entry["outcome"]) for entry in outcomes] == [
        ("acme", "fetched"),
        ("beta", "not_modified"),
        ("globex", "fetched"),
    ]
    assert [entry for entry in outcomes if entry["outcome"] == "failed"] == []
    assert [entry["error"] for entry in outcomes] == [None, None, None]


def test_a_budget_stopped_run_is_not_a_failed_one_and_records_nothing_for_the_rest(
    engine: Engine, client: TestClient, inline_starter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two of three companies asked: `succeeded` with the count, and no outcome row for the third."""
    for slug in THREE_BOARDS:
        _company(slug)
    source = FakeSource(THREE_BOARDS)
    _active_resume()
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "2")

    row = _tick(client, {"greenhouse": source})

    assert source.calls == ["acme", "beta"]
    assert (row["status"], row["error"]) == ("succeeded", None)  # an unfetched company is not a failure
    assert row["companies_not_fetched"] == 1
    assert [(entry["slug"], entry["outcome"]) for entry in _company_outcomes(row["id"])] == [
        ("acme", "fetched"),
        ("beta", "fetched"),
    ]
