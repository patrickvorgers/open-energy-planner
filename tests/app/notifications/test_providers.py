"""Keep domain recognition separate from the shared source lifecycle contract."""

from datetime import UTC, datetime

import pytest

from open_energy_planner.contracts.notifications import (
    contract_notifications,
    explain_contract_problem,
    negative_export_notifications,
)
from open_energy_planner.notifications.collection import NotificationCenter
from open_energy_planner.notifications.model import NoticeText
from open_energy_planner.runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    explain_planning_problem,
    planning_notice,
    planning_notifications,
)
from open_energy_planner.runtime.diagnostics.version_notifications import version_notifications


@pytest.mark.parametrize(
    "reason,resource,destination",
    [
        ("waiting_for_energy_configuration", "Home Assistant Energy dashboard", "/setup"),
        ("energy_configuration_changed", "Home Assistant Energy dashboard", "/setup"),
        ("demand_forecast_pending", "household consumption forecast", "/quality"),
        ("demand_forecast_expired", "household consumption forecast", "/quality"),
        ("solar_configuration_required", "solar sources and inverter limits", "/setup"),
    ],
)
def test_known_input_problems_name_the_responsible_resource_and_recovery_page(
    reason, resource, destination
):
    """Distinct onboarding, demand and PV faults name what needs attention instead of a generic unusable-input message."""
    text = explain_planning_problem(reason)
    assert resource in text.message
    assert text.action_path == destination
    assert "Required planning inputs or saved settings are unusable" not in text.message


@pytest.mark.parametrize("status", ["not_ready", "calculating"])
def test_planning_automatic_retry_preserves_optional_recovery_destination(status):
    """An explanation without a page remains valid while blocked and while publication is recovering."""
    since = datetime(2026, 10, 31, 12, tzinfo=UTC)
    state = PlanningStatus(status, "input_missing", since, "input_missing")
    notice = planning_notice(
        state, NoticeText("Waiting for input", "This input retries automatically.")
    )
    assert notice.key == "planning:input_missing"
    assert notice.text.action_path is None
    assert notice.text.action_label is None
    assert notice.blocks == ("planning",)
    assert notice.state == ("recovering" if status == "calculating" else "active")


def test_contract_owns_its_cause_until_publication_confirms_restored_operation():
    """Contract confirmation and calculation cannot clear the same stable problem before ready publication."""
    since = datetime(2026, 10, 31, 12, tzinfo=UTC)
    cause = "bonus_statistics_prefix_required"
    blocked = PlanningStatus("not_ready", cause, since, cause)
    notice = contract_notifications(blocked)[0]
    assert notice.key == "planning:bonus_statistics_prefix_required"
    assert notice.blocks == ("planning",)
    assert notice.text.action_path == "/contract"
    recovering = contract_notifications(PlanningStatus("calculating", None, since, cause))[0]
    assert recovering.key == notice.key
    assert recovering.state == "recovering"
    assert recovering.since == since
    assert "calculating and checking a new energy plan" in recovering.text.message
    assert "not yet completed successfully" in recovering.text.message
    assert contract_notifications(PlanningStatus("ready", "validated", None)) == ()


def test_contract_and_planner_do_not_report_the_same_propagated_cause_twice():
    """Source registration delegates contract recognition and keeps generic fallback ownership separate."""
    state = PlanningStatus("failed", "tariff_coverage_gap", datetime(2026, 10, 31, tzinfo=UTC))
    center = NotificationCenter()
    center.register(
        "planning",
        lambda: planning_notifications(state, center.explain),
        explain=explain_planning_problem,
        fallback=True,
    )
    center.register(
        "contracts", lambda: contract_notifications(state), explain=explain_contract_problem
    )
    notices = center.document()["notifications"]
    assert len(notices) == 1
    assert notices[0]["owner"] == "contracts"
    assert notices[0]["code"] == "tariff_coverage_gap"
    assert center.explain("bonus_counter_baseline_required").title == "Export bonus needs attention"
    assert center.explain("planning_error").title == "No usable energy plan"
    assert explain_contract_problem("future_unknown_condition") is None


def test_normal_recalculation_is_quiet_but_restart_without_evidence_is_unknown():
    """A previous published plan permits normal quiet work while a fresh process cannot claim recovery."""
    center = NotificationCenter()
    normal = PlanningStatus("calculating", None, None, has_published=True)
    assert planning_notifications(normal, center.explain) == ()
    assert contract_notifications(normal) == ()
    restart = PlanningStatus("calculating", None, None)
    assert planning_notifications(restart, center.explain) is None
    assert contract_notifications(restart) is None
    assert planning_notifications(PlanningStatus("not_ready", None, None), center.explain) is None


def test_known_planning_cause_has_module_advice_when_its_owner_cannot_be_read():
    """A first-read source fault cannot hide a diagnosed blocker before any owner cache exists."""
    state = PlanningStatus("not_ready", "bonus_statistics_prefix_required", None)
    center = NotificationCenter()
    center.register("contracts", lambda: None, explain=explain_contract_problem)
    center.register(
        "planning", lambda: planning_notifications(state, center.explain), fallback=True
    )
    document = center.document()
    assert document["unavailable_sources"] == ["contracts"]
    assert len(document["notifications"]) == 1
    notice = document["notifications"][0]
    assert notice["key"] == "planning:bonus_statistics_prefix_required"
    assert notice["owner"] == "planning"
    assert notice["title"] == "Confirm earlier export"
    assert notice["action_path"] == "/contract"
    assert notice["blocks"] == ["planning"]


def test_negative_statistics_are_an_advisory_without_changing_budget_evidence():
    """Measured negative statistics retain their distinct nonblocking contract explanation."""
    state = PlanningStatus("ready", "validated", None)
    notice = negative_export_notifications(("sensor.export",))[0]
    assert notice.blocks == ()
    assert notice.text.level == "warning"
    assert "Planning can continue" in notice.text.message
    assert notice.detail == "sensor.export"
    assert contract_notifications(state) == ()


def test_negative_statistics_remain_visible_while_planning_status_is_unknown():
    """Accepted independent statistics evidence cannot be hidden by a restarting coordinator."""
    state = PlanningStatus("calculating", None, None)
    center = NotificationCenter()
    center.register("contracts", lambda: contract_notifications(state))
    meters = [("sensor.export",)]
    center.register("contract-statistics", lambda: negative_export_notifications(meters[0]))
    document = center.document()
    assert document["unavailable_sources"] == ["contracts"]
    assert len(document["notifications"]) == 1
    notice = document["notifications"][0]
    assert notice["code"] == "bonus_statistics_negative_difference"
    assert notice["blocks"] == []
    meters[0] = ()
    assert center.document()["notifications"] == []


def test_missing_version_contact_is_not_proof_that_an_identified_mismatch_recovered():
    """A retained mismatch survives contact expiry and clears only with fresh matching evidence."""
    center = NotificationCenter()
    state = [True, True]
    center.register("versions", lambda: version_notifications(state[0], available=state[1]))
    original = center.document()["notifications"][0]
    state[:] = [True, False]
    assert center.document()["notifications"][0]["key"] == original["key"]
    state[:] = [False, False]
    uncertain = center.document()
    assert uncertain["unavailable_sources"] == ["versions"]
    assert uncertain["notifications"][0]["stale"] is True
    state[:] = [False, True]
    assert center.document() == {"notifications": [], "unavailable_sources": []}
