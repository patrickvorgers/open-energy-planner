"""Explain rejected sensor evidence without exposing arbitrary provider diagnostics."""

from open_energy_planner.notifications.model import NoticeText

from .observations import MAX_OBSERVATION_AGE_SECONDS, capture_deadline

_SOURCE_PROBLEMS = {
    "missing_entity": "The configured sensor does not exist in Home Assistant.",
    "unavailable": "Home Assistant reports unknown or unavailable instead of a usable value.",
    "unsupported_unit": "The sensor unit is not supported for its configured measurement role.",
    "invalid_number": "The sensor value is not a finite number.",
    "invalid_attributes": "The sensor attributes cannot be read.",
    "invalid_timestamp": "The sensor timestamp is missing or invalid.",
    "future_measurement": "The sensor timestamp is in the future. Check the source clock.",
    "invalid_soc": "The state of charge is outside 0 to 100 percent.",
    "negative_energy": "The cumulative energy counter is negative.",
    "stale_measurement": "The integration rejected an old sensor timestamp instead of supplying its current HA value. Update the app and integration from the same bundle.",
}


def explain_measurement_problem(reason, configuration, report, now, names=None):
    """Identify each affected required source from accepted current evidence, without changing admission.

    HA timestamps alone never establish communication failure. Sensor provenance
    remains distinct from the integration's current capture deadline.
    Unknown issue codes get safe wording rather than echoing untrusted details.
    Optional names are current validated catalogue labels. They supplement exact
    sensor IDs and never participate in measurement admission.
    """
    if reason not in {
        "required_measurement_unavailable",
        "required_measurement_expired",
        "report_expired",
        "waiting_for_measurements",
    }:
        return None
    if report is None or reason == "report_expired":
        return NoticeText(
            "Waiting for Home Assistant measurements",
            "The Open Energy Planner integration has not supplied a current measurement report. "
            f"Reports must be less than {MAX_OBSERVATION_AGE_SECONDS // 60} minutes old. "
            "Check that the Open Energy Planner integration is loaded and connected. "
            "This concerns communication between Home Assistant and this app, not the age "
            "of an unchanged sensor value. Planning retries automatically.",
            "Check installation",
            "/setup",
        )
    if configuration is None or report.config_revision != configuration.revision:
        return None
    issues = {issue.key: issue.reason for issue in report.issues}
    readings = {reading.key: reading for reading in report.readings}
    problems = []
    for spec in configuration.bindings:
        if not spec.required:
            continue
        reading = readings.get(spec.key)
        problem = None
        if spec.key in issues:
            problem = _SOURCE_PROBLEMS.get(
                issues[spec.key], "The integration could not supply a usable sensor value."
            )
        elif reading is None:
            problem = "The integration report contains no reading for this configured sensor."
        elif now >= capture_deadline(spec, report):
            problem = "The integration capture has expired. Waiting for a new report."
        if problem:
            name = (names or {}).get(spec.entity_id)
            identity = (
                f"{name} ({spec.entity_id})" if name and name != spec.entity_id else spec.entity_id
            )
            problems.append(f"{identity}: {problem}")
    if not problems:
        return None
    return NoticeText(
        "Required measurements are unavailable",
        "Check these sensors in Home Assistant and their selection in Installation settings. "
        "Planning retries automatically when usable readings arrive.\n\n" + "\n\n".join(problems),
        "Check installation",
        "/setup",
    )


def explain_live_problem(live, reason, catalogue):
    """Explain accepted live evidence using current labels without owning input admission."""
    from .names import source_names

    if live is None:
        return None
    return explain_measurement_problem(
        reason, live.configuration, live.report, live.clock(), source_names(catalogue)
    )
