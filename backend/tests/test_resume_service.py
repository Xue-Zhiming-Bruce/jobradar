"""Where the uploads directory comes from, and how a file lands in it.

The two settings this task adds are read in one place each, the way
`DATABASE_URL` is read in `app/db.py` only, and the default has to keep what
arrives at runtime -- a resume -- out of a public repository. The suite therefore
redirects `UPLOADS_DIR` at a temporary directory; nothing here writes to the
default one, it only checks where the default points.
"""

from pathlib import Path

import pytest

from app.services import resume

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BACKEND_ROOT.parent
APP_ROOT = BACKEND_ROOT / "app"

_EXPECTED_EXTENSIONS = {
    resume.TEXT_PLAIN: ".txt",
    resume.APPLICATION_PDF: ".pdf",
    resume.DOCX: ".docx",
}


def test_uploads_dir_is_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in APP_ROOT.rglob("*.py")
        if resume.UPLOADS_DIR_VARIABLE in path.read_text(encoding="utf-8")
    )

    assert readers == ["resume.py"]


def test_max_upload_bytes_is_read_in_one_module_only() -> None:
    readers = sorted(
        path.name
        for path in APP_ROOT.rglob("*.py")
        if resume.MAX_UPLOAD_BYTES_VARIABLE in path.read_text(encoding="utf-8")
    )

    assert readers == ["resume.py"]


def test_the_configured_uploads_directory_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOADS_DIR", "/tmp/jobradar-synthetic-uploads")

    assert resume.uploads_dir() == Path("/tmp/jobradar-synthetic-uploads")


@pytest.mark.parametrize("empty", ["unset", ""])
def test_the_default_uploads_directory_is_outside_the_repository(
    empty: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset means a directory no `git add` in this checkout can reach."""
    if empty == "unset":
        monkeypatch.delenv("UPLOADS_DIR", raising=False)
    else:
        monkeypatch.setenv("UPLOADS_DIR", "")

    directory = resume.uploads_dir()

    assert directory == resume.DEFAULT_UPLOADS_DIR
    assert not directory.is_relative_to(REPOSITORY_ROOT)
    assert not directory.is_relative_to(BACKEND_ROOT)
    assert directory.is_relative_to(Path.home())


@pytest.mark.parametrize("empty", ["unset", ""])
def test_the_default_upload_limit_applies(empty: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if empty == "unset":
        monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    else:
        monkeypatch.setenv("MAX_UPLOAD_BYTES", "")

    assert resume.max_upload_bytes() == resume.DEFAULT_MAX_UPLOAD_BYTES


def test_the_configured_upload_limit_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2048")

    assert resume.max_upload_bytes() == 2048


def test_reading_the_configuration_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    assert resume.uploads_dir() == tmp_path / "uploads"
    assert resume.max_upload_bytes() > 0
    assert not (tmp_path / "uploads").exists()


def test_saving_an_upload_creates_the_directory_and_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory appears on the first upload, and the name is the server's own."""
    directory = tmp_path / "uploads"
    monkeypatch.setenv("UPLOADS_DIR", str(directory))
    assert not directory.exists()

    path = resume.save_upload(b"synthetic resume bytes", resume.TEXT_PLAIN)

    assert directory.is_dir()
    assert path.parent == directory
    assert path.suffix == ".txt"
    assert path.read_bytes() == b"synthetic resume bytes"


@pytest.mark.parametrize("content_type", sorted(_EXPECTED_EXTENSIONS))
def test_each_format_is_stored_with_its_own_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    assert resume.save_upload(b"x", content_type).suffix == _EXPECTED_EXTENSIONS[content_type]
