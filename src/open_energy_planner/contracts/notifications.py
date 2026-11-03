"""Explain contract-owned problems without changing financial or planning evidence."""

from dataclasses import replace

from open_energy_planner.notifications.model import NoticeText, Notification
from open_energy_planner.runtime.diagnostics.planning_notifications import (
    PlanningStatus,
    planning_notice,
)


def negative_export_notifications(
    negative_export_meters: tuple[str, ...],
) -> tuple[Notification, ...]:
    """Expose accepted statistics advisories independently of unknown planning status."""
    if not negative_export_meters:
        return ()
    return (
        Notification(
            "contracts:negative-export-statistics",
            "bonus_statistics_negative_difference",
            NoticeText(
                "Export statistics are below the period reference",
                "Home Assistant reports an export statistics sum below the fixed "
                "reference for this bonus period. OEP counts the measured difference as 0 kWh "
                "and keeps any confirmed earlier export. The reference stays unchanged. "
                "Further export can make the difference positive again. This may follow an "
                "intentional statistics correction or incorrect meter data. Check the meter "
                "history to decide whether it needs correction. Planning can continue with "
                "this nonnegative volume.\n\nAffected export meters: "
                + ", ".join(negative_export_meters),
                "Review export budget",
                "/contract",
            ),
            detail=", ".join(negative_export_meters),
        ),
    )


def contract_notifications(
    state: PlanningStatus,
    history_issue: str | None = None,
    *,
    export_meters: tuple[str, ...] = (),
) -> tuple[Notification, ...] | None:
    """Keep contract blockers until publication confirms restored planning.

    A usable contract input alone does not confirm restored planning. Its blocker
    remains in recovery until the coordinator confirms publication. Read only
    accepted history evidence. Never refresh meters, alter counters or invent a seed.
    """
    if state.unknown:
        return None
    notices = []
    text = explain_contract_problem(state.cause, history_issue) if state.cause else None
    if text is not None and export_meters and state.cause.startswith("bonus_"):
        text = replace(
            text,
            message=text.message + "\n\nConfigured grid export meters: " + ", ".join(export_meters),
        )
    if state.status != "ready" and text is not None:
        notice = planning_notice(state, text)
        detail = (
            history_issue
            if state.status != "calculating" and state.cause.startswith("bonus_")
            else None
        )
        notices.append(replace(notice, detail=detail))
    return tuple(notices)


def explain_contract_problem(reason: str, history_issue: str | None = None) -> NoticeText | None:
    """Recognize only contract and export-history causes and retain their existing recovery advice."""
    if reason in {"bonus_export_measurement_required", "bonus_export_measurement_expired"}:
        return NoticeText(
            "Current export measurements are unavailable",
            "The export bonus needs current readings from the selected export meters. "
            "Check sensor availability and the Home Assistant integration. "
            "A saved annual reference cannot replace current measurements.",
            "Check installation",
            "/setup",
        )
    if reason == "bonus_window_unavailable":
        return NoticeText(
            "The bonus time window is unavailable",
            "The bonus rule needs a usable time window. Check the Home Assistant location "
            "and timezone for daylight rules, or the configured start and end times.",
            "Review contract",
            "/contract",
        )
    if reason in {"bonus_rules_invalid", "bonus_rate_unavailable", "unsupported_contract_bonus"}:
        return NoticeText(
            "The export bonus rule is unusable",
            "Check the bonus formula, price condition and effective dates in Contract settings.",
            "Review contract",
            "/contract",
        )
    if reason.startswith("bonus_"):
        if "bonus_statistics_prefix_required" in (reason, history_issue):
            return NoticeText(
                "Confirm earlier export",
                "A valid export statistics reference is available, but earlier export in this "
                "bonus period is still unknown. Review the reference date in Contract settings "
                "and confirm only the export before that date.",
                "Review contract",
                "/contract",
            )
        message = (
            "The annual export bonus cannot be calculated reliably. Review its export "
            "statistics in Contract settings. Confirm earlier export only when a valid "
            "statistics reference identifies the missing period."
        )
        if history_issue == "incomplete_or_invalid_history":
            message = (
                "Home Assistant export statistics are incomplete or invalid. Check Recorder "
                "continuity and the selected export meters before confirming any missing "
                "earlier export in Contract settings."
            )
        return NoticeText("Export bonus needs attention", message, "Review contract", "/contract")
    if reason == "contract_not_configured":
        return NoticeText(
            "A contract is required",
            "Save the applicable import and export price rules before planning.",
            "Configure contract",
            "/contract",
            "info",
        )
    if reason in {
        "contract_cache_unreadable",
        "tariff_coverage_gap",
        "contract_or_market_prices_invalid",
        "contract_revision_from_future",
        "too_many_billing_segments",
    }:
        return NoticeText(
            "Prices or contract need attention",
            "Current prices or contract rules do not provide a usable planning period. "
            "Check the price sources and contract rules. OEP retries when valid data arrives.",
            "Review contract",
            "/contract",
        )
    return None
