"""Importing and running the app must have no side effects on disk.

`uv run uvicorn app.main:app` takes no arguments and no environment, so the
import is exercised in a subprocess with an empty environment, in a scratch
directory: a required variable or a stray file shows up as a failure here.

The app does create one file once it runs -- the local database at
`data/jobradar.db` -- so that directory is expected and gitignored; anything
else is a leak. Adding a database file to the source tree is #21's problem.
"""

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_DIRECTORY = BACKEND_ROOT / "data"

# Virtualenvs and tool caches are not part of the app's own footprint.
_CACHES = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}

IMPORT_SCRIPT = f"import sys; sys.path.insert(0, {str(BACKEND_ROOT)!r}); import app.main"


def _application_files() -> set[Path]:
    """Files under backend/, excluding virtualenvs and tool caches."""
    return {
        path
        for path in BACKEND_ROOT.rglob("*")
        if path.is_file()
        and not _CACHES.intersection(path.relative_to(BACKEND_ROOT).parts)
    }


def test_importing_the_app_needs_no_environment_and_creates_no_file(tmp_path: Path) -> None:
    files_before = _application_files()

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", IMPORT_SCRIPT],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.iterdir()) == []
    assert _application_files() == files_before


def test_no_database_upload_or_data_file_exists() -> None:
    assert not (BACKEND_ROOT / "uploads").exists()
    # The local database belongs in the gitignored data/ directory and nowhere
    # else: the app writes it there on start, so only stray copies fail here.
    stray = [
        path
        for path in _application_files()
        if path.suffix in {".db", ".sqlite", ".sqlite3"}
        and DEFAULT_DATABASE_DIRECTORY not in path.parents
    ]
    assert stray == []
