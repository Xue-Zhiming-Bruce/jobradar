"""The uv project file is part of the contract: one ORM at most, one Python pin.

#2 introduced SQLAlchemy, so the skeleton's "no database at all" guard is now
"at most one ORM". The exactly-one-ORM / exactly-one-PostgreSQL-driver rule and
the driver itself are finalised by #23; `_DATABASE_PACKAGES` stays the list that
keeps a second ORM or driver from creeping in unnoticed.
"""

import re
import tomllib
from pathlib import Path

from app.main import API_VERSION

BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

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


def _declared_requirements() -> list[str]:
    return _PYPROJECT["project"]["dependencies"] + _PYPROJECT["dependency-groups"]["dev"]


def _package_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def test_at_most_one_database_orm_is_declared() -> None:
    declared = {_package_name(requirement) for requirement in _declared_requirements()}
    assert declared & _DATABASE_PACKAGES <= {"sqlalchemy"}


def test_python_version_is_pinned_to_one_minor_release() -> None:
    assert _PYPROJECT["project"]["requires-python"] == ">=3.12,<3.13"


def test_app_version_matches_the_project_version() -> None:
    assert _PYPROJECT["project"]["version"] == API_VERSION
