"""Publish the original tested app bundle as a Supervisor-readable Git branch."""

import base64
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from script.release.package_release import version_from_tag

APP_STORE_BRANCH = "ha-app-store"
APP_DIRECTORY = "open-energy-planner/"
DISABLED_SOURCE_APP_CONFIG = "ha_app/config.yaml"


def github_api(endpoint, payload=None):
    """Read GitHub or submit JSON without shell interpolation or credential output."""
    arguments = ["gh", "api", endpoint]
    if payload is not None:
        arguments += ["--input", "-"]
    result = json.loads(
        subprocess.check_output(
            arguments,
            input=None if payload is None else json.dumps(payload),
            text=True,
        )
    )
    if isinstance(result, dict) and result.get("errors"):
        raise ValueError("GitHub rejected app repository publication")
    return result


def bundle_files(bundle):
    """Decode only regular bundle files, preserving tested bytes and release identity."""
    bundle = Path(bundle)
    manifest_bytes = (bundle / "release-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    version = version_from_tag(manifest["release_tag"])
    if manifest["source_state"] != "commit" or manifest["version"] != version:
        raise ValueError("App repository requires a tested tagged release")
    archive_path = bundle / "ha-app-repository.tar.gz"
    if (
        hashlib.sha256(archive_path.read_bytes()).hexdigest()
        != manifest["artifacts"][archive_path.name]
    ):
        raise ValueError("App repository archive checksum mismatch")
    files = {"release-manifest.json": manifest_bytes}
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or not path.parts
                or path.parts[0] != "ha-app-repository"
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("App repository archive contains an unsafe path or file type")
            if member.isfile():
                relative = path.relative_to("ha-app-repository").as_posix()
                if relative in files:
                    raise ValueError("App repository archive contains duplicate files")
                stream = archive.extractfile(member)
                files[relative] = stream.read()
    app_identity = json.loads(files["open-energy-planner/src/open_energy_planner/build-info.json"])
    if (
        app_identity
        != {field: manifest[field] for field in ("version", "build", "revision", "release_tag")}
        or json.loads(files["open-energy-planner/config.yaml"])["version"] != version
    ):
        raise ValueError("App repository identity differs from its release manifest")
    if "repository.json" not in files or "open-energy-planner/Dockerfile" not in files:
        raise ValueError("App repository lacks its Supervisor build context")
    return manifest, files


def blob_hash(contents):
    """Compute Git's blob identity for byte-exact publication and retry verification."""
    return hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest()


def repository_tree(repository, revision):
    """Read every file identity at an immutable revision, rejecting truncated results."""
    result = github_api(f"repos/{repository}/git/trees/{revision}?recursive=1")
    if result.get("truncated"):
        raise ValueError("GitHub returned an incomplete app repository tree")
    return {item["path"]: item["sha"] for item in result["tree"] if item["type"] != "tree"}


def verify_publication(repository, revision, expected):
    """Require a signed commit and the exact intended remote files, including on retries."""
    commit = github_api(f"repos/{repository}/commits/{revision}")
    if not commit["commit"]["verification"]["verified"]:
        raise ValueError("Published app repository commit is not GitHub Verified")
    if repository_tree(repository, revision) != expected:
        raise ValueError("Published app repository differs from the tested bundle")


def publish_app_repository(bundle, repository):
    """Advance the stable app channel atomically, never replacing an existing release.

    GitHub creates and signs the generated commit through createCommitOnBranch.
    The expected head protects concurrent publishers. Pre-releases retain their
    downloadable assets but cannot replace the stable Supervisor channel.
    """
    manifest, files = bundle_files(bundle)
    if "-" in manifest["version"]:
        return
    owner, name = repository.split("/")
    result = github_api(
        "graphql",
        {
            "query": """query($owner: String!, $name: String!, $ref: String!) {
              repository(owner: $owner, name: $name) {
                id ref(qualifiedName: $ref) { target { oid } }
              }
            }""",
            "variables": {"owner": owner, "name": name, "ref": f"refs/heads/{APP_STORE_BRANCH}"},
        },
    )["data"]["repository"]
    if result["ref"] is None:
        github_api(
            "graphql",
            {
                "query": """mutation($input: CreateRefInput!) {
                  createRef(input: $input) { ref { name } }
                }""",
                "variables": {
                    "input": {
                        "repositoryId": result["id"],
                        "name": f"refs/heads/{APP_STORE_BRANCH}",
                        "oid": manifest["revision"],
                    }
                },
            },
        )
        head = manifest["revision"]
    else:
        head = result["ref"]["target"]["oid"]
    previous = repository_tree(repository, head)
    # Keep the initial source scaffold, including workflows that this token must
    # never edit. Only the generated app directory is built by Supervisor. Disable
    # the source template so it cannot appear as a second installable app.
    scaffold = {
        path: identity
        for path, identity in previous.items()
        if not path.startswith(APP_DIRECTORY) and path not in {*files, DISABLED_SOURCE_APP_CONFIG}
    }
    expected = {**scaffold, **{path: blob_hash(contents) for path, contents in files.items()}}
    if previous == expected:
        verify_publication(repository, head, expected)
        return
    if "release-manifest.json" in previous:
        previous_blob = github_api(
            f"repos/{repository}/git/blobs/{previous['release-manifest.json']}"
        )
        previous_manifest = json.loads(base64.b64decode(previous_blob["content"]))
        previous_version = version_from_tag(previous_manifest["release_tag"])
        if "-" in previous_version or tuple(map(int, previous_version.split("."))) >= tuple(
            map(int, manifest["version"].split("."))
        ):
            raise ValueError("App repository cannot replace or downgrade a published version")
    elif head != manifest["revision"]:
        raise ValueError("App repository branch has no recognized publication identity")
    result = github_api(
        "graphql",
        {
            "query": """mutation($input: CreateCommitOnBranchInput!) {
              createCommitOnBranch(input: $input) { commit { oid } }
            }""",
            "variables": {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": repository,
                        "branchName": APP_STORE_BRANCH,
                    },
                    "expectedHeadOid": head,
                    "message": {"headline": f"Publish tested app {manifest['release_tag']}"},
                    "fileChanges": {
                        "additions": [
                            {"path": path, "contents": base64.b64encode(contents).decode()}
                            for path, contents in sorted(files.items())
                            if previous.get(path) != expected[path]
                        ],
                        "deletions": [
                            {"path": path} for path in sorted(previous.keys() - expected.keys())
                        ],
                    },
                }
            },
        },
    )
    published = result["data"]["createCommitOnBranch"]["commit"]["oid"]
    verify_publication(repository, published, expected)
