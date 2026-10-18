"""`make test` runs every suite the Testing criterion names, in one command.

The rubric asks that unit and integration tests "run with a single command". That
command is the root `Makefile`'s `test` target, which names each suite and hands
it, with the throwaway PostgreSQL's port, to `scripts/test.sh`. Nothing else in
the suite reads either file, so the failure mode is rot: a suite quietly dropped
from the command, or a failure swallowed so that `make test` finishes green
having run less than it says. This module reads the two files as text and pins
what the recipe is -- the suites, by the command that runs each, and the two
sides of the fail-fast: the script's `set -euo pipefail`, and a `test` target
that ignores no error.

The names, commands and paths below are asserted rather than parsed out of prose:
the recipe's own text is the source, and a rename has to move both sides in the
same commit.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
SCRIPT = REPO_ROOT / "scripts" / "test.sh"

# One line of the `test` target per suite: the name a failure is reported under,
# and the command that runs the suite. A line dropped, or a command changed on
# one side alone, is a failing test.
EXPECTED_SUITES = {
    "backend": "cd backend && uv run pytest",
    "integration": "cd backend && uv run pytest -m integration",
    "frontend": "cd frontend && npm test",
    "mcp": "cd mcp && uv run pytest",
    "oncall": "uv run --project backend pytest ops/oncall",
}

# The one command the target runs: the script that starts the throwaway
# PostgreSQL and runs every suite against it, in the one process that removes it.
ENTRY = "./scripts/test.sh"


def recipe_lines() -> list[str]:
    """The `test` target's recipe, one entry per source line, backslash dropped."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    assert "test:" in lines, "the Makefile has no `test` target"

    recipe = []
    for line in lines[lines.index("test:") + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip().rstrip("\\").strip())
    return recipe


def test_the_test_target_names_every_suite() -> None:
    """Each suite is its own line of the recipe, so one dropped is one named."""
    recipe = recipe_lines()

    missing = [
        suite
        for suite, command in EXPECTED_SUITES.items()
        if f'{suite} "{command}"' not in recipe
    ]

    assert missing == [], f"the Makefile's `test` target does not name: {missing}"


def test_the_recipe_keeps_a_failure_past_a_failing_step() -> None:
    """Nothing in the recipe swallows a failure, and the script runs fail-fast."""
    recipe = recipe_lines()

    assert recipe and recipe[0].startswith(ENTRY), (
        f"the `test` target does not run {ENTRY}: {recipe[:1]}"
    )
    ignored = [
        line for line in recipe if line.startswith("-") or line.endswith("|| true")
    ]
    assert ignored == [], f"the recipe ignores a failure: {ignored}"
    assert "set -euo pipefail" in SCRIPT.read_text(encoding="utf-8")
