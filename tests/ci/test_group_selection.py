"""Prove independent suite unions, directory growth and conservative contract boundaries."""

from pathlib import Path, PurePosixPath

import pytest

from script.ci.ci_scope import (
    APP_TEST_AREAS,
    matrix_profile,
    path_profile,
    select_profile,
    selected_suites,
    selection_summary,
)
from script.ci.pytest_suites import pytest_arguments
from script.ci.scope_report import write_selection_summary

BASE = "1" * 40
TESTED = "2" * 40


@pytest.fixture(autouse=True)
def immutable_sources(monkeypatch):
    """Synthetic path-policy trees have no imports beyond the changed scenario itself."""
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})


def change(path, status="M"):
    """Encode independent whole-PR records including both sides of moves."""
    old = "000000" if status == "A" else "100644"
    new = "000000" if status == "D" else "100644"
    return f":{old} {new} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()


@pytest.mark.parametrize("status", ["A", "M", "D"])
@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/open_energy_planner/frontend/new_page.py", {"ci", "unit-frontend", "browser"}),
        ("src/open_energy_planner/core/new_units.py", {"ci", "unit-app"}),
        ("src/open_energy_planner/contracts/calculation/new_formula.py", {"ci", "unit-app"}),
        ("src/open_energy_planner/demand/calculation/new_model.py", {"ci", "unit-app"}),
        ("src/open_energy_planner/solar/calculation/new_model.py", {"ci", "unit-app"}),
        ("src/open_energy_planner/installation/calculation/new_estimate.py", {"ci", "unit-app"}),
        (
            "src/open_energy_planner/forecast_selection/calculation/new_decision.py",
            {"ci", "unit-app"},
        ),
        ("src/open_energy_planner/optimization/calculation/new_accounting.py", {"ci", "unit-app"}),
        (
            "src/open_energy_planner/supervisor/new_control.py",
            {"ci", "unit-supervisor", "supervisor"},
        ),
        ("script/supervisor/new_control.py", {"ci", "unit-supervisor", "supervisor"}),
        (
            "src/open_energy_planner/home_assistant/new_contract.py",
            {"ci", "unit-ha-contract", "ha-contracts", "ha-history", "ha-controls"},
        ),
        (
            "src/open_energy_planner/storage/adapters/new_query.py",
            {"ci", "unit-app", "browser", "databases", "performance-queries"},
        ),
        (
            "src/open_energy_planner/thermal/storage/adapters/new_query.py",
            {
                "ci",
                "unit-app",
                "unit-ha-contract",
                "browser",
                "databases",
                "performance-queries",
                "ha-thermal",
            },
        ),
        (
            "src/open_energy_planner/storage/adapters/lifecycle/new_startup.py",
            {
                "ci",
                "unit-app",
                "databases",
                "performance-queries",
                "ha-contracts",
                "ha-connection",
                "provisioning",
                "supervisor",
                "unit-supervisor",
            },
        ),
        (
            "src/open_energy_planner/runtime/new_startup.py",
            {"ci", "unit-app", "unit-supervisor", "ha-connection", "provisioning", "supervisor"},
        ),
        (
            "script/provisioning/new_example.py",
            {"ci", "unit-provisioning", "unit-supervisor", "provisioning"},
        ),
        (
            "test_environment/oep_test_fixture/new_device.py",
            {"ci", "unit-test-environment", "ha-test-environment", "provisioning"},
        ),
        ("tests/app/forecast/test_new.py", {"ci", "unit-app-forecast"}),
        ("tests/ha_contract/test_new.py", {"ci", "unit-ha-contract"}),
        ("tests/custom_component/unit/test_new.py", {"ci", "unit-component"}),
        ("tests/custom_component/integration/controls/test_new.py", {"ci", "ha-controls"}),
        ("tests/custom_component/integration/history/test_new.py", {"ci", "ha-history"}),
        ("tests/frontend/unit/test_new.py", {"ci", "unit-frontend"}),
        ("tests/frontend/browser/test_new.py", {"ci", "browser"}),
        ("tests/supervisor/unit/test_new.py", {"ci", "unit-supervisor"}),
        ("tests/provisioning/unit/test_new.py", {"ci", "unit-provisioning"}),
        ("tests/test_environment/unit/test_new.py", {"ci", "unit-test-environment"}),
        ("tests/test_environment/integration/test_new.py", {"ci", "ha-test-environment"}),
        ("tests/database/contracts/test_new.py", {"ci", "database-contracts"}),
    ],
)
def test_growth_inherits_reviewed_owner_on_add_modify_and_delete(
    monkeypatch, status, path, expected
):
    """Known-area growth inherits exact contracts without a per-file allowlist or implicit full superset."""
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: change(path, status)
    )
    assert selected_suites(select_profile(BASE, TESTED)) == expected


@pytest.mark.parametrize(
    "paths,expected",
    [
        (
            ["script/supervisor/new.py", "src/open_energy_planner/web/public/style.css"],
            {"ci", "unit-supervisor", "supervisor", "unit-frontend", "browser"},
        ),
        (
            ["src/open_energy_planner/core/new.py", "tests/database/contracts/test_new.py"],
            {"ci", "unit-app", "database-contracts"},
        ),
        (
            [
                "tests/custom_component/integration/controls/test_new.py",
                "tests/database/contracts/test_new.py",
            ],
            {"ci", "ha-controls", "database-contracts"},
        ),
        (
            [
                "src/open_energy_planner/frontend/assets.py",
                "src/open_energy_planner/storage/adapters/documents.py",
            ],
            {"ci", "unit-frontend", "browser", "unit-app", "databases", "performance-queries"},
        ),
        (["tests/ci/test_new.py", "docs/guide.md"], {"ci"}),
    ],
)
def test_complete_pr_unions_independent_suites_without_full_fallback(monkeypatch, paths, expected):
    """Mixed areas retain earlier edits but never add unrelated expensive suites."""
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output",
        lambda *args, **kwargs: b"".join(change(path) for path in paths),
    )
    profile = select_profile(BASE, TESTED)
    assert profile != "full"
    assert selected_suites(profile) == expected


def test_move_keeps_old_lifecycle_and_new_frontend_coverage(monkeypatch):
    """A cheaper destination cannot erase the lifecycle impact of a removed startup file."""
    records = change("src/open_energy_planner/runtime/startup.py", "D") + change(
        "src/open_energy_planner/frontend/startup.py", "A"
    )
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: records
    )
    suites = selected_suites(select_profile(BASE, TESTED))
    assert {"supervisor", "provisioning", "ha-connection", "browser", "unit-frontend"} <= suites
    assert "databases" not in suites


@pytest.mark.parametrize(
    "path",
    [
        "src/open_energy_planner/new_domain/storage.py",
        "src/open_energy_planner/new_runtime.py",
        "tests/new_domain/test_future.py",
        "custom_components/unknown/sensor.py",
        "script/unknown/new_script.py",
        "compose.yaml",
        "Dockerfile.chain-test",
        "custom_components/open_energy_planner/manifest.json",
        "test_environment/oep_test_fixture/manifest.json",
    ],
)
def test_unknown_areas_and_installation_metadata_stay_full(monkeypatch, path):
    """Unknown contracts and changed installation requirements require a reviewed ownership decision."""
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: change(path, "A")
    )
    assert matrix_profile(select_profile(BASE, TESTED)) == "full"


def test_legacy_profiles_keep_their_original_meaning():
    """Older immutable base policies cannot silently receive narrower execution after upgrading the runner."""
    assert selected_suites("native") == {"native"}
    assert selected_suites("ha") == {"native", "browser", "ha"}
    assert selected_suites("storage") == {"native", "browser", "ha", "databases"}
    assert selected_suites("full") == {"native", "browser", "ha", "databases", "supervisor"}


@pytest.mark.parametrize(
    "profile",
    [
        "selected:",
        "selected:unit-app",
        "selected:ci,ci",
        "selected:unit-app,ci",
        "selected:ci,unknown",
        "selected:ci,ha;echo",
    ],
)
def test_malformed_independent_selections_are_rejected(profile):
    """Unknown names, duplicates, missing quality gates and noncanonical transport cannot run partially."""
    with pytest.raises(ValueError):
        selected_suites(profile)


def test_new_tests_are_collected_by_directory_not_name_lists():
    """New cases in every split HA group and the four-backend suite join real execution automatically."""
    for suite, directory in (
        ("ha-contracts", "contracts"),
        ("ha-controls", "controls"),
        ("ha-history", "history"),
        ("ha-connection", "connection"),
        ("ha-thermal", "thermal"),
    ):
        arguments = pytest_arguments(suite)
        assert "tests/custom_component/integration/" + directory in arguments
        assert "-k" not in arguments
    assert {"tests/database/contracts", "tests/database/migrations"} <= set(
        pytest_arguments("databases")
    )
    assert "tests/test_environment/integration" in pytest_arguments("ha-test-environment")


def test_every_test_file_has_a_declared_area():
    """A new top-level test area must acquire an owner instead of escaping execution."""
    root = Path(__file__).resolve().parents[2]
    unknown = [
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("test_*.py")
        if path_profile(PurePosixPath(path.relative_to(root).as_posix())) == "full"
    ]
    assert not unknown, unknown


def test_every_declared_environment_is_covered_by_full_execution():
    """Adding a group outside the legacy full runner cannot silently leave full coverage incomplete."""
    from script.ci.ci_scope import HA_TEST_DIRECTORIES, PYTEST_AREAS

    native = pytest_arguments("native")
    ignored = [
        argument.removeprefix("--ignore=")
        for argument in native
        if argument.startswith("--ignore=")
    ]
    for suite, directories in PYTEST_AREAS.items():
        assert suite.startswith(("unit-", "ha-")), suite
        for directory in directories:
            assert directory.startswith("tests/"), directory
            if suite.startswith("ha-"):
                assert any(
                    directory == root or directory.startswith(root + "/")
                    for root in HA_TEST_DIRECTORIES
                ), directory
            else:
                assert not any(
                    directory == root or directory.startswith(root + "/") for root in ignored
                ), directory


def test_summary_shows_selected_and_omitted_groups_without_duplicate_rows(capsys):
    """Reviewers see why Supervisor runs and why unrelated HA, browser and database groups do not."""
    profile = "selected:ci,supervisor,unit-supervisor"
    write_selection_summary(selection_summary(profile, [(profile, "script/supervisor/new.py")]))
    summary = capsys.readouterr().out
    assert "| supervisor | Selected | `script/supervisor/new.py`" in summary
    assert "| documentation | Included in ci |" in summary
    for suite in ("ha-controls", "ha-history", "databases", "browser", "provisioning"):
        assert f"| {suite} | Not selected |" in summary
    assert summary.count("| ha-contracts |") == 1
    write_selection_summary(
        selection_summary(
            "frontend", [("frontend", "src/open_energy_planner/web/public/style.css")]
        )
    )
    summary = capsys.readouterr().out
    assert "| ci | Included in native |" in summary
    assert "| unit-frontend | Included in native |" in summary


def test_ci_profile_collects_documentation_checks():
    """A mixed policy and documentation change retains link and database dictionary verification."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, *pytest_arguments("ci", collect_only=True)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_maintained_documentation_links_resolve" in result.stdout
    assert "test_every_test_file_has_a_declared_area" in result.stdout


@pytest.mark.parametrize("area", ["unit", "integration"])
def test_removed_ha_test_layout_cannot_grant_cheap_coverage(area):
    """Adding a file under a retired test root requires new ownership instead of inheriting obsolete coverage."""
    from pathlib import PurePosixPath

    from script.ci.ci_scope import path_profile

    assert path_profile(PurePosixPath(f"tests/home_assistant/{area}/test_new.py")) == "full"


def test_documentation_profile_collects_future_files(tmp_path):
    """A new documentation test runs alongside both required CI policy checks without editing a file list."""
    import subprocess
    import sys

    for relative_path in (
        "tests/documentation/test_future_documentation.py",
        "tests/ci/test_provenance.py",
        "tests/ci/test_ci_scope.py",
    ):
        test_file = tmp_path / relative_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(
            'def test_inherited():\n    """A new case inherits its environment."""\n    assert True\n',
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, *pytest_arguments("documentation", collect_only=True)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/documentation/test_future_documentation.py::test_inherited" in result.stdout
    assert "tests/ci/test_provenance.py::test_inherited" in result.stdout
    assert "tests/ci/test_ci_scope.py::test_inherited" in result.stdout


@pytest.mark.parametrize("area", APP_TEST_AREAS)
def test_app_test_domains_inherit_native_collection_without_other_domains(area):
    """New tests in a reviewed app domain inherit only that native domain and the cheap CI gate."""
    from pathlib import PurePosixPath

    path = PurePosixPath("tests/app") / area / "test_future.py"
    suite = "unit-app-" + area.replace("_", "-")
    assert selected_suites(path_profile(path)) == {"ci", suite}
    assert "tests/app/" + area in pytest_arguments(suite)


def test_new_calculation_domains_and_lifecycle_helpers_keep_their_actual_boundaries():
    """Math avoids platform startup. New bootstrap writers and Recorder contracts retain actual acceptance."""
    from pathlib import PurePosixPath

    assert selected_suites(
        path_profile(PurePosixPath("src/open_energy_planner/installation/calculation/future.py"))
    ) == {"ci", "unit-app"}
    assert {"supervisor", "provisioning", "databases"} <= selected_suites(
        path_profile(PurePosixPath("src/open_energy_planner/storage/adapters/lifecycle/future.py"))
    )
    assert selected_suites(
        path_profile(PurePosixPath("test_environment/oep_test_fixture/calculation/future.py"))
    ) == {"ci", "unit-test-environment"}
    assert {"ha-test-environment", "provisioning"} <= selected_suites(
        path_profile(
            PurePosixPath("test_environment/oep_test_fixture/calculation/recorder/future.py")
        )
    )
    assert path_profile(PurePosixPath("tests/app/unknown/test_future.py")) == "full"


def test_contained_native_and_solver_groups_never_run_twice(capsys):
    """Independent unions preserve parent coverage and report inherited child coverage without duplicate execution."""
    from script.ci.ci_scope import focused_profile

    assert focused_profile("unit-app", "unit-app-price") == "selected:ci,unit-app"
    assert focused_profile("ha-controls", "ha-solver") == "selected:ci,ha-controls"
    write_selection_summary(
        selection_summary("selected:ci,unit-app", [("selected:ci,unit-app", "shared math")])
    )
    assert "| unit-app-price | Included in unit-app |" in capsys.readouterr().out


def test_native_solver_adapters_keep_real_handoff_without_unrelated_history():
    """Native process/backend changes retain a real app-to-HA solve while isolated history and storage can be omitted."""
    from pathlib import PurePosixPath

    for owner in ("highs", "process"):
        profile = path_profile(
            PurePosixPath(f"src/open_energy_planner/optimization/adapters/{owner}/future.py")
        )
        assert selected_suites(profile) == {"ci", "unit-app", "ha-solver"}


def test_source_identity_binding_retains_real_ha_contracts():
    """HA device and measurement identities are a protocol boundary even when their mapping uses no framework imports."""
    from pathlib import PurePosixPath

    suites = selected_suites(
        path_profile(PurePosixPath("src/open_energy_planner/forecast_quality/bindings.py"))
    )
    assert {"ha-contracts", "ha-history"} <= suites
