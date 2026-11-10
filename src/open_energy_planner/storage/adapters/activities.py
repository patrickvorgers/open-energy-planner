"""Retain seven days of detached activity evidence in the app-owned database."""

import json
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from .lifecycle.schema import ACTIVITIES
from .lifecycle.transactions import lifecycle_transaction
from .payloads import decode_payload, encode_payload


class ActivityStore:
    """Borrow the active engine and share its complete writer guard. Transfers include the shared table."""

    def __init__(self, engine):
        """Keep connection ownership with the app."""
        self.engine = engine

    def write(self, row, now):
        """Atomically upsert an execution and expire old history."""
        values = {key: row[key] for key in ("id", "started_at", "owner", "kind", "state")}
        values["payload"] = encode_payload(json.dumps(row, allow_nan=False))
        try:
            with lifecycle_transaction(self.engine) as connection:
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
        """Return newest-first detached rows within the half-open UTC date range.

        The optional before cursor is (started_at, id), both ISO/string values.
        Ordering is descending by start and identity, including equal starts once.
        OSError hides backend failures. Invalid stored JSON raises ValueError.
        """
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
                return [json.loads(decode_payload(row)) for row in connection.scalars(query)]
        except SQLAlchemyError:
            raise OSError("Activity history could not be read") from None

    def prune(self, now):
        """Expire idle installations too, without requiring a new execution or a UI read."""
        try:
            with lifecycle_transaction(self.engine) as connection:
                connection.execute(
                    delete(ACTIVITIES).where(
                        ACTIVITIES.c.started_at < (now - timedelta(days=7)).isoformat()
                    )
                )
        except SQLAlchemyError:
            raise OSError("Activity retention could not be updated") from None

    def restart(self, now):
        """Prune expired rows and mark running executions interrupted at aware now.

        Returns:
            A tuple containing the (owner, kind) pairs whose latest completed or
            failed outcome is failed, and the highest retained finished sequence
            among those terminal outcomes (zero when absent).

        Raises:
            OSError: Backend recovery failed, without exposing backend details.
            ValueError: Retained JSON or sequence values are corrupt.
            KeyError: A retained payload lacks a required field.
            TypeError: Retained sequence values cannot be compared.
        """
        try:
            return self._restart(now)
        except SQLAlchemyError:
            raise OSError("Activity history could not be recovered") from None

    def _restart(self, now):
        """A lost process cannot claim completion or failure for its unfinished work."""
        with lifecycle_transaction(self.engine) as connection:
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
                row = json.loads(decode_payload(payload))
                row.update(
                    state="interrupted",
                    finished_at=now.isoformat(),
                    detail="Restarted before a definitive outcome was recorded.",
                )
                connection.execute(
                    ACTIVITIES.update()
                    .where(ACTIVITIES.c.id == row["id"])
                    .values(state="interrupted", payload=encode_payload(json.dumps(row)))
                )
            latest, sequence = {}, 0
            for payload in connection.scalars(
                select(ACTIVITIES.c.payload)
                .where(ACTIVITIES.c.state.in_(("completed", "failed")))
                .execution_options(yield_per=128)
            ):
                row = json.loads(decode_payload(payload))
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
