"""Retain indexed published wholesale facts for retrospective settlement, separate from forecasts."""

from sqlalchemy import func, select

from open_energy_planner.prices.model import PriceKind
from open_energy_planner.storage.adapters.lifecycle.schema import PUBLISHED_EVENTS, PUBLISHED_PRICES


def retain(connection, series):
    """Upsert newer published corrections in the same transaction as the immutable source archive.

    Original source versions remain in the price archive. Historical settlement
    reads this current published projection and retains its exact source version.
    """
    sequence = connection.scalar(select(func.max(PUBLISHED_EVENTS.c.sequence))) or 0
    for source in series:
        for point in source.points:
            if point.kind != PriceKind.PUBLISHED:
                continue
            key = (PUBLISHED_PRICES.c.area == source.market_area) & (
                PUBLISHED_PRICES.c.start == point.start.isoformat(timespec="microseconds")
            )
            key &= PUBLISHED_PRICES.c.currency == source.currency
            old = connection.execute(select(PUBLISHED_PRICES).where(key)).first()
            received = source.received_at.isoformat(timespec="microseconds")
            if old and old.received_at > received:
                continue
            values = {
                "area": source.market_area,
                "currency": source.currency,
                "start": point.start.isoformat(timespec="microseconds"),
                "end": point.end.isoformat(timespec="microseconds"),
                "received_at": received,
                "version": source.version,
                "price": point.value,
            }
            if old is None:
                connection.execute(PUBLISHED_PRICES.insert().values(**values))
            elif (
                old.version != source.version
                or old.price != point.value
                or old.end != values["end"]
            ):
                connection.execute(PUBLISHED_PRICES.update().where(key).values(**values))
            if old is None or old.price != point.value or old.end != values["end"]:
                sequence += 1
                connection.execute(
                    PUBLISHED_EVENTS.insert().values(
                        sequence=sequence,
                        area=source.market_area,
                        currency=source.currency,
                        start=values["start"],
                        version=source.version,
                    )
                )
