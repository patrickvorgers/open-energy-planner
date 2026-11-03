"""Platform pins must stay stable, official, immutable and aligned with the real HA test runtime."""

from pathlib import Path

import pytest

from script.supervisor.platform_images import platform_pin


def test_core_acceptance_and_framework_share_identical_pinned_image():
    """Actual Supervisor installation and framework tests must execute the same official Core image bytes."""
    root = Path(__file__).resolve().parents[3]
    image, version = platform_pin("core")
    assert image.split("@", 1)[0].endswith(":" + version)
    assert (root / "Dockerfile.chain-test").read_text().splitlines()[0] == "FROM " + image


@pytest.mark.parametrize("version", ["dev", "latest", "2026.11.0b1", "2026.11.0-rc1", "2026.13.1"])
def test_platform_rejects_prereleases_and_mutable_tags(tmp_path, version):
    """Dependency proposals cannot select development, beta, release-candidate or unversioned platform images."""
    manifest = tmp_path / "Dockerfile"
    image = f"ghcr.io/home-assistant/home-assistant:{version}@sha256:" + "a" * 64
    manifest.write_text("FROM " + image + "\n")
    with pytest.raises(ValueError, match="official stable"):
        platform_pin("core", manifest)
