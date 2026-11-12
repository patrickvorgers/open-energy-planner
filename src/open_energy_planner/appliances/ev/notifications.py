"""Own fixed log-safe EV notices and source context for authenticated EV views."""

from open_energy_planner.notifications.model import NoticeText

EXECUTION_DEVIATION_ADVICE = (
    "Measured charging differs from the plan. Check the vehicle charge limit, "
    "charge-point status and Home Assistant charging automation. "
    "OEP replans but cannot determine the physical cause."
)


def explain_problem(reason):
    """Return bounded advice safe for the planning coordinator's diagnostic log."""
    if reason != "ev_forecast_boundary_unavailable":
        return None
    return NoticeText(
        "Charge-point consumption cannot be separated",
        "Planning is paused because historical charging consumption cannot be removed "
        "from the household forecast. Open EV charging to identify the affected configured "
        "charge points. Check their Energy meter links and consumption forecast readiness. "
        "Planning resumes when complete component evidence is available. "
        "No historical EV fallback is used.",
        "Check EV charging",
        "/ev",
        "warning",
    )


def forecast_boundary_issue(points, names):
    """Name configured meters only in the authenticated recovery view, never in log advice."""
    sources = ", ".join(
        f"{names.get(point['energy_entity'], 'Charge point')} ({point['energy_entity']})"
        for point in points
    )
    return (
        f"Historical charging consumption cannot be separated for {sources}. "
        "Check these charge points' Energy meter links and consumption forecast readiness. "
        "Planning is paused until separation is available. No historical EV fallback is used."
    )
