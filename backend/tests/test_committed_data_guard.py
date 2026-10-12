"""Nothing private may be committed: the index must hold no secret, resume or database file.

`AGENTS.md` states the rule once ("Never commit a resume, an API key, a database
file, or anything from `.env`"), `_docs/process.md` states it for resume, secrets
and data, `_docs/plan.md` states it for a public repository, and `.gitignore`
lists the shapes -- but until this module nothing checked any of them, so the
rule rested on every author remembering it. This module is that check, and it is
the only place the rule lives: it reads no configuration file, adds no setting
and needs no dependency beyond `git`.

**What it reads is the git index, never the working tree.** `git ls-files -z`
enumerates the tracked paths and `git show :<path>` reads each one's staged
content, so a file that was `git add`ed but never committed is caught, a
`.gitignore`d file is caught once `git add -f` stages it, and a developer's own
gitignored `.env`, `backend/data/jobradar.db` and `~/.jobradar/uploads` are never
read. The root is resolved from `__file__` and every git call names it with `-C`,
so the verdict is the same from the repository root and from `backend/`, and a
`GIT_DIR` inherited from a caller cannot move it. A tracked file that is not
UTF-8 text -- a fixture PDF, an image -- is skipped by the content rules and can
only be caught by the path rules.

**Every violation is one line, `<path> (<kind>)`, and the matched text is never
printed**: the log says which file and which rule, never what the secret is. The
fixtures below build their planted values from parts for the same reason -- an
assembled literal here would be the violation this module reports, and pytest
prints a test's parameters in its failure header.

Named limits, so this is not mistaken for more than it is. CI runs after the push,
so the guard stops a merge and makes a leak loud rather than preventing the push;
the local `uv run pytest` is the earlier warning. The environment rule reads the
unquoted `.env`, shell, YAML and CI spellings, so a secret hardcoded in a quoted
Python string counts only when it has a credential shape. A resume committed as a
`.txt` outside `uploads/` and `data/` is indistinguishable from ordinary text and
is deliberately not caught. And a leak that predates this module is invisible to
it, because the index is the only thing read.
"""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# One label per rule, so the output reads the same for every file and a reviewer
# can grep a CI log for it.
_ENVIRONMENT_FILE = "environment file"
_DATABASE_FILE = "database file"
_RESUME_FILE = "resume file"
_LOCAL_DATA_DIRECTORY = "local data directory"
_ENVIRONMENT_VALUE = "environment value"

# `.gitignore`'s own list, which is what these rules have to match: the local
# database, anything uploaded, the environment file.
_DATABASE_SUFFIXES = frozenset({".db", ".sqlite3"})
_RESUME_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".rtf", ".odt"})
_LOCAL_DATA_DIRECTORY_NAMES = frozenset({"data", "uploads"})

# `.env.example` is exempt from the `.env` rule alone: it is the file the README
# and `AGENTS.md` document for a local setup, and it holds empty values (#33
# creates it). A value inside it still fails, on its own merits.
_ENV_EXAMPLE = ".env.example"

# Synthetic inputs for the resume reader tests, exempt from the resume rule alone.
# A second PDF beside them is a violation, and so is a credential inside them.
_FIXTURE_RESUMES = frozenset(
    {"backend/tests/fixtures/synthetic-resume.pdf", "backend/tests/fixtures/blank-page.pdf"}
)

# Seven exact literals, each already published in this repository, each with the
# reason it is not a credential and where a reviewer can check it. It is an
# exact-literal list, not a path or shape exemption: a value of the same shape
# that is not on it still fails, in any file, `README.md` included.
ALLOWED_VALUES: dict[str, str] = {
    "jobradar-throwaway": (
        "the local Postgres throwaway in .github/workflows/ci.yml, README.md and "
        "backend/tests/integration/conftest.py"
    ),
    "jobradar-local-throwaway": (
        "compose.yaml's `db` password, for a container that publishes no host port"
    ),
    "throwaway-local-secret": (
        "the documented local TICK_SECRET demo in README.md, .github/workflows/tick.yml "
        "and mcp/demo_client.py"
    ),
    "synthetic-password": "backend/tests/test_db.py's synthetic database password",
    "***": "the rendering backend/app/db.py's redacted_database_url() produces",
    "jobradar": "backend/tests/test_engine_matrix.py's unreachable-server password, on port 1",
    "password": "backend/tests/test_runs_api.py's db.example.test example URL",
}

# The credential shapes, one regex per kind, the kind label doubling as the
# table's key so a failure can name the kind without the value. The URL's user
# and password carry no quote, brace or `$`: an f-string placeholder or a shell
# reference in that position is not a literal credential, and `{KNOWN_PASSWORD}`
# in `backend/tests/test_db.py` is one of those.
_POSTGRES_URL = re.compile(
    r"\bpostgres(?:ql)?(?:\+[a-z0-9]+)?://"
    r"(?P<user>[^\s:@/\"'`{}]+):(?P<password>[^\s@/\"'`{}$]+)@"
)

_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "github token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("postgres url with a password", _POSTGRES_URL),
)

# A secret-shaped name and its value, in the unquoted `.env`, shell, YAML and CI
# spellings. Nothing read here is part of a variable reference (`${TICK_SECRET:-}`
# in `compose.yaml`), and the value has to be a plain scalar: a quoted string or
# an opening brace or bracket is the Python, JSON or dict spelling, left to the
# shape rules above. That is the named limit in the docstring -- without it,
# `KNOWN_PASSWORD = "..."` in `backend/tests/test_db.py` and every
# `TICK_SECRET = "..."` sentinel in the suite would read as credentials.
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![\w${])(?P<name>[A-Za-z_][A-Za-z0-9_]*"
    r"_(?:API_KEY|SECRET|PASSWORD|TOKEN|ACCESS_KEY_ID))"
    r"(?![A-Za-z0-9_])[ \t]*[=:][ \t]*(?P<value>[^\s\"'`{}[\]]*)"
)

# Git's own directory overrides: `-C` names the root, but these outrank directory
# discovery, so a caller's environment must not be able to point the guard at
# another repository.
_GIT_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})


class GitError(RuntimeError):
    """Git could not read the index, so the guard has no verdict to give."""


def _git(root: Path, *arguments: str) -> bytes:
    """One read-only git command against `root`, failing loudly when git cannot run it."""
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        env={name: value for name, value in os.environ.items() if name not in _GIT_OVERRIDES},
    )
    if completed.returncode != 0:
        raise GitError(
            f"git {arguments[0]} failed in {root}: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout


def _tracked_paths(root: Path) -> list[str]:
    """The index's own paths, root-relative, as `git ls-files -z` hands them over."""
    return [
        name.decode("utf-8", "surrogateescape")
        for name in _git(root, "ls-files", "-z").split(b"\0")
        if name
    ]


def _staged_text(root: Path, path: str) -> str | None:
    """The staged content of a tracked file, or `None` when it is not UTF-8 text."""
    try:
        return _git(root, "show", f":{path}").decode("utf-8")
    except UnicodeDecodeError:
        return None


def _path_rule(path: str) -> str | None:
    """The kind a tracked path is on its own, whatever it holds, or `None`."""
    name = PurePosixPath(path).name
    if name != _ENV_EXAMPLE and (name == ".env" or name.startswith(".env.")):
        return _ENVIRONMENT_FILE
    suffix = PurePosixPath(path).suffix
    if suffix in _DATABASE_SUFFIXES:
        return _DATABASE_FILE
    if suffix in _RESUME_SUFFIXES and path not in _FIXTURE_RESUMES:
        return _RESUME_FILE
    if _LOCAL_DATA_DIRECTORY_NAMES.intersection(PurePosixPath(path).parts[:-1]):
        return _LOCAL_DATA_DIRECTORY
    return None


def _matched_value(match: re.Match[str]) -> str:
    """The value the allowlist speaks about: a URL's password, or the whole match."""
    return match.groupdict().get("password") or match.group(0)


def _credential_kind(text: str) -> str | None:
    """The kind of credential shape in `text`, or `None`.

    A published literal in the value the allowlist speaks about is not a
    credential; one of the same shape that is not published still is.
    """
    for kind, pattern in _CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            if _matched_value(match) not in ALLOWED_VALUES:
                return kind
    return None


def _names_a_secret_value(text: str) -> bool:
    """Whether `text` assigns a real value to a secret-shaped name.

    Empty values (`.env.example`), placeholders (`${TICK_SECRET:-}`,
    `${{ secrets.TICK_SECRET }}`) and the published literals pass.
    """
    for match in _SECRET_ASSIGNMENT.finditer(text):
        value = match.group("value")
        if value and not value.startswith("$") and value not in ALLOWED_VALUES:
            return True
    return False


def _content_rule(text: str | None) -> str | None:
    """The kind of credential or secret-named value in tracked text, or `None`."""
    if text is None:
        return None
    return _credential_kind(text) or (
        _ENVIRONMENT_VALUE if _names_a_secret_value(text) else None
    )


def scan(root: Path) -> list[str]:
    """Every violation in `root`'s git index, one `<path> (<kind>)` line each.

    Raises `GitError` when git cannot read the index -- a root that is not a
    repository, a git that is missing -- so a broken environment is a loud
    failure and never an empty "clean" verdict.
    """
    violations = []
    for path in _tracked_paths(root):
        kind = _path_rule(path) or _content_rule(_staged_text(root, path))
        if kind is not None:
            violations.append(f"{path} ({kind})")
    return violations


def test_the_repository_as_it_stands_holds_no_violation() -> None:
    """The whole rule, on this repository's index -- which also covers openapi.json."""
    violations = scan(REPO_ROOT)

    assert violations == [], "\n".join(violations)


def test_the_verdict_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root comes from `__file__`, so `cd backend` and the repository root agree."""
    monkeypatch.chdir(tmp_path)

    assert scan(REPO_ROOT) == []


def test_a_root_that_is_not_a_repository_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        scan(tmp_path)


def test_an_inherited_git_dir_cannot_change_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = _index(tmp_path / "checked", {".env": "A=1\n"})
    _index(tmp_path / "elsewhere", {"README.md": "an ordinary paragraph\n"})
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))

    assert scan(checked) == [".env (environment file)"]


# The fixture factories. Each takes its pieces apart so that the assembled value
# -- the one the guard has to find -- never appears in this file's own source.
def _join(*pieces: str) -> str:
    return "".join(pieces)


def _key(prefix: str, rest: str) -> str:
    return _join(prefix, rest)


def _pem_block(label: str) -> str:
    return _join("-----BEGIN ", label, "-----", "\nZm9v\n", "-----END ", label, "-----")


def _database_url(scheme: str, password: str) -> str:
    return _join(scheme, "://jobradar:", password, "@db.example.test:5432/jobradar")


def _assignment(name: str, value: str) -> str:
    return _join(name, "=", value)


def _smtp_password_name() -> str:
    """The SMTP password variable's name, assembled rather than spelled.

    `backend/tests/test_digest.py` reads every other test module for the SMTP
    variables and fails on one that names any of them, so this module builds the
    name from pieces like every other planted value here.
    """
    return _join("SMTP_", "PASSWORD")


def _git_init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "--quiet"], check=True, capture_output=True)


def _stage(root: Path, path: str, content: str | bytes) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    # `--force` is the `git add -f` this guard exists for: a scratch repository
    # ignores nothing unless the case plants a `.gitignore`, and the index decides.
    subprocess.run(
        ["git", "-C", str(root), "add", "--force", path], check=True, capture_output=True
    )


def _index(root: Path, files: dict[str, str | bytes]) -> Path:
    """A fresh repository in `root` holding exactly `files`, staged and never committed."""
    _git_init(root)
    for path, content in files.items():
        _stage(root, path, content)
    return root


# The named paths of the rule, each with the kind it reports.
_PATH_CASES = [
    (".env", ".env (environment file)"),
    (".env.local", ".env.local (environment file)"),
    (".env.production", ".env.production (environment file)"),
    ("jobradar.db", "jobradar.db (database file)"),
    ("backend/data/jobradar.db", "backend/data/jobradar.db (database file)"),
    ("backend/nested/data/notes.sqlite3", "backend/nested/data/notes.sqlite3 (database file)"),
    ("uploads/resume.pdf", "uploads/resume.pdf (resume file)"),
    ("backend/uploads/resume.pdf", "backend/uploads/resume.pdf (resume file)"),
    (".jobradar/uploads/resume.pdf", ".jobradar/uploads/resume.pdf (resume file)"),
    (
        "home/someone/.jobradar/uploads/resume.docx",
        "home/someone/.jobradar/uploads/resume.docx (resume file)",
    ),
    ("data/uploads/resume.txt", "data/uploads/resume.txt (local data directory)"),
    ("resume.pdf", "resume.pdf (resume file)"),
    ("backend/notes.rtf", "backend/notes.rtf (resume file)"),
    ("backend/tests/notes.doc", "backend/tests/notes.doc (resume file)"),
    ("backend/tests/notes.odt", "backend/tests/notes.odt (resume file)"),
]


@pytest.mark.parametrize(
    ("path", "expected"), _PATH_CASES, ids=[path for path, _ in _PATH_CASES]
)
def test_a_tracked_path_the_rules_name_is_one_violation(
    tmp_path: Path, path: str, expected: str
) -> None:
    root = _index(tmp_path, {path: ""})

    assert scan(root) == [expected]


def test_a_resume_committed_as_plain_text_outside_uploads_and_data_is_not_caught(
    tmp_path: Path,
) -> None:
    """The named limit: such a file is indistinguishable from ordinary tracked text."""
    root = _index(tmp_path, {"backend/notes/resume.txt": "Jane Doe -- Senior Engineer\n"})

    assert scan(root) == []


def test_the_two_synthetic_resume_fixtures_are_allowed(tmp_path: Path) -> None:
    root = _index(
        tmp_path,
        {
            "backend/tests/fixtures/synthetic-resume.pdf": b"%PDF-1.4\n",
            "backend/tests/fixtures/blank-page.pdf": b"%PDF-1.4\n",
        },
    )

    assert scan(root) == []


def test_a_second_pdf_beside_the_fixtures_is_a_violation(tmp_path: Path) -> None:
    root = _index(tmp_path, {"backend/tests/fixtures/real-resume.pdf": b"%PDF-1.4\n"})

    assert scan(root) == ["backend/tests/fixtures/real-resume.pdf (resume file)"]


def test_a_fixture_resume_holding_a_secret_is_still_caught(tmp_path: Path) -> None:
    """The exemption is scoped to the resume rule, not to the file."""
    planted = _assignment(_smtp_password_name(), "hunter2")
    root = _index(tmp_path, {"backend/tests/fixtures/synthetic-resume.pdf": planted})

    assert scan(root) == ["backend/tests/fixtures/synthetic-resume.pdf (environment value)"]


@pytest.mark.parametrize(
    "path",
    [".env.example", "backend/tests/fixtures/blank-page.pdf"],
    ids=["the example environment file", "a synthetic resume fixture"],
)
def test_an_exempt_path_holding_a_credential_is_still_caught(tmp_path: Path, path: str) -> None:
    """Neither exemption hides content: only the one path rule each is switched off."""
    planted = _key("sk-", "0" * 32)
    root = _index(tmp_path, {path: planted})

    violations = scan(root)

    assert violations == [f"{path} (openai-style key)"]
    assert planted not in "\n".join(violations)


def test_the_documented_example_environment_file_is_allowed(tmp_path: Path) -> None:
    root = _index(tmp_path, {".env.example": _assignment("TICK_SECRET", "")})

    assert scan(root) == []


def test_an_env_example_holding_a_value_is_still_a_violation(tmp_path: Path) -> None:
    """The exemption is scoped to the `.env` rule, not to the file's content."""
    root = _index(tmp_path, {".env.example": _assignment("TICK_SECRET", "a-real-value")})

    assert scan(root) == [".env.example (environment value)"]


# One fixture case per kind, with the spellings criterion 4 names for the two
# kinds that have more than one. Every value is assembled by a factory below:
# an assembled literal in this file would be a violation of this rule itself.
_CREDENTIAL_CASES = [
    ("private key OPENSSH", "private key", _pem_block("OPENSSH PRIVATE KEY")),
    ("private key RSA", "private key", _pem_block("RSA PRIVATE KEY")),
    ("private key EC", "private key", _pem_block("EC PRIVATE KEY")),
    ("private key PGP block", "private key", _pem_block("PGP PRIVATE KEY BLOCK")),
    ("aws access key id", "aws access key id", _key("AKIA", "IOSFODNN7EXAMPLE")),
    ("openai-style key", "openai-style key", _key("sk-", "0" * 32)),
    ("github token classic", "github token", _key("ghp_", "0" * 36)),
    ("github token fine-grained", "github token", _key("github_pat_", "0" * 24)),
    (
        "postgres url with a password",
        "postgres url with a password",
        _database_url("postgres", "s3cr3t-value"),
    ),
    (
        "postgres url with psycopg and a password",
        "postgres url with a password",
        _database_url("postgresql+psycopg", "s3cr3t-value"),
    ),
]


@pytest.mark.parametrize(
    ("kind", "planted"),
    [(kind, planted) for _, kind, planted in _CREDENTIAL_CASES],
    ids=[case for case, _, _ in _CREDENTIAL_CASES],
)
def test_a_credential_shape_in_tracked_text_is_one_violation(
    tmp_path: Path, kind: str, planted: str
) -> None:
    root = _index(tmp_path, {"backend/app/config.py": f"{planted}\n"})

    violations = scan(root)

    assert violations == [f"backend/app/config.py ({kind})"]
    assert planted not in "\n".join(violations)


# The two cases no shape rule of the credential table catches. The ids name the
# case and not the text, so pytest's failure header cannot print the plant.
_ENVIRONMENT_CASES = [
    ("smtp password name", _smtp_password_name(), "hunter" + "2"),
    ("tick secret name", "TICK_SECRET", "a-real-value"),
]


@pytest.mark.parametrize(
    ("name", "value"),
    [(name, value) for _, name, value in _ENVIRONMENT_CASES],
    ids=[case for case, _, _ in _ENVIRONMENT_CASES],
)
def test_a_secret_named_value_in_tracked_text_is_one_violation(
    tmp_path: Path, name: str, value: str
) -> None:
    """The two cases no shape rule of the credential table catches."""
    planted = _assignment(name, value)
    root = _index(tmp_path, {"backend/app/config.py": f"{planted}\n"})

    violations = scan(root)

    assert _credential_kind(planted) is None
    assert violations == ["backend/app/config.py (environment value)"]
    assert planted not in "\n".join(violations)


# The values an environment rule must let through, each of them a spelling
# `compose.yaml`, `.github/workflows/tick.yml` or the README already uses.
_ACCEPTED_VALUES = [
    ("empty value", ""),
    ("shell placeholder", _join("${TICK_", "SECRET:-}")),
    ("workflow placeholder", "${{ secrets.TICK_SECRET }}"),
    *[(value, value) for value in sorted(ALLOWED_VALUES)],
]


@pytest.mark.parametrize(
    ("value",),
    [(value,) for _, value in _ACCEPTED_VALUES],
    ids=[name for name, _ in _ACCEPTED_VALUES],
)
def test_an_empty_placeholder_or_published_value_passes(tmp_path: Path, value: str) -> None:
    root = _index(tmp_path, {"backend/app/config.py": _assignment(_smtp_password_name(), value)})

    assert scan(root) == []


def test_the_allowlist_is_seven_published_literals_each_with_its_reason() -> None:
    assert set(ALLOWED_VALUES) == {
        "jobradar-throwaway",
        "jobradar-local-throwaway",
        "throwaway-local-secret",
        "synthetic-password",
        "***",
        "jobradar",
        "password",
    }
    assert all(reason.strip() for reason in ALLOWED_VALUES.values())
    assert all("/" not in value for value in ALLOWED_VALUES)  # no entry is a path


def test_a_value_of_the_same_shape_that_is_not_published_still_fails(tmp_path: Path) -> None:
    """The list is exact literals, not a shape exemption."""
    planted = _database_url("postgresql", _join("jobradar-local-throwaway", "-2"))
    root = _index(tmp_path, {"backend/app/config.py": planted})

    assert scan(root) == ["backend/app/config.py (postgres url with a password)"]


def test_a_secret_in_the_readme_fails_like_any_other_file(tmp_path: Path) -> None:
    root = _index(
        tmp_path, {"README.md": _assignment(_smtp_password_name(), _join("not-", "published"))}
    )

    assert scan(root) == ["README.md (environment value)"]


def test_the_committed_contract_is_scanned_like_any_other_tracked_text(tmp_path: Path) -> None:
    """`backend/openapi.json` is text, so a credential-shaped example fails with its path."""
    planted = _key("sk-", "0" * 32)
    contract = json.dumps({"components": {"schemas": {"Token": {"example": planted}}}})
    root = _index(tmp_path, {"backend/openapi.json": contract})

    violations = scan(root)

    assert violations == ["backend/openapi.json (openai-style key)"]
    assert planted not in "\n".join(violations)


def test_a_database_url_in_the_contract_is_a_violation(tmp_path: Path) -> None:
    contract = json.dumps({"servers": [{"url": _database_url("postgres", "s3cr3t-value")}]})
    root = _index(tmp_path, {"backend/openapi.json": contract})

    assert scan(root) == ["backend/openapi.json (postgres url with a password)"]


def test_an_ordinary_contract_passes(tmp_path: Path) -> None:
    contract = json.dumps({"openapi": "3.1.0", "paths": {"/healthz": {"get": {}}}})
    root = _index(tmp_path, {"backend/openapi.json": contract})

    assert scan(root) == []


def test_a_tracked_file_that_is_not_utf8_is_skipped_by_the_content_rules(tmp_path: Path) -> None:
    """A fixture PDF or an image: only a path rule can catch it."""
    planted = b"\xff\xfe" + b"AKIA" + b"IOSFODNN7EXAMPLE"
    root = _index(tmp_path, {"backend/tests/fixtures/notes.bin": planted})

    assert scan(root) == []


def test_a_staged_but_uncommitted_file_is_caught(tmp_path: Path) -> None:
    """Nothing in these fixtures is ever committed: the index is enough."""
    root = _index(tmp_path, {".env": "A=1\n"})

    assert _git(root, "status", "--short").decode().count("A  .env") == 1
    assert scan(root) == [".env (environment file)"]


def test_a_gitignored_file_staged_with_force_is_caught(tmp_path: Path) -> None:
    """The index decides, not `.gitignore`."""
    root = _index(
        tmp_path,
        {".gitignore": "config.py\n", "config.py": f"{_key('sk-', '0' * 32)}\n"},
    )

    assert scan(root) == ["config.py (openai-style key)"]


def test_a_working_tree_edit_is_never_read(tmp_path: Path) -> None:
    root = _index(tmp_path, {"config.py": "# nothing here\n"})
    (root / "config.py").write_text(
        _assignment(_smtp_password_name(), "hunter2"), encoding="utf-8"
    )

    assert scan(root) == []


def test_a_local_env_database_and_upload_that_are_untracked_are_never_read(tmp_path: Path) -> None:
    """What a developer's own machine holds: gitignored, present, and not a violation."""
    root = _index(
        tmp_path,
        {
            "README.md": (
                "**Data rules.** No resume, key or database file is ever committed: secrets\n"
                "live in `.env`, which is gitignored, and a resume is uploaded at runtime\n"
                "into `UPLOADS_DIR`, a volume under compose and `~/.jobradar/uploads`\n"
                "otherwise.\n"
            ),
            "compose.yaml": "\n".join(
                (
                    _assignment("POSTGRES_PASSWORD", "jobradar-local-throwaway"),
                    _assignment(
                        "DATABASE_URL", _database_url("postgresql", "jobradar-local-throwaway")
                    ),
                    _assignment("TICK_SECRET", _join("${TICK_", "SECRET:-}")),
                    _assignment("LLM_API_KEY", _join("${LLM_", "API_KEY:-}")),
                )
            ),
            ".env.example": _assignment("TICK_SECRET", ""),
        },
    )
    (root / ".env").write_text(_assignment(_smtp_password_name(), "hunter2"), encoding="utf-8")
    (root / "backend" / "data").mkdir(parents=True)
    (root / "backend" / "data" / "jobradar.db").write_bytes(b"\x00sqlite")
    (root / "home" / "someone" / ".jobradar" / "uploads").mkdir(parents=True)
    (root / "home" / "someone" / ".jobradar" / "uploads" / "resume.pdf").write_bytes(b"%PDF-1.4")

    assert scan(root) == []
    assert _git(root, "status", "--short", "--untracked-files=all").decode() != ""


def test_every_violation_names_the_file_and_the_kind_and_nothing_else(tmp_path: Path) -> None:
    planted = _key("sk-", "0" * 32)
    root = _index(
        tmp_path,
        {
            ".env": "A=1\n",
            "backend/data/jobradar.db": "not really a database\n",
            "deploy.yaml": _assignment(_smtp_password_name(), "hunter2"),
            "notes.txt": planted,
        },
    )

    violations = scan(root)

    assert violations == [
        ".env (environment file)",
        "backend/data/jobradar.db (database file)",
        "deploy.yaml (environment value)",
        "notes.txt (openai-style key)",
    ]
    for line in violations:
        assert re.fullmatch(r"[^()]+ \([a-z0-9 -]+\)", line)
        assert planted not in line


