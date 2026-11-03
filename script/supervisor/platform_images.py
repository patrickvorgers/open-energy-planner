"""Read stable platform image pins from the Dockerfiles maintained by Dependabot."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_RECIPES = {
    "supervisor": ROOT / "Dockerfile.supervisor",
    "core": ROOT / "Dockerfile.chain-test",
}
PLATFORM_REPOSITORIES = {
    "supervisor": "ghcr.io/home-assistant/amd64-hassio-supervisor",
    "core": "ghcr.io/home-assistant/home-assistant",
}


def platform_pin(service, recipe=None):
    """Require the official repository, a stable calendar release and a complete immutable digest."""
    source = PLATFORM_RECIPES[service] if recipe is None else recipe
    first_instruction = next(
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not first_instruction.startswith("FROM "):
        raise ValueError("Platform Dockerfiles must start with an explicit FROM image")
    image = first_instruction.removeprefix("FROM ").strip()
    match = re.fullmatch(
        re.escape(PLATFORM_REPOSITORIES[service])
        + r":(?P<version>[0-9]{4}\.(?:[1-9]|1[0-2])\.[0-9]+)@sha256:[0-9a-f]{64}",
        image,
    )
    if match is None:
        raise ValueError("Platform images require an official stable release and SHA256 digest")
    return image, match["version"]
