# Compact measurement storage method

Compare wide and per-series layouts using independently generated synthetic inputs. Include noninteger and nonzero values to avoid unusually favorable integer storage. Repeating one day estimates storage layout only. It does not establish a year of actual measurements, forecast quality or a whole-system history budget.

Energy quarters sum three five-minute deltas. Power and SoC quarters average three means. That SoC is not a quarter-end observation. Convert source units explicitly. Count tables, primary keys and required additional indexes. Check generated files with SQLite `integrity_check`.

The historical acquisition inputs, derived databases, generator and result files remain in the private development archive. They are excluded from the public source because they contain or derive from personal Home Assistant observations. The public repository does not claim to reproduce those private benchmark results. Benchmark the current [shared schema](../../docs/development/DATABASE-SCHEMA.md) with representative retention, indexes and workloads before making a deployment allowance. Include complete plan inputs/results, forecasts, prices, financial evaluations, provenance/corrections, backups and temporary transaction files. External databases also require their own capacity and backup assessment.

OEP archival storage is additional to HA Recorder. Accurate financial settlement needs interval energy, not one power sample per quarter. API coverage for one source does not establish coverage for every source, and compact source storage does not establish a complete continuous-reference model.
