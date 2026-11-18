"""Retain one compact daily capacity observation in the active transferable schema."""

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .lifecycle.schema import STORAGE_SAMPLES
from .lifecycle.transactions import lifecycle_transaction
from .usage import storage_usage

DAILY_SAMPLE_HOUR = 4


class StorageHistory:
    """Borrow the active engine without owning its lifetime or changing old observations."""

    def __init__(self, engine):
        """Share the app's single active engine and mutation guard."""
        self.engine = engine

    def sample(self, clock: Callable[[], datetime], timezone: str | None):
        """Measure once after 04:00 local, including a late startup on the current day.

        Missing installation timezone defers sampling. Offline days are never
        backfilled. Failed/unknown size probes retain null, never a fictional zero.
        The engine guard orders the existence check, measurement and durable insert
        against all other app writers. Read the observation clock after acquiring
        that guard so waiting across midnight cannot backdate a measurement.
        Only one writable app process is supported.
        """
        if not timezone:
            return
        try:
            with lifecycle_transaction(self.engine) as connection:
                now = clock()
                local = now.astimezone(ZoneInfo(timezone))
                if local.hour < DAILY_SAMPLE_HOUR:
                    return
                day = local.date().isoformat()
                if connection.scalar(
                    select(STORAGE_SAMPLES.c.day).where(STORAGE_SAMPLES.c.day == day)
                ):
                    return
                usage = storage_usage(self.engine)
                connection.execute(
                    STORAGE_SAMPLES.insert().values(
                        day=day,
                        observed_at=now.astimezone(UTC).isoformat(timespec="microseconds"),
                        timezone=timezone,
                        backend=usage.backend,
                        database_bytes=usage.database_bytes,
                        database_estimated=usage.database_estimated,
                    )
                )
        except SQLAlchemyError:
            raise OSError("Daily database measurement could not be saved") from None

    def read(self, start: str | None, end: str):
        """Read ordered local sampling dates inclusively, without measuring on page refresh."""
        statement = select(STORAGE_SAMPLES).where(STORAGE_SAMPLES.c.day <= end)
        if start is not None:
            statement = statement.where(STORAGE_SAMPLES.c.day >= start)
        try:
            with self.engine.connect() as connection:
                return [
                    dict(row)
                    for row in connection.execute(
                        statement.order_by(STORAGE_SAMPLES.c.day)
                    ).mappings()
                ]
        except SQLAlchemyError:
            raise OSError("Database growth history could not be read") from None

    def latest(self, end: str):
        """Read the latest retained observation even when it predates the chart window."""
        statement = (
            select(STORAGE_SAMPLES)
            .where(STORAGE_SAMPLES.c.day <= end)
            .order_by(STORAGE_SAMPLES.c.day.desc())
            .limit(1)
        )
        try:
            with self.engine.connect() as connection:
                row = connection.execute(statement).mappings().first()
                return dict(row) if row is not None else None
        except SQLAlchemyError:
            raise OSError("Database growth history could not be read") from None
