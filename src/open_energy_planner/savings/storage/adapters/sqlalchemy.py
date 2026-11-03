"""Portable indexed settlement reads and atomic evidence/state commits on the app engine."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.savings.model import MAX_BATCH_INTERVALS, PendingSettlement
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTIVATIONS,
    PUBLISHED_EVENTS,
    PUBLISHED_PRICES,
    SETTLEMENT_EVALUATIONS,
    SETTLEMENT_FRAMES,
    SETTLEMENT_STATE,
)
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import canonical, digest

MAX_DETAIL_INTERVALS = 96
DAILY_CHART_WINDOW_DAYS = 366
TOTAL_KEYS = (
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


def stamp(instant):
    """Use fixed-width UTC keys so chronology is portable across all four backends."""
    from datetime import datetime

    from open_energy_planner.prices.model import aware

    if isinstance(instant, str):
        instant = datetime.fromisoformat(instant)
    return aware(instant).isoformat(timespec="microseconds")


class SettlementStore:
    """Borrow the active engine. Never open another database or hide a storage failure."""

    def __init__(self, engine):
        """Keep native connections inside the storage boundary."""
        self.engine = engine

    def put_frames(self, frames, context=None, active_connection=None):
        """Idempotently retain original metering. Conflicting replacements require explicit correction handling."""
        with (
            nullcontext(active_connection)
            if active_connection is not None
            else self.engine.begin() as connection
        ):
            for frame in frames:
                key, payload = (
                    stamp(frame.start),
                    canonical({"frame": frame.record(), "context": context}),
                )
                old = connection.scalar(
                    select(SETTLEMENT_FRAMES.c.payload).where(SETTLEMENT_FRAMES.c.start == key)
                )
                if old is not None and old != payload:
                    raise ValueError("Settlement evidence is immutable")
                if old is None:
                    connection.execute(
                        SETTLEMENT_FRAMES.insert().values(
                            start=key, end=stamp(frame.end), payload=payload
                        )
                    )

    def frames(self, after=None, *, end=None, limit=MAX_BATCH_INTERVALS):
        """Read bounded chronological batches using the interval primary key."""
        query = select(SETTLEMENT_FRAMES).order_by(SETTLEMENT_FRAMES.c.start).limit(limit)
        if after is not None:
            query = query.where(SETTLEMENT_FRAMES.c.start >= stamp(after))
        if end is not None:
            query = query.where(SETTLEMENT_FRAMES.c.start < stamp(end))
        with self.engine.connect() as connection:
            return [json.loads(row.payload) for row in connection.execute(query)]

    def first_activation(self):
        """Start comparisons only after an actual plan publication, never from a historical forecast."""
        with self.engine.connect() as connection:
            return connection.scalar(
                select(ACTIVATIONS.c.activated_at)
                .where(ACTIVATIONS.c.plan_id.is_not(None))
                .order_by(ACTIVATIONS.c.activated_at)
                .limit(1)
            )

    def context_plan(self, instant):
        """Resolve recorded activation chronology independently of solver generation time."""
        with self.engine.connect() as connection:
            return connection.scalar(
                select(ACTIVATIONS.c.plan_id)
                .where(
                    ACTIVATIONS.c.activated_at <= stamp(instant), ACTIVATIONS.c.plan_id.is_not(None)
                )
                .order_by(ACTIVATIONS.c.activated_at.desc())
                .limit(1)
            )

    def next_configuration(self, start, end, bindings):
        """Split only at actual physical/binding changes, using indexed activation chronology."""
        with self.engine.connect() as connection:
            return connection.scalar(
                select(ACTIVATIONS.c.activated_at)
                .where(
                    ACTIVATIONS.c.activated_at > stamp(start),
                    ACTIVATIONS.c.activated_at < stamp(end),
                    ACTIVATIONS.c.context_hash != digest(bindings),
                    ACTIVATIONS.c.plan_id.is_not(None),
                )
                .order_by(ACTIVATIONS.c.activated_at)
                .limit(1)
            )

    def state(self, revision):
        """Return detached continuous reference/counter state or an explicit uninitialized value."""
        with self.engine.connect() as connection:
            payload = connection.scalar(
                select(SETTLEMENT_STATE.c.payload).where(SETTLEMENT_STATE.c.revision == revision)
            )
        return json.loads(payload) if payload else None

    def activations(self, start, end):
        """Read the last advice state and subsequent changes for one metering interval."""
        with self.engine.connect() as connection:
            initial = (
                connection.execute(
                    select(ACTIVATIONS)
                    .where(ACTIVATIONS.c.activated_at <= stamp(start))
                    .order_by(ACTIVATIONS.c.activated_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            later = (
                connection.execute(
                    select(ACTIVATIONS)
                    .where(
                        ACTIVATIONS.c.activated_at > stamp(start),
                        ACTIVATIONS.c.activated_at < stamp(end),
                    )
                    .order_by(ACTIVATIONS.c.activated_at)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in ([initial] if initial else []) + list(later)]

    def commit(self, revision, result, state):
        """Commit an interval and both scenarios' continuation together. Retries cannot double-count."""
        with self.engine.begin() as connection:
            current = self.published(
                datetime.fromisoformat(result["start"]),
                datetime.fromisoformat(result["end"]),
                result["market_area"],
                result["currency"],
            )
            for used in result["actual"]["tariffs"]:
                available = [
                    published_interval
                    for published_interval in current
                    if datetime.fromisoformat(published_interval["start"])
                    <= datetime.fromisoformat(used["start"])
                    and datetime.fromisoformat(published_interval["end"])
                    >= datetime.fromisoformat(used["end"])
                ]
                latest = max(
                    available,
                    key=lambda published_price: published_price["received_at"],
                    default=None,
                )
                if latest is None or latest["price"] != used["market_price"]:
                    raise PendingSettlement("Published prices changed during settlement. Retrying")
            key = (SETTLEMENT_EVALUATIONS.c.revision == revision) & (
                SETTLEMENT_EVALUATIONS.c.start == stamp(result["start"])
            )
            old = connection.scalar(select(SETTLEMENT_EVALUATIONS.c.payload).where(key))
            payload = canonical(result)
            if old is not None:
                if old != payload:
                    raise ValueError("Evaluation revision is immutable")
                return
            connection.execute(
                SETTLEMENT_EVALUATIONS.insert().values(
                    revision=revision,
                    start=stamp(result["start"]),
                    payload=payload,
                    **{
                        f"{scenario}_{key}": result[scenario][key]
                        for scenario in ("actual", "reference")
                        for key in TOTAL_KEYS
                    },
                    savings=result["savings"],
                    avoided_import_kwh=result["avoided_import_kwh"],
                    estimated=int(
                        result["quality"] != "recorded"
                        or result["available_pv_estimated"]
                        or result["subinterval_estimated"]
                    ),
                    duration_h=(
                        datetime.fromisoformat(result["end"])
                        - datetime.fromisoformat(result["start"])
                    ).total_seconds()
                    / SECONDS_PER_HOUR,
                )
            )
            where = SETTLEMENT_STATE.c.revision == revision
            if connection.scalar(select(SETTLEMENT_STATE.c.revision).where(where)) is None:
                connection.execute(
                    SETTLEMENT_STATE.insert().values(revision=revision, payload=canonical(state))
                )
            else:
                connection.execute(
                    SETTLEMENT_STATE.update().where(where).values(payload=canonical(state))
                )

    def results(self, revision, start, end):
        """Read only one bounded requested period, preserving date-selection independence."""
        try:
            with self.engine.connect() as connection:
                return [
                    json.loads(payload)
                    for payload in connection.scalars(
                        select(SETTLEMENT_EVALUATIONS.c.payload)
                        .where(
                            SETTLEMENT_EVALUATIONS.c.revision == revision,
                            SETTLEMENT_EVALUATIONS.c.start >= stamp(start),
                            SETTLEMENT_EVALUATIONS.c.start < stamp(end),
                        )
                        .order_by(SETTLEMENT_EVALUATIONS.c.start)
                        .limit(MAX_DETAIL_INTERVALS)
                    )
                ]
        except SQLAlchemyError:
            raise OSError("Settlement archive unavailable") from None

    def published(self, start, end, area, currency="EUR"):
        """Read settled wholesale prices, never forecast prices, over a bounded historical interval."""
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(PUBLISHED_PRICES)
                    .where(
                        PUBLISHED_PRICES.c.area == area,
                        PUBLISHED_PRICES.c.currency == currency,
                        PUBLISHED_PRICES.c.start < stamp(end),
                        PUBLISHED_PRICES.c.end > stamp(start),
                    )
                    .order_by(PUBLISHED_PRICES.c.start)
                ).mappings()
            ]

    def price_cursor(self, area, currency):
        """Read committed ingestion order, independent of provider receipt time or HTTP duration."""
        with self.engine.connect() as connection:
            return (
                connection.scalar(
                    select(func.max(PUBLISHED_EVENTS.c.sequence)).where(
                        PUBLISHED_EVENTS.c.area == area, PUBLISHED_EVENTS.c.currency == currency
                    )
                )
                or 0
            )

    def corrected_prices(self, before, since, area, currency="EUR", through=None):
        """Find committed semantic changes affecting already evaluated intervals in one bounded event range."""
        query = select(PUBLISHED_EVENTS.c.start, PUBLISHED_EVENTS.c.version).where(
            PUBLISHED_EVENTS.c.area == area,
            PUBLISHED_EVENTS.c.currency == currency,
            PUBLISHED_EVENTS.c.sequence > since,
            PUBLISHED_EVENTS.c.start < stamp(before),
        )
        if through is not None:
            query = query.where(PUBLISHED_EVENTS.c.sequence <= through)
        with self.engine.connect() as connection:
            return list(connection.execute(query))

    def summary(self, revision, start, end):
        """Aggregate full periods in SQL without loading years of interval JSON into RAM or a browser."""
        table = SETTLEMENT_EVALUATIONS
        keys = [
            f"{scenario}_{key}" for scenario in ("actual", "reference") for key in TOTAL_KEYS
        ] + ["savings", "avoided_import_kwh", "estimated", "duration_h"]
        where = (
            (table.c.revision == revision)
            & (table.c.start >= stamp(start))
            & (table.c.start < stamp(end))
        )
        monthly = end - start > timedelta(days=DAILY_CHART_WINDOW_DAYS)
        day = func.substr(table.c.start, 1, 7 if monthly else 10)
        with self.engine.connect() as connection:
            totals = (
                connection.execute(
                    select(
                        func.count().label("count"),
                        *(func.coalesce(func.sum(table.c[key]), 0).label(key) for key in keys),
                    ).where(where)
                )
                .mappings()
                .one()
            )
            daily = (
                connection.execute(
                    select(day.label("day"), func.sum(table.c.savings).label("savings"))
                    .where(where)
                    .group_by(day)
                    .order_by(day)
                )
                .mappings()
                .all()
            )
        normalized = {key: float(value) for key, value in totals.items()}
        normalized["count"] = int(totals["count"])
        normalized["estimated"] = int(totals["estimated"])
        bins = []
        for row in daily:
            start = row["day"] + ("-01" if monthly else "")
            first = datetime.fromisoformat(start)
            following = (
                first.replace(year=first.year + (first.month == 12), month=first.month % 12 + 1)
                if monthly
                else first + timedelta(days=1)
            )
            bins.append(
                {
                    "start": start,
                    "end": following.date().isoformat(),
                    "savings": float(row["savings"]),
                }
            )
        return normalized, bins
