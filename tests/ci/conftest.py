"""Keep mocked CI commands from overwriting the invoking run's diagnostic artifacts."""

import io
import tarfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_ci_reports(tmp_path, monkeypatch):
    """Redirect simulated controller timing reports while preserving tests' explicitly owned temporary files."""
    original_write = Path.write_text
    original_unlink = Path.unlink
    shared = Path(__file__).resolve().parents[2] / "results/ci"

    def write(path, *arguments, **options):
        """Only mocked pipeline diagnostics belong in this test's temporary report directory."""
        if (
            path.name in {"phase-times.json", "phase-times-preflight.json", "selection.json"}
            and path.resolve().parent == shared
        ):
            path = tmp_path / path.name
        return original_write(path, *arguments, **options)

    monkeypatch.setattr(Path, "write_text", write)

    def unlink(path, *arguments, **options):
        """A simulated pipeline may reset its own pytest reports, never those of the real parent run."""
        if path.resolve().parent == shared and path.suffix == ".xml":
            path = tmp_path / path.name
        return original_unlink(path, *arguments, **options)

    monkeypatch.setattr(Path, "unlink", unlink)


@pytest.fixture
def policy_archive():
    """Build independently supplied immutable policy files without needing Git in native test images."""

    def archive(files):
        """Encode the provided trusted scripts and their package initializers as raw Git-style tar bytes."""
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as snapshot:
            for name, source in {
                "script/__init__.py": "",
                "script/ci/__init__.py": "",
                **files,
            }.items():
                content = source.encode("utf-8")
                member = tarfile.TarInfo(name)
                member.size = len(content)
                snapshot.addfile(member, io.BytesIO(content))
        return output.getvalue()

    return archive
