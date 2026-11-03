"""Keep simulator identities, sampling and retained-history version policy explicit."""

DOMAIN = "oep_test_fixture"


ENERGY = (
    "grid_import",
    "grid_export",
    "solar_roof",
    "solar_garage",
    "battery_charge",
    "battery_discharge",
)


QUARTER_MINUTES = 15


STATISTIC_MINUTES = 5


PARTS_PER_QUARTER = QUARTER_MINUTES // STATISTIC_MINUTES


QUARTER_HOURS = QUARTER_MINUTES / 60


QUARTER_WEIGHTS = (0.85, 1.05, 1.15, 0.95)


FINE_HISTORY_VERSION = 3
