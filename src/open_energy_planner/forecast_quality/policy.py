"""Share forecast comparability and Recorder admission policy between quality and savings."""

from datetime import timedelta

FORECAST_MODELS = {
    "appliance-residual-weekday-v1": "Historical residual household plus acknowledged appliance cycles",
    "consumer-composite-v1": "Individual consumers and remaining household (decision-validated composition)",
    "startup-v1": "Start profile from annual household estimate",
    "weekday-hour-v1": "Learned weekday/hour consumption profile",
    "ridge-v1": "ML regression (ridge)",
    "pvwatts-v1": "Physical solar model (PVWatts)",
    "pvwatts-calibrated-v1": "PVWatts forecast with learned corrections",
    "startup-v2": "Start profile (estimated bounded quarter projection)",
    "weekday-hour-v2": "Learned weekday/hour profile (bounded quarter projection)",
    "ridge-v2": "ML regression (bounded quarter projection)",
    "xgboost-weather-v1": "Learned nonlinear weather model (bounded quarter projection)",
    "pvwatts-calibrated-v2": "PVWatts quarter geometry with learned corrections",
    "pvwatts-v2": "Physical solar model (PVWatts quarter geometry)",
    "ha-production-v1": "Home Assistant solar production forecast",
    "solar-selected-v1": "Independently selected solar forecasts",
    "solar-xgboost-v1": "Solar ML correction (XGBoost CPU)",
    "solar-xgboost-production-v2": "Independent solar production (XGBoost CPU)",
    "solar-learned-v1": "Per-source learned solar forecasts",
}


HISTORY_LOOKBACK_DAYS = 3

RECORDER_DELAY_MINUTES = 10

HISTORY_RETRY_MINUTES = 15

HOUR = timedelta(hours=1)

DAY = timedelta(days=1)

HISTORY_ERRORS = ("recorder_unavailable", "history_unavailable", "unsupported_statistics")
