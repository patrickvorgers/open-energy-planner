"""Authenticated contract, bonus-counter and annual history HTTP boundaries."""

import asyncio
import logging
from datetime import UTC, datetime

from aiohttp import web

from open_energy_planner.contracts.bonus import CounterBook
from open_energy_planner.contracts.bonus_statistics import (
    MAX_STATISTICS_REPORT_BYTES,
    derive_statistics,
    invalidate_statistics,
    parse_statistics,
)
from open_energy_planner.contracts.compilation import BonusCompiler
from open_energy_planner.contracts.export_meter import export_reading, export_sources
from open_energy_planner.contracts.notifications import (
    contract_notifications,
    explain_contract_problem,
    negative_export_notifications,
)
from open_energy_planner.contracts.wire import (
    contract_document,
    counter_document,
    parse_contract,
    parse_counters,
)
from open_energy_planner.home_assistant.catalogue import COUNTRY_BIDDING_AREA_OPTIONS
from open_energy_planner.notifications.api import NOTIFICATIONS

MAX_HISTORY_DIAGNOSTIC_CHARACTERS = 256
_LOGGER = logging.getLogger(__name__)


class ContractAPI:
    """Own contract editing, explicit bonus seeds and bounded statistics requests under the shared app lock."""

    def __init__(
        self,
        app,
        contracts,
        bonus_counters,
        bonus_history,
        bonus_preview,
        live,
        setup_catalogue,
        prices,
        lock,
        persist_then_install,
        snapshot,
        pending_contract_id,
        *,
        planning_status,
    ):
        """Borrow the app-owned stores and lifecycle callbacks while retaining request-local bonus history faults."""
        self.contracts = contracts
        self.bonus_counters = bonus_counters
        self.bonus_history = bonus_history
        self.bonus_preview = bonus_preview
        self.live = live
        self.setup_catalogue = setup_catalogue
        self.prices = prices
        self.lock = lock
        self.persist_then_install = persist_then_install
        self.snapshot = snapshot
        self.pending_contract_id = pending_contract_id
        self.planning_status = planning_status
        self.history_issue = None
        self.history_diagnostic = None
        app[NOTIFICATIONS].register("contract-statistics", self.current_statistics_notifications)
        app[NOTIFICATIONS].register(
            "contracts", self.current_notifications, explain=explain_contract_problem
        )
        app.router.add_get("/v1/contracts", self.contract_settings)
        app.router.add_post("/v1/contracts/preview", self.preview_contract)
        app.router.add_put("/v1/contracts", self.configure_contract)
        app.router.add_get("/v1/bonus-counters", self.counter_settings)
        app.router.add_put("/v1/bonus-counters", self.configure_counters)
        app.router.add_get("/v1/contracts/budgets", self.bonus_budgets)
        app.router.add_post("/v1/contracts/budgets", self.confirm_bonus_budget)
        app.router.add_post("/v1/history", self.receive_history)

    def current_notifications(self):
        """Read contract-owned explanations from accepted state without refreshing or persisting inputs."""
        return contract_notifications(
            self.planning_status(),
            self.history_issue,
            export_meters=tuple(spec.entity_id for spec in export_sources(self.live.configuration))
            if self.live and self.live.configuration
            else (),
        )

    def current_statistics_notifications(self):
        """Read accepted statistics evidence independently of coordinator readiness or failure."""
        meters = self.negative_export_meters(datetime.now(UTC))
        return None if meters is None else negative_export_notifications(meters)

    def negative_export_meters(self, now):
        """Report active-rule meters below their references, retaining unknown evidence as unavailable."""
        if self.contracts.issue or self.bonus_counters.issue:
            return None
        if not self.contracts.current:
            return ()
        active_bonus = self.contracts.current.bonus_at(now)
        if not active_bonus:
            return ()
        if not self.bonus_counters.current or not self.live or not self.live.configuration:
            return None
        try:
            evidence = BonusCompiler().current_evidence(
                self.contracts.current,
                now,
                self.bonus_counters.current,
                self.live.configuration,
                self.live.report,
                identities={active_bonus.rule_id},
            )
        except ValueError:
            # A separate blocker cannot confirm recovery of this statistics advisory.
            return None
        periods = {
            (budget_evidence.rule_id, budget_evidence.period_start) for budget_evidence in evidence
        }
        return tuple(
            sorted(
                {
                    meter_reference.statistic_id
                    for seed in self.bonus_counters.current.seeds
                    if (seed.rule_id, seed.period_start) in periods and seed.statistics
                    for meter_reference in seed.statistics.series
                    if meter_reference.latest_sum < meter_reference.reference_sum
                }
            )
        )

    async def contract_settings(self, _request):
        """Read current app-owned rules and cache diagnostics without changing any history."""
        async with self.lock:
            return web.json_response(
                {
                    **contract_document(self.contracts.current),
                    "contract_id": self.contracts.current.contract_id
                    if self.contracts.current
                    else self.pending_contract_id,
                    "issue": self.contracts.issue,
                }
            )

    async def preview_contract(self, request):
        """Validate rates and preview current export budgets without publishing rules.

        The latest valid draft may request HA history through a bounded temporary
        cache. Only saved rules and confirmed counters can affect planning.
        """
        try:
            candidate = parse_contract(
                await request.json(),
                datetime.now(UTC),
                installation_timezone=(self.setup_catalogue.value or {}).get("timezone"),
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid or unsupported contract formulas") from None
        async with self.lock:
            self.validate_contract_identity(candidate)
            now = datetime.now(UTC)
            self.bonus_preview.update(candidate, now)
            result = contract_document(candidate)
            result["bonus_budget_preview"] = [
                self.bonus_budget_row(
                    candidate,
                    rule,
                    self.bonus_preview.baselines(self.bonus_counters.current, now),
                    now,
                )
                for rule in candidate.bonuses
            ]
            if self.bonus_preview.issue:
                for row in result["bonus_budget_preview"]:
                    if row["issue"]:
                        row["issue"] = self.bonus_preview.issue
            result["bonus_history_pending"] = self.preview_history_request(now) is not None
        return web.json_response(result)

    def validate_contract_identity(self, candidate):
        """Reject a contract outside the configured normalized market identity before preview or persistence."""
        options = COUNTRY_BIDDING_AREA_OPTIONS.get(
            (self.setup_catalogue.value or {}).get("country")
        )
        if options and candidate.market_area not in options:
            raise web.HTTPBadRequest(
                text="Choose a bidding area belonging to the Home Assistant country"
            )
        if self.prices() is not None and not self.prices().supports_identity(
            candidate.market_area, candidate.currency
        ):
            raise web.HTTPBadRequest(
                text="Contract does not match the active market-price area/currency"
            )

    async def configure_contract(self, request):
        """Persist a validated rule revision before using it for new planning. Preserve original plans."""
        try:
            body = await request.json()
            candidate = parse_contract(
                body,
                datetime.now(UTC),
                installation_timezone=(self.setup_catalogue.value or {}).get("timezone"),
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid or unsupported contract formulas") from None
        async with self.lock:
            self.validate_contract_identity(candidate)
            current_version = self.contracts.current.version if self.contracts.current else None
            if body.get("expected_version") != current_version:
                raise web.HTTPConflict(text="Contract changed. Reload before saving")
            await self.persist_then_install(self.contracts.save, candidate)
        return await self.contract_settings(request)

    async def counter_settings(self, _request):
        """Read confirmed bonus-counter baselines independently from proposed plan volumes."""
        async with self.lock:
            return web.json_response(
                {
                    **counter_document(self.bonus_counters.current),
                    "issue": self.bonus_counters.issue,
                }
            )

    async def configure_counters(self, request):
        """Commit an explicit confirmed seed revision. Replanning never writes this actual state."""
        try:
            body = await request.json()
            seeds = body.get("seeds") if isinstance(body, dict) else None
            if isinstance(seeds, list) and any(
                isinstance(seed, dict) and "statistics" in seed for seed in seeds
            ):
                raise ValueError("Statistics references require Home Assistant history")
            candidate = parse_counters(body, datetime.now(UTC))
        except (ValueError, TypeError, KeyError, OverflowError):
            raise web.HTTPBadRequest(text="Invalid confirmed bonus counter baseline") from None
        async with self.lock:
            current_version = (
                self.bonus_counters.current.version if self.bonus_counters.current else None
            )
            if body.get("expected_version") != current_version:
                raise web.HTTPConflict(text="Bonus counter changed. Reload before saving")
            await self.persist_then_install(self.bonus_counters.save, candidate)
        return await self.counter_settings(request)

    def bonus_budget_row(self, contract, rule, book, now):
        """Read one rule's current reset period from every required export meter. Missing evidence stays unknown."""
        row = {
            "rule_id": rule.rule_id,
            "effective_from": rule.effective_from.isoformat(),
            "period_start": None,
            "cap_kwh": rule.cap_kwh,
            "measured_at": None,
            "opening": None,
            "export_since_opening_kwh": None,
        }
        try:
            if rule.contract_start is not None and now < rule.contract_start:
                row["period_start"] = rule.contract_start.isoformat()
                raise ValueError("bonus_contract_not_started")
            row["period_start"] = rule.period_start(now).isoformat()
            seed = next(
                (
                    item
                    for item in (book.seeds if book else ())
                    if item.rule_id == rule.rule_id and item.period_start == rule.period_start(now)
                ),
                None,
            )
            if seed is not None:
                row["opening"] = {
                    "used_kwh": seed.used_kwh,
                    "measured_at": seed.observed_at.isoformat(),
                    "meter_kwh": seed.meter_kwh,
                    "source": "unknown",
                }
                if seed.statistics:
                    basis = seed.statistics
                    row["statistics"] = basis.document()
                    row["opening"].update(
                        used_kwh=basis.prefix_kwh,
                        measured_at=basis.reference_at.isoformat(),
                        meter_kwh=sum(item.reference_sum for item in basis.series),
                        history_digest=basis.history_digest,
                        source="manual_confirmation"
                        if basis.confirmed_at
                        else "home_assistant_history",
                    )
            if (
                self.contracts.issue
                or self.bonus_counters.issue
                or self.live is None
                or self.live.configuration is None
            ):
                raise ValueError("bonus_configuration_or_evidence_unavailable")
            from dataclasses import replace

            evidence = BonusCompiler().current_evidence(
                replace(contract, bonuses=(rule,)),
                now,
                book,
                self.live.configuration,
                self.live.report,
            )[0]
            row.update(
                used_kwh=evidence.initial_kwh,
                remaining_kwh=max(0, rule.cap_kwh - evidence.initial_kwh),
                measured_at=evidence.meter_observed_at.isoformat(),
                issue=None,
            )
            row["export_since_opening_kwh"] = evidence.initial_kwh - (
                seed.statistics.prefix_kwh if seed.statistics else seed.used_kwh
            )
        except ValueError as error:
            row.update(used_kwh=None, remaining_kwh=None, issue=str(error))
        return row

    async def bonus_budgets(self, _request):
        """Expose saved annual volume with the same continuity checks used by draft preview and planning."""
        async with self.lock:
            now = datetime.now(UTC)
            rows = []
            for versions in (
                BonusCompiler().families(self.contracts.current).values()
                if self.contracts.current
                else ()
            ):
                chosen = next(
                    (rule for rule in reversed(versions) if rule.effective_from <= now), versions[0]
                )
                row = self.bonus_budget_row(
                    self.contracts.current, chosen, self.bonus_counters.current, now
                )
                active = self.contracts.current.bonus_at(now)
                row["active"] = active is not None and chosen.rule_id == active.rule_id
                row["confirmation"] = None
                seed = next(
                    (
                        item
                        for item in (
                            self.bonus_counters.current.seeds if self.bonus_counters.current else ()
                        )
                        if item.rule_id == chosen.rule_id
                        and item.period_start.isoformat() == row["period_start"]
                    ),
                    None,
                )
                if (
                    row["active"]
                    and row["issue"] == "bonus_statistics_prefix_required"
                    and seed
                    and seed.statistics
                    and seed.statistics.prefix_kwh is None
                    and not seed.statistics.issue
                    and self.live
                    and self.live.configuration
                ):
                    row["confirmation"] = {
                        "reference_version": seed.statistics.reference_version,
                        "config_revision": self.live.configuration.revision,
                    }
                try:
                    _reading, components = export_reading(
                        self.live.configuration, self.live.report, now
                    )
                    row["meters"] = [
                        {
                            "entity_id": item.entity_id,
                            "meter_kwh": item.value,
                            "measured_at": item.observed_at.isoformat(),
                        }
                        for item in components
                    ]
                except (ValueError, AttributeError):
                    row["meters"] = []
                if row["opening"]:
                    seed = next(
                        seed
                        for seed in self.bonus_counters.current.seeds
                        if seed.rule_id == chosen.rule_id
                        and seed.period_start.isoformat() == row["period_start"]
                    )
                    origin = await asyncio.to_thread(self.bonus_counters.provenance, seed)
                    if origin:
                        row["opening"].update(
                            {key: value for key, value in origin.items() if key != "seed"}
                        )
                    row["opening"]["meters"] = [
                        {"entity_id": entity, "meter_kwh": value}
                        for entity, value in (
                            seed.component_meters or ((seed.entity_id, seed.meter_kwh),)
                        )
                    ]
                rows.append(row)
            return web.json_response(
                {
                    "contract_version": self.contracts.current.version
                    if self.contracts.current
                    else None,
                    "counter_version": self.bonus_counters.current.version
                    if self.bonus_counters.current
                    else None,
                    "budgets": rows,
                }
            )

    async def confirm_bonus_budget(self, request):
        """Confirm only the missing prefix bound to an immutable statistics reference."""
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid bonus confirmation") from None
        if not isinstance(body, dict) or set(body) != {
            "rule_id",
            "used_kwh",
            "contract_version",
            "reference_version",
            "config_revision",
        }:
            raise web.HTTPBadRequest(text="Invalid bonus confirmation")
        async with self.lock:
            contract = self.contracts.current
            book = self.bonus_counters.current
            if contract is None or body["contract_version"] != contract.version:
                raise web.HTTPConflict(text="Contract or counters changed. Refresh budgets")
            if (
                self.contracts.issue
                or self.bonus_counters.issue
                or self.live is None
                or self.live.configuration is None
            ):
                raise web.HTTPConflict(
                    text="Recover configuration and measured evidence before confirming"
                )
            now = datetime.now(UTC)
            try:
                versions = BonusCompiler().families(contract)[body["rule_id"]]
                chosen = next(
                    (rule for rule in reversed(versions) if rule.effective_from <= now), versions[0]
                )
                active = contract.bonus_at(now)
                previous = next(
                    item
                    for item in (book.seeds if book else ())
                    if item.rule_id == chosen.rule_id
                    and item.period_start == chosen.period_start(now)
                )
                basis = previous.statistics
                if (
                    active is None
                    or active.rule_id != chosen.rule_id
                    or basis is None
                    or basis.prefix_kwh is not None
                    or basis.issue
                    or self.bonus_budget_row(contract, chosen, book, now)["issue"]
                    != "bonus_statistics_prefix_required"
                ):
                    raise web.HTTPConflict(
                        text="Only an unavailable eligible budget can be confirmed"
                    )
                if (
                    body["reference_version"] != basis.reference_version
                    or body["config_revision"] != self.live.configuration.revision
                ):
                    raise web.HTTPConflict(
                        text="Export statistics reference or configuration changed. Refresh budgets"
                    )
                from dataclasses import replace

                confirmed = replace(basis, prefix_kwh=body["used_kwh"], confirmed_at=now)
                seed = replace(previous, used_kwh=confirmed.used(now), statistics=confirmed)
                candidate = (
                    CounterBook(
                        now,
                        tuple(
                            item
                            for item in book.seeds
                            if (item.rule_id, item.period_start)
                            != (seed.rule_id, seed.period_start)
                        )
                        + (seed,),
                    )
                    if book
                    else CounterBook(now, (seed,))
                )
            except (ValueError, TypeError, KeyError, StopIteration):
                raise web.HTTPBadRequest(
                    text="A valid missing-period volume and fresh export statistics are required"
                ) from None
            await self.persist_then_install(self.bonus_counters.save_statistics, candidate)
        return await self.bonus_budgets(request)

    def preview_history_request(self, now):
        """Borrow the existing integration history route without persisting or activating draft rules."""
        if self.bonus_counters.issue or self.contracts.issue:
            return None
        return self.bonus_preview.request(
            self.live.configuration if self.live else None, self.bonus_counters.current, now
        )

    def saved_history_request(self, now):
        """Select authoritative saved-rule history independently of any preview with an equal version."""
        if self.bonus_counters.issue:
            return None
        return self.bonus_history.request(
            self.contracts.current,
            self.live.configuration if self.live else None,
            self.bonus_counters.current,
            now,
        )

    def history_request(self, now=None):
        """Prioritize saved-rule evidence. Only then service the latest bounded editor draft."""
        now = now or datetime.now(UTC)
        return self.saved_history_request(now) or self.preview_history_request(now)

    def report_history_issue(self, issue, detail=None):
        """Retain public status and log changed history failures without repeating reports or payloads."""

        diagnostic = (issue, detail)
        if diagnostic != self.history_diagnostic:
            if issue:
                _LOGGER.info(
                    "Export bonus history unavailable: %s%s. %s",
                    issue,
                    f" ({detail})" if detail else "",
                    explain_contract_problem("bonus_counter_baseline_required", issue).message,
                )
            elif self.history_issue:
                _LOGGER.info("Export bonus history is usable again")
            self.history_diagnostic = diagnostic
        self.history_issue = issue

    async def receive_history(self, request):
        """Validate annual history against the exact saved or temporary draft request.

        Saved-rule baselines are persisted. Draft baselines stay in memory and
        cannot activate planning or replace confirmed evidence.
        This authenticated route uses the shared statistics report body limit
        for bounded series. Other routes retain their existing limits. Immutable references,
        checkpoints and history digests retain evidence without copying raw rows.
        """
        try:
            body = await request.clone(client_max_size=MAX_STATISTICS_REPORT_BYTES).json()
            if not isinstance(body, dict):
                raise TypeError("Invalid history report")
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid history report") from None
        async with self.lock:
            now = datetime.now(UTC)
            pending = self.history_request(now)
            if pending is None or body.get("request_version") != pending.version:
                raise web.HTTPConflict(text="History request changed. Reload before importing")
            is_preview = self.saved_history_request(now) is None
            if set(body) == {"request_version", "error"} and body["error"] in (
                "recorder_unavailable",
                "history_unavailable",
                "history_missing",
                "unsupported_statistics",
            ):
                if is_preview:
                    self.bonus_preview.issue = body["error"]
                else:
                    self.report_history_issue(body["error"])
                    if body["error"] in ("history_missing", "unsupported_statistics"):
                        invalid = invalidate_statistics(
                            self.bonus_counters.current, pending, body, now
                        )
                        if invalid is not None and invalid is not self.bonus_counters.current:
                            await self.persist_then_install(
                                self.bonus_counters.save_statistics, invalid
                            )
            else:
                try:
                    channels = parse_statistics(body, pending)
                    if is_preview:
                        self.bonus_preview.receive(
                            pending, channels, self.bonus_counters.current, now
                        )
                    else:
                        candidate = derive_statistics(
                            pending, channels, self.bonus_counters.current, now
                        )
                except (ValueError, TypeError, KeyError, OverflowError, OSError) as error:
                    if is_preview:
                        self.bonus_preview.issue = "incomplete_or_invalid_history"
                    else:
                        self.report_history_issue(
                            "incomplete_or_invalid_history",
                            f"{type(error).__name__}: {str(error)[:MAX_HISTORY_DIAGNOSTIC_CHARACTERS]}",
                        )
                        invalid = invalidate_statistics(
                            self.bonus_counters.current, pending, body, now
                        )
                        if invalid is not None and invalid is not self.bonus_counters.current:
                            await self.persist_then_install(
                                self.bonus_counters.save_statistics, invalid
                            )
                    raise web.HTTPBadRequest(
                        text="Export statistics are incomplete or invalid. Check Recorder continuity"
                    ) from None
                if not is_preview:
                    if candidate is not self.bonus_counters.current:
                        await self.persist_then_install(
                            self.bonus_counters.save_statistics, candidate
                        )
                    self.report_history_issue(
                        next(
                            (
                                seed.statistics.issue
                                for seed in candidate.seeds
                                if seed.statistics and seed.statistics.issue
                            ),
                            None,
                        )
                    )
        return await self.snapshot(request)
