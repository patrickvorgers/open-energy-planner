"""Explain planning availability and project only confirmed coordinator lifecycle evidence."""

from dataclasses import dataclass
from datetime import datetime

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
            "The inputs are usable again. OEP is calculating and checking a new energy "
            "plan. This can take a few minutes. Planning has not yet completed successfully.",
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
    if state.unknown:
        return None
    if state.status == "ready" or not state.cause:
        return ()
    text = explain(state.cause) or explain_planning_problem(state.cause)
    return (planning_notice(state, text),)


def explain_planning_problem(reason: str, _detail: str | None = None) -> NoticeText:
    """Explain assembled planning inputs and calculation outcomes, with an honest unknown fallback."""
    if reason == "waiting_for_energy_configuration":
        return NoticeText(
            "Waiting for Home Assistant Energy configuration",
            "The OEP integration has not supplied the Home Assistant Energy dashboard configuration. "
            "Check that the integration is loaded and connected, then refresh Home Assistant data "
            "in Installation settings. OEP retries automatically.",
            "Check installation",
            "/setup",
            "info",
        )
    if reason == "energy_configuration_changed":
        return NoticeText(
            "Home Assistant Energy configuration needs synchronization",
            "The saved OEP installation no longer matches the Home Assistant Energy dashboard, "
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
            "OEP retries automatically when an updated forecast is available.",
            "Review household forecast",
            "/quality",
        )
    if reason == "solar_configuration_required":
        return NoticeText(
            "Solar installation settings are incomplete",
            "Solar producers are configured in Home Assistant Energy, but their OEP installation "
            "settings are incomplete. Review the discovered solar sources and inverter limits "
            "in Installation settings.",
            "Review solar installation",
            "/setup",
        )
    if reason == "market_prices_unavailable":
        return NoticeText(
            "Market prices are unavailable",
            "No usable market prices are available. Check the price sources in Installation settings. "
            "OEP retries automatically when prices become available.",
            "Check price sources",
            "/setup",
        )
    if reason in {"sources_not_configured", "forecasts_not_configured"}:
        return NoticeText(
            "Planning setup is incomplete",
            "Complete the installation and its contract before OEP can calculate an energy plan.",
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
            "OEP is waiting for current Home Assistant measurements or a household forecast. "
            "It retries automatically. If this continues, check the integration and selected sensors.",
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
            "a plan could be published. OEP retries with current inputs and a new usable interval. "
            "Check Planning for the next calculation and its current status.",
            "View planning",
            "/overview",
            "info",
        )
    if "forecast" in reason:
        return NoticeText(
            "Forecast coverage is unavailable",
            "The available forecasts or prices do not cover a usable planning period. "
            "Check forecast quality and source availability. OEP retries automatically.",
            "Review forecasts",
            "/quality",
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
        "Required planning inputs or saved settings are unusable. Check the installation "
        "and Home Assistant integration.",
        "Check installation",
        "/setup",
    )
