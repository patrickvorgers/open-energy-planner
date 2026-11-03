"""Keep app composition and native domains selective without hiding peer or lifecycle impact."""

from pathlib import PurePosixPath

import pytest

from script.ci.ci_scope import path_profile, select_profile, selected_suites


@pytest.mark.parametrize(
    "domain",
    [
        "solar",
        "prices",
        "forecast_selection",
        "savings",
        "demand",
        "contracts",
        "history",
    ],
)
def test_native_domain_keeps_app_and_peer_contracts_without_unrelated_ha(domain):
    """New native files inherit app and wire coverage without Recorder or actual control execution."""
    suites = selected_suites(
        path_profile(PurePosixPath(f"src/open_energy_planner/{domain}/future.py"))
    )
    assert {"unit-app", "unit-ha-contract", "browser"} <= suites
    assert not any(suite.startswith("ha-") for suite in suites)


@pytest.mark.parametrize(
    "domain,ha_suites",
    [
        ("appliances", {"ha-controls", "ha-thermal", "ha-history"}),
        ("ev", {"ha-controls"}),
        ("thermal", {"ha-thermal"}),
        ("installation", {"ha-contracts", "ha-controls", "ha-thermal", "ha-history"}),
        ("optimization", {"ha-controls"}),
        ("forecast_quality", {"ha-contracts", "ha-history"}),
    ],
)
def test_execution_domains_select_exactly_their_reviewed_groups(domain, ha_suites):
    """Each execution or identity owner retains its complete independent integration selection."""
    suites = selected_suites(
        path_profile(PurePosixPath(f"src/open_energy_planner/{domain}/future.py"))
    )
    assert suites == {"ci", "unit-app", "unit-ha-contract", "browser"} | ha_suites


@pytest.mark.parametrize(
    "module,required,omitted",
    [
        (
            "app.py",
            {"databases", "supervisor", "provisioning", "ha-connection", "ha-contracts"},
            {"ha-history", "ha-controls", "ha-thermal"},
        ),
        (
            "live_inputs.py",
            {"unit-app", "unit-ha-contract", "ha-contracts", "ha-controls"},
            {"databases", "ha-history", "supervisor"},
        ),
        (
            "discovery.py",
            {"unit-app-thermal", "ha-connection", "supervisor", "unit-ha-contract"},
            {"databases", "ha-history", "ha-thermal"},
        ),
    ],
)
def test_existing_root_owners_keep_responsible_contracts(module, required, omitted):
    """Reviewed root modules retain their real composition or platform proof rather than every feature."""
    suites = selected_suites(path_profile(PurePosixPath("src/open_energy_planner/" + module)))
    assert required <= suites
    assert not omitted & suites


def test_new_root_module_still_requires_reviewed_ownership():
    """An undeclared central module cannot inherit a cheap domain route."""
    assert path_profile(PurePosixPath("src/open_energy_planner/new_coordinator.py")) == "full"


@pytest.mark.parametrize(
    "module", ["thermal/storage.py", "installation/storage.py", "installation/storage/records.py"]
)
def test_domain_storage_keeps_database_and_domain_contracts(module):
    """Storage implementation retains both data safety and its domain's real HA proof."""
    domain = module.split("/", 1)[0]
    ordinary = selected_suites(
        path_profile(PurePosixPath(f"src/open_energy_planner/{domain}/future.py"))
    )
    actual = selected_suites(path_profile(PurePosixPath(f"src/open_energy_planner/{module}")))
    assert actual == ordinary | {"databases"}


def test_undeclared_flat_storage_module_remains_full():
    """An undeclared root storage module cannot bypass the conservative ownership fallback."""
    assert path_profile(PurePosixPath("src/open_energy_planner/storage.py")) == "full"


def test_mixed_solar_and_recorder_change_keeps_real_history(monkeypatch):
    """The complete PR union cannot omit Recorder merely because another path is native."""
    paths = [
        "src/open_energy_planner/solar/future.py",
        "tests/custom_component/integration/history/test_future.py",
    ]
    records = b"".join(
        f":100644 100644 {'3' * 40} {'4' * 40} M\0{path}\0".encode() for path in paths
    )
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *args, **kwargs: records
    )
    monkeypatch.setattr("script.ci.ci_scope.immutable_test_sources", lambda *args: {})
    suites = selected_suites(select_profile("1" * 40, "2" * 40))
    assert {"ha-history", "unit-app", "unit-ha-contract", "browser"} <= suites
    assert "ha-thermal" not in suites


def test_shared_forecast_policy_selects_native_behavior():
    """Forecast age admission retains its app regressions without unrelated HA or database work."""
    suites = selected_suites(
        path_profile(PurePosixPath("src/open_energy_planner/installation/forecast_policy.py"))
    )
    assert suites == {"ci", "unit-app"}
