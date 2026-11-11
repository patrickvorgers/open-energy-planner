"""Read original plan facts without optimizing, attributing AC routes or inventing causal explanations."""

from datetime import datetime, timedelta
from math import isfinite

from open_energy_planner.core.units import WATTS_PER_KW
from open_energy_planner.forecast_quality.policy import FORECAST_MODELS

from .context import physical_context

PERCENT = 100
LIMIT_TOLERANCE_W = 0.001
STOCK_TOLERANCE_KWH = 0.0000001
EVIDENCE_VERSION = "plan-facts-v1"


def recorded_number(value):
    """Keep finite recorded numbers. Absent/invalid numerical evidence is not a zero reading."""
    return type(value) in (int, float) and isfinite(value)


def fact(label, value, unit=""):
    """Keep numbers and units separate for locale-aware accessible presentation. None means unrecorded."""
    return {"label": label, "value": value, "unit": unit}


def card(title, facts, observations=()):
    """Group related recorded inputs/outcomes. Observations describe equality, never causation."""
    return {"title": title, "facts": facts, "observations": list(observations)}


def power_limit(label, energy_kwh, duration_h, limit_w):
    """Describe a reached positive rating only within numerical tolerance, not merely above it."""
    if (
        recorded_number(energy_kwh)
        and limit_w > 0
        and abs(energy_kwh * WATTS_PER_KW / duration_h - limit_w) <= LIMIT_TOLERANCE_W
    ):
        return [label + " reached in this interval."]
    return []


def forecast_facts(plan, kind, source_id=None):
    """Read only the forecast actually recorded in this plan. Shadow/current models must not replace it."""
    entries = plan["input_snapshot"].get(kind + "_evidence", [])
    if source_id is not None:
        entries = [entry for entry in entries if source_id in entry.get("source_ids", [])]
    if not entries:
        return [fact("Forecast model", None), fact("Forecast quality", None)]
    if any(not isinstance(entry.get("quality", "Not recorded"), str) for entry in entries):
        raise ValueError("Archived forecast quality must be text")
    return [
        fact(
            "Forecast model",
            ": ".join(
                dict.fromkeys(
                    FORECAST_MODELS.get(entry.get("model"), entry.get("model") or "Not recorded")
                    for entry in entries
                )
            ),
        ),
        fact(
            "Forecast quality",
            ": ".join(
                dict.fromkeys(
                    entry.get("quality", "Not recorded").replace("_", " ").capitalize()
                    for entry in entries
                )
            ),
        ),
        fact(
            "Forecast version",
            ": ".join(
                dict.fromkeys(entry.get("forecast_version", "Not recorded") for entry in entries)
            ),
        ),
    ]


def recorded_total(values):
    """Sum a whole-period series only when every interval has a finite recorded value."""
    values = tuple(values)
    return sum(values) if values and all(recorded_number(value) for value in values) else None


def evidence(plan):
    """Project immutable installation inputs and planned quantities into a read-only evidence document.

    Legacy/incomplete physical snapshots explicitly lack evidence. Missing flow
    intervals stay missing and retain their original index. No current settings,
    corrected tariffs, new forecasts or solver calls participate. Reached bounds
    are observations only: this layer cannot establish why an alternative lost.
    """
    result = {
        "version": EVIDENCE_VERSION,
        "plan_id": plan["plan_id"],
        "summary": [],
        "intervals": [],
    }
    try:
        problem = physical_context(plan)
        result["summary"] = summary_cards(plan, problem)
        result["period"] = {
            "start": problem.intervals[0].start.isoformat(),
            "end": (
                problem.intervals[-1].start + timedelta(hours=problem.intervals[-1].duration_h)
            ).isoformat(),
            "duration_h": sum(interval.duration_h for interval in problem.intervals),
        }
        result["intervals"] = [
            interval_evidence(plan, problem, index) for index in range(len(problem.intervals))
        ]
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return {
            "version": EVIDENCE_VERSION,
            "plan_id": plan["plan_id"],
            "summary": [],
            "intervals": [],
            "issue": "Original plan evidence is incomplete or unavailable for this plan.",
        }

    return result


def summary_cards(plan, problem):
    """Expose original strategy, initial storage, reserves, losses and inventory assumptions."""
    cards = []
    currency = plan.get("currency") or ""
    names = plan.get("comparison_names", {})
    status = {
        "optimal": "Optimal within solver tolerance",
        "feasible": "Feasible: Financial optimality is not proved.",
    }.get(plan.get("solution_status"), "Not recorded")
    strategy = {
        "minimize cost": "Minimize cost",
        "minimize consumption": "Minimize consumption: corrected import first, financial cost second",
    }.get(plan.get("strategy"), "Not recorded")
    cards.append(
        card(
            "Planning basis",
            [
                fact("Strategy", strategy),
                fact("Solution", status),
                fact("Currency", plan.get("currency")),
                fact("Original contract version", plan["input_snapshot"].get("contract_version")),
                fact("Grid import limit", problem.grid_import_limit_w, "W"),
                fact("Grid export limit", problem.grid_export_limit_w, "W"),
            ],
        )
    )
    cards.append(
        card(
            "Forecast totals: Full planning horizon",
            [
                fact(
                    "Total demand forecast: AC"
                    if plan["input_snapshot"].get("load_components")
                    else "Household demand forecast: AC",
                    sum(interval.load_kwh for interval in problem.intervals),
                    "kWh",
                ),
                fact(
                    "Available solar forecast: DC",
                    sum(interval.pv_kwh for interval in problem.intervals),
                    "kWh",
                ),
                fact(
                    "Forecast horizon",
                    sum(interval.duration_h for interval in problem.intervals),
                    "h",
                ),
            ],
        )
    )
    cards.append(
        card(
            "Household forecast: Whole period",
            forecast_facts(plan, "demand"),
            [
                "The original load breakdown records expected and remaining device use. Scheduled cycles, EV charging and heating come from the plan. Each contribution is counted once."
                if plan["input_snapshot"].get("load_components")
                else "This forecast includes household baseload. Separately controlled loads are excluded only when recorded in the demand configuration."
            ],
        )
    )
    flows = plan.get("expected_flows", [])
    interval_flows = [
        flows[index] or {} if index < len(flows) else {} for index in range(len(problem.intervals))
    ]
    cards.append(
        card(
            "Planned grid exchange: Whole period",
            [
                fact(
                    "Total import",
                    recorded_total(flow.get("grid_import") for flow in interval_flows),
                    "kWh",
                ),
                fact(
                    "Total export",
                    recorded_total(flow.get("grid_export") for flow in interval_flows),
                    "kWh",
                ),
            ],
        )
    )
    for source_index, source in enumerate(problem.pv_sources):
        cards.append(
            card(
                names.get(source.source_id, f"Solar {source_index + 1}")
                + ": Whole-period forecast",
                [
                    fact(
                        "Available forecast: DC: Whole period",
                        sum(
                            dict(interval.pv_sources)[source.source_id]
                            for interval in problem.intervals
                        ),
                        "kWh",
                    ),
                    *forecast_facts(plan, "solar", source.source_id),
                ],
            )
        )
    for index, unit in enumerate(problem.batteries):
        battery = unit.battery
        dispatches = [
            next(
                (item for item in flow.get("batteries", []) if item["device_id"] == unit.device_id),
                {},
            )
            for flow in interval_flows
        ]
        cards.append(
            card(
                names.get(unit.device_id, f"Battery {index + 1}") + ": Whole-period plan",
                [
                    fact(
                        "Total charge: DC terminal",
                        recorded_total(item.get("charge_kwh") for item in dispatches),
                        "kWh",
                    ),
                    fact(
                        "Total discharge: DC terminal",
                        recorded_total(item.get("discharge_kwh") for item in dispatches),
                        "kWh",
                    ),
                    fact("Starting SoC", battery.initial_kwh / battery.capacity_kwh * PERCENT, "%"),
                    fact(
                        "Final SoC",
                        dispatches[-1]["end_kwh"] / battery.capacity_kwh * PERCENT
                        if recorded_number(dispatches[-1].get("end_kwh"))
                        else None,
                        "%",
                    ),
                ],
            )
        )
        cards.append(
            card(
                names.get(unit.device_id, f"Battery {index + 1}") + ": Starting conditions",
                [
                    fact("Initial stored energy", battery.initial_kwh, "kWh"),
                    fact("Usable capacity", battery.capacity_kwh, "kWh"),
                    fact("Configured reserve", battery.minimum_kwh, "kWh"),
                    fact("Charge efficiency", battery.charge_efficiency * PERCENT, "%"),
                    fact("Discharge efficiency", battery.discharge_efficiency * PERCENT, "%"),
                    {
                        **fact(
                            "Wear per internal kWh discharged",
                            battery.wear_cost_per_kwh,
                            currency + "/kWh",
                        ),
                        "key": "wear_cost_per_kwh",
                    },
                ],
                ["Reserve recovery is required."]
                if battery.recover_reserve and battery.initial_kwh < battery.minimum_kwh
                else [],
            )
        )
    for curve in problem.terminal_valuations:
        cards.append(
            card(
                names.get(curve.device_id, "Battery") + ": Remaining energy valuation",
                [
                    fact("Net value per internal kWh", curve.rate, currency + "/kWh"),
                    fact("Maximum useful volume", curve.usable_kwh, "kWh"),
                    fact(
                        "Price basis",
                        "Later forecast prices"
                        if curve.price_basis == "post_horizon_prices"
                        else "Estimated from a repeated trailing price window.",
                    ),
                ],
                ["Inventory estimate, not cash income or an extra charging command."],
            )
        )
    costs = plan.get("independent_costs", {})
    cards.append(
        card(
            "Original expected financial components",
            [
                fact(label, costs.get(key), currency)
                for label, key in (
                    ("Import cost", "import_cost"),
                    ("Export income", "export_income"),
                    ("Bonus income", "bonus_income"),
                    ("Battery wear", "wear_cost"),
                    ("Remaining inventory value", "terminal_value"),
                )
            ],
        )
    )
    return cards


def interval_evidence(plan, problem, interval_index):
    """Project one archived interval without allocating shared-bus energy to an invented source route."""
    currency = plan.get("currency") or ""
    names = plan.get("comparison_names", {})
    flows = plan.get("expected_flows", [])
    interval = problem.intervals[interval_index]
    command = next(
        (
            step
            for step in plan.get("steps", [])
            if datetime.fromisoformat(step["start"])
            <= interval.start
            < datetime.fromisoformat(step["end"])
        ),
        {},
    )
    flow = flows[interval_index] if interval_index < len(flows) else None
    cards = [
        card(
            "Forecast and original prices: Selected interval",
            [
                fact("Household demand: AC: This interval", interval.load_kwh, "kWh"),
                fact("Available solar: DC: This interval", interval.pv_kwh, "kWh"),
                fact(
                    "Import rate before conditional bonuses",
                    interval.buy_price,
                    currency + "/kWh",
                ),
                fact(
                    "Export rate before conditional bonuses",
                    interval.sell_price,
                    currency + "/kWh",
                ),
            ],
        )
    ]
    tariff = next(
        (
            row
            for row in plan["input_snapshot"].get("tariff_evidence", [])
            if datetime.fromisoformat(row["start"])
            <= interval.start
            < datetime.fromisoformat(row["end"])
        ),
        None,
    )
    cards[0]["facts"].append(
        fact(
            "Market price evidence",
            {"published": "Published", "forecast": "Forecast"}.get(
                tariff.get("price_kind") if tariff else None, "Not recorded"
            ),
        )
    )
    cards.append(card("Household forecast: This interval", forecast_facts(plan, "demand")))
    if flow is not None:
        cards.append(
            card(
                "Planned grid exchange: AC",
                [
                    fact("Import", flow.get("grid_import"), "kWh"),
                    fact("Export", flow.get("grid_export"), "kWh"),
                ],
                power_limit(
                    "Grid import limit",
                    flow["grid_import"],
                    interval.duration_h,
                    problem.grid_import_limit_w,
                )
                + power_limit(
                    "Grid export limit",
                    flow["grid_export"],
                    interval.duration_h,
                    problem.grid_export_limit_w,
                )
                if "grid_import" in flow and "grid_export" in flow
                else [],
            )
        )
        for unit_index, unit in enumerate(problem.batteries):
            dispatch = next(
                (item for item in flow.get("batteries", []) if item["device_id"] == unit.device_id),
                None,
            )
            if dispatch is None:
                continue
            battery = unit.battery
            target = command.get("devices", {}).get(unit.device_id, {})
            mode = {
                "fixed_power": "Fixed power",
                "follow_grid_target": "Zero on the meter: Local regulation is active.",
            }.get(target.get("mode"))
            observations = power_limit(
                "Charge power limit",
                dispatch.get("charge_kwh"),
                interval.duration_h,
                battery.charge_limit_w,
            ) + power_limit(
                "Discharge power limit",
                dispatch.get("discharge_kwh"),
                interval.duration_h,
                battery.discharge_limit_w,
            )
            stored = dispatch.get("end_kwh")
            if recorded_number(stored) and abs(stored - battery.minimum_kwh) <= STOCK_TOLERANCE_KWH:
                observations.append("Stored energy ends at the configured reserve.")
            if (
                recorded_number(stored)
                and abs(stored - battery.capacity_kwh) <= STOCK_TOLERANCE_KWH
            ):
                observations.append("Stored energy ends at usable capacity.")
            cards.append(
                card(
                    names.get(unit.device_id, f"Battery {unit_index + 1}") + ": Planned battery",
                    [
                        fact("Control mode", mode),
                        fact(
                            "Fixed power target",
                            "Not applicable"
                            if target.get("mode") == "follow_grid_target"
                            else target.get("power_w"),
                            "W",
                        ),
                        fact("Charge: DC terminal", dispatch.get("charge_kwh"), "kWh"),
                        fact("Discharge: DC terminal", dispatch.get("discharge_kwh"), "kWh"),
                        fact(
                            "End SoC",
                            stored / battery.capacity_kwh * PERCENT
                            if recorded_number(stored)
                            else None,
                            "%",
                        ),
                        fact("Charge limit", battery.charge_limit_w, "W"),
                        fact("Discharge limit", battery.discharge_limit_w, "W"),
                    ],
                    observations,
                )
            )
        for source_index, source in enumerate(problem.pv_sources):
            solar = next(
                (item for item in flow.get("solar", []) if item["source_id"] == source.source_id),
                None,
            )
            cards.append(
                card(
                    names.get(source.source_id, f"Solar {source_index + 1}") + ": Solar",
                    [
                        fact(
                            "Available forecast: DC",
                            dict(interval.pv_sources)[source.source_id],
                            "kWh",
                        ),
                        fact("Planned use: DC", solar.get("used_kwh") if solar else None, "kWh"),
                        fact("Curtailment permitted", "Yes" if source.can_curtail else "No"),
                        *forecast_facts(plan, "solar", source.source_id),
                    ],
                    ["Production disabled in this plan."]
                    if solar and solar.get("enabled") is False
                    else [],
                )
            )
        for converter_index, converter in enumerate(problem.inverters):
            dispatch = next(
                (
                    item
                    for item in flow.get("inverters", [])
                    if item["inverter_id"] == converter.inverter_id
                ),
                None,
            )
            if dispatch is not None:
                cards.append(
                    card(
                        f"Converter {converter_index + 1}: Shared boundary",
                        [
                            fact("AC import", dispatch.get("import_kwh"), "kWh"),
                            fact("AC export", dispatch.get("export_kwh"), "kWh"),
                            fact("Shared AC limit", converter.ac_limit_w, "W"),
                            fact(
                                "Physical clipping recorded",
                                "Yes"
                                if dispatch.get("clipping") is True
                                else "No"
                                if dispatch.get("clipping") is False
                                else None,
                            ),
                            fact("Conversion efficiency", converter.efficiency * PERCENT, "%"),
                        ],
                        power_limit(
                            "Shared AC import limit",
                            dispatch.get("import_kwh"),
                            interval.duration_h,
                            converter.ac_limit_w,
                        )
                        + power_limit(
                            "Shared AC export limit",
                            dispatch.get("export_kwh"),
                            interval.duration_h,
                            converter.ac_limit_w,
                        ),
                    )
                )
    cards.extend(bonus_cards(plan, interval_index))
    complete = flow is not None and all(
        recorded_number(flow.get(key)) for key in ("grid_import", "grid_export")
    )
    if complete:
        complete = all(
            {item.get(key) for item in flow.get(collection, [])} == expected
            and all(
                recorded_number(item.get(field))
                for item in flow.get(collection, [])
                for field in fields
            )
            for collection, key, expected, fields in (
                (
                    "batteries",
                    "device_id",
                    {unit.device_id for unit in problem.batteries},
                    ("charge_kwh", "discharge_kwh", "end_kwh"),
                ),
                (
                    "solar",
                    "source_id",
                    {source.source_id for source in problem.pv_sources},
                    ("used_kwh",),
                ),
                (
                    "inverters",
                    "inverter_id",
                    {converter.inverter_id for converter in problem.inverters},
                    ("import_kwh", "export_kwh"),
                ),
            )
        )
    return {
        "start": interval.start.isoformat(),
        "end": (interval.start + timedelta(hours=interval.duration_h)).isoformat(),
        "cards": cards,
        "issue": None
        if complete
        else "Some planned physical outcomes were not recorded for this interval.",
    }


def bonus_cards(plan, interval_index):
    """Show frozen compiled bonus rates, caps and original counters without settling or guessing absent credits."""
    cards = []
    for rule in plan["input_snapshot"]["problem"].get("export_bonuses", []):
        outcome = next(
            (
                entry
                for entry in plan.get("expected_bonuses", [])
                if entry["rule_id"] == rule["rule_id"]
            ),
            None,
        )
        credits = outcome.get("quota_kwh", []) if outcome else []
        counters = outcome.get("counters_kwh", []) if outcome else []
        if interval_index == 0:
            before = rule["initial_kwh"]
        elif rule["periods"][interval_index] != rule["periods"][interval_index - 1]:
            before = 0
        else:
            before = counters[interval_index - 1] if interval_index <= len(counters) else None
        cards.append(
            card(
                "Export bonus: " + rule["rule_id"],
                [
                    fact(
                        "Effective bonus rate",
                        rule["rates"][interval_index],
                        (plan.get("currency") or "") + "/kWh",
                    ),
                    fact("Period cap", rule["caps_kwh"][interval_index], "kWh"),
                    fact("Counter before this interval", before, "kWh"),
                    fact(
                        "Planned credited volume",
                        credits[interval_index] if interval_index < len(credits) else None,
                        "kWh",
                    ),
                ],
            )
        )
    return cards
