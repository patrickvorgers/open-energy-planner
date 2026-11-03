"""Continue the independent reference into new forecasts without synchronizing it to actual SoC."""

from datetime import datetime, timedelta
from math import isclose

from open_energy_planner.installation.location import SiteLocation

from ..contracts.compilation import BonusCompiler
from ..installation.model import slice_interval
from .context import physical_context
from .model import ENERGY_TOLERANCE_KWH, PendingSettlement
from .reference import dispatch

MAX_REFERENCE_PROJECTION = timedelta(hours=1)


def initial_state(store, documents, plans, document, contract):
    """Continue independent physical stock across the bounded Recorder delay.

    Prefer the physical checkpoint, falling back to the published financial state
    for existing archives. Stocks are in kWh and are never reset to actual SoC.
    Proven financial counters require the same end, capacities, contract and
    working counter version. Any bonus evidence in the new plan must also name
    that counter version. Battery IDs must match and stock differences must
    be within ENERGY_TOLERANCE_KWH, with no relative tolerance.

    Args:
        store: App-owned settlement archive containing frames and financial states.
        documents: Shared savings namespace containing physical/financial pointers.
        plans: Original immutable plan repository supplying retained forecasts.
        document: New plan whose first interval defines the projection endpoint.
        contract: Current tariff/bonus schedule, or None without configured tariffs.

    Returns:
        A dict with stored, counters, status and estimated. Before initialization,
        only these four keys exist, stored is None and counters is empty.
        With a continuation, also return state_at, projected_until,
        evaluation_revision and financial_issue. Initially reuse counters and the
        head evaluation_revision only when physical/financial states align.
        Otherwise start counters empty and evaluation_revision is None. Projection
        may initialize a newly observed annual period at its exact opening.
        A missing projected bonus baseline sets financial_issue while still
        returning physical stored energy. Later comparison also checks the
        required opening budgets. This read-only function never saves a checkpoint.

    Raises:
        PendingSettlement: A long gap, missing original forecast context or hardware
            transition prevents a supported projection. Normal planning continues
            without a physical reference, preserving the last valid stored state.
    """
    head = documents.read("head.json") if documents.exists("head.json") else None
    financial = store.state(head["revision"]) if head else None
    state = (
        documents.read("physical-reference.json")
        if documents.exists("physical-reference.json")
        else financial
    )
    if state is None:
        return {
            "stored": None,
            "counters": {},
            "status": "provisional_common_start",
            "estimated": False,
        }
    problem = physical_context(document)
    until = problem.intervals[0].start
    cursor = datetime.fromisoformat(state["end"])
    if not timedelta(0) <= until - cursor <= MAX_REFERENCE_PROJECTION:
        raise PendingSettlement(
            "Continuous reference awaits recent metering. No SoC reset was performed"
        )
    previous_id = store.context_plan(cursor)
    previous = plans.get_plan(previous_id) if previous_id else None
    if previous is None:
        raise PendingSettlement("Reference forecast context is missing")
    old_problem = physical_context(previous)
    old_bindings = previous.get("comparison_bindings", {})
    changed = store.next_configuration(cursor, until, old_bindings)
    capacities = {
        battery.device_id: battery.battery.capacity_kwh for battery in old_problem.batteries
    }
    new_capacities = {
        battery.device_id: battery.battery.capacity_kwh for battery in problem.batteries
    }
    if changed or capacities != state["capacities"] or capacities != new_capacities:
        raise PendingSettlement("Reference awaits metering across the installation change")
    stored = dict(state["reference_stored"])
    working = documents.read("working.json") if documents.exists("working.json") else None
    # Changed contract or counter provenance cannot supply proven opening quota.
    aligned = (
        financial is not None
        and financial["end"] == state["end"]
        and financial["reference_stored"].keys() == state["reference_stored"].keys()
        and all(
            isclose(
                value, state["reference_stored"][identity], rel_tol=0, abs_tol=ENERGY_TOLERANCE_KWH
            )
            for identity, value in financial["reference_stored"].items()
        )
        and financial["capacities"] == state["capacities"]
        and (contract is None or head["contract_version"] == contract.version)
        and (working is None or working.get("counter_version") == head.get("counter_version"))
        and all(
            evidence.get("counter_version") == head.get("counter_version")
            for evidence in document["input_snapshot"].get("bonus_evidence", ())
        )
    )
    counters = dict(financial["counters"]["reference"]) if aligned else {}
    financial_issue = None
    families = BonusCompiler().families(contract) if contract else {}
    site = document["input_snapshot"].get("site_location") or previous["input_snapshot"].get(
        "site_location"
    )
    location = SiteLocation(**site) if site else None
    while cursor < until:
        source = next(
            (
                interval
                for interval in old_problem.intervals
                if interval.start <= cursor < interval.start + timedelta(hours=interval.duration_h)
            ),
            None,
        )
        if source is None:
            raise PendingSettlement("Retained forecast cannot bridge the reference metering delay")
        right = min(until, source.start + timedelta(hours=source.duration_h))
        if contract:
            edges = BonusCompiler().boundaries(contract, cursor, right, location)
            right = min([right, *(edge for edge in edges if cursor < edge < right)])
        step = slice_interval(source, cursor, right)
        flow = dispatch(old_problem, step, stored)
        stored = flow["stored"]
        for identity, versions in families.items():
            rule = next((row for row in reversed(versions) if row.effective_from <= cursor), None)
            rule = rule or versions[0]
            key = identity + "|" + rule.period_start(cursor).isoformat()
            if key not in counters:
                if rule.period_start(cursor) != cursor:
                    financial_issue = "Reference bonus baseline is awaiting evaluation"
                    continue
                counters[key] = 0
            counters[key] += flow["export_kwh"]
        cursor = right
    return {
        "stored": stored,
        "counters": counters,
        "status": "continuous_reference",
        "estimated": until != datetime.fromisoformat(state["end"]),
        "state_at": state["end"],
        "projected_until": until.isoformat(),
        "evaluation_revision": head["revision"] if aligned else None,
        "financial_issue": financial_issue,
    }
