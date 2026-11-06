"""App-owned settlement orchestration. Original plans and measured evidence are never rewritten."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from itertools import pairwise

from open_energy_planner.contracts.wire import parse_counters
from open_energy_planner.installation.location import SiteLocation

from ..contracts.bonus import CounterSeed
from .accounting import evaluate
from .advice import active_advice
from .bonus import HistoricalBonuses
from .context import physical_context
from .model import (
    MAX_BATCH_INTERVALS,
    POLICY_VERSION,
    SETTLEMENT_INTERVAL,
    PendingSettlement,
    parse_frame,
)
from .physical import advance
from .repricing import corrected_problem

SETTLEMENT_RECORDER_DELAY = timedelta(minutes=10)
MAX_EVALUATION_VERSIONS = 20
SHA256_HEX_LENGTH = 64

# Only fixed owned guidance is public. Other caught exceptions may contain stored or driver values.
PENDING_SETTLEMENT_MESSAGES = {
    "A reset inside the bonus anchor interval prevents exact reconstruction": "An export meter reset prevents reconstruction of the bonus starting total.",
    "Confirmed bonus meter disagrees with its historical boundary": "The confirmed export reading disagrees with the reading in history.",
    "Confirmed bonus meter is outside its historical interval": "The confirmed export reading falls outside the historical interval's readings.",
    "Continuous reference awaits recent metering. No SoC reset was performed": "Recent meter readings are missing from the continuous comparison scenario.",
    "Historical bonus anchor does not match the export meters or period": "The bonus starting reading belongs to different export meters or a different period.",
    "Historical bonus baseline awaits captured export evidence": "The captured export readings needed to establish the bonus starting total are missing.",
    "Historical bonus baseline awaits completed hourly statistics": "The hourly export statistics needed for the bonus starting total are not complete yet.",
    "Historical bonus baseline is being read from Home Assistant": "The bonus starting total is still being read from Home Assistant.",
    "Historical bonus counter archive is unreadable": "The stored bonus starting totals cannot currently be read.",
    "Historical bonus export evidence has a gap or changed meters": "Historical export readings have a gap or use different export meters.",
    "Historical bonus history is incomplete or invalid": "The received bonus export history is incomplete or invalid.",
    "Historical bonus history unavailable: history_unavailable": "Historical export measurements are unavailable from Home Assistant.",
    "Historical bonus history unavailable: recorder_unavailable": "Home Assistant Recorder is unavailable for the historical export read.",
    "Historical bonus history unavailable: unsupported_statistics": "Home Assistant cannot supply supported statistics for these export meters.",
    "Historical bonus period does not start on a whole UTC hour. Confirm a manual baseline": "The bonus period starts between hourly readings and needs a confirmed manual starting total.",
    "Historical exports exceed the confirmed used bonus volume": "Historical exports exceed the confirmed export total used for the bonus.",
    "No original installation plan covers this interval": "The original energy plan for this measurement interval is missing.",
    "Published price coverage is missing": "Published electricity prices are missing for this measurement interval.",
    "Published prices changed during settlement. Retrying": "Published prices changed while this measurement interval was being evaluated.",
    "Reference awaits metering across the installation change": "Meter readings are missing across an installation change.",
    "Reference forecast context is missing": "The captured inputs for the original comparison forecast are missing.",
    "Retained forecast cannot bridge the reference metering delay": "The retained forecast cannot cover the gap in comparison meter readings.",
    "Validated physical bonus endpoint is unavailable": "The confirmed export measurement needed to continue the bonus calculation is unavailable.",
}
SETTLEMENT_VALIDATION_MESSAGES = (
    "Contract coverage is missing",
    "Reference continuity requires the missing interval first",
)


class EvaluationState(StrEnum):
    """Distinguish accepted publication, expected waiting and rejected evidence."""

    COMPLETE = "complete"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class EvaluationResult:
    """Describe one measured batch without confusing committed intervals with published results."""

    state: EvaluationState
    processed: int
    total: int
    waiting_start: str | None = None
    waiting_end: str | None = None


def settlement_activity_detail(result, issue):
    """Explain the actual bounded attempt using only admitted causes and owned recovery advice."""
    progress = f"This attempt processed {result.processed} of {result.total} measured intervals. "
    if result.state == EvaluationState.COMPLETE:
        return progress + "Measured savings were evaluated and the result was published."
    if result.waiting_start is None:
        reason = "The correction has not yet caught up with the previously published period. The next background check continues with the next batch."
    elif issue == "Published price coverage is missing":
        reason = "The next background check tries again. No action is needed while waiting for price publication. If this persists, check price sources under Installation."
    elif issue == "Published prices changed during settlement. Retrying":
        reason = "The next background check uses the updated prices. No action is needed."
    elif issue in (
        "Historical bonus baseline is being read from Home Assistant",
        "Historical bonus baseline awaits completed hourly statistics",
        "Historical bonus baseline awaits captured export evidence",
    ):
        reason = "The historical export total needed for the bonus is not yet available. Home Assistant history is being collected or is not complete yet. The next background check tries again. Check the bonus budget under Contract if this persists."
    elif issue in (
        "Historical bonus history unavailable: history_unavailable",
        "Historical bonus history unavailable: recorder_unavailable",
        "Historical bonus history unavailable: unsupported_statistics",
    ):
        reason = "Home Assistant cannot currently supply the historical export statistics needed for the bonus. Check Recorder and export statistics in Home Assistant, then check the bonus budget under Contract. The next background check tries again."
    elif issue and issue in PENDING_SETTLEMENT_MESSAGES and "bonus" in issue.lower():
        reason = "The retained export history or bonus baseline cannot yet establish an exact historical total. Review the export meters and bonus baseline under Contract. The next background check rechecks the evidence. If it remains unresolved, report this activity for investigation."
    elif issue in (
        "Continuous reference awaits recent metering. No SoC reset was performed",
        "Reference awaits metering across the installation change",
        "Retained forecast cannot bridge the reference metering delay",
    ):
        reason = "The comparison scenario needs continuous meter history for the indicated interval. Check that Home Assistant records the required meters without gaps. The next background check rechecks the evidence. The comparison's battery state is not reset."
    elif issue == "Reference forecast context is missing":
        reason = "The original comparison forecast evidence is missing for the indicated interval. Inspect the original plan under Planning and report this activity if the evidence remains unavailable. The next background check rechecks the evidence."
    elif issue == "No original installation plan covers this interval":
        reason = "No original energy plan covers the indicated measurement interval. Check the retained plans under Planning. If that historical plan is unavailable, report this activity. A new plan does not reconstruct missing original advice."
    elif issue == "Contract coverage is missing":
        reason = "No contract price rule covers the indicated interval. Add or correct the dated rule under Contract. The next background check uses the saved contract."
    elif issue == "Reference continuity requires the missing interval first":
        reason = "An earlier measurement interval is missing, so the comparison cannot continue chronologically. Check meter history in Home Assistant and the coverage under Savings. The next background check rechecks the evidence."
    else:
        reason = "The required historical evidence is incomplete or invalid. The exact missing or invalid item could not be identified. Open Savings to check the available coverage. If the cause remains unclear, report this activity for investigation. The next background check rechecks the evidence."
    cause = PENDING_SETTLEMENT_MESSAGES.get(issue)
    return (
        progress
        + (cause + " " if cause else "")
        + reason
        + " Previous published financial results remain available. Refreshing Activity reloads history and does not start a calculation."
    )


def settlement_issue(error):
    """Project safe evidence guidance without copying retained values or arbitrary exception text."""
    messages = (
        PENDING_SETTLEMENT_MESSAGES
        if isinstance(error, PendingSettlement)
        else SETTLEMENT_VALIDATION_MESSAGES
    )
    for message in messages:
        if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] == message:
            return message
    if isinstance(error, PendingSettlement):
        return (
            "Settlement is waiting for complete validated evidence. "
            "Existing successful results are retained"
        )
    return (
        "Retained settlement evidence is invalid. Check recorded meter data and published prices. "
        "Existing successful results are retained"
    )


def version(value):
    """Hash finite detached settlement inputs without connection or provider details."""
    return sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def quarter(instant):
    """Floor an aware instant to a settlement boundary in UTC."""
    instant = instant.astimezone(UTC)
    seconds = int(SETTLEMENT_INTERVAL.total_seconds())
    return datetime.fromtimestamp(int(instant.timestamp()) // seconds * seconds, UTC)


class SavingsRuntime:
    """Keep ingestion, continuous scenario state and correction generations outside planning."""

    def __init__(self, store, documents, plans):
        """Borrow app-owned stores. A dedicated settlement lock serializes mutations."""
        self.store, self.documents, self.plans = store, documents, plans
        self.bonus = HistoricalBonuses(store)
        self.issue = None
        self.attention = False
        self.events = None
        self.physical_issue = None
        self._bindings_plan, self._bindings = None, None

    def request(self, now, settings, solar, demand, seeds, location):
        """Request at most one day after first publication. Wait for completed Recorder statistics."""
        if settings is None or solar is None or demand is None:
            return None
        first = self.store.first_activation()
        if first is None:
            return None
        start = quarter(datetime.fromisoformat(first)) + SETTLEMENT_INTERVAL
        if self.documents.exists("capture.json"):
            start = datetime.fromisoformat(self.documents.read("capture.json")["end"])
        end = min(
            quarter(now - SETTLEMENT_RECORDER_DELAY),
            start + SETTLEMENT_INTERVAL * MAX_BATCH_INTERVALS,
        )
        if start >= end:
            return None
        bindings = {
            "terms": [asdict(energy_term) for energy_term in demand.terms],
            "solar": [asdict(source) for source in solar.sources],
            "batteries": [asdict(battery) for battery in settings.batteries],
        }
        plan_id = self.store.context_plan(start)
        if plan_id:
            if self._bindings_plan != plan_id:
                original = self.plans.get_plan(plan_id)
                self._bindings = original.get("comparison_bindings")
                self._bindings_plan = plan_id
            bindings = self._bindings or bindings
        changed = self.store.next_configuration(start, end, bindings)
        if changed:
            end = datetime.fromisoformat(changed)
        request = {"start": start.isoformat(), "end": end.isoformat(), "bindings": bindings}
        request["version"] = version(request)
        return request

    def receive(
        self,
        pending: dict,
        body: object,
        now: datetime,
        seeds: tuple[CounterSeed, ...],
        location: SiteLocation | None,
    ) -> None:
        """Validate a requested contiguous prefix before atomically archiving it.

        Each frame spans at most SETTLEMENT_INTERVAL (a timedelta representing
        fifteen minutes). The first start must match the requested start, which
        may be off-grid after a configuration change. Interior frame ends must
        align to the UTC quarters returned by quarter(). A prefix may stop at a
        complete quarter before the requested end. Only the final frame may end
        off-grid, and only at that requested end (a possible configuration-change
        instant). Frame documents kWh units, sign rules and reconstruction quality.

        Args:
            pending: App-owned versioned request with aware bounds and frozen
                Energy bindings. It is not supplied by the reporting client.
            body: Untrusted JSON response with request identity, frames and
                disjoint import/export meter identities.
            now: Aware current time. Future measurement intervals are rejected.
            seeds: Confirmed opening bonus positions retained with the evidence.
            location: Optional installation coordinates/timezone for later billing.

        Returns:
            None after immutable frames and capture.json have committed in one
            transaction and the previous ingestion issue has been cleared.

        Raises:
            ValueError: Request identity, interval coverage, contiguity, quarter
                boundaries, meter roles, channels or finite energy values are
                invalid, or stored immutable evidence conflicts with this batch.
            TypeError: Timestamp fields have unsupported JSON types.

        All intake validation finishes before any write. Storage conflicts or
        other transaction failures leave no durable partial frames or pointer
        advance. The caller serializes ingestion with other settlement mutations.
        """
        if not isinstance(body, dict) or body.get("request_version") != pending["version"]:
            raise ValueError("Settlement request changed")
        if set(body) != {"request_version", "frames", "imports", "exports"}:
            raise ValueError("Invalid settlement response")
        frames = body["frames"]
        if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_BATCH_INTERVALS:
            raise ValueError("Invalid settlement batch")
        frames = tuple(parse_frame(row) for row in frames)
        start, end = (
            datetime.fromisoformat(pending["start"]),
            datetime.fromisoformat(pending["end"]),
        )
        if frames[0].start != start or frames[-1].end > end or frames[-1].end > now:
            raise ValueError("Settlement response outside requested coverage")
        if any(
            previous_item.end != next_item.start for previous_item, next_item in pairwise(frames)
        ):
            raise ValueError("Settlement batch has gaps or overlaps")
        # Only request edges may split a quarter when physical configuration changes.
        if any(row.end != quarter(row.end) for row in frames[:-1]) or (
            frames[-1].end != end and frames[-1].end != quarter(frames[-1].end)
        ):
            raise ValueError("Settlement frame does not end on a requested quarter boundary")
        terms = {
            energy_term["entity_id"]: energy_term["sign"]
            for energy_term in pending["bindings"]["terms"]
        }
        for kind, sign in (("imports", 1), ("exports", -1)):
            ids = body[kind]
            if (
                not isinstance(ids, list)
                or len(ids) != len(set(ids))
                or any(terms.get(device_id) != sign for device_id in ids)
                or (kind in pending["bindings"] and set(ids) != set(pending["bindings"][kind]))
            ):
                raise ValueError("Grid settlement meters do not match Energy configuration")
        if not body["imports"]:
            raise ValueError("Grid import measurement is required")
        allowed = (
            set(terms)
            | {solar_flow["entity_id"] for solar_flow in pending["bindings"]["solar"]}
            | {battery["energy_entity"] for battery in pending["bindings"]["batteries"]}
        )
        batteries = {battery["device_id"] for battery in pending["bindings"]["batteries"]}
        for frame in frames:
            if (
                set(frame.channels) != allowed
                or set(frame.meter_start) - allowed
                or set(frame.battery_start) != batteries
                or set(frame.battery_end) != batteries
            ):
                raise ValueError("Settlement channels do not match requested Energy bindings")
        context = json.loads(
            json.dumps(
                {
                    "bindings": {
                        **pending["bindings"],
                        "imports": body["imports"],
                        "exports": body["exports"],
                    },
                    "seeds": [asdict(seed_item) for seed_item in seeds],
                    "location": asdict(location) if location else None,
                },
                default=lambda value_item: value_item.isoformat(),
            )
        )
        # Raw capture and pointer share the same database transaction.
        with self.documents.transaction():
            self.store.put_frames(frames, context, self.documents.pending.connection)
            self.documents.write("capture.json", {"end": frames[-1].end.isoformat()})
        self.issue = None

    def advance_reference(self):
        """Persist counter-free physical progress before attempting financial settlement.

        Bootstrap from an existing successful continuation once, preserving its
        independent stock. Subsequent contract/counter corrections never rewind it.
        The settlement lock owned by the caller serializes this checkpoint with intake.
        """
        name = "physical-reference.json"
        state = self.documents.read(name) if self.documents.exists(name) else None
        if state is None and self.documents.exists("head.json"):
            head = self.documents.read("head.json")
            previous = self.store.state(head["revision"]) if head else None
            if previous:
                state = {
                    key: previous[key]
                    for key in ("end", "reference_stored", "actual_stored", "capacities")
                }
                self.documents.write(name, state)
        for row in self.store.frames(datetime.fromisoformat(state["end"]) if state else None):
            try:
                frame = parse_frame(row["frame"])
                plan_id = self.store.context_plan(frame.start)
                plan = self.plans.get_plan(plan_id) if plan_id else None
                if plan is None:
                    raise PendingSettlement("No original installation plan covers this interval")
                bindings = dict(row["context"]["bindings"])
                advice = active_advice(
                    frame, self.store.activations(frame.start, frame.end), self.plans
                )
                bindings["curtailed"] = {key: True for key in advice["possible_curtailment"]}
                state = advance(physical_context(plan), frame, bindings, state)["state"]
                self.documents.write(name, state)
            except (ValueError, KeyError, TypeError) as error:
                self.physical_issue = settlement_issue(error)
                return
        self.physical_issue = None

    def process(self, contract, now, book=None):
        """Evaluate bounded chronological batches. Replay on contract or already-settled price corrections."""
        self.advance_reference()
        if contract is None:
            self.issue = "Contract configuration is required for settlement"
            return
        published = self.documents.read("head.json") if self.documents.exists("head.json") else None
        working = (
            self.documents.read("working.json") if self.documents.exists("working.json") else None
        )
        head = working or published
        published_state = self.store.state(published["revision"]) if published else None
        old_state = self.store.state(head["revision"]) if head else None
        price_cursor = self.store.price_cursor(contract.market_area, contract.currency)
        corrections = (
            self.store.corrected_prices(
                datetime.fromisoformat(old_state["end"]),
                head.get("price_cursor", 0),
                contract.market_area,
                contract.currency,
                price_cursor,
            )
            if old_state and head
            else ()
        )
        counter_version = book.version if book else None
        if (
            head is None
            or head["contract_version"] != contract.version
            or head.get("counter_version") != counter_version
            or corrections
        ):
            head = {
                "revision": version(
                    [
                        contract.version,
                        head,
                        [tuple(row_item) for row_item in corrections],
                        POLICY_VERSION,
                    ]
                ),
                "contract_version": contract.version,
                "counter_version": counter_version,
                "created_at": now.isoformat(),
                "previous_revision": published["revision"] if published else None,
                "price_cursor": price_cursor,
            }
            self.documents.write("working.json", head)
        state = self.store.state(head["revision"])
        rows = self.store.frames(datetime.fromisoformat(state["end"]) if state else None)
        if self.events and rows:
            from ..activities.events import Health, Outcome

            first_frame, last_frame = rows[0].get("frame"), rows[-1].get("frame")
            activity_context = (
                {"start": first_frame.get("start"), "end": last_frame.get("end")}
                if isinstance(first_frame, dict) and isinstance(last_frame, dict)
                else None
            )
            with self.events.thread_attempt(
                "settlement",
                "settlement",
                "Evaluate measured savings",
                context=activity_context,
                failure="Settlement processing failed. Last successful financial results remain available.",
            ) as identity:
                evaluation = self._evaluate_rows(
                    contract, now, book, head, published_state, price_cursor, state, rows
                )
                usable = evaluation.state == EvaluationState.COMPLETE
                pending = evaluation.state == EvaluationState.PENDING
                self.events.finish_thread(
                    identity,
                    Outcome.COMPLETED if usable else Outcome.SKIPPED if pending else Outcome.FAILED,
                    settlement_activity_detail(evaluation, self.issue),
                    usable=usable,
                    context={
                        "waiting_start": evaluation.waiting_start,
                        "waiting_end": evaluation.waiting_end,
                    },
                )
                self.events.availability_thread(
                    "settlement",
                    Health.AVAILABLE if usable else Health.UNKNOWN if pending else Health.DEGRADED,
                    "Measured savings are current."
                    if usable
                    else "Waiting for complete savings evidence. Previous financial results remain available."
                    if pending
                    else "Previous financial results are retained. Check Savings for missing evidence.",
                )
        else:
            self._evaluate_rows(
                contract, now, book, head, published_state, price_cursor, state, rows
            )

    def _evaluate_rows(self, contract, now, book, head, published_state, price_cursor, state, rows):
        """Evaluate captured intervals before publishing a successful head.

        Args:
            contract: Current terms whose currency and market are recorded per result.
            now: Aware evaluation time used for financial evidence and metadata.
            book: Optional counter book used to prepare bonus evidence.
            head: Working evaluation metadata, including its revision.
            published_state: Previously published continuation state, if available.
            price_cursor: Price cursor retained after the complete batch.
            state: Continuation state for the next captured interval.
            rows: Captured measurements and contexts in chronological order.

        Returns:
            A result describing COMPLETE after accepted publication, PENDING while evidence or further
            correction batches are needed, or FAILED for rejected evidence or a
            missing original plan. These states do not depend on display text.

        Each successful interval is committed immediately. An early return records
        self.issue and, for handled evaluation errors, self.attention. Earlier
        interval commits remain stored. Only a complete batch writes the new head
        or working state, preserving the previous successful publication.
        """
        counter_version = book.version if book else None
        for processed, row in enumerate(rows):
            frame = parse_frame(row["frame"])
            context = row["context"]
            plan_id = self.store.context_plan(frame.start)
            if plan_id is None:
                self.issue = "No original installation plan covers this interval"
                return EvaluationResult(
                    EvaluationState.FAILED,
                    processed,
                    len(rows),
                    frame.start.isoformat(),
                    frame.end.isoformat(),
                )
            plan = self.plans.get_plan(plan_id)
            try:
                problem = physical_context(plan)
                original_contract = plan["input_snapshot"].get("contract_version")
                if original_contract and original_contract != contract.version:
                    problem, _ = corrected_problem(plan, contract)
                bindings = dict(context["bindings"])
                advice = active_advice(
                    frame, self.store.activations(frame.start, frame.end), self.plans
                )
                bindings["curtailed"] = {key: True for key in advice["possible_curtailment"]}
                seeds = (
                    parse_counters({"seeds": context["seeds"]}, now).seeds
                    if context["seeds"]
                    else ()
                )
                seeds = self.bonus.prepare(
                    contract, frame, bindings, seeds, book, now, version(bindings), state
                )
                prices = self.store.published(
                    frame.start, frame.end, contract.market_area, contract.currency
                )
                result, continuation = evaluate(
                    problem,
                    frame,
                    bindings,
                    state,
                    contract,
                    seeds,
                    prices,
                    SiteLocation(**context["location"]) if context["location"] else None,
                )
                result.update(
                    plan_id=plan_id,
                    strategy=plan["strategy"],
                    evaluated_at=now.isoformat(),
                    revision=head["revision"],
                    contract_version=contract.version,
                    currency=contract.currency,
                    market_area=contract.market_area,
                    measurements=frame.record(),
                    advice=advice,
                    counter_version=counter_version,
                    device_names=plan.get("comparison_names", {}),
                    counter_seeds=json.loads(
                        json.dumps(
                            [asdict(seed) for seed in seeds],
                            default=lambda value: value.isoformat(),
                        )
                    ),
                )
                self.store.commit(head["revision"], result, continuation)
                state = continuation
                self.issue = None
            except (ValueError, KeyError, TypeError) as error:
                self.issue = settlement_issue(error)
                self.attention = not isinstance(error, PendingSettlement)
                return EvaluationResult(
                    EvaluationState.PENDING
                    if isinstance(error, PendingSettlement)
                    else EvaluationState.FAILED,
                    processed,
                    len(rows),
                    frame.start.isoformat(),
                    frame.end.isoformat(),
                )
        head["price_cursor"] = price_cursor
        with self.documents.transaction():
            if state and (published_state is None or state["end"] >= published_state["end"]):
                self.documents.write(f"evaluation-{head['revision']}.json", head)
                self.documents.write("head.json", head)
                self.documents.write("working.json", None)
            else:
                self.documents.write("working.json", head)
        pending = published_state is not None and (
            state is None or state["end"] < published_state["end"]
        )
        self.issue = (
            "Correction is still being evaluated. The previous successful version remains visible"
            if pending
            else None
        )
        self.attention = False
        return EvaluationResult(
            EvaluationState.PENDING if pending else EvaluationState.COMPLETE, len(rows), len(rows)
        )

    def overview(self, start, end, revision=None):
        """Filter stored nonoverlapping results. Never reset either scenario or run a historical optimizer."""
        head = self.documents.read("head.json") if self.documents.exists("head.json") else None
        versions, next_revision = self.evaluation_versions()
        if revision:
            if len(revision) != SHA256_HEX_LENGTH or any(
                candidate not in "0123456789abcdef" for candidate in revision
            ):
                raise ValueError("Invalid evaluation version")
            head = self.documents.read(f"evaluation-{revision}.json")
        rows = self.store.results(head["revision"], start, end) if head else []
        summary, daily = self.store.summary(head["revision"] if head else "", start, end)
        totals = {
            scenario: {
                key: summary[f"{scenario}_{key}"]
                for key in (
                    "purchase",
                    "sale",
                    "bonus",
                    "grid_cash",
                    "wear",
                    "inventory_change",
                    "economic_cost",
                    "import_kwh",
                    "export_kwh",
                )
            }
            for scenario in ("actual", "reference")
        }
        expected = int((end - start) / SETTLEMENT_INTERVAL)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "revision": head["revision"] if head else None,
            "contract_version": head["contract_version"] if head else None,
            "issue": self.issue if not revision else None,
            "intervals": rows,
            "chart": daily,
            "details_truncated": len(rows) < summary["count"],
            "totals": totals,
            "savings": summary["savings"],
            "avoided_import_kwh": summary["avoided_import_kwh"],
            "coverage": {
                "evaluated": summary["duration_h"] / (SETTLEMENT_INTERVAL.total_seconds() / 3600),
                "expected": expected,
                "estimated": summary["estimated"],
            },
            "reference": "Simulated self-consumption without EMS",
            "versions": versions,
            "next_evaluation": next_revision,
            "corrected": bool(head and head.get("previous_revision")),
        }

    def evaluation_versions(self, before=None):
        """Page immutable correction descriptors without exposing or loading interval payloads."""
        if before and (
            len(before) != SHA256_HEX_LENGTH
            or any(candidate not in "0123456789abcdef" for candidate in before)
        ):
            raise ValueError("Invalid evaluation cursor")
        current = (
            self.documents.read(f"evaluation-{before}.json")
            if before
            else (self.documents.read("head.json") if self.documents.exists("head.json") else None)
        )
        versions = []
        while current and len(versions) < MAX_EVALUATION_VERSIONS:
            versions.append(
                {key: current.get(key) for key in ("revision", "created_at", "contract_version")}
            )
            parent = current.get("previous_revision")
            if len(versions) == MAX_EVALUATION_VERSIONS:
                return versions, parent
            current = self.documents.read(f"evaluation-{parent}.json") if parent else None
        return versions, None
