"""`ci.yml` and `make test` run the same suites: drift on either side is red here.

The root `Makefile`'s `test` target names the four suites the rubric's Testing
criterion asks for (#88) and runs them in one local invocation through
`scripts/test.sh`. `.github/workflows/ci.yml` reaches each of the four through
the `Makefile`'s own target for that suite -- `make test-backend`,
`make test-integration`, `make test-frontend`, `make test-mcp` (#107) -- because
each job keeps its own service container and its own verdict. That is what makes
the four commands spelled once, in the `Makefile`, rather than again in a job's
`run:`. This module reads both files as text and compares the suites each side
runs, matched by the command and the directory it runs in -- not by job, step or
line, whose counts move with every unrelated change -- so a suite dropped from
one side and left on the other, a job that stops reaching its suite through the
`Makefile`, or a suite re-spelled as a step is a red build rather than a
reviewer's catch.

Each side is read the way its own file names things, never from prose. The
`Makefile` side is `test_single_test_command.py`'s reading of the `test` target's
recipe, reused so that the local command is pinned in one spelling: the four
suite names and commands it holds are the vocabulary the failures below use. The
`ci.yml` side is each job's own steps -- a step's `run:` line and the
`working-directory:` it runs in -- so the header comment's own command lines are
prose, not steps. The per-suite targets are read the same way the `test` target
is, one recipe line each, so a target and the recipe's line for that suite
naming different commands is red too. `scripts/test.sh` is
`test_single_test_command.py`'s other half.

The checks `ci.yml` runs that `make test` deliberately does not are data below
(`CI_ONLY_CHECKS`): the difference is on purpose (#88 leaves the security scan to
CI, #106 owns the responder's test), and a guard that asked for parity on them
would be asking for a change to one of the two files. A test command left in a
step that is neither a suite reached through a `Makefile` target nor one of those
entries is red, and so is an entry whose command has left the file it was read
from. Neither file is modified by this module: the two are what is compared.
"""

import re
from pathlib import Path

from tests.test_single_test_command import EXPECTED_SUITES, recipe_lines

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The checks `ci.yml` runs that `make test` deliberately does not, one entry per
# check: the command text and the workflow file it was read from. The committed
# data guard (#19) is a subset of the backend suite, kept on its own step as
# CI's faster verdict; the on-call responder's offline test (#93) is run locally
# by #106, not here; the security scan's scope and dispositions are
# `security/scan.md` (#82).
CI_ONLY_CHECKS = [
    ("uv run pytest tests/test_committed_data_guard.py", CI),
    ("uv run --project backend pytest ops/oncall", CI),
    (
        "uv run --project backend ruff check --select S --output-format concise"
        " backend/app mcp/jobradar_mcp",
        CI,
    ),
]

# One line of the `test` target's recipe that names a suite: `mcp "cd mcp && uv run pytest"`.
_SUITE_LINE = re.compile(r'^(\S+) "(.+)"$')

# A suite command in the shell spelling both surfaces use: `cd <directory> && <command>`.
_CD_AND_COMMAND = re.compile(r"^cd (\S+) && (.+)$")

# A command that runs a test, whatever it is spelled through: `uv run pytest`,
# `uv run --project backend pytest ops/oncall`, `npm test`. The word boundaries
# keep a step that only reads a summary out of the suites:
# `grep -qE '... passed' /tmp/pytest-summary.txt` invokes no test.
_TEST_INVOCATION = re.compile(r"(?:^|\s)(?:pytest|npm test)(?:\s|$)")


def suite_key(command: str) -> tuple[str | None, str]:
    """A suite command as the directory it runs in and the command itself.

    `cd mcp && uv run pytest` and a step's `uv run pytest` under
    `working-directory: mcp` are the same suite, which is why the key is the pair
    and not the text. A command with no `cd` runs in the directory a step names,
    or nowhere either surface names.
    """
    match = _CD_AND_COMMAND.match(command)
    return (match.group(1), match.group(2)) if match else (None, command)


def _spelling(key: tuple[str | None, str]) -> str:
    """A suite key back in the shell spelling both surfaces use."""
    directory, command = key
    return f"cd {directory} && {command}" if directory else command


def local_suites() -> dict[str, tuple[str | None, str]]:
    """The `test` target's recipe: each suite it names, and the key its own line runs."""
    suites: dict[str, tuple[str | None, str]] = {}
    for line in recipe_lines():
        match = _SUITE_LINE.match(line)
        if match is not None:
            suites[match.group(1)] = suite_key(match.group(2))
    return suites


def suite_target(name: str) -> str:
    """The `Makefile` target that runs one suite on its own, the way `ci.yml` calls it."""
    return f"test-{name}"


def target_recipe(target: str) -> list[str]:
    """One `Makefile` target's recipe, one entry per source line, backslash dropped.

    `test_single_test_command.recipe_lines()`'s reading, for the per-suite targets
    the workflow calls: a target that has left the `Makefile` fails here, by name.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    assert f"{target}:" in lines, f"the Makefile has no `{target}` target"

    recipe = []
    for line in lines[lines.index(f"{target}:") + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip().rstrip("\\").strip())
    return recipe


def ci_steps(workflow: Path) -> list[list[str]]:
    """Every step of every job in the workflow, as the lines that belong to it.

    The `steps:` lists are found by indentation, with the standard library only: a
    step is an entry of one of those lists, and the lines indented under it -- its
    other keys, and a block scalar's body -- are part of that step. Comments are
    dropped, so a command the header names is not a step that runs it.
    """
    steps: list[list[str]] = []
    steps_indent: int | None = None
    for line in workflow.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "steps:":
            steps_indent = indent
        elif steps_indent is not None and indent == steps_indent + 2 and stripped.startswith("- "):
            steps.append([stripped[2:]])
        elif steps_indent is not None and steps and indent > steps_indent + 2:
            steps[-1].append(stripped)
        elif steps_indent is not None and indent <= steps_indent:
            steps_indent = None
    return steps


def step_run(step: list[str]) -> tuple[str | None, str]:
    """One step's `run:` text and its `working-directory:`, either of them absent."""
    working_directory: str | None = None
    run = ""
    for index, line in enumerate(step):
        key, _, value = line.partition(":")
        if key == "working-directory":
            working_directory = value.strip()
        elif key == "run":
            value = value.strip()
            # `run: |`: the command lines are the ones indented under the key.
            # shortcut: a block scalar's body runs to the end of the step, as it
            # does in `ci.yml`; a key written after one would read as a command.
            run = "\n".join(step[index + 1 :]) if value.startswith(("|", ">")) else value
    return working_directory, run


def ci_run_lines(workflow: Path) -> list[tuple[str | None, str]]:
    """Each command line of each step's `run:`, with the directory it runs in.

    A command piped into another one is the command before the pipe: a suite whose
    summary is teed to a file (`make test-backend | tee ...`) is that suite, and
    the pipeline's other half is not a second command.
    """
    commands: list[tuple[str | None, str]] = []
    for step in ci_steps(workflow):
        working_directory, run = step_run(step)
        for line in run.splitlines():
            command = line.split("|")[0].strip()
            if command:
                commands.append((working_directory, command))
    return commands


def ci_suite_invocations() -> list[tuple[str | None, str]]:
    """Every command in `ci.yml` that runs a test, as (directory, command)."""
    return [
        invocation
        for invocation in ci_run_lines(CI)
        if _TEST_INVOCATION.search(invocation[1])
    ]


def ci_make_targets() -> list[str]:
    """Every `make <target>` command in a step of the workflow, in step order.

    A target piped into another command is read before the pipe, the way a suite
    whose summary is teed to a file is that suite. A step's prose is not read.
    """
    targets: list[str] = []
    for _, command in ci_run_lines(CI):
        words = command.split()
        if len(words) > 1 and words[0] == "make":
            targets.append(words[1])
    return targets


def test_each_of_the_four_suites_is_named_by_the_local_command() -> None:
    """A suite the criterion names, dropped from the recipe, fails here, by name."""
    local = local_suites()

    missing = [
        f"{name} ({command})"
        for name, command in EXPECTED_SUITES.items()
        if suite_key(command) != local.get(name)
    ]

    assert missing == [], f"`make test` names no line for the suite(s): {missing}"


def test_each_suite_target_runs_what_the_local_command_names() -> None:
    """A target and the recipe's line for its suite, naming different commands, fails.

    The four commands live twice inside the `Makefile` -- in the `test` target's
    recipe and in the per-suite targets `ci.yml` calls -- so this is the pin that
    keeps those two spellings one.
    """
    mismatched = [
        f"`make {suite_target(name)}` runs {target_recipe(suite_target(name))}, "
        f"`make test` names {_spelling(key)}"
        for name, key in local_suites().items()
        if [suite_key(line) for line in target_recipe(suite_target(name))] != [key]
    ]

    assert mismatched == [], (
        "a per-suite target and the `test` target's line run different commands: "
        f"{mismatched}"
    )


def test_each_of_the_four_suites_is_reached_in_ci_through_its_target() -> None:
    """A suite whose `make test-<suite>` call left a job fails here, by name."""
    targets = ci_make_targets()

    missing = [
        f"{name} (`make {suite_target(name)}`)"
        for name in EXPECTED_SUITES
        if suite_target(name) not in targets
    ]

    assert missing == [], f"ci.yml runs no step for the suite(s): {missing}"


def test_the_two_surfaces_run_the_same_suites() -> None:
    """A side running a suite the other does not, bar the recorded entries, fails here."""
    local = local_suites()
    targets = ci_make_targets()

    missing_from_ci = [
        f"{name} (`make {suite_target(name)}`)"
        for name in local
        if suite_target(name) not in targets
    ]
    assert missing_from_ci == [], f"ci.yml runs no step for: {missing_from_ci}"

    only_in_ci = [
        f"`make {target}` ({CI.relative_to(REPO_ROOT)})"
        for target in targets
        if target.startswith("test-") and target[len("test-") :] not in local
    ]
    assert only_in_ci == [], f"`make test` names no suite for: {only_in_ci}"


def test_ci_spells_no_suite_invocation() -> None:
    """A suite re-spelled as a step, instead of reached through the `Makefile`, fails.

    The four jobs name no suite command and no suite's working directory: the
    command is the `Makefile` target's. A test command in a step is the guard's
    own fast verdict or another recorded check.
    """
    entries = {command for command, _ in CI_ONLY_CHECKS}

    spelled = [
        _spelling(invocation)
        for invocation in ci_suite_invocations()
        if invocation[1] not in entries
    ]

    assert spelled == [], (
        "ci.yml spells a suite command instead of reaching it through the "
        f"Makefile: {spelled}"
    )


def test_every_ci_only_check_is_still_run_by_ci() -> None:
    """A recorded difference whose command has left its file is a difference that rotted."""
    for command, workflow in CI_ONLY_CHECKS:
        run_lines = [line for _, line in ci_run_lines(workflow)]

        assert command in run_lines, (
            f"{command} is no longer a `run:` line in "
            f"{workflow.relative_to(REPO_ROOT)}"
        )
