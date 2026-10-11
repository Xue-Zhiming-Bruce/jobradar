"""The uv project file is part of the contract: one ORM, one PostgreSQL driver.

#2 declared SQLAlchemy and #22 the PostgreSQL driver, both as runtime
dependencies; #23 states the rule they make up: the project holds exactly one
package from the ORM set and exactly one from the PostgreSQL-driver set, in the
runtime dependencies, however a second one would be spelled (`peewee`,
`sqlmodel`, `pg8000`, `psycopg2`, `asyncpg`). The two sets are named rather than
flattened into one list of database packages, so the migration tool #21 declares
as a runtime dependency is not mistaken for an ORM or for a driver.
"""

import re
import tomllib
from pathlib import Path

from app.main import API_VERSION

BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# Every package that would be a second ORM. The one this project holds is named
# below, so adding a peer fails the ORM tests rather than passing unnoticed.
_ORM_PACKAGES = {"peewee", "pony", "sqlalchemy", "sqlmodel", "tortoise-orm"}

# Every package that would be a second PostgreSQL driver -- including another
# spelling of the same driver, since `psycopg2` is not psycopg 3.
_POSTGRES_DRIVER_PACKAGES = {"asyncpg", "pg8000", "psycopg", "psycopg2", "psycopg2-binary"}

# Neither an ORM nor a PostgreSQL driver, but a second database layer all the
# same: code reaching for one of these would not be going through these models.
# `alembic`, the migration tool #21 declares, is deliberately absent -- it is
# neither of the two things this rule names.
_OTHER_DATABASE_PACKAGES = {"aiosqlite", "databases", "sqlite3"}

# The one of each the project declares, spelled the way `pyproject.toml` does.
_THE_ORM = "sqlalchemy"
_THE_POSTGRES_DRIVER = "psycopg"


def _declared_requirements() -> list[str]:
    return _PYPROJECT["project"]["dependencies"] + _PYPROJECT["dependency-groups"]["dev"]


def _package_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _declared_packages() -> set[str]:
    """The runtime dependencies and the dev group, in package-name spelling."""
    return {_package_name(requirement) for requirement in _declared_requirements()}


def _runtime_packages() -> set[str]:
    """Only the dependencies the shipped image gets."""
    return {_package_name(requirement) for requirement in _PYPROJECT["project"]["dependencies"]}


def test_exactly_one_orm_is_declared() -> None:
    """A second ORM in either group -- `peewee`, `sqlmodel` -- fails here."""
    assert _declared_packages() & _ORM_PACKAGES == {_THE_ORM}


def test_exactly_one_postgres_driver_is_declared() -> None:
    """A second driver in either group -- `pg8000`, `psycopg2`, `asyncpg` -- fails here."""
    assert _declared_packages() & _POSTGRES_DRIVER_PACKAGES == {_THE_POSTGRES_DRIVER}


def test_no_other_database_package_is_declared() -> None:
    assert _declared_packages() & _OTHER_DATABASE_PACKAGES == set()


def test_the_orm_and_the_postgres_driver_are_runtime_dependencies() -> None:
    """The app connects to both engines, so both ship with it and not with the tests."""
    assert _runtime_packages() & _ORM_PACKAGES == {_THE_ORM}
    assert _runtime_packages() & _POSTGRES_DRIVER_PACKAGES == {_THE_POSTGRES_DRIVER}


def test_python_version_is_pinned_to_one_minor_release() -> None:
    assert _PYPROJECT["project"]["requires-python"] == ">=3.12,<3.13"


def test_app_version_matches_the_project_version() -> None:
    assert _PYPROJECT["project"]["version"] == API_VERSION
