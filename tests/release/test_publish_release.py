"""Release retries retain original tested bytes and reject mixed or corrupted assets."""

import hashlib
import json
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from script.release import publish_release


@pytest.fixture
def release_cli(tmp_path, monkeypatch):
    """Model GitHub downloads/uploads with real files and independent bundle identities."""
    bundles = {}
    for attempt in (1, 2):
        folder = tmp_path / str(attempt)
        folder.mkdir()
        hashes = {}
        for name in ("open_energy_planner.zip", "ha-app-repository.tar.gz"):
            contents = f"tested artifact {name}, attempt {attempt}".encode()
            (folder / name).write_bytes(contents)
            hashes[name] = hashlib.sha256(contents).hexdigest()
        (folder / "release-manifest.json").write_text(
            json.dumps(
                {
                    "version": "1.2.3",
                    "release_tag": "v1.2.3",
                    "build": f"42.{attempt}",
                    "revision": "a" * 40,
                    "source_state": "commit",
                    "artifacts": hashes,
                }
            ),
            encoding="utf-8",
        )
        (folder / "SHA256SUMS").write_bytes(
            "".join(f"{digest}  {name}\n" for name, digest in hashes.items()).encode()
        )
        bundles[str(attempt)] = folder
    state = {"assets": {}, "uploads": [], "downloads": [], "fail_after": None}

    def output(arguments, **kwargs):
        """Return the checkout revision or currently published asset names."""
        if arguments == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        assert arguments == ["gh", "release", "view", "v1.2.3", "--json", "assets"]
        return json.dumps({"assets": [{"name": name} for name in state["assets"]]})

    def run(arguments, **kwargs):
        """Transfer immutable bytes and optionally fail after GitHub accepted an upload."""
        assert kwargs["check"]
        if arguments[:3] == ["gh", "run", "download"]:
            assert arguments[3] == "1001"
            name = arguments[arguments.index("--name") + 1]
            state["downloads"].append(name)
            shutil.copytree(bundles[name.rsplit("-", 1)[1]], arguments[-1])
        elif arguments[:3] == ["gh", "release", "download"]:
            name = arguments[arguments.index("--pattern") + 1]
            (Path(arguments[-1]) / name).write_bytes(state["assets"][name])
        else:
            assert arguments[:4] == ["gh", "release", "upload", "v1.2.3"]
            asset = Path(arguments[4])
            assert asset.name not in state["assets"]
            state["assets"][asset.name] = asset.read_bytes()
            state["uploads"].append(asset.name)
            if len(state["uploads"]) == state["fail_after"]:
                raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setattr(publish_release.subprocess, "check_output", output)
    monkeypatch.setattr(publish_release.subprocess, "run", run)
    return state, bundles


def test_retry_before_any_upload_uses_successful_quality_attempt(release_cli, monkeypatch):
    """A publish-only retry selects the successful producer attempt before any manifest exists."""
    state, _ = release_cli
    for name, value in {
        "OEP_RELEASE_TAG": "v1.2.3",
        "GITHUB_RUN_ID": "1001",
        "GITHUB_RUN_NUMBER": "42",
        "GITHUB_RUN_ATTEMPT": "2",
        "OEP_BUILD_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(name, value)
    runpy.run_path(publish_release.__file__, run_name="__main__")
    assert state["downloads"] == ["installation-bundle-amd64-1"]
    assert json.loads(state["assets"]["release-manifest.json"])["build"] == "42.1"


@pytest.mark.parametrize("fail_after", [None, 1, 2, 3, 4])
def test_release_publication_resumes_same_bytes_even_after_full_workflow_retry(
    release_cli, fail_after
):
    """Any partial upload resumes attempt one, even when attempt two built different bytes."""
    state, bundles = release_cli
    state["fail_after"] = fail_after
    if fail_after:
        with pytest.raises(subprocess.CalledProcessError):
            publish_release.publish("v1.2.3", "1001", "42", "1")
    else:
        publish_release.publish("v1.2.3", "1001", "42", "1")
    state["fail_after"] = None
    publish_release.publish("v1.2.3", "1001", "42", "2")
    assert state["downloads"] == ["installation-bundle-amd64-1"] * 2
    assert state["uploads"] == [
        "release-manifest.json",
        "SHA256SUMS",
        "open_energy_planner.zip",
        "ha-app-repository.tar.gz",
    ]
    assert state["assets"] == {
        name: (bundles["1"] / name).read_bytes() for name in state["uploads"]
    }


@pytest.mark.parametrize(
    "failure", ["conflict", "missing_manifest", "other_run", "checksum", "sums", "revision"]
)
def test_release_conflicts_fail_before_any_upload(release_cli, failure):
    """Changed assets, wrong identity and damaged artifacts cannot create a mixed release."""
    state, bundles = release_cli
    original = bundles["1"]
    if failure in ("conflict", "missing_manifest", "other_run"):
        state["assets"] = {
            "release-manifest.json": (original / "release-manifest.json").read_bytes()
        }
        if failure == "conflict":
            state["assets"]["open_energy_planner.zip"] = b"another build"
        elif failure == "missing_manifest":
            state["assets"] = {"open_energy_planner.zip": b"unknown provenance"}
        else:
            state["assets"]["release-manifest.json"] = state["assets"][
                "release-manifest.json"
            ].replace(b"42.1", b"41.1")
    elif failure == "checksum":
        (original / "open_energy_planner.zip").write_bytes(b"corrupt download")
    elif failure == "sums":
        (original / "SHA256SUMS").write_bytes(b"incorrect checksums")
    else:
        path = original / "release-manifest.json"
        path.write_bytes(path.read_bytes().replace(b"a" * 40, b"b" * 40))
    before = dict(state["assets"])
    with pytest.raises(ValueError):
        publish_release.publish("v1.2.3", "1001", "42", "1")
    assert not state["uploads"]
    assert state["assets"] == before
