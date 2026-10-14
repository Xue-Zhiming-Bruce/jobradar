"""The pre-commit hook refuses a commit that stages private data.

`agent-hooks/pre-commit` calls the repository's committed-data guard
(`backend/tests/test_committed_data_guard.py`) and nothing else, so this module
runs the hook where git would call it from -- the working tree root -- with a
private file planted in the index, and checks the two things a guardrail owes: a
non-zero exit and a message naming what it found (`<path> (<kind>)`) without
printing a byte of the file.

The guard resolves its own root from `__file__` and strips `GIT_INDEX_FILE`,
`GIT_DIR` and `GIT_WORK_TREE` (its `_GIT_OVERRIDES`, so a caller cannot point it
at another repository), so it reads the index of the repository it lives in and a
merely redirected scratch index is invisible to it. The scratch here is that
repository: a `git init` holding the hook and a copy of the guard module, so the
plant sits in the index of the root the guard resolves for itself. The hook's
environment carries no `GIT_INDEX_FILE`: the guard ignores one, and its own test
helpers stage with whatever index the environment names, so a redirected one
breaks the very suite the hook runs.

The hook's check is `uv run pytest ...`, which would sync a second virtualenv in
the scratch repository. The `uv` this module puts on `PATH` runs this module's
own interpreter and the pytest already installed, so the run stays offline and
short; the hook's own `uv` resolution is what the missing-`uv` case below checks.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_ROOT / "agent-hooks" / "pre-commit"
GUARD = REPO_ROOT / "backend" / "tests" / "test_committed_data_guard.py"

# The contents the guard must report by path alone -- assembled from parts so no
# planted literal sits in this module's own source.
_PLANTED_CONTENT = "".join(("planted-", "not-a-real-", "secret"))


def _scratch_repository(tmp_path: Path) -> Path:
    """A git repository holding the hook and a copy of the guard, and nothing else."""
    root = tmp_path / "scratch"
    (root / "backend" / "tests").mkdir(parents=True)
    (root / "agent-hooks").mkdir()
    shutil.copy(GUARD, root / "backend" / "tests" / GUARD.name)
    shutil.copy(HOOK, root / "agent-hooks" / HOOK.name)
    subprocess.run(["git", "-C", str(root), "init", "--quiet"], check=True)
    return root


def _stage(root: Path, path: str, content: str) -> None:
    """Stage `content` at `path`, forced the way `git add -f` beats `.gitignore`."""
    file = root / path
    file.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--force", path], check=True, capture_output=True)


def _hook_environment(tmp_path: Path) -> dict[str, str]:
    """The hook's environment: a `uv` that runs this module's interpreter, so no sync."""
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "uv"
    script.write_text(
        "#!/bin/sh\n"
        "# The hook's `uv run pytest <args>` as this module's own interpreter.\n"
        "shift\n"
        "shift\n"
        f'exec "{sys.executable}" -m pytest "$@"\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join((str(shim), environment.get("PATH", "")))
    return environment


def _run_hook(root: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The hook, run from the working tree root the way git runs it."""
    return subprocess.run(
        [str(root / "agent-hooks" / "pre-commit")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )


def test_the_hook_allows_a_clean_index(tmp_path: Path) -> None:
    """The refusal below is the plant's doing and not the scratch setup failing."""
    root = _scratch_repository(tmp_path)

    completed = _run_hook(root, _hook_environment(tmp_path))

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_hook_refuses_a_staged_private_file(tmp_path: Path) -> None:
    root = _scratch_repository(tmp_path)
    _stage(root, ".env", _PLANTED_CONTENT + "\n")

    completed = _run_hook(root, _hook_environment(tmp_path))
    output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert ".env (environment file)" in output
    assert _PLANTED_CONTENT not in output


def test_the_hook_refuses_when_uv_is_absent(tmp_path: Path) -> None:
    """A guardrail that passes when its tool is missing is not one."""
    root = _scratch_repository(tmp_path)
    _stage(root, ".env", _PLANTED_CONTENT + "\n")
    environment = _hook_environment(tmp_path)
    environment["PATH"] = str(tmp_path / "no-tools-here")

    completed = _run_hook(root, environment)
    output = (completed.stdout + completed.stderr).strip()

    assert completed.returncode != 0
    assert output.count("\n") == 0  # one line
    assert "uv" in output
    assert "docs.astral.sh/uv" in output
