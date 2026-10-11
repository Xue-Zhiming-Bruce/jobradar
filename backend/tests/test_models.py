"""The five tables, declared once, under the column names later tasks read.

`__tablename__` may appear in no module other than `app/models.py`: one place
declares table structure, so one place has to be migrated (#21) when it changes.
"""

from pathlib import Path

import pytest

from app import models
from app.models import Base

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# Every column a later task reads. Tables may gain columns; these may not go.
_EXPECTED_COLUMNS = {
    "resume": {
        "id",
        "filename",
        "content_type",
        "text",
        "stored_path",
        "uploaded_at",
        "is_active",
    },
    "company": {"id", "name", "slug", "is_active", "created_at"},
    "posting": {
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
    },
    "match": {
        "id",
        "posting_id",
        "score",
        "reason",
        "gaps",
        "state",
        "created_at",
        "notified_at",
    },
    "run": {
        "id",
        "status",
        "started_at",
        "finished_at",
        "error",
        "postings_seen",
        "matches_created",
    },
}


def test_the_five_tables_are_declared_and_no_others() -> None:
    assert set(Base.metadata.tables) == set(_EXPECTED_COLUMNS)


@pytest.mark.parametrize("table_name", sorted(_EXPECTED_COLUMNS))
def test_every_column_a_later_task_reads_is_present(table_name: str) -> None:
    declared = set(Base.metadata.tables[table_name].columns.keys())
    assert _EXPECTED_COLUMNS[table_name] <= declared


def test_no_other_module_declares_table_structure() -> None:
    declaring = sorted(
        path.relative_to(APP_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if "__tablename__" in path.read_text(encoding="utf-8")
    )
    assert declaring == ["models.py"]


def test_the_docstring_says_tables_only_are_created_and_names_21() -> None:
    docstring = models.__doc__ or ""
    assert "#21" in docstring
    assert "never alters" in docstring
