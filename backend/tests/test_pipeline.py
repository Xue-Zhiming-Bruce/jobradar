"""What one digest run does: the walk, the fetch, the ingest, the gate, the score and the counts.

Every case here runs with `socket.socket.connect` patched to raise the way
`tests/test_greenhouse_source.py` does, so a run that reached for a board fails
instead of quietly passing, and with the model's three variables and the
prefilter's four deleted, so a key or a term in the developer's shell cannot
decide a case. The source and the scorer are this module's own fakes: the source
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
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db
from app.main import app
from app.matching.prefilter import (
    PREFILTER_LOCATION_TERMS,
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
from app.models import Company, Match, Posting, Run
from app.routes.internal import TICK_SECRET_HEADER, get_pipeline, get_run_starter
from app.services import pipeline as pipeline_module
from app.services import resume
from app.services.ingest import IngestResult
from app.services.pipeline import NO_ACTIVE_RESUME, run_pipeline
from app.sources.greenhouse import (
    BoardUnavailable,
    MalformedPayload,
    SourceError,
    SourcePosting,
    SourceTimeout,
    UnknownSlug,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_MODULE = BACKEND_ROOT / "app" / "services" / "pipeline.py"
PIPELINE_SOURCE = PIPELINE_MODULE.read_text(encoding="utf-8")

# The variables the pipeline must not read, and the modules it must not import:
# the source and the scorer arrive as arguments, so neither configuration nor a
# client belongs in this module.
CONFIGURATION_VARIABLES = (
    LLM_API_KEY,
    LLM_MODEL,
    SCORER_MAX_POSTINGS,
    PREFILTER_TITLE_TERMS,
    PREFILTER_SENIORITY_TERMS,
    PREFILTER_LOCATION_TERMS,
    PREFILTER_MUST_HAVE_KEYWORDS,
)
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}

# The secret a tick carries, and a resume with a value in it that could not reach
# a log line, a column or a response by accident.
TICK_SECRET = "tick-secret-sentinel-7f4c1d9ab38e"
RESUME_TEXT = "QA-RESUME-SENTINEL-TEXT: synthetic resume, Python and PostgreSQL."

# Every column of a run row: the two counts the pipeline writes, and the three the
# lifecycle owns and the pipeline must leave alone.
RUN_COLUMNS = (
    "id",
    "status",
    "started_at",
    "finished_at",
    "error",
    "postings_seen",
    "matches_created",
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
    passing as a board nobody listed anything on.
    """

    def __init__(
        self,
        boards: dict[str, list[SourcePosting]] | None = None,
        *,
        failing: dict[str, SourceError] | None = None,
    ) -> None:
        self.boards = boards or {}
        self.failing = failing or {}
        self.calls: list[str] = []

    def __call__(self, slug: str) -> list[SourcePosting]:
        """The board this slug was scripted with, or the error it was scripted to raise."""
        self.calls.append(slug)
        failure = self.failing.get(slug)
        if failure is not None:
            raise failure
        if slug not in self.boards:
            raise UnknownSlug(f"No board is scripted for '{slug}'")
        return self.boards[slug]


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
    """Each case starts with no key, no model, no cap and no prefilter terms set."""
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


def _company(slug: str = "acme", *, name: str | None = None, is_active: bool = True) -> int:
    """One watchlist company, flushed so its id can name its postings: returns that id."""
    with db.session_scope() as session:
        company = Company(name=name or slug.capitalize(), slug=slug, is_active=is_active)
        session.add(company)
        session.flush()
        return company.id


def _active_resume(text: str = RESUME_TEXT) -> None:
    """The one active resume, written through #3's own writer and no file at all."""
    resume.record_active_resume(
        "synthetic-resume.txt", resume.TEXT_PLAIN, text, Path("/tmp/synthetic-resume.txt")
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

    run_pipeline(run, source=source, scorer=FakeScorer(_result()))

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

    run_pipeline(run, source=source, scorer=FakeScorer(_result()))

    assert source.calls == ["acme"]
    assert _postings_of(stopped) == {}
    assert _run_row(run)["postings_seen"] == 1


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

    run_pipeline(_run(), source=source, scorer=FakeScorer(_result()))

    assert calls == [(first, ["1"]), (second, ["2", "3"])]
    assert _rows(Posting) == 0  # the fake wrote nothing, and the pipeline wrote no row itself


def test_no_posting_row_exists_that_ingest_did_not_create(engine: Engine) -> None:
    """The pipeline inserts nothing: every row is one the service was given the payload for."""
    company = _company("acme")
    payload = [_posting("1"), _posting("2")]
    _active_resume()

    run_pipeline(_run(), source=FakeSource({"acme": payload}), scorer=FakeScorer(_result()))

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
    run_pipeline(first, source=source, scorer=first_scorer)
    _mark_saved(_matches()[0]["id"])  # a digest already alerted on one of them
    before = _matches()
    postings_before = _postings_of(company)
    second = _run()
    second_scorer = FakeScorer(_result())

    run_pipeline(second, source=source, scorer=second_scorer)

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
        _run(), source=FakeSource({"acme": [_posting("Y")]}), scorer=FakeScorer(_result())
    )
    (match_id,) = [row["id"] for row in _matches()]
    _mark_saved(match_id)
    with db.session_scope() as session:
        row = session.get(Match, match_id)
        row.score = 77
        row.reason = "The owner saved this one."
    run_pipeline(_run(), source=FakeSource({"acme": []}), scorer=FakeScorer(_result()))
    assert _postings_of(company)["Y"]["closed_at"] is not None
    before = _matches()
    reopened = _run()
    scorer = FakeScorer(_result())
    source = FakeSource({"acme": [_posting("Y")]})

    run_pipeline(reopened, source=source, scorer=scorer)

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

    run_pipeline(_run(), source=source, scorer=scorer)

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

    run_pipeline(run, source=source, scorer=scorer)

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

    run_pipeline(run, source=FakeSource({"acme": payload}), scorer=scorer)

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

    run_pipeline(run, source=FakeSource({"acme": [_posting("1")]}), scorer=FakeScorer(outage))

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
        run_pipeline(run, source=source, scorer=RaisingScorer(failure))

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

    run_pipeline(_run(), source=source, scorer=scorer)

    assert reads == [0]  # once, and before the first fetch
    assert {text for _, text in scorer.calls} == {RESUME_TEXT}


def test_a_run_with_no_active_resume_raises_naming_it_and_calls_no_board(engine: Engine) -> None:
    company = _company("acme")
    source = FakeSource({"acme": [_posting("1")]})
    scorer = FakeScorer(_result())
    run = _run()

    with pytest.raises(RuntimeError) as raised:
        run_pipeline(run, source=source, scorer=scorer)

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
        run, source=FakeSource({"beta": beta, "acme": acme}), scorer=FakeScorer(_result())
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


def test_a_run_that_fails_half_way_keeps_the_counts_and_the_rows_it_earned(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed board aborts the walk, and nothing already written is rolled back."""
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    watched = _company("acme")
    _company("beta")
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

    with pytest.raises(SourceError) as raised:
        run_pipeline(run, source=source, scorer=FakeScorer(_result()))

    assert "beta" in str(raised.value)
    assert len(_postings_of(watched)) == 3
    assert len(_matches()) == 3
    row = _run_row(run)
    assert (row["postings_seen"], row["matches_created"]) == (3, 1)
    assert _rows(Posting) == 3  # the failing company stored nothing


def test_a_retry_after_a_failed_board_stores_and_matches_only_what_is_new(engine: Engine) -> None:
    _company("acme")
    _company("beta")
    _active_resume()
    first = _run()
    source = FakeSource(
        {"acme": [_posting("1")]},
        failing={"beta": SourceError("The board 'beta' could not be reached")},
    )
    with pytest.raises(SourceError):
        run_pipeline(first, source=source, scorer=FakeScorer(_result()))
    before = _matches()
    assert len(before) == 1

    second = _run()
    retried = FakeSource({"acme": [_posting("1")], "beta": [_posting("2")]})
    run_pipeline(second, source=retried, scorer=FakeScorer(_result()))

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
        succeeding, source=FakeSource({"acme": [_posting("1")]}), scorer=FakeScorer(_result())
    )

    failing = _run()
    with pytest.raises(SourceError):
        run_pipeline(
            failing,
            source=FakeSource(failing={"acme": SourceError("no board today")}),
            scorer=FakeScorer(_result()),
        )

    for run_id, seen, matched in ((succeeding, 1, 1), (failing, 0, 0)):
        row = _run_row(run_id)
        assert (row["status"], row["finished_at"], row["error"]) == ("running", None, None)
        assert (row["postings_seen"], row["matches_created"]) == (seen, matched)


# A board that fails fails the run, and the message names the board.


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
def test_each_source_error_fails_the_run_and_the_message_names_the_slug(
    engine: Engine, failure: SourceError
) -> None:
    _company("acme")
    _active_resume()
    run = _run()

    with pytest.raises(SourceError) as raised:
        run_pipeline(
            run, source=FakeSource(failing={"acme": failure}), scorer=FakeScorer(_result())
        )

    assert str(raised.value) == (
        f"The board for company 'acme' could not be fetched: {failure}"
    )
    assert isinstance(raised.value, SourceError)
    assert _rows(Posting) == 0
    assert _run_row(run)["postings_seen"] == 0


# The seam, the wiring, and what the module is not allowed to reach.


def test_the_seam_is_the_run_id_and_the_two_injected_collaborators() -> None:
    """The shape #9's provider returns and the tests drive, named so a change fails loudly."""
    parameters = inspect.signature(run_pipeline).parameters

    assert list(parameters) == ["run_id", "source", "scorer"]
    assert parameters["run_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["source"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["scorer"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_pipeline_reads_no_configuration_and_imports_no_network_client() -> None:
    assert "environ" not in PIPELINE_SOURCE
    assert "getenv" not in PIPELINE_SOURCE
    roots = _imported_roots(PIPELINE_SOURCE)
    assert roots & NETWORK_MODULES == set()
    assert {"os", "smtplib", "email"} & roots == set()


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
        run_pipeline, source=source, scorer=scorer
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


def test_the_pipeline_writes_no_row_of_its_own_but_the_runs_counts() -> None:
    """A posting and a match row are written by the services: this module only reads them back."""
    assert "session.add(" not in PIPELINE_SOURCE
    assert "session.delete(" not in PIPELINE_SOURCE
    assert ".commit(" not in PIPELINE_SOURCE
