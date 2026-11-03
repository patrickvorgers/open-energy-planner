"""Stage only original successful CI artifacts whose source has been merged onto main."""

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from script.ci.check_commit_signatures import GitHub, VerificationError, sha
from script.ci.check_main_ci_reuse import (
    API_PAGE_SIZE,
    BUNDLE_ARTIFACT,
    FULL_SUITE_STEP,
    QUALITY_JOB,
    TREE_STEP_PREFIX,
    WORKFLOW_PATH,
    positive_id,
    require,
    timestamp,
)

REPOSITORY = "patrickvorgers/open-energy-planner"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
IDENTITY_FIELDS = ("version", "build", "revision", "release_tag")
CONTROLLER_DIRECTORIES = ("script", "test_environment", "src", ".devcontainer")
PACKAGE_FILES = {
    "release-manifest.json",
    "SHA256SUMS",
    "open_energy_planner.zip",
    "ha-app-repository.tar.gz",
}


def github_client(root):
    """Reuse existing read authority without putting a credential in arguments or saved files."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        token = values.get("password") if result.returncode == 0 else None
    return GitHub(token)


def complete_rows(document, key):
    """Reject truncated or ambiguous Actions evidence instead of selecting a partial page."""
    rows = document.get(key)
    require(
        isinstance(rows, list)
        and type(document.get("total_count")) is int
        and document["total_count"] == len(rows) <= API_PAGE_SIZE,
        "Incomplete CI evidence",
    )
    return rows


def evidence(client, run_id, archive_digest):
    """Bind a successful original run and artifact to a verified merge that remains on main."""
    root = f"repos/{REPOSITORY}"
    run_id = positive_id(run_id)
    run = client.get(f"{root}/actions/runs/{run_id}")
    workflow = client.get(f"{root}/actions/workflows/checks.yaml")
    head = sha(run.get("head_sha"))
    attempt = positive_id(run.get("run_attempt"))
    number = positive_id(run.get("run_number"))
    require(
        run.get("id") == run_id
        and run.get("workflow_id") == positive_id(workflow.get("id"))
        and workflow.get("path") == WORKFLOW_PATH
        and run.get("path") == WORKFLOW_PATH
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("event") in {"push", "pull_request"}
        and all(
            run.get(key, {}).get("full_name") == REPOSITORY
            for key in ("repository", "head_repository")
        ),
        "CI run is not a successful original Checks run",
    )
    associated = client.get_list(f"{root}/commits/{head}/pulls?per_page={API_PAGE_SIZE}")
    require(len(associated) < API_PAGE_SIZE, "Incomplete merged PR evidence")
    candidates = [
        pull
        for pull in associated
        if pull.get("base", {}).get("ref") == "main"
        and (
            pull.get("head", {}).get("sha") == head
            if run["event"] == "pull_request"
            else pull.get("merge_commit_sha") == head
        )
    ]
    require(len(candidates) == 1, "CI source has no unique merged PR")
    pull = client.get(f"{root}/pulls/{positive_id(candidates[0].get('number'))}")
    require(
        pull.get("merged") is True
        and pull.get("base", {}).get("ref") == "main"
        and all(
            pull.get(key, {}).get("repo", {}).get("full_name") == REPOSITORY
            for key in ("base", "head")
        )
        and (
            pull.get("head", {}).get("sha") == head
            if run["event"] == "pull_request"
            else pull.get("merge_commit_sha") == head
        ),
        "CI source PR has not been merged onto main",
    )
    merged_sha = sha(pull.get("merge_commit_sha"))
    merged = client.get(f"{root}/git/commits/{merged_sha}")
    require(
        merged.get("sha") == merged_sha
        and merged.get("verification", {}).get("verified") is True
        and merged.get("verification", {}).get("reason") == "valid",
        "Merged source is not GitHub Verified",
    )
    tree = sha(merged.get("tree", {}).get("sha"))
    ancestry = client.get(f"{root}/compare/{merged_sha}...main")
    require(
        ancestry.get("status") in {"ahead", "identical"}
        and ancestry.get("merge_base_commit", {}).get("sha") == merged_sha,
        "Source merge is no longer on main",
    )
    jobs = complete_rows(
        client.get(
            f"{root}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page={API_PAGE_SIZE}"
        ),
        "jobs",
    )
    quality = [job for job in jobs if job.get("name") == QUALITY_JOB]
    require(len(quality) == 1, "Missing original quality job")
    quality = quality[0]
    steps = quality.get("steps", [])

    def succeeded(name):
        """Require a unique completed successful step rather than a name or aggregate result."""
        matching = [step for step in steps if step.get("name") == name]
        return (
            len(matching) == 1
            and matching[0].get("status") == "completed"
            and matching[0].get("conclusion") == "success"
        )

    full_suite = succeeded(FULL_SUITE_STEP)
    selected_suite = succeeded("Selected native and integration suites")
    tree_steps = [step for step in steps if step.get("name") == TREE_STEP_PREFIX + tree]
    require(
        quality.get("status") == "completed"
        and quality.get("conclusion") == "success"
        and quality.get("run_id") == run_id
        and quality.get("run_attempt") == attempt
        and quality.get("head_sha") == head
        and full_suite != selected_suite
        and all(
            succeeded(name)
            for name in (
                "Capture the checked-out source tree",
                "Build matched private installation bundle",
                "Preserve matched installation artifacts",
            )
        )
        and len(tree_steps) == 1
        and tree_steps[0].get("status") == "completed"
        # Focused CI intentionally skips the full-only marker after validating its committed package.
        and tree_steps[0].get("conclusion")
        in ({"success"} if full_suite else {"success", "skipped"}),
        "Actual selected suites, packaged source or merged tested tree did not pass this attempt",
    )
    if run["event"] == "pull_request":
        admission = [job for job in jobs if job.get("name") == "Candidate and pre-build checks"]
        require(
            len(admission) == 1
            and admission[0].get("status") == "completed"
            and admission[0].get("conclusion") == "success"
            and admission[0].get("run_id") == run_id
            and admission[0].get("run_attempt") == attempt
            and admission[0].get("head_sha") == head,
            "Candidate admission did not pass",
        )
    artifacts = complete_rows(
        client.get(f"{root}/actions/runs/{run_id}/artifacts?per_page={API_PAGE_SIZE}"), "artifacts"
    )
    bundles = [item for item in artifacts if item.get("name") == BUNDLE_ARTIFACT]
    require(len(bundles) == 1, "Missing original installation bundle")
    artifact = bundles[0]
    require(
        artifact.get("expired") is False
        and timestamp(artifact.get("expires_at")) > datetime.now(UTC)
        and timestamp(quality.get("started_at"))
        <= timestamp(artifact.get("created_at"))
        <= timestamp(quality.get("completed_at"))
        and artifact.get("workflow_run", {}).get("id") == run_id
        and artifact.get("workflow_run", {}).get("head_sha") == head
        and artifact.get("digest") == "sha256:" + archive_digest,
        "Bundle differs from the original unexpired CI artifact",
    )
    refreshed = client.get(f"{root}/actions/runs/{run_id}")
    require(
        all(
            refreshed.get(field) == run.get(field)
            for field in (
                "id",
                "workflow_id",
                "head_sha",
                "event",
                "path",
                "status",
                "conclusion",
                "run_attempt",
                "run_number",
            )
        ),
        "CI run changed during verification",
    )
    return {
        "run_id": run_id,
        "artifact_id": positive_id(artifact.get("id")),
        "build": f"{number}.{attempt}",
        "merge": merged_sha,
        "tree": tree,
        "digest": archive_digest,
    }


def archive_files(data, *, tar=False):
    """Read bounded regular files only, rejecting traversal, links and duplicate destinations."""
    require(len(data) <= MAX_ARCHIVE_BYTES, "Archive exceeds compressed size limit")
    files = {}
    destinations = set()
    total = 0
    with (
        tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        if tar
        else zipfile.ZipFile(io.BytesIO(data))
    ) as archive:
        members = archive.getmembers() if tar else archive.infolist()
        require(len(members) <= MAX_ARCHIVE_FILES, "Too many archive members")
        for member in members:
            name = member.name if tar else member.filename
            path = PurePosixPath(name)
            require(
                name
                and "\\" not in name
                and not path.is_absolute()
                and path.as_posix() == name.rstrip("/")
                and not any(
                    part in {".", ".."}
                    or ":" in part
                    or part != part.rstrip(" .")
                    or any(ord(character) < 32 for character in part)
                    or part.split(".")[0].upper()
                    in {
                        "CON",
                        "PRN",
                        "AUX",
                        "NUL",
                        *(f"COM{number}" for number in range(1, 10)),
                        *(f"LPT{number}" for number in range(1, 10)),
                    }
                    for part in path.parts
                ),
                "Unsafe archive path",
            )
            if member.isdir() if tar else member.is_dir():
                continue
            require(
                member.isfile()
                if tar
                else (member.external_attr >> 16) & 0o170000 in {0, 0o100000},
                "Archive links or special files are not allowed",
            )
            require(path.as_posix().casefold() not in destinations, "Duplicate archive file")
            destinations.add(path.as_posix().casefold())
            total += member.size if tar else member.file_size
            require(total <= MAX_UNPACKED_BYTES, "Archive exceeds unpacked size limit")
            files[path.as_posix()] = (
                archive.extractfile(member).read() if tar else archive.read(member)
            )
    require(
        not any(
            parent.as_posix().casefold() in destinations
            for name in files
            for parent in PurePosixPath(name).parents
        ),
        "Archive file conflicts with directory",
    )
    return files


def unpack(data):
    """Validate paired component identities and all payload hashes before staging any file."""
    package = archive_files(data)
    require(set(package) == PACKAGE_FILES, "Unexpected installation package contents")
    manifest = json.loads(package["release-manifest.json"])
    require(
        isinstance(manifest, dict) and all(field in manifest for field in IDENTITY_FIELDS),
        "Incomplete package identity",
    )
    require(
        isinstance(manifest["version"], str)
        and bool(manifest["version"])
        and (
            manifest["release_tag"] is None
            or isinstance(manifest["release_tag"], str)
            and bool(manifest["release_tag"])
        ),
        "Invalid package version or release tag",
    )
    require(
        manifest.get("source_state") == "commit"
        and re.fullmatch(r"[1-9][0-9]*\.[1-9][0-9]*", manifest.get("build") or ""),
        "A numbered committed-source CI bundle is required",
    )
    sha(manifest.get("revision"))
    payloads = {"open_energy_planner.zip", "ha-app-repository.tar.gz"}
    require(set(manifest.get("artifacts", {})) == payloads, "Incomplete package checksums")
    require(
        all(
            hashlib.sha256(package[name]).hexdigest() == manifest["artifacts"][name]
            for name in payloads
        ),
        "CI payload checksum mismatch",
    )
    require(
        package["SHA256SUMS"].decode("utf-8")
        == "".join(f"{digest}  {name}\n" for name, digest in manifest["artifacts"].items()),
        "Package checksum list differs from manifest",
    )
    integration = archive_files(package["open_energy_planner.zip"])
    application = archive_files(package["ha-app-repository.tar.gz"], tar=True)
    prefix = "ha-app-repository/open-energy-planner/"
    require(
        all(
            name.startswith(prefix) or name == "ha-app-repository/repository.json"
            for name in application
        ),
        "Unexpected app repository layout",
    )
    application = {
        name[len(prefix) :]: value for name, value in application.items() if name.startswith(prefix)
    }
    expected = {field: manifest[field] for field in IDENTITY_FIELDS}
    require(
        all(
            json.loads(component[name]) == expected
            for component, name in (
                (application, "src/open_energy_planner/build-info.json"),
                (integration, "build-info.json"),
            )
        ),
        "App/integration build identities differ",
    )
    require(
        json.loads(application["config.yaml"])["version"] == expected["version"]
        and json.loads(integration["manifest.json"])["version"] == expected["version"],
        "Component versions differ from bundle",
    )
    return manifest, {"app/" + name: value for name, value in application.items()} | {
        "integration/" + name: value for name, value in integration.items()
    }


def source_hashes(root):
    """Pin the clean merged controller and simulator checkout without copying local credentials."""
    require(
        not subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip(),
        "Use a clean checkout of the selected merged commit",
    )
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
    require(
        all(not (root / name).is_symlink() for name in names if name),
        "Source links are not supported",
    )
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names if name}


def require_no_links(path):
    """Reject symlink and Windows junction ancestors before reading or replacing cached sources."""
    require(
        not any(
            item.is_symlink() or getattr(item, "is_junction", lambda: False)()
            for item in (path, *path.parents)
        ),
        "Development staging cannot contain source links",
    )


def safe_directory(root):
    """Confine cache swaps and cleanup to this checkout's dedicated CI bundle directory."""
    directory = root / ".local/dev"
    require(
        directory.resolve().is_relative_to(root.resolve()),
        "Development staging escapes the checkout",
    )
    require_no_links(directory / "bundle")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def update_lock(root):
    """Reject concurrent staging or provisioning from the same checkout on Windows and Linux."""
    directory = safe_directory(Path(root))
    target = directory / "update.lock"
    require_no_links(target)
    with target.open("a+b") as lock:
        if not target.stat().st_size:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def prepare(root, bundle, run_id, client):
    """Verify a downloaded Actions ZIP online, then atomically replace only the code staging cache."""
    root, bundle = Path(root), Path(bundle)
    require(bundle.stat().st_size <= MAX_ARCHIVE_BYTES, "Installation bundle exceeds size limit")
    data = bundle.read_bytes()
    manifest, files = unpack(data)
    receipt = evidence(client, run_id, hashlib.sha256(data).hexdigest())
    require(manifest["build"] == receipt["build"], "Bundle belongs to a different run attempt")
    commit = client.get(f"repos/{REPOSITORY}/git/commits/{manifest['revision']}")
    require(
        commit.get("sha") == manifest["revision"]
        and commit.get("tree", {}).get("sha") == receipt["tree"],
        "Bundle source differs from the merged tested tree",
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    require(
        head == receipt["merge"], "Check out the selected verified main merge before updating dev"
    )
    receipt["source_files"] = source_hashes(root)
    directory = safe_directory(root)
    destination = directory / "bundle"
    with tempfile.TemporaryDirectory(prefix="ci-bundle-", dir=directory) as temporary:
        prepared = Path(temporary) / "prepared"
        prepared.mkdir()
        for name, value in files.items():
            target = prepared / name
            require(target.resolve().is_relative_to(prepared.resolve()), "Archive escapes staging")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        (prepared / "archive.zip").write_bytes(data)
        (prepared / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        validate_cache(root, prepared)
        backup = Path(temporary) / "previous"
        if destination.exists():
            require(
                destination.resolve().is_relative_to(directory.resolve()),
                "Unsafe prior bundle path",
            )
            destination.rename(backup)
        try:
            prepared.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
    return load(root)


def load(root):
    """Reuse an accepted installation offline while rejecting source overlays or altered staging files."""
    root = Path(root)
    return validate_cache(root, root / ".local/dev/bundle")


def validate_cache(root, directory):
    """Validate staged and published caches identically, before replacing any accepted package."""
    require_no_links(directory)
    require(
        directory.resolve().is_relative_to(root.resolve()) and not directory.is_symlink(),
        "Unsafe CI bundle cache",
    )
    if not (directory / "receipt.json").is_file():
        raise VerificationError(
            "Prepare a verified CI package with --bundle and --run-id before starting oep-dev"
        )
    require(
        not any(
            path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            for path in directory.rglob("*")
        ),
        "Cached source links are not allowed",
    )
    receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    require(
        (directory / "archive.zip").stat().st_size <= MAX_ARCHIVE_BYTES,
        "Cached archive exceeds size limit",
    )
    data = (directory / "archive.zip").read_bytes()
    require(hashlib.sha256(data).hexdigest() == receipt["digest"], "Cached CI archive changed")
    manifest, files = unpack(data)
    require(manifest["build"] == receipt["build"], "Cached CI identity changed")
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
        and path.relative_to(directory).as_posix() not in {"archive.zip", "receipt.json"}
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    require(actual == set(files), "CI source files were added or removed")
    require(
        all(
            not (directory / name).is_symlink() and (directory / name).read_bytes() == value
            for name, value in files.items()
        ),
        "CI source files changed",
    )
    require(
        isinstance(receipt.get("source_files"), dict) and bool(receipt["source_files"]),
        "Missing verified controller source receipt",
    )
    for name in CONTROLLER_DIRECTORIES:
        expected = {path for path in receipt["source_files"] if path.startswith(name + "/")}
        actual = {
            path.relative_to(root).as_posix()
            for path in (root / name).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        require(actual == expected, "Controller or simulator source files were added or removed")
    require(
        all(
            (root / name).resolve().is_relative_to(root.resolve())
            and (root / name).is_file()
            and not (root / name).is_symlink()
            and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            for name, digest in receipt["source_files"].items()
        ),
        "Checkout differs from the verified merged controller and simulator",
    )
    return manifest, directory
