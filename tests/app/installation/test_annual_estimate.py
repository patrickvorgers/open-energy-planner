"""Exercise the compact annual setup estimator at its documented boundaries."""

from datetime import UTC, datetime

import pytest

from open_energy_planner.installation.calculation.annual_estimate import annual_estimate

START = 20000 * 86400
END = START + 7 * 86400
NOW = datetime.fromtimestamp(END, UTC)


@pytest.mark.parametrize("endpoint", [[END + 1, 10], [START, 10], [END, True], [END]])
def test_setup_estimate_rejects_invalid_utc_endpoints(endpoint):
    """Reject non-midnight, duplicate, boolean and incomplete cumulative data."""
    with pytest.raises(ValueError, match="^Invalid setup history endpoint$"):
        annual_estimate(
            [{"entity_id": "sensor.import", "sign": 1}],
            [{"entity_id": "sensor.import", "points": [[START, 0], endpoint]}],
            now=NOW,
        )


@pytest.mark.parametrize("duplicate", [False, True])
def test_setup_estimate_rejects_unrequested_or_duplicate_meters(duplicate):
    """A foreign meter or repeated meter cannot complete the household balance."""
    grid = {"entity_id": "sensor.import", "points": [[START, 3], [END, 10]]}
    other = grid if duplicate else {**grid, "entity_id": "sensor.other"}
    with pytest.raises(ValueError, match="^Unexpected setup history channel$"):
        annual_estimate([{"entity_id": grid["entity_id"], "sign": 1}], [grid, other], now=NOW)


@pytest.mark.parametrize(
    "days,export_kwh,expected",
    [(6, 0, None), (7, 8, None), (7, 7, 0.0), (7, 0, 365.0)],
)
def test_setup_estimate_requires_a_week_and_a_nonnegative_household_balance(
    days, export_kwh, expected
):
    """Known zero is usable. A short period or a negative signed balance is unavailable."""
    end = START + days * 86400
    terms = [{"entity_id": "sensor.import", "sign": 1}, {"entity_id": "sensor.export", "sign": -1}]
    history = [
        {"entity_id": "sensor.import", "points": [[START, 0], [end, 7]]},
        {"entity_id": "sensor.export", "points": [[START, 0], [end, export_kwh]]},
    ]
    estimate = annual_estimate(terms, history, now=NOW)
    if expected is None:
        assert estimate is None
    else:
        assert estimate == {
            "annual_kwh": expected,
            "days": days,
            "start": START,
            "end": end,
            "extrapolated": True,
        }
