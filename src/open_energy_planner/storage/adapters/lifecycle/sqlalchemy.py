"""Transactional plan archive with immutable shared input blocks on four SQL backends."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    create_engine,
    event,
    inspect,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from open_energy_planner.home_assistant.observation_wire import configuration_document
from open_energy_planner.storage.adapters.lifecycle.evidence import historical_context
from open_energy_planner.storage.adapters.lifecycle.files import atomic_json
from open_energy_planner.storage.adapters.lifecycle.schema import (
    ACTIVATION_CONTEXTS,
    ACTIVATIONS,
    ARCHIVE_INCOMING_INDEXES,
    FIELD_LENGTH,
    IDENTITY_LENGTH,
    META,
    PARTS,
    PLAN_CONTEXTS,
    PLANS,
    RECORDS,
    SCHEMA,
    STATE,
    VALUES,
)
from open_energy_planner.storage.adapters.payloads import decode_payload, encode_payload

from .transactions import lifecycle_transaction

SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_SECONDS = 5
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
CONNECT_TIMEOUT_SECONDS = 5
POOL_TIMEOUT_SECONDS = 5
DATABASE_IO_TIMEOUT_SECONDS = 30
POSTGRES_POLL_INTERVAL_SECONDS = 0.1
DATABASE_STATEMENT_TIMEOUT_MS = DATABASE_IO_TIMEOUT_SECONDS * 1000
DATABASE_LOCK_TIMEOUT_MS = CONNECT_TIMEOUT_SECONDS * 1000
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100
STATE_KEY = 1
MAX_RECORD_DEPTH = 3
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_DATABASE_MODE = 0o600
SUPPORTED_EXTERNAL_DRIVERS = frozenset({"postgresql+psycopg", "mariadb+pymysql", "mysql+pymysql"})


def canonical(value):
    """Serialize finite JSON losslessly and deterministically. Unsupported objects never enter storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    """Identify exact JSON content without rounding numeric values or normalizing original timestamps."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def archive_engine(directory, url=None):
    """Create one bounded deployment-owned engine. Never substitute local storage after server failure."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(PRIVATE_DIRECTORY_MODE)
    if url:
        from sqlalchemy.engine import make_url

        try:
            parsed = make_url(url)
            if parsed.drivername not in SUPPORTED_EXTERNAL_DRIVERS:
                raise ValueError
            connect_args = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
            if parsed.drivername == "postgresql+psycopg":
                connect_args["options"] = (
                    f"-c statement_timeout={DATABASE_STATEMENT_TIMEOUT_MS} "
                    f"-c lock_timeout={DATABASE_LOCK_TIMEOUT_MS}"
                )
            else:
                connect_args.update(
                    read_timeout=DATABASE_IO_TIMEOUT_SECONDS,
                    write_timeout=DATABASE_IO_TIMEOUT_SECONDS,
                )
            engine = create_engine(
                parsed,
                pool_pre_ping=True,
                pool_timeout=POOL_TIMEOUT_SECONDS,
                connect_args=connect_args,
                hide_parameters=True,
            )
            if parsed.drivername == "postgresql+psycopg":
                driver = engine.dialect.loaded_dbapi

                class BoundedConnection(driver.Connection):
                    """Bound client I/O as well as server execution without abandoning SQL worker threads."""

                    def wait(
                        self, generator, interval=POSTGRES_POLL_INTERVAL_SECONDS, timeout=None
                    ):
                        """Discard an interrupted connection so rollback or pool reuse cannot wait on it again."""
                        limit = (
                            min(timeout, DATABASE_IO_TIMEOUT_SECONDS)
                            if timeout is not None
                            else DATABASE_IO_TIMEOUT_SECONDS
                        )
                        try:
                            return super().wait(generator, interval=interval, timeout=limit)
                        except driver.OperationalError:
                            self.close()
                            raise

                @event.listens_for(engine, "do_connect")
                def connect_postgres(_dialect, _record, arguments, keywords):
                    """Preserve SQLAlchemy's URL/options conversion while using the bounded native driver."""
                    return BoundedConnection.connect(*arguments, **keywords)

            return engine
        except (ValueError, TypeError, SQLAlchemyError):
            raise ValueError("Unsupported archive database configuration") from None
    path = root / "archive.sqlite3"
    path.touch(mode=PRIVATE_DATABASE_MODE, exist_ok=True)
    path.chmod(PRIVATE_DATABASE_MODE)
    engine = create_engine(
        URL.create("sqlite", database=str(path)),
        connect_args={"timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        """Use durable local transactions, enforce references and bound lock waits on every connection."""
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()

    return engine


class SqlPlanRepository:
    """Own app state and immutable plans, committing references before publication.

    An engine-owned lifecycle guard serializes complete mutations, including lazy
    context writes and background collection. Workers receive only plain plans.
    Callers own supplied engines. This adapter owns locally created
    engines. Unreleased development files are deliberately not imported. Only the current
    database format and explicit complete cross-backend transfers are supported.
    """

    def __init__(self, directory, *, engine=None, database_url=None, migrate_database=False):
        """Open an archive or fail closed without creating a replacement history elsewhere."""
        self.engine = engine if engine is not None else archive_engine(directory, database_url)
        self.owns_engine = engine is None
        self.state = None
        self._committed = None
        self.pending_invalidation = False
        try:
            binding_path = Path(directory) / "archive-backend.json"
            binding = {
                "driver": self.engine.url.drivername,
                "host": self.engine.url.host,
                "port": self.engine.url.port,
                "database": self.engine.url.database,
            }
            if binding["driver"] == "sqlite" and binding["database"]:
                binding["database"] = Path(binding["database"]).name
            recorded = (
                json.loads(binding_path.read_text(encoding="utf-8"))
                if self.owns_engine and binding_path.exists()
                else None
            )
            previous_binding = recorded["location"] if recorded else None
            if previous_binding and previous_binding.get("driver") == "sqlite":
                previous_binding = {
                    **previous_binding,
                    "database": Path(previous_binding["database"]).name,
                }
            if (
                self.owns_engine
                and previous_binding == binding
                and not inspect(self.engine).has_table(STATE.name)
            ):
                raise OSError("Active archive is missing. Restore it before starting")
            if previous_binding is not None and previous_binding != binding:
                if migrate_database is not True:
                    raise ValueError(
                        "Archive backend changed. Explicit migration with target overwrite is required"
                    )
                from open_energy_planner.storage.adapters.lifecycle.migration import migrate_archive

                source_engine = archive_engine(directory, recorded["url"])
                try:
                    migrate_archive(source_engine, self.engine, overwrite_target=True)
                finally:
                    source_engine.dispose()
            SCHEMA.create_all(self.engine)
            # create_all does not add indexes to already existing tables.
            for index in ARCHIVE_INCOMING_INDEXES:
                index.create(self.engine, checkfirst=True)
            with lifecycle_transaction(self.engine) as connection:
                version = connection.scalar(select(META.c.version).where(META.c.id == STATE_KEY))
                if version is None:
                    if recorded:
                        raise OSError(
                            "Active archive metadata is missing. Restore it before starting"
                        )
                    connection.execute(META.insert().values(id=STATE_KEY, version=SCHEMA_VERSION))
                elif version != SCHEMA_VERSION:
                    raise ValueError("Unsupported archive schema version")
                row = (
                    connection.execute(select(STATE).where(STATE.c.id == STATE_KEY))
                    .mappings()
                    .first()
                )
                if recorded and row is None:
                    raise OSError("Active archive state is missing. Restore it before starting")
                if row:
                    self.state = json.loads(decode_payload(row["payload"]))
                    self.state["plan"] = (
                        self._read_plan(connection, row["plan_id"]) if row["plan_id"] else None
                    )
            if self.state is None:
                self.state = {
                    "instance_id": str(uuid4()),
                    "enabled": True,
                    "last_control_id": None,
                    "acknowledged_plan": None,
                    "plan": None,
                }
                self.save()
            self._committed = deepcopy(self.state)
            self.pending_invalidation = False
            if self.owns_engine:
                active = {"location": binding, "url": database_url}
                if recorded != active:
                    atomic_json(binding_path, active, mode=PRIVATE_DATABASE_MODE)
        except Exception as error:
            self.close()
            if isinstance(error, SQLAlchemyError):
                raise OSError("Archive database unavailable or schema invalid") from None
            raise

    def close(self):
        """Dispose only an owned engine, after all background writers have drained."""
        if self.owns_engine:
            self.engine.dispose()

    def _pack(self, value, values, records, parts, *, nested_field):
        """Share plan fields, snapshot fields and physical-problem fields without a generic JSON tree per scalar."""
        ref = digest(value)
        if ref in records:
            return ref
        records.add(ref)
        for field, child in value.items():
            if not isinstance(field, str) or not 1 <= len(field) <= FIELD_LENGTH:
                raise ValueError("Invalid archive field")
            if field == nested_field and isinstance(child, dict):
                child_ref = self._pack(
                    child,
                    values,
                    records,
                    parts,
                    nested_field="problem" if field == "input_snapshot" else None,
                )
                parts.append(
                    {"owner": ref, "field": field, "record_ref": child_ref, "value_ref": None}
                )
            else:
                child_ref = digest(child)
                values[child_ref] = canonical(child)
                parts.append(
                    {"owner": ref, "field": field, "value_ref": child_ref, "record_ref": None}
                )
        return ref

    def _write_plan(self, connection, plan):
        """Insert missing content blocks and immutable plan identity inside the caller's transaction."""
        plan_id = plan.get("plan_id")
        if not isinstance(plan_id, str) or not 1 <= len(plan_id) <= IDENTITY_LENGTH:
            raise ValueError("Invalid archived plan identity")
        stamp = datetime.fromisoformat(plan["generated_at"])
        if stamp.utcoffset() is None:
            raise ValueError("Archived plan time requires a timezone")
        values, records, parts = {}, set(), []
        ref = self._pack(plan, values, records, parts, nested_field="input_snapshot")
        existing = connection.scalar(select(PLANS.c.record_ref).where(PLANS.c.plan_id == plan_id))
        if existing:
            if existing != ref:
                raise ValueError("Archived plan identity is immutable")
            self._write_context(connection, plan)
            return
        known_values = set(
            connection.scalars(select(VALUES.c.digest).where(VALUES.c.digest.in_(values)))
        )
        missing_values = [
            {"digest": key, "payload": encode_payload(value)}
            for key, value in values.items()
            if key not in known_values
        ]
        if missing_values:
            connection.execute(VALUES.insert(), missing_values)
        known_records = set(
            connection.scalars(select(RECORDS.c.digest).where(RECORDS.c.digest.in_(records)))
        )
        missing_records = records - known_records
        if missing_records:
            connection.execute(RECORDS.insert(), [{"digest": key} for key in missing_records])
            rows = [row for row in parts if row["owner"] in missing_records]
            if rows:
                connection.execute(PARTS.insert(), rows)
        connection.execute(
            PLANS.insert().values(
                plan_id=plan_id,
                record_ref=ref,
                generated_at=stamp.astimezone(UTC).isoformat(timespec="microseconds"),
                strategy=plan.get("strategy", "unspecified"),
                solution_status=plan.get("solution_status", "unspecified"),
            )
        )
        self._write_context(connection, plan)

    def _write_context(self, connection, plan):
        """Preserve detached calculation context and permanent controls in the plan's transaction."""
        identity = plan["plan_id"]
        if connection.scalar(
            select(PLAN_CONTEXTS.c.plan_id).where(PLAN_CONTEXTS.c.plan_id == identity)
        ):
            return
        controls = plan.get("forecast_quality_controlled", [])
        context = historical_context(plan)
        connection.execute(
            PLAN_CONTEXTS.insert().values(
                plan_id=identity,
                generated_at=datetime.fromisoformat(plan["generated_at"])
                .astimezone(UTC)
                .isoformat(timespec="microseconds"),
                controls=encode_payload(canonical(controls)),
                controls_hash=digest(controls),
                payload=encode_payload(canonical(context)),
                payload_hash=digest(context),
            )
        )

    def preserve_history(self, *, limit=16):
        """Backfill a bounded batch of original contexts and activation identities before any thinning.

        New publications already write these projections atomically. Old plans
        remain untouched until their context and every activation identity are
        durably preserved. Repeated calls safely resume an interrupted upgrade.
        """
        with lifecycle_transaction(self.engine) as connection:
            missing = connection.scalars(
                select(PLANS.c.plan_id)
                .select_from(
                    PLANS.outerjoin(PLAN_CONTEXTS, PLANS.c.plan_id == PLAN_CONTEXTS.c.plan_id)
                )
                .where(PLAN_CONTEXTS.c.plan_id.is_(None))
                .order_by(PLANS.c.generated_at, PLANS.c.plan_id)
                .limit(limit)
            ).all()
            for identity in missing:
                self._write_context(connection, self._read_plan(connection, identity))
            activations = (
                connection.execute(
                    select(ACTIVATIONS.c.activated_at, ACTIVATIONS.c.plan_id)
                    .select_from(
                        ACTIVATIONS.join(
                            PLAN_CONTEXTS, ACTIVATIONS.c.plan_id == PLAN_CONTEXTS.c.plan_id
                        ).outerjoin(
                            ACTIVATION_CONTEXTS,
                            ACTIVATIONS.c.activated_at == ACTIVATION_CONTEXTS.c.activated_at,
                        )
                    )
                    .where(ACTIVATION_CONTEXTS.c.activated_at.is_(None))
                    .order_by(ACTIVATIONS.c.activated_at)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            if activations:
                connection.execute(ACTIVATION_CONTEXTS.insert(), [dict(row) for row in activations])
        return {"contexts": len(missing), "activations": len(activations)}

    def save(self):
        """Commit a complete archive and active pointer atomically. Restore memory on failed publication."""
        try:
            plan = self.state.get("plan")
            state = {key: value for key, value in self.state.items() if key != "plan"}
            payload = encode_payload(canonical(state))
            with lifecycle_transaction(self.engine) as connection:
                if plan is not None:
                    self._write_plan(connection, plan)
                previous = connection.execute(
                    select(STATE.c.plan_id, STATE.c.payload).where(STATE.c.id == STATE_KEY)
                ).first()
                previous_id = previous.plan_id if previous else None
                previous_enabled = (
                    json.loads(decode_payload(previous.payload)).get("enabled")
                    if previous
                    else None
                )
                if previous_id != (
                    plan["plan_id"] if plan else None
                ) or previous_enabled != self.state.get("enabled"):
                    activated_at = datetime.now(UTC).isoformat(timespec="microseconds")
                    connection.execute(
                        ACTIVATIONS.insert().values(
                            activated_at=activated_at,
                            plan_id=plan["plan_id"] if plan else None,
                            enabled=int(bool(self.state.get("enabled"))),
                            context_hash=digest(plan.get("comparison_bindings", {}))
                            if plan
                            else "",
                        )
                    )
                    if plan:
                        connection.execute(
                            ACTIVATION_CONTEXTS.insert().values(
                                activated_at=activated_at, plan_id=plan["plan_id"]
                            )
                        )
                row = {"payload": payload, "plan_id": plan["plan_id"] if plan else None}
                if connection.scalar(select(STATE.c.id).where(STATE.c.id == STATE_KEY)) is None:
                    connection.execute(STATE.insert().values(id=STATE_KEY, **row))
                else:
                    connection.execute(STATE.update().where(STATE.c.id == STATE_KEY).values(**row))
            self._committed = deepcopy(self.state)
            self.pending_invalidation = False
        except Exception as error:
            if self._committed is not None:
                self.state = deepcopy(self._committed)
                if self.pending_invalidation:
                    self.state["plan"] = None
            if isinstance(error, SQLAlchemyError):
                raise OSError("Archive transaction failed. No new plan was published") from None
            raise

    def invalidate_plan(self):
        """Revoke commands immediately. The async owner durably flushes this flag before responding."""
        if self.state["plan"] is not None:
            self.state["plan"] = None
            self.pending_invalidation = True

    def store_sources(self, configuration):
        """Commit measurement bindings and invalidation together before activating changed sources."""
        self.state = {
            **self.state,
            "measurement_config": configuration_document(configuration),
            "plan": None,
        }
        self.save()

    def _read_record(self, connection, ref, cache, depth=0):
        """Reconstruct exact stored JSON and verify its content hash, never rerunning the planner."""
        if depth >= MAX_RECORD_DEPTH:
            raise ValueError("Archive record nesting invalid")
        if ref in cache:
            return deepcopy(cache[ref])
        rows = (
            connection.execute(
                select(PARTS, VALUES.c.payload)
                .outerjoin(VALUES, VALUES.c.digest == PARTS.c.value_ref)
                .where(PARTS.c.owner == ref)
            )
            .mappings()
            .all()
        )
        value = {}
        for row in rows:
            if row["record_ref"]:
                value[row["field"]] = self._read_record(
                    connection, row["record_ref"], cache, depth + 1
                )
            else:
                payload = row["payload"]
                if payload is None:
                    raise ValueError("Archive input block missing")
                child = json.loads(decode_payload(payload))
                if digest(child) != row["value_ref"]:
                    raise ValueError("Archive input hash mismatch")
                value[row["field"]] = child
        if digest(value) != ref:
            raise ValueError("Archive record hash mismatch")
        cache[ref] = value
        return value

    def _read_plan(self, connection, plan_id):
        """Read by immutable ID, returning a detached document or None for an unknown ID."""
        ref = connection.scalar(select(PLANS.c.record_ref).where(PLANS.c.plan_id == plan_id))
        return self._read_record(connection, ref, {}) if ref else None

    def get_plan(self, plan_id):
        """Load an original archived plan independently of current validity/configuration."""
        try:
            with self.engine.connect() as connection:
                return self._read_plan(connection, plan_id)
        except SQLAlchemyError:
            raise OSError("Archive read failed") from None

    def contract_versions(self, plan_ids):
        """Read hash-checked contract leaves without reconstructing physical or forecast inputs.

        This projection verifies the selected leaf, not the complete plan record.
        Absent optional versions return None and still require full repricing.
        Missing plans and corrupt leaves fail closed.
        """
        identities = set(plan_ids)
        if not identities:
            return {}
        snapshots, contracts = PARTS.alias("snapshots"), PARTS.alias("contracts")
        query = (
            select(PLANS.c.plan_id, contracts.c.field, contracts.c.value_ref, VALUES.c.payload)
            .select_from(
                PLANS.outerjoin(
                    snapshots,
                    (snapshots.c.owner == PLANS.c.record_ref)
                    & (snapshots.c.field == "input_snapshot"),
                )
                .outerjoin(
                    contracts,
                    (contracts.c.owner == snapshots.c.record_ref)
                    & (contracts.c.field == "contract_version"),
                )
                .outerjoin(VALUES, VALUES.c.digest == contracts.c.value_ref)
            )
            .where(PLANS.c.plan_id.in_(identities))
        )
        versions = {}
        try:
            with self.engine.connect() as connection:
                for row in connection.execute(query):
                    version = None
                    if row.field is not None:
                        if row.payload is None:
                            raise ValueError("Archive contract block missing")
                        version = json.loads(decode_payload(row.payload))
                        if digest(version) != row.value_ref:
                            raise ValueError("Archive contract hash mismatch")
                        if version is not None and not isinstance(version, str):
                            raise ValueError("Archive contract version invalid")
                    versions[row.plan_id] = version
        except SQLAlchemyError:
            raise OSError("Archive contract read failed") from None
        if versions.keys() != identities:
            raise ValueError("Archive plan missing")
        return versions

    def get_context(self, plan_id):
        """Read a hash-verified historical calculation projection without presenting it as an original plan."""
        try:
            query = select(PLAN_CONTEXTS).where(PLAN_CONTEXTS.c.plan_id == plan_id)
            with self.engine.connect() as connection:
                row = connection.execute(query).mappings().first()
            if row is None:
                with lifecycle_transaction(self.engine) as connection:
                    # A competing lazy reader or backfill may have created it while we waited.
                    row = connection.execute(query).mappings().first()
                    if row is None:
                        plan = self._read_plan(connection, plan_id)
                        if plan is None:
                            return None
                        self._write_context(connection, plan)
                        return historical_context(plan)
            if row["payload"] is None:
                return None
            context = json.loads(decode_payload(row["payload"]))
            if digest(context) != row["payload_hash"]:
                raise ValueError("Historical calculation context hash mismatch")
            return context
        except SQLAlchemyError:
            raise OSError("Historical calculation archive unavailable") from None

    def controlled_entities(self, plan_ids):
        """Read only hash-checked control lists for published plans, without loading solver inputs.

        Missing plans or corrupt control blocks fail closed. Older plans without
        this optional field contribute no controls, as in full-plan reads. This
        projection verifies the selected leaf, not the complete plan record.
        """
        identities = set(plan_ids)
        if not identities:
            return []
        query = (
            select(PLANS.c.plan_id, PARTS.c.field, PARTS.c.value_ref, VALUES.c.payload)
            .select_from(
                PLANS.outerjoin(
                    PARTS,
                    (PARTS.c.owner == PLANS.c.record_ref)
                    & (PARTS.c.field == "forecast_quality_controlled"),
                ).outerjoin(VALUES, VALUES.c.digest == PARTS.c.value_ref)
            )
            .where(PLANS.c.plan_id.in_(identities))
        )
        controlled, found, checked = set(), set(), set()
        try:
            with self.engine.connect() as connection:
                for row in connection.execute(query):
                    found.add(row.plan_id)
                    if row.field is None:
                        continue
                    if row.payload is None:
                        raise ValueError("Archive control block missing")
                    if row.value_ref in checked:
                        continue
                    entities = json.loads(decode_payload(row.payload))
                    if digest(entities) != row.value_ref:
                        raise ValueError("Archive control hash mismatch")
                    if not isinstance(entities, list) or any(
                        not isinstance(entity, str) for entity in entities
                    ):
                        raise ValueError("Archive control list invalid")
                    controlled.update(entities)
                    checked.add(row.value_ref)
        except SQLAlchemyError:
            raise OSError("Archive control read failed") from None
        if found != identities:
            try:
                with self.engine.connect() as connection:
                    for row in connection.execute(
                        select(PLAN_CONTEXTS).where(PLAN_CONTEXTS.c.plan_id.in_(identities - found))
                    ).mappings():
                        entities = json.loads(decode_payload(row["controls"]))
                        if digest(entities) != row["controls_hash"]:
                            raise ValueError("Archive control hash mismatch")
                        if not isinstance(entities, list) or any(
                            not isinstance(entity, str) for entity in entities
                        ):
                            raise ValueError("Archive control list invalid")
                        controlled.update(entities)
                        found.add(row["plan_id"])
            except SQLAlchemyError:
                raise OSError("Archive control read failed") from None
            if found != identities:
                raise ValueError("Controlled-consumer publication evidence unavailable")
        return sorted(controlled)

    def day_plan(self, start, end):
        """Return the first original publication in a local-day window, matching the permanent daily anchor."""
        try:
            with self.engine.connect() as connection:
                return connection.scalar(
                    select(PLANS.c.plan_id)
                    .where(
                        PLANS.c.generated_at
                        >= start.astimezone(UTC).isoformat(timespec="microseconds"),
                        PLANS.c.generated_at
                        < end.astimezone(UTC).isoformat(timespec="microseconds"),
                    )
                    .order_by(PLANS.c.generated_at, PLANS.c.plan_id)
                    .limit(1)
                )
        except SQLAlchemyError:
            raise OSError("Archive history unavailable") from None

    def list_plans(self, *, limit=DEFAULT_HISTORY_LIMIT, before=None):
        """List newest-first summaries using bounded keyset pagination, without loading full plans."""
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValueError("Invalid plan history limit")
        try:
            with self.engine.connect() as connection:
                query = select(
                    PLANS.c.plan_id, PLANS.c.generated_at, PLANS.c.strategy, PLANS.c.solution_status
                )
                if before is not None:
                    cursor = (
                        connection.execute(select(PLANS).where(PLANS.c.plan_id == before))
                        .mappings()
                        .first()
                    )
                    if cursor is None:
                        cursor = (
                            connection.execute(
                                select(PLAN_CONTEXTS).where(PLAN_CONTEXTS.c.plan_id == before)
                            )
                            .mappings()
                            .first()
                        )
                        if cursor is None:
                            raise ValueError("Unknown history cursor")
                    query = query.where(
                        (PLANS.c.generated_at < cursor["generated_at"])
                        | (
                            (PLANS.c.generated_at == cursor["generated_at"])
                            & (PLANS.c.plan_id < cursor["plan_id"])
                        )
                    )
                rows = connection.execute(
                    query.order_by(PLANS.c.generated_at.desc(), PLANS.c.plan_id.desc()).limit(limit)
                ).mappings()
                return [dict(row) for row in rows]
        except SQLAlchemyError:
            raise OSError("Archive history unavailable") from None
