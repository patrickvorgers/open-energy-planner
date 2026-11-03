"""Build one private, version-matched app/integration bundle without publishing a release."""

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

from script.release.package_ha_app import APP_INPUTS, package, reject_ignored, validate_sources

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "open_energy_planner"
DEVELOPMENT_VERSION = "0.0.0.dev0"
MAX_DOCKER_TAG_LENGTH = 128


def version_from_tag(tag):
    """Accept a semantic release tag with an optional v prefix and Docker-compatible prerelease identifiers."""
    numeric = r"(?:0|[1-9][0-9]*)"
    prerelease = rf"(?:{numeric}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    match = re.fullmatch(
        rf"v?({numeric}\.{numeric}\.{numeric}(?:-{prerelease}(?:\.{prerelease})*)?)",
        tag,
    )
    if not match or len(match[1]) > MAX_DOCKER_TAG_LENGTH:
        raise ValueError(
            "Release tag must contain a Docker-compatible semantic version, for example v0.3.0"
        )
    return match[1]


def release_version(root):
    """Reject inconsistent development templates before stamping a GitHub release or development bundle."""
    values = [
        json.loads(
            (root / "custom_components" / DOMAIN / "manifest.json").read_text(encoding="utf-8")
        )["version"],
        json.loads((root / "ha_app/config.yaml").read_text(encoding="utf-8"))["version"],
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        json.loads(
            (root / "custom_components" / DOMAIN / "build-info.json").read_text(encoding="utf-8")
        )["version"],
        json.loads((root / "src" / DOMAIN / "build-info.json").read_text(encoding="utf-8"))[
            "version"
        ],
    ]
    if len(set(values)) != 1:
        raise ValueError("App, integration and package release versions must match")
    return values[0]


def build(destination, *, revision=None, build_number=None, release_tag=None):
    """Archive only release inputs, retain original sources and bind both artifacts to the same Git revision."""
    release_version(ROOT)
    version = DEVELOPMENT_VERSION if release_tag is None else version_from_tag(release_tag)
    source_state = "unverified" if revision is not None else "commit"
    if (
        revision is None
        and subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    ):
        raise ValueError("Commit source changes before building a release bundle")
    if revision is None:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    if len(revision) != 40 or any(candidate not in "0123456789abcdef" for candidate in revision):
        raise ValueError("An immutable Git revision is required")
    if source_state == "commit" and release_tag is not None:
        tagged_revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"refs/tags/{release_tag}^{{commit}}"],
            cwd=ROOT,
            text=True,
        ).strip()
        if tagged_revision != revision:
            raise ValueError("Release tag must identify the packaged commit")
    if build_number is not None and (
        not isinstance(build_number, str)
        or not re.fullmatch(r"[1-9][0-9]*\.[1-9][0-9]*", build_number)
    ):
        raise ValueError("Build number must identify the workflow run and attempt")
    identity = {
        "version": version,
        "build": build_number,
        "revision": revision,
        "release_tag": release_tag,
    }
    validate_sources(ROOT, (*APP_INPUTS, "custom_components/" + DOMAIN))
    if source_state == "commit":
        reject_ignored(ROOT, (*APP_INPUTS, "custom_components/" + DOMAIN))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    app = package(destination / "ha-app-repository")
    app_config = app / "config.yaml"
    config = json.loads(app_config.read_text(encoding="utf-8"))
    config["version"] = version
    app_config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
    identity_bytes = (json.dumps(identity, indent=2) + "\n").encode()
    (app / "src" / DOMAIN / "build-info.json").write_bytes(identity_bytes)
    integration = destination / (DOMAIN + ".zip")
    with zipfile.ZipFile(integration, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "custom_components" / DOMAIN).rglob("*")):
            relative = path.relative_to(ROOT / "custom_components" / DOMAIN)
            if (
                path.is_file()
                and "__pycache__" not in relative.parts
                and not any(part.startswith(".") for part in relative.parts)
                and path.suffix in (".py", ".json", ".yaml", ".png", ".svg")
            ):
                if relative.as_posix() == "build-info.json":
                    archive.writestr("build-info.json", identity_bytes)
                elif relative.as_posix() == "manifest.json":
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                    metadata["version"] = version
                    archive.writestr("manifest.json", json.dumps(metadata, indent=2) + "\n")
                else:
                    archive.write(path, relative.as_posix())
        for name in ("LICENSE", "NOTICE"):
            archive.write(ROOT / name, name)
    app_archive = destination / "ha-app-repository.tar.gz"
    with tarfile.open(app_archive, "w:gz") as archive:
        archive.add(app.parent, arcname="ha-app-repository")
    artifacts = {
        artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()
        for artifact in (integration, app_archive)
    }
    manifest = {
        "version": version,
        "revision": revision,
        "build": build_number,
        "release_tag": release_tag,
        "source_state": source_state,
        "artifacts": artifacts,
        "native_haos_acceptance": "not_recorded",
    }
    (destination / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (destination / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in artifacts.items()),
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main():
    """Prepare a new local/CI bundle. Publication remains a separate authorized release action."""
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    parser.add_argument(
        "--release-tag", help="Published GitHub release tag. Omit for development builds."
    )
    parser.add_argument(
        "--build-number", help="GitHub Actions run number and attempt, for example 142.2"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            build(args.destination, build_number=args.build_number, release_tag=args.release_tag),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
