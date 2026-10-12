"""One workflow, once, through the real app over real PostgreSQL.

The whole path `_docs/plan.md` describes, in one case: the real `POST /resume`, the
real `POST /watchlist`, the real `POST /internal/tick` with its own thread and its
run row, the real `run_pipeline` and the real `send_digest`, and then the second
tick that must change nothing. Exactly three collaborators are faked, each at its
named seam and nowhere else: the Greenhouse fetch, the LLM scorer and the mailer.
`get_pipeline` and `get_run_starter` are not overridden, so the run goes through
the provider this app ships, on the daemon thread it starts.

Everything the case sends is synthetic: the resume text, the company, and both
postings, whose URLs are all `https://example.test/...`. The board, the model and
the mail are faked, so no request leaves the process; a run that reached for a
real board would fail here rather than quietly pass, because
`tests/integration/conftest.py` patches `socket.socket.connect` to raise.

The one prefilter rule the case relies on is a title term it sets itself: of the
two postings the board returns, one is rejected by that rule ("Prefiltered out:
...") and one reaches the fake scorer, which is what makes `matches_created` 1
out of 2 postings seen.
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.matching.prefilter import PREFILTER_TITLE_TERMS, REJECTION_REASON_PREFIX
from app.matching.scorer import FakeScorer, MatchResult
from app.models import Company, Match, Posting, Resume, Run
from app.routes import internal
from app.routes.internal import get_mailer
from app.routes.watchlist import get_source
from app.services import resume
from app.services.digest import DIGEST_SUBJECT, FakeMailer
from app.sources.greenhouse import SourcePosting
from tests.integration.support import RUNNING, rows_of, wait_for_terminal

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

COMPANY_NAME = "Acme Corporation"
SLUG = "acme"

# The board the scripted source answers with: two postings, one of which trips the
# title rule the case configures. Every URL is synthetic.
SURVIVOR = SourcePosting(
    external_id="8172510",
    title="Senior Backend Engineer",
    location="Berlin",
    url="https://example.test/board/8172510",
    description="Synthetic posting written for this test: Python and PostgreSQL.",
)
REJECTED = SourcePosting(
    external_id="8172511",
    title="Intern Data Analyst",
    location="Berlin",
    url="https://example.test/board/8172511",
    description="Synthetic posting written for this test.",
)
BOARD = [SURVIVOR, REJECTED]

# The one rule set for this case: `PREFILTER_TITLE_TERMS` rejects the posting above.
REJECTION_TERM = "intern"

# What the fake scorer answers for the survivor: the score, reason and gaps the
# case then reads back out of the `match` row and out of `GET /matches`.
SCORE = MatchResult(
    score=87, reason="Synthetic fit for this test.", gaps=["No Kubernetes experience."]
)

# A synthetic resume, written for this test: the repository is public and this is
# not a real person's resume. The sentinel is a line of it that must not reach a
# row, a response or a log line.
RESUME_SENTINEL = "SENTINEL-RESUME-LINE-THAT-MUST-NOT-LEAVE-THE-DATABASE"
RESUME_TEXT = (
    "SYNTHETIC RESUME, written for this test: no real person's data.\n"
    f"{RESUME_SENTINEL}\n"
    "Skills: Python, PostgreSQL, FastAPI.\n"
)
SYNTHETIC_RESUME_FILENAME = "synthetic-resume.txt"


class ScriptedSource:
    """Stands in for `greenhouse.fetch`: it records every slug and answers from the script.

    A slug nobody scripted raises instead of answering an empty board, so a case
    that forgot to script one fails rather than passing as a company whose board
    listed nothing.
    """

    def __init__(self, boards: dict[str, list[SourcePosting]]) -> None:
        self.boards = boards
        self.slugs: list[str] = []

    def __call__(self, slug: str) -> list[SourcePosting]:
        self.slugs.append(slug)
        if slug not in self.boards:
            raise AssertionError(f"No board is scripted for '{slug}'")
        return list(self.boards[slug])


@pytest.fixture
def scripted_source() -> ScriptedSource:
    """The fetch seam, for both places this app reaches a board."""
    return ScriptedSource({SLUG: BOARD})


@pytest.fixture
def scorer() -> FakeScorer:
    """The LLM seam's answer: the result `app.matching.scorer.FakeScorer` returns unchanged."""
    return FakeScorer(SCORE)


@pytest.fixture
def mailer() -> FakeMailer:
    """The mail seam's record: every message the digest sends through it."""
    return FakeMailer()


@pytest.fixture
def three_faked_collaborators(
    scripted_source: ScriptedSource,
    scorer: FakeScorer,
    mailer: FakeMailer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """The only three things this suite fakes, each at the seam the real code reads it from.

    The board is installed twice because the app reaches it twice and at two
    different names: `POST /watchlist` takes its probe from `get_source`, while
    the tick's real `get_pipeline` provider reads `fetch` out of its own module,
    so that name is what `monkeypatch` replaces. The scorer is the same shape:
    the provider constructs `LlmScorer()` itself, so the class is what is
    replaced, and it answers with this case's one `FakeScorer` instance -- built
    by the fake the app already ships, not by a fake of this suite's own. The
    mailer is `get_mailer`, the seam #10 declares.

    Nothing else is faked, and both overrides are removed at the end of the case,
    so `app.dependency_overrides` is empty again for whatever runs next.
    """
    app.dependency_overrides[get_source] = lambda: scripted_source
    app.dependency_overrides[get_mailer] = lambda: mailer
    monkeypatch.setattr(internal, "fetch", scripted_source)
    monkeypatch.setattr(internal, "LlmScorer", lambda: scorer)
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_source, None)
        app.dependency_overrides.pop(get_mailer, None)


def git(*arguments: str) -> str:
    """Run git in the repository the suite lives in."""
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def tick(client: TestClient, tick_headers: dict[str, str]) -> dict[str, Any]:
    """One real tick, through the real route, with the shared secret header."""
    response = client.post("/internal/tick", headers=tick_headers)
    assert response.status_code == 202, response.text
    return response.json()


def only_message(mailer: FakeMailer) -> tuple[str, str]:
    """The one message that went out, having checked exactly one did."""
    (message,) = mailer.sent
    return message


def test_the_whole_workflow_from_the_upload_to_the_recorded_digest(
    client: TestClient,
    three_faked_collaborators: None,
    scripted_source: ScriptedSource,
    scorer: FakeScorer,
    mailer: FakeMailer,
    uploads_dir: Path,
    tick_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload, watch, tick, wait, read the matches, read the digest, tick again."""
    repository_before = git("status", "--short", "--untracked-files=all")
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, REJECTION_TERM)
    assert set(app.dependency_overrides) == {get_source, get_mailer}

    # Steps 1-2: the two writes, both through the real routes.
    upload = client.post(
        "/resume",
        files={"file": (SYNTHETIC_RESUME_FILENAME, RESUME_TEXT.encode("utf-8"), "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["is_active"] is True

    (resume_row,) = rows_of(Resume)  # exactly one, and it is the active one
    assert resume_row["is_active"] is True
    assert resume_row["text"] == RESUME_TEXT
    assert resume.active_resume_text() == RESUME_TEXT
    stored = Path(resume_row["stored_path"])
    assert stored.parent == uploads_dir  # nothing lands in the repository or under ~/.jobradar
    assert stored.read_text(encoding="utf-8") == RESUME_TEXT

    watch = client.post("/watchlist", json={"name": COMPANY_NAME, "slug": SLUG})
    assert watch.status_code == 201, watch.text
    assert (watch.json()["slug"], watch.json()["is_active"]) == (SLUG, True)

    (company_row,) = rows_of(Company)  # exactly one, with that slug, active
    assert (company_row["name"], company_row["slug"], company_row["is_active"]) == (
        COMPANY_NAME,
        SLUG,
        True,
    )
    assert scripted_source.slugs == [SLUG]  # the route's own probe answered at the seam

    # Step 3: the tick answers 202 at once, and its row is what is read.
    started = tick(client, tick_headers)
    assert started["status"] == RUNNING
    assert (started["postings_seen"], started["matches_created"]) == (0, 0)
    # The same row the 202 carried is readable at `GET /runs/{id}`: the work has
    # already been handed over, so this read may catch the counts it has written
    # by now -- the row's identity, not its mid-run counts, is what is asserted.
    assert client.get(f"/runs/{started['id']}").json()["id"] == started["id"]

    finished = wait_for_terminal(client, started["id"])
    assert finished["status"] == "succeeded"
    assert finished["error"] is None
    assert (finished["postings_seen"], finished["matches_created"]) == (2, 1)
    assert scripted_source.slugs == [SLUG, SLUG]  # the probe, then the run's own fetch

    # Step 4: the real prefilter, the fake scorer, and the rows they stored.
    response = client.get("/matches")
    assert response.status_code == 200, response.text
    matches = response.json()
    assert len(matches) == 2
    by_title = {match["title"]: match for match in matches}
    assert set(by_title) == {SURVIVOR.title, REJECTED.title}

    rejected = by_title[REJECTED.title]
    assert rejected["score"] == 0
    assert rejected["reason"] == REJECTION_REASON_PREFIX + f"title term '{REJECTION_TERM}'"
    assert rejected["reason"].startswith(REJECTION_REASON_PREFIX)
    assert rejected["gaps"] == []

    survivor = by_title[SURVIVOR.title]
    assert survivor["score"] == SCORE.score
    assert survivor["reason"] == SCORE.reason
    assert survivor["gaps"] == SCORE.gaps  # a list, not the JSON text the column holds
    assert survivor["company_name"] == COMPANY_NAME
    assert survivor["url"] == SURVIVOR.url
    assert [posting.title for posting, _ in scorer.calls] == [SURVIVOR.title]

    # No part of the resume's text is in a response or in a row the run wrote.
    assert RESUME_SENTINEL not in response.text
    written = [*rows_of(Match), *rows_of(Posting), *rows_of(Run)]
    assert [
        name for row in written for name, value in row.items() if RESUME_SENTINEL in str(value)
    ] == []

    # Step 5: the digest really sent, and recorded what it sent.
    (subject, body) = only_message(mailer)
    assert subject == DIGEST_SUBJECT
    assert COMPANY_NAME in body
    assert SURVIVOR.title in body
    assert SURVIVOR.url in body

    notified = {row["id"]: row["notified_at"] for row in rows_of(Match)}
    assert notified[survivor["id"]] is not None  # read through the session: the route hides it
    assert notified[rejected["id"]] is None

    # And the second tick over the unchanged board changes nothing and sends nothing.
    matches_before = rows_of(Match)
    postings_before = rows_of(Posting)

    again = tick(client, tick_headers)
    again_finished = wait_for_terminal(client, again["id"])
    assert again_finished["status"] == "succeeded"
    assert again_finished["error"] is None
    assert (again_finished["postings_seen"], again_finished["matches_created"]) == (2, 0)

    assert rows_of(Match) == matches_before  # score, reason, gaps, state, notified_at included
    assert rows_of(Posting) == postings_before  # one row per external id, nothing re-inserted
    assert len(mailer.sent) == 1

    assert set(app.dependency_overrides) == {get_source, get_mailer}
    assert git("status", "--short", "--untracked-files=all") == repository_before
