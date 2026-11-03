"""Keep moved support helpers connected to all consuming test areas."""

import subprocess
import sys
from pathlib import Path

import pytest

from script.ci.ci_scope import (
    immutable_test_sources,
    matrix_profile,
    select_profile,
    selected_suites,
)
from script.ci.ci_scope import (
    test_consumer_graph as consumer_graph,
)
from script.ci.ci_scope import (
    test_helper_consumers as helper_consumers,
)


def test_new_consumers_inherit_transitive_helper_coverage():
    """New tests and nested domains inherit imports without a filename allowlist."""
    sources = {
        "tests/database/support/archive.py": "",
        "tests/app/forecast/support/http.py": "from tests.database.support import archive",
        "tests/app/forecast/test_new.py": "from .support.http import running",
        "tests/custom_component/integration/history/test_new.py": (
            "from tests.database.support.archive import read_plan"
        ),
        "tests/app/demand/test_unrelated.py": "",
    }
    assert helper_consumers(sources, {"tests/database/support/archive.py"}) == {
        "tests/app/forecast/test_new.py",
        "tests/custom_component/integration/history/test_new.py",
    }


@pytest.mark.parametrize("module_name", ["test_future.py", "future_test.py"])
@pytest.mark.parametrize("binding", ["import", "conftest"])
def test_default_patterns_retain_cross_area_and_fixture_consumers(module_name, binding):
    """Both default pytest filename styles retain importing HA cases and implicit conftest users."""
    consumer = "tests/custom_component/integration/history/nested/" + module_name
    helper = (
        "tests/app/price/support/rates.py"
        if binding == "import"
        else "tests/custom_component/integration/history/conftest.py"
    )
    sources = {
        helper: "value = 1",
        consumer: "from tests.app.price.support.rates import value" if binding == "import" else "",
        "tests/app/price/test_native.py": "from tests.app.price.support.rates import value",
    }
    expected = {consumer}
    if binding == "import":
        expected.add("tests/app/price/test_native.py")
    assert helper_consumers(sources, {helper}) == expected


def test_default_patterns_agree_with_real_pytest_collection(tmp_path):
    """The installed pytest collects both supported styles while ignoring a helper-only filename."""
    for module_name in ("test_first.py", "second_test.py", "helper.py"):
        (tmp_path / module_name).write_text(
            'def test_case():\n    """Collected without executing the body."""\n    assert False\n',
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = {
        line.strip().split("::")[0] for line in result.stdout.splitlines() if "::test_case" in line
    }
    assert collected == {"test_first.py", "second_test.py"}


def test_many_changed_helpers_share_one_parse_without_hiding_orphans(monkeypatch):
    """Parsing cost depends on source count, while each helper retains its own independent consumers."""
    import ast

    sources = {
        "tests/app/price/support/rates.py": "value = 1",
        "tests/app/price/support/unused.py": "value = 2",
        "tests/app/price/test_rates.py": "from .support.rates import value",
        "tests/custom_component/integration/history/test_rates.py": (
            "from tests.app.price.support.rates import value"
        ),
    }
    original_parse = ast.parse
    parsed = []

    def parse(source, *, filename):
        """Count real syntax parses rather than substituting a dependency graph."""
        parsed.append(filename)
        return original_parse(source, filename=filename)

    monkeypatch.setattr("script.ci.ci_scope.ast.parse", parse)
    scenarios_for = consumer_graph(sources)
    for _ in range(40):
        assert scenarios_for("tests/app/price/support/rates.py") == {
            "tests/app/price/test_rates.py",
            "tests/custom_component/integration/history/test_rates.py",
        }
        assert scenarios_for("tests/app/price/support/unused.py") == set()
    assert sorted(parsed) == sorted(sources)


def test_package_initializers_and_conftest_are_implicit_dependencies():
    """Fixtures and package initialization affect descendants even without explicit imports."""
    sources = {
        "tests/app/forecast/__init__.py": "",
        "tests/app/forecast/conftest.py": "",
        "tests/app/forecast/nested/test_future.py": "",
        "tests/app/solar/test_other.py": "",
    }
    for changed in ("tests/app/forecast/conftest.py", "tests/app/forecast/__init__.py"):
        assert helper_consumers(sources, {changed}) == {"tests/app/forecast/nested/test_future.py"}


def test_relative_package_imports_and_cycles_do_not_lose_consumers():
    """A package-relative alias resolves to its helper and import cycles terminate."""
    sources = {
        "tests/ha_contract/support/__init__.py": "from . import metadata",
        "tests/ha_contract/support/metadata.py": "from . import connection",
        "tests/ha_contract/support/connection.py": "from . import metadata",
        "tests/ha_contract/test_future.py": "from .support import connection",
    }
    assert helper_consumers(sources, {"tests/ha_contract/support/metadata.py"}) == {
        "tests/ha_contract/test_future.py"
    }


def test_dynamic_import_users_remain_conservative():
    """An unresolved import target cannot silently exclude its test area."""
    sources = {
        "tests/app/forecast/support/basis.py": "",
        "tests/supervisor/unit/test_dynamic.py": "importlib.import_module(target)",
    }
    assert helper_consumers(sources, {"tests/app/forecast/support/basis.py"}) == {
        "tests/supervisor/unit/test_dynamic.py"
    }


def test_changed_fixture_inside_a_test_module_reaches_external_consumers(monkeypatch):
    """Fixture code need not already live in support for its importing HA test group to remain covered."""
    path = "tests/app/ev/test_existing.py"
    record = f":100644 100644 {'1' * 40} {'2' * 40} M\0{path}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    monkeypatch.setattr(
        "script.ci.ci_scope.immutable_test_sources",
        lambda *args: {
            path: "def configuration(): return {}",
            "tests/custom_component/integration/controls/test_future.py": (
                "from tests.app.ev.test_existing import configuration"
            ),
        },
    )
    assert selected_suites(select_profile("a" * 40, "b" * 40)) == {
        "ci",
        "unit-app-ev",
        "ha-controls",
    }


def test_literal_dynamic_imports_do_not_force_unrelated_supervisor_coverage():
    """A fixed runtime-module import is known and cannot consume an unrelated test helper."""
    sources = {
        "tests/app/forecast/support/basis.py": "",
        "tests/app/forecast/test_new.py": "from .support.basis import forecast_basis",
        "tests/supervisor/unit/test_runtime.py": (
            "from importlib import import_module as load\nload('script.runtime.ha_app')"
        ),
        "tests/app/solar/test_dynamic.py": (
            "from importlib import import_module as load\nload('tests.app.forecast.support.basis')"
        ),
    }
    assert helper_consumers(sources, {"tests/app/forecast/support/basis.py"}) == {
        "tests/app/forecast/test_new.py",
        "tests/app/solar/test_dynamic.py",
    }


def test_conftest_does_not_apply_to_imported_helpers_outside_its_collection_area():
    """Importing a database assertion helper does not load database parametrization fixtures."""
    sources = {
        "tests/database/support/backends.py": "",
        "tests/database/conftest.py": "from .support.backends import BACKENDS",
        "tests/database/contracts/test_backend.py": "",
        "tests/database/support/archive.py": "",
        "tests/app/forecast/test_new.py": "from tests.database.support.archive import read_plan",
        "tests/custom_component/integration/history/test_new.py": (
            "from tests.database.contracts.test_backend import sample"
        ),
    }
    assert helper_consumers(sources, {"tests/database/support/backends.py"}) == {
        "tests/database/contracts/test_backend.py"
    }


def test_backend_configuration_does_not_reach_real_ha_through_ev_fixture():
    """EV wire fixtures must not import backend parametrization into real HA controls."""
    root = Path(__file__).resolve().parents[2]
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8-sig")
        for path in (root / "tests").rglob("*.py")
    }
    consumers = helper_consumers(sources, {"tests/database/support/backends.py"})
    assert consumers
    assert all(
        path.startswith(
            (
                "tests/database/contracts/",
                "tests/database/migrations/",
                "tests/database/performance/",
            )
        )
        for path in consumers
    ), consumers
    wire_consumers = helper_consumers(sources, {"tests/app/ev/support/devices.py"})
    assert any(
        path.startswith("tests/custom_component/integration/controls/") for path in wire_consumers
    )

    assert any(path.startswith("tests/database/contracts/") for path in wire_consumers)


def test_history_and_savings_cases_do_not_own_shared_backend_inputs():
    """Editing a testcase alone must not change migration or volume inputs held in support modules."""
    root = Path(__file__).resolve().parents[2]
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8-sig")
        for path in (root / "tests").rglob("*.py")
    }
    for scenario_path in (
        "tests/app/history/test_history.py",
        "tests/app/savings/test_savings.py",
    ):
        consumers = helper_consumers(sources, {scenario_path})
        assert not any(path.startswith("tests/database/") for path in consumers), consumers


@pytest.mark.parametrize("failure", [None, SyntaxError(), subprocess.CalledProcessError(1, "git")])
def test_selection_reads_both_immutable_trees_and_fails_closed(monkeypatch, failure):
    """A removed HA consumer still selects HA. Unreadable helper evidence keeps full coverage."""
    helper = "tests/app/forecast/support/basis.py"
    record = f":100644 100644 {'1' * 40} {'2' * 40} M\0{helper}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    reads = []

    def snapshot(revision, root):
        """Expose an old consumer absent from the new tree, without running candidate code."""
        reads.append(revision)
        if failure is not None:
            raise failure
        return {
            helper: "",
            **(
                {
                    "tests/custom_component/integration/controls/test_old.py": (
                        "from tests.app.forecast.support.basis import forecast_basis"
                    )
                }
                if revision == "a" * 40
                else {}
            ),
        }

    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", snapshot)
    reasons = []
    profile = select_profile("a" * 40, "b" * 40, reasons=reasons)
    if failure is not None:
        assert profile == "full"
    else:
        assert reads == ["a" * 40, "b" * 40]
        assert selected_suites(profile) == {"ci", "unit-app-forecast", "ha-controls"}
        assert any("Test helper consumer:" in reason for _, reason in reasons)


def test_orphan_helper_requires_review_instead_of_assuming_no_impact(monkeypatch):
    """A helper with no known consumer may use indirect loading and remains full."""
    helper = "tests/app/forecast/support/new.py"
    record = f":000000 100644 {'0' * 40} {'2' * 40} A\0{helper}\0".encode()
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: record
    )
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {helper: ""})
    assert matrix_profile(select_profile("a" * 40, "b" * 40)) == "full"


@pytest.mark.parametrize("damaged", [False, True])
def test_immutable_sources_read_blobs_without_export_filters(monkeypatch, damaged):
    """Git archive attributes cannot hide consumers. Truncated blob output must fail closed."""
    blob = b"c" * 40
    source = b"raise AssertionError('candidate code must never execute')\n"
    calls = []

    def run(command, **options):
        """Supply immutable Git protocol bytes and forbid checkout or candidate execution."""
        calls.append(command)
        assert options["cwd"] == "."
        assert options["check"] is True
        if command[1] == "ls-tree":
            assert command == ["git", "ls-tree", "-rz", "a" * 40, "--", "tests"]
            output = b"100644 blob " + blob + b"\ttests/app/forecast/test_new.py\0"
        else:
            assert command == ["git", "cat-file", "--batch"]
            assert options["input"] == blob + b"\n"
            output = blob + f" blob {len(source)}\n".encode() + source + b"\n"
            if damaged:
                output = output[:-2]
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr("script.ci.proof_git.subprocess.run", run)
    if damaged:
        with pytest.raises(ValueError, match="Truncated"):
            immutable_test_sources("a" * 40, ".")
    else:
        assert immutable_test_sources("a" * 40, ".") == {
            "tests/app/forecast/test_new.py": source.decode()
        }
    assert len(calls) == 2


def test_provisioning_helpers_do_not_depend_on_supervisor_boundaries():
    """Supervisor transport changes cannot pull ordinary provisioning tests into its lifecycle owner."""
    root = Path(__file__).resolve().parents[2]
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8-sig")
        for path in (root / "tests").rglob("*.py")
    }
    supervisor_consumers = helper_consumers(sources, {"tests/supervisor/support/examples.py"})
    assert supervisor_consumers
    assert all(path.startswith("tests/supervisor/") for path in supervisor_consumers)
    registry_consumers = helper_consumers(sources, {"tests/provisioning/support/registry.py"})
    assert "tests/provisioning/unit/test_examples.py" in registry_consumers
    assert "tests/supervisor/unit/test_supervised_examples.py" in registry_consumers


@pytest.mark.parametrize(
    "loader,statically_known",
    [
        ("import builtins\nbuiltins.__import__('tests.app.price.support.rates')", True),
        ("import builtins as loader\nloader.__import__('tests.app.price.support.rates')", True),
        ("from builtins import __import__ as load\nload('tests.app.price.support.rates')", True),
        ("builtins.__import__(target)", False),
        ("builtins.__import__('tests.app.price.support', fromlist=['rates'])", False),
        ("__import__('tests.app.price.support', None, None, ['rates'])", False),
        ("__import__('rates', level=1)", False),
        (
            "from builtins import __import__ as load\nload('tests.app.price.support', fromlist=['rates'])",
            False,
        ),
        ("__import__(target)", False),
    ],
)
def test_builtin_import_forms_preserve_ha_coverage_without_fabricating_static_ownership(
    loader, statically_known
):
    """Literal absolute imports retain exact links. Optional, relative and unknown imports add conservative HA coverage."""
    helper = "tests/app/price/support/rates.py"
    native = "tests/app/price/test_rates.py"
    history = "tests/custom_component/integration/history/test_loader.py"
    sources = {helper: "value = 1", native: "from .support.rates import value", history: loader}
    scenarios_for = consumer_graph(sources)
    assert scenarios_for(helper) == {native, history}
    assert scenarios_for(helper, include_dynamic=False) == (
        {native, history} if statically_known else {native}
    )


@pytest.mark.parametrize(
    "loader",
    [
        "import builtins\nbuiltins.__import__('script.runtime.ha_app')",
        "from builtins import __import__ as load\nload('script.runtime.ha_app')",
    ],
)
def test_unrelated_literal_builtin_imports_do_not_widen_helper_coverage(loader):
    """A known production import cannot consume an unrelated native test helper."""
    helper = "tests/app/price/support/rates.py"
    native = "tests/app/price/test_rates.py"
    sources = {
        helper: "value = 1",
        native: "from .support.rates import value",
        "tests/supervisor/unit/test_runtime.py": loader,
    }
    assert helper_consumers(sources, {helper}) == {native}
