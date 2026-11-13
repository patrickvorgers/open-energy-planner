"""EV-owned recovery mapping keeps log advice fixed and authenticated source context useful."""

from open_energy_planner.appliances.ev.notifications import explain_problem, forecast_boundary_issue


def test_forecast_boundary_notice_has_fixed_actionable_log_advice():
    """The coordinator receives bounded domain text with an existing authenticated recovery path."""
    notice = explain_problem("ev_forecast_boundary_unavailable")
    assert notice.title == "Charge-point consumption cannot be separated"
    assert notice.action_label == "Check EV charging"
    assert notice.action_path == "/ev"
    assert notice.level == "warning"
    assert "Energy meter links" in notice.message
    assert "No historical EV fallback is used" in notice.message
    assert "sensor." not in notice.message
    assert explain_problem("sources_not_configured") is None


def test_authenticated_boundary_projection_preserves_configured_names_and_meter_identity():
    """Source context is available where the user can identify and repair their configuration."""
    message = forecast_boundary_issue(
        ({"energy_entity": "sensor.private_wallbox_meter"},),
        {"sensor.private_wallbox_meter": "Private garage charge point"},
    )
    assert "Private garage charge point (sensor.private_wallbox_meter)" in message
    assert "Energy meter links" in message
    assert "Planning is paused" in message
