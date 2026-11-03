# Solver selection: direct highspy and native HiGHS

Status: Accepted.

## Decision and ownership

Use Python model classes with direct highspy algebraic variables, expressions and
constraints over native HiGHS. The app owns planning and independent validation.
Home Assistant receives virtual commands and does not import the solver.
There is one maintained production solver adapter. Pyomo, APPSI and the earlier
experimental builders are not alternative runtime choices or optional dependencies.

The [technical design](TECHNICAL-DESIGN.md#solver-contract-and-responsibilities)
defines the current adapter boundary. Independent physical and financial checks
remain outside model construction. Keep the hand-derived scenario expectations
in [the maintained regression suite](../tests/app/solver_regression). Removing a
prototype does not remove those requirements or justify regenerated answers.

## Rationale and limits

Direct highspy provides native HiGHS algebraic variables, expressions and constraints without an additional modeling or solver abstraction dependency. Device classes retain local ownership and a shared factory composes the installation. A general matrix framework, nonlinear modeling and interchangeable solvers are not current requirements.

Independent physical and financial validation remains outside the adapter because agreement between two instances of the same model cannot detect shared modeling errors. Keep the hand-derived expectations in the [regression contracts](SOLVER-REGRESSION-MIGRATION.md).

Current CI and release gates establish supported behavior and platform admission. This choice does not promise production latency, arbitrary installation scaling, native ARM performance or whole-app memory usage. Measure representative current workloads before adopting performance changes. Distribution remains governed by the [project notices](../THIRD_PARTY_NOTICES.md).

## Performance changes

The maintained adapter does not use previous-plan warm starts. Revisit only when measured planning delays justify the extra lifecycle and failure handling. Any future hints must match device IDs and absolute intervals, preserve current measurements and hard constraints, share the existing solve budget and fall back to cold planning without a new readiness blocker. Require independently validated service and financial outcomes, substantial input changes and fault-injection checks before adoption.
