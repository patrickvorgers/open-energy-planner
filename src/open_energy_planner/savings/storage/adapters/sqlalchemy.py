"""Portable indexed settlement reads and atomic evidence/state commits on the app engine."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.core.units import SECONDS_PER_HOUR
from open_energy_planner.savings.model import MAX_BATCH_INTERVALS, PendingSettlement
from open_energy_planner.storage.adapters.lifecycle.evidence import activation_history
from open_energy_planner.storage.adapters.lifecycle.schema import (
    PUBLISHED_EVENTS,
    PUBLISHED_PRICES,
    SAVINGS_DAYS,
    SETTLEMENT_EVALUATIONS,
    SETTLEMENT_FRAMES,
    SETTLEMENT_STATE,
)
from open_energy_planner.storage.adapters.lifecycle.sqlalchemy import canonical, digest
from open_energy_planner.storage.adapters.lifecycle.transactions import lifecycle_transaction
from open_energy_planner.storage.adapters.payloads import decode_payload, encode_payload

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
    """Borrow the active engine and its writer guard. Never hide a storage failure.

    Borrowed connections remain inside their caller's complete transaction.
    Independent read snapshots do not acquire the writer guard.
    """

    def __init__(self, engine):
        """Keep native connections inside the storage boundary."""
        self.engine = engine

    def day(self, *, before=None, finalized=None, latest=False):
        """Read one calendar checkpoint, with its independently verified report and continuation."""
        query = select(SAVINGS_DAYS)
        if before is not None:
            query = query.where(SAVINGS_DAYS.c.start < stamp(before))
        if finalized is not None:
            query = query.where(
                SAVINGS_DAYS.c.finalized_at.is_not(None)
                if finalized
                else SAVINGS_DAYS.c.finalized_at.is_(None)
            )
        query = query.order_by(
            SAVINGS_DAYS.c.start.desc() if latest else SAVINGS_DAYS.c.start
        ).limit(1)
        with self.engine.connect() as connection:
            row = connection.execute(query).mappings().first()
        return self._day_report(row) if row else None

    @staticmethod
    def _day_report(row):
        """Fail closed on corrupt compact evidence instead of reconstructing a different historical day."""
        try:
            report = json.loads(decode_payload(row["payload"]))
            if (
                digest(report) != row["payload_hash"]
                or any(
                    report[key] != row[key]
                    for key in ("day", "timezone", "currency", "market_area", "finalized_at")
                )
                or stamp(report["start"]) != row["start"]
                or stamp(report["end"]) != row["end"]
            ):
                raise ValueError
            return report
        except (ValueError, KeyError, TypeError):
            raise OSError("Daily savings archive failed integrity checks") from None

    def put_day(self, report, documents, next_day):
        """Publish one complete daily report and its retry cursor atomically. Final days are immutable."""
        key = stamp(report["start"])
        with documents.transaction():
            connection = documents.pending.connection
            old = (
                connection.execute(select(SAVINGS_DAYS).where(SAVINGS_DAYS.c.start == key))
                .mappings()
                .first()
            )
            if old and old["finalized_at"] is not None:
                if self._day_report(old) != report:
                    raise ValueError("Final daily savings are immutable")
                return
            values = {
                "end": stamp(report["end"]),
                "day": report["day"],
                "timezone": report["timezone"],
                "currency": report["currency"],
                "market_area": report["market_area"],
                "finalized_at": report["finalized_at"],
                "payload": encode_payload(canonical(report)),
                "payload_hash": digest(report),
                **{
                    f"{scenario}_{key}": report["scenarios"][scenario]["totals"][key]
                    if report["scenarios"][scenario]["totals"] is not None
                    else None
                    for scenario in ("actual", "reference", "without_battery")
                    for key in TOTAL_KEYS
                },
                **{
                    f"{scenario}_coverage_seconds": report["scenarios"][scenario][
                        "coverage_seconds"
                    ]
                    for scenario in ("actual", "reference", "without_battery")
                },
                "savings": report["savings"],
                "avoided_import_kwh": report["avoided_import_kwh"],
                **{
                    key: report[key]
                    for key in (
                        "comparison_seconds",
                        "battery_savings",
                        "battery_comparison_seconds",
                        "self_consumption_savings",
                        "self_consumption_comparison_seconds",
                    )
                },
            }
            if old:
                connection.execute(
                    SAVINGS_DAYS.update().where(SAVINGS_DAYS.c.start == key).values(**values)
                )
            else:
                connection.execute(SAVINGS_DAYS.insert().values(start=key, **values))
            documents.write(
                "daily-progress.json", {"next": next_day, "timezone": report["timezone"]}
            )
            if report["finalized_at"] is not None:
                capture = (
                    documents.read("capture.json") if documents.exists("capture.json") else None
                )
                if capture is None or datetime.fromisoformat(
                    capture["end"]
                ) < datetime.fromisoformat(report["end"]):
                    documents.write("capture.json", {"end": report["end"]})

    def daily_overview(self, start, end):
        """Read mutable provisional details, SQL totals and chart bins from one repeatable database snapshot."""
        try:
            with self.engine.connect() as connection:
                if connection.dialect.name != "sqlite":
                    connection = connection.execution_options(isolation_level="REPEATABLE READ")
                with connection.begin():
                    if connection.dialect.name == "sqlite":
                        # sqlite3's legacy mode otherwise begins no read transaction for SELECT.
                        connection.exec_driver_sql("BEGIN")
                    return (
                        self.daily_summary(start, end, _connection=connection),
                        self.days(start, end, _connection=connection),
                        self.daily_chart(start, end, _connection=connection),
                    )
        except SQLAlchemyError:
            raise OSError("Daily savings archive unavailable") from None

    def days(self, start, end, *, _connection=None):
        """Read bounded daily details without retaining or loading quarter measurement payloads."""
        with (
            nullcontext(_connection) if _connection is not None else self.engine.connect()
        ) as connection:
            rows = (
                connection.execute(
                    select(SAVINGS_DAYS)
                    .where(SAVINGS_DAYS.c.start >= stamp(start), SAVINGS_DAYS.c.end <= stamp(end))
                    .order_by(SAVINGS_DAYS.c.start)
                    .limit(DAILY_CHART_WINDOW_DAYS)
                )
                .mappings()
                .all()
            )
        return [self._day_report(row) for row in rows]

    def daily_summary(self, start, end, *, _connection=None):
        """Sum only reported measured coverage. SQL NULL stays unavailable when no day supplies evidence."""
        table = SAVINGS_DAYS
        columns = [
            table.c[f"{scenario}_{key}"]
            for scenario in ("actual", "reference", "without_battery")
            for key in (*TOTAL_KEYS, "coverage_seconds")
        ] + [
            table.c[key]
            for key in (
                "savings",
                "avoided_import_kwh",
                "comparison_seconds",
                "battery_savings",
                "battery_comparison_seconds",
                "self_consumption_savings",
                "self_consumption_comparison_seconds",
            )
        ]
        with (
            nullcontext(_connection) if _connection is not None else self.engine.connect()
        ) as connection:
            return dict(
                connection.execute(
                    select(
                        func.count().label("days"),
                        func.count(func.distinct(table.c.currency)).label("currencies"),
                        func.min(table.c.currency).label("currency"),
                        *(func.sum(column).label(column.name) for column in columns),
                    ).where(table.c.start >= stamp(start), table.c.end <= stamp(end))
                )
                .mappings()
                .one()
            )

    def daily_chart(self, start, end, *, _connection=None):
        """Aggregate local calendar bins in SQL while retaining UTC instants and genuine missing benefit."""
        table = SAVINGS_DAYS
        month = end - start > timedelta(days=DAILY_CHART_WINDOW_DAYS)
        day = func.substr(table.c.day, 1, 7 if month else 10)
        with (
            nullcontext(_connection) if _connection is not None else self.engine.connect()
        ) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(
                        func.min(table.c.start).label("start"),
                        func.max(table.c.end).label("end"),
                        func.sum(table.c.savings).label("savings"),
                        *(
                            func.sum(table.c[f"{kind}_import_kwh"]).label(f"{kind}_import_kwh")
                            for kind in ("actual", "reference", "without_battery")
                        ),
                    )
                    .where(table.c.start >= stamp(start), table.c.end <= stamp(end))
                    .group_by(day)
                    .order_by(day)
                ).mappings()
            ]

    def put_frames(self, frames, context=None, active_connection=None):
        """Idempotently retain original metering. Conflicting replacements require explicit correction handling."""
        with (
            nullcontext(active_connection)
            if active_connection is not None
            else lifecycle_transaction(self.engine) as connection
        ):
            for frame in frames:
                key, payload = (
                    stamp(frame.start),
                    encode_payload(canonical({"frame": frame.record(), "context": context})),
                )
                old = connection.scalar(
                    select(SETTLEMENT_FRAMES.c.payload).where(SETTLEMENT_FRAMES.c.start == key)
                )
                if old is not None and decode_payload(old) != decode_payload(payload):
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
            return [json.loads(decode_payload(row.payload)) for row in connection.execute(query)]

    def first_activation(self):
        """Start comparisons only after an actual plan publication, never from a historical forecast."""
        history = activation_history()
        with self.engine.connect() as connection:
            return connection.scalar(
                select(history.c.activated_at)
                .where(history.c.plan_id.is_not(None))
                .order_by(history.c.activated_at)
                .limit(1)
            )

    def context_plan(self, instant):
        """Resolve recorded activation chronology independently of solver generation time."""
        history = activation_history()
        with self.engine.connect() as connection:
            return connection.scalar(
                select(history.c.plan_id)
                .where(history.c.activated_at <= stamp(instant), history.c.plan_id.is_not(None))
                .order_by(history.c.activated_at.desc())
                .limit(1)
            )

    def next_configuration(self, start, end, bindings):
        """Split only at actual physical/binding changes, using indexed activation chronology."""
        history = activation_history()
        with self.engine.connect() as connection:
            return connection.scalar(
                select(history.c.activated_at)
                .where(
                    history.c.activated_at > stamp(start),
                    history.c.activated_at < stamp(end),
                    history.c.context_hash != digest(bindings),
                    history.c.plan_id.is_not(None),
                )
                .order_by(history.c.activated_at)
                .limit(1)
            )

    def state(self, revision):
        """Return detached continuous reference/counter state or an explicit uninitialized value."""
        with self.engine.connect() as connection:
            payload = connection.scalar(
                select(SETTLEMENT_STATE.c.payload).where(SETTLEMENT_STATE.c.revision == revision)
            )
        return json.loads(decode_payload(payload)) if payload else None

    def activations(self, start, end):
        """Read the last advice state and subsequent changes for one metering interval."""
        history = activation_history()
        with self.engine.connect() as connection:
            initial = (
                connection.execute(
                    select(history)
                    .where(history.c.activated_at <= stamp(start))
                    .order_by(history.c.activated_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            later = (
                connection.execute(
                    select(history)
                    .where(
                        history.c.activated_at > stamp(start),
                        history.c.activated_at < stamp(end),
                    )
                    .order_by(history.c.activated_at)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in ([initial] if initial else []) + list(later)]

    def commit(self, revision, result, state):
        """Commit an interval and both scenarios' continuation together. Retries cannot double-count."""
        with lifecycle_transaction(self.engine) as connection:
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
            payload = encode_payload(canonical(result))
            if old is not None:
                if decode_payload(old) != decode_payload(payload):
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
                    SETTLEMENT_STATE.insert().values(
                        revision=revision, payload=encode_payload(canonical(state))
                    )
                )
            else:
                connection.execute(
                    SETTLEMENT_STATE.update()
                    .where(where)
                    .values(payload=encode_payload(canonical(state)))
                )

    def results(self, revision, start, end):
        """Read only one bounded requested period, preserving date-selection independence."""
        try:
            with self.engine.connect() as connection:
                return [
                    json.loads(decode_payload(payload))
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
