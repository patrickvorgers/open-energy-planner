"""Select Home Assistant recovery advice from the latest synchronized snapshot."""


def _demand_notification(demand):
    """Explain forecast and history failures in their recovery order."""
    if demand.get("issue"):
        if demand["issue"] == "demand_boundary_inconsistent":
            configuration = demand.get("configuration") or {}
            names = ", ".join(
                sorted(
                    {
                        term["entity_id"]
                        for term in (
                            *configuration.get("terms", []),
                            *configuration.get("exclusions", []),
                        )
                    }
                )
            )
            return (
                "demand_boundary_inconsistent",
                "Household energy history contains negative balances beyond the "
                "measurement allowance. Those hours are excluded from forecast "
                "training. Check the Home Assistant Energy balance for overlapping "
                "sources and separately measured devices"
                + (": " + names if names else "")
                + ". Usable hours, when available, are retained. When too little history "
                "remains, forecasting uses the annual-consumption startup estimate. "
                "Recorder data is unchanged. A backup restore is not required.",
            )
        if demand["issue"] == "demand_configuration_unreadable":
            return (
                "demand_forecast_unusable",
                (
                    "The saved household energy configuration cannot be read. "
                    "Restore its settings backup or explicitly save corrected household "
                    "settings in the app. Historical measurements are retained."
                ),
            )
        return (
            "demand_forecast_unusable",
            (
                "The household forecast could not be updated. Open Forecast quality "
                "in the app and check the app logs. The update retries automatically. "
                "Do not restore or remove measured history to resolve a forecast error."
            ),
        )
    if demand.get("history_issues"):
        names = ", ".join(sorted(demand["history_issues"]))
        return (
            "demand_history_unavailable",
            "Household energy history cannot be read from Recorder for: "
            + names
            + ". Check Recorder and select cumulative energy sensors with "
            "long-term statistics. The available forecast remains active. "
            "Without usable history the startup estimate is used.",
        )
    return None


def _solar_notification(solar):
    """Explain unreadable solar settings or actionable Recorder faults."""
    if solar.get("issue") == "solar_configuration_unreadable":
        return (
            "solar_configuration_unreadable",
            "Restore the saved solar configuration or explicitly save corrected settings in the app.",
        )
    affected = sorted(
        entity
        for entity, issue in solar.get("history_issues", {}).items()
        if issue
        in {
            "unsupported_statistics",
            "recorder_unavailable",
            "history_continuity_unavailable",
            "history_cache_unreadable",
        }
    )
    if affected:
        return (
            "solar_history_unavailable",
            "Check Recorder and the selected cumulative solar production sensors: "
            + ", ".join(affected)
            + ". The physical startup forecast remains available when weather is usable.",
        )
    return None


def select_snapshot_notification(
    data, planning_status, observation_issues, required_sources
) -> tuple[str, str] | None:
    """Return the highest-priority action and its message without updating Home Assistant."""
    actionable = [
        issue
        for issue in observation_issues
        if (issue.get("key"), issue.get("entity_id")) in required_sources
        and issue.get("reason")
        in (
            "missing_entity",
            "unsupported_unit",
            "invalid_timestamp",
            "invalid_soc",
        )
    ]
    if data.get("installation", {}).get("issue"):
        return (
            "installation_configuration_unreadable",
            "Restore the installation settings backup or explicitly save corrected settings. No physical limits are guessed.",
        )
    if (planning_status or {}).get("reason") == "energy_configuration_changed":
        return (
            "energy_configuration_changed",
            "The Home Assistant Energy installation needs configuration. Complete its Energy dashboard and open the OEP app to confirm missing device ratings and measurement bindings. Historical plans are retained.",
        )
    if actionable:
        names = ", ".join(issue["entity_id"] for issue in actionable)
        return (
            "measurement_sources_unusable",
            "Check the selected measurement sources, units and timestamps: " + names,
        )
    if data.get("bonus_history", {}).get("issue") == "bonus_counter_cache_unreadable":
        return (
            "bonus_counter_cache_unreadable",
            (
                "The saved bonus counter cannot be read. Restore its backup or "
                "explicitly confirm a recovered manual baseline in the app. "
                "Automatic history initialization is blocked to preserve saved evidence."
            ),
        )
    if notice := _demand_notification(data.get("demand", {})):
        return notice
    if notice := _solar_notification(data.get("solar", {})):
        return notice
    if data.get("bonus_history", {}).get("issue"):
        return (
            "bonus_history_unavailable",
            (
                "Export history cannot establish the bonus counter. Check Recorder "
                "and the selected export sensors. The app accepts manual export only "
                "for a missing period before a validated statistics reference."
            ),
        )
    if "fixed_pv_export_limit" in data.get("installation", {}).get("warnings", []):
        return (
            "fixed_pv_export_limit",
            (
                "The grid export limit is below the combined fixed-PV inverter ratings. "
                "Sunny intervals can have no feasible plan when local consumption and storage cannot absorb production. "
                "Check the ratings and configure PV on/off permission only if your automation can implement it. "
                "Automatic proportional export limiting is not modeled. A grid limit does not grant PV control permission."
            ),
        )
    if data.get("savings_status", {}).get("action_required"):
        return (
            "savings_unavailable",
            "The savings comparison needs attention: "
            + str(
                data["savings_status"].get("issue")
                or "No detailed settlement explanation is available"
            )
            + ". Open Plan and savings in the app. Physical commands remain governed by their own plan validity.",
        )
    return None
