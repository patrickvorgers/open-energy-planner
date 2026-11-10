"""Transactional SQLAlchemy documents for durable configuration and source/model archives."""

import json
from contextlib import contextmanager, nullcontext
from threading import local

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.storage.adapters.lifecycle.schema import DOCUMENTS, IDENTITY_LENGTH
from open_energy_planner.storage.adapters.lifecycle.transactions import lifecycle_transaction
from open_energy_planner.storage.adapters.payloads import decode_payload, encode_payload

DOCUMENT_NAMESPACES = (
    "contracts",
    "bonus-counters",
    "savings-bonus-counters",
    "demand",
    "solar",
    "installation",
    "prices",
    "savings",
    "planning",
)
ARCHIVE_BATCH_ROWS = 128


class SqlDocuments:
    """Borrow the single active engine, keeping versions and pointers in the same transaction.

    Complete transactions share the engine's lifecycle guard with plan publication
    and maintenance. Domain owners still serialize their own changes. A thread-local
    connection supports off-loop saves without sharing connections across threads.
    Bootstrap credentials, certificates and logs never enter this table.
    """

    def __init__(self, engine, namespace):
        """Bind a known durable namespace. Engine ownership stays with the app."""
        if namespace not in DOCUMENT_NAMESPACES:
            raise ValueError("Unknown document namespace")
        self.engine, self.namespace, self.pending = engine, namespace, local()

    def _key(self, name):
        """Validate flat archive identities before querying any database."""
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= IDENTITY_LENGTH
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("Invalid document identity")
        return (DOCUMENTS.c.namespace == self.namespace) & (DOCUMENTS.c.name == name)

    @contextmanager
    def transaction(self):
        """Commit a complete store operation, rolling back all versions/pointers on failure."""
        if getattr(self.pending, "connection", None) is not None:
            yield
            return
        try:
            with lifecycle_transaction(self.engine) as connection:
                self.pending.connection = connection
                try:
                    yield
                finally:
                    self.pending.connection = None
        except SQLAlchemyError:
            raise OSError("Document archive transaction failed") from None

    def exists(self, name):
        """Check the active database only, never a stale legacy file."""
        key = self._key(name)
        try:
            with (
                nullcontext(self.pending.connection)
                if getattr(self.pending, "connection", None) is not None
                else self.engine.connect() as connection
            ):
                return connection.scalar(select(DOCUMENTS.c.name).where(key)) is not None
        except SQLAlchemyError:
            raise OSError("Document archive read failed") from None

    def read(self, name):
        """Read detached JSON, preserving explicit missing/corrupt historical data errors."""
        key = self._key(name)
        try:
            with (
                nullcontext(self.pending.connection)
                if getattr(self.pending, "connection", None) is not None
                else self.engine.connect() as connection
            ):
                payload = connection.scalar(select(DOCUMENTS.c.payload).where(key))
        except SQLAlchemyError:
            raise OSError("Document archive read failed") from None
        if payload is None:
            raise FileNotFoundError("Archived document is missing")
        return json.loads(decode_payload(payload))

    def read_many(self, names):
        """Snapshot known document pointers in one SQL statement. Missing identities remain absent."""
        names = tuple(dict.fromkeys(names))
        for name in names:
            self._key(name)
        if not names:
            return {}
        try:
            with (
                nullcontext(self.pending.connection)
                if getattr(self.pending, "connection", None) is not None
                else self.engine.connect() as connection
            ):
                rows = connection.execute(
                    select(DOCUMENTS.c.name, DOCUMENTS.c.payload).where(
                        DOCUMENTS.c.namespace == self.namespace, DOCUMENTS.c.name.in_(names)
                    )
                ).all()
        except SQLAlchemyError:
            raise OSError("Document archive read failed") from None
        return {name: json.loads(decode_payload(payload)) for name, payload in rows}

    def write(self, name, value):
        """Persist finite JSON without exposing connection parameters in errors."""
        key = self._key(name)
        payload = encode_payload(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=lambda value: value.isoformat(),
            )
        )
        with self.transaction():
            connection = self.pending.connection
            if connection.scalar(select(DOCUMENTS.c.name).where(key)) is not None:
                connection.execute(DOCUMENTS.update().where(key).values(payload=payload))
            else:
                connection.execute(
                    DOCUMENTS.insert().values(namespace=self.namespace, name=name, payload=payload)
                )

    def iter_prefix(self, prefix):
        """Stream namespace-bound JSON, reusing an active transaction and bounding reconstruction memory."""
        self._key(prefix)
        try:
            with (
                nullcontext(self.pending.connection)
                if getattr(self.pending, "connection", None) is not None
                else self.engine.connect() as connection
            ):
                rows = connection.execute(
                    select(DOCUMENTS.c.payload)
                    .where(
                        DOCUMENTS.c.namespace == self.namespace,
                        DOCUMENTS.c.name.startswith(prefix, autoescape=True),
                    )
                    .execution_options(yield_per=ARCHIVE_BATCH_ROWS)
                )
                for (payload,) in rows:
                    yield json.loads(decode_payload(payload))
        except SQLAlchemyError:
            raise OSError("Document archive read failed") from None
