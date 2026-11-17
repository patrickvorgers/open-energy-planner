"""Supervisor publication retains tested bytes, handles retries and rejects stale releases."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from script.release import package_release
from script.release import publish_app_repository as publisher


@pytest.fixture
def bundle(tmp_path, request):
    """Create an independently specified tested release archive with a binary brand file."""
    version = getattr(request, "param", "1.2.3")
    identity = {
        "version": version,
        "build": "42.1",
        "revision": "a" * 40,
        "release_tag": "v" + version,
    }
    files = {
        "repository.json": b'{"name":"Example planner"}',
        "open-energy-planner/config.yaml": json.dumps(
            {"version": version, "arch": ["amd64"]}
        ).encode(),
        "open-energy-planner/Dockerfile": b"FROM example\nCOPY src ./src\n",
        "open-energy-planner/icon.png": b"\x89PNG\r\n\x1a\nexample",
        "open-energy-planner/src/open_energy_planner/build-info.json": json.dumps(
            identity
        ).encode(),
    }
    archive_path = tmp_path / "ha-app-repository.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, contents in files.items():
            member = tarfile.TarInfo("ha-app-repository/" + name)
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
    manifest = {
        **identity,
        "source_state": "commit",
        "artifacts": {archive_path.name: hashlib.sha256(archive_path.read_bytes()).hexdigest()},
    }
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, files


@pytest.fixture
def github(monkeypatch):
    """Apply the documented GraphQL file-change contract to an in-memory remote branch."""
    state = {
        "head": None,
        "files": {
            "README.md": b"source checkout",
            ".github/workflows/checks.yaml": b"original workflow",
            "ha_app/config.yaml": b'{"version":"development"}',
        },
        "commits": [],
        "verified": True,
        "concurrent": False,
    }

    def api(endpoint, payload=None):
        """Emulate immutable reads and guarded publication without touching GitHub."""
        if endpoint == "graphql":
            query = payload["query"]
            if query.startswith("query"):
                assert payload["variables"] == {
                    "owner": "example",
                    "name": "planner",
                    "ref": "refs/heads/ha-app-store",
                }
                ref = None if state["head"] is None else {"target": {"oid": state["head"]}}
                return {"data": {"repository": {"id": "repository-id", "ref": ref}}}
            arguments = payload["variables"]["input"]
            if "createRef" in query:
                assert arguments == {
                    "repositoryId": "repository-id",
                    "name": "refs/heads/ha-app-store",
                    "oid": "a" * 40,
                }
                state["head"] = arguments["oid"]
                return {"data": {"createRef": {"ref": {"name": "ha-app-store"}}}}
            assert "createCommitOnBranch" in query
            assert arguments["expectedHeadOid"] == state["head"]
            assert arguments["branch"] == {
                "repositoryNameWithOwner": "example/planner",
                "branchName": "ha-app-store",
            }
            if state["concurrent"]:
                raise ValueError("Head changed")
            for deletion in arguments["fileChanges"]["deletions"]:
                del state["files"][deletion["path"]]
            for addition in arguments["fileChanges"]["additions"]:
                state["files"][addition["path"]] = base64.b64decode(addition["contents"])
            state["head"] = "b" * 40
            state["commits"].append(arguments)
            return {"data": {"createCommitOnBranch": {"commit": {"oid": state["head"]}}}}
        if "/git/trees/" in endpoint:
            assert state["head"] in endpoint
            return {
                "truncated": state.get("truncated", False),
                "tree": [
                    {"path": name, "type": "blob", "sha": publisher.blob_hash(contents)}
                    for name, contents in state["files"].items()
                ],
            }
        if "/git/blobs/" in endpoint:
            return {"content": base64.b64encode(state["files"]["release-manifest.json"]).decode()}
        assert endpoint == f"repos/example/planner/commits/{state['head']}"
        return {"commit": {"verification": {"verified": state["verified"]}}}

    monkeypatch.setattr(publisher, "github_api", api)
    return state


def test_first_publication_and_retry_preserve_original_files(bundle, github):
    """Publish exact app bytes, disable the source template and preserve the immutable scaffold."""
    folder, files = bundle
    publisher.publish_app_repository(folder, "example/planner")
    assert github["files"] == {
        **files,
        "release-manifest.json": (folder / "release-manifest.json").read_bytes(),
        "README.md": b"source checkout",
        ".github/workflows/checks.yaml": b"original workflow",
    }
    publisher.publish_app_repository(folder, "example/planner")
    assert len(github["commits"]) == 1


def test_newer_release_replaces_and_deletes_previous_files(bundle, github):
    """A newer release advances the channel and removes obsolete build inputs."""
    folder, files = bundle
    github["head"] = "c" * 40
    github["files"]["release-manifest.json"] = b'{"release_tag":"v1.2.2"}'
    github["files"]["open-energy-planner/removed.py"] = b"obsolete app input"
    publisher.publish_app_repository(folder, "example/planner")
    assert set(github["files"]) == {
        *files,
        "release-manifest.json",
        "README.md",
        ".github/workflows/checks.yaml",
    }
    assert github["commits"][0]["expectedHeadOid"] == "c" * 40


def test_real_packaged_context_is_the_only_installable_app(tmp_path, monkeypatch, github):
    """Publishing the actual bundle preserves its build context and exposes exactly one app."""

    def git_output(arguments, **kwargs):
        """Supply a clean tagged source identity while packaging the real repository inputs."""
        return "a" * 40 if arguments[:2] == ["git", "rev-parse"] else ""

    monkeypatch.setattr(package_release.subprocess, "check_output", git_output)
    folder = tmp_path / "bundle"
    package_release.build(folder, build_number="42.1", release_tag="v1.2.3")
    publisher.publish_app_repository(folder, "example/planner")
    files = github["files"]
    manifests = {
        name
        for name in files
        if len(name.split("/")) == 2 and name.rsplit("/", 1)[1] == "config.yaml"
    }
    assert manifests == {"open-energy-planner/config.yaml"}
    app = json.loads(files["open-energy-planner/config.yaml"])
    assert app["version"] == "1.2.3"
    assert app["homeassistant"] == "2026.11.0"
    assert app["arch"] == ["amd64"]
    for context_input in ("requirements.lock", "script/ha_app.py", "LICENSE", "NOTICE"):
        assert files["open-energy-planner/" + context_input]
    assert (
        files["open-energy-planner/src/open_energy_planner/app.py"]
        == (package_release.ROOT / "src/open_energy_planner/app.py").read_bytes()
    )
    assert (
        files["open-energy-planner/icon.png"]
        == (
            package_release.ROOT / "custom_components/open_energy_planner/brand/icon.png"
        ).read_bytes()
    )


@pytest.mark.parametrize("previous", ["v1.2.3", "v1.2.4", "v2.0.0", "v1.3.0-beta.1"])
def test_conflicting_or_older_release_cannot_replace_channel(bundle, github, previous):
    """A same-version conflict, downgrade or invalid stable channel fails before a commit."""
    github["head"] = "c" * 40
    github["files"]["release-manifest.json"] = json.dumps({"release_tag": previous}).encode()
    with pytest.raises(ValueError, match="replace or downgrade"):
        publisher.publish_app_repository(bundle[0], "example/planner")
    assert not github["commits"]


@pytest.mark.parametrize("failure", ["concurrent", "truncated", "unrecognized", "unsigned"])
def test_remote_failures_are_not_successful_publication(bundle, github, failure):
    """Races, incomplete reads, foreign branches and unverified commits fail explicitly."""
    if failure == "concurrent":
        github["concurrent"] = True
    elif failure == "truncated":
        github["truncated"] = True
    elif failure == "unrecognized":
        github["head"] = "c" * 40
    else:
        github["verified"] = False
    with pytest.raises(ValueError):
        publisher.publish_app_repository(bundle[0], "example/planner")
    if failure != "unsigned":
        assert not github["commits"]
    else:
        with pytest.raises(ValueError, match="Verified"):
            publisher.publish_app_repository(bundle[0], "example/planner")
        assert len(github["commits"]) == 1


def test_corrupted_bundle_cannot_create_branch(bundle, github):
    """Checksum failure is detected before any public branch is created."""
    (bundle[0] / "ha-app-repository.tar.gz").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        publisher.publish_app_repository(bundle[0], "example/planner")
    assert github["head"] is None


@pytest.mark.parametrize(
    "field,value", [("source_state", "unverified"), ("version", "1.2.4"), ("build", "42.2")]
)
def test_unmatched_or_unverified_identity_prevents_publication(bundle, github, field, value):
    """Untested sources and mismatched app/manifest identities fail before a public write."""
    manifest_path = bundle[0] / "release-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        publisher.publish_app_repository(bundle[0], "example/planner")
    assert github["head"] is None


@pytest.mark.parametrize("bundle", ["1.3.0-beta.1"], indirect=True)
def test_prerelease_does_not_advance_stable_channel(bundle, github):
    """A tested preview release stays downloadable without changing the stable app repository."""
    publisher.publish_app_repository(bundle[0], "example/planner")
    assert github["head"] is None
    assert not github["commits"]


def test_marketplace_and_app_minimum_version_and_release_asset_contract():
    """Both installers target HA 2026.11 and Marketplace uses the matched integration ZIP."""
    root = Path(__file__).resolve().parents[2]
    marketplace = json.loads((root / "hacs.json").read_bytes())
    app = json.loads((root / "ha_app/config.yaml").read_bytes())
    integration = json.loads(
        (root / "custom_components/open_energy_planner/manifest.json").read_bytes()
    )
    assert marketplace["homeassistant"] == app["homeassistant"] == "2026.11.0"
    assert marketplace["zip_release"] is True
    assert marketplace["hide_default_branch"] is True
    assert marketplace["filename"] == "open_energy_planner.zip"
    assert integration["domain"] == "open_energy_planner"
    assert integration["config_flow"] is True


@pytest.mark.parametrize(
    "unsafe", ["../escape", "/absolute", "other/file", "ha-app-repository/link"]
)
def test_archive_paths_and_links_are_rejected_before_publication(bundle, github, unsafe):
    """Traversal, foreign roots and symbolic links cannot become app repository files."""
    folder, _ = bundle
    archive_path = folder / "ha-app-repository.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo(unsafe)
        member.type = tarfile.SYMTYPE if unsafe.endswith("/link") else tarfile.REGTYPE
        member.linkname = "outside"
        archive.addfile(member, io.BytesIO())
    manifest_path = folder / "release-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"][archive_path.name] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        publisher.publish_app_repository(folder, "example/planner")
    assert github["head"] is None


def test_graphql_errors_fail_even_with_a_successful_http_response(monkeypatch):
    """GraphQL application errors never count as successful API operations."""
    monkeypatch.setattr(
        publisher.subprocess,
        "check_output",
        lambda *args, **kwargs: '{"errors":[{"message":"failed"}]}',
    )
    with pytest.raises(ValueError, match="GitHub rejected"):
        publisher.github_api("graphql", {"query": "mutation"})
