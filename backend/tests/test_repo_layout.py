"""`README.md`'s Repository layout section, and the paths it promises are here.

A reviewer arriving from the rubric's expected-contents list reads that section to
find where each name the list gives lives in this repository. Nothing else in the
suite or the image reads it, so the failure mode is rot: a path the section names
that was renamed, or that never existed. This module reads the section out of
`README.md` -- prose around the paths included -- and resolves every path it
names, so a path that is not on disk fails here, by name, rather than at the
reviewer's terminal.

The paths are the section's backticked spans, which is why the section backticks
only what is here: the rubric's own names are plain text in it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"

SECTION_HEADING = "## Repository layout"
_BACKTICKED = re.compile(r"`([^`]+)`")

# The rubric's names and where each one lives here, spelled as the section spells
# them: both sides of every row, so a row dropped or a path changed on one side
# alone is a failing test rather than a reviewer's catch.
EXPECTED_MAPPINGS = {
    "docs/": "_docs/",
    "product-spec.md": "_docs/plan.md",
    "openapi.yaml": "backend/openapi.json",
    "docker-compose.yml": "compose.yaml",
    "mcp-server/": "mcp/",
    "security/": "security/",
    "ops/": "ops/",
}


def layout_section() -> str:
    """The Repository layout section: its heading to the next `##` heading, exclusive."""
    lines = README.read_text(encoding="utf-8").splitlines()
    assert SECTION_HEADING in lines, f"README.md has no '{SECTION_HEADING}' heading"
    start = lines.index(SECTION_HEADING)
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if line.startswith("## ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def named_paths() -> list[str]:
    """Every backticked span in the section that is a path: no whitespace in it."""
    return [
        span
        for span in _BACKTICKED.findall(layout_section())
        if not any(char.isspace() for char in span)
    ]


def test_every_path_the_layout_section_names_exists() -> None:
    """A path named in the section and not on disk fails here, named in the failure."""
    missing = [path for path in named_paths() if not (REPO_ROOT / path).exists()]

    assert missing == [], f"named in the Repository layout section, not on disk: {missing}"


def test_the_layout_section_maps_every_name_the_rubric_lists() -> None:
    """One row per rubric name, with this repository's path in the same row."""
    lines = layout_section().splitlines()

    assert [
        name
        for name, path in EXPECTED_MAPPINGS.items()
        if not any(name in line and path in line for line in lines)
    ] == []
