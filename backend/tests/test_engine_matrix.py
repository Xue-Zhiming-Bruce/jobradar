"""How the suite picks an engine, checked by running pytest in a fresh process.

The engine matrix is one environment variable, so the way to test the choice is
to start pytest again with that variable unset, wrong and stray. Each subprocess
runs the parametrised modules -- never this module -- so the checks terminate.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import db

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = BACKEND_ROOT / "app"

# The modules whose tests are parametrised over both engines.
PARAMETRISED_MODULES = ["tests/test_engine_parity.py", "tests/test_db.py"]

# A server that is not there. The port is reserved and never listening, so the
# failure is an immediate connection refusal rather than a timeout.
UNREACHABLE_TEST_DATABASE_URL = "postgresql+psycopg://postgres:jobradar@127.0.0.1:1/postgres"

_POSTGRES = "[postgres]"
_SKIPPED = "SKIPPED "


def _pytest(arguments: list[str], **environment: str | None) -> subprocess.CompletedProcess[str]:
    """Run pytest in a fresh interpreter with the named variables (`None` removes one)."""
    env = dict(os.environ)
    for name, value in environment.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return subprocess.run(
        [sys.executable, "-m", "pytest", *arguments],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _collected_ids(**environment: str | None) -> list[str]:
    """The test ids `--collect-only` lists; collection opens no database."""
    completed = _pytest([*PARAMETRISED_MODULES, "--collect-only", "-q"], **environment)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [line.strip() for line in completed.stdout.splitlines() if "::" in line]


def _postgres_cases(ids: list[str]) -> list[str]:
    return [test_id for test_id in ids if test_id.endswith(_POSTGRES)]


def _write_marker_database(path: Path) -> None:
    """A database the app is configured to use, holding data the suite must not touch."""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker (value) VALUES ('the app was here')")


def _marker_database_contents(path: Path) -> tuple[list[str], list[str]]:
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        values = [row[0] for row in connection.execute("SELECT value FROM marker")]
    return tables, values


def test_with_the_variable_unset_the_postgres_cases_skip_and_say_why() -> None:
    collected = _collected_ids(DATABASE_URL=None, TEST_DATABASE_URL=None)
    postgres_cases = _postgres_cases(collected)

    completed = _pytest(
        [*PARAMETRISED_MODULES, "-q", "-rs"], DATABASE_URL=None, TEST_DATABASE_URL=None
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert postgres_cases  # the ids exist with the variable unset, they are not hidden
    skips = [line for line in completed.stdout.splitlines() if line.startswith(_SKIPPED)]
    assert len(skips) == len(postgres_cases)
    assert all("TEST_DATABASE_URL" in line for line in skips)
    assert f"{len(postgres_cases)} skipped" in completed.stdout


def test_the_same_tests_are_collected_with_and_without_the_variable(
    test_database_url: str | None,
) -> None:
    """The variable chooses whether PostgreSQL runs, never which tests exist."""
    without = _collected_ids(DATABASE_URL=None, TEST_DATABASE_URL=None)
    # Collection opens no database, so any value proves the ids do not depend on
    # it: the real one when the suite was started against a server, a wrong one
    # otherwise.
    with_variable = _collected_ids(
        DATABASE_URL=None,
        TEST_DATABASE_URL=test_database_url or UNREACHABLE_TEST_DATABASE_URL,
    )

    assert without == with_variable
    assert _postgres_cases(without)


def test_an_unreachable_test_database_fails_instead_of_skipping() -> None:
    collected = _collected_ids(
        DATABASE_URL=None, TEST_DATABASE_URL=UNREACHABLE_TEST_DATABASE_URL
    )
    postgres_cases = _postgres_cases(collected)

    completed = _pytest(
        [*PARAMETRISED_MODULES, "-q", "-rE"],
        DATABASE_URL=None,
        TEST_DATABASE_URL=UNREACHABLE_TEST_DATABASE_URL,
    )

    assert completed.returncode != 0
    assert _SKIPPED not in completed.stdout  # a dead server is not "not configured"
    # The short summary appends the error's reason once the line fits the
    # terminal, and a CI runner's terminal is wide enough for it: the ids are what
    # this compares, so the reason is cut off. The id is never wrapped, at any
    # width.
    errors = [
        line.split(" ", 1)[1].split(" - ", 1)[0]
        for line in completed.stdout.splitlines()
        if line.startswith("ERROR ")
    ]
    assert errors == postgres_cases
    assert f"{len(collected) - len(postgres_cases)} passed" in completed.stdout


def test_a_stray_database_url_is_neither_used_nor_wiped(tmp_path: Path) -> None:
    marker_database = tmp_path / "app.db"
    _write_marker_database(marker_database)

    completed = _pytest(
        [*PARAMETRISED_MODULES, "-q", "-rs"],
        DATABASE_URL=f"sqlite:///{marker_database}",
        TEST_DATABASE_URL=None,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "TEST_DATABASE_URL" in completed.stdout  # the PostgreSQL cases skipped, visibly
    # The suite ran on SQLite without ever opening the database `DATABASE_URL`
    # names: no JobRadar table appeared in it and the marker row is still there.
    assert _marker_database_contents(marker_database) == (["marker"], ["the app was here"])


def test_no_app_module_reads_the_test_variable() -> None:
    readers = sorted(
        path.relative_to(BACKEND_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if "TEST_DATABASE_URL" in path.read_text(encoding="utf-8")
    )

    assert readers == []


def test_the_test_variable_never_configures_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TEST_DATABASE_URL` picks the engine matrix; `DATABASE_URL` configures the app."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://postgres@nowhere.test/jobradar")

    assert db.database_url() == f"sqlite:///{db.DEFAULT_DATABASE_PATH}"


@pytest.mark.parametrize("unset", [True, False])
def test_an_empty_test_database_url_skips_like_an_unset_one(unset: bool) -> None:
    """A blank value is not a server: it skips instead of failing to connect."""
    completed = _pytest(
        [*PARAMETRISED_MODULES, "-q", "-rs"],
        DATABASE_URL=None,
        TEST_DATABASE_URL=None if unset else "",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "TEST_DATABASE_URL" in completed.stdout
