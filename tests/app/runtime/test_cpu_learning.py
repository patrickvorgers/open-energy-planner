"""Exercise the CPU regression and portable archive APIs required by learning domains."""

import json

import numpy as np
from xgboost import Booster, DMatrix, build_info, train


def test_cpu_boosting_learns_interaction_and_replays_json():
    """CPU trees learn a conditional response and preserve predictions through native JSON."""
    assert build_info()["USE_CUDA"] is False
    assert build_info()["USE_NCCL"] is False
    inputs = np.tile([[0, 0], [0, 1], [1, 0], [1, 1]], (32, 1))
    targets = np.tile([0, 0, 1, 0], 32)
    samples = DMatrix(inputs, label=targets, feature_names=["level", "condition"])
    model = train(
        {
            "objective": "reg:squarederror",
            "tree_method": "hist",
            "device": "cpu",
            "nthread": 1,
            "max_depth": 2,
            "seed": 0,
        },
        samples,
        num_boost_round=32,
    )
    document = json.loads(model.save_raw(raw_format="json"))
    restored = Booster(params={"device": "cpu", "nthread": 1})
    restored.load_model(bytearray(json.dumps(document).encode()))
    np.testing.assert_allclose(restored.predict(samples), targets, atol=0.03)
    np.testing.assert_allclose(restored.predict(samples), model.predict(samples), atol=1e-8)
