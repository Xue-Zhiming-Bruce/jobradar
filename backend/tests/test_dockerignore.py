"""The repository-root `.dockerignore` must keep caches and local data out of the build context.

`#13` builds the image from the repository root -- the frontend stage needs
`frontend/` and the runtime stage needs `backend/` -- so the file that governs
the context is `.dockerignore` at the root, and this test reads that one. Its
rules are the ones `backend/.dockerignore` held, moved, not deleted: the parts
of the repository the build actually reads have to survive it.

Unlike `.gitignore`, `.dockerignore` patterns are anchored at the context root:
`__pycache__/` excludes the root directory only, so after `uv run pytest` the
nested `app/__pycache__/*.pyc` was copied into the image. `ls -a` cannot see
that, because it lists the top level only.

The rules modelled here are Docker's (moby/patternmatcher): every pattern is
anchored at the context root, `**` stands for any number of directories
including none, `*` and `?` never cross a `/`, and a path is excluded when a
pattern matches the path or any of its parent directories. The model was pinned
against a real `docker build` of a context holding these same trees; the
recursive image check that does the same against the built image is documented
in `README.md`.
"""

import re
from pathlib import Path

import pytest

from app.db import DEFAULT_DATABASE_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# Junk at the root and below it: none of it may reach the image.
_EXCLUDED_PATHS = [
    ".venv/lib/python3.12/site-packages/fastapi/__init__.py",
    ".pytest_cache/v/cache/lastfailed",
    ".ruff_cache/0.1.0/data",
    ".mypy_cache/3.12/app.main.data.json",
    "__pycache__/main.cpython-312.pyc",
    "backend/app/__pycache__/__init__.cpython-312.pyc",
    "backend/app/__pycache__/main.cpython-312.pyc",
    "backend/app/__pycache__/export_openapi.cpython-312.pyc",
    "backend/tests/__pycache__/test_health.cpython-312.pyc",
    "backend/app/main.pyc",
    "backend/app/main.pyo",
    "backend/app/main.pyd",
    "backend/.python-version",
    "jobradar.db",
    "backend/data/jobradar.db",
    "backend/nested/data/jobradar.db",
    "jobradar.sqlite3",
    "backend/deep/nested/notes.sqlite3",
    "uploads/resume.pdf",
    "backend/uploads/resume.pdf",
    "backend/nested/uploads/resume.pdf",
    ".env",
    ".env.local",
    "backend/app/.env.local",
    ".DS_Store",
    "backend/app/.DS_Store",
    # The junk the wider context adds: git's history, the frontend's installed
    # dependencies -- `npm ci` runs in the build stage -- and its output, which
    # that stage produces. A committed or host-built `frontend/dist` must never
    # be what the image serves.
    ".git/config",
    ".git/objects/3f/ab12cd",
    "frontend/node_modules/react/index.js",
    "frontend/node_modules/.bin/vite",
    "frontend/dist/index.html",
    "frontend/dist/assets/index-B6OjL1iC.js",
]

# Everything the build reads must survive the ignore file: the backend's source
# and lock file, the migration path the runtime stage copies beside `app/`
# (`alembic.ini`, `migrations/` and the entrypoint script, #47), the frontend's
# source plus the two files `npm ci` installs from, and the files the build
# itself is described by.
_KEPT_PATHS = [
    "Dockerfile",
    "compose.yaml",
    "backend/Dockerfile",
    "backend/docker-entrypoint.sh",
    "backend/alembic.ini",
    "backend/migrations/env.py",
    "backend/migrations/script.py.mako",
    "backend/migrations/versions/0001_baseline_five_tables.py",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/openapi.json",
    "backend/app/__init__.py",
    "backend/app/main.py",
    "backend/app/export_openapi.py",
    "backend/app/matching/score_prompt.txt",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/index.html",
    "frontend/vite.config.ts",
    "frontend/tsconfig.json",
    "frontend/tsconfig.app.json",
    "frontend/tsconfig.node.json",
    "frontend/src/main.tsx",
    "frontend/src/App.tsx",
    "frontend/public/favicon.svg",
]

# The characters moby escapes before handing a pattern to the regexp engine.
_ESCAPED_CHARACTERS = set("\\$^+.()|{}")


def _dockerignore_patterns() -> list[str]:
    return [
        line
        for raw in _DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]


def _clean(pattern: str) -> str:
    cleaned = pattern.removeprefix("./")
    return cleaned.rstrip("/") or cleaned


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    cleaned = _clean(pattern)
    regex = "^"
    index = 0
    while index < len(cleaned):
        character = cleaned[index]
        if cleaned.startswith("**", index):
            index += 2
            if cleaned[index : index + 1] == "/":
                index += 1
            regex += "(.*/)?" if index < len(cleaned) else ".*"
        elif character == "*":
            index += 1
            regex += "[^/]*"
        elif character == "?":
            index += 1
            regex += "[^/]"
        elif character in _ESCAPED_CHARACTERS:
            index += 1
            regex += re.escape(character)
        else:
            index += 1
            regex += character
    return re.compile(regex + "$")


def _is_excluded(path: str, patterns: list[str]) -> bool:
    parts = path.split("/")
    candidates = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    excluded = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        body = pattern[1:] if negated else pattern
        if any(_pattern_regex(body).match(candidate) for candidate in candidates):
            excluded = not negated
    return excluded


def test_the_build_context_is_governed_by_a_dockerignore_file() -> None:
    assert _DOCKERIGNORE.is_file()


@pytest.mark.parametrize("path", _EXCLUDED_PATHS)
def test_caches_and_local_data_are_excluded_at_any_depth(path: str) -> None:
    assert _is_excluded(path, _dockerignore_patterns()), f"{path} would enter the image"


@pytest.mark.parametrize("path", _KEPT_PATHS)
def test_source_and_the_lock_file_survive_the_ignore_file(path: str) -> None:
    assert not _is_excluded(path, _dockerignore_patterns()), f"{path} would be missing"


def test_the_default_local_database_is_excluded() -> None:
    """The one database file the app creates itself must never enter a build context.

    The path is taken from `app.db` instead of being repeated as a literal, so
    moving the default cannot silently take the file out of the ignore file.
    """
    relative = DEFAULT_DATABASE_PATH.relative_to(REPO_ROOT).as_posix()

    assert _is_excluded(relative, _dockerignore_patterns()), f"{relative} would enter the image"


def test_no_exclusion_pattern_is_anchored_at_the_context_root_only() -> None:
    wrong = [pattern for pattern in _dockerignore_patterns() if not pattern.startswith("**/")]
    assert wrong == [], f"root-only patterns let nested paths through: {wrong}"
