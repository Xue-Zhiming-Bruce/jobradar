"""`.env.example` lists every variable the code reads, and this module keeps that true.

`_docs/process.md` says it: secrets live in `.env`, which is gitignored, and
`.env.example` lists the keys. `README.md`'s Configuration section is the table
the file documents -- every variable with its default and what an unset value
does -- and nothing loads the file, which is why no module under `backend/app/`
or `mcp/jobradar_mcp/` mentions it and no dependency was added for it.

The list is not maintained by hand. The scan below resolves every `*_VARIABLE`
constant in the three trees to its own string value -- so the MCP client's
`URL_VARIABLE = "JOBRADAR_URL"` is checked as `JOBRADAR_URL` and not as the
constant's name -- collects every name those trees read with `os.environ.get`,
`os.environ[...]` or `getenv`, and compares that set with the names in the file in
both directions: a variable added to the code with no line in the file, and a line
left behind for a variable the code no longer reads, are both a failing test
rather than a reviewer's catch.

The scan is `pathlib` and `ast` only: it reads the file and the three trees,
compares sets, needs no key, socket or database, and never reads an environment
variable of its own. The one test that shells out is the `git check-ignore` proof,
because the `.gitignore` exception is what lets the file into the repository at
all.
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

from app import db, mcp
from app.main import DEFAULT_STATIC_DIR, static_dir
from app.matching import prefilter, scorer
from app.routes import internal
from app.services import digest, pipeline, resume

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_FILE = REPO_ROOT / ".env.example"

# The three trees the scan reads, named so a failure says which list disagreed.
SCANNED_TREES = ("backend/app", "backend/tests", "mcp/jobradar_mcp")

_VARIABLE_SUFFIX = "_VARIABLE"
_ASSIGNMENT = re.compile(r"(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)")

# The credential shapes `backend/tests/test_committed_data_guard.py` names, spelled
# the same way: every value in the example file must be a placeholder, so none of
# them may appear. The last one is coarse on purpose -- the file holds no
# PostgreSQL URL at all, so a `user:password@` inside one is a credential whatever
# the password reads.
_CREDENTIAL_SHAPES = (
    re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bpostgres(?:ql)?(?:\+[a-z0-9]+)?://\S+:\S+@"),
)


def scanned_files() -> list[Path]:
    """Every Python module under the three trees, sorted so two runs read the same order."""
    return sorted(
        path for tree in SCANNED_TREES for path in (REPO_ROOT / tree).rglob("*.py")
    )


def scanned_modules() -> list[ast.Module]:
    """Each scanned file, parsed."""
    return [ast.parse(path.read_text(encoding="utf-8")) for path in scanned_files()]


def module_string_constants(module: ast.Module) -> dict[str, str]:
    """One parsed module's module-level `NAME = "..."` assignments, as name -> value.

    Resolving the value rather than trusting the name a read passes is what makes
    this checkable: `UPLOADS_DIR_VARIABLE = "UPLOADS_DIR"` and the MCP client's
    `URL_VARIABLE = "JOBRADAR_URL"` are checked as the strings they hold, and the
    digest's, the scorer's and the prefilter's constants are plain names --
    `DIGEST_MIN_SCORE = "DIGEST_MIN_SCORE"` -- with no suffix to go on at all. The
    mapping is per module because one constant's name can mean two things: the
    app's `TICK_SECRET_VARIABLE` holds `TICK_SECRET` and the MCP client's holds
    `JOBRADAR_TICK_SECRET`.
    """
    constants: dict[str, str] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


def _is_environment(node: ast.expr) -> bool:
    """Whether an expression is the environment mapping: `os.environ`, or a bare `environ`."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _read_name(node: ast.AST, constants: dict[str, str]) -> str | None:
    """The environment name one node reads, or `None` when the node reads none.

    The three spellings are `os.environ.get(...)`, `os.environ[...]` and
    `getenv(...)`, the last bare or as `os.getenv`; the argument is a string
    literal, or a name resolved through a `*_VARIABLE` constant. Anything else --
    `os.environ` passed around, a `setenv` -- is not a read of a named variable.
    """
    argument: ast.expr | None = None
    if isinstance(node, ast.Call):
        function = node.func
        named = (
            function.attr
            if isinstance(function, ast.Attribute)
            else getattr(function, "id", None)
        )
        reads_environment = named == "getenv" or (
            named == "get" and isinstance(function, ast.Attribute) and _is_environment(function.value)
        )
        if reads_environment and node.args:
            argument = node.args[0]
    elif isinstance(node, ast.Subscript) and _is_environment(node.value):
        argument = node.slice

    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    if isinstance(argument, ast.Name):
        return constants.get(argument.id)
    return None


def read_names() -> set[str]:
    """Every environment name the three trees read, in any of the three spellings."""
    names: set[str] = set()
    for module in scanned_modules():
        constants = module_string_constants(module)
        for node in ast.walk(module):
            name = _read_name(node, constants)
            if name is not None:
                names.add(name)
    return names


def example_lines() -> list[str]:
    """The example file's lines, as they are."""
    return EXAMPLE_FILE.read_text(encoding="utf-8").splitlines()


def listed_names() -> list[str]:
    """Every name the file assigns, in file order, duplicates included."""
    return [
        match["name"] for line in example_lines() if (match := _ASSIGNMENT.match(line))
    ]


def _comment_above(lines: list[str], index: int) -> str:
    """The contiguous block of comments directly above `lines[index]`, joined."""
    comment: list[str] = []
    for above in reversed(lines[:index]):
        if not above.startswith("#"):
            break
        comment.append(above.lstrip("# "))
    return " ".join(reversed(comment))


def test_the_file_lists_every_name_the_three_trees_read_and_no_other() -> None:
    """Both directions: a variable the code reads but the file omits, and a stale line, fail."""
    in_the_file = set(listed_names())
    in_the_code = read_names()

    assert in_the_code - in_the_file == set(), "read by the code, missing from .env.example"
    assert in_the_file - in_the_code == set(), "listed in .env.example, read nowhere"


def test_the_scan_resolves_a_constant_to_its_value_and_never_to_its_name() -> None:
    """A `*_VARIABLE` constant is a name for a string, and the string is what is read.

    This is what makes the check above honest: the read passes a constant, and the
    name the file has to list is the constant's value -- `UPLOADS_DIR`, not
    `UPLOADS_DIR_VARIABLE`.
    """
    declared: list[tuple[str, str]] = [
        (constant, value)
        for module in scanned_modules()
        for constant, value in module_string_constants(module).items()
        if constant.endswith(_VARIABLE_SUFFIX)
    ]
    names = read_names()

    assert declared, "the trees name their variables with *_VARIABLE constants"
    assert {value for _, value in declared} <= names
    assert names.isdisjoint({constant for constant, _ in declared})


def test_every_name_is_listed_exactly_once() -> None:
    names = listed_names()

    assert names
    assert [name for name in set(names) if names.count(name) > 1] == []


def test_every_line_is_a_key_value_line_or_a_comment() -> None:
    assert [
        line
        for line in example_lines()
        if line.strip() and not line.startswith("#") and not _ASSIGNMENT.match(line)
    ] == []


def _lines_without_a_required_or_optional_comment() -> list[str]:
    """Each line whose comment does not say whether the code works without the variable."""
    lines = example_lines()
    silent: list[str] = []
    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        comment = _comment_above(lines, index).lower()
        if "optional" not in comment and "required" not in comment:
            silent.append(match["name"])
    return silent


def test_every_line_says_whether_the_code_works_without_it() -> None:
    assert _lines_without_a_required_or_optional_comment() == []


def test_no_line_holds_a_credential_shape() -> None:
    """Every value is a placeholder: a default, an empty value, or an example path."""
    assert not any(
        pattern.search(line) for line in example_lines() for pattern in _CREDENTIAL_SHAPES
    ), "a line in .env.example has the shape of a key, a token or a database credential"


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    """One read-only git command against the repository root, the way the guard runs one."""
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *arguments], capture_output=True, text=True
    )


def test_the_real_env_file_is_ignored_and_the_example_is_not() -> None:
    """`.gitignore`'s exception, proven rather than assumed."""
    assert _git("check-ignore", "--quiet", ".env").returncode == 0
    assert _git("check-ignore", "--quiet", ".env.example").returncode != 0


def test_the_example_is_committed() -> None:
    assert _git("ls-files", "--", ".env.example").stdout.strip() == ".env.example"


def test_the_app_keeps_its_own_defaults_with_every_documented_name_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing loads the file: with every name it lists unset, the app's own defaults stand."""
    for name in read_names():
        monkeypatch.delenv(name, raising=False)

    assert EXAMPLE_FILE.is_file()
    assert db.database_url() == f"sqlite:///{db.DEFAULT_DATABASE_PATH}"
    assert static_dir() == DEFAULT_STATIC_DIR
    assert resume.uploads_dir() == resume.DEFAULT_UPLOADS_DIR
    assert resume.max_upload_bytes() == resume.DEFAULT_MAX_UPLOAD_BYTES
    assert internal.tick_secret() == ""
    assert mcp.auth_token() == ""
    assert prefilter.title_terms() == prefilter.DEFAULT_TERMS
    assert prefilter.seniority_terms() == prefilter.DEFAULT_TERMS
    assert prefilter.location_terms() == prefilter.DEFAULT_TERMS
    assert prefilter.must_have_keywords() == prefilter.DEFAULT_TERMS
    assert scorer.api_key() == ""
    assert scorer.model() == scorer.DEFAULT_MODEL
    assert scorer.fallback_model() is None
    assert scorer.max_postings() == scorer.DEFAULT_MAX_POSTINGS
    assert scorer.base_url() == scorer.DEFAULT_BASE_URL
    assert digest.min_score() == digest.DEFAULT_MIN_SCORE
    assert digest.smtp_port() == 587  # the declared default for the port, which the file states
    assert digest.smtp_host() == ""
    assert digest.smtp_username() == ""
    assert digest.smtp_password() == ""
    assert digest.digest_from() == ""
    assert digest.digest_to() == ""
    assert pipeline.request_budget() == pipeline.DEFAULT_REQUEST_BUDGET
    assert pipeline.request_interval_seconds() == pipeline.DEFAULT_REQUEST_INTERVAL_SECONDS
