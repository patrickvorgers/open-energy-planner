# Code documentation standard

Write complete sentences. Do not use em dashes or semicolons as prose punctuation in documentation, comments, docstrings, UI text or log messages. Start a new sentence instead. Required punctuation in programming syntax, SQL, protocols and literal technical examples remains unchanged. Vendored code and licence text remain verbatim.

Short headings, field labels and structured data summaries may remain phrases. Do not expand concise labels into verbose narrative just to add a subject and verb.

Good code documentation is a development and review requirement. All documentation is in English and changes with the behaviour it describes. This project follows [PEP 257](https://peps.python.org/pep-0257/) for docstrings and [Home Assistant's code style](https://contributors.home-assistant.io/core/development/style/#docstrings-and-comments), using [Google-style sections](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings) when extended explanation is useful. PEP 257 defines general conventions. Google style supplies the detailed layout.

## Maintained guides

Maintain documentation only when it helps users or future maintenance. Describe the current behavior, supported configuration, contracts, limitations, relevant design rationale and reproducible validation commands. Update the canonical guide instead of appending implementation updates. Keep PR numbers, review conversations, temporary screenshots, dated run results and before/after delivery reports in the PR or CI artifacts, not in maintained product/site documentation. Git retains development history. Preserve legal notices, immutable provenance, independent test fixtures and actual application history. Those are not disposable delivery reports.

Keep useful current UI references with the owning guide. Do not create a separate report for every styling or implementation change. Review documentation for accuracy and maintenance value alongside code.

## Required documentation

- Every authored production module, class, function and method has a meaningful docstring. A short summary is enough for simple behaviour. Complex contracts need more detail. Generated methods, such as dataclass constructors, are documented through their class and field contracts.
- A module summary explains its purpose. Describe responsibilities, dependency boundaries and ownership when these are not obvious. A class explains its role and important state or invariants. Document a port's guarantees at the interface.
- Function/method docstrings describe behaviour. Add `Args:`, `Returns:`, `Yields:` and `Raises:` sections as applicable when parameter meaning, result interpretation or expected errors need explanation. Document side effects, preconditions and lifecycle restrictions that callers need to know.
- Use type hints for types. Describe semantics in prose instead of repeating the signature. Empty sections, paraphrased names and generated filler do not satisfy the requirement.
- Comments explain a non-obvious reason, constraint or tradeoff close to the relevant code. Use short full sentences ending with a period. Remove stale comments and commented-out code. Git retains history.
- Every test function has a short docstring describing the situation and expected behaviour. A descriptive name is useful but does not replace this explanation. Shared fixtures/helpers document their purpose and non-obvious setup/assumptions. Do not fill docstrings with repeated names or assertion-by-assertion narration.
- Planner scenarios additionally explain why their expected outcome follows from the inputs: price spread, losses, wear, physical limits, remaining bonus budget or terminal value, as relevant. Put shared per-case arithmetic at the owning fixture and reference it precisely from parametrized tests. Keep the explanation and expected values independently derived. Never generate them from the solver under test.

## Behavioral tests and literal values

Execute the responsible public behavior and assert an independent expected result.
A source identifier, string occurrence or exact HTML fragment cannot prove that
a function was called, configuration was persisted or navigation changed views.
Do not rewrite application source text to arrange a test. Override policy values
in an isolated process before importing consumers when import-time defaults matter.
Decode archived JSON and verify its meaning or replay the retained model instead
of searching the serialized text for a key. Use browser interactions for view changes.

Exact strings remain useful at external boundaries. Assert literal API statuses,
protocol fields and error codes against their agreed contract. UI labels and
messages can be checked when their wording is the behavior under test. Prefer
semantic roles and observable state to incidental tags or prose fragments when
testing actions. Do not import a production constant as the only expected value
for that constant's own wire representation.

Use an existing domain enum, or a small enum owned by the responsible model, for
a closed set of decision states reused by producers and consumers. Preserve
existing serialized values. Arbitrary names, provider IDs, JSON field names and
display text do not need enums. An enum does not replace boundary validation.

Structural tests remain appropriate when structure is the actual contract:
import boundaries, packaged assets, pinned image compatibility, security policies,
translation parity, requirements IDs and schema documentation coverage. Describe
that limited claim in the test rather than presenting it as functional proof.

## Descriptive Python names

Use names that explain a value's domain role, including parameters, loop targets,
comprehensions, lambda parameters, nested helpers, development scripts and tests.
Use `battery_flow`, `solar_source`, `interval_index`, `duration_hours`, `ports` and
`add_constraint` instead of `b`, `s`, `t`, `h`, `p` and `c`. Distinguish a battery
definition, measured state and planned flow when several appear together. Use
units where the quantity would otherwise be ambiguous. Short-lived scope does
not make a one-letter name descriptive. Use `_` for a deliberately unused value.

Keep imported types readable, for example `Fraction` rather than `F`. Existing
conventional library aliases such as `np` do not justify one-letter application
variables. Do not rename external wire keys, database fields, entity identities
or third-party APIs as part of a local cleanup. SQLAlchemy's `Table.c` is a
library attribute, not an application variable to rename.

Rename the declaration and every reference together, including keyword callers
and pytest parameter declarations where applicable. Preserve literals,
independent scenario expectations, public serialization and behaviour. Review
checks semantic clarity. Ordinary Ruff does not prove that a name explains its
meaning. Historical archived experiments retain their original evidence.

## Named fixed values

Use descriptive uppercase constants for fixed policy, thresholds, limits, retry
intervals and timeouts. Include units in the name. Keep a constant at the module
that owns its meaning. Import a genuinely shared policy instead of copying its
numeric value into several modules. Derive dependent values rather than defining
another independent setting. For example, `PLANNING_HORIZON_HOURS` owns executable
coverage, while forecast lead buckets and step counts derive from that policy.
Provider API access limits remain their own named capability.

Forecast selection uses `MIN_RELATIVE_MAE_IMPROVEMENT` and
`MIN_ABSOLUTE_MAE_IMPROVEMENT_EUR_PER_KWH`, not unexplained numbers in comparisons.
Initial values are 0.10 and 0.001 EUR/kWh (0.1 ct/kWh). The policy is code, not a new
end-user configuration form. Review checks meaning and reuse. Ordinary Ruff does
not prove that every meaningful fixed value has been named. Independent tests
retain separately specified input and expected values so changing policy cannot
silently change the test oracle. Structural indices and arithmetic identities
remain ordinary literals.

## Energy and integration contracts

Document these where relevant, at their owning contract or algorithm:

| Concern | Required meaning |
|---|---|
| Physical quantities | Units, time basis, sign convention, AC/DC measurement side and conversion/efficiency assumptions |
| Economics | Import/export interpretation, taxes/bonuses, wear and terminal valuation, rounding and independently checked objective |
| Measurements and forecasts | Provenance, sample/receipt time, missing versus zero, freshness, validity and distinction between observations and future predictions |
| Planning and publication | Hard constraints, invalidation, recovery, strategy and conditions under which a result can be published |
| State and storage | Owner, mutation/immutability, persistence and rollback guarantees, lock/thread assumptions and preserved historical evidence |
| HA integration | Entity availability, configuration/lifecycle behaviour and the boundary between virtual commands and user-owned physical control |

Keep the canonical explanation at the owning boundary and refer to it rather than copying it across adapters. Longer cross-module explanations belong in `design/` or a development guide, linked from code when useful. A solver constraint should explain the physical/economic rule when its purpose is not apparent from the expression.

## Writing style

Use triple double quotes and a concise summary ending with a period, followed by a blank line before extended details. Keep ordinary summaries action-oriented, such as "Validate source freshness before publication." Describe expected failure behaviour and important guarantees accurately. Do not promise atomicity, optimality or platform support that the implementation and evidence do not establish.

## Review and adoption

Apply this standard to existing and new production code, development scripts, tests and browser tests. When introducing a quality control, fix existing violations within its agreed scope immediately. Do not create a baseline, debt register or legacy suppressions. A different adoption approach requires an explicit user decision. Production and test adoption document existing definitions without altering their independent expected outcomes. Presence compliance does not prove that every explanation has passed semantic review.

Every PR review checks that a maintainer can understand the changed contract and the reason for non-obvious decisions, and that docstrings/comments still match behaviour. Documentation defects are actionable review findings. Human and AI contributors follow the same standard. CodeRabbit's path instructions explicitly refer to it.

Ruff's central configuration extends its default rules with D100-D107 (missing public module/package, class, function, method and special-method docstrings) and D419 (empty docstrings). The existing lint invocation runs these rules over `src/`, `custom_components/`, `script/`, `tests/` and `tests/frontend/`, both in the Dev Container and the single quality job. There are no test-path exemptions. Missing/empty test docstrings fail CI while all other lint checks remain active. There is no new dependency, custom checker or baseline.

Ruff's public-API rules intentionally do not cover every private/nested helper and can exempt inherited/overridden interfaces according to the pinned version's semantics. Reviewers check documentation content, internal helper completeness and the scenario-to-expected-result rationale. Independent fixtures prove missing/empty public docstrings fail, a meaningful short summary passes, and test paths enforce documentation and unrelated lint failures together. Documentation-link tests check maintained references.

These checks do not establish semantic completeness. CodeRabbit's blanket docstring-percentage check stays disabled: good documentation is required, but a coverage percentage cannot prove its quality. CodeRabbit and human reviewers assess contract accuracy and maintainability. An explicitly requested optional Codex Cloud review may provide additional findings.
