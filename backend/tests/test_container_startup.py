"""The image ships the migrations and runs them before it serves (#47).

Docker is not needed for either half. What the runtime stage copies is text in
`backend/Dockerfile`, and the startup rule -- `alembic upgrade head` first, and a
failed upgrade exits non-zero without the command it was given -- is the
entrypoint script's own shell behaviour, so it is measured by running that script
with a stub `alembic` on `PATH` and a command that records having run.

`tests/test_dockerignore.py` is what keeps the copied files copyable: they are
inside the repository-root context the build is governed by.
"""

import configparser
import subprocess
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
DOCKERFILE = BACKEND_ROOT / "Dockerfile"
ENTRYPOINT = BACKEND_ROOT / "docker-entrypoint.sh"
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
REVISIONS_DIR = BACKEND_ROOT / "migrations" / "versions"

# The revision the history starts at, and the file the image's `versions/` holds.
BASELINE_REVISION_FILE = "0001_baseline_five_tables.py"
BASELINE_REVISION = "0001_baseline"

_UVICORN_COMMAND = 'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'


def _instructions_by_stage() -> dict[str, list[str]]:
    """The Dockerfile's instructions, grouped by the stage each belongs to.

    The name is the `AS <name>` of the stage's own `FROM`, and the comments above
    an instruction are skipped: this reads what the build does, not what it says.
    """
    stages: dict[str, list[str]] = {}
    instructions: list[str] | None = None
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        instruction = line.strip()
        if not instruction or instruction.startswith("#"):
            continue
        if instruction.startswith("FROM "):
            instructions = stages.setdefault(instruction.split()[-1], [])
            continue
        assert instructions is not None, "an instruction before the first FROM"
        instructions.append(instruction)
    return stages


def _copies(stage: str) -> dict[str, str]:
    """Each `COPY` of a stage, as the context path it takes mapped to its destination."""
    return {
        parts[1]: parts[2]
        for parts in (instruction.split() for instruction in _instructions_by_stage()[stage])
        if parts[0] == "COPY" and not parts[1].startswith("--")
    }


def _run_entrypoint(
    tmp_path: Path, *, alembic_exit: int, command: list[str]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the entrypoint with a stub `alembic` on `PATH`, and the arguments it was given.

    The stub is what the image's own `alembic` is for the script: one command on
    `PATH`. It records its arguments and exits with the code under test.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    recorded = tmp_path / "alembic-arguments"
    stub = stub_dir / "alembic"
    stub.write_text(f'#!/bin/sh\necho "$@" > "{recorded}"\nexit {alembic_exit}\n', encoding="utf-8")
    stub.chmod(0o755)

    result = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), *command],
        # A `PATH` of the stub and the system's own directories, and nothing
        # inherited: the script reads no other variable, and `tests/test_env_example.py`
        # reads the names this file mentions.
        env={"PATH": f"{stub_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return result, recorded


def test_the_runtime_stage_copies_the_migration_path_beside_app() -> None:
    """The stage that serves has to carry the history this image's schema is defined by."""
    copies = _copies("runtime")

    assert copies["backend/app"] == "./app"
    assert copies["backend/alembic.ini"] == "./alembic.ini"
    assert copies["backend/migrations"] == "./migrations"
    assert copies["backend/docker-entrypoint.sh"] == "./docker-entrypoint.sh"


def test_the_versions_directory_the_image_copies_holds_the_baseline_revision() -> None:
    """`versions/` ships whole, so the revision the history starts at is in the image."""
    revision_file = REVISIONS_DIR / BASELINE_REVISION_FILE

    assert revision_file.is_file()
    assert f'revision: str = "{BASELINE_REVISION}"' in revision_file.read_text(encoding="utf-8")


def test_the_committed_alembic_config_carries_no_url_value() -> None:
    """The file the image copies names where the history is, and no database.

    `migrations/env.py` takes the URL from `app.db.database_url()`, so this file,
    committed to a public repository and copied into the image, holds no URL and
    no credential: the only thing it says about a database is nothing.
    """
    config = configparser.ConfigParser()
    config.read(ALEMBIC_INI, encoding="utf-8")

    assert config["alembic"]["script_location"] == "migrations"
    assert "sqlalchemy.url" not in config["alembic"]


def test_every_start_goes_through_the_entrypoint_with_uvicorn_still_its_command() -> None:
    """The upgrade is not a step somebody remembers: it is the image's entrypoint."""
    instructions = _instructions_by_stage()["runtime"]

    assert 'ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]' in instructions
    assert instructions[-1] == _UVICORN_COMMAND


def test_the_upgrade_runs_first_and_its_command_runs_after_it(tmp_path: Path) -> None:
    marker = tmp_path / "served"
    result, recorded = _run_entrypoint(
        tmp_path, alembic_exit=0, command=["touch", str(marker)]
    )

    assert result.returncode == 0
    assert recorded.read_text(encoding="utf-8").split() == ["upgrade", "head"]
    assert marker.exists()


def test_a_failed_upgrade_exits_non_zero_and_never_runs_the_command(tmp_path: Path) -> None:
    """No half-served API: the command is not started when the upgrade failed."""
    marker = tmp_path / "served"
    result, recorded = _run_entrypoint(
        tmp_path, alembic_exit=7, command=["touch", str(marker)]
    )

    assert recorded.read_text(encoding="utf-8").split() == ["upgrade", "head"]
    assert result.returncode == 7
    assert not marker.exists()


def test_the_api_healthcheck_reads_the_migration_state_as_well_as_healthz() -> None:
    """`/healthz` answering is not enough: the check also reads the revision the image carries."""
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    api = compose.split("\n  api:")[1].split("\n  db:")[0]

    assert "/healthz" in api
    assert "alembic current" in api
    assert "(head)" in api
