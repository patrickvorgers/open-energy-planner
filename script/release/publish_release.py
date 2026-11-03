"""Resume release publication from one immutable tested CI bundle without replacing assets."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from script.release.package_release import version_from_tag

ARTIFACT_NAMES = ("open_energy_planner.zip", "ha-app-repository.tar.gz")
ASSET_NAMES = ("release-manifest.json", "SHA256SUMS", *ARTIFACT_NAMES)


def publish(tag, run_id, run_number, attempt):
    """Validate existing assets and upload only missing files from the first published bundle.

    The manifest is uploaded first and binds retries to its original workflow
    attempt. Recovery requires that attempt's retained Actions artifact. A
    different workflow run, changed tag or conflicting asset fails before writes.
    GitHub CLI supplies authentication through its existing environment.
    """
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    release = json.loads(
        subprocess.check_output(["gh", "release", "view", tag, "--json", "assets"], text=True)
    )
    existing = {asset["name"] for asset in release["assets"]} & set(ASSET_NAMES)
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        previous = root / "previous"
        previous.mkdir()
        for name in sorted(existing):
            subprocess.run(
                ["gh", "release", "download", tag, "--pattern", name, "--dir", str(previous)],
                check=True,
            )
        if existing:
            if "release-manifest.json" not in existing:
                raise ValueError("Existing release assets have no bundle manifest")
            original = json.loads((previous / "release-manifest.json").read_text(encoding="utf-8"))
            build = original.get("build", "")
            parts = build.split(".") if isinstance(build, str) else []
            if len(parts) != 2 or parts[0] != str(run_number) or not parts[1].isdigit():
                raise ValueError("Release assets belong to another workflow run")
            attempt = parts[1]
        bundle = root / "bundle"
        subprocess.run(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--name",
                f"installation-bundle-amd64-{attempt}",
                "--dir",
                str(bundle),
            ],
            check=True,
        )
        manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
        expected = {
            "version": version_from_tag(tag),
            "release_tag": tag,
            "revision": revision,
            "build": f"{run_number}.{attempt}",
            "source_state": "commit",
        }
        if any(manifest.get(field) != value for field, value in expected.items()):
            raise ValueError("Release bundle identity differs from the tested release")
        hashes = manifest["artifacts"]
        if set(hashes) != set(ARTIFACT_NAMES) or any(
            hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest
            for name, digest in hashes.items()
        ):
            raise ValueError("Release bundle artifact checksum mismatch")
        sums = "".join(f"{hashes[name]}  {name}\n" for name in ARTIFACT_NAMES).encode()
        if (bundle / "SHA256SUMS").read_bytes() != sums:
            raise ValueError("Release bundle checksum list differs from its manifest")
        if any((previous / name).read_bytes() != (bundle / name).read_bytes() for name in existing):
            raise ValueError("Existing release asset differs from the original tested bundle")
        for name in ASSET_NAMES:
            if name not in existing:
                subprocess.run(["gh", "release", "upload", tag, str(bundle / name)], check=True)


if __name__ == "__main__":
    publish(
        os.environ["OEP_RELEASE_TAG"],
        os.environ["GITHUB_RUN_ID"],
        os.environ["GITHUB_RUN_NUMBER"],
        os.environ["OEP_BUILD_ATTEMPT"],
    )
