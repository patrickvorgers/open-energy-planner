# Solver regression contracts

The maintained optimization input is `InstallationProblem`. Results contain battery-terminal DC energy, inverter AC ports, individual PV production and aggregate grid exchange. Independent expected outcomes are not generated from the solver. Validate every candidate with `installation.validation.validate`. Preserve finite-value, identity, reserve, power, physical conservation and financial safeguards.

## Preserved independent expectations

- [Core scenarios](../tests/app/solver_regression/test_planner_scenarios.py): S01
  retains -0.28 arbitrage cost, S02 retains -0.16 paid charging, and S04 retains
  0.28 usable-stock value. Their grid energies, battery stocks and whole-watt
  commands are unchanged. The 0.8/0.9 loss companion retains -0.168 cost.
  Wear of 0.35 prevents cycling, while wear of 0.28 leaves a genuine tie.
- Fixed/on-off PV still yields 0.048 forced-export cost or 0.040 replacement
  import cost. The spent/full quota extremes retain 0 and -0.16 cost. Two
  forced exports still receive the first 0.4 kWh of quota and cost -0.072.
  These cases require no continuously adjustable PV or source-based eligibility.
- [Horizon scenarios](../tests/app/solver_regression/test_horizon_scenarios.py):
  H01/H02 retain their independently integrated household/PV totals. H03 keeps
  10 kWh imports, 8.1 kWh exports, 0.45 wear and -1.385 cost. Every retained
  horizon runs both the original 120 mixed steps and the refined 192 quarters.
- [Shared-grid scenarios](../tests/app/solver_regression/test_shared_ac_bus.py):
  M01/M02/M03/M05/M06/M08 retain their original monetary, total-grid and named
  final-stock expectations. M07 remains infeasible. M03's internal 0.5 AC kWh
  and M04's first-interval 0.4 AC kWh transfer and 0.32 stored kWh are checked
  through the actual converter ports and independent battery states.
- [Converter scenarios](../tests/app/solver_regression/test_topology_scenarios.py):
  T01 keeps 0.729 exported kWh and -0.2916 cost. T04 keeps 1.8 kWh and -0.72.
  T07 keeps 0.1 imported kWh, 0.9 stored kWh and -0.02 cost. T08 keeps 0.75
  exported kWh and -0.3 cost. The T05 current no-AC-exchange variant retains
  0.98 stored kWh and 0.49 stock value, but uses an admissible positive 1 W
  converter and zero grid limits instead of a zero-rated inverter setting.
- [Mixed scenarios](../tests/app/solver_regression/test_mixed_hybrid.py): H03 keeps
  the 0.75 kWh shared export bound and H04 remains infeasible. H05 retains
  0.475 direct-DC and 0.5 AC-derived stored kWh, giving -0.1975 economic cost.
  Separate AC conversion retains its 0.512 kWh bound over 120 steps. The
  0.5*0.8*0.8*0.9=0.288 kWh standalone charge and 1.288 total stock value remain.
- Native stop/status, immediate deadline, time-stopped feasible candidate,
  independent dual bound, native/build/extraction error, unbounded-model and
  repeated-solve tests now build current installation models. Memory-counter
  tests remain unchanged. Component tests retain independent .45/.4/.09/.1
  stocks, .2/.4 repeated-context results and binding ownership checks.
- [Aggregate-export bonuses](../tests/app/contract/test_export_bonuses.py) retain independent settlement, large-cap, 192-step, exhaustive decision and corruption oracles. PV and battery exports share quota. Counters track physical exports even after paid quota is exhausted.

## Current physical semantics

PV on/off permission is distinct from continuous curtailment. Terminal valuation excludes reserve energy. Whole-watt commands constrain feasible schedules before solving. DC conversion losses apply in both directions at the declared measurement side. Inverters require a positive rating, even when grid exchange is disabled. Stable device IDs and converter ports define physical identity. Internal transfers are not external grid consumption.

When a model contract changes, derive a separately named independent scenario rather than rounding or regenerating an incompatible expected result. Keep existing compatible monetary, energy and stock expectations. Use Git for the history of replaced builders and fixtures.
