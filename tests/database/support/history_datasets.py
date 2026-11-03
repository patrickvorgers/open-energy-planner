"""Generate bounded raw history rows independently of application schema and serializers."""

from datetime import UTC, datetime, timedelta
from json import JSONEncoder

START = datetime(2021, 10, 22, tzinfo=UTC)
END = datetime(2026, 10, 22, tzinfo=UTC)
WINDOW_START = END - timedelta(days=30)
DATASET_BATCH_ROWS = 1000
QUARTER = timedelta(minutes=15)


def retained_decisions(area):
    """Stagger the last saved day by bucket, so a global newest-day query would lose older valid preferences."""
    for day in range((END - START).days):
        for bucket in range(3):
            if day >= (END - START).days - bucket:
                continue
            yield {
                "area": area,
                "bucket": bucket,
                "day": (START + timedelta(days=day)).date().isoformat(),
                "preferred": "primary",
                "challenger": None,
                "streak": 0,
                "primary_mae": None,
                "alternative_mae": None,
                "hours": 0,
                "days": 0,
                "reason": "retained-test-evidence",
            }


def retained_pairs(area):
    """Two cohorts give recent hourly MAEs 0.02 and 0.01. Older extreme errors must never enter the window."""
    for hour in range(int((END - START).total_seconds() / 3600)):
        target = START + timedelta(hours=hour)
        for bucket in range(3):
            for cohort in range(2):
                yield {
                    "area": area,
                    "bucket": bucket,
                    "cohort": (target - timedelta(hours=6 + 24 * bucket + cohort)).isoformat(),
                    "target": target.isoformat(),
                    "primary_price": (0.01 if cohort == 0 else 0.03)
                    if target >= WINDOW_START
                    else 999.0,
                    "alternative_price": (-0.005 if cohort == 0 else -0.015)
                    if target >= WINDOW_START
                    else 888.0,
                    "primary_version": "a" * 64,
                    "alternative_version": "b" * 64,
                }


def retained_actuals(area):
    """Supply a zero-price actual for every retained hour without consulting forecast scoring."""
    for hour in range(int((END - START).total_seconds() / 3600)):
        yield {
            "area": area,
            "target": (START + timedelta(hours=hour)).isoformat(),
            "price": 0.0,
            "version": "c" * 64,
        }


def settlement_batches(sample, revision, first, last):
    """Repeat retained input evidence in bounded batches, preserving every timestamp and JSON value."""
    fields = {
        f"{scenario}_{key}": sample[scenario][key]
        for scenario in ("actual", "reference")
        for key in (
            "purchase",
            "sale",
            "bonus",
            "grid_cash",
            "wear",
            "inventory_change",
            "economic_cost",
            "import_kwh",
            "export_kwh",
        )
    }
    shared = {
        "revision": revision,
        **fields,
        "savings": -0.1,
        "avoided_import_kwh": -1,
        "estimated": 0,
        "duration_h": 0.25,
    }
    encoder = JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    count = int((last - first) / QUARTER)
    for offset in range(0, count, DATASET_BATCH_ROWS):
        frames, evaluations = [], []
        for index in range(offset, min(count, offset + DATASET_BATCH_ROWS)):
            start = first + QUARTER * index
            end = start + QUARTER
            start_key = start.isoformat(timespec="microseconds")
            record = {**sample, "start": start.isoformat(), "end": end.isoformat()}
            frames.append(
                {
                    "start": start_key,
                    "end": end.isoformat(timespec="microseconds"),
                    "payload": "{}",
                }
            )
            evaluations.append({**shared, "start": start_key, "payload": encoder.encode(record)})
        yield frames, evaluations
