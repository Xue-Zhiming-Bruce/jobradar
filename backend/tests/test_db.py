"""The local SQLite path, the one create-tables function, and the session helper.

`DATABASE_URL` is never set for the whole test session: each test points the app
at a scratch file with `monkeypatch`, so the suite proves the default path and
the configurable one without ever leaving a database behind in the checkout.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from app import db
from app.main import app
from app.models import Base, Company, Resume

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BACKEND_ROOT.parent

# Test fixtures are synthetic on purpose: the repository is public and holds no
# resume, real or otherwise.
SYNTHETIC_RESUME_TEXT = (
    "SYNTHETIC RESUME TEXT. Written for this test; not a real person's resume.\n"
    "Skills: Python, SQL."
)

_PRELUDE = "import sys; sys.path.insert(0, {root!r}); "
_RESOLVE_DEFAULT_URL = _PRELUDE + "from app.db import database_url; print(database_url())"
_IMPORT_DB = _PRELUDE + "import app.db; print(app.db._engine)"

# One whole app run in one process: create the tables, optionally write a row, read back.
_APP_RUN = (
    _PRELUDE
    + """
from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import Company

init_db()
with session_scope() as session:
    if {write}:
        session.add(Company(name="Acme", slug="acme"))
    print([company.slug for company in session.scalars(select(Company))])
"""
)


def _run(
    script: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a script in a fresh interpreter, the way `uv run uvicorn` would start one."""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=cwd,
        env={} if env is None else env,
        capture_output=True,
        text=True,
    )


def _point_at_a_scratch_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "jobradar.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    return path


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def test_the_environment_wins_when_database_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    assert db.database_url() == "sqlite:///:memory:"


@pytest.mark.parametrize("empty", ["unset", ""])
def test_the_default_is_the_local_file_beside_the_app(
    empty: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if empty == "unset":
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", "")

    assert db.database_url() == f"sqlite:///{db.DEFAULT_DATABASE_PATH}"


def test_the_default_file_is_the_same_from_the_root_and_from_backend() -> None:
    """A clean checkout works with no setup, whichever directory the app starts in."""
    expected = f"sqlite:///{db.DEFAULT_DATABASE_PATH}\n"

    from_root = _run(_RESOLVE_DEFAULT_URL.format(root=str(BACKEND_ROOT)), REPOSITORY_ROOT)
    from_backend = _run(_RESOLVE_DEFAULT_URL.format(root=str(BACKEND_ROOT)), BACKEND_ROOT)

    assert from_root.returncode == 0, from_root.stderr
    assert from_backend.returncode == 0, from_backend.stderr
    assert from_root.stdout == from_backend.stdout == expected


def test_importing_the_database_module_builds_no_engine_and_no_file(tmp_path: Path) -> None:
    """T1 guards `app.main`; `app.db` carries the same rule for the engine itself."""
    completed = _run(_IMPORT_DB.format(root=str(BACKEND_ROOT)), tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "None"
    assert list(tmp_path.iterdir()) == []


def test_database_url_is_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in (BACKEND_ROOT / "app").rglob("*.py")
        if "DATABASE_URL" in path.read_text(encoding="utf-8")
    )

    assert readers == ["db.py"]


def test_init_db_creates_the_five_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)

    db.init_db()

    assert set(inspect(db.get_engine()).get_table_names()) == set(Base.metadata.tables)


def test_init_db_twice_is_a_no_op_that_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    db.init_db()

    assert set(inspect(db.get_engine()).get_table_names()) == set(Base.metadata.tables)
    with db.session_scope() as session:
        assert [company.slug for company in session.scalars(select(Company))] == ["acme"]


def test_two_consecutive_app_runs_share_the_file_and_keep_their_rows(tmp_path: Path) -> None:
    """Restarting against the same file is safe: the second run finds the first run's row."""
    database_file = tmp_path / "jobradar.db"
    env = {"DATABASE_URL": f"sqlite:///{database_file}"}

    first = _run(_APP_RUN.format(root=str(BACKEND_ROOT), write=True), REPOSITORY_ROOT, env)
    second = _run(_APP_RUN.format(root=str(BACKEND_ROOT), write=False), REPOSITORY_ROOT, env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    # The writing run saw only its own row; the writing-free run read it back.
    assert first.stdout.strip() == "['acme']"
    assert second.stdout.strip() == "['acme']"


def test_starting_the_app_creates_the_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_file = _point_at_a_scratch_file(tmp_path, monkeypatch)

    with TestClient(app):
        pass

    assert database_file.is_file()
    assert set(inspect(db.get_engine()).get_table_names()) == set(Base.metadata.tables)


def test_the_session_helper_commits_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()

    with db.session_scope() as session:
        session.add(Company(name="Acme", slug="acme"))

    assert db.get_engine().pool.checkedout() == 0
    with db.session_scope() as session:
        assert session.scalars(select(Company)).one().name == "Acme"


def test_the_session_helper_rolls_back_and_closes_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()
    engine = db.get_engine()

    with pytest.raises(RuntimeError, match="boom"):
        with db.session_scope() as session:
            session.add(Company(name="Acme", slug="acme"))
            session.flush()  # the INSERT has reached the database
            raise RuntimeError("boom")

    assert engine.pool.checkedout() == 0  # the connection went back to the pool
    with db.session_scope() as session:
        assert session.scalars(select(Company)).all() == []


def test_resume_text_lives_in_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at_a_scratch_file(tmp_path, monkeypatch)
    db.init_db()

    with db.session_scope() as session:
        session.add(
            Resume(
                filename="synthetic-resume.txt",
                content_type="text/plain",
                text=SYNTHETIC_RESUME_TEXT,
                stored_path=str(tmp_path / "synthetic-resume.txt"),
            )
        )

    with db.session_scope() as session:
        resume = session.scalars(select(Resume)).one()
        assert resume.text == SYNTHETIC_RESUME_TEXT
        assert resume.is_active is True
        assert resume.uploaded_at is not None


def test_the_default_database_file_is_ignored_by_git() -> None:
    relative = db.DEFAULT_DATABASE_PATH.relative_to(REPOSITORY_ROOT).as_posix()

    ignored = _git("check-ignore", relative)

    assert ignored == relative


def test_no_database_file_is_committed_or_left_for_git_to_report() -> None:
    assert _git("ls-files", "*.db", "*.sqlite3", "data/*") == ""

    reported = _git("status", "--short", "--untracked-files=all").splitlines()
    assert [line for line in reported if line.endswith((".db", ".sqlite3"))] == []
