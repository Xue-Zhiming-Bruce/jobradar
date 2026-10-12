"""The integration suite stays out of the default run and cannot stop being run quietly.

`backend/tests/integration/` is collected by the default `cd backend && uv run
pytest` and deselected by the `integration` marker, so nothing there runs unless
`-m integration` asks for it. That is one typo away from a suite nobody ever
runs, which is what this module is for: each way it could rot silently is a
failing test here.

- the directory holds no test module any more;
- the marker is no longer declared in `backend/pyproject.toml`'s
  `[tool.pytest.ini_options]`;
- the default run no longer deselects the directory, so it would run in it;
- an item collected there does not carry the marker, which would leave the
  default run executing it even with the exclusion in place.

The last one is why `tests/integration/conftest.py` marks every item it collects
instead of each module carrying `pytestmark`: a test module there that forgot
the attribute still cannot leak. Everything here runs pytest again in a fresh
interpreter -- never this suite's own process, and never against a database:
collection opens none, so no `TEST_DATABASE_URL` is needed to read what the two
commands collect.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PYTEST_OPTIONS = tomllib.loads(
    (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["tool"]["pytest"]["ini_options"]
INTEGRATION_DIR = BACKEND_ROOT / "tests" / "integration"

# The marker, spelled once: what `pyproject.toml` declares and what marks an item.
MARKER = "integration"

# The directory's own test modules, as pytest collects them.
TEST_MODULES = "test_*.py"


def _pytest(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run pytest again, in `backend/`, with the project's own configuration and no environment juggling."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", *arguments],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )


def _collected_ids(*arguments: str) -> list[str]:
    """The test ids those arguments collect, with the project's own `addopts` applied."""
    completed = _pytest(*arguments, "--collect-only", "-q")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [line.strip() for line in completed.stdout.splitlines() if "::" in line]


def test_the_integration_directory_holds_a_test_module() -> None:
    """An empty directory would make every check below pass over nothing."""
    modules = sorted(path.name for path in INTEGRATION_DIR.glob(TEST_MODULES))

    assert modules, (
        f"{INTEGRATION_DIR} holds no {TEST_MODULES} module: the integration suite "
        f"would never run, and `-m integration` would collect nothing"
    )


def test_the_marker_is_declared_in_the_pytest_configuration() -> None:
    """Declared, so `--strict-markers` accepts it and `-m integration` can select it."""
    declared = [marker.split(":", 1)[0].strip() for marker in PYTEST_OPTIONS["markers"]]

    assert MARKER in declared


def test_the_default_run_deselects_every_item_in_the_integration_directory() -> None:
    """`cd backend && uv run pytest` collects this suite and executes none of it.

    Asked for the directory on its own, the default run says how many items it
    deselected: that count is the whole suite, and no test of it ran. A
    path-based exclusion instead of the marker would report "no tests ran" here,
    which is exactly the silent version this case refuses.
    """
    completed = _pytest("tests/integration", "-q")

    # 5 is pytest's own "no tests ran", which is this command's whole point; the
    # deselected count below is what distinguishes it from an empty directory.
    assert completed.returncode in {0, 5}, completed.stdout + completed.stderr
    assert "deselected" in completed.stdout
    assert "passed" not in completed.stdout
    assert "skipped" not in completed.stdout


def test_every_item_collected_under_the_directory_carries_the_marker() -> None:
    """An item without the marker would be run by the default command.

    The directory is collected twice -- once asking for the marker, once with no
    marker expression at all -- and the two lists have to be the same one. The
    marked list is what `-m integration` runs; the unmarked one is everything the
    directory holds. A test there that forgot the marker is in the second list
    and not in the first, and fails here.
    """
    marked = _collected_ids("tests/integration", "-m", MARKER)
    everything = _collected_ids("tests/integration", "-m", "")

    assert marked, "the marked command collects nothing: the suite would never run"
    assert marked == everything


def test_the_marked_command_collects_this_directory_and_nothing_else() -> None:
    """`-m integration` over the whole suite: every id from here, and no id from outside it."""
    ids = _collected_ids("-m", MARKER)

    assert ids
    assert [test_id for test_id in ids if not test_id.startswith("tests/integration/")] == []
