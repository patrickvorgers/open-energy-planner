"""Project safe reference-forecast advice without exposing or rewriting retained errors."""

REFERENCE_FORECAST_UNAVAILABLE = "Reference forecast is unavailable. Plan publication continues. Inspect application diagnostics."
REFERENCE_FORECAST_MESSAGES = (
    "Continuous reference awaits recent metering. No SoC reset was performed",
    "Reference forecast context is missing",
    "Reference awaits metering across the installation change",
    "Retained forecast cannot bridge the reference metering delay",
    "Unscheduled appliance work is not a comparable completed service",
    REFERENCE_FORECAST_UNAVAILABLE,
)


def public_reference_plan(plan):
    """Copy a plan's public diagnostic while leaving its immutable stored evidence untouched.

    Older plans can retain arbitrary exception values. Only exact owned reference
    explanations may cross the response boundary. This function sanitizes only the
    top-level ``reference_forecast_issue``. Callers must apply it to each plan they
    embed in a response.
    """
    if plan is None or not plan.get("reference_forecast_issue"):
        return plan
    issue = plan["reference_forecast_issue"]
    return {
        **plan,
        "reference_forecast_issue": next(
            (
                message
                for message in REFERENCE_FORECAST_MESSAGES
                if type(issue) is str and issue == message
            ),
            REFERENCE_FORECAST_UNAVAILABLE,
        ),
    }
