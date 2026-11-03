"""Compose real notification providers for isolated lifecycle and browser fixtures."""

from open_energy_planner.contracts.notifications import (
    contract_notifications,
    explain_contract_problem,
    negative_export_notifications,
)
from open_energy_planner.notifications.collection import NotificationCenter
from open_energy_planner.runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    explain_planning_problem,
    planning_notifications,
)
from open_energy_planner.runtime.diagnostics.version_notifications import version_notifications


def notification_document(
    status,
    reason,
    blocked_since,
    *,
    block_reason=None,
    history_issue=None,
    version_status=None,
    negative_export_meters=(),
):
    """Provide the existing explicit fixture inputs through actual source registration and serialization."""
    state = PlanningStatus(status, reason, blocked_since, block_reason)
    center = NotificationCenter()
    center.register("versions", lambda: version_notifications(version_status == "mismatch"))
    center.register(
        "contract-statistics", lambda: negative_export_notifications(negative_export_meters)
    )
    center.register(
        "contracts",
        lambda: contract_notifications(state, history_issue),
        explain=explain_contract_problem,
    )
    center.register(
        "planning",
        lambda: planning_notifications(state, center.explain),
        explain=explain_planning_problem,
        fallback=True,
    )
    return center.document()


def measurement_evidence(now):
    """Give two required independent failures and one optional fault that must not be called a blocker."""
    from open_energy_planner.home_assistant.observation_wire import (
        parse_configuration,
        parse_report,
    )

    config = parse_configuration(
        {
            "bindings": [
                {"key": "grid.power", "entity_id": "sensor.grid", "quantity": "power"},
                {"key": "battery.soc", "entity_id": "sensor.soc", "quantity": "soc"},
                {
                    "key": "pv.power",
                    "entity_id": "sensor.pv",
                    "quantity": "power",
                    "required": False,
                },
            ]
        }
    )
    report = parse_report(
        {
            "config_revision": config.revision,
            "captured_at": now.isoformat(),
            "readings": [],
            "issues": [
                {"key": "grid.power", "entity_id": "sensor.grid", "reason": "missing_entity"},
                {"key": "battery.soc", "entity_id": "sensor.soc", "reason": "unavailable"},
                {"key": "pv.power", "entity_id": "sensor.pv", "reason": "unsupported_unit"},
            ],
        },
        config,
        now,
    )
    return config, report
