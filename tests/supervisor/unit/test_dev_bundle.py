"""Persistent development accepts only intact packages from successful merged-source CI."""

import copy
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from script.ci.check_commit_signatures import VerificationError
from script.supervisor import dev, dev_bundle, provision_supervisor

HEAD, MERGE, TREE, REVISION = (letter * 40 for letter in "abcd")


def zipped(files):
    """Create independent archive inputs without invoking the production packager."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return output.getvalue()


def package(*, version="0.0.0.dev0", release_tag=None):
    """Build a small matched CI pair with a distinct synthetic tested commit and actual merge."""
    identity = {
        "version": version,
        "build": "733.1",
        "revision": REVISION,
        "release_tag": release_tag,
    }
    integration = zipped(
        {
            "build-info.json": json.dumps(identity),
            "manifest.json": json.dumps({"version": version}),
            "__init__.py": '"""Integration source."""\n',
        }
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, value in {
            "ha-app-repository/repository.json": "{}",
            "ha-app-repository/open-energy-planner/Dockerfile": "FROM scratch\n",
            "ha-app-repository/open-energy-planner/config.yaml": json.dumps({"version": version}),
            "ha-app-repository/open-energy-planner/src/open_energy_planner/build-info.json": json.dumps(
                identity
            ),
        }.items():
            payload = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    payloads = {
        "open_energy_planner.zip": integration,
        "ha-app-repository.tar.gz": output.getvalue(),
    }
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}
    manifest = {**identity, "source_state": "commit", "artifacts": hashes}
    return zipped(
        {
            **payloads,
            "release-manifest.json": json.dumps(manifest),
            "SHA256SUMS": "".join(f"{digest}  {name}\n" for name, digest in hashes.items()),
        }
    )


@pytest.fixture
def evidence():
    """Supply independently specified GitHub run, full-suite, artifact and merged-tree evidence."""
    repository = {"full_name": "patrickvorgers/open-energy-planner"}
    run = {
        "id": 40969348570,
        "workflow_id": 40,
        "head_sha": HEAD,
        "run_attempt": 1,
        "run_number": 733,
        "path": ".github/workflows/checks.yaml",
        "status": "completed",
        "conclusion": "success",
        "event": "pull_request",
        "repository": repository,
        "head_repository": repository,
    }
    job = {
        "run_id": run["id"],
        "run_attempt": 1,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
    }
    pull = {
        "number": 121,
        "merged": True,
        "merge_commit_sha": MERGE,
        "base": {"ref": "main", "repo": repository},
        "head": {"sha": HEAD, "repo": repository},
    }
    root = "repos/patrickvorgers/open-energy-planner"
    documents = {
        f"{root}/actions/runs/40969348570": run,
        f"{root}/actions/workflows/checks.yaml": {
            "id": 40,
            "path": ".github/workflows/checks.yaml",
        },
        f"{root}/commits/{HEAD}/pulls?per_page=100": [pull],
        f"{root}/pulls/121": pull,
        f"{root}/git/commits/{MERGE}": {
            "sha": MERGE,
            "verification": {"verified": True, "reason": "valid"},
            "tree": {"sha": TREE},
        },
        f"{root}/git/commits/{REVISION}": {"sha": REVISION, "tree": {"sha": TREE}},
        f"{root}/compare/{MERGE}...main": {"status": "ahead", "merge_base_commit": {"sha": MERGE}},
        f"{root}/actions/runs/40969348570/attempts/1/jobs?per_page=100": {
            "total_count": 2,
            "jobs": [
                {
                    **job,
                    "name": "Quality and integration (AMD64)",
                    "started_at": "2026-10-29T12:00:00Z",
                    "completed_at": "2026-10-29T13:00:00Z",
                    "steps": [
                        {
                            "name": "Capture the checked-out source tree",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "Build matched private installation bundle",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "Preserve matched installation artifacts",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "Native checks, planner scenarios, app smoke and real HA handoff",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "Full CI source tree: " + TREE,
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ],
                },
                {**job, "name": "Candidate and pre-build checks"},
            ],
        },
        f"{root}/actions/runs/40969348570/artifacts?per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 1234,
                    "name": "installation-bundle-amd64",
                    "expired": False,
                    "expires_at": "2099-10-30T13:00:00Z",
                    "created_at": "2026-10-29T12:59:00Z",
                    "workflow_run": {"id": run["id"], "head_sha": HEAD},
                    "digest": "sha256:" + "e" * 64,
                }
            ],
        },
    }

    class GitHub:
        """Read independent API documents while allowing tests to inject inconsistent evidence."""

        def get(self, path):
            """Return a copy so the verifier cannot accidentally repair an input document."""
            return copy.deepcopy(documents[path])

        get_list = get

    client = GitHub()
    client.documents = documents
    return client


def test_original_full_ci_bundle_is_bound_to_a_verified_merge(evidence):
    """A completed full candidate resolves to its actual merged tree and numeric build."""
    result = dev_bundle.evidence(evidence, 40969348570, "e" * 64)
    assert result == {
        "run_id": 40969348570,
        "artifact_id": 1234,
        "build": "733.1",
        "merge": MERGE,
        "tree": TREE,
        "digest": "e" * 64,
    }


def test_successful_selected_ci_package_remains_eligible(evidence):
    """A scoped suite success is real CI evidence even when the full-suite branch is skipped."""
    jobs = next(value for key, value in evidence.documents.items() if "/attempts/" in key)
    steps = jobs["jobs"][0]["steps"]
    steps[3]["conclusion"] = "skipped"
    steps[4]["conclusion"] = "skipped"
    steps.append(
        {
            "name": "Selected native and integration suites",
            "status": "completed",
            "conclusion": "success",
        }
    )
    assert dev_bundle.evidence(evidence, 40969348570, "e" * 64)["build"] == "733.1"


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_unsuccessful_tree_steps_never_admit_a_package(evidence, focused, conclusion):
    """Neither full nor focused CI may conceal a failed or unfinished tree-marker step."""
    jobs = next(value for key, value in evidence.documents.items() if "/attempts/" in key)
    steps = jobs["jobs"][0]["steps"]
    if focused:
        steps[3]["name"] = "Selected native and integration suites"
    steps[4]["conclusion"] = conclusion
    steps[4]["status"] = "in_progress" if conclusion is None else "completed"
    with pytest.raises(VerificationError, match="merged tested tree"):
        dev_bundle.evidence(evidence, 40969348570, "e" * 64)


def test_full_suite_cannot_skip_its_tree_validation(evidence):
    """The intentional focused-route skip does not weaken full-suite source validation."""
    jobs = next(value for key, value in evidence.documents.items() if "/attempts/" in key)
    jobs["jobs"][0]["steps"][4]["conclusion"] = "skipped"
    with pytest.raises(VerificationError, match="merged tested tree"):
        dev_bundle.evidence(evidence, 40969348570, "e" * 64)


def test_run_changed_during_verification_is_rejected(evidence):
    """Starting a new attempt while evidence is read invalidates the original admission."""
    original_get = evidence.get
    reads = 0

    def changed_run(path):
        """Expose an independently timed attempt change only on the final run recheck."""
        nonlocal reads
        result = original_get(path)
        if path.endswith("runs/40969348570"):
            reads += 1
            if reads == 2:
                result["run_attempt"] = 2
        return result

    evidence.get = changed_run
    with pytest.raises(VerificationError, match="changed during verification"):
        dev_bundle.evidence(evidence, 40969348570, "e" * 64)


@pytest.mark.parametrize(
    "mutation",
    [
        "unmerged",
        "unsigned",
        "off_main",
        "failed",
        "cancelled",
        "wrong_workflow",
        "wrong_attempt",
        "skipped_suite",
        "wrong_tree",
        "skipped_admission",
        "digest",
        "expired",
        "old_artifact",
        "truncated",
    ],
)
def test_incomplete_or_different_ci_evidence_is_rejected(evidence, mutation):
    """No green aggregate or supplied number can substitute for the original merged full candidate."""
    documents = evidence.documents
    run = next(value for key, value in documents.items() if key.endswith("runs/40969348570"))
    pull = next(value for key, value in documents.items() if key.endswith("pulls/121"))
    jobs = next(value for key, value in documents.items() if "/attempts/" in key)
    artifacts = next(value for key, value in documents.items() if "/artifacts?" in key)
    if mutation == "unmerged":
        pull["merged"] = False
    elif mutation == "unsigned":
        documents[f"repos/{dev_bundle.REPOSITORY}/git/commits/{MERGE}"]["verification"][
            "verified"
        ] = False
    elif mutation == "off_main":
        documents[f"repos/{dev_bundle.REPOSITORY}/compare/{MERGE}...main"]["status"] = "diverged"
    elif mutation in {"failed", "cancelled"}:
        run["conclusion"] = mutation
    elif mutation == "wrong_workflow":
        run["path"] = ".github/workflows/quick.yaml"
    elif mutation == "wrong_attempt":
        jobs["jobs"][0]["run_attempt"] = 2
    elif mutation == "skipped_suite":
        jobs["jobs"][0]["steps"][3]["conclusion"] = "skipped"
    elif mutation == "wrong_tree":
        jobs["jobs"][0]["steps"][4]["name"] = "Full CI source tree: " + HEAD
    elif mutation == "skipped_admission":
        jobs["jobs"][1]["conclusion"] = "skipped"
    elif mutation == "digest":
        artifacts["artifacts"][0]["digest"] = "sha256:" + "f" * 64
    elif mutation == "expired":
        artifacts["artifacts"][0]["expires_at"] = "2000-01-01T00:00:00Z"
    elif mutation == "old_artifact":
        artifacts["artifacts"][0]["created_at"] = "2026-10-29T11:59:00Z"
    else:
        jobs["total_count"] = 3
    with pytest.raises(VerificationError):
        dev_bundle.evidence(evidence, 40969348570, "e" * 64)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "app/../../outside",
        "app\\outside",
        "app/CON.py",
        "app/a. ",
        "app/a:b",
        "app//a",
        "app/./a",
    ],
)
def test_archive_paths_cannot_escape_or_alias_windows_files(name):
    """Unsafe nested package destinations fail before any file is written."""
    with pytest.raises(VerificationError, match="Unsafe archive path"):
        dev_bundle.archive_files(zipped({name: b"payload"}))


def test_archive_links_collisions_and_size_limits_are_rejected(monkeypatch):
    """Links and platform aliases cannot replace or escape a code staging directory."""
    with pytest.raises(VerificationError, match="Duplicate"):
        dev_bundle.archive_files(zipped({"app/a.py": b"one", "app/A.py": b"two"}))
    with pytest.raises(VerificationError, match="conflicts"):
        dev_bundle.archive_files(zipped({"app/a": b"one", "app/a/b": b"two"}))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("app/link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(VerificationError, match="links"):
        dev_bundle.archive_files(output.getvalue())
    monkeypatch.setattr(dev_bundle, "MAX_UNPACKED_BYTES", 1)
    with pytest.raises(VerificationError, match="unpacked size"):
        dev_bundle.archive_files(zipped({"file": b"two"}))


def test_paired_payload_identity_and_checksum_validation():
    """The manifest binds real app and integration identities and both nested payloads."""
    data = package()
    manifest, files = dev_bundle.unpack(data)
    assert manifest["build"] == "733.1"
    assert json.loads(files["integration/build-info.json"])["revision"] == REVISION
    outer = dev_bundle.archive_files(data)
    outer["open_energy_planner.zip"] += b"changed"
    with pytest.raises(VerificationError, match="checksum mismatch"):
        dev_bundle.unpack(zipped(outer))
    outer = dev_bundle.archive_files(data)
    manifest["build"] = None
    outer["release-manifest.json"] = json.dumps(manifest)
    with pytest.raises(VerificationError, match="numbered"):
        dev_bundle.unpack(zipped(outer))


def test_wrong_integration_identity_is_rejected_even_with_correct_payload_hashes():
    """Matching archive hashes cannot legitimize different app and integration build numbers."""
    outer = dev_bundle.archive_files(package())
    integration = dev_bundle.archive_files(outer["open_energy_planner.zip"])
    identity = json.loads(integration["build-info.json"])
    identity["build"] = "732.1"
    integration["build-info.json"] = json.dumps(identity)
    outer["open_energy_planner.zip"] = zipped(integration)
    manifest = json.loads(outer["release-manifest.json"])
    manifest["artifacts"]["open_energy_planner.zip"] = hashlib.sha256(
        outer["open_energy_planner.zip"]
    ).hexdigest()
    outer["release-manifest.json"] = json.dumps(manifest)
    outer["SHA256SUMS"] = "".join(
        f"{digest}  {name}\n" for name, digest in manifest["artifacts"].items()
    )
    with pytest.raises(VerificationError, match="build identities differ"):
        dev_bundle.unpack(zipped(outer))


@pytest.mark.parametrize("field", ["version", "build", "revision", "release_tag"])
def test_every_manifest_identity_key_is_required(field):
    """Absent keys cannot be silently treated as nullable matching component identities."""
    outer = dev_bundle.archive_files(package(version=None if field == "version" else "0.0.0.dev0"))
    manifest = json.loads(outer["release-manifest.json"])
    del manifest[field]
    outer["release-manifest.json"] = json.dumps(manifest)
    with pytest.raises(VerificationError, match="Incomplete package identity"):
        dev_bundle.unpack(zipped(outer))


@pytest.mark.parametrize("version", [None, "", 42])
def test_matching_components_still_require_a_real_version(version):
    """Even matching null or malformed versions fail before cache publication and runtime activation."""
    with pytest.raises(VerificationError, match="Invalid package version"):
        dev_bundle.unpack(package(version=version))


@pytest.mark.parametrize("release_tag", ["", 42])
def test_release_tag_is_nullable_but_cannot_be_malformed(release_tag):
    """An absent release remains null, while an empty or nontext tag fails before publication."""
    with pytest.raises(VerificationError, match="Invalid package version or release tag"):
        dev_bundle.unpack(package(release_tag=release_tag))


@pytest.fixture
def staged(tmp_path, monkeypatch, evidence):
    """Stage a validated pair against a small clean merged checkout without reaching GitHub."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "controller.py").write_text("merged controller\n")
    original = package()
    bundle = tmp_path / "ci.zip"
    bundle.write_bytes(original)
    for key, value in evidence.documents.items():
        if "/artifacts?" in key:
            value["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(original).hexdigest()

    def git(command, **kwargs):
        """Expose clean merged checkout files at the process boundary, independently of receipt logic."""
        if command[1] == "rev-parse":
            return MERGE
        if command[1] == "status":
            return ""
        assert command[1:] == ["ls-files", "-z"]
        return b"controller.py\0"

    monkeypatch.setattr(dev_bundle.subprocess, "check_output", git)
    dev_bundle.prepare(root, bundle, 40969348570, evidence)
    return root, bundle, evidence


@pytest.mark.parametrize(
    "changed", ["payload", "archive", "controller", "controller_added", "added", "removed"]
)
def test_warm_cache_rejects_changed_sources(staged, changed):
    """Restarting cannot silently turn a numbered installation into a source overlay."""
    root, _bundle, _client = staged
    manifest, directory = dev_bundle.load(root)
    assert manifest["build"] == "733.1"
    if changed == "controller":
        (root / "controller.py").write_text("unmerged edit\n")
    elif changed == "controller_added":
        (root / "script").mkdir()
        (root / "script/extra.py").write_text("unmerged controller\n")
    elif changed == "added":
        (directory / "integration/extra.py").write_text("extra\n")
    elif changed == "removed":
        (directory / "integration/__init__.py").unlink()
    else:
        path = directory / ("archive.zip" if changed == "archive" else "integration/__init__.py")
        path.write_bytes(b"changed")
    with pytest.raises(VerificationError):
        dev_bundle.load(root)


def test_rejected_update_keeps_prior_bundle_credentials_and_data(staged):
    """Validation failure leaves the last accepted code and all private state intact."""
    root, bundle, client = staged
    credentials = root / ".local/dev/developer.json"
    credentials.write_text('"private credentials"')
    before = (root / ".local/dev/bundle/archive.zip").read_bytes()
    bundle.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        dev_bundle.prepare(root, bundle, 40969348570, client)
    assert (root / ".local/dev/bundle/archive.zip").read_bytes() == before
    assert credentials.read_text() == '"private credentials"'
    assert dev_bundle.load(root)[0]["build"] == "733.1"


def test_failed_cache_swap_restores_previous_accepted_bundle(staged, monkeypatch):
    """A filesystem rename failure restores the old cache instead of leaving a half-installed pair."""
    root, bundle, client = staged
    before = (root / ".local/dev/bundle/archive.zip").read_bytes()
    rename = Path.rename

    def fail_new_cache(path, target):
        """Fail only publication of the new staging directory, allowing the old cache to recover."""
        if path.name == "prepared":
            raise OSError("Simulated rename failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_new_cache)
    with pytest.raises(OSError, match="rename failure"):
        dev_bundle.prepare(root, bundle, 40969348570, client)
    assert (root / ".local/dev/bundle/archive.zip").read_bytes() == before
    assert dev_bundle.load(root)[0]["build"] == "733.1"


def test_invalid_controller_never_replaces_the_previous_cache(staged):
    """An extra ignored controller file is rejected before publishing or deleting the prior package."""
    root, bundle, client = staged
    before = (root / ".local/dev/bundle/receipt.json").read_bytes()
    (root / "script").mkdir()
    extra = root / "script/ignored_overlay.py"
    extra.write_text("untracked source overlay\n")
    with pytest.raises(VerificationError, match="Controller or simulator"):
        dev_bundle.prepare(root, bundle, 40969348570, client)
    assert (root / ".local/dev/bundle/receipt.json").read_bytes() == before
    extra.unlink()
    assert dev_bundle.load(root)[0]["build"] == "733.1"


def test_package_preparation_cannot_overlap_an_update(tmp_path):
    """The host lock rejects another staging operation until provisioning releases its sources."""
    with dev_bundle.update_lock(tmp_path), pytest.raises(OSError), dev_bundle.update_lock(tmp_path):
        pytest.fail("Concurrent source replacement was admitted")
    with dev_bundle.update_lock(tmp_path):
        assert (tmp_path / ".local/dev/update.lock").is_file()


def test_persistent_staging_uses_only_the_accepted_pair(staged, monkeypatch):
    """Loose application and integration sources cannot replace either accepted package component."""
    root, _bundle, _client = staged
    fixture = root / "test_environment/oep_test_fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "__init__.py").write_text('"""Stable simulated fixture."""\n')
    (root / "test_environment/configuration.yaml").write_text("default_config:\n")
    (root / "test_environment/dashboard.yaml").write_text("title: Example\n")
    raw = root / "custom_components/open_energy_planner"
    raw.mkdir(parents=True)
    (raw / "__init__.py").write_text("unmerged source must not be copied\n")
    receipt_path = root / ".local/dev/bundle/receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["source_files"].update(
        {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / "test_environment").rglob("*")
            if path.is_file()
        }
    )
    receipt_path.write_text(json.dumps(receipt))
    monkeypatch.delenv("OEP_SUPERVISOR_SMOKE", raising=False)
    monkeypatch.setattr(provision_supervisor, "ROOT", root)
    target = root / "isolated-supervisor"
    monkeypatch.setattr(provision_supervisor, "SUPERVISOR_ROOT", target)
    result = provision_supervisor.prepare_sources({})
    assert result == provision_supervisor.prepare_sources(result)
    assert (
        json.loads(
            (
                target / "homeassistant/custom_components/open_energy_planner/build-info.json"
            ).read_text()
        )["build"]
        == "733.1"
    )
    assert (
        target / "homeassistant/custom_components/open_energy_planner/__init__.py"
    ).read_text() == '"""Integration source."""\n'
    assert (
        json.loads(
            (
                target / "apps/local/open-energy-planner/src/open_energy_planner/build-info.json"
            ).read_text()
        )["revision"]
        == REVISION
    )


def test_merged_tree_mismatch_preserves_previous_package(staged):
    """The exact original artifact is still rejected if its source tree differs from the verified merge."""
    root, bundle, client = staged
    client.documents[f"repos/{dev_bundle.REPOSITORY}/git/commits/{REVISION}"]["tree"]["sha"] = HEAD
    before = (root / ".local/dev/bundle/archive.zip").read_bytes()
    with pytest.raises(VerificationError, match="merged tested tree"):
        dev_bundle.prepare(root, bundle, 40969348570, client)
    assert (root / ".local/dev/bundle/archive.zip").read_bytes() == before


def test_missing_bundle_stops_before_compose(tmp_path, monkeypatch):
    """An ordinary start has no raw-source fallback and cannot mutate the running lab."""
    monkeypatch.delenv("OEP_DEVELOPER_ENVIRONMENT", raising=False)
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "require_local_engine", lambda: "local-engine")
    monkeypatch.setattr(dev.sys, "argv", ["dev", "start"])

    def unexpected(*args, **kwargs):
        """Any Compose or credential copy after failed package admission is a regression."""
        pytest.fail("Docker mutation before package acceptance")

    monkeypatch.setattr(dev.subprocess, "run", unexpected)
    with pytest.raises(VerificationError, match="verified CI package"):
        dev.main()


@pytest.mark.parametrize("mismatch", [None, "app", "integration", "revision", "stale"])
def test_loaded_components_must_match_the_accepted_package(monkeypatch, mismatch):
    """A stale integration or app blocks the final success receipt even when installation returned success."""
    identity = {"version": "0.0.0.dev0", "build": "733.1", "revision": REVISION}
    manifest = {**identity, "source_state": "commit"}
    status = {"status": "matched", "app": dict(identity), "integration": dict(identity)}
    if mismatch in {"app", "integration"}:
        status[mismatch]["build"] = "732.1"
    elif mismatch == "revision":
        status["app"]["revision"] = HEAD
    elif mismatch:
        status["status"] = "stale"
    monkeypatch.setattr(dev_bundle, "load", lambda root: (manifest, Path("unused")))
    monkeypatch.setattr(provision_supervisor, "ha", lambda *args: {"ip_address": "172.30.33.1"})
    monkeypatch.setattr(provision_supervisor, "run", lambda *args: "private-test-token")

    def read(url, token, route):
        """Use the actual private version route without writing or printing the credential."""
        assert (url, token, route) == ("http://172.30.33.1:8099", "private-test-token", "version")
        return status

    monkeypatch.setattr(provision_supervisor, "read_app", read)
    if mismatch:
        with pytest.raises(ValueError):
            provision_supervisor.verify_installed_package()
    else:
        assert provision_supervisor.verify_installed_package() == status
