"""The digest (#10): the seam, the cutoff, the selection, the body, and the two values it writes.

Every case here sends through `digest.FakeMailer`, reached through
`app.dependency_overrides[internal.get_mailer]` wherever the real provider runs:
no case constructs `SmtpMailer`, imports `smtplib` or sets an SMTP variable, and
`socket.socket.connect` is patched to raise, so a send that reached a relay would
fail this suite instead of passing quietly. The seven variables the digest reads
are deleted at the start of every case, so a value in the developer's shell
cannot decide one.

The row-level cases ask for the shared `engine` fixture of `tests/conftest.py`,
so each runs on SQLite and on PostgreSQL, and they seed the `match` rows #34's
pipeline would have stored. The tick-level ones drive the real `POST
/internal/tick` over a temporary SQLite file the way `tests/test_internal_tick.py`
does: the starter is replaced by one that runs the work on this thread, so the
run row is final when the `202` is read, and #34's own two collaborators are
injected where #9's provider calls `run_pipeline`, because the digest selects
over every un-notified match and takes only the run's id.

The resume is synthetic and its text is a sentinel: no case reads a real one,
sets a key or opens a socket.
"""

import ast
import inspect
import json
import logging
import os
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from app import db
from app.main import app
from app.matching.prefilter import REJECTION_REASON_PREFIX, PrefilterDecision, record_rejection
from app.matching.scorer import NOT_SCORED_PREFIX, FakeScorer, MatchResult, record_score
from app.models import Company, Match, Posting, Resume, Run
from app.routes import internal
from app.routes.internal import TICK_SECRET_HEADER, get_mailer, get_pipeline, get_run_starter
from app.services import digest, resume
from app.services.digest import (
    DEFAULT_MIN_SCORE,
    DIGEST_FROM,
    DIGEST_MIN_SCORE,
    DIGEST_SUBJECT,
    DIGEST_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    FakeMailer,
    Mailer,
    send_digest,
)
from app.services.pipeline import RUN_REQUEST_BUDGET_VARIABLE, run_pipeline
from app.sources.base import BoardPostings, SourceError, SourcePosting

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = BACKEND_ROOT / "app"
TESTS_ROOT = BACKEND_ROOT / "tests"
DIGEST_SOURCE = (APP_ROOT / "services" / "digest.py").read_text(encoding="utf-8")
INTERNAL_SOURCE = (APP_ROOT / "routes" / "internal.py").read_text(encoding="utf-8")
PIPELINE_SOURCE = (APP_ROOT / "services" / "pipeline.py").read_text(encoding="utf-8")

# The shared secret a tick carries, and a resume whose text holds one value that
# could not reach a subject, a body, a log line or a row by accident.
TICK_SECRET = "tick-secret-sentinel-7f4c1d9ab38e"
RESUME_MARKER = "QA-RESUME-SENTINEL-TEXT: synthetic resume, Python and PostgreSQL."

# The variable that holds the cutoff, and the six a send would need. None of them
# is set for any case in this module.
SMTP_VARIABLES = (SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, DIGEST_FROM, DIGEST_TO)
THE_VARIABLES = (DIGEST_MIN_SCORE, *SMTP_VARIABLES)

RUN_COLUMNS = (
    "id",
    "status",
    "started_at",
    "finished_at",
    "error",
    "postings_seen",
    "matches_created",
    "matches_emailed",
    "companies_not_fetched",
)

# Two instants a day apart, so "newest first" has something to sort and the
# tie-break has two rows written in the same one.
EARLY = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


class RecordingMailer(FakeMailer):
    """A `FakeMailer` that also says when it was called, on a list shared with the test."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def send(self, *, subject: str, body: str) -> None:
        self.events.append("send")
        super().send(subject=subject, body=body)


class TimedMailer(FakeMailer):
    """A `FakeMailer` that remembers the instant it was asked to send."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_at: datetime | None = None

    def send(self, *, subject: str, body: str) -> None:
        self.sent_at = datetime.now(UTC)
        super().send(subject=subject, body=body)


class RaisingMailer:
    """A `Mailer` whose transport fails: the send that reaches nothing and marks nothing."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def send(self, *, subject: str, body: str) -> None:
        raise self.error


class FakeSource:
    """Stands in for `greenhouse.fetch`: it answers with the postings it was built with.

    `failing` names the companies whose board raises instead: the walk records that
    company's `'failed'` outcome and the rest of the run is unaffected.
    """

    def __init__(
        self, postings: list[SourcePosting], *, failing: dict[str, SourceError] | None = None
    ) -> None:
        self.postings = postings
        self.failing = failing or {}
        self.calls: list[str] = []

    def __call__(
        self, slug: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> BoardPostings:
        self.calls.append(slug)
        failure = self.failing.get(slug)
        if failure is not None:
            raise failure
        return BoardPostings(list(self.postings), None, None)


@pytest.fixture(autouse=True)
def no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with no cutoff and no SMTP variable set, whatever the shell holds."""
    for name in THE_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here reaches the network: an unpatched transport fails rather than connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app over one temporary SQLite file, with the tick's own secret set.

    The way `tests/test_internal_tick.py` drives the endpoint: no server and no
    port, and the database is a file under `tmp_path` that the test's own
    teardown removes, so `git status --short` stays empty.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobradar.db'}")
    monkeypatch.setenv(internal.TICK_SECRET_VARIABLE, TICK_SECRET)
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


@pytest.fixture
def fake_mailer() -> Iterator[FakeMailer]:
    """The mailer seam: `get_mailer` replaced, so the digest sends through the fake."""
    fake = FakeMailer()
    app.dependency_overrides[get_mailer] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_mailer, None)


def _run_inline(work: Callable[[], None]) -> None:
    """The starter's body: run the work here and now, so the row is final when the tick answers."""
    work()


def _company(slug: str = "acme", name: str = "Acme Corporation") -> int:
    """One watchlist company, flushed so its id can name its postings: returns that id."""
    with db.session_scope() as session:
        company = Company(name=name, slug=slug)
        session.add(company)
        session.flush()
        return company.id


def _posting(
    company_id: int,
    external_id: str = "1",
    *,
    title: str | None = None,
    url: str | None = None,
) -> int:
    """One `posting` row inserted straight through the app's own session, and its id."""
    with db.session_scope() as session:
        row = Posting(
            company_id=company_id,
            external_id=external_id,
            title=title or f"Backend Engineer {external_id}",
            url=url or f"https://boards.test/{external_id}",
        )
        session.add(row)
        session.flush()
        return row.id


def _match(
    posting_id: int,
    *,
    score: int = 90,
    reason: str = "Synthetic fit.",
    gaps: list[str] | None = None,
    state: str = "new",
    outcome: str = "scored",
    created_at: datetime | None = None,
    notified_at: datetime | None = None,
) -> int:
    """One `match` row on that posting, with #8's stored gaps, and its id.

    `gaps` is written the way `scorer.record_score` writes it -- the JSON text of
    the list, or NULL when the scorer reported none -- so a case reads back the
    shape the digest really meets. `outcome` is written as the writers write it:
    `'scored'` unless a case is about a rejection or a not-scored result, and it
    is the column the selection reads, not the reason's text.
    """
    with db.session_scope() as session:
        row = Match(
            posting_id=posting_id,
            score=score,
            reason=reason,
            gaps=None if gaps is None else json.dumps(gaps),
            state=state,
            outcome=outcome,
            created_at=created_at,
            notified_at=notified_at,
        )
        session.add(row)
        session.flush()
        return row.id


def _active_resume(text: str = RESUME_MARKER) -> None:
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


def _rows_of(model: type) -> list[dict[str, Any]]:
    """Every row of one table as plain column values, in id order, so a stray write is visible."""
    with db.session_scope() as session:
        return [
            {column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in session.scalars(select(model).order_by(model.id))
        ]


def _run_row(run_id: int) -> dict[str, Any]:
    """One `run` row as plain column values."""
    with db.session_scope() as session:
        row = session.get(Run, run_id)
        return {column: getattr(row, column) for column in RUN_COLUMNS}


def _only_message(mailer: FakeMailer) -> tuple[str, str]:
    """The one message that went out, having checked exactly one did."""
    (message,) = mailer.sent
    return message


def _notified(match_id: int) -> datetime | None:
    """The marker on one match row, read back through the app's own session."""
    with db.session_scope() as session:
        return session.get(Match, match_id).notified_at


def _match_id_of(posting_id: int) -> int:
    """The id of the one match row that posting has, read the way `_notified` reads a row."""
    with db.session_scope() as session:
        return session.scalar(select(Match.id).where(Match.posting_id == posting_id))


def _tick(client: TestClient) -> Response:
    """One valid tick: the header a caller sends, with the secret this suite configured."""
    return client.post("/internal/tick", headers={TICK_SECRET_HEADER: TICK_SECRET})


def _counts_writer(postings_seen: int, matches_created: int) -> Callable[..., None]:
    """#34's half, recorded where #9's provider calls it: the counts it earns and no row.

    The walk, the board and the model are #34's own tests' business; what a
    digest case needs is a pipeline half that ends without raising and says how
    far the run got.
    """

    def work(run_id: int, **_: object) -> None:
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            run.postings_seen = postings_seen
            run.matches_created = matches_created

    return work


def _finished_run(matches_emailed: int | None) -> int:
    """One finished `run` row carrying that count, for the cases that only read a row back."""
    with db.session_scope() as session:
        run = Run(
            status="succeeded",
            finished_at=datetime.now(UTC),
            matches_emailed=matches_emailed,
        )
        session.add(run)
        session.flush()
        return run.id


def _emailed(run_id: int) -> int | None:
    """How many matches that run's row says it emailed, read through the app's own session."""
    with db.session_scope() as session:
        return session.get(Run, run_id).matches_emailed


def _calls(source: str, function: str) -> bool:
    """Whether this module *calls* `function`: a call node, not a mention in a docstring."""
    return any(
        isinstance(node, ast.Call) and getattr(node.func, "id", None) == function
        for node in ast.walk(ast.parse(source))
    )


def _assigns(source: str, column: str) -> bool:
    """Whether this module writes a run row's `column`: `row.column = ...` or `Run(column=...)`.

    The two spellings and no others, for the reason
    `tests/test_models.py::_assigns_a_match_column` gives: declaring a column in
    the model, comparing it and rendering it into a response are none of them
    writes.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Run":
            if any(keyword.arg == column for keyword in node.keywords):
                return True
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            targets = []
        if any(getattr(target, "attr", None) == column for target in targets):
            return True
    return False


def _imported_roots(source: str) -> set[str]:
    """The top-level module names `source` imports, however it imports them."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _log_lines(records: list[logging.LogRecord]) -> list[str]:
    """One line per captured record: its message, its arguments and its exception."""
    return [
        f"{record.name}: {record.getMessage()} {record.args!r} {record.exc_info!r}"
        for record in records
    ]


# One seam: the mailer is faked here, and no case reaches the real transport.


def test_the_mailer_seam_is_one_method_taking_the_subject_and_the_body_by_keyword() -> None:
    """The shape `get_mailer` returns and every test replaces, named so a change fails loudly."""
    assert isinstance(FakeMailer(), Mailer)

    parameters = inspect.signature(Mailer.send).parameters

    assert list(parameters) == ["self", "subject", "body"]
    assert [name for name, p in parameters.items() if p.kind is not inspect.Parameter.KEYWORD_ONLY] == [
        "self"
    ]


def test_the_fake_mailer_records_every_message_it_was_handed_in_order() -> None:
    mailer = FakeMailer()

    mailer.send(subject="one", body="first")
    mailer.send(subject="two", body="second")

    assert mailer.sent == [("one", "first"), ("two", "second")]


def test_the_provider_is_the_one_place_the_real_mailer_is_constructed() -> None:
    builders = sorted(
        path.name
        for path in APP_ROOT.rglob("*.py")
        if "SmtpMailer()" in path.read_text(encoding="utf-8")
    )

    assert builders == ["internal.py"]


def test_no_test_reaches_the_real_mailer_or_an_smtp_variable() -> None:
    """This file names the things it forbids, so it does not scan itself."""
    forbidden = ("import smtplib", "from smtplib", "SmtpMailer()", *SMTP_VARIABLES)
    scanned = [
        path for path in sorted(TESTS_ROOT.glob("*.py")) if path.name != Path(__file__).name
    ]

    assert scanned, "a scan over no files proves nothing"
    assert [
        (path.name, needle)
        for path in scanned
        for needle in forbidden
        if needle in path.read_text(encoding="utf-8")
    ] == []


def test_the_suite_needs_no_smtp_variable_set() -> None:
    assert [name for name in THE_VARIABLES if name in os.environ] == []


# The cutoff: one variable, one reader, per call.


def test_the_cutoff_defaults_to_seventy_and_is_read_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DIGEST_MIN_SCORE, raising=False)

    assert digest.min_score() == 70 == DEFAULT_MIN_SCORE

    monkeypatch.setenv(DIGEST_MIN_SCORE, "  ")

    assert digest.min_score() == 70  # an empty value is the default, not a cutoff of nothing

    monkeypatch.setenv(DIGEST_MIN_SCORE, "0")

    assert digest.min_score() == 0  # 0 is legitimate, and `outcome` is what it needs

    monkeypatch.setenv(DIGEST_MIN_SCORE, "85")
    assert digest.min_score() == 85

    monkeypatch.setenv(DIGEST_MIN_SCORE, "60")

    assert digest.min_score() == 60  # read again, not a value imported once


def test_every_variable_is_named_in_digest_and_read_in_no_other_module() -> None:
    for name in THE_VARIABLES:
        readers = sorted(
            path.name
            for path in APP_ROOT.rglob("*.py")
            if name in path.read_text(encoding="utf-8")
        )
        assert readers == ["digest.py"], name

    assert DIGEST_MIN_SCORE == "DIGEST_MIN_SCORE"
    assert DEFAULT_MIN_SCORE == 70


# What qualifies: un-notified, at or above the cutoff, and scored.


def test_a_prefiltered_and_a_not_scored_row_are_never_emailed_at_a_cutoff_of_zero(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All of these sit at `score = 0`, so at 0 the row's own `outcome` is what decides.

    The rows come from the real writers -- `record_rejection`, and `record_score`
    with a genuine 0 and with a model failure -- so this case measures the whole
    chain rather than the selection's spelling of it. The reason texts keep the
    prefixes the writers write, because #30 changes no stored text; the rejected
    row with no prefix on its reason is kept out by the column and not the text,
    and the genuine `('scored', 0)` row -- the case the prefixes could not express
    -- is the one that goes out.
    """
    monkeypatch.setenv(DIGEST_MIN_SCORE, "0")
    acme = _company()
    rejected_posting = _posting(acme, "rejected", title="Intern Backend Engineer")
    record_rejection(rejected_posting, PrefilterDecision(reasons=("title term 'intern'",)))
    rejected = _match_id_of(rejected_posting)
    prefixless_rejection = _match(
        _posting(acme, "rejected-note", title="Filed Away Engineer"),
        score=0,
        reason="An operator's own note, with no prefix on it.",
        outcome="rejected",
    )
    unscored_posting = _posting(acme, "unscored", title="Unknown Data Engineer")
    record_score(
        unscored_posting,
        MatchResult(score=0, reason=NOT_SCORED_PREFIX + "the model call failed", gaps=[]),
    )
    unscored = _match_id_of(unscored_posting)
    scored_posting = _posting(acme, "scored", title="Genuinely Scored Engineer")
    record_score(
        scored_posting, MatchResult(score=0, reason="Synthetic fit, at the floor.", gaps=[])
    )
    scored = _match_id_of(scored_posting)
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    _, body = _only_message(mailer)
    assert "Intern Backend Engineer" not in body
    assert "Filed Away Engineer" not in body
    assert "Unknown Data Engineer" not in body
    assert REJECTION_REASON_PREFIX not in body and NOT_SCORED_PREFIX not in body
    assert "Genuinely Scored Engineer" in body  # a scored 0 is emailed at a cutoff of 0
    assert (_notified(rejected), _notified(prefixless_rejection), _notified(unscored)) == (
        None,
        None,
        None,
    )
    assert _notified(scored) is not None
    assert _emailed(run_id) == 1  # the one row that went out, read off the run's own row


def test_the_selection_reads_the_outcome_column_and_neither_prefix() -> None:
    """The reader changed in #30: one column, and no import of either constant."""
    assert "REJECTION_REASON_PREFIX" not in DIGEST_SOURCE
    assert "NOT_SCORED_PREFIX" not in DIGEST_SOURCE
    assert "app.matching" not in DIGEST_SOURCE
    assert "_is_a_non_score" not in DIGEST_SOURCE


def test_no_match_is_skipped_for_its_state(engine: Engine) -> None:
    """`state` is not a selection rule: a saved or dismissed row is still emailed once."""
    acme = _company()
    states = ("new", "saved", "dismissed")
    ids = {
        state: _match(
            _posting(acme, state, title=f"{state.title()} Engineer"), score=90, state=state
        )
        for state in states
    }
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    _, body = _only_message(mailer)
    assert [state for state in states if f"{state.title()} Engineer" not in body] == []
    assert [_notified(match_id) is None for match_id in ids.values()] == [False, False, False]
    assert _emailed(run_id) == len(states)  # one email, and its row says how many it carried


# One email, carrying all of them, newest first.


def test_one_send_carries_every_qualifying_match_newest_first(engine: Engine) -> None:
    """Newest by `created_at`, and the equal instants by `id` descending."""
    acme = _company()
    _match(_posting(acme, "oldest", title="Oldest Engineer"), created_at=EARLY)
    _match(_posting(acme, "tie-a", title="Tie A Engineer"), created_at=EARLY)
    _match(
        _posting(acme, "tie-b", title="Tie B Engineer"),
        created_at=EARLY + timedelta(hours=1),
    )
    _match(
        _posting(acme, "middle", title="Middle Engineer"), created_at=EARLY + timedelta(days=1)
    )
    _match(_posting(acme, "newest", title="Newest Engineer"), created_at=EARLY + timedelta(days=2))
    _match(
        _posting(acme, "below", title="Below Engineer"),
        score=10,
        created_at=EARLY + timedelta(days=3),
    )
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    assert len(mailer.sent) == 1  # one message for all of them, never one per match or company
    _, body = _only_message(mailer)
    expected = [
        "Newest Engineer",
        "Middle Engineer",
        "Tie B Engineer",  # the later id breaks the tie on the same instant
        "Tie A Engineer",
        "Oldest Engineer",
    ]
    order = [body.index(title) for title in expected]
    assert order == sorted(order)
    assert [body.count(title) for title in expected] == [1] * len(expected)
    assert "Below Engineer" not in body
    assert _emailed(run_id) == len(expected)  # the below-cutoff row is not counted


# What the body carries: who, what, where, how well, why, and each gap on its own line.


def test_the_body_carries_the_company_title_url_score_reason_and_one_line_per_gap(
    engine: Engine,
) -> None:
    acme = _company(name="Acme Corporation")
    gaps = ["No Kubernetes.", "No Terraform."]
    _match(
        _posting(acme, "1", title="Backend Engineer", url="https://boards.test/acme/backend"),
        score=87,
        reason="Synthetic fit.",
        gaps=gaps,
    )
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    subject, body = _only_message(mailer)
    lines = body.splitlines()
    assert subject == DIGEST_SUBJECT
    assert "Acme Corporation: Backend Engineer" in lines  # the company with the posting it owns
    assert [line for line in lines if line.startswith("URL: ")] == [
        "URL: https://boards.test/acme/backend"
    ]
    assert [line for line in lines if line.startswith("Score: ")] == ["Score: 87"]
    assert [line for line in lines if line.startswith("Reason: ")] == ["Reason: Synthetic fit."]
    assert [line for line in lines if line.startswith("Gap: ")] == [
        "Gap: No Kubernetes.",
        "Gap: No Terraform.",
    ]
    assert json.dumps(gaps) not in body  # decoded, never the JSON text #8 stored
    assert [body.count(gap) for gap in gaps] == [1, 1]


def test_a_match_with_no_gaps_still_carries_its_score_and_reason(engine: Engine) -> None:
    """`gaps` is `"[]"` for a scored match with none and NULL for a row that never had any."""
    acme = _company()
    _match(_posting(acme, "empty", title="Empty Gaps Engineer"), score=91, gaps=[])
    _match(_posting(acme, "null", title="Null Gaps Engineer"), score=92, gaps=None)
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    _, body = _only_message(mailer)
    lines = body.splitlines()
    assert sorted(line for line in lines if line.startswith("Score: ")) == [
        "Score: 91",
        "Score: 92",
    ]
    assert sorted(line for line in lines if line.startswith("Reason: ")) == [
        "Reason: Synthetic fit.",
        "Reason: Synthetic fit.",
    ]
    assert [line for line in lines if line.startswith("Gap: ")] == []
    assert "[]" not in body
    assert _emailed(run_id) == 2  # two rows in the one message


# The resume's text is user data: it is in no subject, no body and no log line.


def test_the_resume_marker_is_in_no_subject_no_body_and_no_log_line(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _active_resume()
    acme = _company()
    _match(_posting(acme, "1", title="Backend Engineer"), score=90, gaps=["No Kubernetes."])
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    subject, body = _only_message(mailer)
    assert resume.active_resume_text() == RESUME_MARKER  # the row holding it really exists
    assert RESUME_MARKER not in subject
    assert RESUME_MARKER not in body
    assert [line for line in _log_lines(caplog.records) if RESUME_MARKER in line or body in line] == []
    assert "import logging" not in DIGEST_SOURCE  # and no log line exists to carry either
    assert "logger" not in DIGEST_SOURCE


# Below the cutoff: stored, never emailed, still eligible when the cutoff lowers.


def test_a_below_cutoff_match_is_absent_and_emailed_when_the_cutoff_lowers(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DIGEST_MIN_SCORE, "80")
    acme = _company()
    high = _match(_posting(acme, "high", title="High Engineer"), score=90)
    low = _match(_posting(acme, "low", title="Low Engineer"), score=79)
    first = FakeMailer()
    first_run = _run()

    send_digest(first, first_run)

    _, first_body = _only_message(first)
    assert "High Engineer" in first_body
    assert "Low Engineer" not in first_body
    assert _notified(low) is None  # stored, un-notified, and still eligible
    assert _emailed(first_run) == 1  # one match in that run's one message

    monkeypatch.setenv(DIGEST_MIN_SCORE, "70")
    second = FakeMailer()
    second_run = _run()

    send_digest(second, second_run)

    _, second_body = _only_message(second)
    assert "Low Engineer" in second_body
    assert "High Engineer" not in second_body  # already marked by the run above
    assert _notified(high) is not None
    assert _notified(low) is not None
    assert _emailed(second_run) == 1  # and this row describes this run, not the one before it


# The marker: one write, one instant, only what went out, and only after the send returned.


def test_only_the_sent_matches_are_marked_at_one_aware_utc_instant(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _company()
    first = _match(_posting(acme, "first", title="First Engineer"), score=90)
    second = _match(_posting(acme, "second", title="Second Engineer"), score=95)
    below = _match(_posting(acme, "below", title="Below Engineer"), score=10)
    mailer = TimedMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    marks = {match_id: _notified(match_id) for match_id in (first, second, below)}
    assert marks[below] is None  # nothing that stayed home was marked
    assert marks[first] == marks[second]  # one instant, read once for the whole write
    assert marks[first] is not None and marks[first].tzinfo == UTC
    assert mailer.sent_at is not None
    assert marks[first] >= mailer.sent_at  # read after the send returned, never before
    assert _emailed(run_id) == 2  # and the row counts what went out, not what stayed home


def test_the_marker_is_written_in_one_session_scope_after_the_send(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _company()
    _match(_posting(acme, "1", title="Backend Engineer"), score=90)
    run_id = _run()  # before the counting starts: this row is the test's own plumbing
    events: list[str] = []
    real_session_scope = db.session_scope

    def counted_session_scope() -> Any:
        events.append("session")
        return real_session_scope()

    monkeypatch.setattr(digest.db, "session_scope", counted_session_scope)

    send_digest(RecordingMailer(events), run_id)

    # The read, the send, then the one write the marker and the count share.
    assert events == ["session", "send", "session"]
    assert _emailed(run_id) == 1


def test_a_second_run_sends_nothing_writes_zero_and_changes_no_marker(engine: Engine) -> None:
    acme = _company()
    _match(_posting(acme, "1", title="Backend Engineer"), score=90)
    first = FakeMailer()
    first_run = _run()

    send_digest(first, first_run)

    assert len(first.sent) == 1
    assert _emailed(first_run) == 1
    marked = _rows_of(Match)

    second = FakeMailer()
    second_run = _run()

    send_digest(second, second_run)

    assert second.sent == []
    assert _rows_of(Match) == marked  # the same instant, not a second one
    # `0` and not NULL: this run really did email nothing, which is a different
    # answer from the row of a run that never got to say (a failed send).
    assert _emailed(second_run) == 0


# A send that raises: nothing is marked, and the same matches go out next time.


def test_a_send_that_raises_writes_no_marker_and_its_matches_go_out_next_time(
    engine: Engine,
) -> None:
    acme = _company()
    _match(_posting(acme, "1", title="First Engineer"), score=90)
    _match(_posting(acme, "2", title="Second Engineer"), score=95)
    before = _rows_of(Match)
    error = RuntimeError("the relay refused the message")
    run_id = _run()

    with pytest.raises(RuntimeError) as raised:
        send_digest(RaisingMailer(error), run_id)

    assert raised.value is error
    assert _rows_of(Match) == before  # every marker is still NULL, so nothing is lost
    assert _emailed(run_id) is None  # and no count either: "unknown" is the honest value

    retried = FakeMailer()
    retried_run = _run()

    send_digest(retried, retried_run)

    _, body = _only_message(retried)
    assert "First Engineer" in body and "Second Engineer" in body
    assert _emailed(retried_run) == 2


# A run with nothing to send: no send, no marker, and no row touched.


def test_a_run_with_nothing_to_send_records_zero_and_writes_no_other_row(engine: Engine) -> None:
    acme = _company()
    _match(_posting(acme, "below", title="Below Engineer"), score=10)  # stored, below the cutoff

    assert _rows_of(Match) != []  # so the comparison below is about a row that exists

    before = _rows_of(Match)
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    assert mailer.sent == []
    assert _rows_of(Match) == before
    assert _emailed(run_id) == 0  # "no new matches", written down by the run itself

    with db.session_scope() as session:
        session.execute(delete(Match))

    empty = FakeMailer()
    empty_run = _run()

    send_digest(empty, empty_run)

    assert empty.sent == []  # and the same on a database with no match at all
    assert _emailed(empty_run) == 0


# The row alone: "emailed nothing" told from "emailed", with no mailer and no match row.


def test_the_row_alone_tells_a_run_that_emailed_nothing_from_one_that_did(engine: Engine) -> None:
    """The sentence `_docs/plan.md` asks for, read off three finished rows and nothing else.

    No mailer is built, no log line is read and no `match` row is walked: `0` is
    this run having emailed nothing, `2` is two matches in its one message, and
    NULL is a run that never got to say -- which is why `0` is not the column's
    default. Those three are different answers, and each is told from the others
    by its own row.
    """
    emailed_nothing = _finished_run(0)
    emailed_two = _finished_run(2)
    unknown = _finished_run(None)

    assert [_emailed(run_id) for run_id in (emailed_nothing, emailed_two, unknown)] == [0, 2, None]
    assert _emailed(emailed_nothing) != _emailed(unknown)
    assert _emailed(emailed_two) > _emailed(emailed_nothing)


# The only writes: the marker on the matches, and the run's own count.


def test_the_digest_writes_no_row_but_the_marker_and_the_runs_count(engine: Engine) -> None:
    acme = _company()
    _active_resume()
    _match(_posting(acme, "1", title="Backend Engineer", url="https://boards.test/1"), score=90)
    tables = (Match, Posting, Company, Resume)
    before = {model.__tablename__: _rows_of(model) for model in tables}
    mailer = FakeMailer()
    run_id = _run()

    send_digest(mailer, run_id)

    after = {model.__tablename__: _rows_of(model) for model in tables}

    def without_the_marker(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The marker is the one column this case allows the digest to move."""
        return [
            {name: value for name, value in row.items() if name != "notified_at"} for row in rows
        ]

    # the score, the reason, the gaps, the state and the age are all as they were
    assert without_the_marker(after["match"]) == without_the_marker(before["match"])
    assert [row["id"] for row in after["match"]] == [row["id"] for row in before["match"]]
    assert [row["notified_at"] for row in after["match"]] != [
        row["notified_at"] for row in before["match"]
    ]
    assert after["posting"] == before["posting"]
    assert after["company"] == before["company"]
    assert after["resume"] == before["resume"]
    assert _emailed(run_id) == 1  # the fourth table the digest writes: this run's own count


# Where the call hangs: the second half of #9's provider, after #34's pipeline.


def test_the_digest_is_the_second_half_of_the_providers_callable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(internal, "run_pipeline", lambda run_id, **_: events.append("pipeline"))
    acme = _company()
    _match(_posting(acme, "1", title="Backend Engineer"), score=90)
    run_id = _run()

    get_pipeline(mailer=RecordingMailer(events))(run_id)

    assert events == ["pipeline", "send"]  # the walk first, the digest second
    # The closure handed the digest the id it was called with, so the count lands
    # on this run's row and on no other.
    assert _emailed(run_id) == 1


def test_the_provider_still_takes_no_arguments_and_its_work_still_takes_the_run_id() -> None:
    assert list(inspect.signature(get_pipeline(mailer=FakeMailer())).parameters) == ["run_id"]
    assert callable(get_pipeline())  # #9's tests call it with no arguments at all


def test_the_run_id_bearing_send_digest_has_one_caller_outside_its_own_module() -> None:
    """The provider's closure, and nothing else: no route and no other module sends a digest."""
    callers = sorted(
        path.relative_to(APP_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if _calls(path.read_text(encoding="utf-8"), "send_digest")
    )

    assert callers == ["routes/internal.py"]
    assert "send_digest(mailer, run_id)" in INTERNAL_SOURCE  # with the id the closure was handed


def test_the_digest_is_the_only_module_that_writes_the_emailed_count() -> None:
    """One writer for the column: the digest's own `_record`, and no other module assigns it."""
    assigning = sorted(
        path.relative_to(APP_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if _assigns(path.read_text(encoding="utf-8"), "matches_emailed")
    )

    assert assigning == ["services/digest.py"]


def test_a_partly_failed_run_still_emails_the_matches_its_answering_boards_produced(
    client: TestClient,
    fake_mailer: FakeMailer,
    inline_starter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk recorded a failed board and returned, so the digest half runs (#59).

    The run's own row says both things at once: one board failed, so the status is
    `failed` and the summary names it, and the board that did answer produced a
    match, so the mail carries it and `matches_emailed` counts it. A partly failed
    run that sent nothing would be the silent miss `_docs/plan.md` forbids.
    """
    _company("acme")
    _company("beta")
    _active_resume()
    source = FakeSource(
        [
            SourcePosting(
                external_id="1",
                title="Backend Engineer",
                location="Berlin",
                url="https://boards.test/1",
                description="Synthetic posting, written for this test.",
            )
        ],
        failing={"beta": SourceError("The board 'beta' refused to answer: HTTP 503")},
    )

    def the_real_walk(run_id: int, **_: object) -> None:
        """#34's walk itself, so the outcome rows and the counts are the ones #59 writes."""
        run_pipeline(
            run_id,
            sources={"greenhouse": source},
            scorer=FakeScorer(MatchResult(score=90, reason="Synthetic fit.", gaps=[])),
        )

    monkeypatch.setattr(internal, "run_pipeline", the_real_walk)

    response = _tick(client)

    row = _run_row(response.json()["id"])
    assert (row["status"], row["error"]) == ("failed", "1 of 2 companies failed: 'beta'")
    assert (row["postings_seen"], row["matches_created"]) == (1, 1)
    assert row["matches_emailed"] == 1  # the run failed for one board and mailed the other's match
    _, body = _only_message(fake_mailer)
    assert "Backend Engineer" in body
    (match_row,) = _rows_of(Match)
    assert _notified(match_row["id"]) is not None  # the match went out, so it is marked


def test_a_run_whose_pipeline_raises_sends_no_email_and_the_match_goes_out_next_time(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = _company()

    def the_walk_stored_it_and_the_next_board_failed(run_id: int, **_: object) -> None:
        _match(_posting(acme, "1", title="Backend Engineer"), score=90)
        raise SourceError("The board for company 'beta' could not be fetched: HTTP 503")

    monkeypatch.setattr(internal, "run_pipeline", the_walk_stored_it_and_the_next_board_failed)
    failing = FakeMailer()
    failing_run = _run()

    with pytest.raises(SourceError):
        get_pipeline(mailer=failing)(failing_run)

    assert failing.sent == []
    assert [_notified(row["id"]) for row in _rows_of(Match)] == [None]
    assert _emailed(failing_run) is None  # no send, so no count: the run never got to say

    monkeypatch.setattr(internal, "run_pipeline", lambda run_id, **_: None)
    succeeding = FakeMailer()

    get_pipeline(mailer=succeeding)(_run())

    _, body = _only_message(succeeding)
    assert "Backend Engineer" in body  # the unscored-by-this-run match was not lost


def test_the_pipeline_module_gains_no_socket_no_email_and_no_configuration() -> None:
    """#34's own test pins its imports; this pins that #10 added nothing to that module.

    `os` was in that pinned set while the walk read no environment at all; #56's two
    settings are the only reason it is imported now, and `#34's` own test in
    `test_pipeline.py` pins that they are the only two names it reads.
    """
    roots = _imported_roots(PIPELINE_SOURCE)

    assert {"smtplib", "email"} & roots == set()
    assert "send_digest" not in PIPELINE_SOURCE
    assert "Mailer" not in PIPELINE_SOURCE


# Through the tick: the real provider, its own seam, and the row it ends up writing.


def test_a_tick_sends_through_the_overridden_mailer_and_only_marks_what_it_sent(
    client: TestClient, fake_mailer: FakeMailer, inline_starter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SMTP variable is set, no socket is opened, and the send goes through the fake."""
    monkeypatch.setattr(internal, "run_pipeline", _counts_writer(7, 3))
    acme = _company()
    sent = _match(_posting(acme, "sent", title="Backend Engineer"), score=90)
    below = _match(_posting(acme, "below", title="Below Engineer"), score=10)

    response = _tick(client)

    assert response.status_code == 202
    _, body = _only_message(fake_mailer)
    assert "Backend Engineer" in body and "Below Engineer" not in body
    assert _notified(sent) is not None
    assert _notified(below) is None
    row = _run_row(response.json()["id"])
    assert (row["status"], row["error"]) == ("succeeded", None)
    assert (row["postings_seen"], row["matches_created"]) == (7, 3)
    assert row["matches_emailed"] == 1  # the one match this run's digest carried


def test_a_tick_with_nothing_to_send_succeeds_with_the_counts_the_pipeline_wrote(
    client: TestClient, fake_mailer: FakeMailer, inline_starter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary "nothing new" run: no send, no marker, and the counts are #34's."""
    monkeypatch.setattr(internal, "run_pipeline", _counts_writer(7, 3))
    acme = _company()
    below = _match(_posting(acme, "below", title="Below Engineer"), score=10)

    response = _tick(client)

    assert fake_mailer.sent == []
    assert _notified(below) is None
    row = _run_row(response.json()["id"])
    assert (row["status"], row["error"]) == ("succeeded", None)
    assert (row["postings_seen"], row["matches_created"]) == (7, 3)
    assert row["matches_emailed"] == 0  # and the row says the run had nothing to send


def test_a_tick_whose_send_raises_ends_the_run_failed_with_the_transport_message(
    client: TestClient, fake_mailer: FakeMailer, inline_starter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(internal, "run_pipeline", _counts_writer(1, 1))
    _active_resume()
    acme = _company()
    match_id = _match(_posting(acme, "1", title="Backend Engineer"), score=90)
    error = RuntimeError("the relay refused the message")
    app.dependency_overrides[get_mailer] = lambda: RaisingMailer(error)

    failing = _tick(client)

    row = _run_row(failing.json()["id"])
    assert row["status"] == "failed"
    assert row["error"] == str(error)  # the transport's own message, never a body
    assert RESUME_MARKER not in str(row["error"]) and "Backend Engineer" not in str(row["error"])
    assert _notified(match_id) is None  # so the match is still there to send
    assert (row["postings_seen"], row["matches_created"]) == (1, 1)
    assert row["matches_emailed"] is None  # a send that failed wrote no count (#10's rule)

    app.dependency_overrides[get_mailer] = lambda: fake_mailer

    succeeding = _tick(client)

    _, body = _only_message(fake_mailer)
    assert "Backend Engineer" in body
    assert _notified(match_id) is not None
    succeeded_row = _run_row(succeeding.json()["id"])
    assert succeeded_row["status"] == "succeeded"
    assert succeeded_row["matches_emailed"] == 1  # the retry's own row carries the count


def test_the_resume_marker_reaches_no_subject_and_no_body_through_a_tick(
    client: TestClient,
    fake_mailer: FakeMailer,
    inline_starter: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#34's real walk, scoring with the marker resume, then #10's real digest."""
    caplog.set_level(logging.DEBUG)
    _active_resume()
    acme = _company()
    source = FakeSource(
        [
            SourcePosting(
                external_id="1",
                title="Backend Engineer",
                location="Berlin",
                url="https://boards.test/1",
                description="Synthetic description.",
            )
        ]
    )
    scorer = FakeScorer(
        MatchResult(score=90, reason="Synthetic fit.", gaps=["No Kubernetes."])
    )
    monkeypatch.setattr(
        internal,
        "run_pipeline",
        lambda run_id, **_: run_pipeline(run_id, sources={"greenhouse": source}, scorer=scorer),
    )

    response = _tick(client)

    subject, body = _only_message(fake_mailer)
    assert [text for _, text in scorer.calls] == [RESUME_MARKER]  # the run really scored with it
    assert source.calls == ["acme"]
    assert RESUME_MARKER not in subject
    assert RESUME_MARKER not in body
    assert [line for line in _log_lines(caplog.records) if RESUME_MARKER in line] == []
    assert "Backend Engineer" in body
    row = _run_row(response.json()["id"])
    assert (row["status"], row["postings_seen"], row["matches_created"]) == ("succeeded", 1, 1)
    assert _notified(_rows_of(Match)[0]["id"]) is not None


def test_a_budget_stopped_walk_still_sends_its_digest_and_records_the_count(
    client: TestClient,
    fake_mailer: FakeMailer,
    inline_starter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that ran out of budget is not a failed run: the day's mail goes out over what it fetched.

    The real walk, bounded to one of the two watched companies, then #10's real
    digest over the matches that one board produced. The second company is asked
    nothing, the run ends `succeeded`, and its own row carries both the count of
    companies left for a later run and the count of matches this digest emailed.
    """
    _active_resume()
    _company("acme")
    _company("globex")
    source = FakeSource(
        [
            SourcePosting(
                external_id="1",
                title="Backend Engineer",
                location="Berlin",
                url="https://boards.test/1",
                description="Synthetic description.",
            )
        ]
    )
    scorer = FakeScorer(MatchResult(score=90, reason="Synthetic fit.", gaps=["No Kubernetes."]))
    monkeypatch.setattr(
        internal,
        "run_pipeline",
        lambda run_id, **_: run_pipeline(run_id, sources={"greenhouse": source}, scorer=scorer),
    )
    monkeypatch.setenv(RUN_REQUEST_BUDGET_VARIABLE, "1")

    response = _tick(client)

    _, body = _only_message(fake_mailer)
    assert "Backend Engineer" in body
    assert source.calls == ["acme"]  # the company past the bound was asked nothing
    row = _run_row(response.json()["id"])
    assert (row["status"], row["error"]) == ("succeeded", None)
    assert row["companies_not_fetched"] == 1
    assert row["matches_emailed"] == 1  # the digest behind the stopped walk still wrote its count
