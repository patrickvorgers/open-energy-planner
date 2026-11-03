"""Keep isolated raw inputs bounded, deterministic and independent of product serializers."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from tests.database.support.history_datasets import (
    retained_actuals,
    retained_decisions,
    retained_pairs,
    settlement_batches,
)


def settlement_sample():
    """Specify raw evidence independently, including a nested value that serialization must preserve."""
    return {
        "actual": {
            "purchase": 1.0,
            "sale": 0.2,
            "bonus": 0,
            "grid_cash": 0.8,
            "wear": 0,
            "inventory_change": 0,
            "economic_cost": 0.8,
            "import_kwh": 2,
            "export_kwh": 1,
        },
        "reference": {
            "purchase": 0.9,
            "sale": 0.2,
            "bonus": 0,
            "grid_cash": 0.7,
            "wear": 0,
            "inventory_change": 0,
            "economic_cost": 0.7,
            "import_kwh": 1,
            "export_kwh": 1,
        },
        "context": {"quality": "observed", "unknown": None},
        "start": "overridden-start",
        "end": "overridden-end",
    }


def test_settlement_generator_keeps_rows_json_and_input_across_leap_day():
    """Thirteen days spanning leap day contain 1248 quarters, split into 1000 and248 fresh input rows."""
    sample = settlement_sample()
    original = deepcopy(sample)
    batches = list(
        settlement_batches(
            sample,
            "frozen-revision",
            datetime(2024, 2, 28, tzinfo=UTC),
            datetime(2024, 3, 12, tzinfo=UTC),
        )
    )
    assert [len(frames) for frames, _ in batches] == [1000, 248]
    frames = [row for frame_rows, _ in batches for row in frame_rows]
    evaluations = [row for _, evaluation_rows in batches for row in evaluation_rows]
    assert len(evaluations) == 1248
    assert frames[0] == {
        "start": "2024-02-28T00:00:00.000000+00:00",
        "end": "2024-02-28T00:15:00.000000+00:00",
        "payload": "{}",
    }
    assert frames[-1]["end"] == "2024-03-12T00:00:00.000000+00:00"
    assert evaluations[0]["start"] == frames[0]["start"]
    assert evaluations[-1]["start"] == frames[-1]["start"]
    assert evaluations[0]["savings"] == -0.1
    assert evaluations[0]["avoided_import_kwh"] == -1
    assert evaluations[0]["duration_h"] == 0.25
    assert evaluations[0]["revision"] == "frozen-revision"
    assert evaluations[0]["actual_purchase"] == 1.0
    assert evaluations[0]["reference_import_kwh"] == 1
    assert json.loads(evaluations[0]["payload"]) == {
        **original,
        "start": "2024-02-28T00:00:00+00:00",
        "end": "2024-02-28T00:15:00+00:00",
    }
    assert sample == original


def test_raw_dataset_encoding_rejects_nonfinite_input():
    """Separating test data from the product serializer retains its finite JSON boundary."""
    sample = settlement_sample()
    sample["actual"]["purchase"] = float("nan")
    with pytest.raises(ValueError):
        next(
            settlement_batches(
                sample,
                "revision",
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 1, 2, tzinfo=UTC),
            )
        )


def test_price_generators_retain_independent_area_and_horizon_inputs():
    """First retained evidence predates scoring and preserves the deliberately extreme old errors."""
    pair = next(retained_pairs("fixture-area"))
    assert pair["area"] == "fixture-area"
    assert pair["target"] == "2021-10-22T00:00:00+00:00"
    assert pair["primary_price"] == 999.0
    assert pair["alternative_price"] == 888.0
    assert next(retained_actuals("fixture-area"))["price"] == 0.0
    assert next(retained_decisions("fixture-area"))["day"] == "2021-10-22"
