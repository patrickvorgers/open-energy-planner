"""Bounded, restartable plan thinning after independent historical evidence is durable."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from open_energy_planner.core.calendar import day_window
from open_energy_planner.planning_policy import FINE_INTERVAL_MINUTES
from open_energy_planner.storage.adapters.payloads import (
    COMPRESSED_PREFIX,
    COMPRESSION_MIN_BYTES,
    MAX_COMPRESSED_JSON_BYTES,
    decode_payload,
    encode_payload,
)

from .evidence import activation_history
from .schema import (
    ACTIVATION_CONTEXTS,
    ACTIVATIONS,
    PARTS,
    PLAN_CONTEXTS,
    PLANS,
    RECORDS,
    SAVINGS_DAYS,
    SCHEMA,
    SETTLEMENT_EVALUATIONS,
    SETTLEMENT_FRAMES,
    STATE,
    VALUES,
)
from .sqlalchemy import MAX_RECORD_DEPTH, digest

ALL_PLANS_HOURS = 24
QUARTER_PLANS_DAYS = 3
HOURLY_PLANS_DAYS = 14
RETENTION_BATCH_PLANS = 32
COMPRESSION_BATCH_ROWS = 16
GC_BATCH_ROWS = 128


def bucket(instant, now, timezone):
    """Choose a UTC quarter/hour or HA local calendar day. DST repeated hours remain distinct."""
    age = now - instant
    if age < timedelta(days=QUARTER_PLANS_DAYS):
        start = instant.replace(
            minute=instant.minute // FINE_INTERVAL_MINUTES * FINE_INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        )
        return start, start + timedelta(minutes=FINE_INTERVAL_MINUTES)
    if age < timedelta(days=HOURLY_PLANS_DAYS):
        start = instant.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    return day_window(instant.astimezone(ZoneInfo(timezone)).date().isoformat(), timezone)


def stamp(instant):
    """Use the archive's fixed-width UTC comparison keys."""
    return instant.astimezone(UTC).isoformat(timespec="microseconds")


def collect_blocks(connection):
    """Delete only unreachable shared records and leaves, in bounded parent-before-child batches."""
    removed = 0
    for _level in range(MAX_RECORD_DEPTH):
        identities = connection.scalars(
            select(RECORDS.c.digest)
            .where(
                ~select(PLANS.c.plan_id).where(PLANS.c.record_ref == RECORDS.c.digest).exists(),
                ~select(PARTS.c.owner).where(PARTS.c.record_ref == RECORDS.c.digest).exists(),
            )
            .order_by(RECORDS.c.digest)
            .limit(GC_BATCH_ROWS)
        ).all()
        if not identities:
            break
        connection.execute(PARTS.delete().where(PARTS.c.owner.in_(identities)))
        connection.execute(RECORDS.delete().where(RECORDS.c.digest.in_(identities)))
        removed += len(identities)
    leaves = connection.scalars(
        select(VALUES.c.digest)
        .where(~select(PARTS.c.owner).where(PARTS.c.value_ref == VALUES.c.digest).exists())
        .order_by(VALUES.c.digest)
        .limit(GC_BATCH_ROWS)
    ).all()
    if leaves:
        connection.execute(VALUES.delete().where(VALUES.c.digest.in_(leaves)))
    return removed + len(leaves)


def thin(plans, documents, now, timezone):
    """Retain the first plan in each age bucket and the active plan, with a durable bounded cursor.

    The caller serializes maintenance with financial work. A context and every
    logical activation must survive before their full original plan can expire.
    No raw financial evidence expires before its day has been finalized.
    """
    plans.preserve_history(limit=RETENTION_BATCH_PLANS)
    progress = documents.read("retention.json") if documents.exists("retention.json") else {}
    deleted = 0
    with documents.transaction():
        connection = documents.pending.connection
        query = select(PLANS).where(
            PLANS.c.generated_at < stamp(now - timedelta(hours=ALL_PLANS_HOURS))
        )
        if progress.get("after"):
            generated, identity = progress["after"]
            query = query.where(
                (PLANS.c.generated_at > generated)
                | ((PLANS.c.generated_at == generated) & (PLANS.c.plan_id > identity))
            )
        rows = (
            connection.execute(
                query.order_by(PLANS.c.generated_at, PLANS.c.plan_id).limit(RETENTION_BATCH_PLANS)
            )
            .mappings()
            .all()
        )
        for row in rows:
            identity = row["plan_id"]
            left, right = bucket(datetime.fromisoformat(row["generated_at"]), now, timezone)
            keeper = connection.scalar(
                select(PLANS.c.plan_id)
                .where(PLANS.c.generated_at >= stamp(left), PLANS.c.generated_at < stamp(right))
                .order_by(PLANS.c.generated_at, PLANS.c.plan_id)
                .limit(1)
            )
            active = connection.scalar(select(STATE.c.plan_id).where(STATE.c.plan_id == identity))
            if identity == keeper or active:
                continue
            context = (
                connection.execute(select(PLAN_CONTEXTS).where(PLAN_CONTEXTS.c.plan_id == identity))
                .mappings()
                .first()
            )
            if context is None:
                continue
            # Verify copied evidence before the irreversible removal of its original.
            if context["payload"] is not None and (
                digest(json.loads(decode_payload(context["payload"]))) != context["payload_hash"]
            ):
                raise ValueError("Historical calculation context hash mismatch")
            unmapped = connection.scalar(
                select(ACTIVATIONS.c.activated_at)
                .where(
                    ACTIVATIONS.c.plan_id == identity,
                    ~select(ACTIVATION_CONTEXTS.c.activated_at)
                    .where(
                        ACTIVATION_CONTEXTS.c.activated_at == ACTIVATIONS.c.activated_at,
                        ACTIVATION_CONTEXTS.c.plan_id == identity,
                    )
                    .exists(),
                )
                .limit(1)
            )
            if unmapped is not None:
                continue
            connection.execute(
                ACTIVATIONS.update().where(ACTIVATIONS.c.plan_id == identity).values(plan_id=None)
            )
            connection.execute(PLANS.delete().where(PLANS.c.plan_id == identity))
            deleted += 1
        garbage_pending = bool(deleted or progress.get("garbage_pending"))
        if garbage_pending:
            garbage_pending = collect_blocks(connection) > 0
        documents.write(
            "retention.json",
            {
                "after": [rows[-1]["generated_at"], rows[-1]["plan_id"]]
                if len(rows) == RETENTION_BATCH_PLANS
                else None,
                "garbage_pending": garbage_pending,
            },
        )
        sealed = (
            connection.execute(
                select(SAVINGS_DAYS)
                .where(SAVINGS_DAYS.c.finalized_at.is_not(None))
                .order_by(SAVINGS_DAYS.c.start.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if sealed:
            report = json.loads(decode_payload(sealed["payload"]))
            if digest(report) != sealed["payload_hash"] or report.get("finalized_at") is None:
                raise ValueError("Daily savings archive hash mismatch")
            # The verified report, not a mutable lookup column, authorizes detail expiry.
            expire_evidence(connection, report["end"])
    return deleted


def expire_evidence(connection, before):
    """Remove bounded temporary quarters and projections only behind an immutable daily checkpoint."""
    boundary = stamp(datetime.fromisoformat(before))
    frames = connection.scalars(
        select(SETTLEMENT_FRAMES.c.start)
        .where(SETTLEMENT_FRAMES.c.end <= boundary)
        .order_by(SETTLEMENT_FRAMES.c.start)
        .limit(GC_BATCH_ROWS)
    ).all()
    if frames:
        connection.execute(
            SETTLEMENT_EVALUATIONS.delete().where(SETTLEMENT_EVALUATIONS.c.start.in_(frames))
        )
        connection.execute(SETTLEMENT_FRAMES.delete().where(SETTLEMENT_FRAMES.c.start.in_(frames)))
    # The installation/advice already active at the boundary can cover a later day.
    history = activation_history()
    preceding = connection.scalar(
        select(history.c.plan_id)
        .where(history.c.activated_at <= boundary, history.c.plan_id.is_not(None))
        .order_by(history.c.activated_at.desc())
        .limit(1)
    )
    protected = select(history.c.plan_id).where(
        history.c.activated_at >= boundary, history.c.plan_id.is_not(None)
    )
    identities = connection.scalars(
        select(PLAN_CONTEXTS.c.plan_id)
        .where(
            PLAN_CONTEXTS.c.generated_at < boundary,
            PLAN_CONTEXTS.c.payload.is_not(None),
            ~PLAN_CONTEXTS.c.plan_id.in_(protected),
            ~PLAN_CONTEXTS.c.plan_id.in_(
                select(STATE.c.plan_id).where(STATE.c.plan_id.is_not(None))
            ),
            PLAN_CONTEXTS.c.plan_id != preceding if preceding else True,
        )
        .order_by(PLAN_CONTEXTS.c.generated_at)
        .limit(GC_BATCH_ROWS)
    ).all()
    if identities:
        connection.execute(
            PLAN_CONTEXTS.update()
            .where(PLAN_CONTEXTS.c.plan_id.in_(identities))
            .values(payload=None)
        )


def recompress(documents):
    """Losslessly rewrite a bounded existing JSON batch with a restartable table/primary-key cursor.

    Plain archives remain readable throughout. Hashes refer to decoded originals.
    Oversized or incompressible originals are skipped without starving later rows.
    """
    progress = documents.read("compression.json") if documents.exists("compression.json") else {}
    completed = progress.get("completed", [])
    tables = [
        table
        for table in SCHEMA.sorted_tables
        if "payload" in table.c and table.name not in completed
    ]
    if not tables:
        return 0
    table = next((table for table in tables if table.name == progress.get("table")), tables[0])
    keys = list(table.primary_key.columns)
    with documents.transaction():
        connection = documents.pending.connection
        query = select(*keys).where(
            table.c.payload.is_not(None),
            ~table.c.payload.like(COMPRESSED_PREFIX + "%"),
            func.length(table.c.payload) >= COMPRESSION_MIN_BYTES,
            func.length(table.c.payload) <= MAX_COMPRESSED_JSON_BYTES,
        )
        cursor = progress.get("after") if progress.get("table") == table.name else None
        if cursor:
            from sqlalchemy import and_, or_

            query = query.where(
                or_(
                    *(
                        and_(
                            *(keys[prior] == cursor[prior] for prior in range(position)),
                            key > cursor[position],
                        )
                        for position, key in enumerate(keys)
                    )
                )
            )
        rows = (
            connection.execute(query.order_by(*keys).limit(COMPRESSION_BATCH_ROWS)).mappings().all()
        )
        changed = 0
        for row in rows:
            key_match = [key == row[key.name] for key in keys]
            stored = connection.scalar(select(table.c.payload).where(*key_match))
            if stored is None:
                continue
            original = decode_payload(stored)
            compressed = encode_payload(original)
            if compressed != stored:
                if decode_payload(compressed) != original:
                    raise ValueError("Lossless archive compression verification failed")
                result = connection.execute(
                    table.update()
                    .where(
                        *key_match,
                        (
                            table.c.payload.collate("utf8mb4_bin")
                            if connection.dialect.name in ("mysql", "mariadb")
                            else table.c.payload
                        )
                        == stored,
                    )
                    .values(payload=compressed)
                )
                changed += result.rowcount
        documents.write(
            "compression.json",
            {
                "table": table.name if len(rows) == COMPRESSION_BATCH_ROWS else None,
                "after": [rows[-1][key.name] for key in keys]
                if len(rows) == COMPRESSION_BATCH_ROWS
                else None,
                # New writes already use the codec. Finished tables need no repeated payload scan.
                "completed": completed
                if len(rows) == COMPRESSION_BATCH_ROWS
                else [*completed, table.name],
            },
        )
    return changed
