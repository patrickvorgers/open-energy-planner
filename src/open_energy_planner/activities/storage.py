"""Retain seven days of detached activity evidence in the app-owned database."""

import json
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from ..storage.adapters.lifecycle.schema import ACTIVITIES


class ActivityStore:
    """One serialized writer borrows the active engine. Transfers include the shared table."""

    def __init__(self, engine):
        """Keep connection ownership with the app."""
        self.engine = engine

    def write(self, row, now):
        """Atomically upsert an execution and expire old history."""
        values = {key: row[key] for key in ("id", "started_at", "owner", "kind", "state")}
        values["payload"] = json.dumps(row, allow_nan=False)
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    delete(ACTIVITIES).where(
                        ACTIVITIES.c.started_at < (now - timedelta(days=7)).isoformat()
                    )
                )
                existing = connection.scalar(
                    select(ACTIVITIES.c.id).where(ACTIVITIES.c.id == row["id"])
                )
                if existing:
                    connection.execute(
                        ACTIVITIES.update().where(ACTIVITIES.c.id == row["id"]).values(**values)
                    )
                else:
                    connection.execute(ACTIVITIES.insert().values(**values))
        except SQLAlchemyError:
            raise OSError("Activity history could not be saved") from None

    def read(self, start, end, before=None, limit=100, kind=None):
        """Page by UTC start and identity, including simultaneous starts exactly once."""
        query = select(ACTIVITIES.c.payload).where(
            ACTIVITIES.c.started_at >= start.isoformat(), ACTIVITIES.c.started_at < end.isoformat()
        )
        if kind:
            query = query.where(ACTIVITIES.c.kind == kind)
        if before:
            stamp, identity = before
            query = query.where(
                (ACTIVITIES.c.started_at < stamp)
                | ((ACTIVITIES.c.started_at == stamp) & (ACTIVITIES.c.id < identity))
            )
        query = query.order_by(ACTIVITIES.c.started_at.desc(), ACTIVITIES.c.id.desc()).limit(limit)
        try:
            with self.engine.connect() as connection:
                return [json.loads(row) for row in connection.scalars(query)]
        except SQLAlchemyError:
            raise OSError("Activity history could not be read") from None

    def prune(self, now):
        """Expire idle installations too, without requiring a new execution or a UI read."""
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    delete(ACTIVITIES).where(
                        ACTIVITIES.c.started_at < (now - timedelta(days=7)).isoformat()
                    )
                )
        except SQLAlchemyError:
            raise OSError("Activity retention could not be updated") from None

    def restart(self, now):
        """Sanitize backend errors while retaining explicit corrupt-history failures."""
        try:
            return self._restart(now)
        except SQLAlchemyError:
            raise OSError("Activity history could not be recovered") from None

    def _restart(self, now):
        """A lost process cannot claim completion or failure for its unfinished work."""
        with self.engine.begin() as connection:
            connection.execute(
                delete(ACTIVITIES).where(
                    ACTIVITIES.c.started_at < (now - timedelta(days=7)).isoformat()
                )
            )
            rows = list(
                connection.scalars(
                    select(ACTIVITIES.c.payload).where(ACTIVITIES.c.state == "running")
                )
            )
            for payload in rows:
                row = json.loads(payload)
                row.update(
                    state="interrupted",
                    finished_at=now.isoformat(),
                    detail="Restarted before a definitive outcome was recorded.",
                )
                connection.execute(
                    ACTIVITIES.update()
                    .where(ACTIVITIES.c.id == row["id"])
                    .values(state="interrupted", payload=json.dumps(row))
                )
            latest, sequence = {}, 0
            for payload in connection.scalars(
                select(ACTIVITIES.c.payload)
                .where(ACTIVITIES.c.state.in_(("completed", "failed")))
                .execution_options(yield_per=128)
            ):
                row = json.loads(payload)
                sequence = max(sequence, row.get("finished_sequence", 0))
                key = row["owner"], row["kind"]
                order = (
                    row.get("finished_at") or row["started_at"],
                    row.get("finished_sequence", 0),
                    row["id"],
                    row["state"],
                )
                if key not in latest or order > latest[key]:
                    latest[key] = order
            return {key for key, order in latest.items() if order[-1] == "failed"}, sequence
