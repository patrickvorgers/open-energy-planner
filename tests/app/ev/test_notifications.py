"""EV notices provide recovery advice without exposing private meter identities in logs."""

from open_energy_planner.appliances.ev.notifications import explain_problem, forecast_boundary_issue


def test_forecast_wait_notice_tells_user_to_wait_and_where_to_check():
    """A forecast delay points to diagnostics only if retrying does not resolve it."""
    notice = explain_problem("ev_forecast_boundary_unavailable")
    assert notice.title == "Waiting for the charge-point forecast"
    assert "automatic retry" in notice.message
    assert "If this persists" in notice.message
    assert notice.action_path == "/appliance-settings"
    assert "meter links" not in notice.message
    assert explain_problem("sources_not_configured") is None


def test_missing_meter_notice_keeps_identity_in_authenticated_settings():
    """An absent Energy meter gives specific recovery advice and stays private in logs."""
    reason = "ev_energy_meter_missing"
    meter = "sensor.example_wallbox_meter"
    notice = explain_problem(reason)
    assert notice.title == "Charge-point meter missing from Home Assistant Energy"
    assert notice.action_path == "/appliance-settings/ev"
    assert "Refresh Home Assistant sources" in notice.message
    assert meter not in notice.message

    message = forecast_boundary_issue(reason, (meter,), {meter: "Example wallbox"})
    assert "Example wallbox (sensor.example_wallbox_meter)" in message
    assert "Add the missing meter in Home Assistant Energy" in message
    assert "Planning is paused" in message
