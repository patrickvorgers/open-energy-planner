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
    state = {
        "assets": {},
        "uploads": [],
        "downloads": [],
        "fail_after": None,
        "is_draft": True,
        "is_prerelease": False,
        "app_failure": False,
        "publication_failure": None,
        "events": [],
        "tag": "v1.2.3",
    }

    def output(arguments, **kwargs):
        """Return the checkout revision or currently published asset names."""
        if arguments == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        assert arguments == [
            "gh",
            "release",
            "view",
            state["tag"],
            "--json",
            "assets,isDraft,isPrerelease",
        ]
        return json.dumps(
            {
                "assets": [{"name": name} for name in state["assets"]],
                "isDraft": state["is_draft"],
                "isPrerelease": state["is_prerelease"],
            }
        )

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
        elif arguments[:3] == ["gh", "release", "edit"]:
            assert arguments == ["gh", "release", "edit", state["tag"], "--draft=false"]
            state["events"].append("publish-release")
            if state["publication_failure"] == "before":
                raise subprocess.CalledProcessError(1, arguments)
            if state["publication_failure"] != "ignored":
                state["is_draft"] = False
            if state["publication_failure"] == "after":
                raise subprocess.CalledProcessError(1, arguments)
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
    monkeypatch.setenv("GH_REPO", "example/planner")
    state["app_publications"] = []

    def publish_app(bundle, repository):
        """Record publication only after the complete original bundle has been uploaded."""
        state["app_publications"].append(
            (json.loads((bundle / "release-manifest.json").read_text())["build"], repository)
        )
        assert set(state["assets"]) == {
            "release-manifest.json",
            "SHA256SUMS",
            "open_energy_planner.zip",
            "ha-app-repository.tar.gz",
        }
        state["events"].append("publish-app")
        if state["app_failure"]:
            raise ValueError("App publication failed")

    monkeypatch.setattr(publish_release, "publish_app_repository", publish_app)
    monkeypatch.setattr("script.release.publish_app_repository.publish_app_repository", publish_app)
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
    assert state["app_publications"][-1] == ("42.1", "example/planner")
    assert state["events"].index("publish-app") < state["events"].index("publish-release")
    assert state["events"].count("publish-release") == 1
    assert state["is_draft"] is False


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
    assert not state["app_publications"]


def test_numeric_tag_marked_as_prerelease_never_changes_stable_app_channel(release_cli):
    """GitHub's prerelease flag keeps a numeric-tag preview out of the stable app store."""
    state, _ = release_cli
    state["is_prerelease"] = True
    publish_release.publish("v1.2.3", "1001", "42", "1")
    assert len(state["uploads"]) == 4
    assert not state["app_publications"]
    assert state["is_draft"] is False
    assert state["events"] == ["publish-release"]


def test_app_publication_failure_keeps_integration_release_unpublished(release_cli):
    """An app-channel failure leaves uploaded integration assets in a private draft for retry."""
    state, _ = release_cli
    state["app_failure"] = True
    with pytest.raises(ValueError, match="App publication failed"):
        publish_release.publish("v1.2.3", "1001", "42", "1")
    assert state["is_draft"] is True
    assert state["events"] == ["publish-app"]
    assert len(state["uploads"]) == 4
    state["app_failure"] = False
    publish_release.publish("v1.2.3", "1001", "42", "2")
    assert state["events"] == ["publish-app", "publish-app", "publish-release"]
    assert state["is_draft"] is False
    assert len(state["uploads"]) == 4
    assert state["app_publications"][-1][0] == "42.1"


@pytest.mark.parametrize("failure", ["before", "after"])
def test_final_release_publication_retry_preserves_original_assets(release_cli, failure):
    """API failure or a lost success response resumes without replacing tested bytes."""
    state, _ = release_cli
    state["publication_failure"] = failure
    with pytest.raises(subprocess.CalledProcessError):
        publish_release.publish("v1.2.3", "1001", "42", "1")
    original = dict(state["assets"])
    state["publication_failure"] = None
    publish_release.publish("v1.2.3", "1001", "42", "2")
    assert state["assets"] == original
    assert len(state["uploads"]) == 4
    assert state["is_draft"] is False
    assert state["events"].count("publish-release") == (2 if failure == "before" else 1)


def test_public_release_without_staged_assets_fails_before_writes(release_cli):
    """Premature public releases cannot start the staged distribution procedure."""
    state, _ = release_cli
    state["is_draft"] = False
    with pytest.raises(ValueError, match="draft release"):
        publish_release.publish("v1.2.3", "1001", "42", "1")
    assert not state["downloads"]
    assert not state["uploads"]
    assert not state["events"]


def test_semantic_prerelease_cannot_be_announced_as_a_stable_release(release_cli):
    """A prerelease tag with a stable GitHub flag fails before exposing unmatched channels."""
    state, _ = release_cli
    state["tag"] = "v1.2.3-beta.1"
    with pytest.raises(ValueError, match="GitHub prerelease"):
        publish_release.publish("v1.2.3-beta.1", "1001", "42", "1")
    assert not state["uploads"]
    assert not state["events"]


def test_success_requires_public_release_readback(release_cli):
    """An accepted edit that leaves the release in draft is not reported as successful."""
    state, _ = release_cli
    state["publication_failure"] = "ignored"
    with pytest.raises(ValueError, match="not completed"):
        publish_release.publish("v1.2.3", "1001", "42", "1")
    assert state["is_draft"] is True


def test_release_workflow_requires_explicit_tag_request_after_quality():
    """The publication boundary is a deliberate tagged dispatch with successful full quality."""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/checks.yaml").read_text(
        encoding="utf-8"
    )
    triggers = workflow.split("\npermissions:", 1)[0]
    publication = workflow.split("\n  publish-release:\n", 1)[1]
    # These Actions expressions are the structural authorization contract.
    assert "\n  release:" not in triggers
    assert "workflow_dispatch:" in triggers and "release_tag:" in triggers
    assert "if: github.event_name == 'workflow_dispatch' && inputs.release_tag != ''" in publication
    assert "needs: quality" in publication
    assert (
        "concurrency:\n      group: oep-release-publication\n      cancel-in-progress: false"
        in publication
    )
    assert "OEP_RELEASE_TAG: ${{ inputs.release_tag }}" in publication
    assert 'test "$GITHUB_REF" = "refs/tags/$OEP_RELEASE_TAG"' in workflow
    assert "refs/tags/$OEP_RELEASE_TAG^{commit}" in workflow
