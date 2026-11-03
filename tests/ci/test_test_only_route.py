"""Keep test-only reuse bound to owned tests, immutable policy and actual candidate proof."""

import json
import subprocess
from pathlib import Path

import pytest

from script.ci.check_main_ci_reuse import base_test_only_profile
from script.ci.ci_scope import main, matrix_profile, select_profile, selected_suites
from script.ci.ci_scope import test_only_profile as only_test_profile

ROOT = Path(__file__).resolve().parents[2]
BASE = "1" * 40
TESTED = "2" * 40


@pytest.fixture
def changed_paths(monkeypatch):
    """Supply complete tree changes with independent test source blobs."""
    changes = []
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: b"".join(changes)
    )
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    return changes


def change(path, status="M", mode="100644"):
    """Represent additions, deletions and both sides of moves as raw immutable diff records."""
    old_mode = "000000" if status == "A" else mode
    new_mode = "000000" if status == "D" else mode
    return f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}\0{path}\0".encode()


@pytest.mark.parametrize("status", ["A", "M", "D"])
@pytest.mark.parametrize(
    "path,suites",
    [
        ("tests/app/price/test_example.py", {"ci", "unit-app-price"}),
        ("tests/frontend/browser/test_example.py", {"ci", "browser"}),
        ("tests/database/contracts/test_example.py", {"ci", "database-contracts"}),
        ("tests/custom_component/integration/history/test_example.py", {"ci", "ha-history"}),
        ("tests/ci/test_example.py", {"ci"}),
    ],
)
def test_owned_cases_keep_their_required_environment(changed_paths, path, suites, status):
    """Owned Python cases retain browser, real HA or four-backend coverage as applicable."""
    changed_paths.append(change(path, status))
    assert selected_suites(only_test_profile(BASE, TESTED)) == suites


@pytest.mark.parametrize(
    "other_path",
    [
        "src/open_energy_planner/core/units.py",
        "custom_components/open_energy_planner/entity.py",
        ".github/workflows/checks.yaml",
        "script/ci/ci_scope.py",
        "pyproject.toml",
        "AGENTS.md",
        "docs/development/CI-STAGES.md",
        "tests/frontend/browser/input.json",
        "tests/new_area/test_example.py",
        "tests/conftest.py",
        "tests/app/price/support/orphan.py",
    ],
)
def test_runtime_policy_unknown_and_non_python_changes_are_not_test_only(changed_paths, other_path):
    """A test alongside any excluded path cannot hide that path behind a cheap test-only marker."""
    changed_paths.extend([change("tests/app/price/test_example.py"), change(other_path)])
    assert only_test_profile(BASE, TESTED) is None


def test_runtime_to_test_move_does_not_hide_removed_runtime_code(changed_paths):
    """No-rename comparison checks the deleted runtime side as well as the new test file."""
    changed_paths.extend(
        [
            change("src/open_energy_planner/core/units.py", "D"),
            change("tests/ci/test_units.py", "A"),
        ]
    )
    assert only_test_profile(BASE, TESTED) is None


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_special_test_modes_keep_full_checks(changed_paths, mode):
    """Symlinks and gitlinks under a test directory cannot authorize focused reuse."""
    changed_paths.append(change("tests/app/price/test_example.py", mode=mode))
    assert only_test_profile(BASE, TESTED) is None


def test_shared_test_helper_keeps_old_and_current_consumers(changed_paths, monkeypatch):
    """A moved HA consumer remains required even when only native tests import the helper now."""
    helper = "tests/app/price/support/prices.py"
    changed_paths.append(change(helper))

    def sources(revision, root):
        """Retain an HA importer in the old tree and a browser importer in the new tree."""
        consumer = (
            "tests/custom_component/integration/history/test_prices.py"
            if revision == BASE
            else "tests/frontend/browser/test_prices.py"
        )
        return {helper: "", consumer: "from tests.app.price.support.prices import prices"}

    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", sources)
    assert selected_suites(only_test_profile(BASE, TESTED)) == {
        "ci",
        "unit-app-price",
        "ha-history",
        "browser",
    }


@pytest.mark.parametrize("known_consumer", [False, True])
def test_dynamic_imports_add_coverage_without_proving_orphan_ownership(
    changed_paths, monkeypatch, known_consumer
):
    """Unknown loaders retain HA coverage but cannot make an otherwise unused helper eligible."""
    helper = "tests/app/price/support/rates.py"
    case = "tests/app/price/test_rates.py"
    changed_paths.extend([change(helper), change(case)])
    sources = {
        helper: "value = 1",
        case: "from .support.rates import value" if known_consumer else "def test_rate(): pass",
        "tests/custom_component/integration/history/test_dynamic.py": (
            "importlib.import_module(target)"
        ),
    }
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: sources)
    profile = only_test_profile(BASE, TESTED)
    if known_consumer:
        assert selected_suites(profile) == {"ci", "unit-app-price", "ha-history"}
    else:
        assert profile is None


@pytest.mark.parametrize(
    "declaration",
    [
        "pytest_plugins = 'tests.app.price.support.rates'",
        "pytest_plugins = ('tests.app.price.support.rates',)",
        "pytest_plugins = ['tests.app.price.support.rates']",
        "pytest_plugins = load_plugins()",
        "pytest_plugins: tuple[str, ...] = ('tests.app.price.support.rates',)",
        "from plugin_config import PLUGINS as pytest_plugins",
        "from plugin_config import pytest_plugins",
        "import plugin_config as pytest_plugins",
        "from plugin_config import *",
    ],
)
def test_pytest_plugin_loading_requires_reviewed_ownership(changed_paths, monkeypatch, declaration):
    """An unmodeled plugin loader cannot silently omit HA users of a statically owned native helper."""
    helper = "tests/app/price/support/rates.py"
    changed_paths.append(change(helper))
    sources = {
        helper: "value = 1",
        "tests/app/price/test_rates.py": "from .support.rates import value",
        "tests/custom_component/integration/history/test_plugin.py": declaration,
    }
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: sources)
    assert matrix_profile(select_profile(BASE, TESTED)) == "full"
    assert only_test_profile(BASE, TESTED) is None


@pytest.mark.parametrize(
    "loader",
    [
        "import builtins\nload = builtins.__import__\nload(target)",
        "import builtins as loaders\nload = loaders.__import__\nload(target)",
        "load = __import__\nload(target)",
        "from builtins import __import__ as original\nload = original\nload(target)",
        "import importlib\nload = importlib.import_module\nload(target)",
        "from importlib import import_module as original\nload = original\nload(target)",
        "import builtins\nloaders = [builtins.__import__]\nloaders[0](target)",
        "import builtins\ninvoke(builtins.__import__, target)",
        (
            "def first():\n from builtins import __import__ as load\n"
            " load('tests.app.price.support', fromlist=['rates'])\n"
            "def second():\n from importlib import import_module as load\n load('math')"
        ),
        (
            "def first():\n from importlib import import_module as load\n load('math')\n"
            "def second():\n from builtins import __import__ as load\n"
            " load('tests.app.price.support', fromlist=['rates'])"
        ),
    ],
)
def test_escaped_and_conflicting_import_functions_retain_ha_consumers(
    changed_paths, monkeypatch, loader
):
    """Indirect or ambiguous import bindings must not omit HA consumers of a native helper."""
    helper = "tests/app/price/support/rates.py"
    changed_paths.append(change(helper))
    sources = {
        helper: "value = 1",
        "tests/app/price/test_rates.py": "from .support.rates import value",
        "tests/custom_component/integration/history/test_loader.py": loader,
    }
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: sources)
    assert selected_suites(only_test_profile(BASE, TESTED)) == {
        "ci",
        "unit-app-price",
        "ha-history",
    }


@pytest.mark.parametrize(
    "event_name,mixed,expected",
    [
        ("pull_request", False, True),
        ("pull_request", True, False),
        ("workflow_dispatch", False, False),
    ],
)
def test_actions_outputs_and_selection_report_share_test_only_decision(
    changed_paths, tmp_path, monkeypatch, event_name, mixed, expected
):
    """Only an eligible complete PR enables the workflow marker and report-only packaging guards."""
    changed_paths.append(change("tests/app/price/test_example.py"))
    if mixed:
        changed_paths.append(change("src/open_energy_planner/core/units.py"))
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"base": {"sha": BASE}}}))
    for name, value in {
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_SHA": TESTED,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    main()
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())
    report = json.loads((tmp_path / "results/ci/selection.json").read_text())
    assert outputs["test_only"] == str(expected).lower()
    assert report["test_only"] is expected
    assert report["profile"] == outputs["profile"]


@pytest.mark.parametrize("fault", [None, "old_policy", "unavailable", "timeout"])
@pytest.mark.parametrize("selector", ["test_only_profile", "performance_queries_required"])
def test_first_parent_policy_runs_isolated_with_exact_diff_arguments(
    tmp_path, monkeypatch, fault, selector, policy_archive
):
    """Only the requested parent policy executes, isolated from the candidate's current policy."""
    monkeypatch.setattr("script.ci.proof_git.tempfile.tempdir", str(tmp_path))
    original_output = subprocess.check_output
    candidate_policy = tmp_path / "script/ci/ci_scope.py"
    candidate_policy.parent.mkdir(parents=True)
    candidate_policy.write_text("raise RuntimeError('never execute changed policy')\n")
    expected = False if selector == "performance_queries_required" else "selected:ci,unit-app-price"
    policy = (
        "import sys\n"
        f"def {selector}(base, tested):\n"
        "    assert sys.flags.isolated == 1 and sys.flags.no_site == 1\n"
        f"    assert (base, tested) == ('{BASE}', '{TESTED}')\n"
        f"    return {expected!r}\n"
        "if __name__ == '__main__':\n"
        "    raise RuntimeError('never execute policy CLI or write Actions outputs')\n"
    )
    if fault == "old_policy":
        policy = (
            "if __name__ == '__main__':\n"
            "    raise RuntimeError('old policy must not run its ordinary CLI')\n"
        )

    def immutable_policy(command, **options):
        """Model immutable Git transport while retaining the real isolated Python subprocess."""
        if command[0] == "git":
            assert command == ["git", "archive", "--format=tar", BASE, "script"]
            if fault == "unavailable":
                raise subprocess.CalledProcessError(128, command)
            return policy_archive({"script/ci/ci_scope.py": policy})
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return original_output(command, **options)

    monkeypatch.setattr("script.ci.proof_git.subprocess.check_output", immutable_policy)
    monkeypatch.chdir(tmp_path)
    assert base_test_only_profile(BASE, TESTED, selector=selector) == (
        expected if fault is None else None
    )


def test_workflow_test_only_marker_requires_completion_and_cannot_package():
    """The actual workflow gates test-only proof after completion and excludes installation publication."""
    # Workflow wiring is an explicit structural boundary, not app behavior.
    source = (ROOT / ".github/workflows/checks.yaml").read_text(encoding="utf-8")
    for title in (
        "Build matched private installation bundle",
        "Preserve matched installation artifacts",
    ):
        step = source.split("- name: " + title, 1)[1].split("- name:", 1)[0]
        assert "steps.scope.outputs.test_only != 'true'" in step
    marker = source.split("- name: 'Test-only CI source tree:", 1)[1].split("- name:", 1)[0]
    assert "steps.scope.outputs.test_only == 'true'" in marker
    assert "git diff --exit-code HEAD --" in marker
    assert source.index("script.ci.check_suite_completion") < source.index(
        "Test-only CI source tree:"
    )
