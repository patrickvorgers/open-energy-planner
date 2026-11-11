"""Prove four-machine selection, image transfer and fail-closed report aggregation."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from script.ci import check_suite_completion as completion
from script.ci import parallel_checks as parallel
from script.ci.ci_groups import ATOMIC_SUITES, PROFILES, focused_profile
from tests.ci.browser_evidence import write_browser_evidence

ROOT = Path(__file__).resolve().parents[2]


def test_hosted_images_share_one_storage_setup_and_failed_machines_cancel_peers():
    """Exporter and consumers use one backend before image operations and cancellation cannot package."""
    workflow = (ROOT / ".github/workflows/checks.yaml").read_text(encoding="utf-8")
    preparation = workflow.split("  prepare:\n", 1)[1].split("\n  tests:\n", 1)[0]
    tests = workflow.split("  tests:\n", 1)[1].split("\n  quality:\n", 1)[0]
    quality = workflow.split("  quality:\n", 1)[1].split("\n  publish-release:\n", 1)[0]
    action = "uses: ./.github/actions/shared-image-storage"
    assert preparation.count(action) == tests.count(action) == 1
    assert preparation.index(action) < preparation.index("uses: docker/bake-action@")
    setup = tests.split(action, 1)[1].split("      - name:", 1)[0]
    assert "if: matrix.lane != 'supervisor'" in setup
    assert tests.index(action) < tests.index("script.ci.parallel_checks load")
    assert "fail-fast: true" in tests
    assert "if: always()" in tests and "ci-reports-${{ matrix.lane }}" in tests
    assert "needs.tests.result == 'success'" in quality
    assert quality.index("script.ci.parallel_checks join") < quality.index(
        "script.release.package_release"
    )


@pytest.mark.parametrize("state", ["missing", "existing", "invalid"])
def test_shared_storage_setup_preserves_configuration_and_rejects_invalid_json(tmp_path, state):
    """The extracted real setup changes only its feature flag and never overwrites malformed settings."""
    action = (ROOT / ".github/actions/shared-image-storage/action.yaml").read_text(encoding="utf-8")
    setup = dedent(action.split("sudo python3 - <<'PY'\n", 1)[1].split("\n        PY", 1)[0])
    configuration = tmp_path / "daemon.json"
    if state == "existing":
        configuration.write_text(
            json.dumps({"log-driver": "json-file", "features": {"retained": True}})
        )
    elif state == "invalid":
        configuration.write_text("invalid existing configuration")

    # Redirect only the fixed system path, leaving the extracted setup executable unchanged.
    assert setup.count("Path('/etc/docker/daemon.json')") == 1
    script = tmp_path / "storage-setup.py"
    script.write_text(
        setup.replace("Path('/etc/docker/daemon.json')", f"Path({str(configuration)!r})")
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, check=False)
    if state == "invalid":
        assert result.returncode != 0
        assert configuration.read_text() == "invalid existing configuration"
    elif state == "existing":
        assert result.returncode == 0
        assert json.loads(configuration.read_text()) == {
            "log-driver": "json-file",
            "features": {"retained": True, "containerd-snapshotter": True},
        }
    else:
        assert result.returncode == 0
        assert json.loads(configuration.read_text()) == {
            "features": {"containerd-snapshotter": True}
        }


@pytest.mark.parametrize("profile", [*PROFILES, *map(focused_profile, sorted(ATOMIC_SUITES))])
def test_every_selection_partitions_without_expansion_or_duplicate_suites(profile):
    """Legacy parents and each independently selected group have exactly one machine owner."""
    assignments = parallel.lane_suites(profile)
    flattened = [suite for suites in assignments.values() for suite in suites]
    assert len(assignments) <= 4
    assert len(flattened) == len(set(flattened))
    assert set(flattened) == completion.required_suites(profile)


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("documentation", {"native": ["native"]}),
        ("selected:ci,unit-supervisor", {"native": ["native"]}),
        (
            "selected:browser,ci,unit-frontend",
            {"native": ["native"], "browser": ["ui-tests"]},
        ),
        (
            "selected:ci,databases,supervisor",
            {"native": ["native"], "databases": ["native"], "supervisor": []},
        ),
        ("selected:ci,ha-controls", {"native": ["native", "ha-tests", "app"]}),
        ("selected:ci,ha-history", {"native": ["native", "ha-tests"]}),
    ],
)
def test_selected_machines_download_only_the_images_their_commands_use(profile, expected):
    """Browser-only changes never prepare databases or actual Supervisor acceptance machines."""
    assert {
        lane: parallel.lane_images(profile, lane) for lane in parallel.lane_suites(profile)
    } == expected


@pytest.fixture
def hosted_identity(monkeypatch, tmp_path):
    """Give each transfer an independent checkout identity and disposable filesystem."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_SHA", "1" * 40)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "oep-check-123-2")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setattr(parallel, "checkout_tree", lambda: "2" * 40)
    monkeypatch.setattr(completion, "checkout_tree", lambda: "2" * 40)
    monkeypatch.setattr(completion, "REPORT", tmp_path / "results/ci/suite-completion.json")
    return tmp_path


def prepare_plan(profile):
    """Write a captured selection independently from the transfer or aggregation operations."""
    document = parallel.identity(profile)
    document["lanes"] = {
        lane: sorted(suites) for lane, suites in parallel.lane_suites(profile).items()
    }
    parallel.PREPARED.mkdir(parents=True)
    (parallel.PREPARED / "parallel-plan.json").write_text(json.dumps(document))
    (parallel.PREPARED / "preflight").mkdir()
    return document


def test_preparation_exports_shared_native_image_once_and_retains_all_aliases(
    hosted_identity, monkeypatch
):
    """Native and database machines consume one built image without a second build or save."""
    calls = []
    Path("results/ci").mkdir(parents=True)
    Path("results/ci/selection.json").write_text("{}")

    def inspect(command):
        """Return one image identity for independently provided native aliases."""
        calls.append(command)
        return json.dumps([{"Id": "sha256:fixture"} for _ in command[3:]]).encode()

    def save(command, **options):
        """Record the actual save boundary and provide nonempty transferable bytes."""
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"saved native layers")

    monkeypatch.setattr(subprocess, "check_output", inspect)
    monkeypatch.setattr(subprocess, "run", save)
    parallel.export_images("selected:ci,databases,supervisor")
    saved = [command for command in calls if "save" in command]
    assert len(saved) == 1
    assert saved[0][-3:] == [
        "oep-check-123-2-native",
        "oep-check-123-2-db-tests",
        "oep-check-123-2-provision-tests",
    ]
    matrix = json.loads(Path("outputs").read_text().removeprefix("matrix="))
    assert matrix == {
        "include": [
            {"lane": "native", "images": ["native"]},
            {"lane": "databases", "images": ["native"]},
            {"lane": "supervisor", "images": []},
        ]
    }


@pytest.mark.parametrize("fault", ["none", "checksum", "image", "attempt", "assignment"])
def test_image_load_requires_exact_archive_build_and_candidate_identity(
    hosted_identity, monkeypatch, fault
):
    """Corrupt bytes, wrong loaded images and stale plans cannot reach test execution."""
    profile = "selected:ci,unit-supervisor"
    plan = prepare_plan(profile)
    archive = parallel.PREPARED / "native.tar"
    archive.write_bytes(b"original image")
    plan["images"] = {
        "native": {
            "sha256": parallel.file_digest(archive),
            "image_id": "sha256:expected",
            "tags": ["oep-check-123-2-native"],
        }
    }
    if fault == "checksum":
        archive.write_bytes(b"altered image")
    elif fault == "attempt":
        plan["run_attempt"] = "1"
    elif fault == "assignment":
        plan["lanes"]["supervisor"] = ["supervisor"]
    (parallel.PREPARED / "parallel-plan.json").write_text(json.dumps(plan))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command: json.dumps(
            [{"Id": "sha256:wrong" if fault == "image" else "sha256:expected"}]
        ).encode(),
    )
    if fault != "none":
        with pytest.raises(RuntimeError):
            parallel.load_images(profile, "native")
        assert not calls or fault == "image"
    else:
        parallel.load_images(profile, "native")
        assert len(calls) == 1 and calls[0][1:3] == ["image", "load"]
        assert not archive.exists()


@pytest.mark.parametrize(
    "fault",
    ["none", "missing", "extra", "tree", "attempt", "suite", "count", "failed", "foreign-report"],
)
def test_join_requires_every_original_lane_and_rechecks_actual_reports(
    hosted_identity, monkeypatch, fault
):
    """Only matching complete successful lane evidence can publish the existing package gate."""
    profile = "selected:ci,supervisor,unit-supervisor"
    prepare_plan(profile)
    lanes = Path(".local/ci-lanes")
    for lane, suites in {
        "native": {"ci", "unit-supervisor"},
        "supervisor": {"supervisor"},
    }.items():
        location = lanes / f"ci-reports-{lane}"
        location.mkdir(parents=True)
        for suite in suites:
            name = "supervisor-replay" if suite == "supervisor" else suite
            (location / f"{name}.xml").write_text(
                f'<testsuite><testcase name="{suite}" /></testsuite>'
            )
        monkeypatch.setattr(completion, "REPORT", location / "suite-completion.json")
        parallel.write_lane_completion(profile, lane, suites)
    monkeypatch.setattr(completion, "REPORT", hosted_identity / "results/ci/suite-completion.json")
    location = lanes / "ci-reports-supervisor"
    receipt = location / "lane-completion-supervisor.json"
    document = json.loads(receipt.read_text())
    if fault == "missing":
        receipt.unlink()
    elif fault == "extra":
        (lanes / "ci-reports-browser").mkdir()
    elif fault == "tree":
        document["source_tree"] = "3" * 40
    elif fault == "attempt":
        document["run_attempt"] = "1"
    elif fault == "suite":
        document["completed"] = ["ci"]
    elif fault == "count":
        document["executed_tests"]["supervisor"] = 2
    elif fault == "failed":
        (location / "supervisor-replay.xml").write_text(
            '<testsuite><testcase name="bad"><failure /></testcase></testsuite>'
        )
    elif fault == "foreign-report":
        (location / "ci.xml").write_text('<testsuite><testcase name="duplicate" /></testsuite>')
    if fault != "missing":
        receipt.write_text(json.dumps(document))
    if fault != "none":
        with pytest.raises((RuntimeError, FileNotFoundError)):
            parallel.join_reports(profile, lanes)
        assert not completion.REPORT.exists()
    else:
        parallel.join_reports(profile, lanes)
        completion.verify_completion(profile, "2" * 40)
        assert json.loads(completion.REPORT.read_text())["executed_tests"] == {
            "ci": 1,
            "unit-supervisor": 1,
            "supervisor": 1,
        }


@pytest.mark.parametrize("fault", ["none", "browser-extra", "native-extra", "missing-worker"])
def test_join_validates_nested_xml_without_rejecting_proven_browser_worker_reports(
    hosted_identity, monkeypatch, fault
):
    """Expected worker reports retain identity proof while any unassigned nested XML blocks packaging."""
    profile = "selected:browser,ci"
    prepare_plan(profile)
    lanes = Path(".local/ci-lanes")
    for lane, suites in {"native": {"ci"}, "browser": {"browser"}}.items():
        location = lanes / f"ci-reports-{lane}"
        location.mkdir(parents=True)
        if lane == "browser":
            write_browser_evidence(location)
        else:
            (location / "ci.xml").write_text('<testsuite><testcase name="ci" /></testsuite>')
        monkeypatch.setattr(completion, "REPORT", location / "suite-completion.json")
        parallel.write_lane_completion(profile, lane, suites)
    monkeypatch.setattr(completion, "REPORT", hosted_identity / "results/ci/suite-completion.json")
    if fault == "browser-extra":
        (lanes / "ci-reports-browser/browser-0/extra.xml").write_text(
            '<testsuite><testcase name="unassigned" /></testsuite>'
        )
    elif fault == "native-extra":
        extra = lanes / "ci-reports-native/unexpected/nested.xml"
        extra.parent.mkdir()
        extra.write_text('<testsuite><testcase name="unassigned" /></testsuite>')
    elif fault == "missing-worker":
        (lanes / "ci-reports-browser/browser-1/ui.xml").unlink()
    if fault != "none":
        with pytest.raises(RuntimeError):
            parallel.join_reports(profile, lanes)
        assert not completion.REPORT.exists()
    else:
        parallel.join_reports(profile, lanes)
        completion.verify_completion(profile, "2" * 40)
        assert json.loads(completion.REPORT.read_text())["executed_tests"] == {
            "ci": 1,
            "browser": 1,
        }
        for worker in (0, 1):
            assert (completion.REPORT.parent / f"browser-{worker}/ui.xml").is_file()


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("lane", ["native", "browser", "databases", "supervisor"])
def test_hosted_controller_executes_only_its_assigned_real_command_boundaries(
    hosted_identity, monkeypatch, lane, fails
):
    """The matrix cannot accidentally repeat native tests in browser, DB or Supervisor jobs."""
    profile = "selected:browser,ci,databases,ha-controls,supervisor,unit-app"
    calls = []
    completed = []

    def run(command, **options):
        """Fail workload launches while allowing inspection, diagnostics and cleanup."""
        calls.append(command)
        if fails and ("run" in command or command[-1] == "script.supervisor.check_supervisor"):
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        parallel,
        "write_lane_completion",
        lambda route, owner, suites: completed.append((owner, suites)),
    )
    from script.ci import check_browser

    def browser(compose):
        """Record the worker controller boundary and model a terminal worker failure."""
        calls.append(["browser"])
        if fails:
            raise subprocess.CalledProcessError(1, ["browser"])

    monkeypatch.setattr(check_browser, "run_workers", browser)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ci.py", "--prebuilt", "--stage", "tests", "--profile", profile, "--lane", lane],
    )
    if fails:
        with pytest.raises(subprocess.CalledProcessError):
            runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
        assert not completed
        assert any("down" in command for command in calls)
        return
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert completed == [(lane, parallel.lane_suites(profile)[lane])]
    assert (["browser"] in calls) is (lane == "browser")
    assert any(command[-1] == "script.supervisor.check_supervisor" for command in calls) is (
        lane == "supervisor"
    )
    assert any("db-tests" in command for command in calls) is (lane == "databases")
    assert any("--ci-only" in command for command in calls) is (lane == "native")
    assert any("ha-controls" == command[-1] for command in calls) is (lane == "native")


@pytest.mark.parametrize(
    "profile,lane,expected",
    [
        ("full", "native", {"native", "ha", "ha-contracts", "provisioning"}),
        ("full", "databases", {"databases"}),
        ("full+performance-queries", "databases", {"databases", "performance-queries"}),
        ("documentation", "native", {"documentation"}),
    ],
)
def test_legacy_and_documentation_lanes_retain_original_execution_without_repeating_backends(
    hosted_identity, monkeypatch, profile, lane, expected
):
    """Full native excludes separately assigned databases and documentation starts no unrelated suites."""
    calls = []
    completed = []
    monkeypatch.setattr(subprocess, "run", lambda command, **options: calls.append(command))
    monkeypatch.setattr(
        parallel,
        "write_lane_completion",
        lambda route, owner, suites: completed.append((owner, suites)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ci.py", "--prebuilt", "--stage", "tests", "--profile", profile, "--lane", lane],
    )
    runpy.run_path(str(ROOT / "script/ci/ci.py"), run_name="__main__")
    assert completed == [(lane, expected)]
    if lane == "native" and profile == "full":
        assert sum(command[-1] == "ha-contracts" for command in calls) == 1
        assert sum(command[-1] == "--without-database-contracts" for command in calls) == 1
        assert not any("db-tests" in command or "ui-tests" in command for command in calls)
    if profile == "documentation":
        launches = [command for command in calls if "run" in command]
        assert len(launches) == 1 and launches[0][-1] == "--documentation-only"
