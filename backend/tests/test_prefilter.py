"""What the prefilter decides, rule by rule, and what one recorded rejection writes.

Every case here runs with `socket.socket.connect` patched to raise, the way
`test_greenhouse_source.py` does, so an implementation that reached for a board
fails instead of quietly passing. No case sets an API key, starts a server or
reads a fixture: the postings are built in this module, and the four rules are
driven by `monkeypatch.setenv` -- an autouse fixture removes them first, so a
variable left in the developer's shell cannot decide a case here.

Every rule case asserts the whole decision -- the `reasons` tuple, or the exact
string that reached `match.reason` -- rather than a boolean, so a decision taken
for some other reason fails here. The recording cases ask for the shared `engine`
fixture of `tests/conftest.py`, so each of them runs on SQLite and on PostgreSQL.
"""

import ast
import inspect
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from app import db
from app.matching import prefilter as prefilter_module
from app.matching.prefilter import (
    DEFAULT_TERMS,
    PREFILTER_LOCATION_TERMS,
    PREFILTER_MUST_HAVE_KEYWORDS,
    PREFILTER_SENIORITY_TERMS,
    PREFILTER_TITLE_TERMS,
    REJECTION_REASON_PREFIX,
    PrefilterDecision,
    location_terms,
    must_have_keywords,
    prefilter,
    record_rejection,
    seniority_terms,
    title_terms,
)
from app.models import Company, Match, Posting, Run
from app.sources.base import SourcePosting

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PREFILTER_MODULE = BACKEND_ROOT / "app" / "matching" / "prefilter.py"
PREFILTER_SOURCE = PREFILTER_MODULE.read_text(encoding="utf-8")

# The four variables, in the order the rules read them, with the reader beside
# each, so one case can drive the configuration without naming it twice.
THE_FOUR = (
    (PREFILTER_TITLE_TERMS, title_terms),
    (PREFILTER_SENIORITY_TERMS, seniority_terms),
    (PREFILTER_LOCATION_TERMS, location_terms),
    (PREFILTER_MUST_HAVE_KEYWORDS, must_have_keywords),
)
TermsReader = Callable[[], tuple[str, ...]]
VARIABLE_NAMES = tuple(name for name, _ in THE_FOUR)

# The modules a network client would come from, and the terms that would name a
# model or its credential: this module imports neither and reads no key.
NETWORK_MODULES = {"http", "httpx", "httpx2", "requests", "socket", "urllib", "aiohttp"}
MODEL_TERMS = ("api_key", "api-key", "openai", "anthropic", "Authorization")

# Every column of both tables, so a case can compare a whole row without naming
# the columns one by one and a change to either table shows up as a failure here.
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


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module reaches the network: an unpatched client fails rather connects."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def no_terms_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case starts with all four variables unset, whatever its shell holds."""
    for name in VARIABLE_NAMES:
        monkeypatch.delenv(name, raising=False)


def _posting(**overrides: object) -> SourcePosting:
    """A posting as the adapter would hand it over: synthetic, and this module's own."""
    fields: dict[str, object] = {
        "external_id": "1",
        "title": "Backend Engineer",
        "location": "Berlin",
        "url": "https://example.test/1",
        "description": "We use Python and PostgreSQL daily.",
    }
    fields.update(overrides)
    return SourcePosting(**fields)


def _posting_that_trips_every_rule() -> SourcePosting:
    """One posting each of the four rules can reject, so one variable turns one rule on."""
    return _posting(
        title="Junior Intern Backend Engineer",
        location="Berlin, Germany",
        description="We use Python and PostgreSQL daily.",
    )


def _stored_postings(count: int) -> list[int]:
    """`count` posting rows for a rejection to point at, stored here and by no app function."""
    with db.session_scope() as session:
        company = Company(name="Acme", slug="acme")
        session.add(company)
        session.flush()
        postings = [
            Posting(
                company_id=company.id,
                external_id=str(number),
                title="Backend Engineer",
                location="Berlin",
                url=f"https://example.test/{number}",
                description="We use Python and PostgreSQL daily.",
            )
            for number in range(1, count + 1)
        ]
        session.add_all(postings)
        session.flush()
        return [posting.id for posting in postings]


def _one_stored_posting() -> int:
    return _stored_postings(1)[0]


def _matches() -> list[dict[str, object]]:
    """Every `match` row as plain column values, in id order."""
    with db.session_scope() as session:
        return [
            {column: getattr(row, column) for column in MATCH_COLUMNS}
            for row in session.scalars(select(Match).order_by(Match.id))
        ]


def _stored_posting(posting_id: int) -> dict[str, object]:
    """One `posting` row as plain column values, so a stray write to it is visible."""
    with db.session_scope() as session:
        row = session.get(Posting, posting_id)
        return {column: getattr(row, column) for column in POSTING_COLUMNS}


def _rows(model: type) -> int:
    with db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(model))


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# The configuration: four variables, read in one place, unset meaning "rule off".


def test_the_four_variables_are_read_in_one_module_only() -> None:
    """The one-place rule #4 proved for the payload, proved here for the rules."""
    readers = sorted(
        path.relative_to(BACKEND_ROOT).as_posix()
        for path in (BACKEND_ROOT / "app").rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in VARIABLE_NAMES)
    )

    assert readers == ["app/matching/prefilter.py"]


@pytest.mark.parametrize("name", VARIABLE_NAMES)
def test_each_variable_name_is_a_module_constant(name: str) -> None:
    assert getattr(prefilter_module, name) == name


def test_a_list_is_trimmed_lower_cased_and_stripped_of_empties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern,  Apprentice ,")

    assert title_terms() == ("intern", "apprentice")


@pytest.mark.parametrize(("name", "reader"), THE_FOUR, ids=[name for name, _ in THE_FOUR])
def test_an_unset_or_empty_variable_means_no_terms(
    name: str, reader: TermsReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared default: all four empty, so an unconfigured JobRadar rejects nothing."""
    monkeypatch.delenv(name, raising=False)
    assert reader() == DEFAULT_TERMS == ()

    monkeypatch.setenv(name, "")
    assert reader() == ()

    monkeypatch.setenv(name, "   ")
    assert reader() == ()


def test_a_variable_is_read_per_call_and_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `monkeypatch.setenv` between two calls changes what the next call sees."""
    assert title_terms() == ()

    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    assert title_terms() == ("intern",)

    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern, junior")
    assert title_terms() == ("intern", "junior")

    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "")
    assert title_terms() == ()


IMPORT_SCRIPT = f"""
import os
import sys

sys.path.insert(0, {str(BACKEND_ROOT)!r})

reads = []


class RecordingEnvironment(dict):
    def get(self, key, *args):
        reads.append(key)
        return args[0] if args else None

    def __getitem__(self, key):
        reads.append(key)
        raise KeyError(key)


os.environ = RecordingEnvironment()
from app.matching import prefilter

assert [name for name in reads if name.startswith("PREFILTER_")] == [], reads
"""


def test_importing_the_prefilter_reads_none_of_the_four_variables() -> None:
    """The readers run per call: importing the module must not settle the rules."""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", IMPORT_SCRIPT],
        cwd=BACKEND_ROOT,
        env={},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_the_comma_limit_is_named_in_the_module_docstring() -> None:
    """The format's one limit, in the docstring's own words."""
    docstring = " ".join((prefilter_module.__doc__ or "").split())

    assert "a term cannot contain a comma" in docstring


# The decision: one rule per case, the whole decision asserted.


def test_a_posting_that_trips_no_rule_passes_with_no_reasons() -> None:
    decision = prefilter(_posting_that_trips_every_rule())

    assert decision == PrefilterDecision(reasons=())
    assert decision.passed is True


def test_the_title_rule_rejects_on_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")

    assert prefilter(_posting(title="Intern Program, Summer")) == PrefilterDecision(
        reasons=("title term 'intern'",)
    )


def test_the_seniority_rule_rejects_on_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFILTER_SENIORITY_TERMS, "junior")

    assert prefilter(_posting(title="Junior Backend Engineer", description="No keyword here.")) == (
        PrefilterDecision(reasons=("seniority term 'junior'",))
    )


def test_the_location_rule_rejects_on_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFILTER_LOCATION_TERMS, "berlin")

    assert prefilter(_posting(location="Berlin, Germany")) == PrefilterDecision(
        reasons=("location term 'berlin'",)
    )


def test_the_must_have_rule_rejects_a_keyword_absent_from_both_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PREFILTER_MUST_HAVE_KEYWORDS, "rust")

    assert prefilter(
        _posting(title="Backend Engineer", description="We use Python and PostgreSQL daily.")
    ) == PrefilterDecision(reasons=("missing must-have 'rust'",))


def test_a_must_have_keyword_in_either_field_is_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule reads the title and the description together, one field at a time."""
    monkeypatch.setenv(PREFILTER_MUST_HAVE_KEYWORDS, "python, postgresql")

    assert prefilter(
        _posting(title="Python Engineer", description="We use PostgreSQL daily.")
    ).passed is True


def test_matching_ignores_case_on_both_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "InTeRn")

    assert prefilter(_posting(title="INTERN Program")) == PrefilterDecision(
        reasons=("title term 'intern'",)
    )


def test_a_none_location_can_never_trip_the_location_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PREFILTER_LOCATION_TERMS, "berlin")

    assert prefilter(_posting(location=None)).passed is True


def test_a_none_description_leaves_the_must_have_rule_to_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` reads as the empty string: the posting is judged by its title alone."""
    monkeypatch.setenv(PREFILTER_MUST_HAVE_KEYWORDS, "python, rust")

    assert prefilter(_posting(title="Python Engineer", description=None)) == PrefilterDecision(
        reasons=("missing must-have 'rust'",)
    )


def test_the_must_have_rule_fires_once_for_every_keyword_that_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PREFILTER_MUST_HAVE_KEYWORDS, "python, rust, kubernetes")

    assert prefilter(
        _posting(title="Backend Engineer", description="We use Python and PostgreSQL daily.")
    ) == PrefilterDecision(reasons=("missing must-have 'rust'", "missing must-have 'kubernetes'"))


# The order, and the one string a rejection is recorded as.


def test_a_posting_that_trips_several_rules_records_all_of_them_in_rule_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    monkeypatch.setenv(PREFILTER_SENIORITY_TERMS, "junior")
    monkeypatch.setenv(PREFILTER_LOCATION_TERMS, "berlin")
    monkeypatch.setenv(PREFILTER_MUST_HAVE_KEYWORDS, "rust")

    decision = prefilter(_posting_that_trips_every_rule())

    assert decision.reasons == (
        "title term 'intern'",
        "seniority term 'junior'",
        "location term 'berlin'",
        "missing must-have 'rust'",
    )


def test_the_clauses_keep_the_order_the_variable_lists_its_terms_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not sorted, not set order: the reason is a function of the variable's own list."""
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "zebra, intern")

    assert prefilter(_posting(title="Intern Zebra Engineer")).reasons == (
        "title term 'zebra'",
        "title term 'intern'",
    )


def test_the_same_posting_and_the_same_variables_always_give_the_same_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern, junior")

    decisions = {prefilter(_posting(title="Junior Intern")).reasons for _ in range(5)}

    assert decisions == {("title term 'intern'", "title term 'junior'")}


# Each rule is driven by its own variable.


@pytest.mark.parametrize(
    ("variable", "value", "clause"),
    [
        (PREFILTER_TITLE_TERMS, "intern", "title term 'intern'"),
        (PREFILTER_SENIORITY_TERMS, "intern", "seniority term 'intern'"),
        (PREFILTER_LOCATION_TERMS, "berlin", "location term 'berlin'"),
        (PREFILTER_MUST_HAVE_KEYWORDS, "rust", "missing must-have 'rust'"),
    ],
    ids=["title", "seniority", "location", "must-have"],
)
def test_setting_one_variable_turns_its_rule_on_and_emptying_it_turns_it_off(
    variable: str, value: str, clause: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    posting = _posting_that_trips_every_rule()
    assert prefilter(posting) == PrefilterDecision(reasons=())

    monkeypatch.setenv(variable, value)
    rejected = prefilter(posting)
    assert rejected.reasons == (clause,)
    assert rejected.passed is False

    monkeypatch.setenv(variable, "")
    assert prefilter(posting).passed is True
    monkeypatch.delenv(variable, raising=False)
    assert prefilter(posting).passed is True


def test_adding_a_term_rejects_the_posting_the_shorter_list_let_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posting = _posting(title="Intern Backend Engineer")

    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "manager")
    assert prefilter(posting).passed is True

    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "manager, intern")
    assert prefilter(posting).reasons == ("title term 'intern'",)


def test_a_term_that_does_not_occur_rejects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every rule configured, none of them matching: "off" is a list that does not occur."""
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "manager")
    monkeypatch.setenv(PREFILTER_SENIORITY_TERMS, "principal")
    monkeypatch.setenv(PREFILTER_LOCATION_TERMS, "tokyo")
    monkeypatch.setenv(PREFILTER_MUST_HAVE_KEYWORDS, "python")

    assert prefilter(_posting()).passed is True


# The function is pure: it answers twice, and writes nothing anywhere.


def test_two_calls_on_one_posting_agree_and_leave_both_tables_empty(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    posting = _posting(title="Intern Backend Engineer")

    first = prefilter(posting)
    second = prefilter(posting)

    assert first == second
    assert first.reasons == ("title term 'intern'",)
    assert _rows(Posting) == 0
    assert _rows(Match) == 0


# One recorded rejection, on the row the table keys by posting.


def test_one_call_records_the_rejection_on_the_row_keyed_by_the_posting(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    posting_id = _one_stored_posting()
    posting_before = _stored_posting(posting_id)
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")

    record_rejection(posting_id, prefilter(_posting(title="Intern Backend Engineer")))

    rows = _matches()
    assert len(rows) == 1
    stored = rows[0]
    assert stored["posting_id"] == posting_id
    assert stored["score"] == 0
    assert stored["reason"] == "Prefiltered out: title term 'intern'"
    assert stored["gaps"] is None
    assert stored["state"] == "new"
    assert stored["notified_at"] is None
    assert stored["created_at"].tzinfo is not None
    assert stored["created_at"].utcoffset() == timedelta(0)
    assert datetime.now(UTC) - stored["created_at"] < timedelta(minutes=1)
    # The posting row, and every other table, are as they were.
    assert _stored_posting(posting_id) == posting_before
    assert _rows(Posting) == 1
    assert _rows(Run) == 0


def test_the_recorded_reason_is_the_prefix_and_the_clauses_in_rule_order(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact string the issue names, and the marker #10 reads."""
    posting_id = _one_stored_posting()
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern, junior")

    record_rejection(posting_id, prefilter(_posting(title="Junior Intern")))

    assert REJECTION_REASON_PREFIX == "Prefiltered out: "
    assert _matches()[0]["reason"] == "Prefiltered out: title term 'intern'; title term 'junior'"


def test_a_second_call_updates_the_one_row_and_keeps_its_id_and_created_at(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`match.posting_id` is unique: a re-rejection updates, and never inserts a second row."""
    posting_id = _one_stored_posting()
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    record_rejection(posting_id, prefilter(_posting(title="Intern Backend Engineer")))
    first = _matches()[0]

    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "junior")
    record_rejection(posting_id, prefilter(_posting(title="Junior Backend Engineer")))

    rows = _matches()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["posting_id"] == posting_id
    assert rows[0]["created_at"] == first["created_at"]
    assert rows[0]["score"] == 0
    assert rows[0]["state"] == first["state"] == "new"
    assert rows[0]["reason"] == "Prefiltered out: title term 'junior'"


def test_a_rejection_of_another_posting_is_a_row_of_its_own(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_posting, second_posting = _stored_postings(2)
    monkeypatch.setenv(PREFILTER_TITLE_TERMS, "intern")
    decision = prefilter(_posting(title="Intern Backend Engineer"))

    record_rejection(first_posting, decision)
    record_rejection(second_posting, decision)

    assert {row["posting_id"] for row in _matches()} == {first_posting, second_posting}


def test_a_decision_that_passed_is_refused_and_leaves_no_row(engine: Engine) -> None:
    """A score-0 row for a posting that passed is a rejection the digest must never see."""
    posting_id = _one_stored_posting()

    with pytest.raises(ValueError) as raised:
        record_rejection(posting_id, prefilter(_posting()))

    assert str(posting_id) in str(raised.value)
    assert _matches() == []


# The seam itself, and what this module is not allowed to reach.


def test_the_seam_is_one_posting_and_one_decision() -> None:
    """The shape #9 calls, named here so a change to it fails loudly."""
    assert list(inspect.signature(prefilter).parameters) == ["posting"]
    assert list(inspect.signature(record_rejection).parameters) == ["posting_id", "decision"]
    assert PrefilterDecision.__dataclass_fields__.keys() == {"reasons"}
    assert PrefilterDecision.__dataclass_params__.frozen
    assert PrefilterDecision(reasons=()).passed is True
    assert PrefilterDecision(reasons=("title term 'x'",)).passed is False


def test_the_prefilter_reaches_no_network_and_names_no_model_or_key() -> None:
    assert _imported_roots(PREFILTER_SOURCE) & NETWORK_MODULES == set()
    assert [term for term in MODEL_TERMS if term in PREFILTER_SOURCE] == []
    assert "fetch(" not in PREFILTER_SOURCE


def test_only_the_recorder_opens_a_session() -> None:
    """One writer in this module, and the pure function is not it."""
    opening: list[str] = []
    for node in ast.parse(PREFILTER_SOURCE).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "session_scope"
            for inner in ast.walk(node)
        ):
            opening.append(node.name)

    assert opening == ["record_rejection"]


def test_the_module_holds_the_readers_the_pure_function_and_the_recorder() -> None:
    names = [
        node.name
        for node in ast.parse(PREFILTER_SOURCE).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]

    assert names == [
        "title_terms",
        "seniority_terms",
        "location_terms",
        "must_have_keywords",
        "prefilter",
        "record_rejection",
    ]
