"""Debug the real synthetic model without a parent watchdog or physical publication."""

import json
import os
from dataclasses import asdict


def main():
    """Solve and independently validate the synthetic scenario without physical command publication."""
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"

    from open_energy_planner.installation.validation import validate
    from open_energy_planner.optimization.adapters.highs import HighsSolver
    from open_energy_planner.optimization.domain import SolveOptions
    from open_energy_planner.synthetic import capture

    snapshot = capture()
    result = HighsSolver().solve(snapshot.problem, SolveOptions(60))
    if result.plan is None:
        raise RuntimeError("Synthetic debugging scenario returned no plan")
    costs = validate(snapshot.problem, result.plan)
    print(json.dumps({"status": result.status.value, "independent_costs": asdict(costs)}))


if __name__ == "__main__":
    main()
