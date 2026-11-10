"""Project historical calculation evidence independently of complete plan and forecast archives."""

from copy import deepcopy

from sqlalchemy import func, select

from .schema import ACTIVATION_CONTEXTS, ACTIVATIONS

CONTEXT_FIELDS = (
    "plan_id",
    "generated_at",
    "valid_until",
    "strategy",
    "currency",
    "market_area",
    "comparison_bindings",
    "comparison_names",
    "display_sources",
    "steps",
    "expected_flows",
    "appliance_schedule",
)
SNAPSHOT_FIELDS = ("problem", "contract_version", "tariff_evidence")


def activation_history():
    """Resolve preserved and not-yet-backfilled original activation IDs through the same chronological view."""
    return (
        select(
            ACTIVATIONS.c.activated_at,
            func.coalesce(ACTIVATIONS.c.plan_id, ACTIVATION_CONTEXTS.c.plan_id).label("plan_id"),
            ACTIVATIONS.c.enabled,
            ACTIVATIONS.c.context_hash,
        )
        .select_from(
            ACTIVATIONS.outerjoin(
                ACTIVATION_CONTEXTS,
                ACTIVATION_CONTEXTS.c.activated_at == ACTIVATIONS.c.activated_at,
            )
        )
        .subquery()
    )


def historical_context(plan):
    """Detach only physics, original tariffs and active advice needed by historical settlement.

    The result is explicitly not a complete original plan. Forecast reports,
    future reference replays, measurement snapshots and forecast-model archives
    are omitted. Control ownership has its separate permanent compact projection.
    """
    snapshot = plan.get("input_snapshot", {})
    return deepcopy(
        {
            **{key: plan[key] for key in CONTEXT_FIELDS if key in plan},
            "input_snapshot": {key: snapshot[key] for key in SNAPSHOT_FIELDS if key in snapshot},
        }
    )
