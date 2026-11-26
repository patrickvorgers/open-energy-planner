"""Explain planning availability and project only confirmed coordinator lifecycle evidence."""

from dataclasses import dataclass
from datetime import datetime

from open_energy_planner.appliances.ev.fallback import EV_CONTROL_DEFERRED, EV_FALLBACK_REASONS
from open_energy_planner.notifications.model import Lifecycle, NoticeText, Notification


@dataclass(frozen=True)
class PlanningStatus:
    """Share coordinator evidence without exposing its worker, solver or mutation methods.

    Only the coordinator's successful publication sets ``ready``. A recovering
    calculation retains ``blocked_since`` and ``block_reason`` until that point.
    The start is aware and process-local. An uninitialized status is unknown,
    rather than proof that a previous problem has resolved after a restart.
    """

    status: str
    reason: str | None
    blocked_since: datetime | None
    block_reason: str | None = None
    has_published: bool = False
    block_text: NoticeText | None = None
    ev_fallback_active: bool = False

    @property
    def unknown(self):
        """Withhold recovery confirmation until there is a usable result or a known cause."""
        if self.status == "calculating" and self.has_published and self.blocked_since is None:
            return False
        return self.status not in {"ready", "calculating", "not_ready", "failed"} or (
            self.status != "ready" and self.reason is None and self.block_reason is None
        )

    @property
    def cause(self):
        """Keep the original cause key during recovery, independently of current wording."""
        return self.block_reason if self.status == "calculating" else self.reason


def planning_notice(state: PlanningStatus, text: NoticeText) -> Notification:
    """Keep a diagnosed planning cause active until publication, including recovery calculation."""
    recovering = state.status == "calculating"
    content = (
        NoticeText(
            "Calculating a new energy plan",
            (
                ""
                if state.cause in {"worker_timeout", "reserve_recovery_time_limit"}
                else "The inputs are usable again. "
            )
            + "A new energy plan is being calculated and checked. "
            "This can take a few minutes. Planning has not yet completed successfully.",
            "View planning status" if text.action_path else None,
            text.action_path,
            "info",
        )
        if recovering
        else NoticeText(
            text.title,
            text.message + "\n\nNo new energy plan can be published while this condition remains.",
            text.action_label,
            text.action_path,
            text.level,
        )
    )
    return Notification(
        f"planning:{state.cause}",
        "planning_resuming" if recovering else state.cause,
        content,
        state=Lifecycle.RECOVERING if recovering else Lifecycle.ACTIVE,
        blocks=("planning",),
        since=state.blocked_since,
    )


def planning_notifications(state: PlanningStatus, explain):
    """Project coordinator evidence with module advice as a fallback to authoritative domain notices."""
    if state.ev_fallback_active:
        return (
            Notification(
                "planning:ev_control_deferred",
                EV_CONTROL_DEFERRED,
                NoticeText(
                    "Energy plan active without EV charging control",
                    "The plan includes expected charging consumption. OEP retries charging "
                    "optimization at the next planning round. You don't need to do anything now.",
                    "View EV charging",
                    "/ev",
                    "info",
                ),
            ),
        )
    if state.unknown:
        return None
    if state.status == "ready" or not state.cause:
        return ()
    text = explain(state.cause) or state.block_text or explain_planning_problem(state.cause)
    return (planning_notice(state, text),)


def explain_planning_problem(reason: str, _detail: str | None = None) -> NoticeText:
    """Explain assembled planning inputs and calculation outcomes, with an honest unknown fallback."""
    if reason in EV_FALLBACK_REASONS:
        return NoticeText(
            "Waiting for a new energy plan",
            "OEP could not confirm the charging schedule or publish a checked fallback. "
            "It retries automatically at the next planning round. You don't need to do anything now.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "configuration_changed":
        return NoticeText(
            "Waiting for inputs for the updated settings",
            "The installation configuration or planning strategy changed during calculation. "
            "Planning retries automatically with the current configuration.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "report_configuration_mismatch":
        return NoticeText(
            "Waiting for a matching measurement report",
            "The latest measurement report refers to a different installation configuration. "
            "Planning retries automatically when a matching report arrives.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "planning_work_changed":
        return NoticeText(
            "Waiting for a plan with current device work",
            "The appliance, charging or heating work no longer matches the calculation. "
            "Planning retries automatically with the current inputs.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "inputs_unavailable":
        return NoticeText(
            "Waiting for current planning inputs",
            "The current planning inputs are not available for the final publication check.\n\n"
            "OEP automatically rechecks incoming data and can retry once usable inputs are available. "
            "If this persists, check the integration connection and the source warnings in Planning.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "battery_dispatch_unavailable":
        return NoticeText(
            "Waiting to recheck battery limits",
            "The latest battery measurements no longer allow the remaining charging or discharging "
            "within its energy limits. Battery charge can change while a calculation runs.\n\n"
            "OEP retries automatically with current "
            "measurements at a later planning round. This is usually temporary. "
            "If it keeps recurring, check the battery state and configured limits.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "ev_capacity_unavailable":
        return NoticeText(
            "Waiting to recheck vehicle charging capacity",
            "The vehicle's latest charge level leaves too little room for the remaining charging "
            "commands before they expire. The calculated plan was not published.\n\nOEP retries "
            "automatically with current measurements at a later planning round. "
            "If this persists, check the vehicle's charge level and charging request in EV charging.",
            "Check EV charging",
            "/ev",
            "info",
        )
    if reason == "ev_session_unavailable":
        return NoticeText(
            "Vehicle charging session changed",
            "A charging session used by the calculation is no longer available. "
            "The vehicle may have disconnected or its charging request may have changed.\n\n"
            "OEP rechecks sessions automatically and can plan again when the required session "
            "inputs are usable. If this was unexpected, check the vehicle connection and request in EV charging.",
            "Check EV charging",
            "/ev",
        )
    if reason == "thermal_dispatch_window_elapsed":
        return NoticeText(
            "Heating start passed during calculation",
            "A scheduled heating start passed while the plan was being calculated. "
            "The result was not published.\n\nOEP retries automatically with a current schedule "
            "at a later planning round. This is usually temporary. "
            "If it keeps recurring, check the heating deadlines and calculation duration.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "candidate_input_mismatch":
        return NoticeText(
            "Calculated plan could not be matched to its inputs",
            "The calculated result could not be matched to the data for this planning round, "
            "so it was not published. This is an internal consistency check, not evidence "
            "of a missing sensor. Check Activity and the app log if it recurs.",
            "View activity",
            "/activity",
        )
    if reason == "battery_calibration_changed":
        return NoticeText(
            "Updating the battery calibration plan",
            "Battery calibration progressed. A replacement plan is being calculated and checked "
            "before normal battery commands can resume.",
            "View planning",
            "/overview",
            "info",
        )
    if reason == "battery_calibration_unreadable":
        return NoticeText(
            "Battery calibration progress is unavailable",
            "The retained calibration progress for the configured batteries is missing or unreadable. "
            "Planning cannot assume a completed calibration. Check the active database, "
            "or disable periodic calibration in Installation > Batteries and save before enabling it again. "
            "This explicitly starts a new calibration schedule.",
            "Check battery calibration",
            "/setup",
        )
    if reason == "waiting_for_energy_configuration":
        return NoticeText(
            "Waiting for Home Assistant Energy configuration",
            "The Open Energy Planner integration has not supplied the Home Assistant Energy dashboard configuration. "
            "Check that the integration is loaded and connected, then refresh Home Assistant data "
            "in Installation settings. Planning retries automatically.",
            "Check installation",
            "/setup",
            "info",
        )
    if reason == "energy_configuration_changed":
        return NoticeText(
            "Home Assistant Energy configuration needs synchronization",
            "The saved installation no longer matches the Home Assistant Energy dashboard, "
            "or required installation settings are incomplete. Refresh Home Assistant data and "
            "review the discovered devices in Installation settings. Home Assistant Energy "
            "remains the source of truth for sensor membership.",
            "Review installation",
            "/setup",
        )
    if reason in {"demand_forecast_pending", "demand_forecast_expired"}:
        return NoticeText(
            "Household consumption forecast is unavailable",
            "The household consumption forecast has not arrived or no longer covers the current "
            "planning period. Review its source and model status in Forecast quality. "
            "Planning retries automatically when an updated forecast is available.",
            "Review household forecast",
            "/quality",
        )
    if reason == "solar_configuration_required":
        return NoticeText(
            "Solar installation settings are incomplete",
            "Solar producers are configured in Home Assistant Energy, but their installation "
            "settings are incomplete. Review the discovered solar sources and inverter limits "
            "in Installation settings.",
            "Review solar installation",
            "/setup",
        )
    if reason == "market_prices_unavailable":
        return NoticeText(
            "Market prices are unavailable",
            "No usable market prices are available. Check the price sources in Installation settings. "
            "Planning retries automatically when prices become available.",
            "Check price sources",
            "/setup",
        )
    if reason in {"sources_not_configured", "forecasts_not_configured"}:
        return NoticeText(
            "Planning setup is incomplete",
            "Complete the installation and its contract before an energy plan can be calculated.",
            "Open installation",
            "/setup",
            "info",
        )
    if reason in {
        "waiting_for_measurements",
        "installation_measurements_pending",
        "demand_forecast_pending",
    }:
        return NoticeText(
            "Waiting for planning inputs",
            "Planning is waiting for current Home Assistant measurements or a household forecast. "
            "Planning retries automatically. If this continues, check the integration and selected sensors.",
            "Check installation",
            "/setup",
            "info",
        )
    if reason in {
        "required_measurement_unavailable",
        "required_measurement_expired",
        "report_expired",
        "battery_sources_required",
        "battery_state_outside_configured_bounds",
    }:
        return NoticeText(
            "Required measurements are unavailable",
            "A required sensor is missing, stale or outside its configured limits. "
            "Check the Home Assistant integration, sensor availability and installation settings.",
            "Check installation",
            "/setup",
        )
    if reason in {"publication_inputs_expired", "stale_input", "input_expired_during_solve"}:
        return NoticeText(
            "Planning inputs expired before publication",
            "The measurements became too old or the first transfer interval elapsed before "
            "a plan could be published. Planning retries automatically with current inputs and a new usable interval. "
            "This is usually temporary and needs no settings change. If it keeps recurring, "
            "check measurement freshness and calculation duration in Planning.",
            "View planning",
            "/overview",
            "info",
        )
    if "forecast" in reason:
        return NoticeText(
            "Forecast coverage is unavailable",
            "The available forecasts or prices do not cover a usable planning period. "
            "Check forecast quality and source availability. Planning retries automatically.",
            "Review forecasts",
            "/quality",
        )
    if reason == "worker_timeout":
        return NoticeText(
            "Planning calculation timed out",
            "The calculation did not return a result within its time budget. "
            "Planning retries automatically. If this keeps happening, review the failed rounds "
            "in Planning performance.",
            "View calculation history",
            "/planning-performance",
        )
    if reason == "reserve_recovery_time_limit":
        return NoticeText(
            "Battery reserve calculation timed out",
            "The calculation reached its time budget while checking how to restore the battery reserve. "
            "Planning retries automatically. If this keeps happening, review the failed rounds "
            "in Planning performance.",
            "View calculation history",
            "/planning-performance",
        )
    if reason in {"planning_error", "infeasible", "no_usable_plan"}:
        return NoticeText(
            "No usable energy plan",
            "The calculation failed or could not find a usable plan. Check installation limits "
            "and device requirements. The app log contains calculation diagnostics.",
            "Review installation",
            "/setup",
        )
    return NoticeText(
        "Planning is blocked",
        "OEP cannot publish a new plan, but this status has no specific explanation yet. "
        "It does not identify a missing sensor or incorrect setting.\n\n"
        "If the message persists, check Activity and the app log for the cause before changing settings.",
        "View activity",
        "/activity",
    )
