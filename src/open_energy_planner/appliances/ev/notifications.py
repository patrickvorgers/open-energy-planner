"""Own fixed log-safe EV notices and source context for authenticated EV views."""

from open_energy_planner.notifications.model import NoticeText

EV_FORECAST_UNAVAILABLE = "ev_forecast_boundary_unavailable"
EV_METER_MISSING = "ev_energy_meter_missing"

EXECUTION_DEVIATION_ADVICE = (
    "Measured charging differs from the plan. Check the vehicle charge limit, "
    "charge-point status and Home Assistant charging automation. "
    "OEP replans but cannot determine the physical cause."
)


def explain_problem(reason):
    """Return bounded advice safe for the planning coordinator's diagnostic log."""
    if reason == EV_FORECAST_UNAVAILABLE:
        return NoticeText(
            "Waiting for the charge-point forecast",
            "Planning is paused until OEP can separate charging from household consumption. "
            "Wait for OEP to retry automatically. If this persists, check Consumption and "
            "cycles for forecast or history issues.",
            "Check consumption forecasts",
            "/appliance-settings",
        )
    if reason == EV_METER_MISSING:
        return NoticeText(
            "Charge-point meter missing from Home Assistant Energy",
            "Planning is paused. Open EV charging settings to see which meter is missing. "
            "Add it to Home Assistant Energy, then choose Refresh Home Assistant sources in OEP.",
            "Open EV charging settings",
            "/appliance-settings/ev",
        )
    return None


def forecast_boundary_issue(reason, missing_meters, names):
    """Add confirmed missing meter identities only to the authenticated settings view."""
    notice = explain_problem(reason)
    if notice is None:
        return None
    if reason == EV_METER_MISSING and missing_meters:
        sources = ", ".join(
            f"{names.get(meter, 'Charge point')} ({meter})" for meter in missing_meters
        )
        return (
            f"Planning is paused. Home Assistant Energy is missing: {sources}. "
            "Add the missing meter in Home Assistant Energy, then choose "
            "Refresh Home Assistant sources here."
        )
    return notice.message
