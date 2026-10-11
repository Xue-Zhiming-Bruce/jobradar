"""The uv project file is part of the contract: one ORM at most, one Python pin.

#2 introduced SQLAlchemy, so the skeleton's "no database at all" guard is now
"at most one ORM". #22 needed a PostgreSQL driver to run the second half of the
engine matrix and declared it, so the guard also allows that one driver; it is
what keeps a second ORM or a third driver from creeping in unnoticed. The
exactly-one-ORM / exactly-one-driver rule and the driver's final home are #23's.
"""

import re
import tomllib
from pathlib import Path

from app.main import API_VERSION

BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# Every package that would be a second ORM or another database driver. The list
# stays as it is; what #22 narrowed is the set allowed to appear, not the list.
_DATABASE_PACKAGES = {
    "aiosqlite",
    "alembic",
    "asyncpg",
    "databases",
    "psycopg",
    "psycopg2",
    "sqlalchemy",
    "sqlite3",
    "sqlmodel",
}

# One ORM, and the one PostgreSQL driver the engine matrix needs.
_ALLOWED_DATABASE_PACKAGES = {"psycopg", "sqlalchemy"}


def _declared_requirements() -> list[str]:
    return _PYPROJECT["project"]["dependencies"] + _PYPROJECT["dependency-groups"]["dev"]


def _package_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def test_no_second_orm_or_database_driver_is_declared() -> None:
    declared = {_package_name(requirement) for requirement in _declared_requirements()}
    assert declared & _DATABASE_PACKAGES <= _ALLOWED_DATABASE_PACKAGES


def test_the_one_postgres_driver_is_a_runtime_dependency() -> None:
    """The app connects to PostgreSQL as well as SQLite, so the driver ships with it."""
    runtime = {
        _package_name(requirement) for requirement in _PYPROJECT["project"]["dependencies"]
    }
    assert runtime & _DATABASE_PACKAGES == _ALLOWED_DATABASE_PACKAGES


def test_python_version_is_pinned_to_one_minor_release() -> None:
    assert _PYPROJECT["project"]["requires-python"] == ">=3.12,<3.13"


def test_app_version_matches_the_project_version() -> None:
    assert _PYPROJECT["project"]["version"] == API_VERSION
