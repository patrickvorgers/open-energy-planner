"""Strict JSON translation for app-owned contract settings and previews."""

from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from open_energy_planner.contracts.bonus import BonusRule, CounterBook, CounterSeed
from open_energy_planner.contracts.calculation.condition import (
    ComparisonCondition,
    ComparisonOperator,
)
from open_energy_planner.contracts.calculation.formula import Formula
from open_energy_planner.contracts.model import ContractPeriod, ContractSchedule
from open_energy_planner.contracts.windows import MONTHS_PER_YEAR, ClockWindow

MAX_CONTRACT_RULE_ITEMS = 64
RATE_EXAMPLE_PRICES_EUR_PER_KWH = (-0.2, -0.1, 0.0, 0.1, 0.2)


def effective_instant(value: str, timezone: str | None) -> datetime:
    """Resolve an HA-local calendar date or retain an explicit absolute timestamp.

    Args:
        value: ISO calendar date or timezone-aware timestamp.
        timezone: Discovered HA installation zone. Required for calendar dates.

    Raises:
        ValueError: The date has no known installation zone or no real midnight.
    """
    if isinstance(value, str) and len(value) == 10:
        if not timezone:
            raise ValueError("Home Assistant installation timezone is required for calendar dates")
        day = date.fromisoformat(value)
        local = datetime.combine(day, datetime.min.time(), ZoneInfo(timezone))
        if local.astimezone(UTC).astimezone(local.tzinfo).replace(tzinfo=None) != local.replace(
            tzinfo=None
        ):
            raise ValueError("The selected date has no midnight in the Home Assistant timezone")
        return local
    return datetime.fromisoformat(value)


def parse_contract(
    body: dict, recorded_at: datetime, *, installation_timezone=None, validate_examples=True
) -> ContractSchedule:
    """Validate complete dated price rules without silently dropping unsupported terms.

    Args:
        body: Contract document. Each period may carry its supplier contract start,
            separately from its rule start. Both accept calendar dates or absolute timestamps.
        recorded_at: Absolute recording instant of this immutable revision.
        installation_timezone: HA zone, required for calendar dates. Overrides bonus zones.
        validate_examples: Check all five current display samples for new admission.
            Archive restoration retains its original three-sample validation policy.

    Raises:
        ValueError: Unsupported fields, invalid dates/formulas, discontinuous periods or
            inconsistent bonus counter policies. Nonexistent local midnights are rejected.
    """
    allowed = {
        "contract_id",
        "market_area",
        "currency",
        "periods",
        "expected_version",
        "bonuses",
    }
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("Unsupported contract fields")
    periods = body.get("periods")
    if not isinstance(periods, list) or not 1 <= len(periods) <= MAX_CONTRACT_RULE_ITEMS:
        raise ValueError("Expected 1-64 contract periods")
    converted = []
    for period in periods:
        required = {
            "effective_from",
            "effective_until",
            "import_formula",
            "export_formula",
        }
        if (
            not isinstance(period, dict)
            or not required <= set(period)
            or set(period) - required - {"contract_start"}
        ):
            raise ValueError("Unsupported contract period")
        converted.append(
            ContractPeriod(
                effective_instant(period["effective_from"], installation_timezone),
                effective_instant(period["effective_until"], installation_timezone)
                if period["effective_until"] is not None
                else None,
                Formula(period["import_formula"]),
                Formula(period["export_formula"]),
                effective_instant(period["contract_start"], installation_timezone)
                if period.get("contract_start") is not None
                else None,
            )
        )
    result = ContractSchedule(
        body["contract_id"],
        body["market_area"],
        body["currency"],
        recorded_at,
        tuple(converted),
        parse_bonuses(
            body.get("bonuses", []),
            installation_timezone=installation_timezone,
            periods=tuple(converted),
            contract_id=body["contract_id"],
        ),
    )
    # This mandatory preview catches undefined arithmetic at positive/zero/negative prices.
    # Every actual input is checked again. Samples cannot prove all prices valid.
    result.preview()
    if validate_examples:
        document = contract_document(result)
        samples = [sample for period in document["preview"] for sample in period]
        samples.extend(
            sample for bonus in document.get("bonus_preview", []) for sample in bonus["samples"]
        )
        if any(sample.get("issue") for sample in samples):
            raise ValueError("Undefined illustrative contract rate")
    return result


def rate_example(market, calculators):
    """Keep undefined illustrative rates unknown without hiding saved contract evidence.

    Existing contracts were validated at three prices. The two extra display
    samples must not make a previously readable or successfully saved rule fail.
    Actual planning prices still pass the existing independent validation.

    Args:
        market: Illustrative spot price in EUR/kWh.
        calculators: Output names mapped to callables that accept the spot price
            and calculate one rate in EUR/kWh.

    Returns:
        A mapping containing the market price and each calculated rate. If a
        calculator raises ValueError or ArithmeticError, its rate is None and
        issue is undefined_rate_example. A calculated zero remains zero.
    """
    sample = {"market": market}
    for name, calculate in calculators.items():
        try:
            sample[name] = calculate(market)
        except (ValueError, ArithmeticError):
            sample[name] = None
            sample["issue"] = "undefined_rate_example"
    return sample


def contract_document(contract: ContractSchedule | None) -> dict:
    """Expose original text, recorded/effective times, version and illustrative preview."""
    if contract is None:
        return {"status": "not_configured", "version": None}
    document = {
        "status": "configured",
        "version": contract.version,
        "contract_id": contract.contract_id,
        "market_area": contract.market_area,
        "currency": contract.currency,
        "recorded_at": contract.recorded_at.isoformat(),
        "periods": [
            {
                "effective_from": period.effective_from.isoformat(),
                "effective_until": period.effective_until.isoformat()
                if period.effective_until
                else None,
                "import_formula": period.import_formula.text,
                "export_formula": period.export_formula.text,
                **(
                    {"contract_start": period.contract_start.isoformat()}
                    if period.contract_start
                    else {}
                ),
            }
            for period in contract.periods
        ],
        "preview": [
            [
                rate_example(
                    market,
                    {
                        "buy_price": period.import_formula.evaluate,
                        "sell_price": period.export_formula.evaluate,
                    },
                )
                for market in RATE_EXAMPLE_PRICES_EUR_PER_KWH
            ]
            for period in contract.periods
        ],
    }
    if contract.bonuses:
        document["bonuses"] = [bonus_document(rule) for rule in contract.bonuses]
        document["bonus_preview"] = [
            {
                "rule_id": rule.rule_id,
                "effective_from": rule.effective_from.isoformat(),
                "scope": "rate_and_price_condition_only",
                "samples": [
                    rate_example(
                        market,
                        {
                            "extra_rate": lambda market, bonus_rule=rule: bonus_rule.extra_rate(
                                market, bonus_rule.effective_from, check_window=False
                            )
                        },
                    )
                    for market in RATE_EXAMPLE_PRICES_EUR_PER_KWH
                ],
            }
            for rule in contract.bonuses
        ]
    return document


def parse_bonuses(values, *, installation_timezone=None, periods=(), contract_id=None):
    """Translate bonus rules using the owning price rule's supplier contract boundary.

    Args:
        values: Strict bonus dictionaries. Optional technical IDs retain archived provenance.
        installation_timezone: HA zone replacing client bonus zones, required for calendar dates.
        periods: Ordered typed price rules supplying contract-start context.
        contract_id: App-managed identity used to derive stable opaque IDs for a supplier contract.

    Raises:
        ValueError: Unsupported fields, invalid dates, formulas, comparisons or reset policies.
            Date-only values require a usable installation zone and a real local midnight.
    """
    if not isinstance(values, list) or len(values) > MAX_CONTRACT_RULE_ITEMS:
        raise ValueError("Expected at most 64 bonus rules")
    rules = []
    allowed = {
        "rule_id",
        "effective_from",
        "rate_formula",
        "cap_kwh",
        "timezone",
        "reset_month",
        "reset_day",
        "price_condition",
        "window",
        "start_time",
        "end_time",
        "monthly_schedule",
    }
    for raw in values:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("Unsupported bonus fields")
        data = dict(raw)
        if installation_timezone is not None:
            data["timezone"] = installation_timezone
        data["effective_from"] = effective_instant(data["effective_from"], installation_timezone)
        owner = next(
            (
                period
                for period in reversed(periods)
                if period.effective_from <= data["effective_from"]
            ),
            None,
        )
        if owner is not None and owner.contract_start is not None:
            data["contract_start"] = owner.contract_start
            if not data.get("rule_id"):
                data["rule_id"] = uuid5(
                    NAMESPACE_URL, f"oep:{contract_id}:{owner.contract_start.isoformat()}"
                ).hex
        data["rate_formula"] = Formula(data["rate_formula"])
        if data.get("price_condition") is not None:
            condition = data["price_condition"]
            if not isinstance(condition, dict) or set(condition) != {
                "expression",
                "operator",
                "threshold",
            }:
                raise ValueError("Expected an explicit price comparison")
            if type(condition["threshold"]) not in (str, int, float):
                raise ValueError("Comparison threshold must be a decimal price")
            try:
                data["price_condition"] = ComparisonCondition(
                    Formula(condition["expression"]),
                    ComparisonOperator(condition["operator"]),
                    Decimal(str(condition["threshold"])),
                )
            except InvalidOperation as error:
                raise ValueError("Invalid comparison threshold") from error
        if "monthly_schedule" in data:
            schedule = data["monthly_schedule"]
            if not isinstance(schedule, list) or len(schedule) != MONTHS_PER_YEAR:
                raise ValueError("Monthly schedule requires twelve months")
            entries = []
            for entry in schedule:
                if entry is not None and (
                    not isinstance(entry, dict) or set(entry) != {"start_time", "end_time"}
                ):
                    raise ValueError("Expected a monthly clock range or null")
                entries.append(ClockWindow(**entry) if entry is not None else None)
            data["monthly_schedule"] = tuple(entries)
        rules.append(BonusRule(**data))
    return tuple(rules)


def bonus_document(rule):
    """Preserve the original separate extra-rate and comparison for a dated bonus rule."""
    data = asdict(rule)
    if rule.monthly_schedule:
        data["monthly_schedule"] = list(data["monthly_schedule"])
    else:
        data.pop("monthly_schedule")
    data.pop("contract_start")  # Derived from the owning price rule, never an independent setting.
    data["effective_from"] = rule.effective_from.isoformat()
    data["rate_formula"] = rule.rate_formula.text
    data["price_condition"] = (
        {
            "expression": rule.price_condition.expression.text,
            "operator": rule.price_condition.operator.value,
            "threshold": str(rule.price_condition.threshold),
        }
        if rule.price_condition
        else None
    )
    return data


def parse_counters(body, recorded_at):
    """Require explicit confirmed per-rule/per-period seed and cumulative export-meter provenance."""
    if not isinstance(body, dict) or set(body) - {"seeds", "expected_version"}:
        raise ValueError("Unsupported counter fields")
    values = body.get("seeds")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_CONTRACT_RULE_ITEMS:
        raise ValueError("Expected 1-64 counter seeds")
    seeds = []
    for raw in values:
        if not isinstance(raw, dict) or set(raw) - {"component_meters", "statistics"} != {
            "rule_id",
            "period_start",
            "used_kwh",
            "meter_kwh",
            "entity_id",
            "observed_at",
        }:
            raise ValueError("Invalid counter seed fields")
        data = dict(raw)
        if data.get("statistics") is not None:
            from open_energy_planner.contracts.bonus_statistics import StatisticsBasis

            data["statistics"] = StatisticsBasis.parse(data["statistics"])
        else:
            data.pop("statistics", None)
        for key in ("period_start", "observed_at"):
            data[key] = datetime.fromisoformat(data[key])
        seeds.append(CounterSeed(**data))
    return CounterBook(recorded_at, tuple(seeds))


def counter_document(book):
    """Expose the confirmed version and seeds without advancing them from a proposed plan."""
    if book is None:
        return {"status": "not_configured", "version": None}
    return {
        "status": "configured",
        "version": book.version,
        "recorded_at": book.recorded_at.isoformat(),
        "seeds": [
            {
                **{
                    key: value
                    for key, value in asdict(seed).items()
                    if key not in ("component_meters", "statistics") or value
                },
                "period_start": seed.period_start.isoformat(),
                "observed_at": seed.observed_at.isoformat(),
                **({"statistics": seed.statistics.document()} if seed.statistics else {}),
            }
            for seed in book.seeds
        ],
    }
